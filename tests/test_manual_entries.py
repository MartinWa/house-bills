import json
import sqlite3
from datetime import date, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError
from django.test import Client

from bills import backups, storage
from bills.configuration import add_months
from bills.invoice_coverage import read_invoice_coverage
from bills.management.commands import import_ledger_invoices
from bills.models import (
    BillingSection,
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    PropertyIdentifier,
    PropertyService,
    ReviewRevision,
    ServiceVendorAssignment,
    Vendor,
    VendorBillingSchedule,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.reporting import read_report

from .test_reporting import allocation, ledger  # noqa: F401
from .test_review import manual_fields, review_data

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def configured(tmp_path, settings, client, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "originals"
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


def test_cancelling_unfinished_manual_entry_deletes_the_placeholder(configured, client):
    document = create_manual(client)
    other = create_manual(client)
    detail = client.get(f"/documents/{document.pk}/").content.decode()
    assert "Delete document" in detail and "No invoice has been saved" in detail
    path, data = review_data(client, document)
    review_page = client.get(path).content.decode()
    delete_path = f"/documents/{document.pk}/cancel/"
    assert f'formaction="{delete_path}" formnovalidate>Cancel</button>' in review_page
    # Cancellation submits an incomplete form, including any unsaved edits.
    response = client.post(delete_path, {**data, "reference": "SYNTHETIC-UNSAVED"})
    assert response.status_code == 302 and response.url == "/documents/"
    assert not Document.objects.filter(pk=document.pk).exists()
    assert Document.objects.filter(pk=other.pk).exists()
    assert not Invoice.objects.exists() and not ReviewRevision.objects.exists()
    listing = client.get(response.url).content.decode()
    assert "Unfinished manual entry deleted." in listing
    assert f'href="/documents/{document.pk}/"' not in listing
    assert f'href="/documents/{other.pk}/"' in listing
    assert client.get(f"/documents/{document.pk}/").status_code == 404
    assert client.post(delete_path).status_code == 404
    # A review opened before deletion must not recreate the removed entry.
    _, vendor, houses = configured
    assert client.post(path, manual_fields(data, vendor, houses[0])).status_code == 404
    assert not Invoice.objects.exists()


def test_manual_cancellation_requires_login_csrf_and_post(configured, client):
    document = create_manual(client)
    delete_path = f"/documents/{document.pk}/cancel/"
    anonymous = Client()
    for method in (anonymous.get, anonymous.post):
        response = method(delete_path)
        assert response.status_code == 302 and response.url.startswith("/login/")
    secured = Client(enforce_csrf_checks=True)
    secured.force_login(configured[0])
    assert secured.get(delete_path).status_code == 405
    assert secured.post(delete_path).status_code == 403
    assert Document.objects.filter(pk=document.pk).exists()
    secured.get(f"/documents/{document.pk}/")
    response = secured.post(
        delete_path, {"csrfmiddlewaretoken": secured.cookies["csrftoken"].value}
    )
    assert response.status_code == 302 and response.url == "/documents/"
    assert not Document.objects.filter(pk=document.pk).exists()


@pytest.mark.parametrize("state", [Document.State.READY, Document.State.REVIEW])
def test_stale_cancel_action_retains_approved_manual_invoice(configured, client, state):
    _, vendor, houses = configured
    document = create_manual(client)
    delete_path = f"/documents/{document.pk}/cancel/"
    assert "Delete document" in client.get(f"/documents/{document.pk}/").content.decode()
    path, data = review_data(client, document)
    assert client.post(path, manual_fields(data, vendor, houses[0])).status_code == 302
    invoice = document.invoices.get()
    snapshot = invoice.reviews.get().snapshot
    report = read_report()
    # A document can need attention again while retaining its saved approval.
    Document.objects.filter(pk=document.pk).update(state=state)
    response = client.post(delete_path, follow=True)
    assert response.status_code == 200
    page = response.content.decode()
    assert "Only unfinished manual entries can be cancelled." in page
    assert "Delete document" in page
    assert Document.objects.filter(pk=document.pk).exists()
    invoice.refresh_from_db()
    assert invoice.status == Invoice.Status.APPROVED and invoice.review_revision == 1
    assert invoice.reviews.get().snapshot == snapshot
    assert read_report() == report
    assert client.get(path).content.decode().count("formaction=") == 0


@pytest.mark.parametrize("kind", ["upload", "invoice", "extraction"])
def test_manual_cancellation_retains_uploaded_and_saved_records(configured, client, kind):
    if kind == "upload":
        document = Document.objects.create(
            sha256="a" * 64, original_filename="synthetic.pdf", state=Document.State.REVIEW
        )
    else:
        document = create_manual(client)
        if kind == "invoice":
            Invoice.objects.create(
                document=document, vendor=configured[1], reference="SYNTHETIC-DRAFT-001"
            )
        else:
            ExtractionAttempt.objects.create(
                document=document, revision=1, state=ExtractionAttempt.State.FAILED
            )
    counts = (Document.objects.count(), Invoice.objects.count(), ExtractionAttempt.objects.count())
    detail = client.get(f"/documents/{document.pk}/").content.decode()
    assert "Delete document" in detail
    response = client.post(f"/documents/{document.pk}/cancel/", follow=True)
    assert response.status_code == 200
    assert "Only unfinished manual entries can be cancelled." in response.content.decode()
    assert Document.objects.filter(pk=document.pk).exists()
    assert counts == (
        Document.objects.count(),
        Invoice.objects.count(),
        ExtractionAttempt.objects.count(),
    )


def test_failed_cancellation_rolls_back_and_keeps_the_entry(configured, client, monkeypatch):
    document = create_manual(client)
    original_delete = Document.delete

    def fail_after_delete(instance, *args, **kwargs):
        original_delete(instance, *args, **kwargs)
        raise DatabaseError("Synthetic failure after deleting the row")

    monkeypatch.setattr(Document, "delete", fail_after_delete)
    response = client.post(f"/documents/{document.pk}/cancel/", follow=True)
    assert response.status_code == 200
    assert "The entry was retained; try again." in response.content.decode()
    assert Document.objects.filter(pk=document.pk).exists()
    assert "Delete document" in response.content.decode()


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


@pytest.mark.parametrize("quantity", [None, 0, 12000])
@pytest.mark.parametrize("payment", [False, True])
def test_exact_ledger_waste_costs_retain_optional_partial_and_zero_weights(
    configured, tmp_path, quantity, payment
):
    actor, vendor, houses = configured
    reported = (
        []
        if quantity is None
        else [
            {
                "utility": "waste",
                "subcategory": "matavfall",
                "unit": "kg",
                "quantity_milli": quantity,
                "basis": "billed_weight",
                "start": "2024-01-01",
                "end": "2024-02-01",
            }
        ]
    )
    allocations = [
        {
            "section_id": "combined",
            "category": "waste",
            "subcategory": "waste_collection",
            "net_minor": 16000,
            "vat_minor": 4000,
            "gross_minor": 20000,
            "start": "2024-01-01",
            "end": "2024-02-01",
            "usages": reported,
        },
        {
            "section_id": "garden",
            "category": "waste",
            "subcategory": "tradgardsavfall",
            "net_minor": 4000,
            "vat_minor": 1000,
            "gross_minor": 5000,
            "start": "2024-01-01",
            "end": "2024-02-01",
        },
    ]
    source = ledger_source(
        tmp_path,
        houses[0].pk,
        services={},
        total=250.0,
        allocations=allocations,
        **(
            {"date": "2024-04-02", "date_semantics": "payment", "period_mode": "calendar"}
            if payment
            else {}
        ),
    )
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", commit=True),
        stdout=StringIO(),
    )
    invoice = Invoice.objects.get()
    assert invoice.invoice_date == (date(2024, 3, 1) if payment else date(2024, 1, 15))
    assert invoice.sections.get(section_id="combined").start == date(2024, 1, 1)
    assert invoice.sections.get(section_id="combined").gross_minor == 20000
    assert invoice.sections.get(section_id="garden").gross_minor == 5000
    weights = list(invoice.sections.get(section_id="combined").usages.all())
    assert len(weights) == int(quantity is not None)
    if quantity is not None:
        assert weights[0].quantity_milli == quantity and weights[0].subcategory_id == "matavfall"
    assert invoice.sections.get(section_id="garden").usages.count() == 0
    report = read_report()
    assert report["waste_total"]["gross_minor"] == 25000
    assert report["waste_general"][0]["cost"]["gross_minor"] == 20000


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
    assert row["invoice_date"] == "2024-01-15" and row["due_date"] is None
    assert artifact["date_semantics"] == {vendor.slug: "invoice"}
    assert artifact["period_modes"] == {vendor.slug: "offset"}


@pytest.mark.parametrize(
    ("payment_date", "invoice_date", "start", "end"),
    [
        ("2017-04-03", "2017-03-01", "2017-02-01", "2017-03-01"),
        ("2017-05-02", "2017-04-01", "2017-03-01", "2017-04-01"),
        ("2018-10-31", "2018-10-01", "2018-09-01", "2018-10-01"),
        ("2019-07-31", "2019-07-01", "2019-06-01", "2019-07-01"),
        ("2024-03-24", "2024-02-01", "2024-01-01", "2024-02-01"),
        ("2024-03-25", "2024-03-01", "2024-02-01", "2024-03-01"),
        ("2024-01-02", "2023-12-01", "2023-11-01", "2023-12-01"),
    ],
)
def test_payment_calendar_import_derives_months_and_preserves_provenance(
    configured, tmp_path, payment_date, invoice_date, start, end
):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, date=payment_date)
    plan = tmp_path / "plan.json"
    output = StringIO()
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, True),
        "--date-semantics",
        f"{vendor.slug}=payment",
        "--period-mode",
        "Synthetic Utilities=calendar",
        stdout=output,
    )
    invoice = Invoice.objects.get()
    assert invoice.invoice_date.isoformat() == invoice_date and invoice.due_date is None
    section = invoice.sections.get()
    assert (section.start.isoformat(), section.end.isoformat()) == (start, end)
    assert invoice.invoice_total_minor == 12500 and section.usages.count() == 0
    revision = invoice.reviews.get()
    assert f"Ledger date {payment_date} is a payment date" in revision.note
    assert f"Invoice date {invoice_date} is derived" in revision.note
    assert "first day anchors" in revision.note
    assert "Original invoice unavailable" in revision.note
    assert revision.snapshot["values"]["due_date"] is None
    artifact = json.loads(plan.read_text(encoding="utf-8"))
    assert artifact["rows"][0]["payment_date"] == payment_date
    assert artifact["rows"][0]["invoice_date"] == invoice_date
    assert artifact["date_semantics"] == {vendor.slug: "payment"}
    assert f"DATES {vendor.slug}: payment, period mode calendar" in output.getvalue()


