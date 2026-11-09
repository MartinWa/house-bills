"""Coordinated, verified operational backups of SQLite and private originals."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path, PurePosixPath

import django
from django.conf import settings
from django.db import connection
from django.utils import timezone
from filelock import FileLock

from .storage import _mkdir, _sync_directory, originals_lock

FORMAT = "house-bills-operational-backup"
VERSION = 1
CHECKSUM = re.compile(r"[a-f0-9]{64}")


class BackupError(Exception):
    pass


def safe_path(root, relative):
    """Reject ambiguous paths and symlinks before opening archive content."""
    if not isinstance(relative, str) or not relative:
        raise BackupError("An archive path is invalid.")
    value = PurePosixPath(relative)
    if (
        value.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.split("/"))
        or "\\" in relative
        or ":" in relative
    ):
        raise BackupError("An archive path is invalid.")
    root = Path(root).resolve()
    path = root.joinpath(*value.parts)
    if not path.resolve().is_relative_to(root):
        raise BackupError("An archive path escaped its directory.")
    for component in (path, *path.parents):
        if component == root:
            break
        if component.is_symlink():
            raise BackupError("Archive symlinks are not supported.")
    return path


def file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def copy_private(source, destination):
    _mkdir(destination.parent)
    with Path(source).open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    destination.chmod(0o600)


def archive_files(root, *, skip_lock=False):
    """Include pending receipts, partial uploads and files needing operator inspection."""
    if not root.exists():
        return []
    result = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        safe_path(root, relative)
        if path.is_dir():
            continue
        if not path.is_file():
            raise BackupError("Only regular files and directories can be backed up.")
        if not skip_lock or not (
            relative == ".originals.lock" or re.fullmatch(r"\.parse-[a-f0-9]{64}\.lock", relative)
        ):
            result.append(relative)
    return result


def open_snapshot(database):
    # Immutable is valid only for the completed backup snapshot, never the live WAL database.
    return sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


def check_database(snapshot):
    if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise BackupError("SQLite integrity check failed.")
    if snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise BackupError("The snapshot contains broken foreign-key relationships.")


def check_originals(snapshot, root, originals):
    try:
        columns = {row[1] for row in snapshot.execute("PRAGMA table_info(bills_document)")}
        source = "source" if "source" in columns else "'upload'"
        documents = snapshot.execute(
            f"SELECT sha256, original_path, staging_path, publication_path, {source} "
            "FROM bills_document ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise BackupError("The snapshot does not contain the supported document schema.") from exc
    for checksum, *references, kind in documents:
        if kind == "manual":
            # Manual entries have no original bytes; their approved snapshot is
            # the recoverable record and must not point at private storage.
            if checksum or any(references):
                raise BackupError("A manual document unexpectedly references an original.")
            continue
        if not isinstance(checksum, str) or not CHECKSUM.fullmatch(checksum):
            raise BackupError("A document has an invalid original checksum.")
        found = False
        for relative in filter(None, references):
            safe_path(root, relative)
            if relative in originals:
                if originals[relative] != checksum:
                    raise BackupError("An original failed its stored document checksum.")
                found = True
        if not found:
            raise BackupError("A document has no recoverable original in the backup.")
    # Complete, not-yet-registered intake must also retain checksum-correct bytes.
    for relative in originals:
        if not relative.startswith(".staging/") or not relative.endswith(".json"):
            continue
        try:
            receipt = json.loads(safe_path(root, relative).read_text(encoding="utf-8"))
            staged = receipt["staging_path"]
            checksum = receipt["sha256"]
            if (
                not isinstance(staged, str)
                or not staged.startswith(".staging/")
                or not staged.endswith(".pdf")
                or staged[:-4] + ".json" != relative
                or not isinstance(checksum, str)
                or not CHECKSUM.fullmatch(checksum)
            ):
                continue
            safe_path(root, staged)
        except ValueError, KeyError, TypeError, BackupError:
            # Malformed receipts and incomplete uploads remain available for inspection.
            continue
        if staged in originals:
            if originals[staged] != checksum:
                raise BackupError("A completed staged upload failed its receipt checksum.")
        elif checksum not in originals.values():
            raise BackupError("A completed upload receipt has no recoverable original.")
    return len(documents)


def add_file(files, temporary, relative, kind):
    checksum, size = file_digest(safe_path(temporary, relative))
    files.append({"path": relative, "kind": kind, "sha256": checksum, "bytes": size})


def sync_tree(root):
    for path in sorted((path for path in root.rglob("*") if path.is_dir()), reverse=True):
        _sync_directory(path)
    _sync_directory(root)


def new_destination(destination, *, backup=None):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise BackupError("The destination already exists; nothing was overwritten.")
    destination = destination.resolve()
    live_roots = (
        Path(settings.MEDIA_ROOT).resolve(),
        Path(settings.DATABASES["default"]["NAME"]).resolve().parent,
    )
    if any(destination.is_relative_to(root) for root in live_roots):
        raise BackupError("Use a destination outside the running instance's data storage.")
    if backup is not None and destination.is_relative_to(backup):
        raise BackupError("Restore outside the completed backup directory.")
    _mkdir(destination.parent)
    return destination


def publish_directory(temporary, destination):
    if destination.exists() or destination.is_symlink():
        raise BackupError("The destination appeared during the operation; nothing was overwritten.")
    os.rename(temporary, destination)
    try:
        _sync_directory(destination.parent)
    except OSError as exc:
        raise BackupError(
            "The completed directory was published but its final storage flush failed. "
            "Retain and verify it before use."
        ) from exc


def create_backup(destination, configuration_files):
    """Hold the originals lock from the SQLite backup-API snapshot through file copying."""
    if connection.in_atomic_block:
        raise BackupError("Create a backup outside database transactions.")
    database = Path(settings.DATABASES["default"]["NAME"]).resolve()
    if not database.is_file():
        raise BackupError("Initialize the instance before creating an operational backup.")
    if not configuration_files:
        raise BackupError("Supply the private configuration file(s) needed for recovery.")
    configuration_files = [Path(path).resolve() for path in configuration_files]
    if any(not path.is_file() for path in configuration_files):
        raise BackupError("A recovery configuration file does not exist.")
    destination = new_destination(destination)
    temporary = Path(tempfile.mkdtemp(prefix=".backup-pending-", dir=destination.parent))
    try:
        files = []
        # Coordinate with the server's startup migration lock as well as billing/storage writes.
        with FileLock(str(database.parent / ".migrate.lock"), timeout=10, mode=0o600):
            with originals_lock():
                connection.ensure_connection()
                if connection.connection.in_transaction:
                    raise BackupError("Create a backup outside database transactions.")
                snapshot_path = temporary / "database.sqlite3"
                target = sqlite3.connect(snapshot_path)
                try:
                    connection.connection.backup(target, pages=1000, sleep=0.01)
                    target.execute("PRAGMA journal_mode=DELETE")
                    check_database(target)
                finally:
                    target.close()
                snapshot_path.chmod(0o600)
                with snapshot_path.open("r+b") as stream:
                    os.fsync(stream.fileno())
                add_file(files, temporary, "database.sqlite3", "database")
                root = Path(settings.MEDIA_ROOT).resolve()
                for relative in archive_files(root, skip_lock=True):
                    archive_path = "invoices/" + relative
                    copy_private(safe_path(root, relative), safe_path(temporary, archive_path))
                    add_file(files, temporary, archive_path, "original")
                for index, source in enumerate(configuration_files, start=1):
                    relative = f"configuration/{index:03d}-{source.name}"
                    copy_private(source, safe_path(temporary, relative))
                    add_file(files, temporary, relative, "configuration")
        manifest = {
            "format": FORMAT,
            "version": VERSION,
            "created_at": timezone.now().isoformat(),
            "application_version": os.environ.get("HOUSE_BILLS_VERSION", "development"),
            "django_version": django.get_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "files": files,
        }
        with (temporary / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        (temporary / "manifest.json").chmod(0o600)
        verify_backup(temporary)
        sync_tree(temporary)
        publish_directory(temporary, destination)
        return manifest
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BackupError("Backup failed; no complete backup was published.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def verify_backup(backup):
    """Verify every archived byte, SQLite integrity/FKs and recoverable document paths."""
    backup = Path(backup).resolve()
    try:
        manifest_path = safe_path(backup, "manifest.json")
        if manifest_path.stat().st_size > 8 * 1024 * 1024:
            raise BackupError("The backup manifest is too large.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("format") != FORMAT
            or type(manifest.get("version")) is not int
            or manifest["version"] != VERSION
            or not isinstance(manifest.get("files"), list)
        ):
            raise BackupError("This is not a supported House Bills operational backup.")
        paths = set()
        originals = {}
        configurations = 0
        for item in manifest["files"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("path"), str)
                or not isinstance(item.get("sha256"), str)
                or not CHECKSUM.fullmatch(item["sha256"])
                or type(item.get("bytes")) is not int
                or item["bytes"] < 0
            ):
                raise BackupError("The manifest contains invalid file metadata.")
            relative = item["path"]
            source = safe_path(backup, relative)
            if relative in paths:
                raise BackupError("The manifest has duplicate file paths.")
            paths.add(relative)
            if not source.is_file() or file_digest(source) != (item["sha256"], item["bytes"]):
                raise BackupError("A backup file failed its checksum or size check.")
            if item.get("kind") == "database" and relative == "database.sqlite3":
                pass
            elif item.get("kind") == "original" and relative.startswith("invoices/"):
                originals[relative.removeprefix("invoices/")] = item["sha256"]
            elif item.get("kind") == "configuration" and relative.startswith("configuration/"):
                configurations += 1
            else:
                raise BackupError("The manifest contains an unexpected file kind/path.")
        if "database.sqlite3" not in paths or not configurations:
            raise BackupError("The backup lacks its database or recovery configuration.")
        if set(archive_files(backup)) != paths | {"manifest.json"}:
            raise BackupError("The backup includes unlisted or missing files.")
        snapshot = open_snapshot(backup / "database.sqlite3")
        try:
            check_database(snapshot)
            check_originals(snapshot, backup / "invoices", originals)
        finally:
            snapshot.close()
        return manifest
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise BackupError("The operational backup could not be verified.") from exc


def restore_backup(backup, destination):
    """Restore into a new directory; recover configuration for operator review."""
    backup = Path(backup).resolve()
    destination = new_destination(destination, backup=backup)
    manifest = verify_backup(backup)
    temporary = Path(tempfile.mkdtemp(prefix=".restore-pending-", dir=destination.parent))
    try:
        for item in manifest["files"]:
            relative = item["path"]
            if item["kind"] == "database":
                relative = "db.sqlite3"
            elif item["kind"] == "configuration":
                relative = "recovered-configuration/" + relative.removeprefix("configuration/")
            target = safe_path(temporary, relative)
            copy_private(safe_path(backup, item["path"]), target)
            if file_digest(target) != (item["sha256"], item["bytes"]):
                raise BackupError("A restored file failed its checksum or size check.")
        _mkdir(temporary / "invoices")
        snapshot = open_snapshot(temporary / "db.sqlite3")
        try:
            check_database(snapshot)
            originals = {
                item["path"].removeprefix("invoices/"): item["sha256"]
                for item in manifest["files"]
                if item["kind"] == "original"
            }
            check_originals(snapshot, temporary / "invoices", originals)
        finally:
            snapshot.close()
        sync_tree(temporary)
        publish_directory(temporary, destination)
        return manifest
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise BackupError("Restore failed; no complete instance was published.") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
