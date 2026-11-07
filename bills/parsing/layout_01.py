"""Prototype for contract-based multi-service specifications, including water reversals."""

import re

from .common import allocate_vat, find, header, last_money, reconcile
from .normalization import DATE, NUMBER, PERIOD, money, quantity, source_period
from .types import Charge, Evidence, Identifier, Section, Usage

SERVICE = {"Vatten": "water", "Hushållsavfall": "waste", "Elnät": "electricity_network"}
SECTION = re.compile(r"^(Vatten|Hushållsavfall|Elnät) avtal (\d+)\b")
ITEM = re.compile(
    r"^(?:Grundavgift|Bostadsenhetsavgift|Dagvattenavgift|Förbrukning|"
    r"Matavfall .*kärl|Restavfall .*kärl|Hämtningsavgift|Trädgårdsavfall .*kärl|"
    r"Effektavgift|Energiavgift|Energiskatt)\b"
)
UNIT_QUANTITY = re.compile(rf"(?<![\w,.])(?P<value>{NUMBER})\s+(?P<unit>m[3³]|kWh|kg)\b")


def parse(pages: list[list[Evidence]]):
    candidate = header(pages, "layout_01")
    relevant = [
        line
        for page in pages
        if any(line.text == "FAKTURASPECIFIKATION" for line in page)
        for line in page
    ]
    blocks: list[tuple[Section, list[Evidence]]] = []
    current = None
    for line in relevant:
        if match := SECTION.match(line.text):
            current = Section(
                f"section_{len(blocks) + 1:02}",
                SERVICE[match[1]],
                [Identifier("contract", match[2], line)],
            )
            blocks.append((current, []))
        if current is not None:
            blocks[-1][1].append(line)
    waste_totals = []
    for section, lines in blocks:
        period_line = next(
            (line for line in lines if line.text.startswith(("Avstämd period", "Fakturaperiod"))),
            None,
        )
        if period_line is None:
            raise ValueError("Missing section period")
        default_period = source_period(period_line.text)
        reversing = False
        estimated = any(re.match(rf"^Beräknad\s+{DATE}", line.text) for line in lines)
        measured = any(re.match(rf"^Avläsning\s+{DATE}", line.text) for line in lines)
        for line in lines:
            if line.text.startswith("Avgår tidigare fakturerat"):
                reversing = True
            if line.text.startswith("Beräknat för ny period"):
                reversing, estimated = False, True
            if ITEM.match(line.text) and re.search(r"kr\s*$", line.text):
                period = source_period(line.text) if PERIOD.search(line.text) else default_period
                item = Charge(line.text.split("  ")[0], *period, last_money(line), line)
                section.charges.append(item)
                unit = UNIT_QUANTITY.search(PERIOD.sub("", line.text))
                if section.service == "water" and line.text.startswith("Förbrukning"):
                    if not unit or unit["unit"] not in ("m3", "m³"):
                        candidate.issues.append(f"Invalid water quantity in {section.section_id}")
                        continue
                    basis = "billed"
                    if reversing:
                        basis = "reversal"
                    elif estimated:
                        basis = "estimated"
                    elif measured:
                        basis = "measured"
                    section.usages.append(
                        Usage("water", "m3", quantity(unit["value"]), *period, basis, line)
                    )
                elif section.service == "waste" and unit and unit["unit"] == "kg":
                    section.usages.append(
                        Usage(
                            "waste", "kg", quantity(unit["value"]), *period, "billed_weight", line
                        )
                    )
            if match := re.match(rf"Moms\s+25\s*%\s+på\s+({NUMBER})\s+kr", line.text):
                if section.tax_evidence is not None:
                    raise ValueError("Multiple tax totals in one contract section")
                section.net_minor = money(match[1])
                section.vat_minor = last_money(line)
                section.gross_minor = section.net_minor + section.vat_minor
                section.tax_evidence = line
            elif section.tax_evidence is not None and line.text.startswith("Summa "):
                section.total_evidence = line
                if section.service == "waste":
                    waste_totals.append((line.page, last_money(line)))
                elif last_money(line) != section.gross_minor:
                    candidate.issues.append(
                        f"Printed service total mismatch in {section.section_id}"
                    )
        if section.service == "electricity_network":
            match, installation_ev = find(
                [line for page in pages for line in page], r"Anläggnings-ID[: ]+([\d ]+)"
            )
            installation = match[1].replace(" ", "")
            if len(installation) != 18:
                candidate.issues.append(f"Invalid installation identifier in {section.section_id}")
            section.identifiers.append(Identifier("installation", installation, installation_ev))
            match, reading = find(lines, rf"^Avläsning\s+({DATE})\s+({NUMBER})\s+kWh\b")
            billed = quantity(match[2])
            section.usages.append(
                Usage("electricity", "kWh", billed, *default_period, "measured", reading)
            )
            for line in lines:
                if line.text.startswith(("Energiavgift", "Energiskatt")):
                    unit = UNIT_QUANTITY.search(PERIOD.sub("", line.text))
                    if not unit or unit["unit"] != "kWh" or quantity(unit["value"]) != billed:
                        candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
        candidate.sections.append(section)
    if waste_totals and waste_totals[-1][1] != sum(
        s.gross_minor or 0 for s in candidate.sections if s.service == "waste"
    ):
        candidate.issues.append("Printed waste total mismatch")
    return reconcile(allocate_vat(candidate))
