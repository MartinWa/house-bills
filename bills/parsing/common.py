"""Shared candidate header extraction and reconciliation; no acceptance side effects."""

import re

from bills.categories import usage_units

from .normalization import DATE, NUMBER, money, source_date, text
from .types import Candidate, Evidence, Identifier

AMOUNT = re.compile(rf"(?<![\w,.])(?P<amount>{NUMBER})\s+kr\b", re.I)
# A run of four or more digits directly before a match cannot start a group, so it is
# the tail of a number split at a wrong boundary ("1234 567,89", "Summa -1234 567,89")
# and must not be read as a number of its own. Shorter runs are neighbouring values
# ("Antal 12 1 080,06", "Tidigare saldo 0,00 1 350,00"). Digits attached to a word or to
# ":" and "/" belong to an identifier ("SE000000000001 473,08", "Mätarnummer:400041",
# "Andel 1/2024"), and a date or clock component stays valid ("2024-03-05 1 080,06",
# "05-03-2024 1 080,06"), as does a year after a month name ("29 mars 2024 1 080,06").
# Month names mirror source_date(), so the first three letters are enough. A minus sign
# is deliberately not an attachment: it belongs to the value, never to a date part.
MISGROUPED = re.compile(r"(?<![\w,.:/])\d{4,}$")
MONTH_TOKEN = re.compile(
    r"(?:jan|feb|mar|apr|maj|jun|jul|aug|sep|okt|nov|dec)\w*[ \u00a0\u202f]$", re.I
)
DATE_COMPONENT = re.compile(r"(?<![\w.:/-])\d{1,4}[-.:/]\d{1,2}(?:[-.:/]\d{1,4})?$")
SEPARATORS = " \u00a0\u202f"


def valid_number(value: str, match: re.Match) -> bool:
    """Reject a match that only continues a mis-grouped run of digits."""
    prefix = value[: match.start()].rstrip(SEPARATORS)
    run = MISGROUPED.search(prefix)
    if run is None:
        return True
    if MONTH_TOKEN.search(prefix[: run.start()]) is not None:
        return True
    return DATE_COMPONENT.search(prefix) is not None


def find_number(value: str, pattern: re.Pattern) -> re.Match | None:
    """Return the first match of the pattern that is not a mis-grouped tail."""
    return next((match for match in pattern.finditer(value) if valid_number(value, match)), None)


def last_money(line: Evidence) -> int:
    """Read the final amount; a malformed final match is an error, not a fallback."""
    value = text(line)
    matches = list(AMOUNT.finditer(value))
    if not matches:
        raise ValueError("Missing monetary amount")
    if not valid_number(value, matches[-1]):
        raise ValueError("Invalid monetary amount")
    return money(matches[-1]["amount"])


def find(lines: list[Evidence], pattern: str) -> tuple[re.Match, Evidence]:
    for line in lines:
        if match := re.search(pattern, text(line), re.I):
            return match, line
    raise ValueError(f"Missing required field: {pattern}")


