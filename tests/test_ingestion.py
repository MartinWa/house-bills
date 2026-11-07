import hashlib
import io
import json
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections
from django.test import Client

from bills import storage
from bills.models import BillingSection, Document, Invoice, Property, Usage, Vendor
from bills.parsing import layout_02
from bills.parsing.reader import read_pdf
from bills.services import (
    approve_review,
    invoice_identity_conflicts,
    record_extraction,
    register_invoice_identity,
)

from .synthetic import shared_supply, write_pdf

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def original_root(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "originals"
    return Path(settings.MEDIA_ROOT)


@pytest.fixture
def pdf(tmp_path):
    return write_pdf(tmp_path / "synthetic.pdf", shared_supply())


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(
        "synthetic-uploader", password="synthetic-password"
    )


def ingest(pdf, filename="synthetic.pdf"):
    with pdf.open("rb") as source:
        return storage.ingest_pdf(source, filename=filename)


def approve(document, pdf, user, *, properties=2, reference=None):
    candidate = layout_02.parse(read_pdf(pdf))
    if properties == 1:
        # Two installation sections can share a property, but their overlapping
        # supply usage needs explicit review rather than being silently summed.
        candidate.sections[1].usages = []
    if reference is not None:
        candidate.reference = reference
    vendor, _ = Vendor.objects.get_or_create(name="Synthetic Vendor", slug="synthetic-vendor")
    houses = [
        Property.objects.create(name=f"House {i}", slug=f"house-{i}") for i in range(properties)
    ]
    invoice = register_invoice_identity(document.pk, vendor=vendor, reference=candidate.reference)
    attempt = record_extraction(document.pk, candidate, vendor=vendor)
    return approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={
            section.section_id: houses[i % properties]
            for i, section in enumerate(candidate.sections)
        },
        actor=user,
        extraction=attempt,
    )


def test_original_bytes_deduplicate_across_house_folders_and_names(pdf, original_root):
    document, duplicate = ingest(pdf, r"house-one\first.pdf")
    assert not duplicate and document.original_filename == "first.pdf"
    assert document.sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert document.original_path == f"inbox/{document.sha256}.pdf"
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()
    same, duplicate = ingest(pdf, "house-two/renamed.PDF")
    assert duplicate and same.pk == document.pk and same.original_filename == "first.pdf"
    assert Document.objects.count() == 1 and Invoice.objects.count() == 0
    assert BillingSection.objects.count() == Usage.objects.count() == 0
    assert len(list(original_root.rglob("*.pdf"))) == 1
    assert not list(original_root.rglob("*.json"))
    assert document.state == "review" and document.storage_state == "inbox"
    assert storage.private_path(document.original_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "data,filename", [(b"not a pdf", "bad.pdf"), (b"", "empty.pdf"), (b"%PDF-1.7", "bad.txt")]
)
def test_invalid_upload_rejected_without_rows_or_originals(data, filename, original_root):
    with pytest.raises(storage.UploadRejected):
        storage.ingest_pdf(io.BytesIO(data), filename=filename)
    assert not Document.objects.exists()
    assert not list(original_root.rglob("*.pdf"))
    assert not list(original_root.rglob("*.json"))


def test_stream_limit_uses_received_bytes_and_accepts_exact_limit(settings, original_root):
    settings.MAX_PDF_UPLOAD_BYTES = 10
    document, _ = storage.ingest_pdf(io.BytesIO(b"%PDF-12345"), filename="small.pdf")
    with pytest.raises(storage.UploadRejected, match="size limit"):
        storage.ingest_pdf(io.BytesIO(b"%PDF-123456"), filename="large.pdf")
    assert Document.objects.count() == 1
    assert len(list(original_root.rglob("*.pdf"))) == 1
    assert storage.private_path(document.original_path).read_bytes() == b"%PDF-12345"


def test_interrupted_input_does_not_publish_partial_bytes(original_root):
    class BrokenStream:
        calls = 0

        def read(self, size):
            self.calls += 1
            if self.calls == 1:
                return b"%PDF-1.7"
            raise OSError("Synthetic connection interruption")

    with pytest.raises(OSError):
        storage.ingest_pdf(BrokenStream(), filename="partial.pdf")
    assert not Document.objects.exists() and not list(original_root.rglob("*.pdf"))


