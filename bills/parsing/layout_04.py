"""Single-installation electricity supply with a cover totals row and specification."""

import re

from .common import reconcile
from .layout_helpers import (
    QUANTITY,
    capture,
    cost_period,
    empty_header,
    identity_fields,
    installation,
    price_quantities,
)
from .normalization import (
    DATE,
    NUMBER,
    anchor,
    money,
    quantity,
    source_date,
    source_period,
    starts,
    text,
)
from .types import Section, Usage

FAMILY, VERSION, REVISION = "layout_04", "v1", "1"
ITEM = re.compile(r"^\*?(Medelspotpris|Rörliga kostnader|Fasta påslag|Månadsavgift)\b", re.I)


def matches(pages):
    return any(
        any(anchor(line, "Specifikation:") for line in page)
        and any(re.match(rf"^Elhandel\s+{DATE}", text(line), re.I) for line in page)
        and any(ITEM.match(text(line)) for line in page)
        for page in pages
    )


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    identity_fields(
        candidate, pages, r"^Fakturanr/OCR-nr\s*:?\s*(\d+)\b", due=rf"Oss tillhanda\s*:?\s*({DATE})"
    )
    lines = pages[0] if pages else []
    fields = ("due_date", "net_minor", "vat_minor", "rounding_minor", "amount_due_minor")
    row_pattern = rf"^({DATE})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})\s+({NUMBER})$"
    for index, line in enumerate(lines):
        if anchor(line, "Oss tillhanda Netto Moms total Öresutjämning Att betala"):
            row = lines[index + 1 : index + 2]
            for group, field in enumerate(fields, 1):
                capture(
                    candidate,
                    field,
                    row,
                    row_pattern,
                    source_date if group == 1 else money,
                    group=group,
                )
            candidate.issues = [
                issue
                for issue in candidate.issues
                if issue != "Missing or invalid due_date" or candidate.due_date is None
            ]
            break
    for field in fields:
        if getattr(candidate, field) is None:
            candidate.issues.append(f"Missing or invalid {field}")
    candidate.invoice_total_minor = candidate.amount_due_minor
    if "amount_due" in candidate.header_evidence:
        candidate.header_evidence["invoice_total"] = candidate.header_evidence["amount_due"]
    return candidate


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    for page in pages:
        if not any(anchor(line, "Specifikation:") for line in page):
            continue
        section = Section(f"section_{len(candidate.sections) + 1:02}", "electricity_supply", [])
        candidate.sections.append(section)
        installation(candidate, section, pages[0] if pages else [])
        periods = {}
        for line in page:
            if re.match(rf"^Elhandel\s+{DATE}", text(line), re.I):
                try:
                    periods[source_period(text(line), end_inclusive=True)] = line
                except ValueError:
                    candidate.issues.append(f"Invalid billing period in {section.section_id}")
            try:
                if match := re.fullmatch(
                    rf"Moms\s+25[,.]00\s*%\s+({NUMBER})\s*SEK", text(line), re.I
                ):
                    section.vat_minor, section.tax_evidence = money(match[1]), line
                elif match := re.fullmatch(rf"Totalt\s+({NUMBER})\s*SEK", text(line), re.I):
                    section.gross_minor, section.total_evidence = money(match[1]), line
            except ValueError:
                candidate.issues.append(f"Invalid cost totals in {section.section_id}")
        if None not in (section.gross_minor, section.vat_minor):
            section.net_minor = section.gross_minor - section.vat_minor
        cost_period(section, periods)
        prices = price_quantities(candidate, section, page, ITEM)
        summaries = {}
        for line in page:
            if not starts(line, "Avläst", "Energiförbrukning", "Totalt") or "SEK" in text(line):
                continue
            value = text(line)
            if starts(line, "Avläst"):
                reading = re.match(rf"^Avläst\s+{DATE}\s+{NUMBER}\s+(.+)$", value, re.I)
                value = reading[1] if reading else ""
            if match := QUANTITY.search(value):
                try:
                    if match[2].casefold() != "kwh":
                        raise ValueError("Invalid electricity unit")
                    summaries.setdefault(quantity(match[1]), line)
                except ValueError:
                    candidate.issues.append(
                        f"Invalid billed electricity consumption in {section.section_id}"
                    )
        if len(summaries) > 1 or (summaries and set(summaries) != set(prices)):
            candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
        if len(prices) == 1 and len(periods) == 1:
            billed, line = next(iter(prices.items()))
            section.usages.append(
                Usage(
                    "electricity",
                    "kWh",
                    billed,
                    *next(iter(periods)),
                    "billed",
                    summaries.get(billed, line),
                )
            )
        else:
            candidate.issues.append(f"Missing or ambiguous billed kWh in {section.section_id}")
    if len(candidate.sections) != 1:
        candidate.issues.append("Multiple supply specifications require manual allocation")
    cover = []
    for line in pages[0] if pages else []:
        if match := re.fullmatch(rf"Elhandel\s+({NUMBER})\s*SEK", text(line), re.I):
            try:
                cover.append(money(match[1]))
            except ValueError:
                candidate.issues.append("Invalid cover service total")
    if len(cover) != 1 or cover[0] != sum(
        section.gross_minor or 0 for section in candidate.sections
    ):
        candidate.issues.append("Cover service total mismatch")
    return reconcile(candidate)
