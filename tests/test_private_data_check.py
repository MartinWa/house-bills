"""Rules of the private-data hygiene check."""

import os
import struct
import subprocess
import time
from pathlib import Path

import pytest

from tools.check_no_private_data import (
    ALLOW_MARKER,
    MAX_LINE_BYTES,
    PNG_SIGNATURE,
    READ_CHUNK_BYTES,
    SYMLINK_MODE,
    BlobReader,
    BlobReadError,
    IndexEntry,
    find_violations,
    index_entries,
    index_violations,
    inside_work_tree,
    is_violation,
    main,
    modified_paths,
    untracked_files,
    worktree_violations,
)

REPOSITORY = Path(__file__).resolve().parent.parent
PRIVATE_LINE = "contact owner@" + "vendor.example.se\n"


def git(*arguments):
    subprocess.run(["git", *arguments], check=True)


@pytest.fixture
def scratch_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for name, value in (
        ("GIT_AUTHOR_NAME", "Synthetic"),
        ("GIT_AUTHOR_EMAIL", "synthetic@example.invalid"),
        ("GIT_COMMITTER_NAME", "Synthetic"),
        ("GIT_COMMITTER_EMAIL", "synthetic@example.invalid"),
    ):
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    git("init", "--quiet")
    return tmp_path


@pytest.mark.parametrize(
    "path",
    [
        "planning/notes.md",
        "reference/vendor.pdf",
        "test-data/sample.txt",
        "data/export.csv",
        "backups/db.sqlite3",
        "state/db.sqlite3-wal",
        ".env",
        "deployment.env",
    ],
)
def test_private_names_are_violations(path):
    assert is_violation(path)


@pytest.mark.parametrize(
    "path",
    [
        "bills/models.py",
        "docs/api/example.py",
        ".env.example",
        "docs/examples/parsers/layout_01.py",
    ],
)
def test_source_names_are_allowed(path):
    assert not is_violation(path)


def test_a_force_added_pdf_original_is_refused_by_name():
    assert is_violation("documents/SYNTHETIC-INVOICE.PDF")


def test_the_documented_query_example_is_exempt_by_exact_path_only():
    assert not is_violation("docs/reporting-examples.sql")
    assert is_violation("docs/export.sql")
    assert is_violation("copy/docs/reporting-examples.sql")


def findings_for(tmp_path, text):
    path = tmp_path / "sample.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return [finding.rsplit(": ", 1)[-1] for finding in worktree_violations([str(path)])]


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("private key block", "-----BEGIN RSA PRIVATE KEY-----"),  # hygiene: allow
        ("AWS access key", "aws_access_key_id = " + "AKIA" + "IOSFODNN7EXAMPLE"),
        ("GitHub token", "token: " + "ghp_" + "0123456789abcdef" * 2 + "0123"),
        (
            "GitHub token",
            "token: " + "github_pat_" + "0123456789abcdef0123" + "0123456789abcdef",
        ),
        ("Slack token", "hook = https://hooks.slack.invalid/" + "xoxb-" + "123456789012"),
        ("assigned secret", 'PASSWORD = "0123456789abcdef0123456789abcdef"'),  # hygiene: allow
        ("email address", "invoice owner: person@vendor.example.se"),  # hygiene: allow
    ],
)
def test_content_rules_report_their_label(tmp_path, label, line):
    assert findings_for(tmp_path, line) == [label]


@pytest.mark.parametrize(
    "line",
    [
        "cp parsers/*.py /root/house-bills/parsers",  # hygiene: allow
        "HOUSE_BILLS_PARSER_PLUGINS=/srv/house-bills/parsers",  # hygiene: allow
        "mount /mnt/backup/house-bills",  # hygiene: allow
    ],
)
def test_absolute_host_paths_stay_detected(tmp_path, line):
    assert findings_for(tmp_path, line) == ["absolute host path"]


@pytest.mark.parametrize(
    "line",
    [
        'SECRET_KEY = "synthetic-test-secret-not-for-real-installations"',
        'API_KEY = "replace-with-a-secret-of-at-least-32-characters"',
        'TOKEN = "test-only-access-token-not-for-real-use-here"',
    ],
)
def test_placeholder_secrets_are_skipped(tmp_path, line):
    assert findings_for(tmp_path, line) == []


def test_allow_marker_exempts_a_line(tmp_path):
    line = f"owner@vendor.example.se {ALLOW_MARKER}"  # hygiene: allow
    assert findings_for(tmp_path, line) == []


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", 0)


def test_png_text_metadata_is_reported(tmp_path):
    image = tmp_path / "synthetic.png"
    image.write_bytes(
        PNG_SIGNATURE + png_chunk(b"tEXt", b"Comment\x00synthetic") + png_chunk(b"IEND", b"")
    )

    assert worktree_violations([str(image)]) == [f"{image}: PNG tEXt metadata chunk"]


def test_a_large_image_still_reports_metadata(tmp_path):
    image = tmp_path / "huge.png"
    image.write_bytes(
        PNG_SIGNATURE
        + png_chunk(b"IDAT", b"\x00" * MAX_LINE_BYTES)
        + png_chunk(b"tEXt", b"Comment\x00synthetic")
        + png_chunk(b"IEND", b"")
    )

    assert worktree_violations([str(image)]) == [f"{image}: PNG tEXt metadata chunk"]


@pytest.mark.parametrize("extra_bytes", [1, READ_CHUNK_BYTES + 1])
def test_a_line_past_the_cap_is_reported_and_the_rest_is_still_scanned(tmp_path, extra_bytes):
    path = tmp_path / "long.log"
    path.write_bytes(b"a" * (MAX_LINE_BYTES + extra_bytes) + b"\n" + PRIVATE_LINE.encode())

    assert worktree_violations([str(path)]) == [
        f"{path}: line 1 is too long to scan",
        f"{path}:2: email address",
    ]


