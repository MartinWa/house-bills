"""Fail when private data is tracked by git, by name or by content.

A line that legitimately needs to look like private data (for example a security
contact address) can opt out of the content scan by carrying the marker
``hygiene: allow``.

Tracked files are read from the git index, so the scan covers what a commit would
contain even when the working copy differs. ``--untracked`` additionally scans
working-tree files that git reports as untracked and not ignored. Symlinks are never
followed, image metadata is streamed instead of loaded whole, and a file that cannot
be inspected is reported rather than passed.
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
READ_CHUNK_BYTES = 64 * 1024
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
    (
        "GitHub token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}"),
    ),
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


def skip_bytes(stream, count: int) -> bool:
    """Read past `count` bytes in bounded chunks; return whether the stream was long enough."""
    while count > 0:
        chunk = stream.read(min(count, READ_CHUNK_BYTES))
        if not chunk:
            return False
        count -= len(chunk)
    return True


def text_findings(path: str, stream) -> list[str]:
    data = stream.read(MAX_SCAN_BYTES + 1)
    if len(data) > MAX_SCAN_BYTES:
        return []
    findings = []
    for line_number, line in enumerate(data.decode("utf-8", errors="ignore").splitlines(), 1):
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


def png_findings(path: str, stream) -> list[str]:
    """Walk PNG chunk headers, skipping payloads without buffering them."""
    if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
        return []
    findings = []
    while True:
        header = stream.read(8)
        if len(header) < 8:
            findings.append(f"{path}: truncated PNG chunk header")
            break
        length = int.from_bytes(header[:4], "big")
        chunk_type = header[4:8]
        if chunk_type in PNG_METADATA_CHUNKS:
            findings.append(f"{path}: PNG {chunk_type.decode('latin-1')} metadata chunk")
        if not skip_bytes(stream, length + 4):
            findings.append(f"{path}: truncated PNG chunk data")
            break
        if chunk_type == b"IEND":
            break
    return findings


def jpeg_findings(path: str, stream) -> list[str]:
    """Search for the Exif marker in fixed-size chunks, keeping only a small overlap."""
    overlap = b""
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            return []
        window = overlap + chunk
        if JPEG_EXIF_MARKER in window:
            return [f"{path}: JPEG Exif metadata"]
        overlap = window[len(window) - (len(JPEG_EXIF_MARKER) - 1) :]


def stream_findings(path: str, name: str, stream) -> list[str]:
    if name.endswith(".png"):
        return png_findings(path, stream)
    if name.endswith((".jpg", ".jpeg")):
        return jpeg_findings(path, stream)
    return text_findings(path, stream)


class BoundedStream:
    """Read at most `size` bytes of a stream that carries further blobs behind them."""

    def __init__(self, stream, size: int) -> None:
        self.stream = stream
        self.remaining = size

    def read(self, count: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if count < 0 or count > self.remaining:
            count = self.remaining
        data = self.stream.read(count)
        self.remaining -= len(data)
        return data


def worktree_findings(path: str) -> list[str]:
    """Inspect one working-tree file without following links."""
    candidate = Path(path)
    if candidate.is_symlink():
        return [f"{path}: symlink, not followed"]
    try:
        with candidate.open("rb") as stream:
            return stream_findings(path, candidate.name.lower(), stream)
    except OSError:
        return [f"{path}: unreadable, content not inspected"]


def index_findings(paths: Iterable[str]) -> list[str]:
    """Inspect the staged content of each path, streaming one blob at a time."""
    paths = list(paths)
    if not paths:
        return []
    findings = []
    process = subprocess.Popen(
        ["git", "cat-file", "--batch"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        for path in paths:
            process.stdin.write(f":{path}\n".encode())
            process.stdin.flush()
            header = process.stdout.readline().decode(errors="replace").split()
            if len(header) != 3 or not header[2].isdigit():
                findings.append(f"{path}: not readable from the git index")
                continue
            blob = BoundedStream(process.stdout, int(header[2]))
            findings.extend(stream_findings(path, PurePosixPath(path).name.lower(), blob))
            skip_bytes(blob, blob.remaining)
            process.stdout.read(1)
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    return findings


def content_violations(tracked: Iterable[str], untracked: Iterable[str] = ()) -> list[str]:
    findings = index_findings(tracked)
    for path in untracked:
        findings.extend(worktree_findings(path))
    return findings


def worktree_content_violations(paths: Iterable[str]) -> list[str]:
    findings = []
    for path in paths:
        findings.extend(worktree_findings(path))
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
        tracked = tracked_files()
        untracked = untracked_files() if options.untracked else []
        violations = find_violations([*tracked, *untracked])
        findings = content_violations(tracked, untracked)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError) as error:
        print(f"Hygiene check failed: git could not list files: {error}", file=sys.stderr)
        return 2
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
