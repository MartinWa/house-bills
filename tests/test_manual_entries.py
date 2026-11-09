import json
import sqlite3
from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from bills import backups, storage
from bills.management.commands import import_ledger_invoices
from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Property,
    PropertyIdentifier,
    ReviewRevision,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.reporting import read_report

from .test_review import manual_fields, review_data

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def configured(tmp_path, client, django_user_model):
    actor = django_user_model.objects.create_user("synthetic-manual-reviewer")
    client.force_login(actor)
    houses = [Property.objects.create(name=f"House {i}", slug=f"house-{i}") for i in (1, 2)]
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000999901")
    for family in ("layout_01", "layout_02"):
        VendorLayoutFamily.objects.create(vendor=vendor, family=family)
    for i, house in enumerate(houses, 1):
        PropertyIdentifier.objects.create(
            vendor=vendor, property=house, kind="installation", value=f"{i:018}"
        )
    return actor, vendor, houses


def create_manual(client):
    response = client.post("/invoices/manual/")
    assert response.status_code == 302
    return Document.objects.get(pk=int(response.url.split("/")[2]))


def test_manual_entry_creates_document_without_original(configured, client):
    document = create_manual(client)
    assert document.source == Document.Source.MANUAL
    assert document.sha256 == "" and document.original_filename == ""
    assert document.original_path == document.staging_path == document.publication_path == ""
    assert document.storage_state == Document.StorageState.NONE
    assert document.state == Document.State.REVIEW
    assert not document.invoices.exists()
    assert document.display_name == f"Manual entry #{document.pk}"
    page = client.get(f"/documents/{document.pk}/").content.decode()
    assert "Manual entry" in page and "no original PDF" in page
    assert client.get(f"/documents/{document.pk}/source/").status_code == 404
    assert client.get(f"/documents/{document.pk}/view/").status_code == 404


def test_manual_entry_is_approved_through_the_normal_review(configured, client):
    _, vendor, houses = configured
    document = create_manual(client)
    path, data = review_data(client, document)
    assert data["vendor"] == "" and data["invoice_total_minor"] == ""
    response = client.post(path, manual_fields(data, vendor, houses[0]))
    assert response.status_code == 302, response.content.decode()
    invoice = document.invoices.get()
    assert invoice.status == Invoice.Status.APPROVED
    assert invoice.reference == "SYNTHETIC-CREDIT-001"
    review = invoice.reviews.get()
    assert review.extraction is None
    assert review.snapshot["values"]["layout_family"] == "manual"
    document.refresh_from_db()
    assert document.state == Document.State.READY
    assert document.storage_state == Document.StorageState.NONE

    detail = client.get(f"/invoices/{invoice.pk}/").content.decode()
    assert "Manual entry" in detail and "View original PDF" not in detail
    listing = client.get("/invoices/").content.decode()
    assert "Manual entry" in listing

    report = read_report()
    assert report["manual_invoices"] == 1
    assert report["manual_document_ids"] == [document.pk]
    assert read_report(date_from=date(2025, 1, 1))["manual_invoices"] == 0
    assert read_report(date_to=date(2023, 12, 31))["manual_document_ids"] == []
    page = client.get("/reports/").content.decode()
    assert "1 approved invoice" in page and "0 from PDFs" in page and "1 manual" in page


def test_manual_entry_cannot_be_reparsed_or_touched_by_storage_recovery(configured, client):
    document = create_manual(client)
    response = client.post(
        f"/documents/{document.pk}/reparse/", {"extraction_revision": "0"}, follow=True
    )
    assert response.status_code == 200
    assert "Manual entries have no original PDF" in response.content.decode()
    document.refresh_from_db()
    assert document.extraction_revision == 0 and not document.extractions.exists()
    assert storage.recover_originals() == []
    document.refresh_from_db()
    assert document.storage_state == Document.StorageState.NONE
    assert document.state == Document.State.REVIEW


