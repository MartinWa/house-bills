"""Contract-based multi-service v1, including water reversals and continuations."""

import re

from bills.categories import waste_cost_subcategory, waste_subcategory

from .common import AMOUNT, header, last_money, reconcile
from .electricity_demand import split_electricity_demand
from .layout_helpers import capture, cost_period
from .normalization import (
    DATE,
    NUMBER,
    PERIOD,
    money,
    quantity,
    source_date,
    source_period,
    starts,
    text,
)
from .normalization import unit as normalize_unit
from .types import Evidence, Identifier, Section, Usage

FAMILY, VERSION, REVISION = "layout_01", "v1", "12"
SERVICE = {
    "vatten": "water",
    "hushållsavfall": "waste",
    "elnät": "electricity_network",
    "elhandel": "electricity_supply",
}
SECTION = re.compile(r"^(Vatten|Hushållsavfall|Elnät|Elhandel) avtal (\d+)\b", re.I)
WASTE_USAGE = re.compile(r"^(?:Matavfall|Restavfall|Trädgårdsavfall)\b.*\bkärl\b.*\bkg\b", re.I)
UNIT_QUANTITY = re.compile(rf"(?<![\w,.])(?P<value>{NUMBER})\s+(?P<unit>m[3³]|kbm|kWh|kg)\b", re.I)
INSTALLATION = re.compile(r"(?:Anläggnings[- ]?ID|Anl\s+id)[: ]+((?:\d\s*){18})(?!\d)", re.I)


def specification(line):
    return bool(
        re.fullmatch(r"FAKTURASPECIFIKATION(?:\s+Sida\s*:\s*\d+/\d+)?[.:]?", text(line), re.I)
    )


def matches(pages):
    return (
        any(any(specification(line) for line in page) for page in pages)
        and any(SECTION.match(text(line)) for page in pages for line in page)
        and not any(
            re.match(
                r"^(?:Elnätavgifter|Vatten & Avlopp|Elhandelskostnad|Vatten|Hushållsavfall|Avfall|"
                r"Elnät|Elhandel)\s+prodnr\b",
                text(line),
                re.I,
            )
            for page in pages
            for line in page
        )
    )


def read_header(pages):
    candidate = header(pages, FAMILY, VERSION, REVISION)
    lines = [line for page in pages for line in page]
    if not candidate.reference:
        capture(candidate, "reference", lines, r"\bFakturanummer[: ]+(\d+)\b")
        if candidate.reference:
            candidate.issues.remove("Missing or invalid reference")
    # Some covers split the totals across nearby text fragments or omit VAT/rounding
    # from their introductory page. Only another explicitly labelled totals block is used.
    for marker in lines:
        if not re.search(r"Summa exkl\.? moms Moms Öresutjämning", text(marker), re.I):
            continue
        nearby = sorted(
            [
                line
                for line in lines
                if line.page == marker.page and 4 <= line.bbox[1] - marker.bbox[1] <= 25
            ],
            key=lambda line: line.bbox[0],
        )
        amounts = [(match, line) for line in nearby for match in AMOUNT.finditer(text(line))]
        if len(amounts) == 4:
            recovered = True
            for field, key, (match, line) in zip(
                ("net_minor", "vat_minor", "rounding_minor", "amount_due_minor"),
                ("net", "vat", "rounding", "amount_due"),
                amounts,
                strict=True,
            ):
                try:
                    value = money(match["amount"])
                except ValueError:
                    recovered = False
                    continue
                current = getattr(candidate, field)
                if current is not None and current != value:
                    recovered = False
                    candidate.issues.append(f"Ambiguous {field}")
                else:
                    setattr(candidate, field, value)
                    candidate.header_evidence[key] = line
                    candidate.issues = [
                        issue
                        for issue in candidate.issues
                        if issue != f"Missing or invalid {field}"
                    ]
            if recovered:
                candidate.issues = [
                    issue
                    for issue in candidate.issues
                    if issue != "Missing or invalid header totals row"
                ]
            if candidate.due_date is None:
                capture(candidate, "due_date", nearby, rf"^({DATE})\b", source_date)
                if candidate.due_date:
                    candidate.issues = [
                        issue
                        for issue in candidate.issues
                        if issue != "Missing or invalid due_date"
                    ]
    if candidate.invoice_total_minor is None:
        candidate.invoice_total_minor = candidate.amount_due_minor
    return candidate


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
    meter_first = any(re.search(r"Ställning\s+Förbrukning", text(line), re.I) for line in lines)
    for reading in readings:
        try:
            value = text(reading)
            if meter_first and re.search(r"\bkWh\s*$", value, re.I):
                # The older table prints an ungrouped meter value before billed kWh.
                pattern = rf"^Avläsning\s+({DATE})\s+\d+(?:[,.]\d+)?\s+({NUMBER})\s+kWh$"
            else:
                pattern = rf"^Avläsning\s+({DATE})\s+({NUMBER})\s+kWh(?:\s+{NUMBER})?$"
            match = re.fullmatch(pattern, value, re.I)
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
    for line in lines:
        if starts(line, "Energiavgift", "Energiskatt"):
            value = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
            try:
                price_period = source_period(text(line)) if PERIOD.search(text(line)) else period
                covered = [
                    usage
                    for usage in usages
                    if price_period[0] <= usage.start and usage.end <= price_period[1]
                ]
                consistent = (
                    value is not None
                    and normalize_unit(value["unit"]) == "kWh"
                    and bool(covered)
                    and covered[0].start == price_period[0]
                    and covered[-1].end == price_period[1]
                    and quantity(value["value"]) == sum(usage.quantity_milli for usage in covered)
                )
            except ValueError:
                consistent = False
            if not consistent:
                candidate.issues.append(f"Inconsistent billed kWh in {section.section_id}")