def test_failed_inbox_move_keeps_complete_bytes_and_retry_cleans_stage(
    pdf, original_root, monkeypatch
):
    move = storage._move_original
    monkeypatch.setattr(
        storage,
        "_move_original",
        lambda *args: (_ for _ in ()).throw(OSError("Synthetic full disk")),
    )
    document, _ = ingest(pdf)
    assert document.storage_state == "failed" and document.staging_path
    assert storage.private_path(document.staging_path).read_bytes() == pdf.read_bytes()
    monkeypatch.setattr(storage, "_move_original", move)
    document = storage.retry_document(document.pk)
    assert document.storage_state == "inbox" and not document.storage_error
    assert not document.staging_path and len(list(original_root.rglob("*.pdf"))) == 1
    assert not list(original_root.rglob("*.json"))


def test_database_failure_before_registration_recovered_from_durable_receipt(pdf, monkeypatch):
    get_or_create = Document.objects.get_or_create
    monkeypatch.setattr(
        Document.objects,
        "get_or_create",
        lambda **kwargs: (_ for _ in ()).throw(OperationalError("Synthetic database outage")),
    )
    with pytest.raises(OperationalError):
        ingest(pdf, "house-two/remembered.pdf")
    assert not Document.objects.exists()
    monkeypatch.setattr(Document.objects, "get_or_create", get_or_create)
    call_command("recover_documents")
    document = Document.objects.get()
    assert document.original_filename == "remembered.pdf" and document.storage_state == "inbox"
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()


@pytest.mark.parametrize("after_move", [False, True])
def test_process_interruption_during_inbox_publication_is_resumable(pdf, monkeypatch, after_move):
    class Interrupted(BaseException):
        pass

    move = storage._move_original

    def interrupt(*args):
        if after_move:
            move(*args)
        raise Interrupted()

    monkeypatch.setattr(storage, "_move_original", interrupt)
    with pytest.raises(Interrupted):
        ingest(pdf)
    document = Document.objects.get()
    assert document.staging_path and document.storage_state == "pending"
    monkeypatch.setattr(storage, "_move_original", move)
    storage.recover_originals()
    document.refresh_from_db()
    assert document.storage_state == "inbox" and not document.staging_path
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()


def test_approval_publishes_one_shared_original_and_duplicate_preserves_costs(
    pdf, user, original_root
):
    document, _ = ingest(pdf)
    invoice = approve(document, pdf, user)
    document.refresh_from_db()
    assert document.original_path.startswith("shared/synthetic-vendor/2024/2024-03-05_")
    assert document.storage_state == "approved" and not document.publication_path
    assert not storage.private_path(f"inbox/{document.sha256}.pdf").exists()
    same, duplicate = ingest(pdf, "house-two/duplicate.pdf")
    assert same.pk == document.pk and duplicate
    assert len(list(original_root.rglob("*.pdf"))) == 1
    assert same.state == "ready" and same.storage_state == "approved"
    assert Invoice.objects.count() == 1 and Usage.objects.count() == 2
    assert (
        sum(BillingSection.objects.values_list("gross_minor", flat=True)) + invoice.rounding_minor
        == 44700
    )
    assert invoice.review_revision == 1 and invoice.reviews.count() == 1


def test_different_bytes_with_same_identity_are_flagged_without_overwriting_approval(
    pdf, user, client
):
    original, _ = ingest(pdf)
    approved = approve(original, pdf, user)
    other, _ = storage.ingest_pdf(
        io.BytesIO(pdf.read_bytes() + b"\n% synthetic variation"), filename="variation.pdf"
    )
    candidate = layout_02.parse(read_pdf(pdf))
    record_extraction(other.pk, candidate, vendor=approved.vendor)
    conflicting = other.invoices.get()
    assert list(invoice_identity_conflicts(conflicting)) == [approved]
    assert conflicting.status == "review" and not conflicting.sections.exists()
    client.force_login(user)
    for source in (original, other):
        response = client.get(f"/documents/{source.pk}/")
        assert b"Different files share this invoice identity" in response.content
    approved.refresh_from_db()
    assert approved.status == "approved" and approved.invoice_total_minor == 44700
    with pytest.raises(ValidationError):
        approve_review(
            conflicting.pk,
            expected_revision=0,
            candidate=candidate,
            vendor=approved.vendor,
            section_properties={
                section.section_id: section.property for section in approved.sections.all()
            },
            actor=user,
        )