def test_an_untracked_symlink_is_reported_and_not_followed(tmp_path):
    target = tmp_path / "private-notes.md"
    target.write_text(PRIVATE_LINE, encoding="utf-8")
    link = tmp_path / "notes-link.md"
    link.symlink_to(target)

    assert worktree_violations([str(link)]) == [f"{link}: symbolic link not inspected"]


def test_tracked_content_is_read_from_the_index_not_the_working_tree(scratch_repository):
    notes = Path("notes.md")
    notes.write_text(PRIVATE_LINE, encoding="utf-8")
    git("add", "notes.md")
    notes.unlink()

    assert index_violations(index_entries()) == ["notes.md:1: email address"]
    assert worktree_violations(["notes.md"]) == ["notes.md: unreadable, content not inspected"]


def test_an_unstaged_change_is_scanned_too(scratch_repository):
    notes = Path("notes.md")
    notes.write_text("synthetic notes\n", encoding="utf-8")
    git("add", "notes.md")
    notes.write_text(PRIVATE_LINE, encoding="utf-8")

    assert index_violations(index_entries()) == []
    assert modified_paths() == ["notes.md"]
    assert worktree_violations(modified_paths()) == ["notes.md:1: email address"]


def test_a_staged_symlink_is_reported_before_any_exemption(scratch_repository):
    Path("private-notes.md").write_text(PRIVATE_LINE, encoding="utf-8")
    Path("notes-link.md").symlink_to("private-notes.md")
    git("add", "notes-link.md")

    modes = {entry.path: entry.mode for entry in index_entries()}
    assert modes["notes-link.md"] == SYMLINK_MODE
    assert index_violations(index_entries()) == ["notes-link.md: symbolic link not inspected"]


def test_a_symlink_at_an_exempt_path_is_still_reported():
    entry = IndexEntry(SYMLINK_MODE, "0" * 40, "docs/reporting-examples.sql")

    assert not is_violation(entry.path)
    assert index_violations([entry]) == ["docs/reporting-examples.sql: symbolic link not inspected"]


def test_a_missing_blob_fails_the_check(scratch_repository):
    reader = BlobReader()
    try:
        with pytest.raises(BlobReadError):
            reader.blob("0" * 40)
    finally:
        reader.close()


def test_an_assigned_secret_past_a_read_window_is_still_found(tmp_path):
    secret = "0123456789abcdef" * 2
    path = tmp_path / "wide.log"
    payload = b' PASSWORD = "' + secret.encode() + b'"\n'
    path.write_bytes(b"a" * (READ_CHUNK_BYTES + 1000) + payload)

    assert worktree_violations([str(path)]) == [f"{path}:1: assigned secret"]


def test_one_stray_at_sign_on_a_long_line_is_scanned_quickly(tmp_path):
    path = tmp_path / "stray.log"
    path.write_bytes(b"a" * (MAX_LINE_BYTES - 100) + b"@" + b"b" * 40 + b"\n")

    started = time.monotonic()
    assert worktree_violations([str(path)]) == []
    assert time.monotonic() - started < 5


def test_a_tracked_file_replaced_by_a_dangling_link_is_reported(scratch_repository):
    notes = Path("notes.md")
    notes.write_text("synthetic notes\n", encoding="utf-8")
    git("add", "notes.md")
    notes.unlink()
    notes.symlink_to("missing-target")

    assert "notes.md" in modified_paths()
    assert worktree_violations(["notes.md"]) == ["notes.md: symbolic link not inspected"]


def test_a_link_at_an_exempt_path_is_reported_by_both_passes(scratch_repository):
    Path("docs").mkdir()
    Path("docs/reporting-examples.sql").symlink_to("missing-target")
    git("add", "docs/reporting-examples.sql")

    assert not is_violation("docs/reporting-examples.sql")
    assert "docs/reporting-examples.sql: symbolic link not inspected" in index_violations(
        index_entries()
    )
    assert worktree_violations(["docs/reporting-examples.sql"]) == [
        "docs/reporting-examples.sql: symbolic link not inspected"
    ]


def test_a_renamed_file_still_scans_its_working_copy(scratch_repository):
    Path("old.md").write_text("synthetic notes\n", encoding="utf-8")
    git("add", "old.md")
    git("commit", "--message", "synthetic", "--no-gpg-sign")
    git("mv", "old.md", "new.md")
    Path("new.md").write_text(PRIVATE_LINE, encoding="utf-8")

    assert modified_paths() == ["new.md"]
    assert worktree_violations(modified_paths()) == ["new.md:1: email address"]


def test_untracked_mode_reuses_the_name_and_content_checks(scratch_repository):
    Path(".gitignore").write_text("*.csv\n", encoding="utf-8")
    Path("notes.md").write_text("synthetic notes\n", encoding="utf-8")
    Path("dump.db").write_text("synthetic dump\n", encoding="utf-8")
    Path("export.csv").write_text("synthetic export\n", encoding="utf-8")

    untracked = untracked_files()
    assert "notes.md" in untracked
    assert "dump.db" in untracked
    assert "export.csv" not in untracked
    assert find_violations(untracked) == ["dump.db"]


def test_outside_a_work_tree_the_check_fails_without_listing_files(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert main([]) == 2
    captured = capsys.readouterr()
    assert "run it inside a git work tree" in captured.err
    assert "private-data" not in captured.out


@pytest.mark.skipif(not inside_work_tree(), reason="the hygiene check needs the git work tree")
def test_the_tracked_tree_passes_the_hygiene_check(monkeypatch, capsys):
    monkeypatch.chdir(REPOSITORY)

    assert main([]) == 0
    assert "Hygiene check passed" in capsys.readouterr().out
