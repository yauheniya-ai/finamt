"""
finamt.tax.elster
~~~~~~~~~~~~~~~~~
ELSTER XML submission for German tax filings.

Supported form types:
  - UStVA  (Umsatzsteuer-Voranmeldung)   — VAT pre-return
  - EÜR    (Einnahmen-Überschuss-Rechnung) — planned
  - E-Bilanz                              — planned

ELSTER transmission overview:
  1. Build the payload XML (NutzdatenXML) for the specific form.
  2. Wrap it in the ELSTER envelope (TransferHeader + DatenTeil).
  3. Sign the envelope using an ELSTER PKCS#12 certificate.
  4. POST the signed XML to the ELSTER server.
  5. Parse the Rückmeldung (acknowledgement) for success / error codes.

Certificates:
  Obtain a free ELSTER Organisationszertifikat at:
    https://www.elster.de/eportal/portal/honk#!

  Store it securely — never commit to version control.
  Pass path and password via environment variables or the ElsterConfig dataclass.

Dependencies (install with  pip install finamt[elster]):
  - lxml           (XML building + XPath)
  - cryptography   (PKCS#12 parsing, RSA signing)
  - requests       (HTTPS submission)

UStVA Kennzahlen (2024 form — verify against current official form):
  Ausgangsseite (sales):
    Kz 81   Lieferungen/Leistungen 19 %  — Bemessungsgrundlage (netto, ganze EUR)
    Kz 86   Lieferungen/Leistungen 7 %   — Bemessungsgrundlage
    Kz 35   Andere Steuersätze           — Bemessungsgrundlage
    Kz 36   Andere Steuersätze           — Steuerbetrag
  Vorsteuer (purchases):
    Kz 66   Abziehbare Vorsteuerbeträge  — gesamt
  Ergebnis:
    Kz 83   Verbleibende Vorauszahlung / Erstattung (computed by Finanzamt)

⚠  IMPORTANT:
  - Always test against the ELSTER test environment first (use_test=True).
  - Kennzahlen change annually — verify with the official Formulare on www.elster.de.
  - This module does NOT replace review by a Steuerberater.
  - Decimal amounts for Kz 81/86/35 must be rounded to whole euros (int) per ELSTER spec.

Usage::

    from finamt.tax.elster import ElsterConfig, ElsterClient
    from finamt.tax.ustva import generate_ustva

    config = ElsterConfig(
        cert_path="~/.finamt/elster.pfx",
        cert_password="my-secret",
        steuernummer="21/815/08150",    # Steuernummer from your Finanzamt
        finanzamt_nr="2181",             # 4-digit Finanzamtsnummer
        bundesland_kz="21",              # 2-digit Länderkennzeichen
    )

    client = ElsterClient(config, use_test=True)   # always start in test mode!
    result = client.submit_ustva(report, year=2024, period=1)  # period=1 → January
    print(result)
"""

from __future__ import annotations

import hashlib
import os
import uuid
from base64 import b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Optional imports — only needed for actual signing / submission
# ---------------------------------------------------------------------------

try:
    from lxml import etree
    _LXML_AVAILABLE = True
except ImportError:
    _LXML_AVAILABLE = False

try:
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from .ustva import USTVAReport


# ---------------------------------------------------------------------------
# ELSTER endpoints
# ---------------------------------------------------------------------------

ELSTER_URL_PRODUCTION = "https://www.elster.de/ekona/upload/elster"
ELSTER_URL_TEST       = "https://www.elster.de/ekona/upload/elstertest"

# Testmerker — signals the Finanzamt that this is a test submission
TESTMERKER = "700000004"

# ELSTER XML namespace
NS = "http://www.elster.de/elsterxml/schema/v12"

