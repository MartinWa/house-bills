"""Contract-based multi-service v1, including water reversals and continuations."""

import re

from bills.categories import waste_subcategory

from .common import header, last_money, reconcile
from .normalization import (
    DATE,
    NUMBER,
    PERIOD,
    anchor,
    money,
    quantity,
    source_date,
    source_period,
    starts,
    text,
)
from .normalization import unit as normalize_unit
from .types import Evidence, Identifier, Section, Usage

FAMILY, VERSION, REVISION = "layout_01", "v1", "5"
SERVICE = {"vatten": "water", "hushållsavfall": "waste", "elnät": "electricity_network"}
SECTION = re.compile(r"^(Vatten|Hushållsavfall|Elnät) avtal (\d+)\b", re.I)
WASTE_USAGE = re.compile(r"^(?:Matavfall|Restavfall)\b.*\bkärl\b", re.I)
UNIT_QUANTITY = re.compile(rf"(?<![\w,.])(?P<value>{NUMBER})\s+(?P<unit>m[3³]|kWh|kg)\b", re.I)
INSTALLATION = re.compile(r"Anläggnings-ID[: ]+([\d ]+)", re.I)


def matches(pages):
    return any(
        any(anchor(line, "FAKTURASPECIFIKATION") for line in page) for page in pages
    ) and any(SECTION.match(text(line)) for page in pages for line in page)


def _installation_keys(lines):
    keys = {}
    for line in lines:
        if match := INSTALLATION.search(text(line)):
            value = match[1].replace(" ", "")
            if len(value) != 18:
                raise ValueError("Invalid installation identifier")
            keys[value] = Identifier("installation", value, line)
    return keys


def _network_consumption(candidate, section, lines, period):
    readings = [line for line in lines if re.match(r"^Avläsning\b", text(line), re.I)]
    if not readings:
        candidate.issues.append(f"Missing billed electricity consumption in {section.section_id}")
        return
    usages = []
    previous, end = period
    for reading in readings:
        try:
            match = re.match(rf"^Avläsning\s+({DATE})\s+({NUMBER})\s+kWh\b", text(reading), re.I)
            if not match:
                raise ValueError("Invalid reading")
            day, billed = source_date(match[1]), quantity(match[2])
        except ValueError:
            candidate.issues.append(
                f"Invalid billed electricity consumption in {section.section_id}"
            )
            return
        if not previous < day <= end:
            candidate.issues.append(
                f"Ambiguous electricity reading periods in {section.section_id}"
            )
            return
        usages.append(Usage("electricity", "kWh", billed, previous, day, "measured", reading))
        previous = day
    if previous != end:
        candidate.issues.append(f"Incomplete electricity reading periods in {section.section_id}")
        return
    section.usages.extend(usages)
    billed = sum(usage.quantity_milli for usage in usages)
    for line in lines:
        if starts(line, "Energiavgift", "Energiskatt"):
            value = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
            try:
                consistent = (
                    value is not None
                    and normalize_unit(value["unit"]) == "kWh"
                    and quantity(value["value"]) == billed
                )
            except ValueError:
                consistent = False
            if not consistent:
                candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")


def _extract_section(candidate, section, lines, all_lines, waste_totals):
    for line in lines:
        if match := re.match(r"Leveransadress:\s*(.+)$", text(line), re.I):
            section.identifiers.append(Identifier("delivery_address", match[1], line))
    period_line = next(
        (line for line in lines if starts(line, "Avstämd period", "Fakturaperiod")), None
    )
    default_period = None
    if period_line is not None:
        try:
            default_period = source_period(text(period_line))
            section.start, section.end = default_period
            section.period_evidence = period_line
        except ValueError:
            pass  # Reconciliation names a missing/invalid allocation period.
    reversing = False
    estimated = any(re.match(rf"^Beräknad\s+{DATE}", text(line), re.I) for line in lines)
    measured = any(re.match(rf"^Avläsning\s+{DATE}", text(line), re.I) for line in lines)
    for line in lines:
        if starts(line, "Avgår tidigare fakturerat"):
            reversing = True
        if starts(line, "Beräknat för ny period"):
            reversing, estimated = False, True
        water = section.service == "water" and starts(line, "Förbrukning")
        waste = section.service == "waste" and WASTE_USAGE.match(text(line))
        if water or waste:
            value = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
            utility, unit = ("water", "m3") if water else ("waste", "kg")
            try:
                if not value or normalize_unit(value["unit"]) != unit:
                    raise ValueError("Invalid unit")
                billed = quantity(value["value"])
                period = source_period(text(line)) if PERIOD.search(text(line)) else default_period
                if period is None:
                    raise ValueError("Missing consumption period")
            except ValueError:
                candidate.issues.append(
                    f"Invalid {utility} quantity or period in {section.section_id}"
                )
                continue
            if water:
                basis = (
                    "reversal"
                    if reversing
                    else ("estimated" if estimated else "measured" if measured else "billed")
                )
                section.usages.append(Usage("water", "m3", billed, *period, basis, line))
            else:
                section.usages.append(
                    Usage(
                        "waste",
                        "kg",
                        billed,
                        *period,
                        "billed_weight",
                        line,
                        subcategory=waste_subcategory(text(line)),
                    )
                )
        if match := re.match(rf"Moms\s+25\s*%\s+på\s+({NUMBER})\s+kr", text(line), re.I):
            if section.tax_evidence is not None:
                raise ValueError("Multiple tax totals in one contract section")
            section.net_minor = money(match[1])
            section.vat_minor = last_money(line)
            section.gross_minor = section.net_minor + section.vat_minor
            section.tax_evidence = line
        elif section.tax_evidence is not None and starts(line, "Summa "):
            section.total_evidence = line
            if section.service == "waste":
                waste_totals.append((line.page, last_money(line)))
            elif last_money(line) != section.gross_minor:
                candidate.issues.append(f"Printed service total mismatch in {section.section_id}")
    if section.service == "electricity_network":
        # A section-local key is authoritative. A document-wide key is safe only
        # when all printed installations agree, as in the checked current example.
        keys = _installation_keys(lines) or _installation_keys(all_lines)
        if len(keys) != 1:
            candidate.issues.append(f"Ambiguous or missing installation in {section.section_id}")
        else:
            section.identifiers.append(next(iter(keys.values())))
        if default_period is not None:
            _network_consumption(candidate, section, lines, default_period)


def parse(pages: list[list[Evidence]], *, candidate=None):
    candidate = candidate or header(pages, FAMILY, VERSION, REVISION)
    relevant = [
        line
        for page in pages
        if any(anchor(line, "FAKTURASPECIFIKATION") for line in page)
        for line in page
    ]
    blocks = []
    current = None
    for line in relevant:
        if match := SECTION.match(text(line)):
            current = Section(
                f"section_{len(blocks) + 1:02}",
                SERVICE[match[1].casefold()],
                [Identifier("contract", match[2], line)],
            )
            candidate.sections.append(current)
            blocks.append((current, []))
        if current is not None:
            blocks[-1][1].append(line)
    waste_totals = []
    all_lines = [line for page in pages for line in page]
    for section, lines in blocks:
        try:
            _extract_section(candidate, section, lines, all_lines, waste_totals)
        except ValueError:
            candidate.issues.append(f"Incomplete or invalid billing fields in {section.section_id}")
    if waste_totals and waste_totals[-1][1] != sum(
        s.gross_minor or 0 for s in candidate.sections if s.service == "waste"
    ):
        candidate.issues.append("Printed waste total mismatch")
    return reconcile(candidate)
