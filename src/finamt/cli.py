"""
finamt.cli
~~~~~~~~~~~~~
Command-line interface for finamt.

Entry point registered in pyproject.toml::

    [project.scripts]
    finamt = "finamt.cli:main"

Usage examples
--------------
    finamt --version

    # Single receipt
    finamt process receipt1 --input-dir receipts/

    # Batch
    finamt batch --input-dir receipts/ --output-dir results/

    # Scan receipts into local DB, then generate Q1 UStVA report
    finamt ustva --input-dir receipts/ --quarter 1 --year 2024

    # Generate report from already-stored receipts
    finamt ustva --quarter 1 --year 2024 --output ustva_q1.json

    # Use a custom DB path
    finamt ustva --input-dir receipts/ --db /tmp/mydb.db

    # Start the web UI
    finamt serve --port 8000
"""

from __future__ import annotations

import warnings
try:
    from requests.exceptions import RequestsDependencyWarning
    warnings.filterwarnings("ignore", category=RequestsDependencyWarning)
except ImportError:
    pass

import json
import logging
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from importlib.metadata import version
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from typing_extensions import Annotated

from finamt import FinanceAgent
from finamt.storage.sqlite import DEFAULT_DB_PATH
from finamt.storage import get_repository
from finamt.tax.ustva import generate_ustva

# ---------------------------------------------------------------------------
# CLI class
# ---------------------------------------------------------------------------

