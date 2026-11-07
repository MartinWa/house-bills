"""Electricity supply v1: an installation row above each specification."""

import re

from .common import header, last_money, reconcile
from .normalization import NUMBER, PERIOD, anchor, quantity, source_period, starts, text
from .normalization import unit as normalize_unit
from .types import Evidence, Identifier, Section, Usage

FAMILY, VERSION, REVISION = "layout_02", "v1", "3"
TITLE = "Fakturaspecifikation"
ITEM = re.compile(r"^(Medelspotpris|Rörliga kostnader|Fasta påslag|Fast avgift)\b", re.I)
QUANTITY = re.compile(rf"(?<![\w,.])({NUMBER})\s+(kWh|m[3³]|kg)\b", re.I)


def matches(pages):
    return any(
        any(anchor(line, TITLE) for line in page)
        and any(starts(line, "Anläggningsid:") for line in page)
        and any(ITEM.match(text(line)) for line in page)
        for page in pages
    )


def installation(page):
    index = next(i for i, line in enumerate(page) if starts(line, "Anläggningsid:"))
    label = page[index]
    match = re.match(r"Anläggningsid:\s*(\d{18})\b", text(label), re.I)
    if match:
        return Identifier("installation", match[1], label)
    row = page[index + 1]
    match = re.match(r"(\d{18})\b", text(row))
    if not match:
        raise ValueError("Missing installation identifier")
    return Identifier("installation", match[1], row)


def extract_supply(pages, candidate, *, title=TITLE, identify=installation):
    for page in pages:
        if not any(anchor(line, title) for line in page):
            continue
        section = Section(f"section_{len(candidate.sections) + 1:02}", "electricity_supply", [])
        candidate.sections.append(section)
        try:
            section.identifiers.append(identify(page))
        except ValueError, IndexError, StopIteration:
            candidate.issues.append(f"Missing or invalid installation in {section.section_id}")
        quantities, periods, fallback_periods = {}, {}, {}
        for line in page:
            if match := re.match(r"Leveransadress:\s*(.+)$", text(line), re.I):
                section.identifiers.append(Identifier("delivery_address", match[1], line))
            if ITEM.match(text(line)):
                match = QUANTITY.search(PERIOD.sub("", text(line)))
                try:
                    period = source_period(text(line), end_inclusive=True)
                    if not match:
                        fallback_periods.setdefault(period, line)
                        continue
                    periods.setdefault(period, line)
                    if normalize_unit(match[2]) != "kWh":
                        candidate.issues.append(f"Invalid electricity unit in {section.section_id}")
                        continue
                    billed = quantity(match[1])
                except ValueError:
                    if match:
                        candidate.issues.append(
                            f"Invalid billed electricity consumption in {section.section_id}"
                        )
                    continue
                if period in quantities and quantities[period][0] != billed:
                    candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
                quantities.setdefault(period, (billed, line))
            try:
                if starts(line, "Summa exkl moms"):
                    section.net_minor = last_money(line)
                elif starts(line, "Moms (25%)", "Moms (25 %)"):
                    section.vat_minor = last_money(line)
                    section.tax_evidence = line
                elif starts(line, "Summa inkl moms"):
                    section.gross_minor = last_money(line)
                    section.total_evidence = line
            except ValueError:
                candidate.issues.append(f"Invalid cost totals in {section.section_id}")
        periods = periods or fallback_periods
        if periods:
            section.start = min(period[0] for period in periods)
            section.end = max(period[1] for period in periods)
            section.period_evidence = periods.get((section.start, section.end)) or Evidence(
                next(iter(periods.values())).page,
                "Billing period covering the printed consumption periods.",
                (0, 0, 0, 0),
            )
        for period, (billed, line) in quantities.items():
            section.usages.append(Usage("electricity", "kWh", billed, *period, "billed", line))
        if not quantities:
            candidate.issues.append(f"Missing billed kWh in {section.section_id}")
    return reconcile(candidate)


def parse(pages: list[list[Evidence]], *, candidate=None):
    return extract_supply(pages, candidate or header(pages, FAMILY, VERSION, REVISION))