def test_single_property_path_sanitizes_reference_and_uses_collision_suffix(pdf, user, monkeypatch):
    document, _ = ingest(pdf)
    reference = "../../private\\ invoice:reference"
    expected = (
        "house-0/synthetic-vendor/2024/2024-03-05_private-invoicereference_"
        f"{document.sha256[:12]}.pdf"
    )
    occupied = storage.private_path(expected)
    storage._mkdir(occupied.parent)
    occupied.write_bytes(b"different retained bytes")
    approve(document, pdf, user, properties=1, reference=reference)
    document.refresh_from_db()
    assert document.original_path == expected[:-4] + "-1.pdf"
    assert occupied.read_bytes() == b"different retained bytes"
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()


def test_unapproved_invoice_cannot_publish(pdf):
    document, _ = ingest(pdf)
    with pytest.raises(ValidationError, match="Approve every"):
        storage.publish_original(document.pk)
    assert document.storage_state == "inbox"


def test_failed_approval_publication_preserves_approved_values_and_retries(pdf, user, monkeypatch):
    document, _ = ingest(pdf)
    move = storage._move_original
    monkeypatch.setattr(
        storage,
        "_move_original",
        lambda *args: (_ for _ in ()).throw(OSError("Synthetic rename failure")),
    )
    invoice = approve(document, pdf, user)
    document.refresh_from_db()
    assert invoice.status == "approved" and document.storage_state == "failed"
    assert document.publication_path and storage.private_path(document.original_path).is_file()
    monkeypatch.setattr(storage, "_move_original", move)
    storage.retry_document(document.pk)
    document.refresh_from_db()
    assert document.storage_state == "approved" and not document.publication_path
    assert invoice.sections.count() == 2


def test_interruption_after_approved_move_before_database_update_recovers(pdf, user, monkeypatch):
    document, _ = ingest(pdf)
    move = storage._move_original

    class Interrupted(BaseException):
        pass

    def move_then_interrupt(*args):
        move(*args)
        raise Interrupted()

    monkeypatch.setattr(storage, "_move_original", move_then_interrupt)
    with pytest.raises(Interrupted):
        approve(document, pdf, user)
    document.refresh_from_db()
    assert document.storage_state == "publishing" and document.publication_path
    assert not storage.private_path(document.original_path).exists()
    assert storage.private_path(document.publication_path).is_file()
    monkeypatch.setattr(storage, "_move_original", move)
    call_command("recover_documents")
    document.refresh_from_db()
    assert document.storage_state == "approved" and not document.publication_path
    assert document.invoices.get().status == "approved"


def test_exact_duplicate_restores_missing_approved_original(pdf, user):
    document, _ = ingest(pdf)
    invoice = approve(document, pdf, user)
    document.refresh_from_db()
    original_path = document.original_path
    storage.private_path(original_path).unlink()
    duplicate, reused = ingest(pdf, "restore.pdf")
    assert reused and duplicate.pk == document.pk
    assert duplicate.storage_state == "approved" and duplicate.original_path == original_path
    assert storage.private_path(original_path).read_bytes() == pdf.read_bytes()
    invoice.refresh_from_db()
    assert invoice.status == "approved" and invoice.review_revision == 1


def test_corrupt_original_is_retained_and_never_replaced_by_duplicate(pdf):
    document, _ = ingest(pdf)
    target = storage.private_path(document.original_path)
    target.write_bytes(b"synthetic corruption")
    duplicate, reused = ingest(pdf)
    assert reused and duplicate.storage_state == "failed"
    assert target.read_bytes() == b"synthetic corruption"
    with pytest.raises(OSError):
        storage.open_original(document.pk)