# Product identification sent to ELSTER
PRODUKT_NAME    = "finamt"
PRODUKT_VERSION = "0.1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class ElsterConfig:
    """
    Configuration for ELSTER submissions.

    Parameters
    ----------
    cert_path:
        Path to the ELSTER PKCS#12 certificate file (.pfx or .p12).
        Obtain for free at https://www.elster.de/eportal/portal/honk#!
    cert_password:
        Password for the certificate.  Prefer passing via environment variable
        FINAMT_ELSTER_CERT_PASSWORD rather than hard-coding.
    steuernummer:
        Your company's Steuernummer in the format used by your Finanzamt,
        e.g. "21/815/08150" (Bayern) or "30/450/09999" (Berlin).
        Will be normalised to the 13-digit ELSTER format automatically.
    finanzamt_nr:
        4-digit Finanzamtsnummer, e.g. "2181" (München).
        Look up at: https://www.bzst.de/DE/Service/Behoerdenwegweiser/Finanzamtsuche
    bundesland_kz:
        2-digit Länderkennzeichen (state prefix of the Steuernummer).
        E.g. "21" for Bayern, "30" for Berlin, "22" for Brandenburg.
    """

    cert_path:      str | Path
    cert_password:  str
    steuernummer:   str
    finanzamt_nr:   str
    bundesland_kz:  str

    @classmethod
    def from_env(cls) -> "ElsterConfig":
        """Load config from environment variables."""
        return cls(
            cert_path     = os.environ["FINAMT_ELSTER_CERT_PATH"],
            cert_password = os.environ["FINAMT_ELSTER_CERT_PASSWORD"],
            steuernummer  = os.environ["FINAMT_ELSTER_STEUERNUMMER"],
            finanzamt_nr  = os.environ["FINAMT_ELSTER_FINANZAMT_NR"],
            bundesland_kz = os.environ["FINAMT_ELSTER_BUNDESLAND_KZ"],
        )


# ---------------------------------------------------------------------------
# Submission result
# ---------------------------------------------------------------------------

@dataclass
class SubmissionResult:
    success:        bool
    telenummer:     str | None = None    # ELSTER transfer ticket number
    error_code:     str | None = None
    error_message:  str | None = None
    raw_response:   str | None = None

    def __str__(self) -> str:
        if self.success:
            return f"✓ Übermittlung erfolgreich — Telenummer: {self.telenummer}"
        return f"✗ Fehler {self.error_code}: {self.error_message}"


# ---------------------------------------------------------------------------
# Steuernummer normalisation
# ---------------------------------------------------------------------------

def normalise_steuernummer(raw: str, bundesland_kz: str) -> str:
    """
    Normalise a Steuernummer to the 13-digit ELSTER format.

    German Steuernummern are formatted differently per Bundesland.
    ELSTER expects a 13-digit format: BBBFFFBBBBBB (no separators).

    This covers the most common formats.  For unusual cases, pass the
    13-digit form directly (the function returns it unchanged).

    Reference: https://www.bundesfinanzministerium.de (Steuernummer-Aufbau)
    """
    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 13:
        return digits
    # Strip leading Länderkennzeichen if already present
    if digits.startswith(bundesland_kz):
        digits = digits[len(bundesland_kz):]
    # Pad to 11 digits (local format), then prepend Länderkennzeichen
    digits = digits.zfill(11)
    return bundesland_kz + digits


# ---------------------------------------------------------------------------
# UStVA Kennzahlen mapping
# ---------------------------------------------------------------------------

def _ustva_kennzahlen(report: USTVAReport) -> dict[str, str]:
    """
    Map USTVAReport figures to UStVA Kennzahlen.

    Amounts for Kz 81/86/35 are rounded DOWN to whole euros per ELSTER spec.
    VAT amounts are rounded to 2 decimal places (cents).

    ⚠  Verify these mappings against the current official UStVA form on www.elster.de.
    """
    def whole(d: Decimal) -> str:
        """Round down to whole euros, return as string without decimal point."""
        return str(int(d.quantize(Decimal("1"), rounding=ROUND_DOWN)))

    def cents(d: Decimal) -> str:
        return f"{d:.2f}"

    kz: dict[str, str] = {}

    # Standard tax rates
    ln19 = report.line_19
    if ln19 and ln19.sale_net > 0:
        kz["Kz81"] = whole(ln19.sale_net)      # Umsätze 19 % — Bemessungsgrundlage

    ln7 = report.line_7
    if ln7 and ln7.sale_net > 0:
        kz["Kz86"] = whole(ln7.sale_net)       # Umsätze 7 % — Bemessungsgrundlage

    # Other rates (Kz 35 / Kz 36)
    for rate_key, ln in report.lines.items():
        if rate_key not in ("19", "7", "unknown") and ln.sale_net > 0:
            kz["Kz35"] = kz.get("Kz35", "")
            kz["Kz36"] = kz.get("Kz36", "")
            # Accumulate if multiple non-standard rates
            prev_net = Decimal(kz["Kz35"]) if kz["Kz35"] else Decimal("0")
            prev_tax = Decimal(kz["Kz36"]) if kz["Kz36"] else Decimal("0")
            kz["Kz35"] = whole(prev_net + ln.sale_net)
            kz["Kz36"] = cents(prev_tax + ln.sale_vat)

    # Input tax (Vorsteuer) — all rates combined in Kz 66
    if report.total_input_vat > 0:
        kz["Kz66"] = cents(report.total_input_vat)

    return kz


