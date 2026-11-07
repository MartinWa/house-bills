import io
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, connections, transaction

from bills import backups, storage
from bills.models import Property, Vendor
from bills.parsing import layout_02
from bills.parsing.reader import read_pdf
from bills.services import approve_review

from .synthetic import shared_supply, write_pdf
from .test_ingestion import approve, ingest

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def retained_instance(settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "live" / "invoices"
    pdf = write_pdf(tmp_path / "synthetic.pdf", shared_supply())
    document, _ = ingest(pdf)
    user = django_user_model.objects.create_user("synthetic-backup-reviewer")
    invoice = approve(document, pdf, user)
    candidate = layout_02.parse(read_pdf(pdf))
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={
            section.section_id: row.property
            for section, row in zip(
                candidate.sections, invoice.sections.order_by("pk"), strict=True
            )
        },
        actor=user,
        note="Synthetic retained correction history.",
    )
    configuration = tmp_path / "operator.env"
    configuration.write_text("HOUSE_BILLS_SECRET_KEY=synthetic-recovery-configuration\n")
    return document, configuration


def rows(database):
    with sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True) as snapshot:
        tables = snapshot.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            name: snapshot.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for (name,) in tables
        }


def rewrite_manifest(backup):
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        item["sha256"], item["bytes"] = backups.file_digest(backup / item["path"])
    manifest_path.write_text(json.dumps(manifest))


def test_backup_restore_preserves_all_rows_originals_and_configuration(retained_instance, tmp_path):
    document, configuration = retained_instance
    expected = rows(connection.settings_dict["NAME"])
    destination = tmp_path / "backup"
    call_command("backup_bills", str(destination), "--config", str(configuration))
    call_command("verify_backup", str(destination))
    assert rows(destination / "database.sqlite3") == expected
    with backups.open_snapshot(destination / "database.sqlite3") as snapshot:
        assert snapshot.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    restored = tmp_path / "restored"
    call_command("restore_bills", str(destination), str(restored))
    assert rows(restored / "db.sqlite3") == expected
    document.refresh_from_db()
    assert (restored / "invoices" / document.original_path).read_bytes() == storage.private_path(
        document.original_path
    ).read_bytes()
    assert (restored / "recovered-configuration/001-operator.env").read_bytes() == (
        configuration.read_bytes()
    )
    assert not (destination / "invoices/.originals.lock").exists()
    assert (restored / "db.sqlite3").stat().st_mode & 0o777 == 0o600
    assert restored.stat().st_mode & 0o777 == 0o700
    result = subprocess.run(
        [sys.executable, "manage.py", "migrate", "--check"],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(restored),
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert rows(restored / "db.sqlite3") == expected


def test_unregistered_upload_receipt_restores_and_recovers(retained_instance, tmp_path):
    document, configuration = retained_instance
    pdf = write_pdf(
        tmp_path / "pending.pdf",
        [[line.replace("880003", "880099") for line in page] for page in shared_supply()],
    )
    with storage.originals_lock(), pdf.open("rb") as source:
        receipt = storage._stage(source, "pending.pdf")
    # Incomplete/invalid intake is retained for inspection as well as the valid receipt.
    storage.private_path(".staging/partial.pdf").write_bytes(b"%PDF-incomplete")
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])
    restored = tmp_path / "restored"
    backups.restore_backup(backup, restored)
    assert (restored / "invoices" / receipt["staging_path"]).read_bytes() == pdf.read_bytes()
    assert (restored / "invoices/.staging/partial.pdf").read_bytes() == b"%PDF-incomplete"
    result = subprocess.run(
        [sys.executable, "manage.py", "recover_documents"],
        env={
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "house_bills.settings.development",
            "HOUSE_BILLS_DATA_DIR": str(restored),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    with backups.open_snapshot(restored / "db.sqlite3") as snapshot:
        assert snapshot.execute("SELECT COUNT(*) FROM bills_document").fetchone() == (2,)
        assert snapshot.execute(
            "SELECT COUNT(*) FROM bills_invoice WHERE status='approved'"
        ).fetchone() == (1,)
        assert snapshot.execute("SELECT review_revision FROM bills_invoice").fetchone() == (2,)
    document.refresh_from_db()
    assert (restored / "invoices" / document.original_path).is_file()


@pytest.mark.parametrize("kind", ["original", "database", "configuration", "extra", "lock"])
def test_corrupt_or_unlisted_backup_is_rejected(retained_instance, tmp_path, kind):
    _, configuration = retained_instance
    backup = tmp_path / "backup"
    manifest = backups.create_backup(backup, [configuration])
    if kind in {"extra", "lock"}:
        (backup / ("unlisted.txt" if kind == "extra" else ".originals.lock")).write_text("extra")
    else:
        item = next(item for item in manifest["files"] if item["kind"] == kind)
        with (backup / item["path"]).open("ab") as stream:
            stream.write(b"changed")
    with pytest.raises(CommandError, match="checksum|unlisted"):
        call_command("verify_backup", str(backup))
    with pytest.raises(backups.BackupError):
        backups.restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_manifest_cannot_bless_changed_document_bytes(retained_instance, tmp_path):
    document, configuration = retained_instance
    document.refresh_from_db()
    backup = tmp_path / "backup"
    manifest = backups.create_backup(backup, [configuration])
    item = next(
        item for item in manifest["files"] if item["path"] == "invoices/" + document.original_path
    )
    (backup / item["path"]).write_bytes(b"%PDF-wrong-original")
    rewrite_manifest(backup)
    with pytest.raises(backups.BackupError, match="stored document checksum"):
        backups.verify_backup(backup)


@pytest.mark.parametrize("failure", ["path", "foreign_key", "missing_original"])
def test_changed_snapshot_is_rejected_after_manifest_rehash(retained_instance, tmp_path, failure):
    _, configuration = retained_instance
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])
    with sqlite3.connect(backup / "database.sqlite3") as snapshot:
        if failure == "path":
            snapshot.execute("UPDATE bills_document SET original_path='../outside.pdf'")
        elif failure == "foreign_key":
            snapshot.execute("DELETE FROM bills_invoice")
        else:
            snapshot.execute("UPDATE bills_document SET original_path='missing.pdf'")
    rewrite_manifest(backup)
    with pytest.raises(backups.BackupError, match="path|foreign-key|recoverable"):
        backups.verify_backup(backup)


