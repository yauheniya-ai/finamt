"""
examples/process_receipt.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Process a single receipt or all receipts in a folder.
Accepts PDF, PNG, JPG, JPEG, TIFF, BMP, WEBP.
Results are automatically saved to the local DB.

Usage
-----
    python -m examples.process_receipt                          # all receipts in examples/receipts/
    python -m examples.process_receipt --file receipt1.pdf      # single file
    python -m examples.process_receipt --input-dir my/scans/
    python -m examples.process_receipt --file invoice1.pdf --type sale
    python -m examples.process_receipt --output-dir results/    # also save JSON
    python -m examples.process_receipt --db /tmp/test.db

Output layout (default project)
-------------------------------
    ~/.finamt/default/
        finamt.db           ← SQLite database
        pdfs/               ← archived copy of every receipt file
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
from pathlib import Path

logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s — %(message)s")

from finamt import FinanceAgent
from finamt.storage.project import layout_from_db_path
from finamt.storage.sqlite import DEFAULT_DB_PATH


SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".webp"}


def _collect_files(input_dir: Path, file_name: str | None) -> list[Path]:
    """Return an ordered list of receipt files to process."""
    if file_name:
        candidate = input_dir / file_name
        # also try appending .pdf if no extension given
        if not candidate.exists() and "." not in file_name:
            candidate = input_dir / f"{file_name}.pdf"
        if not candidate.exists():
            print(f"[error] File not found: {candidate}", file=sys.stderr)
            return []
        return [candidate]
    files = sorted(
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )
    if not files:
        print(f"[error] No supported files found in {input_dir}", file=sys.stderr)
    return files


def process_receipt(
    receipt_path: Path,
    output_dir: Path | None = None,
    db_path: Path | None = None,       # None → use default ~/.finamt/default/finamt.db
    no_db: bool = False,               # True → disable persistence entirely
    receipt_type: str = "purchase",
) -> bool:
    if not receipt_path.exists():
        print(f"[error] File not found: {receipt_path}", file=sys.stderr)
        return False

    print(f"Processing: {receipt_path}")

    # db_path=None → disabled; explicit path or DEFAULT_DB_PATH → persist
    resolved_db = None if no_db else (db_path if db_path else DEFAULT_DB_PATH)
    agent = FinanceAgent(db_path=resolved_db)
    result = agent.process_receipt(receipt_path, receipt_type=receipt_type)

    if result.duplicate:
        print(f"\n  ⚠  Duplicate detected — this receipt was already processed.")
        print(f"     Existing ID : {result.existing_id}")
        print(f"     Vendor      : {result.data.counterparty.name if result.data and result.data.counterparty else '—'}")
        print(f"     No changes made to the database.\n")
        return True

    if not result.success:
        print(f"[error] Extraction failed: {result.error_message}", file=sys.stderr)
        return False

    data = result.data

    # ------------------------------------------------------------------
    # Print summary
    # ------------------------------------------------------------------
    W = 44
    print("\n" + "─" * W)
    print(f"  {'EXTRACTION RESULT':^{W - 4}}")
    print("─" * W)

    cp = data.counterparty

    def row(label: str, value: object) -> None:
        print(f"  {label:<18} {str(value) if value is not None else '—'}")

    row("Type",         str(data.receipt_type).upper())
    row("Counterparty", cp.name if cp else None)
    if cp and cp.address:
        row("Address",  str(cp.address))
    if cp and cp.vat_id:
        row("VAT ID",   cp.vat_id)
    row("Receipt #",    data.receipt_number)
    row("Date",         data.receipt_date.date() if data.receipt_date else None)
    row("Category",     str(data.category))
    print("  " + "·" * (W - 4))
    row("Total",        f"{data.total_amount} EUR"   if data.total_amount   else None)
    row("VAT %",        f"{data.vat_percentage} %"   if data.vat_percentage else None)
    row("VAT amount",   f"{data.vat_amount} EUR"     if data.vat_amount     else None)
    row("Net",          f"{data.net_amount} EUR"     if data.net_amount     else None)

    if data.items:
        print("  " + "·" * (W - 4))
        print(f"  {'Items':<18}")
        for item in data.items:
            price = f"{item.total_price} EUR" if item.total_price else "—"
            print(f"    • {item.description[:28]:<28}  {price}  [{item.category}]")

    print("─" * W)
    print(f"  Processing time : {result.processing_time:.2f}s")
    print(f"  ID (hash)       : {data.id[:16]}…")

    if no_db:
        print("  DB persistence  : disabled")
    else:
        resolved_db_display = db_path or DEFAULT_DB_PATH
        layout = layout_from_db_path(Path(resolved_db_display))
        actual_db    = layout.db_path
        actual_debug = layout.debug_dir
        data_missing = (
            not data.total_amount and not data.receipt_date and not data.counterparty
        )
        print(f"  Saved to DB     : {actual_db}")
        print(f"  LLM debug logs  : {actual_debug}/")
        if data_missing:
            print(f"  \u26a0  No data extracted \u2014 inspect debug output:")
            print(f"     {actual_debug}/")
            print(f"     Tip: cat '{actual_debug}/<receipt-id>/agent1_raw.txt'")

    # ------------------------------------------------------------------
    # Optionally save JSON
    # ------------------------------------------------------------------
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_path = output_dir / f"{receipt_path.stem}_extracted.json"
        out_path.write_text(
            json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Saved JSON      : {out_path}")

    print()
    return True


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Process receipt files (PDF/PNG/JPG/…); auto-saves to DB.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--file",       default=None,               metavar="FILENAME",
                   help="Single file name (with or without extension). Omit to process all files in --input-dir.")
    p.add_argument("--input-dir",  default="examples/receipts", metavar="DIR")
    p.add_argument("--output-dir", default=None,               metavar="DIR",
                   help="Also write extracted JSON here (optional).")
    p.add_argument("--type",       default="purchase",         choices=["purchase", "sale"],
                   help="purchase = Eingangsrechnung; sale = Ausgangsrechnung.")
    p.add_argument("--db",         default=None,               metavar="FILE",
                   help="SQLite DB path (default: ~/.finamt/default/finamt.db).")
    p.add_argument("--no-db",      action="store_true",
                   help="Disable DB persistence (JSON extraction only).")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    input_dir  = Path(args.input_dir)
    output_dir = Path(args.output_dir) if args.output_dir else None
    db_path    = Path(args.db) if args.db else None

    files = _collect_files(input_dir, args.file)
    if not files:
        sys.exit(1)

    failed = 0
    for receipt_path in files:
        ok = process_receipt(
            receipt_path=receipt_path,
            output_dir=output_dir,
            db_path=db_path,
            no_db=args.no_db,
            receipt_type=args.type,
        )
        if not ok:
            failed += 1

    sys.exit(1 if failed else 0)