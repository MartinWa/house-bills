"""Example plugin: deterministic extraction for the layout_02_v2 invoice structure."""

import re

from bills.parsing import api

from .layout_02 import ITEM, extract_supply

API_VERSION = 1

Identifier = api.Identifier
anchor = api.anchor
header = api.header
text = api.text

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
