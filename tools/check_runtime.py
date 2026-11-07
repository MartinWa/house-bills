"""Verify the actual interpreter and linked SQLite in both image targets."""

import platform
import sqlite3

import pdfplumber
import pypdfium2


def check_runtime():
    assert platform.system() == "Linux", "Use the Linux development/production container."
    assert platform.python_version() == "3.14.10", platform.python_version()
    assert sqlite3.sqlite_version_info == (3, 51, 3), sqlite3.sqlite_version
    assert pdfplumber.__version__ == "0.11.10", pdfplumber.__version__
    assert pypdfium2.PdfDocument
    print(
        f"Python {platform.python_version()}, linked SQLite {sqlite3.sqlite_version}, "
        "pdfplumber OK."
    )


if __name__ == "__main__":
    check_runtime()