# ---------------------------------------------------------------------------
# XML Builder
# ---------------------------------------------------------------------------

class ElsterXMLBuilder:
    """
    Builds the ELSTER XML envelope for a tax form submission.

    The ELSTER v12 envelope structure:
      <Elster>
        <TransferHeader>          — metadata, certificate reference, signature placeholder
        <DatenTeil>
          <Nutzdatenblock>
            <NutzdatenHeader>     — ticket, recipient Finanzamt, product info
            <Nutzdaten>           — the actual form data (UStVA, EÜR, ...)
    """

    def __init__(self, config: ElsterConfig) -> None:
        if not _LXML_AVAILABLE:
            raise ImportError(
                "lxml is required for ELSTER XML generation. "
                "Install with: pip install finamt[elster]"
            )
        self.config = config

    def _ns(self, tag: str) -> str:
        return f"{{{NS}}}{tag}"

    def build_ustva(
        self,
        report: USTVAReport,
        year: int,
        period: int,
        is_berichtigung: bool = False,
        use_test: bool = True,
    ) -> bytes:
        """
        Build the ELSTER XML for a UStVA submission.

        Parameters
        ----------
        report:
            The USTVAReport to submit.
        year:
            Fiscal year, e.g. 2024.
        period:
            Reporting period:
              1–12  → monthly (Monatszahler)
              41    → Q1 (January–March)
              42    → Q2 (April–June)
              43    → Q3 (July–September)
              44    → Q4 (October–December)
        is_berichtigung:
            True if this is an amended return (Berichtigte Voranmeldung).
        use_test:
            True → include Testmerker (test submission, not legally binding).
        """
        ticket    = str(uuid.uuid4()).replace("-", "").upper()[:20]
        steuernr  = normalise_steuernummer(self.config.steuernummer, self.config.bundesland_kz)
        kz        = _ustva_kennzahlen(report)
        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d%H%M%S")

        root = etree.Element("Elster", xmlns=NS, version="12")

        # ── TransferHeader ────────────────────────────────────────────
        th = etree.SubElement(root, "TransferHeader", version="12")
        etree.SubElement(th, "Verfahren").text  = "ElsterAnmeldung"
        etree.SubElement(th, "DatenArt").text   = "UStVA"
        etree.SubElement(th, "Vorgang").text    = "send-Auth"
        etree.SubElement(th, "TransferTicket").text = ticket
        if use_test:
            etree.SubElement(th, "Testmerker").text = TESTMERKER
        etree.SubElement(th, "Empfaenger", id="L")   # L = Landesfinanzbehörde
        etree.SubElement(th, "HerstellerID").text = "74931"  # placeholder — register at ELSTER
        sig_root = etree.SubElement(th, "Signaturen")
        sig      = etree.SubElement(sig_root, "Signatur", version="12")
        etree.SubElement(sig, "Beschreibung").text = "Softwarezertifikat"
        # Placeholder nodes — filled in by ElsterSigner
        etree.SubElement(sig, "DigestValue")
        etree.SubElement(sig, "SignatureValue")
        etree.SubElement(sig, "X509Certificate")

        # ── DatenTeil ─────────────────────────────────────────────────
        dt    = etree.SubElement(root, "DatenTeil")
        ndb   = etree.SubElement(dt,   "Nutzdatenblock")

        # NutzdatenHeader
        ndh = etree.SubElement(ndb, "NutzdatenHeader", version="12")
        etree.SubElement(ndh, "NutzdatenTicket").text = ticket
        emp = etree.SubElement(ndh, "Empfaenger", id="F")    # F = Finanzamt
        etree.SubElement(emp, "Adressat").text = self.config.finanzamt_nr
        herst = etree.SubElement(ndh, "Hersteller")
        etree.SubElement(herst, "ProduktName").text    = PRODUKT_NAME
        etree.SubElement(herst, "ProduktVersion").text = PRODUKT_VERSION

        # Nutzdaten — UStVA payload
        nd    = etree.SubElement(ndb, "Nutzdaten")
        anm   = etree.SubElement(nd,  "Anmeldungssteuern",
                                  art="UStVA", version=f"{year}01")
        sf    = etree.SubElement(anm, "Steuerfall")
        ustva = etree.SubElement(sf,  "Umsatzsteuervoranmeldung")

        etree.SubElement(ustva, "Jahr").text    = str(year)
        etree.SubElement(ustva, "Zeitraum").text = str(period).zfill(2)
        etree.SubElement(ustva, "Steuernummer").text = steuernr
        etree.SubElement(ustva, "Kz09").text    = self.config.finanzamt_nr
        etree.SubElement(ustva, "Kz10").text    = "1" if is_berichtigung else "0"

        # Write Kennzahlen
        for kz_name, kz_value in kz.items():
            etree.SubElement(ustva, kz_name).text = kz_value

        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ---------------------------------------------------------------------------
