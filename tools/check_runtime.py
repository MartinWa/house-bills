"""Verify the actual interpreter and linked SQLite in both image targets."""

import json
import platform
import sqlite3
from pathlib import Path

import pdfplumber
import pypdfium2


def check_runtime(pins_path=Path("/opt/house-bills/runtime-pins.json")):
    pins = json.loads(Path(pins_path).read_text(encoding="utf-8"))
    assert platform.system() == "Linux", "Use the Linux development/production container."
    assert platform.python_version() == pins["python"], platform.python_version()
    assert sqlite3.sqlite_version_info == (3, 51, 3), sqlite3.sqlite_version
    assert pdfplumber.__version__ == pins["pdfplumber"], pdfplumber.__version__
    assert pypdfium2.PdfDocument
    print(
        f"Python {platform.python_version()}, linked SQLite {sqlite3.sqlite_version}, "
        "pdfplumber OK."
    )


if __name__ == "__main__":
    check_runtime()
