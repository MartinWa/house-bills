"""Electricity supply with an explicit consumption table beside a historical chart."""

import re

from .common import reconcile
from .layout_helpers import (
    QUANTITY,
    capture,
    cost_period,
    due_amount,
    empty_header,
    identity_fields,
    installation,
    tax_basis,
)
from .normalization import DATE, NUMBER, PERIOD, anchor, money, quantity, source_period, text
from .types import Section, Usage

FAMILY, VERSION, REVISION = "layout_05", "v1", "1"
TAX = rf"^Moms\s+25\s*%\s+av\s+({NUMBER})\s*kr\s+({NUMBER})\s*kr$"
TOTAL = rf"^Summa\s+[^\d]+\s+({NUMBER})\s*kr$"
ITEM = re.compile(r"^(Medelspotpris|Rörliga kostnader|Fast påslag|Årsavgift)\b", re.I)


def matches(pages):
    return any(
        any(anchor(line, "Det här betalar du för:") for line in page)
        and any(anchor(line, "Period Förbrukning") for line in page)
        and any(re.search(r"Anläggnings-ID\s*:", text(line), re.I) for line in page)
        for page in pages
    )


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    lines = [line for page in pages for line in page]
    identity_fields(
        candidate,
        pages,
        r"Fakturanummer\s*:\s*(\d+)\b",
        due=rf"(?:Förfallodatum|Oss tillhanda senast)\s*:\s*({DATE})",
    )
    tax_basis(candidate, lines, TAX)
    capture(
        candidate,
        "rounding_minor",
        lines,
        rf"Öres(?:avrundning|utjämning)\s*:?\s*({NUMBER})\s*kr",
        money,
        optional=True,
    )
    if candidate.rounding_minor is None:
        candidate.rounding_minor = 0
    due_amount(candidate, pages[0] if pages else [])
    capture(candidate, "invoice_total_minor", lines, TOTAL, money)
    if candidate.invoice_total_minor is not None:
        candidate.invoice_total_minor += candidate.rounding_minor
    return candidate


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    for page in pages:
        if not any(anchor(line, "Det här betalar du för:") for line in page):
            continue
        section = Section(f"section_{len(candidate.sections) + 1:02}", "electricity_supply", [])
        candidate.sections.append(section)
        installation(candidate, section, page)
        summary = {}
        in_consumption, in_cost = False, False
        price_values = {}
        periods = {}
        for line in page:
            value = text(line)
            if anchor(line, "Period Förbrukning"):
                in_consumption = True
            elif anchor(line, "Det här betalar du för:"):
                in_consumption, in_cost = False, True
            if in_consumption and (period_match := PERIOD.search(value)):
                match = QUANTITY.search(value[period_match.end() :])
                try:
                    if not match or match[2].casefold() != "kwh":
                        raise ValueError("Missing or invalid consumption")
                    period = source_period(value, end_inclusive=True)
                    billed = quantity(match[1])
                    if period in summary and summary[period][0] != billed:
                        candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
                    summary.setdefault(period, (billed, line))
                except ValueError:
                    candidate.issues.append(
                        f"Invalid billed electricity consumption in {section.section_id}"
                    )
            if in_cost and ITEM.match(value) and PERIOD.search(value):
                try:
                    period = source_period(value, end_inclusive=True)
                    periods[period] = line
                    if match := QUANTITY.search(value[PERIOD.search(value).end() :]):
                        if match[2].casefold() != "kwh":
                            raise ValueError("Invalid electricity unit")
                        price_values.setdefault(period, set()).add(quantity(match[1]))
                except ValueError:
                    candidate.issues.append(
                        f"Invalid billed electricity consumption in {section.section_id}"
                    )
            try:
                if in_cost and (match := re.fullmatch(TAX, value, re.I)):
                    section.net_minor, section.vat_minor = money(match[1]), money(match[2])
                    section.tax_evidence = line
                elif in_cost and (match := re.fullmatch(TOTAL, value, re.I)):
                    section.gross_minor, section.total_evidence = money(match[1]), line
                    in_cost = False
            except ValueError:
                candidate.issues.append(f"Invalid cost totals in {section.section_id}")
        cost_period(section, periods)
        for period, (billed, line) in summary.items():
            section.usages.append(Usage("electricity", "kWh", billed, *period, "billed", line))
        if not summary:
            candidate.issues.append(f"Missing billed consumption table in {section.section_id}")
        if set(summary) != set(price_values) or any(
            price_values.get(period) != {row[0]} for period, row in summary.items()
        ):
            candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
    if len(candidate.sections) != 1:
        candidate.issues.append("Multiple supply specifications require manual allocation")
    return reconcile(candidate)
