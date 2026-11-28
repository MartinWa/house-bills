"""Fail when private data is tracked by git, by name or by content.

A line that legitimately needs to look like private data (for example a security
contact address) can opt out of the content scan by carrying the marker
``hygiene: allow``.

The check enumerates the git index, reports symbolic links without opening them, reads
each blob by object id in bounded pieces, and also scans a tracked file's working-tree
copy when it differs from the index, so an unstaged secret is caught too. ``--untracked``
adds the working-tree files that git reports as untracked and not ignored.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
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
# An exact path, matched by membership: a documented query example rather than a dump.
ALLOWED_PATHS = frozenset({"docs/reporting-examples.sql"})
MAX_LINE_BYTES = 2 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
ALLOW_MARKER = "hygiene: allow"
SYMLINK_MODE = "120000"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_METADATA_CHUNKS = frozenset({b"tEXt", b"iTXt", b"zTXt", b"eXIf"})
JPEG_EXIF_MARKER = b"Exif\x00\x00"
ASSIGNED_SECRET = re.compile(
    r"(?i)\b(secret[_-]?key|api[_-]?key|token|password)\b"
    r"\s*[:=]\s*[\"']([A-Za-z0-9_\-/+]{32,})[\"']"
)
# A deliberately fake value names itself as fake instead of being exempted by file.
PLACEHOLDER_SECRET_MARKERS = ("synthetic", "test-only", "replace-with", "not-for-real")

# Each rule is tried only when one of its literal markers occurs in the line, which keeps
# long single-line files cheap to scan.
CONTENT_PATTERNS = (
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), ("PRIVATE KEY",)),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}"), ("AKIA",)),
    (
        "GitHub token",
        re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{20,}"),
        ("gh", "github_pat_"),
    ),
    ("Slack token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), ("xox",)),
    ("assigned secret", ASSIGNED_SECRET, (":", "=")),
    (
        "absolute host path",
        re.compile(
            r"(?:[A-Za-z]:\\Users\\|/Users/[A-Za-z]|/home/(?!developer\b)[A-Za-z]|/(?:root|srv|mnt)/)"
        ),
        ("/", "\\"),
    ),
)
UNTRACKED_HELP = "also scan files that are untracked but not ignored by .gitignore"
SYMLINK_FINDING = "symbolic link not inspected"
EMAIL_LOCAL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
EMAIL_DOMAIN_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-")
EMAIL_DOMAIN_PATTERN = re.compile(r"[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}")
# Exact names, not a prefix test: "example.company" is a real domain, not reserved.
EMAIL_EXEMPT_DOMAINS = frozenset({"example.com", "example.org", "example.net", "example.invalid"})
# Sentence punctuation that can trail an address in prose and is not part of it.
EMAIL_TRAILING_CHARS = ".,;:!?)]}>\"'"
EMAIL_LOCAL_LIMIT = 64
EMAIL_DOMAIN_LIMIT = 255


class BlobReadError(RuntimeError):
    """Raised when git cannot supply the staged content that the index declares."""


@dataclass(frozen=True)
class IndexEntry:
    mode: str
    object_id: str
    path: str


def is_placeholder_secret(match: re.Match[str]) -> bool:
    """Return whether an assigned value names itself as a synthetic placeholder."""
    value = match.group(2).lower()
    return any(marker in value for marker in PLACEHOLDER_SECRET_MARKERS)


def address_at(line: str, position: int) -> bool:
    """Return whether the '@' at `position` sits inside a plausible email address.

    The local and domain parts are bounded by their RFC lengths, so the scan of a long
    line stays linear instead of restarting an unbounded pattern at every character.
    Trailing sentence punctuation is trimmed first, so an address that ends a sentence or
    sits just before a closing bracket or quote is still recognised.
    """
    start = position
    while start > 0 and position - start < EMAIL_LOCAL_LIMIT:
        if line[start - 1] not in EMAIL_LOCAL_CHARS:
            break
        start -= 1
    end = position + 1
    while end < len(line) and end - position <= EMAIL_DOMAIN_LIMIT:
        if line[end] not in EMAIL_DOMAIN_CHARS:
            break
        end += 1
    if start == position:
        return False
    domain = line[position + 1 : end].rstrip(EMAIL_TRAILING_CHARS)
    if not EMAIL_DOMAIN_PATTERN.fullmatch(domain):
        return False
    return domain not in EMAIL_EXEMPT_DOMAINS


def has_email(line: str) -> bool:
    position = line.find("@")
    while position >= 0:
        if address_at(line, position):
            return True
        position = line.find("@", position + 1)
    return False


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


def rule_matches(label: str, pattern: re.Pattern[str], line: str) -> bool:
    """Return whether any match on the line counts as a finding.

    A conditional rule inspects every match, so a placeholder value early on the line
    cannot hide a real value assigned later on the same line.
    """
    placeholder = PLACEHOLDER_CHECKS.get(label)
    if placeholder is None:
        return pattern.search(line) is not None
    return any(not placeholder(match) for match in pattern.finditer(line))


def line_findings(path: str, line_number: int, raw: bytes) -> list[str]:
    if len(raw) > MAX_LINE_BYTES:
        return [f"{path}: line {line_number} is too long to scan"]
    line = raw.decode("utf-8", errors="ignore")
    if ALLOW_MARKER in line:
        return []
    findings = []
    for label, pattern, markers in CONTENT_PATTERNS:
        if not any(marker in line for marker in markers):
            continue
        if rule_matches(label, pattern, line):
            findings.append(f"{path}:{line_number}: {label}")
    if has_email(line):
        findings.append(f"{path}:{line_number}: email address")
    return findings


def text_findings(path: str, stream) -> list[str]:
    """Scan every line of the stream without buffering more than one line at a time."""
    findings = []
    line_number = 0
    buffer = b""
    too_long = False
    while True:
        chunk = stream.read(READ_CHUNK_BYTES)
        if not chunk:
            break
        buffer += chunk
        if too_long:
            end = buffer.find(b"\n")
            if end < 0:
                buffer = b""
                continue
            buffer = buffer[end + 1 :]
            too_long = False
        parts = buffer.split(b"\n")
        buffer = parts.pop()
        for raw in parts:
            line_number += 1
            findings.extend(line_findings(path, line_number, raw))
        if len(buffer) > MAX_LINE_BYTES:
            line_number += 1
            findings.append(f"{path}: line {line_number} is too long to scan")
            buffer = b""
            too_long = True
    if buffer and not too_long:
        line_number += 1
        findings.extend(line_findings(path, line_number, buffer))
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


class BlobReader:
    """Read one staged blob at a time from a single ``git cat-file --batch`` process.

    Each request is written and its whole response consumed before the next request, so
    git never blocks on a full output pipe.
    """

    def __init__(self) -> None:
        self.process = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def close(self) -> None:
        self.process.stdin.close()
        self.process.stdout.close()
        if self.process.wait() != 0:
            raise BlobReadError("git cat-file exited unsuccessfully")

    def blob(self, object_id: str) -> BoundedStream:
        self.process.stdin.write(f"{object_id}\n".encode())
        self.process.stdin.flush()
        header = self.process.stdout.readline().decode(errors="replace").split()
        if len(header) != 3 or not header[2].isdigit():
            raise BlobReadError(f"{object_id} is missing from the git object store")
        return BoundedStream(self.process.stdout, int(header[2]))

    def drain(self, object_id: str, blob: BoundedStream) -> None:
        if not skip_bytes(blob, blob.remaining):
            raise BlobReadError(f"{object_id} was read short")
        if self.process.stdout.read(1) != b"\n":
            raise BlobReadError(f"{object_id} was not terminated by git")


def run_git(*arguments: str) -> bytes:
    result = subprocess.run(["git", *arguments], capture_output=True, check=True, text=False)
    return result.stdout


def index_entries() -> list[IndexEntry]:
    """Return the mode, object id and path of every entry in the git index."""
    entries = []
    for record in run_git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, object_id, _stage = meta.decode().split()
        entries.append(IndexEntry(mode, object_id, path.decode()))
    return entries


def index_violations(entries: Iterable[IndexEntry]) -> list[str]:
    """Inspect staged content, reporting symbolic links before any exemption applies."""
    entries = list(entries)
    findings = [
        f"{entry.path}: {SYMLINK_FINDING}" for entry in entries if entry.mode == SYMLINK_MODE
    ]
    regular = [entry for entry in entries if entry.mode != SYMLINK_MODE]
    if not regular:
        return findings
    reader = BlobReader()
    try:
        for entry in regular:
            blob = reader.blob(entry.object_id)
            findings.extend(
                stream_findings(entry.path, PurePosixPath(entry.path).name.lower(), blob)
            )
            reader.drain(entry.object_id, blob)
    finally:
        reader.close()
    return findings


def worktree_findings(path: str) -> list[str]:
    """Inspect one working-tree file without following links."""
    candidate = Path(path)
    if candidate.is_symlink():
        return [f"{path}: {SYMLINK_FINDING}"]
    try:
        with candidate.open("rb") as stream:
            return stream_findings(path, candidate.name.lower(), stream)
    except OSError:
        return [f"{path}: unreadable, content not inspected"]


def worktree_violations(paths: Iterable[str]) -> list[str]:
    findings = []
    for path in paths:
        findings.extend(worktree_findings(path))
    return findings


def modified_paths() -> list[str]:
    """Return tracked paths whose working-tree copy differs from the index.

    Rename and copy records carry the destination first and the original path in the
    following NUL field, which is consumed and discarded. A file that is now a symlink
    counts as changed even when the link is dangling, so it is reported, not skipped.
    """
    paths = []
    records = run_git("status", "--porcelain", "-z", "--untracked-files=no").split(b"\0")
    skip_next = False
    for record in records:
        if not record:
            continue
        if skip_next:
            skip_next = False
            continue
        status = record[:2].decode(errors="replace")
        path = record[3:].decode()
        if status[0] in "RC":
            skip_next = True
        if status[1] in " D":
            continue
        candidate = Path(path)
        if candidate.is_symlink() or candidate.exists():
            paths.append(path)
    return paths


def untracked_files() -> list[str]:
    output = run_git("ls-files", "--others", "--exclude-standard", "-z")
    return [entry.decode() for entry in output.split(b"\0") if entry]


def inside_work_tree() -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == b"true"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--untracked", action="store_true", help=UNTRACKED_HELP)
    options = parser.parse_args(argv)
    try:
        if not inside_work_tree():
            print("Hygiene check failed: run it inside a git work tree.", file=sys.stderr)
            return 2
        entries = index_entries()
        changed = modified_paths()
        untracked = untracked_files() if options.untracked else []
        violations = find_violations([entry.path for entry in entries] + untracked)
        findings = index_violations(entries)
        findings += worktree_violations([*changed, *untracked])
    except (BlobReadError, subprocess.CalledProcessError, FileNotFoundError, OSError) as error:
        print(f"Hygiene check failed: {error}", file=sys.stderr)
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