class FinamtCLI:

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def print_version(self) -> None:
        try:
            print(f"finamt version: {version('finamt')}")
        except Exception:
            print("finamt version: unknown")

    # ------------------------------------------------------------------
    # Single receipt
    # ------------------------------------------------------------------

    def process_receipt(
        self,
        file_stem: str,
        input_dir: str | Path,
        output_dir: str | Path | None = None,
        verbose: bool = False,
        receipt_type: str = "purchase",
        db_path: Path | None = None,
        no_db: bool = False,
    ) -> int:
        """Process one receipt PDF. DB save is automatic. Returns exit code."""
        receipt_path = Path(input_dir) / f"{file_stem}.pdf"
        if not receipt_path.exists():
            print(f"[error] File not found: {receipt_path}", file=sys.stderr)
            return 1

        if verbose:
            print(f"Processing: {receipt_path}")

        agent = FinanceAgent(db_path=None if no_db else (db_path if db_path else DEFAULT_DB_PATH))
        result = agent.process_receipt(receipt_path, receipt_type=receipt_type)

        if result.duplicate:
            d = result.data
            cp = d.counterparty.name if d and d.counterparty else "—"
            print(f"⚠  Duplicate — already in DB: {cp}  (id: {result.existing_id[:16]}…)")
            return 0

        if result.success:
            d = result.data
            cp = d.counterparty.name if d.counterparty else "—"
            print(f"✓  {str(d.receipt_type).upper()}  {cp}  {d.total_amount} EUR")
            if output_dir:
                out_dir = Path(output_dir)
                out_dir.mkdir(parents=True, exist_ok=True)
                out = out_dir / f"{file_stem}_extracted.json"
                out.write_text(result.data.to_json(), encoding="utf-8")
                print(f"   JSON → {out}")
            return 0
        else:
            print(f"✗  Extraction failed: {result.error_message}", file=sys.stderr)
            return 1

    # ------------------------------------------------------------------
    # Batch
    # ------------------------------------------------------------------

    def batch_process(
        self,
        input_dir: str | Path,
        output_dir: str | Path | None = None,
        verbose: bool = False,
        receipt_type: str = "purchase",
        db_path: Path | None = None,
        no_db: bool = False,
    ) -> int:
        """Batch-process all PDFs. DB save is automatic. Returns exit code."""
        input_dir = Path(input_dir)
        out_dir   = Path(output_dir) if output_dir else None
        pdf_files = sorted(input_dir.glob("*.pdf"))

        if not pdf_files:
            print(f"No PDF files found in {input_dir.resolve()}")
            return 1

        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)

        agent   = FinanceAgent(db_path=None if no_db else (db_path if db_path else DEFAULT_DB_PATH))
        results = {}

        for pdf_path in pdf_files:
            if verbose:
                print(f"Processing {pdf_path.name} ...")
            result = agent.process_receipt(pdf_path, receipt_type=receipt_type)
            results[str(pdf_path)] = result
            if out_dir:
                json_path = out_dir / f"{pdf_path.stem}_extracted.json"
                json_path.write_text(
                    json.dumps(result.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

        self._print_batch_report(results)
        failed = sum(1 for r in results.values() if not r.success and not r.duplicate)
        return 1 if failed else 0

    def _print_batch_report(self, results: dict) -> None:
        successful = [r for r in results.values() if r.success and not r.duplicate]
        duplicates = [r for r in results.values() if r.duplicate]
        failed     = [r for r in results.values() if not r.success]

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

        W    = 50
        div  = "─" * W
        hdiv = "═" * W
        rate = len(successful) / len(results) if results else 0

        print(f"\n{hdiv}")
        print(f"  {'BATCH PROCESSING REPORT':^{W - 4}}")
        print(hdiv)
        print(f"  Total receipts : {len(results)}")
        print(f"  Newly saved    : {len(successful)}")
        print(f"  Duplicates     : {len(duplicates)}")
        print(f"  Failed         : {len(failed)}")
        print(f"  Success rate   : {rate:.0%}")
        print(f"\n  {'FINANCIALS (new only)':^{W - 4}}")
        print(div)
        print(f"  Total expenses : {total_amount:.2f} EUR")
        print(f"  Total VAT      : {total_vat:.2f} EUR")
        print(f"  Net (excl.VAT) : {total_amount - total_vat:.2f} EUR")

        if by_category:
            print(f"\n  {'BY CATEGORY':^{W - 4}}")
            print(div)
            for cat, amt in sorted(by_category.items(), key=lambda x: -x[1]):
                print(f"  {cat:<22} {amt:>10.2f} EUR")

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
                print(f"\n  ✓  {name}  ({t})")
                print(f"     {str(d.receipt_type).upper():<10}  {cp}")
                print(f"     Date    : {dt}   Total: {amt}   VAT: {vat}")
                print(f"     Category: {d.category}   Items: {len(d.items)}")
            else:
                print(f"\n  ✗  {name}")
                print(f"     Error: {result.error_message}")

        print(f"\n{hdiv}\n")

    # ------------------------------------------------------------------
    # UStVA
    # ------------------------------------------------------------------

    @staticmethod
    def _quarter_bounds(quarter: int, year: int) -> tuple[date, date]:
        starts = {1: (1, 1), 2: (4, 1), 3: (7, 1), 4: (10, 1)}
        ends   = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
        ms, ds = starts[quarter]
        me, de = ends[quarter]
        return date(year, ms, ds), date(year, me, de)

    def ingest_receipts(
        self,
        input_dir: Path,
        db_path: Path | None = None,
        verbose: bool = False,
        receipt_type: str = "purchase",
    ) -> int:
        """Scan PDFs and auto-save to DB. Returns number saved."""
        pdf_files = sorted(input_dir.glob("*.pdf"))
        if not pdf_files:
            print(f"No PDF files found in {input_dir.resolve()}")
            return 0

        agent = FinanceAgent(db_path=db_path if db_path else DEFAULT_DB_PATH)
        saved = 0
        dupes = 0

        for pdf in pdf_files:
            if verbose:
                print(f"  {pdf.name} ...", end=" ", flush=True)
            result = agent.process_receipt(pdf, receipt_type=receipt_type)
            if result.duplicate:
                dupes += 1
                if verbose:
                    print("DUPLICATE (skipped)")
            elif result.success:
                saved += 1
                if verbose:
                    d  = result.data
                    cp = d.counterparty.name if d.counterparty else "unknown"
                    print(f"OK  ({cp}, {d.total_amount} EUR)")
            else:
                if verbose:
                    print(f"FAILED — {result.error_message}")

        print(f"{saved} saved, {dupes} duplicates skipped (of {len(pdf_files)} total).")
        return saved

    def run_ustva(
        self,
        quarter: int,
        year: int,
        db_path: Path | None = None,
        output: Path | None = None,
        output_dir: Path | None = None,
    ) -> int:
        """
        Generate and print a UStVA report for the given quarter.

        Args:
            output:     Explicit output file path. Takes priority over output_dir.
            output_dir: Directory to write an auto-named file
                        (e.g. ``ustva_q1_2024.json``).
        """
        start, end = self._quarter_bounds(quarter, year)

        with get_repository(db_path) as repo:
            receipts = list(repo.find_by_period(start, end))

        if not receipts:
            print(
                f"No receipts found for Q{quarter} {year}.\n"
                f"Run with --input-dir to scan and store PDFs first."
            )
            return 1

        report = generate_ustva(receipts, start, end)
        print(report.summary())

        # Resolve output path: explicit --output beats --output-dir auto-name
        out_path: Path | None = None
        if output:
            out_path = output
        elif output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)
            out_path = output_dir / f"ustva_q{quarter}_{year}.json"

        if out_path:
            report.to_json(out_path)
            print(f"Report saved to {out_path}")

        return 0



# ---------------------------------------------------------------------------
# Typer app
# ---------------------------------------------------------------------------

console = Console()

_ASCII_BANNER = """
[bold yellow]____________                         _____ 
___  __/__(_)____________ _______ _____  /_
__  /_ __  /__  __ \\  __ `/_  __ `__ \\  __/
_  __/ _  / _  / / / /_/ /_  / / / / / /_  
/_/    /_/  /_/ /_/\\__,_/ /_/ /_/ /_/\\__/[/bold yellow]  
"""

app = typer.Typer(
    name="finamt",
    help="finamt: process German receipts and prepare tax returns.",
    rich_markup_mode="rich",
    add_completion=False,
)

ReceiptType = typer.Option("--type", help="purchase = Eingangsrechnung; sale = Ausgangsrechnung.")
QuarterOpt  = typer.Option("--quarter", help="Fiscal quarter (1–4).", show_default=True)
YearOpt     = typer.Option("--year", help="Fiscal year.", show_default=True)
DbOpt       = typer.Option("--db", help="SQLite DB path (default: ~/.finamt/finamt.db).", show_default=False)
VerboseOpt  = typer.Option("--verbose", "-v", help="Enable verbose output.")


def _version_callback(value: bool) -> None:
    if value:
        try:
            ver = version("finamt")
        except Exception:
            ver = "unknown"
        rprint(f"finamt version [bold green]{ver}[/bold green]")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def callback(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show package version and exit.",
        ),
    ] = False,
) -> None:
    """finamt: process German receipts and prepare German tax returns."""
    if ctx.invoked_subcommand is None:
        rprint(_ASCII_BANNER)
        rprint(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------
# process sub-command
# ---------------------------------------------------------------------------

@app.command("process")
def cmd_process(
    file: Annotated[str, typer.Argument(help="Receipt filename without the .pdf extension.")],
    input_dir: Annotated[Path, typer.Option("--input-dir", help="Directory containing the receipt PDF.")],
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", help="Write extracted JSON here.")] = None,
    verbose: Annotated[bool, VerboseOpt] = False,
    receipt_type: Annotated[str, ReceiptType] = "purchase",
    db: Annotated[Optional[Path], DbOpt] = None,
    no_db: Annotated[bool, typer.Option("--no-db", help="Disable DB persistence.")] = False,
) -> None:
    """Process a single receipt PDF and extract its data."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(name)s %(message)s")

    rc = FinamtCLI().process_receipt(
        file_stem=file,
        input_dir=input_dir,
        output_dir=output_dir,
        verbose=verbose,
        receipt_type=receipt_type,
        db_path=db,
        no_db=no_db,
    )
    if rc != 0:
        raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# batch sub-command
# ---------------------------------------------------------------------------

@app.command("batch")
def cmd_batch(
    input_dir: Annotated[Path, typer.Option("--input-dir", help="Directory containing receipt PDFs.")],
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", help="Write extracted JSONs here.")] = None,
    verbose: Annotated[bool, VerboseOpt] = False,
    receipt_type: Annotated[str, ReceiptType] = "purchase",
    db: Annotated[Optional[Path], DbOpt] = None,
    no_db: Annotated[bool, typer.Option("--no-db", help="Disable DB persistence.")] = False,
) -> None:
    """Batch-process all PDFs in a directory."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(name)s %(message)s")

    rc = FinamtCLI().batch_process(
        input_dir=input_dir,
        output_dir=output_dir,
        verbose=verbose,
        receipt_type=receipt_type,
        db_path=db,
        no_db=no_db,
    )
    if rc != 0:
        raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# ustva sub-command
