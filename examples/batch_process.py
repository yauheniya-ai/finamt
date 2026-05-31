"""
examples/batch_process.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Batch-process all receipts (PDF, PNG, JPG, TIFF) in a directory.
Results are automatically saved to the local DB — no explicit save needed.

Usage
-----
    python -m examples.batch_process
    python -m examples.batch_process --input-dir examples/receipts --output-dir results/
    python -m examples.batch_process --type sale          # all are sales invoices
    python -m examples.batch_process --db /tmp/test.db
    python -m examples.batch_process --no-db              # JSON only, no DB

Output layout (default project)
-------------------------------
    ~/.finamt/default/
        finamt.db           ← SQLite database
        pdfs/               ← archived copies of every receipt PDF
        debug/
            <receipt-id>/   ← one folder per receipt (SHA-256 prefix)
                agent1_prompt.txt   agent1_raw.txt   agent1_parsed.json
                agent2_prompt.txt   agent2_raw.txt   agent2_parsed.json
                agent3_prompt.txt   agent3_raw.txt   agent3_parsed.json
                agent4_prompt.txt   agent4_raw.txt   agent4_parsed.json

If an agent returns no data, inspect the corresponding *_raw.txt file to
see the raw model output — this is the first place to look for prompt or
parsing issues.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Dict

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)

from finamt import FinanceAgent
from finamt.models import ExtractionResult
from finamt.storage.project import layout_from_db_path, resolve_project
from finamt.storage.sqlite import DEFAULT_DB_PATH


def process_receipts(
    input_dir: Path,
    output_dir: Path | None = None,
    db_path: Path | None = None,
    no_db: bool = False,
    receipt_type: str = "purchase",
) -> Dict[str, ExtractionResult]:
    """
    Process all receipts (PDF, PNG, JPG, TIFF) in *input_dir*.

    DB save happens automatically inside FinanceAgent.
    JSON files are written to *output_dir* only if it is specified.
    """
    pdf_files = sorted(
        f
        for pattern in ("*.pdf", "*.png", "*.jpg", "*.jpeg", "*.tiff", "*.tif")
        for f in input_dir.glob(pattern)
    )
    pdf_files = sorted(set(pdf_files))
    if not pdf_files:
        logging.warning("No receipt files found in %s", input_dir.resolve())
        return {}

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    resolved_db = None if no_db else (db_path if db_path else DEFAULT_DB_PATH)
    agent = FinanceAgent(db_path=resolved_db)
    results: Dict[str, ExtractionResult] = {}

    for pdf_path in pdf_files:
        logging.info("Processing %s …", pdf_path.name)
        result = agent.process_receipt(pdf_path, receipt_type=receipt_type)
        results[str(pdf_path)] = result

        if output_dir:
            json_path = output_dir / f"{pdf_path.stem}_extracted.json"
            json_path.write_text(
                json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    return results


def generate_report(
    results: Dict[str, ExtractionResult],
    db_path: Path | None = None,
    no_db: bool = False,
) -> None:
    if not results:
        print("No receipts processed.")
        return

    # ── Resolve actual output paths ───────────────────────────────────────
    effective_db = db_path or DEFAULT_DB_PATH
    try:
        layout = layout_from_db_path(effective_db)
        actual_db    = layout.db_path
        actual_pdfs  = layout.pdfs_dir
        actual_debug = layout.debug_dir
    except Exception:
        actual_db    = effective_db
        actual_pdfs  = effective_db.parent / "pdfs"
        actual_debug = effective_db.parent / "debug"

    successful  = [r for r in results.values() if r.success and not r.duplicate]
    duplicates  = [r for r in results.values() if r.duplicate]
    failed      = [r for r in results.values() if not r.success]

    total_amount: Decimal = Decimal(0)
    total_vat:    Decimal = Decimal(0)
    by_category:  dict[str, Decimal] = defaultdict(Decimal)

    for result in successful:
        d = result.data
        if d.total_amount:
            total_amount += d.total_amount
        if d.vat_amount:
            total_vat += d.vat_amount
        if d.category:
            by_category[str(d.category)] += d.total_amount or Decimal(0)

    W    = 52
    div  = "─" * W
    hdiv = "═" * W
    rate = len(successful) / len(results) if results else 0

    print(f"\n{hdiv}")
    print(f"  {'BATCH PROCESSING REPORT':^{W - 4}}")
    print(hdiv)
    print(f"  Total receipts  : {len(results)}")
    print(f"  Newly saved     : {len(successful)}")
    print(f"  Duplicates      : {len(duplicates)}")
    print(f"  Failed          : {len(failed)}")
    print(f"  Success rate    : {rate:.0%}")

    print(f"\n  {'FINANCIALS (new receipts only)':^{W - 4}}")
    print(div)
    print(f"  Total expenses  : {total_amount:.2f} EUR")
    print(f"  Total VAT       : {total_vat:.2f} EUR")
    print(f"  Net (excl.VAT)  : {total_amount - total_vat:.2f} EUR")

    if by_category:
        print(f"\n  {'BY CATEGORY':^{W - 4}}")
        print(div)
        for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
            print(f"  {cat:<24} {amt:>10.2f} EUR")

    if duplicates:
        print(f"\n  {'DUPLICATES (already in DB)':^{W - 4}}")
        print(div)
        for result in duplicates:
            d = result.data
            cp_name = d.counterparty.name if d and d.counterparty else "—"
            print(f"  ⚠  {cp_name}  (id: {result.existing_id[:16]}…)")

    print(f"\n  {'DETAIL':^{W - 4}}")
    print(div)
    for path, result in results.items():
        name = Path(path).name
        if result.duplicate:
            print(f"\n  ⚠  {name}  [duplicate — skipped]")
        elif result.success:
            d   = result.data
            amt = f"{d.total_amount:.2f} EUR" if d.total_amount else "—"
            dt  = d.receipt_date.date() if d.receipt_date else "—"
            vat = f"{d.vat_percentage}%" if d.vat_percentage else "—"
            t   = f"{result.processing_time:.1f}s" if result.processing_time else ""
            cp  = d.counterparty.name if d.counterparty else "—"
            data_missing = not d.total_amount and not d.receipt_date and not d.counterparty
            print(f"\n  ✓  {name}  ({t})")
            print(f"     {str(d.receipt_type).upper():<10} {cp}")
            print(f"     Date     : {dt}   Total: {amt}   VAT: {vat}")
            print(f"     Category : {d.category}   Items: {len(d.items)}")
            if data_missing and not no_db:
                print(f"     ⚠  No data extracted — inspect debug output:")
                print(f"        {actual_debug}/")
        else:
            print(f"\n  ✗  {name}")
            print(f"     Error: {result.error_message}")

    print(f"\n{hdiv}")
    if no_db:
        print(f"  DB persistence : disabled")
    else:
        print(f"  Database       : {actual_db}")
        print(f"  PDFs archived  : {actual_pdfs}/")
        print(f"  LLM debug logs : {actual_debug}/")
        print(f"  Tip: cat '{actual_debug}/<receipt-id>/agent1_raw.txt'")
    print(f"{hdiv}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Batch-process receipt files (auto-saves to DB).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--input-dir",  default="examples/receipts", metavar="DIR")
    p.add_argument("--output-dir", default=None,               metavar="DIR",
                   help="Also write JSON result files here (optional).")
    p.add_argument("--type",       default="purchase",         choices=["purchase", "sale"])
    p.add_argument("--db",         default=None,               metavar="FILE",
                   help="SQLite DB path (default: ~/.finamt/finamt.db).")
    p.add_argument("--no-db",      action="store_true",
                   help="Disable DB persistence.")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main() -> int:
    args      = _build_parser().parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    input_dir  = Path(args.input_dir).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    db_path    = Path(args.db) if args.db else None

    if not input_dir.exists():
        logging.error("Input directory not found: %s", input_dir)
        return 1

    try:
        results = process_receipts(
            input_dir, output_dir, db_path,
            no_db=args.no_db, receipt_type=args.type,
        )
        generate_report(results, db_path, no_db=args.no_db)
        failed = sum(1 for r in results.values() if not r.success and not r.duplicate)
        return 1 if failed else 0
    except Exception:
        logging.exception("Batch processing failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())