"""Electricity supply v2: service-specific title and inline installation key.

The replacement structure has its own signature; v1 stays installed for old bills.
"""

import re

from .common import header
from .layout_02 import ITEM, extract_supply
from .normalization import anchor, text
from .types import Identifier

FAMILY, VERSION, REVISION = "layout_02", "v2", "2"
TITLE = "Fakturaspecifikation elhandel"
INSTALLATION = re.compile(r"^Anläggnings-ID:\s*([\d ]+)\s*$", re.I)


def matches(pages):
    return any(
        any(anchor(line, TITLE) for line in page)
        and any(INSTALLATION.match(text(line)) for line in page)
        and any(ITEM.match(text(line)) for line in page)
        for page in pages
    )


def installation(page):
    for line in page:
        if match := INSTALLATION.match(text(line)):
            value = match[1].replace(" ", "")
            if len(value) != 18:
                raise ValueError("Invalid installation identifier")
            return Identifier("installation", value, line)
    raise ValueError("Missing installation identifier")


def parse(pages, *, candidate=None):
    return extract_supply(
        pages,
        candidate or header(pages, FAMILY, VERSION, REVISION),
        title=TITLE,
        identify=installation,
    )