def test_payment_calendar_sequence_has_contiguous_coverage_and_is_idempotent(configured, tmp_path):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk)
    template = json.loads(source.read_text(encoding="utf-8"))["only_manual"][0]
    starts = [add_months(date(2017, 2, 1), index) for index in range(29)]
    rows = []
    for index, start in enumerate(starts):
        payment = add_months(start, 2).replace(day=2)
        if index in (20, 27, 28):
            payment = payment.replace(day=1) - timedelta(days=1)
        rows.append({**template, "date": payment.isoformat(), "reference": f"SYNTHETIC-{index}"})
    source.write_text(json.dumps({"only_manual": rows}), encoding="utf-8")
    service = PropertyService.objects.create(
        property=houses[0],
        category="electricity",
        subcategory="electricity_supply",
        start_month=date(2017, 3, 1),
        end_month=date(2019, 7, 1),
    )
    ServiceVendorAssignment.objects.create(
        property_service=service,
        vendor=vendor,
        start_month=service.start_month,
        end_month=service.end_month,
    )
    VendorBillingSchedule.objects.create(
        vendor=vendor,
        start_month=service.start_month,
        end_month=service.end_month,
        first_expected_month=service.start_month,
    )
    arguments = import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", True)
    arguments += [
        "--date-semantics",
        f"{vendor.slug}=payment",
        "--period-mode",
        f"{vendor.slug}=calendar",
    ]
    for _ in range(2):
        call_command("import_ledger_invoices", *arguments, stdout=StringIO())
    assert Invoice.objects.count() == ReviewRevision.objects.count() == 29
    assert list(BillingSection.objects.order_by("start").values_list("start", flat=True)) == starts
    assert list(
        Invoice.objects.order_by("invoice_date").values_list("invoice_date", flat=True)
    ) == [add_months(start, 1) for start in starts]
    assert sum(Invoice.objects.values_list("invoice_total_minor", flat=True)) == 29 * 12500
    coverage = read_invoice_coverage(
        date_from=service.start_month, date_to=service.end_month, as_of=date(2024, 1, 1)
    )
    assert coverage["received_count"] == len(coverage["coverage_rows"]) == 29
    assert coverage["missing_count"] == 0