def test_manual_document_is_exempt_from_original_backup_checks(configured, tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "invoices"
    manual = Document.objects.create(
        source=Document.Source.MANUAL,
        state=Document.State.REVIEW,
        processing_message="Manual entry; no original PDF.",
    )
    configuration = tmp_path / "operator.env"
    configuration.write_text("HOUSE_BILLS_SECRET_KEY=synthetic-recovery-configuration\n")
    backup = tmp_path / "backup"
    backups.create_backup(backup, [configuration])
    backups.verify_backup(backup)
    with sqlite3.connect(backup / "database.sqlite3") as snapshot:
        row = snapshot.execute(
            "SELECT source, sha256, original_filename FROM bills_document WHERE id = ?",
            (manual.pk,),
        ).fetchone()
    assert row == ("manual", "", "")
    restored = tmp_path / "restored"
    backups.restore_backup(backup, restored)


def ledger_source(tmp_path, property_id, **overrides):
    row = {
        "property_id": property_id,
        "vendor": "Synthetic Utilities",
        "date": "2024-01-15",
        "reference": "",
        "sheet": "Synthetic",
        "total": 125.0,
        "services": {"electricity_supply": 125.0},
        "row": {"property_id": property_id, "date": "2024-01-15", "total": 125.0},
    }
    row.update(overrides)
    source = tmp_path / "comparison.json"
    source.write_text(json.dumps({"only_manual": [row]}), encoding="utf-8")
    return source


def import_arguments(source, actor, vendor, house, plan, commit):
    arguments = [
        "--source",
        str(source),
        "--actor",
        actor.username,
        "--vendor",
        f"Synthetic Utilities={vendor.slug}",
        "--property",
        f"{house.pk}={house.slug}",
        "--period",
        f"{vendor.slug}=10:30",
        "--plan",
        str(plan),
    ]
    return arguments + (["--commit"] if commit else [])


def test_ledger_import_dry_run_reports_without_writing(configured, tmp_path):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk)
    plan = tmp_path / "plan.json"
    output = StringIO()
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, commit=False),
        stdout=output,
    )
    assert not Document.objects.exists() and not Invoice.objects.exists()
    assert "WOULD Synthetic Utilities 2024-01-15" in output.getvalue()
    assert "64 rows" not in output.getvalue()
    artifact = json.loads(plan.read_text(encoding="utf-8"))
    assert artifact["mode"] == "dry-run"
    assert artifact["summary"] == {
        "imported": 1,
        "skipped": 0,
        "failed": 0,
        "gross_minor": 12500,
        "gross_total": "125.00",
    }
    row = artifact["rows"][0]
    assert row["status"] == "would import"
    assert row["reference"] == f"LEDGER-2024-01-15-P{houses[0].pk}"
    assert row["period_start"] == "2024-01-05" and row["period_end"] == "2024-02-04"
    assert row["net_minor"] == 10000 and row["vat_minor"] == 2500


def test_ledger_export_is_a_self_contained_live_input(configured, tmp_path):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk)
    live = tmp_path / "live.json"
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", False),
        "--export",
        str(live),
        stdout=StringIO(),
    )
    assert not Invoice.objects.exists()
    artifact = json.loads(live.read_text(encoding="utf-8"))
    assert artifact["format"] == "house-bills-ledger-import-input"
    assert "only_manual" not in artifact and len(artifact["rows"]) == 1
    row = artifact["rows"][0]
    assert row["vendor_slug"] == vendor.slug and row["property_slug"] == houses[0].slug
    assert (row["offset_days"], row["length_days"]) == (10, 30)
    assert row["reference"] == f"LEDGER-2024-01-15-P{houses[0].pk}"

    call_command(
        "import_ledger_invoices",
        "--source",
        str(live),
        "--actor",
        actor.username,
        "--commit",
        stdout=StringIO(),
    )
    invoice = Invoice.objects.get()
    section = invoice.sections.get()
    assert invoice.vendor == vendor and section.property == houses[0]
    assert (section.start.isoformat(), section.end.isoformat()) == ("2024-01-05", "2024-02-04")
    assert invoice.invoice_total_minor == 12500 and invoice.vat_minor == 2500


def test_ledger_import_commits_once_and_rereads_as_skip(configured, tmp_path):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, reference="SYNTHETIC-LEDGER-1")
    plan = tmp_path / "plan.json"
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, commit=True),
        stdout=StringIO(),
    )
    invoice = Invoice.objects.get()
    assert invoice.status == Invoice.Status.APPROVED
    assert invoice.reference == "SYNTHETIC-LEDGER-1"
    assert invoice.net_minor == 10000 and invoice.vat_minor == 2500
    assert invoice.invoice_total_minor == 12500
    section = invoice.sections.get()
    assert (section.start.isoformat(), section.end.isoformat()) == ("2024-01-05", "2024-02-04")
    assert section.property == houses[0]
    note = invoice.reviews.get().note
    assert "household ledger" in note and "VAT derived" in note and "period" in note
    document = invoice.document
    assert document.source == Document.Source.MANUAL and document.state == Document.State.READY

    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, commit=True),
        stdout=StringIO(),
    )
    again = json.loads(plan.read_text(encoding="utf-8"))
    assert again["summary"]["skipped"] == 1
    assert Invoice.objects.count() == 1
    assert Document.objects.count() == 1