# Signer
# ---------------------------------------------------------------------------

class ElsterSigner:
    """
    Signs an ELSTER XML document using a PKCS#12 certificate.

    ELSTER uses a simplified XML signature scheme:
      1. Compute SHA-256 digest of the NutzdatenXML (Nutzdaten element).
      2. Sign the digest with the private key from the certificate (RSA-SHA256).
      3. Insert DigestValue, SignatureValue, and the X.509 certificate (DER, base64)
         into the TransferHeader/Signaturen/Signatur element.

    ⚠  This is a simplified implementation of ELSTER's signing protocol.
       The official ERiC library handles additional steps (e.g. challenge-response
       for server-side validation).  For production use, validate against the
       official ELSTER documentation and test portal before filing.
    """

    def __init__(self, config: ElsterConfig) -> None:
        if not _CRYPTO_AVAILABLE:
            raise ImportError(
                "cryptography is required for ELSTER signing. "
                "Install with: pip install finamt[elster]"
            )
        cert_path = Path(config.cert_path).expanduser()
        with open(cert_path, "rb") as f:
            pfx_data = f.read()
        private_key, certificate, _ = pkcs12.load_key_and_certificates(
            pfx_data, config.cert_password.encode()
        )
        self._private_key  = private_key
        self._certificate  = certificate

    def sign(self, xml_bytes: bytes) -> bytes:
        """
        Insert signature values into the ELSTER XML envelope.

        Modifies the placeholder DigestValue / SignatureValue / X509Certificate
        nodes that ElsterXMLBuilder inserts.
        """
        if not _LXML_AVAILABLE:
            raise ImportError("lxml is required for XML signing.")

        tree = etree.fromstring(xml_bytes)

        # Locate the Nutzdaten element — this is what we sign
        nutzdaten_nodes = tree.xpath("//*[local-name()='Nutzdaten']")
        if not nutzdaten_nodes:
            raise ValueError("No <Nutzdaten> element found in XML.")
        nutzdaten_xml = etree.tostring(nutzdaten_nodes[0], encoding="UTF-8")

        # 1. Digest
        digest = hashlib.sha256(nutzdaten_xml).digest()
        digest_b64 = b64encode(digest).decode()

        # 2. Sign digest with private key
        signature = self._private_key.sign(
            digest,
            asym_padding.PKCS1v15(),
            hashes.SHA256(),
        )
        sig_b64 = b64encode(signature).decode()

        # 3. DER-encode certificate, base64
        cert_der = self._certificate.public_bytes(serialization.Encoding.DER)
        cert_b64 = b64encode(cert_der).decode()

        # 4. Fill placeholder nodes
        def fill(xpath: str, value: str) -> None:
            nodes = tree.xpath(f"//*[local-name()='{xpath}']")
            if nodes:
                nodes[0].text = value

        fill("DigestValue",     digest_b64)
        fill("SignatureValue",  sig_b64)
        fill("X509Certificate", cert_b64)

        return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

