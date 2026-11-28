"""Rules of the private-data hygiene check."""

import os
import struct
import subprocess
from pathlib import Path

import pytest

from tools.check_no_private_data import (
    ALLOW_MARKER,
    MAX_SCAN_BYTES,
    PNG_SIGNATURE,
    candidate_files,
    content_violations,
    find_violations,
    inside_work_tree,
    is_violation,
    main,
)

REPOSITORY = Path(__file__).resolve().parent.parent


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


def test_the_documented_query_example_stays_committable_but_dumps_do_not():
    assert not is_violation("docs/reporting-examples.sql")
    assert is_violation("docs/export.sql")


def findings_for(tmp_path, text):
    path = tmp_path / "sample.txt"
    path.write_text(text + "\n", encoding="utf-8")
    return [finding.rsplit(": ", 1)[-1] for finding in content_violations([str(path)])]


@pytest.mark.parametrize(
    ("label", "line"),
    [
        ("private key block", "-----BEGIN RSA PRIVATE KEY-----"),  # hygiene: allow
        ("AWS access key", "aws_access_key_id = " + "AKIA" + "IOSFODNN7EXAMPLE"),
        ("GitHub token", "token: " + "ghp_" + "0123456789abcdef" * 2 + "0123"),
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

    assert content_violations([str(image)]) == [f"{image}: PNG tEXt metadata chunk"]


def test_an_oversized_image_is_skipped_before_it_is_read(tmp_path):
    image = tmp_path / "huge.png"
    image.write_bytes(
        PNG_SIGNATURE + png_chunk(b"tEXt", b"Comment\x00synthetic") + b"\x00" * MAX_SCAN_BYTES
    )

    assert content_violations([str(image)]) == []


def test_untracked_mode_reuses_the_name_and_content_checks(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "--quiet"], check=True)
    Path(".gitignore").write_text("*.csv\n", encoding="utf-8")
    Path("notes.md").write_text("synthetic notes\n", encoding="utf-8")
    Path("dump.db").write_text("synthetic dump\n", encoding="utf-8")
    Path("export.csv").write_text("synthetic export\n", encoding="utf-8")

    assert candidate_files() == []
    untracked = candidate_files(untracked=True)
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