def test_ledger_import_separates_same_date_invoices_by_total(configured, tmp_path):
    actor, vendor, houses = configured
    first = ledger_source(tmp_path, houses[0].pk, reference="SYNTHETIC-SUPPLY-A")
    call_command(
        "import_ledger_invoices",
        *import_arguments(
            source=first,
            actor=actor,
            vendor=vendor,
            house=houses[0],
            plan=tmp_path / "first.json",
            commit=True,
        ),
        stdout=StringIO(),
    )

    row = json.loads(first.read_text(encoding="utf-8"))["only_manual"][0]
    row.update(reference="SYNTHETIC-SUPPLY-B", total=100.0, services={"electricity_supply": 100.0})
    second = tmp_path / "second.json"
    second.write_text(json.dumps({"only_manual": [row]}), encoding="utf-8")
    call_command(
        "import_ledger_invoices",
        *import_arguments(
            source=second,
            actor=actor,
            vendor=vendor,
            house=houses[0],
            plan=tmp_path / "second-plan.json",
            commit=True,
        ),
        stdout=StringIO(),
    )
    assert Invoice.objects.filter(vendor=vendor).count() == 2

    row.update(reference="SYNTHETIC-SUPPLY-C")
    third = tmp_path / "third.json"
    third.write_text(json.dumps({"only_manual": [row]}), encoding="utf-8")
    call_command(
        "import_ledger_invoices",
        *import_arguments(
            source=third,
            actor=actor,
            vendor=vendor,
            house=houses[0],
            plan=tmp_path / "third-plan.json",
            commit=True,
        ),
        stdout=StringIO(),
    )
    artifact = json.loads((tmp_path / "third-plan.json").read_text(encoding="utf-8"))
    assert artifact["summary"]["skipped"] == 1
    assert Invoice.objects.filter(vendor=vendor).count() == 2


def test_ledger_import_refuses_services_that_do_not_reconcile(configured, tmp_path):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, total=200.0)
    errors = StringIO()
    with pytest.raises(CommandError):
        call_command(
            "import_ledger_invoices",
            *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", False),
            stdout=StringIO(),
            stderr=errors,
        )
    assert "differ from the ledger total" in errors.getvalue()
    assert not Invoice.objects.exists()


@pytest.mark.parametrize("failed_row_first", [True, False])
def test_ledger_import_failures_leave_no_documents_and_count_only_successes(
    configured, tmp_path, failed_row_first
):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, reference="SYNTHETIC-LEDGER-VALID")
    valid = json.loads(source.read_text(encoding="utf-8"))["only_manual"][0]
    invalid = {
        **valid,
        "date": "2024-01-16",
        "reference": "SYNTHETIC-LEDGER-INVALID",
        "total": 250.0,
        "services": {"unknown_service": 250.0},
    }
    rows = [invalid, valid] if failed_row_first else [valid, invalid]
    source.write_text(json.dumps({"only_manual": rows}), encoding="utf-8")
    plan = tmp_path / "plan.json"

    for attempt in range(2):
        with pytest.raises(CommandError, match="Some rows failed"):
            call_command(
                "import_ledger_invoices",
                *import_arguments(source, actor, vendor, houses[0], plan, commit=True),
                stdout=StringIO(),
                stderr=StringIO(),
            )
        artifact = json.loads(plan.read_text(encoding="utf-8"))
        assert artifact["summary"] == {
            "imported": 1 - attempt,
            "skipped": attempt,
            "failed": 1,
            "gross_minor": 12500 if attempt == 0 else 0,
            "gross_total": "125.00" if attempt == 0 else "0.00",
        }
        assert sorted(row["status"] for row in artifact["rows"]) == [
            "failed",
            "imported" if attempt == 0 else "skip",
        ]
        assert Document.objects.count() == Invoice.objects.count() == 1
        invoice = Invoice.objects.get()
        assert invoice.status == Invoice.Status.APPROVED
        assert invoice.reference == "SYNTHETIC-LEDGER-VALID"
        assert invoice.invoice_total_minor == 12500


def test_ledger_import_rolls_back_a_failure_after_approval(configured, tmp_path, monkeypatch):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk)
    approve = import_ledger_invoices.approve_document_review

    def fail_after_approval(*args, **kwargs):
        approve(*args, **kwargs)
        raise RuntimeError("Synthetic failure after approval.")

    monkeypatch.setattr(import_ledger_invoices, "approve_document_review", fail_after_approval)
    with pytest.raises(CommandError, match="Some rows failed"):
        call_command(
            "import_ledger_invoices",
            *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", True),
            stdout=StringIO(),
            stderr=StringIO(),
        )
    assert not Document.objects.exists()
    assert not Invoice.objects.exists()
    assert not BillingSection.objects.exists()
    assert not ReviewRevision.objects.exists()
