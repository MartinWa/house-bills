"""Explicit whole-document deletion with recoverable cleanup of private originals."""

import json
import os
from contextlib import nullcontext

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.db.models import Q
from filelock import FileLock

from .models import Document, ExtractionAttempt, Invoice, ReviewRevision
from .storage import _mkdir, _sync_directory, originals_lock, private_path
from .validators import sha256_validator

DELETION_SALT = "bills.delete-document"


class StaleDeletionError(ValidationError):
    pass


def _version(document):
    return {
        "document_id": document.pk,
        "extraction_revision": document.extraction_revision,
        "invoices": [
            list(row)
            for row in document.invoices.order_by("pk").values_list("pk", "review_revision")
        ],
    }


def deletion_token(document):
    return signing.dumps(_version(document), salt=DELETION_SALT)


def _original_paths(document):
    paths = {
        path
        for path in (document.original_path, document.staging_path, document.publication_path)
        if path
    }
    if document.sha256:
        # Failed duplicate uploads can leave additional complete receipts for this PDF.
        for receipt_path in private_path(".staging").glob("*.json"):
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(receipt, dict) or receipt.get("sha256") != document.sha256:
                    continue
                staged = private_path(receipt["staging_path"])
                if (
                    staged.parent == receipt_path.parent
                    and staged.with_suffix(".json") == receipt_path
                ):
                    paths.add(receipt["staging_path"])
            except ValueError, KeyError, TypeError, ValidationError:
                continue
    for path in paths:
        if private_path(path).suffix != ".pdf":
            raise ValidationError("An original has an invalid PDF path.")
    return sorted(paths)


def _write_receipt(document, paths):
    directory = private_path(".deletions")
    _mkdir(directory)
    receipt_path = directory / f"{document.pk}.json"
    pending = receipt_path.with_suffix(".json.tmp")
    pending.unlink(missing_ok=True)
    with pending.open("x", encoding="utf-8") as stream:
        os.chmod(pending, 0o600)
        json.dump({"document_id": document.pk, "sha256": document.sha256, "paths": paths}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, receipt_path)
    _sync_directory(directory)
    return receipt_path


def _finish_deletion(receipt_path):
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    document_id = receipt["document_id"]
    if type(document_id) is not int or receipt_path.name != f"{document_id}.json":
        raise ValidationError("Invalid document deletion receipt.")
    if Document.objects.filter(pk=document_id).exists():
        # The process stopped before its database transaction committed.
        receipt_path.unlink()
        _sync_directory(receipt_path.parent)
        return
    checksum, paths = receipt["sha256"], receipt["paths"]
    if not isinstance(checksum, str) or not isinstance(paths, list) or (not checksum and paths):
        raise ValidationError("Invalid document deletion paths.")
    if checksum:
        sha256_validator(checksum)
    for relative in paths:
        path = private_path(relative)
        if path.suffix != ".pdf":
            raise ValidationError("Invalid document deletion path.")
        if Document.objects.filter(
            Q(original_path=relative) | Q(staging_path=relative) | Q(publication_path=relative)
        ).exists():
            # A later upload may have adopted a retained original. Keep its bytes.
            continue
        if path.parent == private_path(".staging"):
            upload_receipt = path.with_suffix(".json")
            if upload_receipt.exists():
                upload = json.loads(upload_receipt.read_text(encoding="utf-8"))
                if (
                    not isinstance(upload, dict)
                    or upload.get("sha256") != checksum
                    or upload.get("staging_path") != relative
                ):
                    raise ValidationError("The staged original belongs to another upload.")
                # Remove the receipt first so an interrupted cleanup cannot resurrect a bill.
                upload_receipt.unlink()
                _sync_directory(path.parent)
            temporary = path.with_suffix(".json.tmp")
            if temporary.exists():
                temporary.unlink()
                _sync_directory(path.parent)
        if path.exists():
            path.unlink()
            _sync_directory(path.parent)
    receipt_path.unlink()
    _sync_directory(receipt_path.parent)


def recover_document_deletions():
    """Resume committed cleanup; discard receipts for transactions that rolled back."""
    with originals_lock():
        for receipt_path in sorted(private_path(".deletions").glob("*.json")):
            try:
                _finish_deletion(receipt_path)
            except ValueError, KeyError, TypeError:
                raise ValidationError(
                    "Invalid document deletion receipt; check private storage."
                ) from None


def delete_document(document_id, *, token):
    """Delete the confirmed document and all its records, then remove its originals.

    Return False only when the committed deletion still needs file cleanup. The receipt
    survives interruption and is retried before upload, storage recovery and backup.
    """
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Delete documents outside database transactions.")
    try:
        expected = signing.loads(token, salt=DELETION_SALT)
        if not isinstance(expected, dict) or expected.get("document_id") != document_id:
            raise ValueError
    except signing.BadSignature, TypeError, ValueError:
        raise ValidationError("The delete form is invalid; reload the page.") from None
    source = Document.objects.get(pk=document_id)
    if source.sha256:
        _mkdir(private_path(f".parse-{source.sha256}.lock").parent)
    parse_lock = (
        FileLock(str(private_path(f".parse-{source.sha256}.lock")), timeout=10, mode=0o600)
        if source.sha256
        else nullcontext()
    )
    # Use the parser's lock order: parse lock first, then the originals/backup lock.
    with parse_lock, originals_lock():
        recover_document_deletions()
        source.refresh_from_db()
        if _version(source) != expected:
            raise StaleDeletionError("This document has changed; reload the delete page.")
        receipt_path = _write_receipt(source, _original_paths(source))
        try:
            with transaction.atomic():
                source = Document.objects.select_for_update().get(pk=document_id)
                if _version(source) != expected:
                    raise StaleDeletionError("This document has changed; reload the delete page.")
                # History stays immutable during review. Explicit whole-document deletion
                # removes it in dependency order, including approved allocations and usage.
                ReviewRevision.objects.filter(invoice__document_id=document_id).delete()
                Invoice.objects.filter(document_id=document_id).delete()
                ExtractionAttempt.objects.filter(document_id=document_id).delete()
                source.delete()
        except Exception:
            try:
                receipt_path.unlink(missing_ok=True)
                _sync_directory(receipt_path.parent)
            except OSError:
                pass
            raise
        try:
            _finish_deletion(receipt_path)
        except OSError, DatabaseError, ValidationError, ValueError, KeyError, TypeError:
            return False
        return True