@pytest.mark.parametrize("due_date", [None, "2017-04-04"])
def test_payment_export_round_trip_freezes_semantics_and_retains_explicit_due_date(
    configured, tmp_path, due_date
):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, date="2017-04-03", due_date=due_date)
    live = tmp_path / "live.json"
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", False),
        "--date-semantics",
        f"{vendor.slug}=payment",
        "--period-mode",
        f"{vendor.slug}=calendar",
        "--export",
        str(live),
        stdout=StringIO(),
    )
    assert not Invoice.objects.exists()
    source.unlink()
    row = json.loads(live.read_text(encoding="utf-8"))["rows"][0]
    assert row["date"] == "2017-04-03"
    assert row["date_semantics"] == "payment" and row["period_mode"] == "calendar"
    assert row["due_date"] == due_date
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
    assert invoice.invoice_date == date(2017, 3, 1)
    assert invoice.due_date == (date.fromisoformat(due_date) if due_date else None)
    assert invoice.sections.get().start == date(2017, 2, 1)
    assert "Ledger date 2017-04-03 is a payment date" in invoice.reviews.get().note


def test_date_semantics_apply_per_vendor_and_calendar_preserves_invoice_dates(configured, tmp_path):
    actor, vendor, houses = configured
    other = Vendor.objects.create(name="Synthetic Early Issuer", slug="synthetic-early-issuer")
    source = ledger_source(tmp_path, houses[0].pk, date="2024-04-02")
    row = json.loads(source.read_text(encoding="utf-8"))["only_manual"][0]
    source.write_text(
        json.dumps({"only_manual": [row, {**row, "vendor": other.name}]}), encoding="utf-8"
    )
    plan = tmp_path / "plan.json"
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, False),
        "--date-semantics",
        f"{vendor.slug}=payment",
        "--period-mode",
        f"{vendor.slug}=calendar",
        stdout=StringIO(),
    )
    rows = json.loads(plan.read_text(encoding="utf-8"))["rows"]
    assert rows[0]["invoice_date"] == "2024-03-01"
    assert rows[1]["invoice_date"] == "2024-04-02"
    assert rows[1]["date_semantics"] == "invoice" and rows[1]["period_mode"] == "offset"
    call_command(
        "import_ledger_invoices",
        *import_arguments(source, actor, vendor, houses[0], plan, False),
        "--period-mode",
        f"{other.slug}=calendar",
        stdout=StringIO(),
    )
    rows = json.loads(plan.read_text(encoding="utf-8"))["rows"]
    assert rows[1]["invoice_date"] == "2024-04-02"
    assert (rows[1]["period_start"], rows[1]["period_end"]) == ("2024-03-01", "2024-04-01")


