"""Shared candidate header extraction and reconciliation; no acceptance side effects."""

import re
from dataclasses import replace
from decimal import Decimal

from .normalization import DATE, NUMBER, money, rounded_vat, source_date, text
from .types import Candidate, Evidence, Identifier

AMOUNT = re.compile(rf"(?<![\w,.])(?P<amount>{NUMBER})\s+kr\b", re.I)


def last_money(line: Evidence) -> int:
    matches = list(AMOUNT.finditer(text(line)))
    if not matches:
        raise ValueError("Missing monetary amount")
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
            row = lines[horizontal + 1]
            values = list(AMOUNT.finditer(text(row)))
            if len(values) != 4:
                raise ValueError("Invalid header totals row")
            for field, key, match in zip(
                ("net_minor", "vat_minor", "rounding_minor", "amount_due_minor"),
                ("net", "vat", "rounding", "amount_due"),
                values,
                strict=True,
            ):
                candidate.header_evidence[key] = row
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


def allocate_vat(candidate: Candidate) -> Candidate:
    """Allocate explicit section VAT to period-bearing rows using stable remainders.

    Allocated line VAT is derived evidence, never a claim of a printed line tax amount.
    A discrepancy beyond the displayed-line rounding bound leaves allocation unknown.
    """
    rate = Decimal("0.25")
    for section in candidate.sections:
        if not section.charges or section.vat_minor is None or section.net_minor is None:
            continue
        if sum(charge.net_minor for charge in section.charges) != section.net_minor:
            continue  # Reconciliation flags the mismatch; do not allocate an unbounded delta.
        if (
            abs(rounded_vat(section.net_minor, rate) - section.vat_minor)
            > (len(section.charges) + 1) // 2
        ):
            continue
        taxes = [rounded_vat(charge.net_minor, rate) for charge in section.charges]
        delta = section.vat_minor - sum(taxes)
        residuals = [
            Decimal(charge.net_minor) * rate - tax
            for charge, tax in zip(section.charges, taxes, strict=True)
        ]
        order = sorted(range(len(taxes)), key=lambda i: residuals[i], reverse=delta > 0)
        for offset in range(abs(delta)):
            taxes[order[offset % len(order)]] += 1 if delta > 0 else -1
        section.charges = [
            replace(
                charge,
                vat_minor=tax,
                gross_minor=charge.net_minor + tax,
                vat_basis="allocated_from_section",
            )
            for charge, tax in zip(section.charges, taxes, strict=True)
        ]
    return candidate


def reconcile(candidate: Candidate) -> Candidate:
    """Exact allocations; explicit printed VAT is authoritative within rounding bounds."""
    if not candidate.sections:
        candidate.issues.append("No billing sections")
    for section in candidate.sections:
        if not section.charges or None in (
            section.net_minor,
            section.vat_minor,
            section.gross_minor,
        ):
            candidate.issues.append(f"Incomplete section {section.section_id}")
            continue
        if sum(c.net_minor for c in section.charges) != section.net_minor:
            candidate.issues.append(f"Net charge mismatch in {section.section_id}")
        if section.net_minor + section.vat_minor != section.gross_minor:
            candidate.issues.append(f"Gross total mismatch in {section.section_id}")
        if all(c.gross_minor is not None for c in section.charges):
            if sum(c.gross_minor for c in section.charges) != section.gross_minor:
                candidate.issues.append(f"Gross charge allocation mismatch in {section.section_id}")
            if sum(c.vat_minor for c in section.charges) != section.vat_minor:
                candidate.issues.append(f"VAT allocation mismatch in {section.section_id}")
        # Each displayed line can lose half an öre before tax calculation.
        tolerance = (len(section.charges) + 1) // 2
        if abs(rounded_vat(section.net_minor, Decimal("0.25")) - section.vat_minor) > tolerance:
            candidate.issues.append(f"VAT mismatch in {section.section_id}")
        expected_unit = {
            "water": "m3",
            "waste": "kg",
            "electricity_network": "kWh",
            "electricity_supply": "kWh",
        }[section.service]
        for usage in section.usages:
            if usage.unit != expected_unit or usage.start >= usage.end:
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
