"""Prototype for one electricity-supply specification per installation/page."""

import re

from .common import allocate_vat, header, last_money, reconcile
from .normalization import NUMBER, PERIOD, quantity, source_period
from .types import Charge, Evidence, Identifier, Section, Usage

ITEM = re.compile(r"^(Medelspotpris|Rörliga kostnader|Fasta påslag|Fast avgift)\b")


def parse(pages: list[list[Evidence]]):
    candidate = header(pages, "layout_02")
    for page in pages:
        if not any(line.text == "Fakturaspecifikation." for line in page):
            continue
        index = next(i for i, line in enumerate(page) if line.text.startswith("Anläggningsid:"))
        id_line = page[index + 1]
        identifier = re.match(r"(\d{18})\b", id_line.text)
        if not identifier:
            raise ValueError("Missing installation identifier")
        section = Section(
            f"section_{len(candidate.sections) + 1:02}",
            "electricity_supply",
            [Identifier("installation", identifier[1], id_line)],
        )
        quantities = {}
        for line in page:
            if ITEM.match(line.text):
                period = source_period(line.text, end_inclusive=True)
                section.charges.append(Charge(line.text, *period, last_money(line), line))
                if match := re.search(rf"(?<![\w,.])({NUMBER})\s+kWh\b", PERIOD.sub("", line.text)):
                    billed = quantity(match[1])
                    if period in quantities and quantities[period][0] != billed:
                        candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")
                    quantities.setdefault(period, (billed, line))
            elif line.text.startswith("Summa exkl moms"):
                section.net_minor = last_money(line)
            elif line.text.startswith("Moms (25%)"):
                section.vat_minor = last_money(line)
                section.tax_evidence = line
            elif line.text.startswith("Summa inkl moms"):
                section.gross_minor = last_money(line)
                section.total_evidence = line
        for period, (billed, line) in quantities.items():
            section.usages.append(Usage("electricity", "kWh", billed, *period, "billed", line))
        if not quantities:
            candidate.issues.append(f"Missing billed kWh in {section.section_id}")
        candidate.sections.append(section)
    return reconcile(allocate_vat(candidate))
