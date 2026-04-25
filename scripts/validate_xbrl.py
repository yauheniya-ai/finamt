#!/usr/bin/env python3
"""
validate_xbrl.py — Pre-validate an E-Bilanz XBRL instance with Arelle.

Generates a sample XBRL instance (if no file is given) and runs Arelle
taxonomy validation against it, printing every error/warning to stdout.

The HGB XBRL taxonomy must be available locally (german-gaap-taxonomy-v6/).
The script auto-detects the taxonomy directory relative to the repo root (two
levels above pypi/) or you can pass it explicitly via --taxonomy-dir.

Usage:
    # Validate a specific file:
    python scripts/validate_xbrl.py path/to/ebilanz.xbrl

    # Generate a minimal sample + validate it immediately:
    python scripts/validate_xbrl.py

    # Also write the generated sample to disk for inspection:
    python scripts/validate_xbrl.py --save sample_ebilanz.xbrl

    # Explicit taxonomy dir:
    python scripts/validate_xbrl.py --taxonomy-dir /path/to/german-gaap-taxonomy-v6

Requirements:
    pip install finamt[dev]       # includes arelle-release
    # or individually:
    pip install arelle-release lxml
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from decimal import Decimal
from pathlib import Path


# ---------------------------------------------------------------------------
# Build a minimal sample XBRL instance via finamt
# ---------------------------------------------------------------------------

def _build_sample_xbrl() -> bytes:
    from finamt.tax.bilanz import Bilanz, GuV, Jahresabschluss
    from finamt.tax.ebilanz import EBilanzConfig, build_xbrl

    bilanz = Bilanz(
        year=2024,
        kassenbestand=Decimal("5000.00"),
        forderungen=Decimal("8000.00"),
        vorräte=Decimal("2000.00"),
        anlagevermögen=Decimal("15000.00"),
        stammkapital=Decimal("25000.00"),
        gewinnvortrag=Decimal("0.00"),
        jahresergebnis=Decimal("12000.00"),
        rückstellungen=Decimal("3000.00"),
        verbindlichkeiten=Decimal("10000.00"),
    )
    guv = GuV(
        year=2024,
        umsatzerlöse=Decimal("80000.00"),
        sonstige_betriebserlöse=Decimal("0.00"),
        materialaufwand=Decimal("20000.00"),
        personalaufwand=Decimal("30000.00"),
        abschreibungen=Decimal("5000.00"),
        sonstige_betriebsausgaben=Decimal("13000.00"),
        zinsaufwendungen=Decimal("0.00"),
    )
    jab = Jahresabschluss(bilanz=bilanz, guv=guv)

    cfg = EBilanzConfig(
        steuernummer="37/539/50531",
        company_name="Muster GmbH",
        legal_form="GmbH",
        fiscal_year_start="2024-01-01",
        fiscal_year_end="2024-12-31",
    )
    return build_xbrl(jab, cfg)


# ---------------------------------------------------------------------------
# Locate the local taxonomy
# ---------------------------------------------------------------------------

# Repo layout:  …/finamt/pypi/scripts/validate_xbrl.py
#               …/finamt/german-gaap-taxonomy-v6/de-gaap-ci-2025-04-01/
_SCRIPT_DIR  = Path(__file__).resolve().parent
_REPO_ROOT   = _SCRIPT_DIR.parent.parent          # …/finamt/
_TAXONOMY_V6 = _REPO_ROOT / "german-gaap-taxonomy-v6"

# The XBRL schemaRef uses these relative paths — they live in the gaap-ci folder.
# GCD is referenced as ../de-gcd-2025-04-01/de-gcd-2025-04-01.xsd from the gaap-ci dir.
_GAAP_CI_DIR = _TAXONOMY_V6 / "de-gaap-ci-2025-04-01"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(xbrl_path: Path, taxonomy_dir: Path | None = None) -> bool:
    """Run Arelle validation; return True if no errors."""
    try:
        from arelle.CntlrCmdLine import parseAndRun  # type: ignore[import]
    except ImportError:
        print(
            "ERROR: arelle-release is not installed.\n"
            "       Run: pip install arelle-release",
            file=sys.stderr,
        )
        sys.exit(2)

    # Resolve taxonomy directory so the schemaRef relative paths work.
    # The XBRL instance references e.g. "de-gaap-ci-2025-04-01-shell-fiscal-microbilg.xsd"
    # as a relative filename, so Arelle must find it next to the XBRL file.
    gaap_ci_dir = taxonomy_dir or _GAAP_CI_DIR
    validate_path = xbrl_path
    cleanup_copy: Path | None = None
    if not gaap_ci_dir.exists():
        print(
            f"WARNING: Taxonomy directory not found: {gaap_ci_dir}\n"
            "         Schema resolution will likely fail.  Pass --taxonomy-dir to fix.\n",
            file=sys.stderr,
        )
    else:
        # Copy the XBRL file next to the taxonomy XSDs so relative schema refs resolve.
        import shutil
        validate_path = gaap_ci_dir / xbrl_path.name
        shutil.copy2(xbrl_path, validate_path)
        cleanup_copy = validate_path
        # GCD is resolved via relative path ../de-gcd-2025-04-01/de-gcd-2025-04-01.xsd
        # which works automatically when the XBRL sits in the gaap-ci dir.

    print(f"\n{'='*70}")
    print(f"  Arelle XBRL validation: {xbrl_path}")
    if cleanup_copy:
        print(f"  Resolving schemas from:  {gaap_ci_dir}")
    print(f"{'='*70}\n")

    with tempfile.NamedTemporaryFile(suffix=".log", mode="w", delete=False) as lf:
        log_path = lf.name

    try:
        parseAndRun([
            "--file",     str(validate_path),
            "--validate",
            "--logFile",  log_path,
        ])
    finally:
        if cleanup_copy and cleanup_copy.exists():
            cleanup_copy.unlink()

    log_text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    Path(log_path).unlink(missing_ok=True)

    if not log_text.strip():
        print("✓ No issues found — Arelle reported an empty log.\n")
        return True

    # Arelle log format: "[errorCode] message - file line"
    # Info entries use [info] or [] (empty code); everything else is an error.
    import re
    _INFO_PAT = re.compile(r"^\[(info|)\]", re.IGNORECASE)

    lines = log_text.splitlines()
    errors = [l for l in lines if l.strip() and not _INFO_PAT.match(l.strip())]
    info   = [l for l in lines if l.strip() and _INFO_PAT.match(l.strip())]

    if errors:
        print(f"✗ {len(errors)} ISSUE(S) FOUND:\n")
        for l in errors:
            print(f"  {l}")
        print()
    if info:
        print(f"ℹ  {len(info)} informational message(s):\n")
        for l in info:
            print(f"  {l}")
        print()

    if not errors:
        print("✓ Validation passed (no XBRL errors).\n")
        return True

    return False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate an E-Bilanz XBRL file with Arelle."
    )
    parser.add_argument(
        "xbrl_file",
        nargs="?",
        help="Path to an existing .xbrl file.  If omitted, a sample is generated.",
    )
    parser.add_argument(
        "--save",
        metavar="OUTPUT",
        help="When generating a sample, also write it to this path.",
    )
    parser.add_argument(
        "--taxonomy-dir",
        metavar="DIR",
        help=(
            "Path to the de-gaap-ci-2025-04-01 folder of the HGB taxonomy "
            f"(default: auto-detected as {_GAAP_CI_DIR})"
        ),
    )
    args = parser.parse_args()

    taxonomy_dir = Path(args.taxonomy_dir) if args.taxonomy_dir else None

    if args.xbrl_file:
        xbrl_path = Path(args.xbrl_file)
        if not xbrl_path.exists():
            print(f"ERROR: File not found: {xbrl_path}", file=sys.stderr)
            sys.exit(1)
        if xbrl_path.suffix.lower() not in {".xbrl", ".xml"}:
            print(
                f"ERROR: Expected a .xbrl or .xml file, got: {xbrl_path.name}\n"
                "       Pass an XBRL instance document, not an HTML or PDF file.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print("No file given — generating sample XBRL from finamt …\n")
        xbrl_bytes = _build_sample_xbrl()

        if args.save:
            xbrl_path = Path(args.save)
            xbrl_path.write_bytes(xbrl_bytes)
            print(f"Sample written to: {xbrl_path}\n")
        else:
            tmp = tempfile.NamedTemporaryFile(suffix=".xbrl", delete=False)
            tmp.write(xbrl_bytes)
            tmp.close()
            xbrl_path = Path(tmp.name)
            print(f"Sample written to temp file: {xbrl_path}\n")

    ok = _validate(xbrl_path, taxonomy_dir=taxonomy_dir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
