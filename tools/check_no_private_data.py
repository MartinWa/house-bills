"""Fail when private data is tracked by git, by name or by content.

A line that legitimately needs to look like private data (for example a security
contact address) can opt out of the content scan by carrying the marker
``hygiene: allow``.

Tracked files are scanned by default. ``--untracked`` additionally scans the files
that git reports as untracked and not ignored, which catches private data before it
is added to the index.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

BAD_SUFFIXES = (
    ".7z",
    ".bak",
    ".backup",
    ".csv",
    ".db",
    ".dump",
    ".env",
    ".gz",
    ".pdf",
    ".sql",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".zip",
)
BAD_DIRECTORIES = frozenset({"planning", "reference", "test-data"})
# A documented query example, not a database dump, stays committable despite its suffix.
ALLOWED_PATHS = frozenset({"docs/reporting-examples.sql"})
MAX_SCAN_BYTES = 2 * 1024 * 1024
ALLOW_MARKER = "hygiene: allow"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf"})
JPEG_EXIF_MARKER = b"Exif\x00\x00"
ASSIGNED_SECRET = re.compile(
    r"(?i)\b(secret[_-]?key|api[_-]?key|token|password)\b"
    r"\s*[:=]\s*[\"']([A-Za-z0-9_\-/+]{32,})[\"']"
)
# A deliberately fake value names itself as fake instead of being exempted by file.
PLACEHOLDER_SECRET_MARKERS = ("synthetic", "test-only", "replace-with", "not-for-real")

CONTENT_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("assigned secret", ASSIGNED_SECRET),
    (
        "email address",
        re.compile(
            r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org|net|invalid)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
        ),
    ),
    (
        "absolute host path",
        re.compile(
            r"(?:[A-Za-z]:\\Users\\|/Users/[A-Za-z]|/home/(?!developer\b)[A-Za-z]|/(?:root|srv|mnt)/)"
        ),
    ),
)
UNTRACKED_HELP = "also scan files that are untracked but not ignored by .gitignore"


def is_placeholder_secret(match: re.Match[str]) -> bool:
    """Return whether an assigned value names itself as a synthetic placeholder."""
    value = match.group(2).lower()
    return any(marker in value for marker in PLACEHOLDER_SECRET_MARKERS)


# Only these labels report a match conditionally; every other label reports on any match.
PLACEHOLDER_CHECKS = {"assigned secret": is_placeholder_secret}


def is_violation(path: str) -> bool:
    if path in ALLOWED_PATHS:
        return False
    candidate = PurePosixPath(path)
    if any(part in BAD_DIRECTORIES for part in candidate.parts):
        return True
    name = candidate.name.lower()
    if name.startswith(".env") and name != ".env.example":
        return True
    if ".db-" in name or ".sqlite-" in name or ".sqlite3-" in name:
        return True
    return name.endswith(BAD_SUFFIXES)


def find_violations(paths: Iterable[str]) -> list[str]:
    return sorted(path for path in paths if is_violation(path))


def png_metadata_chunks(data: bytes) -> list[str]:
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset + 8 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_type = data[offset + 4 : offset + 8]
        if chunk_type in PNG_METADATA_CHUNKS:
            chunks.append(chunk_type.decode("latin-1"))
        offset += 12 + length
        if chunk_type == b"IEND":
            break
    return chunks


def content_violations(paths: Iterable[str]) -> list[str]:
    findings = []
    for path in paths:
        candidate = Path(path)
        name = candidate.name.lower()
        try:
            if candidate.stat().st_size > MAX_SCAN_BYTES:
                continue
            if name.endswith(".png"):
                data = candidate.read_bytes()
                if data.startswith(PNG_SIGNATURE):
                    for chunk in png_metadata_chunks(data):
                        findings.append(f"{path}: PNG {chunk} metadata chunk")
                continue
            if name.endswith((".jpg", ".jpeg")):
                if JPEG_EXIF_MARKER in candidate.read_bytes():
                    findings.append(f"{path}: JPEG Exif metadata")
                continue
            text = candidate.read_text(encoding="utf-8")
        except OSError, UnicodeDecodeError:
            continue
        for line_number, line in enumerate(text.splitlines(), start=1):
            if ALLOW_MARKER in line:
                continue
            for label, pattern in CONTENT_PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if PLACEHOLDER_CHECKS.get(label, lambda _match: False)(match):
                    continue
                findings.append(f"{path}:{line_number}: {label}")
    return findings


def run_git(*arguments: str) -> list[str]:
    """Return the NUL-separated file names reported by a git listing command."""
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        check=True,
        text=False,
    )
    return [entry.decode() for entry in result.stdout.split(b"\0") if entry]


def tracked_files() -> list[str]:
    return run_git("ls-files", "-z")


def untracked_files() -> list[str]:
    return run_git("ls-files", "-z", "--others", "--exclude-standard")


def inside_work_tree() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == b"true"


def candidate_files(*, untracked: bool = False) -> list[str]:
    files = tracked_files()
    if untracked:
        files += untracked_files()
    return files


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--untracked", action="store_true", help=UNTRACKED_HELP)
    options = parser.parse_args(argv)
    try:
        if not inside_work_tree():
            print("Hygiene check failed: run it inside a git work tree.", file=sys.stderr)
            return 2
        files = candidate_files(untracked=options.untracked)
    except (subprocess.CalledProcessError, FileNotFoundError) as error:
        print(f"Hygiene check failed: git could not list files: {error}", file=sys.stderr)
        return 2
    violations = find_violations(files)
    findings = content_violations(files)
    scope = "Tracked or untracked" if options.untracked else "Tracked"
    if violations:
        print(f"{scope} private-data files found:")
        for path in violations:
            print(f"  {path}")
    if findings:
        print(f"Private data found in {scope.lower()} file contents:")
        for finding in findings:
            print(f"  {finding}")
    if violations or findings:
        return 1
    if options.untracked:
        print("Hygiene check passed: no private-data files or content are tracked or untracked.")
    else:
        print("Hygiene check passed: no private-data files or content are tracked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
