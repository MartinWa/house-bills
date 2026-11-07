"""Electricity supply with an area-labelled consumption row and inclusive VAT."""

import re

from .common import reconcile
from .layout_helpers import capture, cost_period, empty_header, identity_fields, installation
from .normalization import DATE, NUMBER, anchor, money, quantity, source_date, source_period, text
from .types import Section, Usage

FAMILY, VERSION, REVISION = "layout_07", "v1", "1"
TAX = rf"^Moms\s+25\s*%\s+på\s+({NUMBER})\s*kr\s+ingår\s+med\s+({NUMBER})\s*kr$"
TOTAL = rf"^Summa\s+Elhandel\s+({NUMBER})\s*kr$"
# Consume the area's whole label before matching a possibly grouped Swedish number.
# Otherwise NUMBER treats "Område 3 786" as the valid grouped number "3 786".
CONSUMPTION = re.compile(rf"^Förbrukning(?:\s+Område\s+\d+)?\s+({NUMBER})\s*kWh\b", re.I)


def specification(page):
    return any(re.match(r"^Fakturaspecifikation\b", text(line), re.I) for line in page)


def matches(pages):
    return any(
        specification(page)
        and any(re.match(r"^Fakturaperiod\b", text(line), re.I) for line in page)
        and any(re.match(r"^Moms\s+25\s*%\s+på\b", text(line), re.I) for line in page)
        and any(re.match(r"^Summa\s+Elhandel\b", text(line), re.I) for line in page)
        for page in pages
    )


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    cover = pages[0] if pages else []
    identity_fields(candidate, pages, r"Faktura/OCR-nummer\s*:?\s*(\d+)\b")
    capture(candidate, "due_date", cover, rf"^({DATE})$", source_date)
    rows = []
    for index, line in enumerate(cover):
        if anchor(line, "Oss tillhanda Summa exkl moms Moms Öresutjämning"):
            rows.extend(cover[index + 1 : index + 4])
    amounts = rf"^({NUMBER})\s*kr\s+({NUMBER})\s*kr\s+({NUMBER})\s*kr\s+({NUMBER})\s*kr$"
    for group, field in enumerate(
        ("net_minor", "vat_minor", "rounding_minor", "amount_due_minor"), 1
    ):
        capture(candidate, field, rows, amounts, money, group=group)
    candidate.invoice_total_minor = candidate.amount_due_minor
    if "amount_due" in candidate.header_evidence:
        candidate.header_evidence["invoice_total"] = candidate.header_evidence["amount_due"]
    return candidate


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    for page in pages:
        if not specification(page):
            continue
        section = Section(f"section_{len(candidate.sections) + 1:02}", "electricity_supply", [])
        candidate.sections.append(section)
        installation(candidate, section, pages[0], pattern=r"\bAnl\s+id\s*:?\s*((?:\d\s*){18})\b")
        periods, quantities = {}, {}
        for line in page:
            value = text(line)
            try:
                if re.match(r"^Fakturaperiod\b", value, re.I):
                    periods[source_period(value, end_inclusive=True)] = line
                elif match := re.fullmatch(TAX, value, re.I):
                    section.net_minor, section.vat_minor = money(match[1]), money(match[2])
                    section.tax_evidence = line
                elif match := re.fullmatch(TOTAL, value, re.I):
                    section.gross_minor, section.total_evidence = money(match[1]), line
                elif re.match(r"^Förbrukning\b", value, re.I):
                    match = CONSUMPTION.match(value)
                    if not match:
                        raise ValueError("Missing or invalid consumption")
                    quantities.setdefault(quantity(match[1]), line)
            except ValueError:
                candidate.issues.append(f"Invalid billed field in {section.section_id}")
        cost_period(section, periods)
        if len(periods) == len(quantities) == 1:
            billed, line = next(iter(quantities.items()))
            section.usages.append(
                Usage("electricity", "kWh", billed, *next(iter(periods)), "billed", line)
            )
        else:
            candidate.issues.append(f"Missing or ambiguous billed kWh in {section.section_id}")
    if len(candidate.sections) != 1:
        candidate.issues.append("Multiple supply specifications require manual allocation")
    return reconcile(candidate)