def test_private_path_rejects_traversal_and_symlinks(original_root, tmp_path):
    with pytest.raises(ValidationError):
        storage.private_path("../outside.pdf")
    original_root.mkdir()
    (original_root / "outside").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ValidationError):
        storage.private_path("outside/original.pdf")


def test_two_threads_uploading_same_pdf_create_one_original(pdf, original_root):
    barrier = Barrier(2)

    def worker(index):
        connections.close_all()
        try:
            barrier.wait(timeout=5)
            document, duplicate = ingest(pdf, f"house-{index}/invoice.pdf")
            return document.pk, duplicate
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, (1, 2)))
    assert results[0][0] == results[1][0]
    assert sorted(duplicate for _, duplicate in results) == [False, True]
    assert Document.objects.count() == 1 and len(list(original_root.rglob("*.pdf"))) == 1


def test_backup_lock_blocks_another_process_and_snapshot_matches_originals(
    pdf, original_root, tmp_path
):
    document, _ = ingest(pdf)
    marker = tmp_path / "lock-acquired"
    script = """
import django, sys
from pathlib import Path
django.setup()
from django.conf import settings
from bills.storage import originals_lock
settings.MEDIA_ROOT = Path(sys.argv[1])
print('waiting', flush=True)
with originals_lock():
    Path(sys.argv[2]).write_text('acquired')
"""
    with storage.originals_lock():
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(original_root), str(marker)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "waiting"
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
            assert not marker.exists()
            backup = tmp_path / "backup.sqlite3"
            with sqlite3.connect(backup) as target:
                connection.connection.backup(target)
            shutil.copytree(original_root, tmp_path / "backup-originals")
        except BaseException:
            process.kill()
            raise
    assert process.communicate(timeout=5)[1] == "" and process.returncode == 0
    assert marker.read_text() == "acquired"
    with sqlite3.connect(backup) as source:
        digest, path = source.execute("SELECT sha256, original_path FROM bills_document").fetchone()
        assert source.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert hashlib.sha256((tmp_path / "backup-originals" / path).read_bytes()).hexdigest() == digest
    assert digest == document.sha256


def test_authenticated_upload_duplicate_and_private_source_with_csrf(pdf, user):
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    client.get("/upload/")
    token = client.cookies["csrftoken"].value
    assert (
        client.post(
            "/upload/", {"pdf": SimpleUploadedFile("invoice.pdf", pdf.read_bytes())}
        ).status_code
        == 403
    )
    assert not Document.objects.exists()
    response = client.post(
        "/upload/",
        {"pdf": SimpleUploadedFile("invoice.pdf", pdf.read_bytes()), "csrfmiddlewaretoken": token},
    )
    document = Document.objects.get()
    assert response.status_code == 302 and response.url == f"/documents/{document.pk}/"
    response = client.post(
        "/upload/",
        {"pdf": SimpleUploadedFile("renamed.pdf", pdf.read_bytes()), "csrfmiddlewaretoken": token},
        follow=True,
    )
    assert b"already stored" in response.content and Document.objects.count() == 1
    response = client.get(f"/documents/{document.pk}/source/")
    assert response.status_code == 200 and response["Content-Type"] == "application/pdf"
    assert response["Content-Disposition"].startswith("attachment;")
    assert "no-store" in response["Cache-Control"]
    assert b"".join(response.streaming_content) == pdf.read_bytes()
    assert client.get("/documents/").status_code == 200
    assert client.get(f"/documents/{document.pk}/retry/").status_code == 405
    assert client.post(f"/documents/{document.pk}/retry/").status_code == 403


def test_anonymous_upload_status_source_and_retry_are_private(pdf, client):
    document, _ = ingest(pdf)
    for path in (
        "/upload/",
        "/documents/",
        f"/documents/{document.pk}/",
        f"/documents/{document.pk}/source/",
    ):
        response = client.get(path)
        assert response.status_code == 302 and response.url.startswith("/login/")
    assert (
        client.post(
            "/upload/", {"pdf": SimpleUploadedFile("anonymous.pdf", pdf.read_bytes())}
        ).status_code
        == 302
    )
    assert Document.objects.count() == 1


