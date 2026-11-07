"""Installed versions coexist; structural matches never depend on invoice date."""

from . import layout_01, layout_02, layout_02_v2, layout_03, layout_04, layout_05, layout_06

PARSERS = (layout_01, layout_02, layout_02_v2, layout_03, layout_04, layout_05, layout_06)
LAYOUTS = {}
for parser in PARSERS:
    LAYOUTS.setdefault(parser.FAMILY, {})[parser.VERSION] = parser.REVISION


def layout_family_choices():
    return [(family, family) for family in LAYOUTS]


def match_layouts(pages, families):
    return [parser for parser in PARSERS if parser.FAMILY in families and parser.matches(pages)]