# ---------------------------------------------------------------------------

@app.command("ustva")
def cmd_ustva(
    quarter: Annotated[int, QuarterOpt] = 1,
    year: Annotated[int, YearOpt] = date.today().year,
    input_dir: Annotated[Optional[Path], typer.Option("--input-dir", help="Scan & ingest PDFs before reporting.")] = None,
    output: Annotated[Optional[Path], typer.Option("--output", help="Write JSON report to this path.")] = None,
    output_dir: Annotated[Optional[Path], typer.Option("--output-dir", help="Auto-named JSON written here.")] = None,
    verbose: Annotated[bool, VerboseOpt] = False,
    receipt_type: Annotated[str, ReceiptType] = "purchase",
    db: Annotated[Optional[Path], DbOpt] = None,
) -> None:
    """Generate a UStVA (VAT pre-return) report for a fiscal quarter."""
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(name)s %(message)s")

    cli = FinamtCLI()
    if input_dir:
        cli.ingest_receipts(
            input_dir=input_dir,
            db_path=db,
            verbose=verbose,
            receipt_type=receipt_type,
        )
    rc = cli.run_ustva(
        quarter=quarter,
        year=year,
        db_path=db,
        output=output,
        output_dir=output_dir,
    )
    if rc != 0:
        raise typer.Exit(code=rc)


# ---------------------------------------------------------------------------
# serve sub-command
# ---------------------------------------------------------------------------

@app.command("serve")
def cmd_serve(
    host: Annotated[str, typer.Option("--host", help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port number.")] = 8000,
    no_browser: Annotated[bool, typer.Option("--no-browser", help="Do not open the browser.")] = False,
    reload: Annotated[bool, typer.Option("--reload", help="Enable hot-reload (dev mode).")] = False,
    log_level: Annotated[str, typer.Option("--log-level", help="Uvicorn log level.")] = "warning",
) -> None:
    """Start the finamt web UI server."""
    from finamt.ui.server import launch
    launch(
        host=host,
        port=port,
        reload=reload,
        open_browser=not no_browser,
        log_level=log_level,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app()


if __name__ == "__main__":
    main()
