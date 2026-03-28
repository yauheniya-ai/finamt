"""
finamt.tax.eur
~~~~~~~~~~~~~~
Einnahmen-Überschuss-Rechnung (EÜR) — German income-surplus statement.

Applicable to:
  - Sole traders (Einzelunternehmer)
  - Freelancers (Freiberufler)
  - Partnerships (Personengesellschaften) below the bookkeeping threshold
  - NOT for GmbH / UG — those require a Bilanz (double-entry)

Legal basis: § 4 Abs. 3 EStG
Form:        Anlage EÜR (annual attachment to Einkommensteuererklärung)

Cash principle (Zufluss-/Abfluss-Prinzip):
  Revenue is recognised when received, expenses when paid.
  This module trusts that receipt_date reflects the payment date.

Anlage EÜR Kennzahlen used (2024 form):
  Income (Betriebseinnahmen):
    Kz 111  Umsatzsteuer-pflichtige Betriebseinnahmen (Netto)
    Kz 112  Umsatzsteuerfreie / nicht steuerbare Betriebseinnahmen
  Expenses (Betriebsausgaben):
    Kz 145  Waren, Roh-, Hilfs- und Betriebsstoffe (material)
    Kz 140  Abschreibungen (equipment — simplified: full deduction in year of purchase
             for GWG up to 800 € net; otherwise Kz 130 for regular AfA)
    Kz 145  Büromaterial, Porto, Telefon (software, internet, telecommunication)
    Kz 165  Reisekosten (travel)
    Kz 183  Fortbildungskosten (education)
    Kz 172  Versicherungen (insurance)
    Kz 176  Sonstige Betriebsausgaben (everything else → category "other")
  Result:
    Gewinn / Verlust = Betriebseinnahmen − Betriebsausgaben

⚠ DISCLAIMER:
  The Kennzahlen and category mappings are provided for guidance only.
  Always verify against the current official Anlage EÜR form and consult
  a tax advisor (Steuerberater) before filing.

Usage::

    from finamt.storage import get_repository
    from finamt.tax.eur import generate_eur

    with get_repository() as repo:
        receipts = repo.find_by_period(date(2024, 1, 1), date(2024, 12, 31))
    report = generate_eur(receipts, 2024)
    print(report.summary())
    report.to_json("eur_2024.json")
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from ..models import ReceiptData, ReceiptCategory

_TWO = Decimal("0.01")


def _r(d: Decimal) -> Decimal:
    return d.quantize(_TWO, rounding=ROUND_HALF_UP)


def _to_date(dt: date | datetime) -> date:
    return dt.date() if isinstance(dt, datetime) else dt


# ---------------------------------------------------------------------------
# Mapping: ReceiptCategory → Anlage EÜR Kennzahl + label
# ---------------------------------------------------------------------------

# (kz, short_label)
_EXPENSE_MAPPING: dict[str, tuple[str, str]] = {
    "material":           ("145", "Waren, Roh-/Hilfsstoffe"),
    "equipment":          ("140", "Abschreibungen / GWG"),
    "software":           ("145", "Büromaterial / Software"),
    "internet":           ("145", "Internet / Hosting"),
    "telecommunication":  ("145", "Telekommunikation"),
    "travel":             ("165", "Reisekosten"),
    "education":          ("183", "Fortbildungskosten"),
    "utilities":          ("176", "Energie / Nebenkosten"),
    "insurance":          ("172", "Versicherungen"),
    "taxes":              ("176", "Steuerberatung / Abgaben"),
    "other":              ("176", "Sonstige Betriebsausgaben"),
}

_INCOME_MAPPING: dict[str, tuple[str, str]] = {
    "services":    ("111", "Einnahmen aus Dienstleistungen"),
    "consulting":  ("111", "Einnahmen aus Beratung"),
    "products":    ("111", "Einnahmen aus Produktverkauf"),
    "licensing":   ("111", "Einnahmen aus Lizenzen"),
    "other":       ("112", "Sonstige Betriebseinnahmen"),
}


# ---------------------------------------------------------------------------
# Per-category line
# ---------------------------------------------------------------------------

@dataclass
class EURLinie:
    """Aggregated figures for one expense/income category."""

    category:     str
    kz:           str           # Anlage EÜR Kennzahl
    label:        str
    net_amount:   Decimal = field(default_factory=Decimal)
    vat_amount:   Decimal = field(default_factory=Decimal)
    gross_amount: Decimal = field(default_factory=Decimal)
    count:        int = 0

    def to_dict(self) -> dict:
        return {
            "category":     self.category,
            "kz":           self.kz,
            "label":        self.label,
            "net_amount":   str(self.net_amount),
            "vat_amount":   str(self.vat_amount),
            "gross_amount": str(self.gross_amount),
            "count":        self.count,
        }


# ---------------------------------------------------------------------------
# EÜR Report
# ---------------------------------------------------------------------------

@dataclass
class EURReport:
    """
    Einnahmen-Überschuss-Rechnung for one fiscal year.

    ``gewinn > 0``  → profit (taxable)
    ``gewinn < 0``  → loss (deductible against other income)
    """

    year:              int
    einnahmen_lines:   dict[str, EURLinie] = field(default_factory=dict)
    ausgaben_lines:    dict[str, EURLinie] = field(default_factory=dict)
    skipped_count:     int = 0

    # ------------------------------------------------------------------
    # Aggregated totals
    # ------------------------------------------------------------------

    @property
    def total_einnahmen_netto(self) -> Decimal:
        """Total net revenue (excl. VAT charged to clients)."""
        return sum((ln.net_amount for ln in self.einnahmen_lines.values()), Decimal("0"))

    @property
    def total_einnahmen_ust(self) -> Decimal:
        """Total VAT collected from clients (Umsatzsteuer-Einnahme)."""
        return sum((ln.vat_amount for ln in self.einnahmen_lines.values()), Decimal("0"))

    @property
    def total_ausgaben_netto(self) -> Decimal:
        """Total net expenses (excl. VAT paid to vendors, claimable as Vorsteuer)."""
        return sum((ln.net_amount for ln in self.ausgaben_lines.values()), Decimal("0"))

    @property
    def total_ausgaben_vorsteuer(self) -> Decimal:
        """Total VAT paid on purchases (Vorsteuer — claimable separately via UStVA)."""
        return sum((ln.vat_amount for ln in self.ausgaben_lines.values()), Decimal("0"))

    @property
    def gewinn(self) -> Decimal:
        """
        Betriebsgewinn / -verlust (§ 4 Abs. 3 EStG).

        = net revenue − net expenses
        VAT is excluded: Vorsteuer and Umsatzsteuer are handled via UStVA,
        not through the EÜR (unless VAT is not deductible, e.g. Kleinunternehmer).
        """
        return _r(self.total_einnahmen_netto - self.total_ausgaben_netto)

    # ------------------------------------------------------------------
    # Anlage EÜR aggregated by Kennzahl
    # ------------------------------------------------------------------

    def kz_totals(self) -> dict[str, Decimal]:
        """Net amounts aggregated by Kennzahl — for populating the official form."""
        totals: dict[str, Decimal] = {}
        for ln in self.einnahmen_lines.values():
            totals[ln.kz] = totals.get(ln.kz, Decimal("0")) + ln.net_amount
        for ln in self.ausgaben_lines.values():
            totals[ln.kz] = totals.get(ln.kz, Decimal("0")) + ln.net_amount
        return {k: _r(v) for k, v in sorted(totals.items())}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "year":                    self.year,
            "skipped_count":           self.skipped_count,
            "total_einnahmen_netto":   str(self.total_einnahmen_netto),
            "total_einnahmen_ust":     str(self.total_einnahmen_ust),
            "total_ausgaben_netto":    str(self.total_ausgaben_netto),
            "total_ausgaben_vorsteuer": str(self.total_ausgaben_vorsteuer),
            "gewinn":                  str(self.gewinn),
            "kz_totals":               {k: str(v) for k, v in self.kz_totals().items()},
            "einnahmen":               {k: v.to_dict() for k, v in self.einnahmen_lines.items()},
            "ausgaben":                {k: v.to_dict() for k, v in self.ausgaben_lines.items()},
        }

    def to_json(self, path: str | Path | None = None) -> str:
        raw = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        if path:
            Path(path).write_text(raw, encoding="utf-8")
        return raw

    def summary(self) -> str:
        W = 60
        div  = "─" * W

        def fmt(label: str, amount: Decimal, indent: int = 2) -> str:
            pad = " " * indent
            return f"{pad}{label:<40} {amount:>10.2f} EUR"

        lines = [
            "=" * W,
            f"  Einnahmen-Überschuss-Rechnung — {self.year}",
            "  (§ 4 Abs. 3 EStG)",
            "=" * W,
            f"  Übersprungene Belege: {self.skipped_count}",
            div,
            "  BETRIEBSEINNAHMEN",
            div,
        ]
        for ln in sorted(self.einnahmen_lines.values(), key=lambda x: x.kz):
            lines.append(fmt(f"Kz {ln.kz}  {ln.label} ({ln.count})", ln.net_amount))
        lines += [
            fmt("Summe Betriebseinnahmen (netto)", self.total_einnahmen_netto),
            div,
            "  BETRIEBSAUSGABEN",
            div,
        ]
        for ln in sorted(self.ausgaben_lines.values(), key=lambda x: x.kz):
            lines.append(fmt(f"Kz {ln.kz}  {ln.label} ({ln.count})", ln.net_amount))
        lines += [
            fmt("Summe Betriebsausgaben (netto)", self.total_ausgaben_netto),
            "═" * W,
        ]

        if self.gewinn >= 0:
            lines.append(fmt("GEWINN (§ 4 Abs. 3 EStG)", self.gewinn))
        else:
            lines.append(fmt("VERLUST (§ 4 Abs. 3 EStG)", self.gewinn))

        lines += [
            div,
            fmt("Vorsteuer gezahlt (→ UStVA Kz 66)", self.total_ausgaben_vorsteuer),
            fmt("Umsatzsteuer eingenommen (→ UStVA)", self.total_einnahmen_ust),
            "=" * W,
            "  ⚠  Kennzahlen sind Richtwerte — vor Abgabe mit aktuellem",
            "     Anlage EÜR Formular und Steuerberater abgleichen.",
            "=" * W,
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_eur(
    receipts: Iterable[ReceiptData],
    year: int,
) -> EURReport:
    """
    Compute the EÜR figures from an iterable of receipts for a full fiscal year.

    Skips receipts that:
    - have no ``receipt_date``
    - fall outside the calendar year
    - have no ``total_amount``

    Note on VAT:
      For Regelbesteuerer: net amounts are used (VAT flows through UStVA separately).
      For Kleinunternehmer (§ 19 UStG): pass gross amounts as net_amount since
      they cannot claim Vorsteuer and do not charge VAT — adjust in your ReceiptData
      before calling this function.
    """
    report = EURReport(year=year)
    period_start = date(year, 1, 1)
    period_end   = date(year, 12, 31)

    for r in receipts:
        if r.receipt_date is None:
            report.skipped_count += 1
            continue
        d = _to_date(r.receipt_date)
        if not (period_start <= d <= period_end):
            report.skipped_count += 1
            continue
        if not r.total_amount:
            report.skipped_count += 1
            continue

        cat = str(r.category) if r.category else "other"
        net = _r(r.business_net if r.business_net is not None else (r.net_amount or Decimal("0")))
        vat = _r(r.business_vat if r.business_vat is not None else (r.vat_amount or Decimal("0")))
        gross = _r(net + vat)

        if r.is_purchase:
            kz, label = _EXPENSE_MAPPING.get(cat, ("176", "Sonstige Betriebsausgaben"))
            lines = report.ausgaben_lines
        else:
            kz, label = _INCOME_MAPPING.get(cat, ("112", "Sonstige Betriebseinnahmen"))
            lines = report.einnahmen_lines

        if cat not in lines:
            lines[cat] = EURLinie(category=cat, kz=kz, label=label)
        ln = lines[cat]
        ln.net_amount   += net
        ln.vat_amount   += vat
        ln.gross_amount += gross
        ln.count        += 1

    # Final rounding pass
    for ln in (*report.einnahmen_lines.values(), *report.ausgaben_lines.values()):
        ln.net_amount   = _r(ln.net_amount)
        ln.vat_amount   = _r(ln.vat_amount)
        ln.gross_amount = _r(ln.gross_amount)

    return report