def _extract_section(candidate, section, lines, all_lines, waste_totals, *, legacy=False):
    if section.service == "waste":
        section.subcategory = waste_cost_subcategory(text(line) for line in lines)
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
    else:
        # Fee periods describe costs; weight collection intervals and meter events
        # retain their own separate usage periods.
        periods = {}
        for line in lines:
            if PERIOD.search(text(line)) and re.match(
                r"^(?:Grundavgift|Mätarplatsavgift|Mätaravgift|Lägenhetsavgift|Tömningsavgift|"
                r"Trädgårdsavfall|Fast årsavgift|Beräknat för ny period)\b",
                text(line),
                re.I,
            ):
                try:
                    periods[source_period(text(line))] = line
                except ValueError:
                    candidate.issues.append(f"Invalid billing period in {section.section_id}")
        cost_period(section, periods)
        if section.start is not None:
            default_period = section.start, section.end
    reversing = False
    estimated = any(re.match(rf"^Beräkn(?:ad|ing)\s+{DATE}", text(line), re.I) for line in lines)
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
                if not value:
                    raise ValueError("Invalid unit")
                source_unit = (
                    "m3"
                    if legacy and value["unit"].casefold() == "kbm"
                    else normalize_unit(value["unit"])
                )
                if source_unit != unit:
                    raise ValueError("Invalid unit")
                billed = quantity(value["value"])
                period = source_period(text(line)) if PERIOD.search(text(line)) else default_period
                if period is None:
                    raise ValueError("Missing consumption period")
                if water and reversing and not PERIOD.search(text(line)):
                    raise ValueError("Missing reversal consumption period")
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
    if section.service in {"electricity_network", "electricity_supply"}:
        # A section-local key is authoritative. A document-wide key is safe only
        # when all printed installations agree, as in the checked current example.
        keys = _installation_keys(lines) or _installation_keys(all_lines)
        if len(keys) != 1:
            candidate.issues.append(f"Ambiguous or missing installation in {section.section_id}")
        else:
            section.identifiers.append(next(iter(keys.values())))
        if default_period is not None and section.service == "electricity_network":
            _network_consumption(candidate, section, lines, default_period)
        elif default_period is not None:
            quantities = {}
            for line in lines:
                if starts(line, "Energiavgift", "Energiskatt", "Elcertifikat"):
                    match = UNIT_QUANTITY.search(PERIOD.sub("", text(line)))
                    try:
                        if not match or normalize_unit(match["unit"]) != "kWh":
                            raise ValueError("Invalid supply consumption")
                        period = (
                            source_period(text(line))
                            if PERIOD.search(text(line))
                            else default_period
                        )
                        billed = quantity(match["value"])
                        if period in quantities and quantities[period][0] != billed:
                            candidate.issues.append(
                                f"Inconsistent billed kWh in {section.section_id}"
                            )
                        quantities.setdefault(period, (billed, line))
                    except ValueError:
                        candidate.issues.append(
                            f"Invalid billed electricity consumption in {section.section_id}"
                        )
            for period, (billed, line) in quantities.items():
                section.usages.append(Usage("electricity", "kWh", billed, *period, "billed", line))
            if not quantities:
                candidate.issues.append(
                    f"Missing billed electricity consumption in {section.section_id}"
                )


def parse(pages: list[list[Evidence]], *, candidate=None):
    return parse_sections(pages, candidate=candidate or read_header(pages))


def parse_sections(pages, *, candidate, pattern=SECTION, services=SERVICE, legacy=False):
    relevant = [
        line for page in pages if any(specification(line) for line in page) for line in page
    ]
    blocks = []
    current = None
    for line in relevant:
        if match := pattern.match(text(line)):
            kind = "product" if legacy and "prodnr" in text(line).casefold() else "contract"
            current = Section(
                f"section_{len(blocks) + 1:02}",
                services[match[1].casefold()],
                [Identifier(kind, match[2], line)],
            )
            candidate.sections.append(current)
            blocks.append((current, []))
        if current is not None:
            blocks[-1][1].append(line)
    waste_totals = []
    all_lines = [line for page in pages for line in page]
    for section, lines in blocks:
        try:
            _extract_section(candidate, section, lines, all_lines, waste_totals, legacy=legacy)
        except ValueError:
            candidate.issues.append(f"Incomplete or invalid billing fields in {section.section_id}")
    if waste_totals and waste_totals[-1][1] != sum(
        s.gross_minor or 0 for s in candidate.sections if s.service == "waste"
    ):
        candidate.issues.append("Printed waste total mismatch")
    _, warnings = split_electricity_demand(candidate, pages)
    candidate.issues.extend(warnings)
    return reconcile(candidate)
