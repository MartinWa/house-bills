"""PDF reading has no vendor, property or billing semantics."""

from pathlib import Path

import pdfplumber

from .types import Evidence


def read_pdf(path: str | Path) -> list[list[Evidence]]:
    pages = []
    with pdfplumber.open(path, password="") as pdf:
        for number, page in enumerate(pdf.pages, 1):
            pages.append(
                [
                    Evidence(
                        number,
                        line["text"],
                        tuple(round(line[k], 3) for k in ("x0", "top", "x1", "bottom")),
                    )
                    for line in page.extract_text_lines(return_chars=False, y_tolerance=4)
                ]
            )
            page.close()
    return pages
