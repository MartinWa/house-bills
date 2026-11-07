"""Installed layout metadata. Recognition and parser routing come later."""

LAYOUTS = {
    "layout_01": {"v1": "1"},
    "layout_02": {"v1": "1"},
}


def layout_family_choices():
    return [(family, family) for family in LAYOUTS]