def header(pages: list[list[Evidence]], family: str, version="v1", revision="2") -> Candidate:
    """Keep independent header fields when another field is absent or invalid."""
    lines = pages[0] if pages else []
    all_lines = [line for page in pages for line in page]
    candidate = Candidate(
        family, version, revision, "", None, None, "SEK", None, None, None, None, None, {}
    )

    def capture(field, source, pattern, convert, evidence_key=None):
        try:
            matched = [
                (match, line) for line in source if (match := re.search(pattern, text(line), re.I))
            ]
            if not matched:
                raise ValueError("Missing field")
            match, evidence = matched[0]
            candidate.header_evidence[evidence_key or field] = evidence
            setattr(candidate, field, convert(match[1]))
            if len({convert(match[1]) for match, _ in matched}) > 1:
                candidate.issues.append(f"Ambiguous {field}")
        except ValueError:
            candidate.issues.append(f"Missing or invalid {field}")

    capture(
        "reference",
        all_lines,
        r"(?:Faktura-?/OCR-nummer|OCR-/fakturanummer|OCR-nummer)[: ]+([0-9]+)",
        str,
    )
    capture("invoice_date", lines, rf"Fakturadatum[: ]+({DATE})", source_date)
    if len(candidate.reference) > 120:
        candidate.issues.append("Invalid reference length")
    capture(
        "due_date", all_lines, rf"(?:Förfallodatum|Oss tillhanda senast)[: ]+({DATE})", source_date
    )
    horizontal = next(
        (
            i
            for i, line in enumerate(lines)
            if re.search(r"Summa exkl moms Moms Öresutjämning Att betala", text(line), re.I)
        ),
        None,
    )
    if horizontal is not None:
        try:
            row_evidence = lines[horizontal + 1]
            row = text(row_evidence)
            values = [match for match in AMOUNT.finditer(row) if valid_number(row, match)]
            if len(values) != 4:
                raise ValueError("Invalid header totals row")
            for field, key, match in zip(
                ("net_minor", "vat_minor", "rounding_minor", "amount_due_minor"),
                ("net", "vat", "rounding", "amount_due"),
                values,
                strict=True,
            ):
                candidate.header_evidence[key] = row_evidence
                setattr(candidate, field, money(match["amount"]))
        except ValueError, IndexError:
            candidate.issues.append("Missing or invalid header totals row")
    else:
        for field, key, label in (
            ("amount_due_minor", "amount_due", r"Att betala(?:\s*\(SEK\))?"),
            ("net_minor", "net", r"(?:Summa exkl\.? moms|Momspliktigt belopp)"),
            ("vat_minor", "vat", r"Moms"),
            ("rounding_minor", "rounding", r"Öres(?:utjämning|avrundning)"),
        ):
            source = (
                [
                    line
                    for line in lines
                    if not re.search(r"Summa (?:exkl|inkl)|Momspliktigt belopp", text(line), re.I)
                ]
                if field == "vat_minor"
                else lines
            )
            capture(field, source, rf"(?<!\w){label}[: ]+({NUMBER})\s+kr\b", money, key)
    # These layouts have no carried balance. An explicit invoice-total line is
    # retained separately; reconciliation then requires review of any difference.
    candidate.invoice_total_minor = candidate.amount_due_minor
    if any(re.match(r"Fakturabelopp\b", text(line), re.I) for line in lines):
        capture("invoice_total_minor", lines, rf"^Fakturabelopp[: ]+({NUMBER})\s+kr", money)
    if any(re.search(r"\b(?:EUR|USD|NOK|DKK)\b", text(line), re.I) for line in lines):
        candidate.issues.append("Unsupported currency")
    try:
        customer, customer_ev = find(lines, r"Kundnummer[: ]+(\d+)")
        candidate.identifiers.append(Identifier("customer_account", customer[1], customer_ev))
    except ValueError:
        pass  # Supporting account hints are optional and never assign a property.
    return candidate


def reconcile(candidate: Candidate) -> Candidate:
    """Reconcile authoritative service totals, billing periods and billed usage."""
    if not candidate.sections:
        candidate.issues.append("No billing sections")
    for section in candidate.sections:
        if None in (
            section.net_minor,
            section.vat_minor,
            section.gross_minor,
        ):
            candidate.issues.append(f"Incomplete section {section.section_id}")
            continue
        if section.net_minor + section.vat_minor != section.gross_minor:
            candidate.issues.append(f"Gross total mismatch in {section.section_id}")
        if section.start is None or section.end is None or section.start >= section.end:
            candidate.issues.append(f"Missing or invalid billing period in {section.section_id}")
        category = "electricity" if section.service.startswith("electricity_") else section.service
        for usage in section.usages:
            expected_units = usage_units(usage.utility, usage.subcategory)
            if (
                usage.utility != category
                or usage.unit not in expected_units
                or usage.start is None
                or usage.end is None
                or usage.start >= usage.end
            ):
                candidate.issues.append(f"Invalid usage in {section.section_id}")
    if all(s.gross_minor is not None for s in candidate.sections) and None not in (
        candidate.rounding_minor,
        candidate.invoice_total_minor,
        candidate.net_minor,
        candidate.vat_minor,
    ):
        if (
            sum(s.gross_minor for s in candidate.sections) + candidate.rounding_minor
            != candidate.invoice_total_minor
        ):
            candidate.issues.append("Invoice allocation mismatch")
        if sum(s.net_minor or 0 for s in candidate.sections) != candidate.net_minor:
            candidate.issues.append("Invoice net mismatch")
        if sum(s.vat_minor or 0 for s in candidate.sections) != candidate.vat_minor:
            candidate.issues.append("Invoice VAT mismatch")
    if None not in (
        candidate.net_minor,
        candidate.vat_minor,
        candidate.rounding_minor,
        candidate.invoice_total_minor,
    ) and (
        candidate.net_minor + candidate.vat_minor + candidate.rounding_minor
        != candidate.invoice_total_minor
    ):
        candidate.issues.append("Header total mismatch")
    if candidate.amount_due_minor != candidate.invoice_total_minor:
        candidate.issues.append("Amount due differs from invoice total")
    candidate.issues = list(dict.fromkeys(candidate.issues))
    return candidate
