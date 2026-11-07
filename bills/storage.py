"""Private originals, a durable publication journal and one process-shared lock.

All paths are relative to MEDIA_ROOT. Backups must hold originals_lock() while
using SQLite's backup API and copying originals; never copy a live WAL database.
File I/O runs outside the short database transactions below.
"""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify
from filelock import FileLock

from .models import Document, Invoice
from .validators import sha256_validator, validate_storage_path


class UploadRejected(ValidationError):
    pass


def private_path(relative):
    validate_storage_path(relative)
    if not relative:
        raise ValidationError("An original path is required.")
    root = Path(settings.MEDIA_ROOT).resolve()
    path = root.joinpath(relative)
    if not path.resolve().is_relative_to(root):
        raise ValidationError("The original path leaves private storage.")
    return path


@contextmanager
def originals_lock():
    root = Path(settings.MEDIA_ROOT).resolve()
    _mkdir(root)
    # Singleton instances allow approval's on_commit callback to reacquire this
    # thread's lock. The OS lock also serializes other workers and backup tools.
    with FileLock(str(root / ".originals.lock"), timeout=10, mode=0o600, is_singleton=True):
        yield


def coordinated_write(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with originals_lock():
            return function(*args, **kwargs)

    return wrapped


def _sync_directory(path):
    # The supported runtime is Linux, where directory fsync makes renames durable.
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mkdir(path):
    if not path.exists():
        _mkdir(path.parent)
        path.mkdir(mode=0o700, exist_ok=True)
        _sync_directory(path.parent)


def _digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _verify(path, digest):
    if not path.is_file() or _digest(path) != digest:
        raise OSError("The stored original is missing or its checksum does not match.")


def _move_original(source, target, digest):
    _mkdir(target.parent)
    if target.exists():
        _verify(target, digest)
        if source != target and source.exists():
            _verify(source, digest)
            source.unlink()
            _sync_directory(source.parent)
        return
    _verify(source, digest)
    os.replace(source, target)
    _sync_directory(target.parent)
    if source.parent != target.parent:
        _sync_directory(source.parent)


def _filename(name):
    # Folder names are metadata hints only and never assign a property.
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip()[:255]
    if not name.lower().endswith(".pdf"):
        raise UploadRejected("Choose a PDF file with a .pdf filename.")
    return name


def _stage(source, filename):
    directory = private_path(".staging")
    _mkdir(directory)
    descriptor, raw_path = tempfile.mkstemp(suffix=".pdf", dir=directory)
    staged = Path(raw_path)
    complete = False
    try:
        digest, size, prefix = hashlib.sha256(), 0, b""
        with os.fdopen(descriptor, "wb") as target:
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > settings.MAX_PDF_UPLOAD_BYTES:
                    raise UploadRejected("The PDF exceeds the upload size limit.")
                if len(prefix) < 5:
                    prefix = (prefix + chunk)[:5]
                digest.update(chunk)
                target.write(chunk)
            if prefix != b"%PDF-":
                raise UploadRejected("The file does not have a PDF header.")
            target.flush()
            os.fsync(target.fileno())
        relative = staged.relative_to(Path(settings.MEDIA_ROOT).resolve()).as_posix()
        receipt = {"sha256": digest.hexdigest(), "filename": filename, "staging_path": relative}
        # Only complete receipts are discoverable after interruption.
        pending = staged.with_suffix(".json.tmp")
        with pending.open("x", encoding="utf-8") as handle:
            os.chmod(pending, 0o600)
            json.dump(receipt, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, staged.with_suffix(".json"))
        complete = True
        _sync_directory(directory)
        return receipt
    except BaseException:
        if not complete:
            staged.unlink(missing_ok=True)
            staged.with_suffix(".json.tmp").unlink(missing_ok=True)
        raise


def _remove_receipt(receipt):
    staged = private_path(receipt["staging_path"])
    staged.unlink(missing_ok=True)
    staged.with_suffix(".json").unlink(missing_ok=True)
    _sync_directory(staged.parent)


def _storage_failed(document, message):
    Document.objects.filter(pk=document.pk).update(
        storage_state=Document.StorageState.FAILED, storage_error=message
    )
    document.refresh_from_db()
    return document


def _resume(document):
    try:
        if document.publication_path:
            target = document.publication_path
            source = document.original_path
            final_state = Document.StorageState.APPROVED
        elif document.staging_path:
            target = document.original_path or f"inbox/{document.sha256}.pdf"
            source = document.staging_path
            final_state = (
                Document.StorageState.INBOX
                if target.startswith("inbox/")
                else Document.StorageState.APPROVED
            )
        else:
            _verify(private_path(document.original_path), document.sha256)
            if document.storage_state == Document.StorageState.FAILED:
                state = (
                    Document.StorageState.INBOX
                    if document.original_path.startswith("inbox/")
                    else Document.StorageState.APPROVED
                )
                Document.objects.filter(pk=document.pk).update(
                    storage_state=state, storage_error=""
                )
                document.refresh_from_db()
            return document
        _move_original(private_path(source), private_path(target), document.sha256)
        changes = {
            "original_path": target,
            "staging_path": "",
            "publication_path": "",
            "storage_state": final_state,
            "storage_error": "",
        }
        if final_state == Document.StorageState.INBOX and not document.extraction_revision:
            changes.update(
                state=Document.State.REVIEW, processing_message="Awaiting invoice review."
            )
        staged = document.staging_path
        with transaction.atomic():
            Document.objects.filter(pk=document.pk).update(**changes)
        document.refresh_from_db()
        if staged:
            _remove_receipt({"staging_path": staged})
        return document
    except OSError, ValidationError:
        return _storage_failed(
            document, "Original storage failed. Retry after checking private storage."
        )


def _accept_receipt(receipt):
    sha256_validator(receipt["sha256"])
    filename = _filename(receipt["filename"])
    staged = private_path(receipt["staging_path"])
    if staged.parent != private_path(".staging"):
        raise ValidationError("Invalid staging receipt.")
    with transaction.atomic():
        document, created = Document.objects.get_or_create(
            sha256=receipt["sha256"],
            defaults={
                "original_filename": filename,
                "staging_path": receipt["staging_path"],
                "state": Document.State.PROCESSING,
            },
        )
        if not created and not document.staging_path and not document.publication_path:
            # An exact duplicate can restore a missing original without replacing
            # its approved values, filename or property assignments.
            if not document.original_path or not private_path(document.original_path).exists():
                document.staging_path = receipt["staging_path"]
                document.save(update_fields=["staging_path"])
    document = _resume(document)
    if document.storage_state != Document.StorageState.FAILED:
        _remove_receipt(receipt)
    return document, not created


def ingest_pdf(source, *, filename):
    """Shared stream-based intake. Hash original bytes; never create costs here.

    The caller owns the input stream. A different folder or filename cannot defeat
    deduplication. Complete staged uploads survive a database/publication failure.
    """
    filename = _filename(filename)
    # Bound streaming intake also for future folder-import callers. No database
    # transaction is held while receiving bytes or waiting for the storage lock.
    with originals_lock():
        receipt = _stage(source, filename)
        document, duplicate = _accept_receipt(receipt)
    from .processing import parse_document

    parse_document(document.pk)
    document.refresh_from_db()
    return document, duplicate


def _approved_path(document):
    invoices = list(document.invoices.select_related("vendor").order_by("invoice_date", "pk"))
    if not invoices or any(invoice.status != Invoice.Status.APPROVED for invoice in invoices):
        raise ValidationError(
            "Approve every invoice in this document before publishing its original."
        )
    properties = set()
    for invoice in invoices:
        properties.update(invoice.sections.values_list("property__slug", flat=True))
    if not properties or None in properties or len({i.vendor_id for i in invoices}) != 1:
        raise ValidationError("Publication needs reviewed properties and one vendor.")
    first = invoices[0]
    house = next(iter(properties)) if len(properties) == 1 else "shared"
    reference = slugify(first.reference)[:64] or f"invoice-{first.pk}"
    directory = f"{house}/{first.vendor.slug}/{first.invoice_date.year}"
    stem = f"{first.invoice_date}_{reference}_{document.sha256[:12]}"
    for counter in range(1000):
        suffix = f"-{counter}" if counter else ""
        relative = f"{directory}/{stem}{suffix}.pdf"
        target = private_path(relative)
        owner = (
            Document.objects.filter(Q(original_path=relative) | Q(publication_path=relative))
            .exclude(pk=document.pk)
            .exists()
        )
        if not owner and (not target.exists() or _digest(target) == document.sha256):
            return relative
    raise OSError("Unable to choose an unused original filename.")


def publish_original(document_id):
    with originals_lock():
        document = Document.objects.get(pk=document_id)
        if document.publication_path:
            return _resume(document)
        if document.staging_path:
            document = _resume(document)
        if document.storage_state == Document.StorageState.FAILED:
            return document
        try:
            target = _approved_path(document)
            if (
                target == document.original_path
                and document.storage_state == Document.StorageState.APPROVED
            ):
                _verify(private_path(target), document.sha256)
                return document
            with transaction.atomic():
                Document.objects.filter(pk=document.pk).update(
                    publication_path=target,
                    storage_state=Document.StorageState.PUBLISHING,
                    storage_error="",
                )
            document.refresh_from_db()
            return _resume(document)
        except OSError:
            return _storage_failed(
                document, "Original publication failed. Check storage and retry."
            )


def retry_document(document_id):
    with originals_lock():
        document = _resume(Document.objects.get(pk=document_id))
        if document.storage_state != Document.StorageState.FAILED:
            if (
                document.invoices.exists()
                and not document.invoices.exclude(status="approved").exists()
            ):
                document = publish_original(document_id)
    from .processing import parse_document

    parse_document(document.pk)
    document.refresh_from_db()
    return document


def recover_originals():
    """Resume durable receipts and journal rows after a process interruption."""
    with originals_lock():
        staging = private_path(".staging")
        invalid = 0
        for path in sorted(staging.glob("*.json")):
            try:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if private_path(receipt["staging_path"]).with_suffix(".json") != path:
                    raise ValidationError("Staging receipt path mismatch.")
                _accept_receipt(receipt)
            except ValueError, KeyError, TypeError, ValidationError:
                # Keep malformed receipts and bytes for operator inspection.
                invalid += 1
        if invalid:
            raise ValidationError(
                f"{invalid} invalid staging receipts remain; inspect private storage."
            )
    return [
        retry_document(pk) for pk in Document.objects.order_by("pk").values_list("pk", flat=True)
    ]


def open_original(document_id):
    """Open under the move/backup lock; the descriptor stays valid after a rename."""
    with originals_lock():
        document = Document.objects.get(pk=document_id)
        source = private_path(document.original_path)
        _verify(source, document.sha256)
        return source.open("rb"), document.original_filename