def test_upload_http_limit_and_invalid_header_are_visible(pdf, settings, client, user):
    client.force_login(user)
    settings.MAX_PDF_UPLOAD_BYTES = 10
    response = client.post("/upload/", {"pdf": SimpleUploadedFile("large.pdf", pdf.read_bytes())})
    assert response.status_code == 413 and b"size limit" in response.content
    assert not Document.objects.exists()
    response = client.post("/upload/", {"pdf": SimpleUploadedFile("bad.pdf", b"bad")})
    assert response.status_code == 400 and b"PDF header" in response.content
    response = client.post(
        "/upload/",
        {"pdf": SimpleUploadedFile("large.pdf", b"%PDF-1")},
        CONTENT_LENGTH=str(settings.MAX_UPLOAD_REQUEST_BYTES + 1),
    )
    assert response.status_code == 413


def test_reparse_failure_and_candidates_survive_duplicate_upload(pdf, user):
    document, _ = ingest(pdf)
    approve(document, pdf, user)
    record_extraction(document.pk, error="Synthetic unsupported replacement")
    duplicate, reused = ingest(pdf)
    assert reused and duplicate.state == "failed"
    assert duplicate.processing_message == "Synthetic unsupported replacement"
    assert duplicate.extractions.count() == 3 and duplicate.invoices.get().status == "approved"


def test_database_failure_after_move_keeps_journal_and_approval_recoverable(pdf, user, monkeypatch):
    document, _ = ingest(pdf)
    move = storage._move_original
    original_filter = Document.objects.filter
    failed = False

    def fail_final_write(*args):
        nonlocal failed
        move(*args)
        if not failed:
            failed = True

            class FailedUpdate:
                def update(self, **kwargs):
                    monkeypatch.setattr(Document.objects, "filter", original_filter)
                    raise OperationalError("Synthetic post-move database interruption")

            monkeypatch.setattr(Document.objects, "filter", lambda **kwargs: FailedUpdate())

    monkeypatch.setattr(storage, "_move_original", fail_final_write)
    invoice = approve(document, pdf, user)
    document.refresh_from_db()
    assert invoice.status == "approved" and document.storage_state == "failed"
    assert document.publication_path and storage.private_path(document.publication_path).is_file()
    monkeypatch.setattr(storage, "_move_original", move)
    call_command("recover_documents")
    document.refresh_from_db()
    assert document.storage_state == "approved" and not document.publication_path


def test_invalid_receipt_is_reported_and_bytes_retained(pdf, original_root):
    document, _ = ingest(pdf)
    staged = storage.private_path(".staging/invalid.pdf")
    staged.write_bytes(pdf.read_bytes())
    staged.with_suffix(".json").write_text(json.dumps({"staging_path": "../../outside.pdf"}))
    with pytest.raises(CommandError, match="invalid staging receipts"):
        call_command("recover_documents")
    assert staged.read_bytes() == pdf.read_bytes()
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()


def test_approved_correction_moves_original_to_current_reviewed_path(pdf, user, original_root):
    document, _ = ingest(pdf)
    invoice = approve(document, pdf, user)
    document.refresh_from_db()
    previous_path = document.original_path
    candidate = layout_02.parse(read_pdf(pdf))
    candidate.sections[1].usages = []
    house = invoice.sections.first().property
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={section.section_id: house for section in candidate.sections},
        actor=user,
    )
    document.refresh_from_db()
    assert document.original_path.startswith(house.slug + "/")
    assert not storage.private_path(previous_path).exists()
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()
    assert len(list(original_root.rglob("*.pdf"))) == 1


def test_reserved_publication_destination_is_not_reused(pdf, user):
    document, _ = ingest(pdf)
    expected = f"shared/synthetic-vendor/2024/2024-03-05_880003_{document.sha256[:12]}.pdf"
    Document.objects.create(
        sha256="f" * 64,
        original_filename="different.pdf",
        publication_path=expected,
        storage_state="publishing",
    )
    approve(document, pdf, user)
    document.refresh_from_db()
    assert document.original_path == expected[:-4] + "-1.pdf"
