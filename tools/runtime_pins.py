"""Record the pinned build interpreter and locked PDF library for runtime verification."""

import json
import platform
import sys
import tomllib
from pathlib import Path


def write_runtime_pins(lockfile, destination):
    with Path(lockfile).open("rb") as source:
        lock = tomllib.load(source)
    (pdfplumber_version,) = [
        package["version"] for package in lock["package"] if package["name"] == "pdfplumber"
    ]
    Path(destination).write_text(
        json.dumps({"python": platform.python_version(), "pdfplumber": pdfplumber_version}) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_runtime_pins(*sys.argv[1:])
