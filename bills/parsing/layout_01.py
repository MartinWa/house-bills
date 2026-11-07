"""Contract-based multi-service v1, including water reversals and continuations."""

import re

from .common import allocate_vat, header, last_money, reconcile
from .normalization import (
    DATE,
    NUMBER,
    PERIOD,
    anchor,
    money,
    quantity,
    source_period,
    starts,
    text,
)
from .normalization import unit as normalize_unit
from .types import Charge, Evidence, Identifier, Section, Usage

FAMILY, VERSION, REVISION = "layout_01", "v1", "2"
SERVICE = {"vatten": "water", "hushållsavfall": "waste", "elnät": "electricity_network"}
SECTION = re.compile(r"^(Vatten|Hushållsavfall|Elnät) avtal (\d+)\b", re.I)
ITEM = re.compile(
    r"^(?:Grundavgift|Bostadsenhetsavgift|Dagvattenavgift|Förbrukning|"
    r"Matavfall .*kärl|Restavfall .*kärl|Hämtningsavgift|Trädgårdsavfall .*kärl|"
    r"Effektavgift|Energiavgift|Energiskatt)\b",
    re.I,
)
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


def _extract_section(candidate, section, lines, all_lines, waste_totals):
    for line in lines:
        if match := re.match(r"Leveransadress:\s*(.+)$", text(line), re.I):
            section.identifiers.append(Identifier("delivery_address", match[1], line))
    period_line = next(
        (line for line in lines if starts(line, "Avstämd period", "Fakturaperiod")), None
    )
    if period_line is None:
        raise ValueError("Missing section period")
    default_period = source_period(text(period_line))
    reversing = False
    estimated = any(re.match(rf"^Beräknad\s+{DATE}", text(line), re.I) for line in lines)
    measured = any(re.match(rf"^Avläsning\s+{DATE}", text(line), re.I) for line in lines)
    for line in lines:
        if starts(line, "Avgår tidigare fakturerat"):
            reversing = True
        if starts(line, "Beräknat för ny period"):
            reversing, estimated = False, True
        if ITEM.match(text(line)) and re.search(r"kr\s*$", text(line), re.I):
            period = source_period(text(line)) if PERIOD.search(text(line)) else default_period
            section.charges.append(Charge(text(line), *period, last_money(line), line))
            value = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
            if section.service == "water" and starts(line, "Förbrukning"):
                if not value or normalize_unit(value["unit"]) != "m3":
                    candidate.issues.append(f"Invalid water quantity in {section.section_id}")
                    continue
                basis = (
                    "reversal"
                    if reversing
                    else ("estimated" if estimated else "measured" if measured else "billed")
                )
                section.usages.append(
                    Usage("water", "m3", quantity(value["value"]), *period, basis, line)
                )
            elif section.service == "waste" and value:
                if normalize_unit(value["unit"]) != "kg":
                    candidate.issues.append(f"Invalid waste quantity in {section.section_id}")
                    continue
                section.usages.append(
                    Usage("waste", "kg", quantity(value["value"]), *period, "billed_weight", line)
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
        readings = []
        for line in lines:
            if match := re.match(rf"^Avläsning\s+({DATE})\s+({NUMBER})\s+kWh\b", text(line), re.I):
                readings.append((match, line))
        if len(readings) != 1:
            raise ValueError("Missing or ambiguous billed reading")
        match, reading = readings[0]
        billed = quantity(match[2])
        section.usages.append(
            Usage("electricity", "kWh", billed, *default_period, "measured", reading)
        )
        for line in lines:
            if starts(line, "Energiavgift", "Energiskatt"):
                value = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
                if (
                    not value
                    or normalize_unit(value["unit"]) != "kWh"
                    or quantity(value["value"]) != billed
                ):
                    candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")


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
    return reconcile(allocate_vat(candidate))
