"""Historical product-number network, water and supply specifications."""

import re

from . import layout_01
from .normalization import text

FAMILY, VERSION, REVISION = "layout_01", "v2", "5"
PRODUCT = re.compile(
    r"^(Elnätavgifter|Vatten & Avlopp|Elhandelskostnad|Vatten|Hushållsavfall|Avfall|Elnät|Elhandel)"
    r"\s+prodnr\s+(\d+)\b",
    re.I,
)
SECTION = re.compile(
    r"^(Elnätavgifter|Vatten & Avlopp|Elhandelskostnad|Vatten|Hushållsavfall|Avfall|Elnät|Elhandel)"
    r"\s+(?:prodnr|avtal)\s+(\d+)\b",
    re.I,
)
SERVICE = {
    **layout_01.SERVICE,
    "elnätavgifter": "electricity_network",
    "vatten & avlopp": "water",
    "elhandelskostnad": "electricity_supply",
    "avfall": "waste",
}


def matches(pages):
    return any(layout_01.specification(line) for page in pages for line in page) and any(
        PRODUCT.match(text(line)) for page in pages for line in page
    )


def read_header(pages):
    candidate = layout_01.read_header(pages)
    candidate.layout_version, candidate.parser_revision = VERSION, REVISION
    return candidate


def parse(pages, *, candidate=None):
    return layout_01.parse_sections(
        pages,
        candidate=candidate or read_header(pages),
        pattern=SECTION,
        services=SERVICE,
        legacy=True,
    )
