"""Network and energy-tax totals with monthly billed kWh and bounded CID repair."""

import re
from datetime import timedelta

from .common import reconcile
from .layout_helpers import (
    capture,
    cost_period,
    due_amount,
    empty_header,
    installation,
    price_quantities,
)
from .normalization import DATE, NUMBER, money, normalized_text, source_date
from .types import Identifier, Section, Usage

FAMILY, VERSION, REVISION = "layout_06", "v1", "3"
CID = {196: "Ä", 197: "Å", 214: "Ö", 228: "ä", 229: "å", 246: "ö"}
COST_PERIOD = re.compile(
    rf"^(?:Kostnad|(?:Statlig )?energiskatt)\s+({DATE})\s+t\s*o\s*m\s+({DATE})$", re.I
)
ITEM = re.compile(r"^(Överföringsavgift|Rörlig elnätsavgift|Energiskatt)\b", re.I)
PRICE_TABLE = re.compile(
    r"(?:Pris (?:fr o m|from) )?Antal Pris "
    r"(?:exkl\. moms Summa|inkl\. moms Summa inkl\. moms)",
    re.I,
)
RATE_START = re.compile(
    r"^(?:Energiskatt|Rörlig elnätsavgift|Överföringsavgift)\s+(\d{1,2})\s+([a-zåäö]+)-(\d{2})\s+",
    re.I,
)


def invoice_heading(line):
    return bool(
        re.fullmatch(r"Faktura elnät(?:\s+Sid\s+\d+\s+av\s+\d+)?", network_text(line), re.I)
    )


def network_text(line):
    # Only the six visually checked Swedish character codes in this layout are repaired.
    # Evidence.text remains exactly as the reader extracted it.
    value = re.sub(r"\(cid:(\d+)\)", lambda match: CID.get(int(match[1]), match[0]), line.text)
    return normalized_text(value)


def billing_lines(lines):
    in_table = False
    for line in lines:
        value = network_text(line)
        if COST_PERIOD.fullmatch(value):
            in_table = True
            continue
        elif re.match(r"^Summa\b", value, re.I):
            in_table = False
        if in_table:
            yield line


def historical_prices(lines):
    for line in billing_lines(lines):
        value = network_text(line)
        if RATE_START.match(value) or (
            PRICE_TABLE.fullmatch(value)
            and ("exkl." in value.casefold() or value.casefold().startswith("pris "))
        ):
            return True
    return False


def matches(pages):
    lines = [line for page in pages for line in page]
    return (
        any(invoice_heading(line) for line in lines)
        and any(
            re.match(r"^(?:Statlig )?energiskatt\s", network_text(line), re.I) for line in lines
        )
        and any(
            re.match(r"^Antal Pris inkl\. moms Summa inkl\. moms$", network_text(line), re.I)
            for line in lines
        )
        and not historical_prices(lines)
    )


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    lines = pages[0] if pages else []
    capture(
        candidate,
        "reference",
        lines,
        r"^Faktura/OCR-nummer\s*:\s*([\d ]+)\b",
        lambda value: value.replace(" ", ""),
        decode=network_text,
    )
    dates = []
    for index, line in enumerate(lines):
        if invoice_heading(line):
            dates = lines[index + 1 : index + 2]
            break
    capture(candidate, "invoice_date", dates, rf"^({DATE})$", source_date, decode=network_text)
    capture(
        candidate,
        "due_date",
        lines,
        rf"^Betala senast\s*:\s*({DATE})$",
        source_date,
        decode=network_text,
    )
    capture(
        candidate,
        "net_minor",
        lines,
        rf"Momsgrundande\s*:\s*({NUMBER})\s*kr",
        money,
        decode=network_text,
    )
    capture(
        candidate,
        "vat_minor",
        lines,
        rf"Moms\s+25\s*%\s*:\s*({NUMBER})\s*kr",
        money,
        decode=network_text,
    )
    capture(
        candidate,
        "rounding_minor",
        lines,
        rf"öresutjämning\s+({NUMBER})\s*kr",
        money,
        decode=network_text,
    )
    due_amount(candidate, lines, decode=network_text)
    candidate.invoice_total_minor = candidate.amount_due_minor
    if "amount_due" in candidate.header_evidence:
        candidate.header_evidence["invoice_total"] = candidate.header_evidence["amount_due"]
    for line in lines:
        if match := re.search(r"Kundnummer\s*:\s*(\d+)", network_text(line), re.I):
            candidate.identifiers.append(Identifier("customer_account", match[1], line))
            break
    for line in lines:
        if match := re.search(rf"Ej momsgrundande\s*:\s*({NUMBER})\s*kr", network_text(line), re.I):
            try:
                if money(match[1]) != 0:
                    candidate.issues.append("Non-taxable costs require manual allocation")
            except ValueError:
                candidate.issues.append("Invalid non-taxable cost total")
    return candidate


def parse(pages, *, candidate=None):
    return parse_totals(pages, candidate=candidate)


def parse_totals(pages, *, candidate=None, price_reader=price_quantities):
    candidate = candidate or read_header(pages)
    lines = [line for page in pages for line in page]
    section = Section("section_01", "electricity_network", [])
    candidate.sections.append(section)
    installation(candidate, section, lines, decode=network_text)
    section.net_minor, section.vat_minor = candidate.net_minor, candidate.vat_minor
    section.tax_evidence = candidate.header_evidence.get("vat")
    periods, totals = {}, []
    for line in lines:
        value = network_text(line)
        if match := COST_PERIOD.fullmatch(value):
            try:
                period = source_date(match[1]), source_date(match[2]) + timedelta(days=1)
                if period[0] >= period[1]:
                    raise ValueError("Invalid service period")
                periods[period] = line
            except ValueError:
                candidate.issues.append("Invalid billing period in section_01")
        if match := re.fullmatch(
            rf"Summa(?: som .+ betalar vidare till Skatteverket)?\s+({NUMBER})\s*kr", value, re.I
        ):
            try:
                totals.append(money(match[1]))
                section.total_evidence = line
            except ValueError:
                candidate.issues.append("Invalid cost totals in section_01")
    if len(totals) == 2:
        section.gross_minor = sum(totals)
    else:
        candidate.issues.append("Missing or ambiguous network and energy-tax totals in section_01")
    cost_period(section, periods)
    prices = price_reader(candidate, section, lines, ITEM, decode=network_text)
    if len(periods) == 1 and len(prices) == 1:
        billed, line = next(iter(prices.items()))
        section.usages.append(
            Usage("electricity", "kWh", billed, *next(iter(periods)), "billed", line)
        )
    else:
        candidate.issues.append("Missing or ambiguous billed electricity consumption in section_01")
    if any("(cid:" in network_text(line) for line in lines if ITEM.match(network_text(line))):
        candidate.issues.append(
            "Unrecognized character codes in billed consumption require manual review"
        )
    return reconcile(candidate)