@pytest.mark.parametrize(
    "relative", ["../outside", "/absolute", "a/../b", "a//b", "./a", "a\\b", "c:x"]
)
def test_archive_paths_reject_ambiguous_or_traversing_names(tmp_path, relative):
    with pytest.raises(backups.BackupError, match="path"):
        backups.safe_path(tmp_path, relative)


def test_symlinks_and_existing_destinations_are_rejected(retained_instance, tmp_path):
    _, configuration = retained_instance
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(backups.BackupError, match="already exists"):
        backups.restore_backup(backup, existing)
    with pytest.raises(backups.BackupError, match="already exists"):
        backups.create_backup(backup, [configuration])
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "absent")
    with pytest.raises(backups.BackupError, match="already exists"):
        backups.restore_backup(backup, dangling)
    (backup / "linked-directory").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(backups.BackupError, match="symlinks|escaped"):
        backups.verify_backup(backup)
    assert list(existing.iterdir()) == []


def test_live_storage_and_backup_boundaries_are_rejected(retained_instance, tmp_path, settings):
    _, configuration = retained_instance
    with pytest.raises(backups.BackupError, match="outside"):
        backups.create_backup(Path(settings.MEDIA_ROOT) / "backup", [configuration])
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])
    with pytest.raises(backups.BackupError, match="outside"):
        backups.restore_backup(backup, backup / "restore")
    with transaction.atomic(), pytest.raises(backups.BackupError, match="transactions"):
        backups.create_backup(tmp_path / "transaction-backup", [configuration])


def test_missing_or_corrupt_live_original_publishes_no_backup(retained_instance, tmp_path):
    document, configuration = retained_instance
    document.refresh_from_db()
    storage.private_path(document.original_path).write_bytes(b"%PDF-corrupt")
    with pytest.raises(backups.BackupError, match="checksum"):
        backups.create_backup(tmp_path / "backup", [configuration])
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".backup-pending-*"))


def test_failed_copy_and_restore_leave_no_complete_destination(
    retained_instance, tmp_path, monkeypatch
):
    _, configuration = retained_instance
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])

    def fail_copy(*args):
        raise OSError("Synthetic storage interruption")

    monkeypatch.setattr(backups, "copy_private", fail_copy)
    with pytest.raises(backups.BackupError, match="Backup failed"):
        backups.create_backup(tmp_path / "failed-backup", [configuration])
    with pytest.raises(backups.BackupError, match="Restore failed"):
        backups.restore_backup(backup, tmp_path / "failed-restore")
    assert not (tmp_path / "failed-backup").exists()
    assert not (tmp_path / "failed-restore").exists()
    assert not list(tmp_path.glob(".*-pending-*"))
    backups.verify_backup(backup)


def test_operational_backup_holds_lock_until_original_copy_finishes(
    retained_instance, tmp_path, monkeypatch, django_user_model
):
    _, configuration = retained_instance
    started = Event()
    finished = Event()
    copy = backups.copy_private

    def writer():
        connections.close_all()
        try:
            started.set()
            with storage.originals_lock(), transaction.atomic():
                Vendor.objects.update(name="After snapshot")
            finished.set()
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = None

        def overlapping_copy(source, destination):
            nonlocal future
            if future is None:
                future = pool.submit(writer)
                assert started.wait(timeout=5)
                assert not finished.wait(timeout=0.1)
                # Account writes may continue while originals/billing writes are paused.
                django_user_model.objects.create_user("synthetic-during-backup")
            return copy(source, destination)

        monkeypatch.setattr(backups, "copy_private", overlapping_copy)
        backups.create_backup(tmp_path / "backup", [configuration])
        future.result(timeout=10)
    assert Vendor.objects.get().name == "After snapshot"
    with backups.open_snapshot(tmp_path / "backup/database.sqlite3") as snapshot:
        assert snapshot.execute("SELECT name FROM bills_vendor").fetchone() == ("Synthetic Vendor",)
        assert snapshot.execute(
            "SELECT COUNT(*) FROM auth_user WHERE username='synthetic-during-backup'"
        ).fetchone() == (0,)
    assert Property.objects.count() == 2


def test_valid_receipt_checksum_is_checked_before_publication(retained_instance, tmp_path):
    _, configuration = retained_instance
    with storage.originals_lock():
        receipt = storage._stage(io.BytesIO(b"%PDF-1.7 synthetic pending"), "pending.pdf")
    storage.private_path(receipt["staging_path"]).write_bytes(b"%PDF-replaced")
    with pytest.raises(backups.BackupError, match="receipt checksum"):
        backups.create_backup(tmp_path / "backup", [configuration])
    assert not (tmp_path / "backup").exists()
