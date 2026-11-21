"""Load the bundled example parser plugins as ordinary importable modules.

The application installs parser code only from a plugin directory, so the examples
in `docs/examples/parsers` are not importable as `bills.parsing.layout_*` modules.
Tests that exercise extraction logic import them here, and `HOUSE_BILLS_PARSER_PLUGINS`
points at the same directory so ingestion, routing and configured splits run too.
"""

import importlib
import sys
import types
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples" / "parsers"

_package = types.ModuleType("example_parsers")
_package.__path__ = [str(EXAMPLES)]
sys.modules["example_parsers"] = _package

layout_01 = importlib.import_module("example_parsers.layout_01")
layout_01_v2 = importlib.import_module("example_parsers.layout_01_v2")
layout_02 = importlib.import_module("example_parsers.layout_02")
layout_02_v2 = importlib.import_module("example_parsers.layout_02_v2")
layout_03 = importlib.import_module("example_parsers.layout_03")
layout_04 = importlib.import_module("example_parsers.layout_04")
layout_05 = importlib.import_module("example_parsers.layout_05")
layout_05_v2 = importlib.import_module("example_parsers.layout_05_v2")
layout_06 = importlib.import_module("example_parsers.layout_06")
layout_06_v2 = importlib.import_module("example_parsers.layout_06_v2")
layout_07 = importlib.import_module("example_parsers.layout_07")
layout_08 = importlib.import_module("example_parsers.layout_08")


def installed_identity(family, version):
    """Recorded routing identity for an installed parser, as ingestion writes it."""
    from bills.parsing import registry

    parser = registry.installed(family, version)
    assert parser is not None
    return {
        "family": parser.FAMILY,
        "version": parser.VERSION,
        "revision": parser.REVISION,
        "fingerprint": parser.FINGERPRINT,
    }


__all__ = [
    "EXAMPLES",
    "installed_identity",
    "layout_01",
    "layout_01_v2",
    "layout_02",
    "layout_02_v2",
    "layout_03",
    "layout_04",
    "layout_05",
    "layout_05_v2",
    "layout_06",
    "layout_06_v2",
    "layout_07",
    "layout_08",
]
