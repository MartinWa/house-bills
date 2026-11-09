import json

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, connection
from django.test import Client

from bills import backups, deleting, storage, views
from bills.models import (
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    ReviewRevision,
    Usage,
    Vendor,
)
from bills.processing import parse_document
from bills.reporting import read_report
from bills.services import discard_manual_entry

from .synthetic import shared_supply
from .test_review import approved, manual_fields, review_data, upload
from .test_review import configured as configured

pytestmark = pytest.mark.django_db(transaction=True)


def delete_form(client, document):
    path = f"/documents/{document.pk}/delete/"
    response = client.get(path)
    assert response.status_code == 200
    assert Document.objects.filter(pk=document.pk).exists()
    return path, {"token": response.context["form"]["token"].value()}


@pytest.mark.parametrize("state", Document.State.values)
def test_uploaded_document_can_be_deleted_in_every_state(configured, client, tmp_path, state):
    document, _ = upload(client, tmp_path)
    Document.objects.filter(pk=document.pk).update(state=state)
    original = storage.private_path(document.original_path)
    path, data = delete_form(client, document)
    response = client.post(path, data)
    assert response.status_code == 302 and response.url == "/documents/"
    assert not Document.objects.filter(pk=document.pk).exists()
    assert not Invoice.objects.exists() and not ExtractionAttempt.objects.exists()
    assert not original.exists()
    assert client.get(f"/documents/{document.pk}/view/").status_code == 404
    assert parse_document(document.pk) is None


@pytest.mark.parametrize("missing", [True, False])
def test_failed_documents_with_missing_or_corrupt_originals_can_be_deleted(
    configured, client, tmp_path, missing
):
    if missing:
        document = Document.objects.create(
            sha256="b" * 64,
            original_filename="missing.pdf",
            storage_state=Document.StorageState.FAILED,
            state=Document.State.FAILED,
        )
    else:
        document, _ = upload(client, tmp_path)
        storage.private_path(document.original_path).write_bytes(b"Synthetic corrupt original")
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302
    assert not Document.objects.exists()


def test_delete_removes_approved_history_usage_and_reports_but_keeps_other_documents(
    configured, client, tmp_path
):
    actor, vendor, houses = configured
    document, _ = upload(client, tmp_path, filename="first.pdf")
    invoice = approved(client, document)
    path, data = review_data(client, document)
    assert client.post(path, {**data, "reference": "SYNTHETIC-CORRECTED-001"}).status_code == 302
    document.refresh_from_db()
    assert (
        client.post(
            f"/documents/{document.pk}/reparse/",
            {"extraction_revision": document.extraction_revision},
        ).status_code
        == 302
    )
    assert invoice.reviews.count() == 2 and document.extractions.count() == 2
    document.refresh_from_db()
    original = storage.private_path(document.original_path)

    pages = shared_supply()
    pages[0].append("Synthetic second retained document")
    other, other_bytes = upload(client, tmp_path, pages, filename="second.pdf")
    other_invoice = approved(client, other)
    other.refresh_from_db()
    assert read_report()["total_invoices"] == 2

    path, data = delete_form(client, document)
    response = client.post(path, data)
    assert response.status_code == 302
    assert not Document.objects.filter(pk=document.pk).exists()
    assert not Invoice.objects.filter(pk=invoice.pk).exists()
    assert not ReviewRevision.objects.filter(invoice_id=invoice.pk).exists()
    assert not ExtractionAttempt.objects.filter(document_id=document.pk).exists()
    assert not Usage.objects.filter(section__invoice_id=invoice.pk).exists()
    assert not original.exists()
    assert read_report()["total_invoices"] == read_report()["pdf_invoices"] == 1
    assert other.invoices.get().pk == other_invoice.pk
    assert other_invoice.reviews.count() == 1 and other.extractions.count() == 1
    assert storage.private_path(other.original_path).read_bytes() == other_bytes
    assert Vendor.objects.filter(pk=vendor.pk).exists()
    assert Property.objects.filter(pk__in=[house.pk for house in houses]).count() == 2
    assert actor.__class__.objects.filter(pk=actor.pk).exists()
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA foreign_key_check")
        assert cursor.fetchall() == []


