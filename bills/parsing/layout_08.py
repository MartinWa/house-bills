"""Compact supply summary with printed consumption, VAT and invoice rounding."""

import re
from datetime import date, timedelta

from .common import reconcile
from .layout_helpers import capture, empty_header, installation
from .normalization import NUMBER, money, quantity, source_date, text
from .types import Section, Usage

FAMILY, VERSION, REVISION = "layout_08", "v1", "3"
DAY_MONTH = r"(\d{1,2})([a-zåäö]+)\.?"
PERIOD = re.compile(rf"^Faktura{DAY_MONTH}-{DAY_MONTH}(\d{{4}})$", re.I)


def compact(line):
    # Some PDFs position glyphs without word spaces. Decode only this structure;
    # source evidence is kept untouched, including its original numeric text.
    return re.sub(r"\s+", "", text(line))


def matches(pages):
    cover = pages[0] if pages else []
    return (
        any(PERIOD.fullmatch(compact(line)) for line in cover)
        and any(
            compact(line).casefold().startswith("underperiodenharduförbrukat:") for line in cover
        )
        and any(compact(line).casefold().startswith("totalbeloppattbetala:") for line in cover)
        and any(
            "anläggningsuppgifter" in compact(line).casefold()
            for page in pages[1:]
            for line in page
        )
    )


def table_field(candidate, field, cover, label, pattern, convert=str):
    rows = []
    for marker in cover:
        if re.search(label, compact(marker), re.I):
            rows.extend(line for line in cover if 4 <= line.bbox[1] - marker.bbox[1] <= 20)
    capture(candidate, field, rows, pattern, convert)


def detail_total(candidate, lines):
    # Larger total figures can have a different top coordinate from their label.
    # The reader may also join unrelated sidebar text onto either fragment.
    amount = rf"(?:Totalbelopp:\s*)?({NUMBER})\s*kr(?=$|[^a-zåäö])"
    rows = []
    for marker in lines:
        if not compact(marker).casefold().startswith("totalbelopp:"):
            continue
        if re.match(rf"^{amount}", text(marker), re.I):
            rows.append(marker)
            continue
        rows.extend(
            line
            for line in lines
            if line.page == marker.page
            and line.bbox[0] > marker.bbox[0]
            and abs(line.bbox[1] - marker.bbox[1]) <= 20
            and re.match(rf"^{amount}", text(line), re.I)
        )
    capture(candidate, "invoice_total_minor", rows, rf"^{amount}", money)


def read_header(pages):
    candidate = empty_header(pages, FAMILY, VERSION, REVISION)
    cover = pages[0] if pages else []
    table_field(candidate, "reference", cover, r"Kundnummer:OCRNr:", r"^\d+\s+(\d+)$")
    table_field(
        candidate,
        "invoice_date",
        cover,
        r"Fakturadatum:Förfallodag:",
        r"(\d{4}-\d{2}-\d{2})",
        source_date,
    )
    capture(
        candidate,
        "due_date",
        cover,
        r"Osstillhanda:?(\d{4}-\d{2}-\d{2})",
        source_date,
        decode=compact,
    )
    capture(
        candidate,
        "amount_due_minor",
        cover,
        rf"^Totalbeloppattbetala:({NUMBER})kr$",
        money,
        decode=compact,
    )
    details = [line for page in pages[1:] for line in page]
    detail_total(candidate, details)
    capture(candidate, "vat_minor", details, rf"\(Varavmoms({NUMBER})kr\)", money, decode=compact)
    capture(
        candidate,
        "rounding_minor",
        details,
        rf"^Öresavrundning:({NUMBER})kr",
        money,
        decode=compact,
    )
    # Printed total includes rounding. Subtract printed VAT from the unrounded
    # service gross; fee taxes and rates are never synthesized.
    if None not in (candidate.invoice_total_minor, candidate.rounding_minor, candidate.vat_minor):
        candidate.net_minor = (
            candidate.invoice_total_minor - candidate.rounding_minor - candidate.vat_minor
        )
        candidate.header_evidence["net"] = candidate.header_evidence["vat"]
    return candidate


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    details = [line for page in pages[1:] for line in page]
    section = Section("section_01", "electricity_supply", [])
    candidate.sections.append(section)
    installation(candidate, section, details, r"(?:^|\s)ID:\s*((?:\d\s*){18})(?!\d)")
    section.net_minor, section.vat_minor = candidate.net_minor, candidate.vat_minor
    section.tax_evidence = candidate.header_evidence.get("vat")
    section.total_evidence = candidate.header_evidence.get("invoice_total")
    if None not in (candidate.invoice_total_minor, candidate.rounding_minor):
        section.gross_minor = candidate.invoice_total_minor - candidate.rounding_minor
    periods = {}
    for line in pages[0] if pages else []:
        if match := PERIOD.fullmatch(compact(line)):
            try:
                year = int(match[5])
                start = source_date(f"{match[1]} {match[2]} {year}")
                end = source_date(f"{match[3]} {match[4]} {year}")
                if start > end:
                    if start.month == 12 and end.month == 1:
                        start = date(year - 1, start.month, start.day)
                    else:
                        raise ValueError("Reversed billing period")
                periods[start, end + timedelta(days=1)] = line
            except ValueError:
                candidate.issues.append("Invalid billing period in section_01")
    if len(periods) == 1:
        (section.start, section.end), section.period_evidence = next(iter(periods.items()))
    else:
        candidate.issues.append("Missing or ambiguous billing period in section_01")
    quantities = []
    for line in pages[0] if pages else []:
        if compact(line).casefold().startswith("underperiodenharduförbrukat:"):
            try:
                match = re.fullmatch(
                    rf"Underperiodenharduförbrukat:({NUMBER})kWh", compact(line), re.I
                )
                if not match:
                    raise ValueError("Invalid billed consumption")
                quantities.append((quantity(match[1]), line))
            except ValueError:
                candidate.issues.append("Invalid billed electricity consumption in section_01")
    if len(quantities) == 1 and len(periods) == 1:
        billed, line = quantities[0]
        section.usages.append(
            Usage("electricity", "kWh", billed, section.start, section.end, "billed", line)
        )
    else:
        candidate.issues.append("Missing or ambiguous billed electricity consumption in section_01")
    return reconcile(candidate)