def test_payment_offset_uses_originals_and_export_pins_observed_intervals(
    ledger,  # noqa: F811 - Imported pytest fixture.
    tmp_path,
    monkeypatch,
):
    actor, house = ledger["actor"], ledger["houses"][0]
    for index, interval in enumerate((20, 21, 22, 1, 1, 1, 1)):
        original = index < 3
        invoice = ledger["make"](
            [allocation(house, start=date(2024, 2, 1), end=date(2024, 3, 2))],
            invoice_date=date(2024, 3, 13) if original else date(2024, 2, 2),
            source=Document.Source.UPLOAD if original else Document.Source.MANUAL,
        )
        candidate = ledger["candidates"][invoice.pk]
        candidate.due_date = candidate.invoice_date + timedelta(days=interval)
        ledger["correct"](invoice, candidate)
    source = ledger_source(
        tmp_path,
        house.pk,
        date="2024-04-03",
        date_semantics="payment",
        total=250,
        services={"electricity_supply": 250},
    )
    live = tmp_path / "live.json"
    plan = tmp_path / "plan.json"
    # No --period override: both intervals must come from the approved originals.
    arguments = ["--source", str(source), "--actor", actor.username]
    call_command(
        "import_ledger_invoices",
        *arguments,
        "--export",
        str(live),
        "--plan",
        str(plan),
        stdout=StringIO(),
    )
    row = json.loads(plan.read_text(encoding="utf-8"))["rows"][0]
    assert row["payment_offset_days"] == 21
    assert row["invoice_date"] == "2024-03-13"
    assert (row["period_start"], row["period_end"]) == ("2024-02-01", "2024-03-02")
    assert json.loads(live.read_text(encoding="utf-8"))["rows"][0]["payment_offset_days"] == 21
    # Repeat with just the exported file, without any original samples.
    monkeypatch.setattr(import_ledger_invoices.Command, "payment_samples", lambda *_: [])
    monkeypatch.setattr(import_ledger_invoices.Command, "period_samples", lambda *_, **__: [])
    call_command(
        "import_ledger_invoices",
        "--source",
        str(live),
        "--actor",
        actor.username,
        "--commit",
        stdout=StringIO(),
    )
    invoice = Invoice.objects.get(reference=f"LEDGER-2024-04-03-P{house.pk}")
    assert invoice.invoice_date == date(2024, 3, 13) and invoice.due_date is None
    assert invoice.sections.get().start == date(2024, 2, 1)
    assert "21-day invoice-to-due interval" in invoice.reviews.get().note


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"date_semantics": "payments"}, "date_semantics must be"),
        ({"period_mode": "monthly"}, "period_mode must be"),
        ({"date_semantics": "payment"}, "need approved original invoices"),
        ({"date_semantics": "payment", "payment_offset_days": True}, "payment_offset_days"),
        ({"date_semantics": "payment", "payment_offset_days": 0}, "payment_offset_days"),
        ({"date_semantics": "payment", "payment_offset_days": 91}, "payment_offset_days"),
    ],
)
def test_ledger_date_semantics_failures_are_audited_without_writes(
    configured, tmp_path, fields, message
):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk, **fields)
    errors = StringIO()
    with pytest.raises(CommandError, match="Some rows failed"):
        call_command(
            "import_ledger_invoices",
            *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", True),
            stdout=StringIO(),
            stderr=errors,
        )
    assert message in errors.getvalue()
    assert not Invoice.objects.exists() and not Document.objects.exists()


@pytest.mark.parametrize("option", ["--date-semantics", "--period-mode"])
def test_ledger_date_options_reject_unknown_choices(configured, tmp_path, option):
    actor, vendor, houses = configured
    source = ledger_source(tmp_path, houses[0].pk)
    with pytest.raises(CommandError, match=option):
        call_command(
            "import_ledger_invoices",
            *import_arguments(source, actor, vendor, houses[0], tmp_path / "plan.json", True),
            option,
            f"{vendor.slug}=unknown",
            stdout=StringIO(),
        )
    assert not Invoice.objects.exists()


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