def test_approved_manual_document_deletes_all_its_invoices(configured, client):
    _, vendor, houses = configured
    response = client.post("/invoices/manual/")
    document = Document.objects.get(pk=int(response.url.split("/")[2]))
    path, data = review_data(client, document)
    assert client.post(path, manual_fields(data, vendor, houses[0])).status_code == 302
    Invoice.objects.create(document=document, vendor=vendor, reference="SYNTHETIC-UNAPPROVED-002")
    assert read_report()["manual_invoices"] == 1
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302
    assert not Document.objects.exists() and not Invoice.objects.exists()
    assert not ReviewRevision.objects.exists() and not Usage.objects.exists()
    assert read_report()["manual_invoices"] == 0


@pytest.mark.parametrize("approve_first", [False, True])
def test_identical_pdf_upload_starts_fresh_after_deletion(
    configured, client, tmp_path, approve_first
):
    document, original_bytes = upload(client, tmp_path)
    if approve_first:
        approved(client, document)
    old_pk = document.pk
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302
    response = client.post(
        "/upload/", {"pdf": SimpleUploadedFile("again.pdf", original_bytes)}, follow=True
    )
    assert response.status_code == 200
    assert "PDF uploaded." in response.content.decode()
    assert "already stored" not in response.content.decode()
    fresh = Document.objects.get()
    assert fresh.pk != old_pk and fresh.original_filename == "again.pdf"
    assert fresh.state == Document.State.REVIEW and fresh.extraction_revision == 1
    assert fresh.invoices.get().review_revision == 0
    assert fresh.invoices.get().status == Invoice.Status.REVIEW
    assert not ReviewRevision.objects.exists()
    assert storage.private_path(fresh.original_path).read_bytes() == original_bytes
    assert read_report()["total_invoices"] == 0


def test_delete_page_requires_login_csrf_and_a_valid_confirmation(configured, client, tmp_path):
    document, _ = upload(client, tmp_path)
    path = f"/documents/{document.pk}/delete/"
    anonymous = Client()
    for method in (anonymous.get, anonymous.post):
        response = method(path)
        assert response.status_code == 302 and response.url.startswith("/login/")
    secured = Client(enforce_csrf_checks=True)
    secured.force_login(configured[0])
    assert secured.post(path).status_code == 403
    page = secured.get(path)
    assert page.status_code == 200
    assert Document.objects.filter(pk=document.pk).exists()
    data = {"csrfmiddlewaretoken": secured.cookies["csrftoken"].value}
    assert secured.post(path, data).status_code == 400
    assert secured.post(path, {**data, "token": "invalid-synthetic-token"}).status_code == 400
    assert Document.objects.filter(pk=document.pk).exists()
    data["token"] = page.context["form"]["token"].value()
    assert secured.post(path, data).status_code == 302
    assert not Document.objects.filter(pk=document.pk).exists()


@pytest.mark.parametrize("change", ["extraction", "approval", "correction"])
def test_stale_delete_confirmation_retains_changed_document(configured, client, tmp_path, change):
    document, _ = upload(client, tmp_path)
    if change == "correction":
        approved(client, document)
    path, data = delete_form(client, document)
    if change == "extraction":
        assert (
            client.post(
                f"/documents/{document.pk}/reparse/",
                {"extraction_revision": document.extraction_revision},
            ).status_code
            == 302
        )
    else:
        approved(client, document)
    response = client.post(path, data)
    assert response.status_code == 409
    assert b"This document has changed" in response.content
    assert Document.objects.filter(pk=document.pk).exists()
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302


def test_delete_token_cannot_remove_another_document(configured, client, tmp_path):
    document, _ = upload(client, tmp_path)
    other = Document.objects.create(source=Document.Source.MANUAL, state=Document.State.REVIEW)
    _, data = delete_form(client, document)
    assert client.post(f"/documents/{other.pk}/delete/", data).status_code == 400
    assert Document.objects.count() == 2


