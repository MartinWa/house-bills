"""Identify a local source checkout once, while development settings load."""

import re
import subprocess


def checkout_version(directory):
    try:
        result = subprocess.run(
            [
                "git",
                "describe",
                "--tags",
                "--always",
                "--dirty",
                "--match",
                "[0-9]*.[0-9]*.[0-9]*",
                "--match",
                "v[0-9]*.[0-9]*.[0-9]*",
            ],
            cwd=directory,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except OSError, subprocess.SubprocessError:
        return "development"
    version = result.stdout.strip()
    if re.fullmatch(
        r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
        r"(-[0-9]+-g[0-9a-f]{7,40})?(-dirty)?",
        version,
    ):
        return version
    if re.fullmatch(r"[0-9a-f]{7,40}(-dirty)?", version):
        return f"dev-{version}"
    return "development"
