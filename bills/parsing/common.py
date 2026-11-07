"""Shared candidate header extraction and reconciliation; no acceptance side effects."""

import re
from dataclasses import replace
from decimal import Decimal

from .normalization import DATE, NUMBER, money, rounded_vat, source_date
from .types import Candidate, Evidence, Identifier

AMOUNT = re.compile(rf"(?<![\w,.])(?P<amount>{NUMBER})\s+kr\b", re.I)


def last_money(line: Evidence) -> int:
    matches = list(AMOUNT.finditer(line.text))
    if not matches:
        raise ValueError("Missing monetary amount")
    return money(matches[-1]["amount"])


def find(lines: list[Evidence], pattern: str) -> tuple[re.Match, Evidence]:
    for line in lines:
        if match := re.search(pattern, line.text, re.I):
            return match, line
    raise ValueError(f"Missing required field: {pattern}")


def header(pages: list[list[Evidence]], family: str) -> Candidate:
    lines = pages[0]
    all_lines = [line for page in pages for line in page]
    ref, ref_ev = find(
        all_lines, r"(?:Faktura-?/OCR-nummer|OCR-/fakturanummer|OCR-nummer)[: ]+([0-9]+)"
    )
    issued, issued_ev = find(lines, rf"Fakturadatum[: ]+({DATE})")
    due, due_ev = find(all_lines, rf"(?:Förfallodatum|Oss tillhanda senast)[: ]+({DATE})")
    horizontal = next(
        (
            i
            for i, line in enumerate(lines)
            if re.search(r"Summa exkl moms Moms Öresutjämning Att betala", line.text)
        ),
        None,
    )
    if horizontal is not None:
        row = lines[horizontal + 1]
        values = list(AMOUNT.finditer(row.text))
        if len(values) != 4:
            raise ValueError("Invalid header totals row")
        net, vat, rounding, amount = (money(m["amount"]) for m in values)
        net_ev = vat_ev = rounding_ev = amount_ev = row
    else:
        _, amount_ev = find(lines, rf"Att betala(?:\s*\(SEK\))?[: ]+{NUMBER}\s+kr")
        _, net_ev = find(lines, rf"(?:Summa exkl\.? moms|Momspliktigt belopp)[: ]+{NUMBER}\s+kr")
        _, vat_ev = find(
            lines, rf"^(?!.*(?:Summa exkl|Momspliktigt belopp)).*?\bMoms[: ]+{NUMBER}\s+kr"
        )
        _, rounding_ev = find(lines, rf"Öres(?:utjämning|avrundning)[: ]+{NUMBER}\s+kr")
        net, vat, rounding, amount = (
            last_money(ev) for ev in (net_ev, vat_ev, rounding_ev, amount_ev)
        )
    candidate = Candidate(
        family,
        "v1",
        "1",
        ref[1],
        source_date(issued[1]),
        source_date(due[1]),
        "SEK",
        net,
        vat,
        rounding,
        amount,
        amount,
        {
            "reference": ref_ev,
            "invoice_date": issued_ev,
            "due_date": due_ev,
            "net": net_ev,
            "vat": vat_ev,
            "rounding": rounding_ev,
            "amount_due": amount_ev,
        },
    )
    customer, customer_ev = find(lines, r"Kundnummer[: ]+(\d+)")
    candidate.identifiers.append(Identifier("customer_account", customer[1], customer_ev))
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
    if all(s.gross_minor is not None for s in candidate.sections):
        if (
            sum(s.gross_minor for s in candidate.sections) + candidate.rounding_minor
            != candidate.invoice_total_minor
        ):
            candidate.issues.append("Invoice allocation mismatch")
        if sum(s.net_minor or 0 for s in candidate.sections) != candidate.net_minor:
            candidate.issues.append("Invoice net mismatch")
        if sum(s.vat_minor or 0 for s in candidate.sections) != candidate.vat_minor:
            candidate.issues.append("Invoice VAT mismatch")
    if (
        candidate.net_minor + candidate.vat_minor + candidate.rounding_minor
        != candidate.invoice_total_minor
    ):
        candidate.issues.append("Header total mismatch")
    return candidate