class ElsterClient:
    """
    Submits signed ELSTER XML to the ELSTER server and parses the response.

    Parameters
    ----------
    config:
        ElsterConfig instance.
    use_test:
        True  → submit to test endpoint (Testmerker 700000004, not legally binding).
        False → submit to production endpoint — real filing!
    """

    def __init__(self, config: ElsterConfig, use_test: bool = True) -> None:
        if not _REQUESTS_AVAILABLE:
            raise ImportError(
                "requests is required for ELSTER submission. "
                "Install with: pip install finamt[elster]"
            )
        self.config    = config
        self.use_test  = use_test
        self._builder  = ElsterXMLBuilder(config)
        self._signer   = ElsterSigner(config)
        self._url      = ELSTER_URL_TEST if use_test else ELSTER_URL_PRODUCTION

        if not use_test:
            import warnings
            warnings.warn(
                "ElsterClient is in PRODUCTION mode. "
                "Submissions are legally binding.",
                stacklevel=2,
            )

    def submit_ustva(
        self,
        report: USTVAReport,
        year: int,
        period: int,
        is_berichtigung: bool = False,
        timeout: int = 30,
    ) -> SubmissionResult:
        """
        Build, sign, and submit a UStVA.

        Parameters
        ----------
        report:
            Computed USTVAReport for the period.
        year:
            Fiscal year.
        period:
            1–12 (monthly) or 41–44 (quarterly).
        is_berichtigung:
            True = amended return.
        timeout:
            HTTP request timeout in seconds.
        """
        # 1. Build XML
        xml_unsigned = self._builder.build_ustva(
            report, year, period,
            is_berichtigung=is_berichtigung,
            use_test=self.use_test,
        )

        # 2. Sign
        xml_signed = self._signer.sign(xml_unsigned)

        # 3. Submit
        return self._post(xml_signed, timeout=timeout)

    def _post(self, xml_bytes: bytes, timeout: int) -> SubmissionResult:
        """POST the signed XML and parse the ELSTER Rückmeldung."""
        try:
            resp = _requests.post(
                self._url,
                data=xml_bytes,
                headers={"Content-Type": "text/xml; charset=UTF-8"},
                timeout=timeout,
            )
            raw = resp.text
        except Exception as exc:
            return SubmissionResult(
                success=False,
                error_code="HTTP_ERROR",
                error_message=str(exc),
            )

        return self._parse_response(raw)

    @staticmethod
    def _parse_response(raw: str) -> SubmissionResult:
        """
        Parse the ELSTER server Rückmeldung XML.

        Success response contains:
          <TransferTicket>  — the Telenummer / filing reference
          <Erfolg>
            <Telenummer>

        Error response contains:
          <Fehler>
            <Code>
            <Meldung>
        """
        if not _LXML_AVAILABLE:
            # Fallback: basic string search
            if "<Telenummer>" in raw:
                start = raw.index("<Telenummer>") + len("<Telenummer>")
                end   = raw.index("</Telenummer>")
                return SubmissionResult(success=True, telenummer=raw[start:end], raw_response=raw)
            return SubmissionResult(success=False, error_message="Unbekannte Antwort", raw_response=raw)

        try:
            tree = etree.fromstring(raw.encode())
        except etree.XMLSyntaxError:
            return SubmissionResult(
                success=False,
                error_code="PARSE_ERROR",
                error_message=f"Ungültige XML-Antwort: {raw[:200]}",
                raw_response=raw,
            )

        def text(xpath: str) -> str | None:
            nodes = tree.xpath(f"//*[local-name()='{xpath}']")
            return nodes[0].text if nodes else None

        telenummer  = text("Telenummer")
        error_code  = text("Code")
        error_msg   = text("Meldung")

        if telenummer:
            return SubmissionResult(success=True, telenummer=telenummer, raw_response=raw)

        return SubmissionResult(
            success=False,
            error_code=error_code or "UNKNOWN",
            error_message=error_msg or "Keine Fehlermeldung in der Antwort",
            raw_response=raw,
        )

    # ------------------------------------------------------------------
    # Convenience: save signed XML without submitting (for inspection)
    # ------------------------------------------------------------------

    def export_ustva_xml(
        self,
        report: USTVAReport,
        year: int,
        period: int,
        path: str | Path,
        is_berichtigung: bool = False,
    ) -> Path:
        """
        Build and sign the UStVA XML, save to file — but do NOT submit.
        Useful for manual review before filing.
        """
        xml_unsigned = self._builder.build_ustva(
            report, year, period,
            is_berichtigung=is_berichtigung,
            use_test=self.use_test,
        )
        xml_signed = self._signer.sign(xml_unsigned)
        out = Path(path)
        out.write_bytes(xml_signed)
        return out