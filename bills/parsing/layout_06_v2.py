"""Network/tax specification with exclusive VAT prices and dated tax-rate rows."""

from . import layout_06
from .layout_helpers import QUANTITY
from .normalization import quantity, source_date

FAMILY, VERSION, REVISION = "layout_06", "v2", "2"


def matches(pages):
    lines = [line for page in pages for line in page]
    return (
        any(layout_06.invoice_heading(line) for line in lines)
        and any(layout_06.COST_PERIOD.fullmatch(layout_06.network_text(line)) for line in lines)
        and any(
            layout_06.PRICE_TABLE.fullmatch(layout_06.network_text(line))
            for line in layout_06.billing_lines(lines)
        )
        and layout_06.historical_prices(lines)
    )


def read_header(pages):
    candidate = layout_06.read_header(pages)
    candidate.layout_version, candidate.parser_revision = VERSION, REVISION
    return candidate


def price_quantities(candidate, section, lines, item, *, decode):
    if section.start is None or section.end is None:
        candidate.issues.append("Invalid billing period in section_01")
        return {}
    network, tax = {}, {}
    for line in layout_06.billing_lines(lines):
        value = decode(line)
        if not item.match(value):
            continue
        try:
            start = section.start
            date_match = layout_06.RATE_START.match(value)
            if date_match:
                year = section.start.year // 100 * 100 + int(date_match[3])
                start = source_date(f"{date_match[1]} {date_match[2]} {year}")
                if start >= section.end:
                    raise ValueError("Rate starts outside billing period")
                start = max(start, section.start)
            match = QUANTITY.search(value[date_match.end() :] if date_match else value)
            if not match or match[2].casefold() != "kwh":
                raise ValueError("Invalid billed quantity")
            billed = quantity(match[1])
            if value.casefold().startswith("energiskatt"):
                if start in tax and tax[start] != billed:
                    candidate.issues.append("Inconsistent billed kWh in section_01")
                tax.setdefault(start, billed)
            else:
                if start in network and network[start][0] != billed:
                    candidate.issues.append("Inconsistent billed kWh in section_01")
                network.setdefault(start, (billed, line))
        except ValueError:
            candidate.issues.append("Invalid billed electricity consumption in section_01")
    if (
        not network
        or not tax
        or min(network, default=None) != section.start
        or min(tax, default=None) != section.start
    ):
        candidate.issues.append("Inconsistent billed kWh in section_01")
    if network and tax:
        # Each table can split billed consumption at different price changes.
        # Compare totals only between boundaries present in both tables.
        boundaries = sorted(network.keys() & tax.keys()) + [section.end]
        for start, end in zip(boundaries, boundaries[1:], strict=False):
            if sum(billed for day, billed in tax.items() if start <= day < end) != sum(
                row[0] for day, row in network.items() if start <= day < end
            ):
                candidate.issues.append("Inconsistent billed kWh in section_01")
    return (
        {sum(row[0] for row in network.values()): next(iter(network.values()))[1]}
        if network
        else {}
    )


def parse(pages, *, candidate=None):
    return layout_06.parse_totals(
        pages, candidate=candidate or read_header(pages), price_reader=price_quantities
    )