def test_failed_database_deletion_restores_history_and_keeps_original(
    configured, client, tmp_path, monkeypatch
):
    document, original_bytes = upload(client, tmp_path)
    invoice = approved(client, document)
    document.refresh_from_db()
    original = storage.private_path(document.original_path)
    snapshot, report = invoice.reviews.get().snapshot, read_report()
    original_delete = Document.delete

    def fail_after_delete(instance, *args, **kwargs):
        original_delete(instance, *args, **kwargs)
        raise DatabaseError("Synthetic failure after deleting the document")

    monkeypatch.setattr(Document, "delete", fail_after_delete)
    path, data = delete_form(client, document)
    response = client.post(path, data)
    assert response.status_code == 503 and b"document was retained" in response.content
    assert Document.objects.filter(pk=document.pk).exists()
    assert invoice.reviews.get().snapshot == snapshot
    assert original.read_bytes() == original_bytes
    assert read_report() == report
    assert list(storage.private_path(".deletions").glob("*.json")) == []


@pytest.mark.parametrize("failure", [OSError, DatabaseError])
def test_failed_cleanup_is_recoverable(configured, client, tmp_path, monkeypatch, failure):
    document, _ = upload(client, tmp_path)
    original = storage.private_path(document.original_path)
    path, data = delete_form(client, document)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            deleting,
            "_finish_deletion",
            lambda *_: (_ for _ in ()).throw(failure("Synthetic cleanup failure")),
        )
        response = client.post(path, data, follow=True)
    assert response.status_code == 200 and b"PDF cleanup is pending" in response.content
    assert not Document.objects.exists() and original.exists()
    assert storage.recover_originals() == []
    assert not original.exists()
    assert list(storage.private_path(".deletions").glob("*.json")) == []


@pytest.mark.parametrize("kind", ["manual", "parsed", "approved"])
def test_review_save_returns_404_when_document_is_deleted_before_write(
    configured, client, tmp_path, monkeypatch, kind
):
    if kind == "manual":
        response = client.post("/invoices/manual/")
        document = Document.objects.get(pk=int(response.url.split("/")[2]))
    else:
        document, _ = upload(client, tmp_path)
        if kind == "approved":
            approved(client, document)
    path, data = review_data(client, document)
    if kind == "manual":
        data = manual_fields(data, configured[1], configured[2][0])
    approve = views.approve_document_review

    def delete_before_write(document_id, **review):
        # The request has already loaded its source and validated its form.
        if kind == "manual":
            discard_manual_entry(document_id)
        else:
            current = Document.objects.get(pk=document_id)
            deleting.delete_document(document_id, token=deleting.deletion_token(current))
        return approve(document_id, **review)

    monkeypatch.setattr(views, "approve_document_review", delete_before_write)
    response = client.post(path, data)
    assert response.status_code == 404
    assert not Document.objects.exists() and not Invoice.objects.exists()
    assert not ReviewRevision.objects.exists() and not ExtractionAttempt.objects.exists()
    assert not Usage.objects.exists()
    assert read_report()["total_invoices"] == 0


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_deletion_recovers_consistently(
    configured, client, tmp_path, monkeypatch, committed
):
    document, original_bytes = upload(client, tmp_path)
    approved(client, document)
    document.refresh_from_db()
    original = storage.private_path(document.original_path)
    path, data = delete_form(client, document)
    with monkeypatch.context() as interrupted:
        if committed:

            def stop_cleanup(*_):
                raise SystemExit("Synthetic interruption after database commit")

            interrupted.setattr(deleting, "_finish_deletion", stop_cleanup)
        else:
            original_delete = Document.delete

            def stop_after_delete(instance, *args, **kwargs):
                original_delete(instance, *args, **kwargs)
                raise SystemExit("Synthetic interruption before database commit")

            interrupted.setattr(Document, "delete", stop_after_delete)
        with pytest.raises(SystemExit):
            client.post(path, data)
    assert Document.objects.filter(pk=document.pk).exists() is not committed
    assert original.read_bytes() == original_bytes
    deleting.recover_document_deletions()
    assert original.exists() is not committed
    assert list(storage.private_path(".deletions").glob("*.json")) == []
    assert Invoice.objects.exists() is not committed


