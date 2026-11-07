"""PDF reading has no vendor, property or billing semantics."""

from pathlib import Path
from typing import BinaryIO

import pdfplumber

from .types import Evidence


class PDFReadError(ValueError):
    def __init__(self, message, pages):
        super().__init__(message)
        self.pages = pages


def read_pdf(path: str | Path | BinaryIO) -> list[list[Evidence]]:
    pages = []
    try:
        with pdfplumber.open(path, password="") as pdf:
            if len(pdf.pages) > 100:
                raise PDFReadError("PDF exceeds the 100-page parsing limit.", pages)
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
    except PDFReadError:
        raise
    except Exception:
        # Decoder messages may contain source text; retain only a safe reason.
        raise PDFReadError("PDF is unreadable or password-protected.", pages) from None
    return pages
