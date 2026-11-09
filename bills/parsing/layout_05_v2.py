"""Supply specification with explicit billed meter-table consumption beside a chart."""

from . import layout_05
from .normalization import anchor, text

FAMILY, VERSION, REVISION = "layout_05", "v2", "1"


def matches(pages):
    return any(
        any(anchor(line, "Det här betalar du för:") for line in page)
        and any(anchor(line, "Datum Mätarställning") for line in page)
        and any("anläggnings-id:" in text(line).casefold() for line in page)
        and not any(anchor(line, "Period Förbrukning") for line in page)
        for page in pages
    )


def read_header(pages):
    candidate = layout_05.read_header(pages)
    candidate.layout_version, candidate.parser_revision = VERSION, REVISION
    return candidate


def parse(pages, *, candidate=None):
    return layout_05.parse_specifications(
        pages, candidate=candidate or read_header(pages), meter_table=True
    )