def test_deleting_failed_intake_removes_duplicate_receipts_and_prevents_recovery(
    configured, client, tmp_path, monkeypatch
):
    with monkeypatch.context() as failed_storage:
        failed_storage.setattr(
            storage,
            "_move_original",
            lambda *_: (_ for _ in ()).throw(OSError("Synthetic storage failure")),
        )
        document, original_bytes = upload(client, tmp_path)
        assert (
            client.post(
                "/upload/", {"pdf": SimpleUploadedFile("duplicate.pdf", original_bytes)}
            ).status_code
            == 302
        )
    staging = storage.private_path(".staging")
    assert len(list(staging.glob("*.json"))) == 2
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302
    assert list(staging.iterdir()) == []
    assert storage.recover_originals() == []
    assert not Document.objects.exists()


def test_deleting_interrupted_publication_removes_the_moved_original(
    configured, client, tmp_path, monkeypatch
):
    document, _ = upload(client, tmp_path)
    original_move = storage._move_original

    def stop_after_move(*args):
        original_move(*args)
        raise SystemExit("Synthetic interruption after moving the original")

    with monkeypatch.context() as interrupted:
        interrupted.setattr(storage, "_move_original", stop_after_move)
        with pytest.raises(SystemExit):
            approved(client, document)
    document.refresh_from_db()
    target = storage.private_path(document.publication_path)
    assert target.exists() and document.storage_state == Document.StorageState.PUBLISHING
    path, data = delete_form(client, document)
    assert client.post(path, data).status_code == 302
    assert not target.exists() and not Document.objects.exists()
    assert storage.recover_originals() == []


def test_backup_resumes_pending_deletion_and_excludes_deleted_records(
    configured, client, tmp_path, monkeypatch
):
    document, _ = upload(client, tmp_path)
    path, data = delete_form(client, document)
    with monkeypatch.context() as interrupted:
        interrupted.setattr(
            deleting,
            "_finish_deletion",
            lambda *_: (_ for _ in ()).throw(SystemExit("Synthetic interrupted cleanup")),
        )
        with pytest.raises(SystemExit):
            client.post(path, data)
    configuration = tmp_path / "operator.env"
    configuration.write_text("HOUSE_BILLS_SECRET_KEY=synthetic-backup-configuration\n")
    destination = tmp_path / "backup"
    manifest = backups.create_backup(destination, [configuration])
    backups.verify_backup(destination)
    assert not any(item["path"].endswith(".pdf") for item in manifest["files"])
    assert not Document.objects.exists()


def test_deletion_recovery_preserves_a_later_documents_original(configured, client, tmp_path):
    document, original_bytes = upload(client, tmp_path)
    original_path, checksum, old_pk = document.original_path, document.sha256, document.pk
    document.extractions.all().delete()
    document.invoices.all().delete()
    document.delete()
    later = Document.objects.create(
        sha256=checksum,
        original_filename="later.pdf",
        original_path=original_path,
        storage_state=Document.StorageState.INBOX,
    )
    directory = storage.private_path(".deletions")
    directory.mkdir()
    (directory / f"{old_pk}.json").write_text(
        json.dumps({"document_id": old_pk, "sha256": checksum, "paths": [original_path]})
    )
    deleting.recover_document_deletions()
    assert storage.private_path(later.original_path).read_bytes() == original_bytes
    assert Document.objects.filter(pk=later.pk).exists()


def test_deletion_receipts_cannot_escape_private_storage(configured, tmp_path):
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"Synthetic bytes outside private originals")
    directory = storage.private_path(".deletions")
    directory.mkdir(parents=True)
    (directory / "999.json").write_text(
        json.dumps({"document_id": 999, "sha256": "a" * 64, "paths": ["../outside.pdf"]})
    )
    with pytest.raises(ValidationError):
        deleting.recover_document_deletions()
    assert outside.exists()
