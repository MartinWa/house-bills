import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import date
from threading import Barrier
from urllib.parse import urlencode

import pytest
from django.core import signing
from django.core.exceptions import RequestDataTooBig, TooManyFieldsSent, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client, RequestFactory

from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Property,
    PropertyIdentifier,
    ReviewRevision,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.presentation import warning_details
from bills.review_forms import IdentifierFormSet, ScaledNumberField, SectionFormSet, UsageFormSet
from bills.reviewing import current_review_context
from bills.storage import private_path

from .synthetic import multi_service, shared_supply, write_pdf

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def configured(settings, tmp_path, client, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "originals"
    actor = django_user_model.objects.create_user("synthetic-reviewer")
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
    for key in ("9901", "9902", "9903", "9904"):
        PropertyIdentifier.objects.create(
            vendor=vendor, property=houses[0], kind="contract", value=key
        )
    return actor, vendor, houses


def upload(client, tmp_path, pages=None, *, filename="synthetic.pdf"):
    pages = copy.deepcopy(shared_supply() if pages is None else pages)
    pages[0].append("VAT-nr SE000000999901")
    pdf = write_pdf(tmp_path / filename, pages).read_bytes()
    response = client.post("/upload/", {"pdf": SimpleUploadedFile(filename, pdf)})
    assert response.status_code == 302
    document = Document.objects.get(pk=int(response.url.split("/")[2]))
    return document, pdf


def posted_fields(response):
    review = response.context["review"]
    result = {}

    def include(form):
        for field in form:
            value = field.value()
            if value is True:
                value = "on"
            elif value is False or value is None:
                value = ""
            elif hasattr(value, "isoformat"):
                value = value.isoformat()
            result[field.html_name] = str(value)

    include(review.header)
    for _, _, group in review.groups:
        include(group.management_form)
        for form in group:
            include(form)
    return result


def review_data(client, document, *, proposal=False):
    path = f"/documents/{document.pk}/review/"
    response = client.get(path + ("?proposal=latest" if proposal else ""))
    assert response.status_code == 200
    return path, posted_fields(response)


def approved(client, document):
    path, data = review_data(client, document)
    response = client.post(path, data)
    assert response.status_code == 302, response.content.decode()
    return document.invoices.get()


def test_product_numbers_map_independently_and_survive_approval(client, configured, tmp_path):
    _, vendor, houses = configured
    PropertyIdentifier.objects.create(
        vendor=vendor, property=houses[1], kind="product", value="9901"
    )
    pages = multi_service()
    pages[2] = [
        line.replace("Vatten avtal 9901", "Vatten & Avlopp prodnr 9901")
        if isinstance(line, str)
        else line
        for line in pages[2]
    ]
    document, _ = upload(client, tmp_path, pages)
    candidate = document.extractions.get().candidate
    assert not candidate["issues"]
    assert candidate["routing"]["section_properties"]["section_01"] == houses[1].pk
    path = f"/documents/{document.pk}/review/"
    response = client.get(path)
    product_form = next(
        form for form in response.context["review"].identifiers if form["kind"].value() == "product"
    )
    assert 'value="product" selected' in str(product_form["kind"])
    invoice = approved(client, document)
    water = invoice.sections.get(service="water")
    assert water.property == houses[1]
    assert water.identifiers[0] == candidate["sections"][0]["identifiers"][0]
    assert water.identifiers[0]["kind"] == "product"


def test_document_shows_manual_approval_and_archives_failed_extraction(
    client, configured, tmp_path
):
    _, vendor, houses = configured
    VendorIssuerIdentifier.objects.filter(vendor=vendor).delete()
    document, _ = upload(client, tmp_path, [["Unsupported synthetic invoice"]])
    attempt = document.extractions.get()
    original = copy.deepcopy(attempt.candidate)
    path, data = review_data(client, document)
    data = manual_fields(data, vendor, houses[0])
    assert client.post(path, data).status_code == 302
    invoice = document.invoices.get()
    snapshot = copy.deepcopy(invoice.reviews.get().snapshot)

    response = client.get(f"/documents/{document.pk}/")
    page = response.content.decode()
    current, separator, history = page.partition('<details class="extraction-history">')
    assert response.status_code == 200 and separator
    assert "Approved invoice" in current and "Synthetic Utilities" in current
    assert "SYNTHETIC-CREDIT-001" in current and "-125,00 SEK" in current
    assert "House 1" in current and "2024-02-29" in current
    assert "Unknown vendor" not in current and "Needs attention" not in current
    assert "Review is required before costs and usage are approved." not in page
    assert "Extraction history" in history and "Extraction warnings" in history
    assert "Unknown vendor" in history and "No billing sections could be extracted." in history
    assert response.context["proposal_reviewed"] and not response.context["differences"]
    attempt.refresh_from_db()
    assert attempt.candidate == original
    assert invoice.reviews.get().snapshot == snapshot


def test_document_keeps_new_failed_extraction_visible_until_reviewed(client, configured, tmp_path):
    from bills.services import record_extraction

    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    snapshot = copy.deepcopy(invoice.reviews.get().snapshot)
    attempt = record_extraction(document.pk, error="Synthetic re-parsing failure")
    document_path = f"/documents/{document.pk}/"
    response = client.get(document_path)
    page = response.content.decode()
    assert response.status_code == 200 and not response.context["proposal_reviewed"]
    assert "Approved invoice" in page and "447,00 SEK" in page
    assert "Extracted proposal" in page and "Needs attention" in page
    assert "Synthetic re-parsing failure" in page
    assert '<details class="extraction-history">' not in page
    assert invoice.reviews.get().snapshot == snapshot

    path, data = review_data(client, document)
    assert client.post(path, data).status_code == 302
    response = client.get(document_path)
    assert response.context["proposal_reviewed"] and not response.context["differences"]
    assert b"Synthetic re-parsing failure" in response.content
    assert b"Needs attention" not in response.content
    assert document.extractions.count() == 2
    attempt.refresh_from_db()
    assert attempt.error == "Synthetic re-parsing failure"
    invoice.refresh_from_db()
    assert invoice.review_revision == 2 and invoice.invoice_total_minor == 44700


def test_waste_subcategories_survive_review_correction_and_proposal_comparison(
    configured, client, tmp_path
):
    document, _ = upload(client, tmp_path, multi_service())
    path, data = review_data(client, document)
    assert [data[f"usages-{i}-subcategory"] for i in range(4)] == [
        "",
        "matavfall",
        "restavfall",
        "",
    ]
    invoice = approved(client, document)
    original = copy.deepcopy(invoice.reviews.get(revision=1).snapshot)
    page = client.get(f"/invoices/{invoice.pk}/").content.decode()
    assert "Restavfall" in page and "Matavfall" in page and "Electricity › Elnät" in page
    _, data = review_data(client, document)
    data["usages-1-subcategory"] = "restavfall"
    assert client.post(path, data).status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 2
    assert invoice.reviews.get(revision=1).snapshot == original
    assert list(
        invoice.sections.get(section_id="section_02").usages.values_list(
            "subcategory", "quantity_milli"
        )
    ) == [("restavfall", 12000), ("restavfall", 7500)]
    response = client.get(f"/documents/{document.pk}/")
    assert response.context["differences"] == []
    document.refresh_from_db()
    assert (
        client.post(
            f"/documents/{document.pk}/reparse/",
            {"extraction_revision": document.extraction_revision},
        ).status_code
        == 302
    )
    response = client.get(f"/documents/{document.pk}/")
    assert any(
        row["field"].endswith("subcategory")
        and row["approved"] == "Restavfall"
        and row["proposed"] == "Matavfall"
        for row in response.context["differences"]
    )


def test_legacy_waste_review_infers_subcategories_and_retains_original_evidence(
    configured, client, tmp_path
):
    document, _ = upload(client, tmp_path, multi_service())
    attempt = document.extractions.get()
    legacy = copy.deepcopy(attempt.candidate)
    for section in legacy["sections"]:
        for row in section["usages"]:
            row.pop("subcategory")
    type(attempt).objects.filter(pk=attempt.pk).update(candidate=legacy)
    _, data = review_data(client, document)
    assert data["usages-1-subcategory"] == "matavfall"
    assert data["usages-2-subcategory"] == "restavfall"
    invoice = approved(client, document)
    rows = invoice.reviews.get().snapshot["values"]["sections"][1]["usages"]
    assert [row["subcategory"] for row in rows] == ["matavfall", "restavfall"]
    assert [row["evidence"] for row in rows] == [
        row["evidence"] for row in legacy["sections"][1]["usages"]
    ]
    attempt.refresh_from_db()
    assert attempt.candidate == legacy


def test_waste_subcategory_rejected_on_electricity_without_changing_approval(
    configured, client, tmp_path
):
    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    path, data = review_data(client, document)
    response = client.post(path, {**data, "usages-0-subcategory": "matavfall"})
    assert response.status_code == 400
    assert response.context["review"].usages[0].errors["subcategory"]
    invoice.refresh_from_db()
    assert invoice.review_revision == 1


def manual_fields(data, vendor, house):
    return {
        **data,
        "vendor": str(vendor.pk),
        "reference": "SYNTHETIC-CREDIT-001",
        "kind": "credit",
        "invoice_date": "2024-03-05",
        "due_date": "",
        "currency": "SEK",
        "net_minor": "-100,00",
        "vat_minor": "-25,00",
        "rounding_minor": "0,00",
        "invoice_total_minor": "-125,00",
        "amount_due_minor": "-125,00",
        "page": "1",
        "warnings_confirmed": "on",
        "sections-0-property": str(house.pk),
        "sections-0-service": "water",
        "sections-0-net_minor": "-100,00",
        "sections-0-vat_minor": "-25,00",
        "sections-0-gross_minor": "-125,00",
        "sections-0-start": "2024-02-29",
        "sections-0-end": "2024-02-29",
        "note": "Manually entered from the unsupported synthetic source.",
    }


def full_review_fields(data, vendor, house):
    data = manual_fields(data, vendor, house)
    data.update({"reference": "🧾" * 120, "note": "🧾" * 2000, "csrfmiddlewaretoken": "x" * 64})
    section_ids = ["🧾" * 117 + f"{index:03}" for index in range(SectionFormSet.absolute_max)]
    for index, section_id in enumerate(section_ids):
        row = {
            field: data[f"sections-0-{field}"]
            for field in SectionFormSet.form.base_fields
            if field != "DELETE"
        }
        row["section_id"] = section_id
        if index:
            row.update(net_minor="0,00", vat_minor="0,00", gross_minor="0,00")
        data.update({f"sections-{index}-{field}": value for field, value in row.items()})
        data[f"sections-{index}-DELETE"] = ""
    for index in range(UsageFormSet.absolute_max):
        row = {
            "section": section_ids[index % len(section_ids)],
            "utility": "water",
            "subcategory": "",
            "unit": "m3",
            "quantity_milli": "-1,250",
            "start": "2024-02-29",
            "end": "2024-02-29",
            "basis": "billed",
            "page": "1",
            "DELETE": "",
        }
        data.update({f"usages-{index}-{field}": value for field, value in row.items()})
    for index in range(IdentifierFormSet.absolute_max):
        row = {
            "scope": section_ids[index % len(section_ids)],
            "kind": "delivery_address",
            "value": "🧾" * 197 + f"{index:03}",
            "page": "1",
            "DELETE": "",
        }
        data.update({f"identifiers-{index}-{field}": value for field, value in row.items()})
    for prefix, formset in (
        ("sections", SectionFormSet),
        ("usages", UsageFormSet),
        ("identifiers", IdentifierFormSet),
    ):
        data[f"{prefix}-TOTAL_FORMS"] = str(formset.absolute_max)
        data[f"{prefix}-MAX_NUM_FORMS"] = str(formset.absolute_max)
    return data


@pytest.mark.parametrize("encoding", ["urlencoded", "multipart"])
def test_full_review_saves_and_retains_entries_after_validation_errors(
    configured, client, tmp_path, settings, encoding
):
    _, vendor, houses = configured
    document, _ = upload(client, tmp_path, [["Unsupported synthetic invoice"]])
    path, initial = review_data(client, document)
    data = full_review_fields(initial, vendor, houses[0])
    assert 1000 < len(data) <= settings.DATA_UPLOAD_MAX_NUMBER_FIELDS
    assert 64 * 1024 < len(urlencode(data).encode()) <= settings.DATA_UPLOAD_MAX_MEMORY_SIZE

    def submit(values):
        if encoding == "urlencoded":
            return client.post(
                path, urlencode(values), content_type="application/x-www-form-urlencoded"
            )
        return client.post(path, values)

    invalid = submit({**data, "invoice_date": "invalid"})
    assert invalid.status_code == 400 and b"Your entries are retained below" in invalid.content
    retained = posted_fields(invalid)
    assert all(
        retained[key] == value
        for key, value in {**data, "invoice_date": "invalid"}.items()
        if key in retained
    )
    assert not document.invoices.exists()

    saved = submit(data)
    assert saved.status_code == 302, saved.content.decode()
    invoice = document.invoices.get()
    assert invoice.sections.count() == SectionFormSet.absolute_max
    assert Usage.objects.count() == UsageFormSet.absolute_max
    assert sum(len(section.identifiers) for section in invoice.sections.all()) == (
        IdentifierFormSet.absolute_max
    )
    assert invoice.reviews.get().note == data["note"]
    _, correction = review_data(client, document)
    assert submit(correction).status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 2


@pytest.mark.parametrize("prefix", ["sections", "usages", "identifiers"])
def test_review_rejects_one_row_over_each_limit_and_retains_entries(
    configured, client, tmp_path, prefix
):
    _, vendor, houses = configured
    document, _ = upload(client, tmp_path, [["Unsupported synthetic invoice"]])
    path, initial = review_data(client, document)
    data = full_review_fields(initial, vendor, houses[0])
    limit = int(data[f"{prefix}-TOTAL_FORMS"])
    data[f"{prefix}-TOTAL_FORMS"] = str(limit + 1)
    response = client.post(path, urlencode(data), content_type="application/x-www-form-urlencoded")
    assert response.status_code == 400
    assert f"at most {limit} forms".encode() in response.content
    assert b"Your entries are retained below" in response.content
    assert posted_fields(response)["note"] == data["note"]
    assert not document.invoices.exists()


def test_request_body_size_boundary(settings):
    factory = RequestFactory()
    for extra in (0, 1):
        request = factory.post(
            "/",
            "note=" + "x" * (settings.DATA_UPLOAD_MAX_MEMORY_SIZE - 5 + extra),
            content_type="application/x-www-form-urlencoded",
        )
        if extra:
            with pytest.raises(RequestDataTooBig):
                _ = request.POST
        else:
            assert len(request.POST["note"]) == settings.DATA_UPLOAD_MAX_MEMORY_SIZE - 5


@pytest.mark.parametrize("encoding", ["urlencoded", "multipart"])
def test_request_field_count_boundary(settings, encoding):
    factory = RequestFactory()
    for extra in (0, 1):
        data = {
            f"field-{index}": "x" for index in range(settings.DATA_UPLOAD_MAX_NUMBER_FIELDS + extra)
        }
        request = (
            factory.post("/", urlencode(data), content_type="application/x-www-form-urlencoded")
            if encoding == "urlencoded"
            else factory.post("/", data)
        )
        if extra:
            with pytest.raises(TooManyFieldsSent):
                _ = request.POST
        else:
            assert len(request.POST) == settings.DATA_UPLOAD_MAX_NUMBER_FIELDS


def test_supported_upload_approval_list_detail_and_private_original(client, configured, tmp_path):
    actor, _, houses = configured
    document, pdf = upload(client, tmp_path)
    invoice = approved(client, document)
    assert invoice.status == "approved" and invoice.review_revision == 1
    assert invoice.approved_by == actor and invoice.invoice_total_minor == 44700
    assert set(invoice.sections.values_list("property_id", flat=True)) == {h.pk for h in houses}
    assert list(Usage.objects.order_by("pk").values_list("quantity_milli", flat=True)) == [
        140250,
        510000,
    ]
    assert all(
        (section.start, section.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for section in invoice.sections.all()
    )
    assert all(
        "charges" not in section for section in invoice.reviews.get().snapshot["values"]["sections"]
    )
    document.refresh_from_db()
    assert document.state == "ready" and document.storage_state == "approved"
    assert document.original_path.startswith("shared/")
    assert private_path(document.original_path).read_bytes() == pdf
    detail = client.get(f"/invoices/{invoice.pk}/").content.decode()
    for text in ("House 1", "House 2", "447,00 SEK", "140,250 kWh", "2024-02-29"):
        assert text in detail
    assert f'href="/documents/{document.pk}/"' in detail
    assert f'href="/documents/{document.pk}/review/"' not in detail
    assert f'href="/documents/{document.pk}/view/"' not in detail
    assert f'href="/documents/{document.pk}/source/"' not in detail
    for house in houses:
        listing = client.get("/invoices/", {"property": house.pk, "service": "electricity_supply"})
        assert listing.status_code == 200 and listing.context["page"].paginator.count == 1
        assert b"880003" in listing.content
    assert client.get("/invoices/", {"service": "water"}).context["page"].paginator.count == 0
    assert client.get("/invoices/", {"property": "999999"}).context["form"].errors
    for route, disposition in (("source", "attachment"), ("view", "inline")):
        response = client.get(f"/documents/{document.pk}/{route}/")
        assert response.status_code == 200 and b"".join(response.streaming_content) == pdf
        assert response["Content-Disposition"].startswith(disposition)
        assert "no-store" in response["Cache-Control"]


def test_review_and_approval_keep_multiple_network_readings_without_a_false_warning(
    client, configured, tmp_path
):
    document, pdf = upload(client, tmp_path, multi_service(split_network_readings=True))
    path = f"/documents/{document.pk}/review/"
    response = client.get(path)
    assert response.status_code == 200 and response.context["warning_details"] == []
    assert b"Extraction warnings" not in response.content
    assert (
        len(
            [
                row
                for row in response.context["review"].usages
                if row["section"].value() == "section_04"
            ]
        )
        == 2
    )
    invoice = approved(client, document)
    network = invoice.sections.get(section_id="section_04")
    assert (network.net_minor, network.vat_minor, network.gross_minor) == (37846, 9462, 47308)
    assert list(network.usages.order_by("start").values_list("quantity_milli", "start", "end")) == [
        (121125, date(2024, 2, 1), date(2024, 2, 15)),
        (200000, date(2024, 2, 15), date(2024, 3, 1)),
    ]
    document.refresh_from_db()
    assert private_path(document.original_path).read_bytes() == pdf


def test_unsupported_manual_entry_with_signed_usage_identifiers_and_balance(
    client, configured, tmp_path
):
    _, vendor, houses = configured
    document, pdf = upload(client, tmp_path, [["Unfamiliar synthetic invoice"]])
    assert not document.invoices.exists()
    path, data = review_data(client, document)
    client.get(path)
    assert not document.invoices.exists()  # Opening/reloading the form performs no write.
    data = manual_fields(data, vendor, houses[0])
    added = client.post(path, {**data, "add_row": "usages"})
    assert added.status_code == 200 and not document.invoices.exists()
    data = posted_fields(added)
    data.update(
        {
            "usages-0-utility": "water",
            "usages-0-unit": "m3",
            "usages-0-quantity_milli": "−1,250",
            "usages-0-start": "2024-02-29",
            "usages-0-end": "2024-02-29",
            "usages-0-basis": "reversal",
            "amount_due_minor": "-115,00",
            "amount_due_confirmed": "on",
        }
    )
    added = client.post(path, {**data, "add_row": "identifiers"})
    assert added.status_code == 200
    data = posted_fields(added)
    data.update(
        {
            "identifiers-0-scope": "manual-1",
            "identifiers-0-kind": "installation",
            "identifiers-0-value": "000 000-000 000 000 003",
        }
    )
    response = client.post(path, data)
    assert response.status_code == 302, response.content.decode()
    invoice = document.invoices.get()
    assert invoice.kind == "credit" and invoice.invoice_total_minor == -12500
    assert invoice.amount_due_minor == -11500 and invoice.reviews.get().amount_due_confirmed
    section = invoice.sections.get()
    assert section.property == houses[0] and section.identifiers[0]["value"] == "000000000000000003"
    usage = section.usages.get()
    assert usage.quantity_milli == -1250 and usage.end.isoformat() == "2024-03-01"
    assert usage.evidence["text"].startswith("Manually reviewed")
    assert (section.start, section.end) == (date(2024, 2, 29), date(2024, 3, 1))
    assert section.period_evidence["text"].startswith("Manually reviewed")
    assert invoice.reviews.get().snapshot["values"]["layout_family"] == "manual"
    document.refresh_from_db()
    assert private_path(document.original_path).read_bytes() == pdf
    detail = client.get(response.url).content.decode()
    assert "-1,250 m³" in detail and "2024-02-29 to 2024-02-29" in detail


@pytest.mark.parametrize("factory", [lambda: multi_service(correction=True), multi_service])
def test_multi_service_and_dated_corrections_survive_web_review(
    client, configured, tmp_path, factory
):
    document, _ = upload(client, tmp_path, factory())
    invoice = approved(client, document)
    assert invoice.sections.count() == 4
    assert set(invoice.sections.values_list("service", flat=True)) == {
        "water",
        "waste",
        "electricity_network",
    }
    quantities = list(Usage.objects.order_by("pk").values_list("quantity_milli", flat=True))
    expected = (
        [45000, -12000, -14000, 12000, 7500, 321125]
        if factory != multi_service
        else [12000, 12000, 7500, 321125]
    )
    assert quantities == expected


@pytest.mark.parametrize(
    "changes,error",
    [
        ({"net_minor": "357,551"}, "decimal places"),
        ({"usages-0-quantity_milli": "140,2501"}, "decimal places"),
        ({"sections-0-end": "2024-01-31"}, "Last billed day"),
        ({"usages-0-unit": "kg"}, "unit that matches"),
        ({"usages-0-utility": "water", "usages-0-unit": "m3"}, "allocation&#x27;s service"),
        ({"usages-0-basis": "reversal"}, "negative quantity"),
        ({"sections-0-gross_minor": "100,00"}, "Net plus VAT"),
        ({"vendor": "999999"}, "Select a valid choice"),
        ({"sections-0-property": ""}, "required"),
        ({"invoice_total_minor": "448,00"}, "must equal invoice total"),
        (
            {
                "sections-0-net_minor": "0,00",
                "sections-0-vat_minor": "0,00",
                "sections-0-gross_minor": "0,00",
            },
            "Allocation amounts must add up",
        ),
        ({"sections-1-property": "1"}, "Overlapping electricity"),
        ({"currency": "EUR"}, "Select a valid choice"),
    ],
)
def test_invalid_corrections_show_errors_and_retain_approved_rows(
    client, configured, tmp_path, changes, error
):
    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    old_rows = list(BillingSection.objects.values())
    path, data = review_data(client, document)
    if changes == {"sections-1-property": "1"}:
        changes = {"sections-1-property": str(configured[2][0].pk)}
    response = client.post(path, {**data, **changes})
    assert response.status_code == 400 and error in response.content.decode()
    invoice.refresh_from_db()
    assert invoice.status == "approved" and invoice.review_revision == 1
    assert list(BillingSection.objects.values()) == old_rows
    assert invoice.reviews.count() == 1 and document.extractions.count() == 1


def test_correction_and_reparse_proposals_preserve_edits_until_accepted(
    client, configured, tmp_path
):
    _, _, houses = configured
    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    extraction = copy.deepcopy(document.extractions.get().candidate)
    path, data = review_data(client, document)
    data.update(
        {
            "reference": "SYNTHETIC-CORRECTED-001",
            "usages-0-quantity_milli": "141.125",
            "usages-0-end": "2024-02-28",
            "identifiers-0-value": "9901",
            "sections-0-property": str(houses[1].pk),
            "sections-1-property": str(houses[0].pk),
            "note": "Corrected reference, property assignments and billed consumption.",
        }
    )
    assert client.post(path, data).status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 2 and invoice.reference == "SYNTHETIC-CORRECTED-001"
    assert Usage.objects.order_by("pk").first().quantity_milli == 141125
    first = invoice.reviews.get(revision=1).snapshot["values"]
    assert (
        first["reference"] == "880003"
        and first["sections"][0]["usages"][0]["quantity_milli"] == 140250
    )
    assert document.extractions.get().candidate == extraction
    assert not client.get(path).context["has_latest_changes"]
    document_page = client.get(f"/documents/{document.pk}/")
    assert document_page.context["proposal_reviewed"]
    summary = document_page.context["approved"]
    assert summary["reference"] == "SYNTHETIC-CORRECTED-001"
    assert summary["sections"][0]["property"] == houses[1].name
    assert summary["usages"][0]["quantity"] == "141,125"
    current, separator, history = document_page.content.decode().partition(
        '<details class="extraction-history">'
    )
    assert separator and "141,125 kWh" in current and "140,250 kWh" in history
    assert "140,250 kWh" not in current
    _, old_form = review_data(client, document)
    document.refresh_from_db()
    reparse = {"extraction_revision": document.extraction_revision}
    assert client.post(f"/documents/{document.pk}/reparse/", reparse).status_code == 302
    assert client.post(f"/documents/{document.pk}/reparse/", reparse).status_code == 302
    assert document.extractions.count() == 2 and document.invoices.count() == 1
    invoice.refresh_from_db()
    assert invoice.reference == "SYNTHETIC-CORRECTED-001" and invoice.review_revision == 2
    page = client.get(f"/documents/{document.pk}/")
    assert not page.context["proposal_reviewed"]
    assert b"Proposed differences" in page.content and b"141,125 kWh" in page.content
    differences = page.context["differences"]
    assert any(
        row["approved"] == "141,125 kWh" and row["proposed"] == "140,250 kWh" for row in differences
    )
    assert client.post(path, old_form).status_code == 409
    _, corrected = review_data(client, document)
    assert corrected["usages-0-quantity_milli"] == "141,125"
    review_page = client.get(path)
    assert review_page.context["has_latest_changes"]
    assert b"Load latest extraction" in review_page.content
    assert b"Load latest extraction" not in page.content
    _, proposed = review_data(client, document, proposal=True)
    assert proposed["reference"] == "880003" and proposed["usages-0-quantity_milli"] == "140,250"
    assert client.post(path, proposed).status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 3 and invoice.reference == "880003"
    assert invoice.reviews.count() == 3 and invoice.reviews.get(revision=2).note.startswith(
        "Corrected"
    )
    assert not client.get(path).context["has_latest_changes"]


def test_matching_reparse_does_not_offer_latest_extraction(client, configured, tmp_path):
    document, _ = upload(client, tmp_path)
    approved(client, document)
    document.refresh_from_db()
    client.post(
        f"/documents/{document.pk}/reparse/",
        {"extraction_revision": document.extraction_revision},
    )
    response = client.get(f"/documents/{document.pk}/review/")
    assert not response.context["has_latest_changes"]
    assert b"Load latest extraction" not in response.content
    assert posted_fields(response)["usages-0-quantity_milli"] == "140,250"


@pytest.mark.parametrize("target", ["property", "vendor"])
def test_same_named_routing_changes_can_be_reviewed_and_accepted(
    client, configured, tmp_path, target
):
    _, vendor, houses = configured
    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    if target == "property":
        previous = houses[0]
        replacement = Property.objects.create(name=previous.name, slug="replacement-house")
        PropertyIdentifier.objects.filter(vendor=vendor, property=previous).update(
            property=replacement
        )
        field, label = "sections-0-property", "Allocation 1 property"
    else:
        previous = vendor
        replacement = Vendor.objects.create(name=previous.name, slug="replacement-utilities")
        for model in (VendorIssuerIdentifier, VendorLayoutFamily, PropertyIdentifier):
            model.objects.filter(vendor=previous).update(vendor=replacement)
        field, label = "vendor", "Vendor"

    document.refresh_from_db()
    assert (
        client.post(
            f"/documents/{document.pk}/reparse/",
            {"extraction_revision": document.extraction_revision},
        ).status_code
        == 302
    )
    assert not document.extractions.latest("revision").candidate["issues"]
    path = f"/documents/{document.pk}/review/"
    current = client.get(path)
    assert current.context["has_latest_changes"]
    assert b"Load latest extraction" in current.content
    assert posted_fields(current)[field] == str(previous.pk)
    expected_difference = {
        "field": label,
        "approved": f"{previous.name} ({previous.slug})",
        "proposed": f"{replacement.name} ({replacement.slug})",
    }
    assert expected_difference in current.context["differences"]
    assert expected_difference in client.get(f"/documents/{document.pk}/").context["differences"]

    _, proposed = review_data(client, document, proposal=True)
    assert proposed[field] == str(replacement.pk)
    assert client.post(path, proposed).status_code == 302
    invoice.refresh_from_db()
    if target == "property":
        assert invoice.sections.get(section_id="section_01").property_id == replacement.pk
    else:
        assert invoice.vendor_id == replacement.pk
    assert invoice.review_revision == 2
    assert document.invoices.count() == 1
    assert not client.get(path).context["has_latest_changes"]


def test_review_can_keep_approved_corrections_when_new_extraction_differs(
    client, configured, tmp_path
):
    document, _ = upload(client, tmp_path)
    invoice = approved(client, document)
    path, data = review_data(client, document)
    data["usages-0-quantity_milli"] = "141,125"
    assert client.post(path, data).status_code == 302
    document.refresh_from_db()
    client.post(
        f"/documents/{document.pk}/reparse/",
        {"extraction_revision": document.extraction_revision},
    )
    assert client.get(path).context["has_latest_changes"]
    _, current = review_data(client, document)
    assert client.post(path, current).status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 3
    assert (
        Usage.objects.filter(section__invoice=invoice).order_by("pk").first().quantity_milli
        == 141125
    )
    assert not client.get(path).context["has_latest_changes"]
    assert document.extractions.count() == 2
    assert document.pk not in {row.pk for row in client.get("/documents/").context["page"]}


@pytest.mark.parametrize("previous_approval", [False, True])
def test_legacy_evidence_prefills_periods_and_allows_cost_correction_without_fee_rows(
    client, configured, tmp_path, previous_approval
):
    document, _ = upload(client, tmp_path)
    attempt = document.extractions.get()
    legacy = copy.deepcopy(attempt.candidate)
    legacy["parser_revision"] = "2"
    for section in legacy["sections"]:
        section["charges"] = [
            {
                "description": "Synthetic legacy service fee",
                "start": section.pop("start"),
                "end": section.pop("end"),
                "net_minor": section["net_minor"],
                "vat_minor": section["vat_minor"],
                "gross_minor": section["gross_minor"],
                "vat_basis": "allocated",
                "evidence": section.pop("period_evidence"),
            }
        ]
    # Simulate immutable JSON written by the previous schema, before migration.
    type(attempt).objects.filter(pk=attempt.pk).update(candidate=legacy, parser_revision="2")
    old_review = None
    if previous_approval:
        invoice = approved(client, document)
        old_review = invoice.reviews.get()
        snapshot = copy.deepcopy(old_review.snapshot)
        snapshot["values"] = legacy
        ReviewRevision.objects.filter(pk=old_review.pk).update(snapshot=snapshot)
    path, data = review_data(client, document)
    assert not any(name.startswith("charges-") for name in data)
    assert (data["sections-0-start"], data["sections-0-end"]) == ("2024-02-01", "2024-02-29")
    data.update(
        {
            "sections-0-net_minor": "111,55",
            "sections-0-gross_minor": "139,19",
            "net_minor": "358,55",
            "invoice_total_minor": "448,00",
            "amount_due_minor": "448,00",
            "note": "Corrected synthetic service totals from the PDF.",
        }
    )
    response = client.post(path, data)
    assert response.status_code == 302, response.content.decode()
    invoice = document.invoices.get()
    section = invoice.sections.get(section_id="section_01")
    assert (section.net_minor, section.vat_minor, section.gross_minor) == (11155, 2764, 13919)
    assert (section.start, section.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert invoice.review_revision == (2 if previous_approval else 1)
    assert all(
        "charges" not in row
        for row in invoice.reviews.latest("revision").snapshot["values"]["sections"]
    )
    attempt.refresh_from_db()
    assert attempt.candidate == legacy
    if old_review:
        old_review.refresh_from_db()
        assert old_review.snapshot == snapshot


def test_review_tokens_from_the_itemized_schema_are_rejected_without_writes(
    client, configured, tmp_path
):
    document, _ = upload(client, tmp_path)
    path, data = review_data(client, document)
    token = signing.dumps(asdict(current_review_context(document)), salt="bills.document-review")
    response = client.post(path, {**data, "review_token": token})
    assert response.status_code == 400 and b"review form is invalid" in response.content
    assert not BillingSection.objects.exists() and not ReviewRevision.objects.exists()


def test_retry_and_stale_review_cannot_replace_newer_values(client, configured, tmp_path):
    document, _ = upload(client, tmp_path)
    path, data = review_data(client, document)
    assert client.post(path, data).status_code == 302
    repeated = client.post(path, data)
    assert repeated.status_code == 409 and b"reload before saving" in repeated.content
    _, old = review_data(client, document)
    assert client.post(path, {**old, "usages-0-quantity_milli": "142,000"}).status_code == 302
    stale = client.post(path, {**old, "usages-0-quantity_milli": "143,000"})
    assert stale.status_code == 409 and b"143,000" in stale.content
    assert b"Reload the latest review" in stale.content
    invoice = document.invoices.get()
    assert invoice.review_revision == 2 and invoice.reviews.count() == 2
    assert Usage.objects.order_by("pk").first().quantity_milli == 142000


@pytest.mark.parametrize("manual", [False, True])
def test_competing_web_saves_approve_once(configured, client, tmp_path, manual):
    actor, vendor, houses = configured
    document, _ = upload(client, tmp_path, [["Unsupported synthetic source"]] if manual else None)
    path, data = review_data(client, document)
    if manual:
        data = manual_fields(data, vendor, houses[0])
    clients = [Client(), Client()]
    for reviewer in clients:
        reviewer.force_login(actor)
    ready = Barrier(2)

    def save(reviewer, note):
        try:
            ready.wait(timeout=5)
            return reviewer.post(path, {**data, "note": note}).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [
            pool.submit(save, reviewer, f"Synthetic competing save {i}")
            for i, reviewer in enumerate(clients)
        ]
        assert sorted(task.result(timeout=20) for task in tasks) == [302, 409]
    invoice = document.invoices.get()
    assert invoice.review_revision == 1 and invoice.reviews.count() == 1
    assert ReviewRevision.objects.count() == 1


def test_conflicting_identity_visible_and_cannot_duplicate_costs(client, configured, tmp_path):
    document, _ = upload(client, tmp_path)
    first = approved(client, document)
    other, _ = upload(client, tmp_path, shared_supply(version="v2"), filename="conflict.pdf")
    page = client.get(f"/documents/{other.pk}/")
    assert b"Different files share this invoice identity" in page.content
    assert f"/documents/{document.pk}/".encode() in page.content
    path, data = review_data(client, other)
    response = client.post(path, data)
    assert response.status_code == 400 and b"An approved invoice already uses" in response.content
    assert other.invoices.get().status == "review" and other.invoices.get().sections.count() == 0
    assert first.sections.count() == 2 and Usage.objects.count() == 2
    assert Invoice.objects.filter(status="approved").count() == 1


def test_rows_can_be_removed_explicitly_but_truncating_management_data_is_rejected(
    client, configured, tmp_path
):
    document, _ = upload(client, tmp_path)
    path, data = review_data(client, document)
    bad = client.post(path, {**data, "sections-TOTAL_FORMS": "1"})
    assert bad.status_code == 400 and b"Form rows changed unexpectedly" in bad.content
    assert not BillingSection.objects.exists()
    data.update({"sections-1-DELETE": "on", "usages-1-DELETE": "on", "identifiers-2-DELETE": "on"})
    data.update(
        {
            "net_minor": "110,55",
            "vat_minor": "27,64",
            "rounding_minor": "0,00",
            "invoice_total_minor": "138,19",
            "amount_due_minor": "138,19",
        }
    )
    response = client.post(path, data)
    assert response.status_code == 302, response.content.decode()
    invoice = document.invoices.get()
    assert invoice.sections.count() == 1 and Usage.objects.count() == 1


def test_warning_acknowledgement_invalid_tokens_and_row_limits_do_not_approve(
    client, configured, tmp_path
):
    _, vendor, houses = configured
    document, _ = upload(client, tmp_path, [["<script>Synthetic unsupported invoice</script>"]])
    path, data = review_data(client, document)
    data = manual_fields(data, vendor, houses[0])
    no_warning = client.post(path, {**data, "warnings_confirmed": ""})
    assert no_warning.status_code == 400 and b"required" in no_warning.content
    for token in ("", "synthetic-invalid-token", data["review_token"][:-1]):
        response = client.post(path, {**data, "review_token": token})
        assert response.status_code == 400 and b"review form is invalid" in response.content
    response = client.post(path, {**data, "sections-TOTAL_FORMS": "10000000"})
    assert response.status_code == 400 and b"at most 20 forms" in response.content
    other, _ = upload(client, tmp_path, [["Another synthetic source"]], filename="other.pdf")
    response = client.post(f"/documents/{other.pk}/review/", data)
    assert response.status_code == 400
    assert not Invoice.objects.exists() and not BillingSection.objects.exists()


def test_review_and_reparse_require_login_csrf_and_post(client, configured, tmp_path):
    document, _ = upload(client, tmp_path)
    path, data = review_data(client, document)
    anonymous = Client()
    for route in (
        "/invoices/",
        path,
        f"/documents/{document.pk}/reparse/",
        f"/documents/{document.pk}/view/",
    ):
        assert anonymous.get(route).status_code == 302
    secured = Client(enforce_csrf_checks=True)
    secured.force_login(configured[0])
    secured.get(path)
    assert secured.post(path, data).status_code == 403
    assert (
        secured.post(f"/documents/{document.pk}/reparse/", {"extraction_revision": 1}).status_code
        == 403
    )
    assert secured.get(f"/documents/{document.pk}/reparse/").status_code == 405
    data["csrfmiddlewaretoken"] = secured.cookies["csrftoken"].value
    response = secured.post(path, data)
    assert response.status_code == 302
    assert anonymous.get(response.url).status_code == 302


def test_money_input_cannot_round_away_overprecision_near_the_storage_limit():
    field = ScaledNumberField()
    assert field.clean("92233720368547758.07") == 2**63 - 1
    assert field.clean("-92233720368547758,08") == -(2**63)
    with pytest.raises(ValidationError, match="decimal places"):
        field.clean("92233720368547758.070000000001")
    with pytest.raises(ValidationError, match="too large"):
        field.clean("92233720368547758.08")


def test_missing_reference_does_not_flag_another_incomplete_invoice_as_a_duplicate(
    client, configured, tmp_path
):
    document, _ = upload(client, tmp_path, [["Synthetic invoice with no reference"]])
    other = Document.objects.create(sha256="f" * 64, original_filename="other-synthetic.pdf")
    Invoice.objects.create(document=other, vendor=configured[1])
    response = client.get(f"/documents/{document.pk}/")
    assert response.status_code == 200
    assert not response.context["conflicts"]
    assert b"Different files share this invoice identity" not in response.content


@pytest.mark.parametrize("property_name", ["House 1", "House <script>synthetic</script>"])
def test_section_warning_names_and_links_its_allocation_and_billing_page(
    client, configured, tmp_path, property_name
):
    house = configured[2][0]
    house.name = property_name
    house.save()
    pages = [
        [
            line
            for line in page
            if not (isinstance(line, str) and line.startswith("Avläsning") and "kWh" in line)
        ]
        for page in multi_service()
    ]
    document, _ = upload(client, tmp_path, pages)
    attempt = document.extractions.get()
    warning = "Missing billed electricity consumption in section_04"
    assert warning in attempt.candidate["issues"]
    response = client.get(f"/documents/{document.pk}/review/")
    assert response.status_code == 200
    details = response.context["warning_details"]
    allocation = next(
        part for item in details for part in item["parts"] if part.get("anchor") == "sections-3"
    )
    assert allocation["text"] == f"Allocation 4 · {property_name} · Electricity network"
    assert allocation["pages"] == [4]
    assert all("section_04" not in part["text"] for item in details for part in item["parts"])
    html = response.content.decode()
    assert 'href="#sections-3"' in html and '<fieldset id="sections-3"' in html
    assert f'href="/documents/{document.pk}/view/#page=4"' in html
    assert "<script>synthetic</script>" not in html
    document_page = client.get(f"/documents/{document.pk}/").content.decode()
    assert f'href="/documents/{document.pk}/review/?proposal=latest#sections-3"' in document_page
    assert "<script>synthetic</script>" not in document_page
    attempt.refresh_from_db()
    assert warning in attempt.candidate["issues"]  # Original diagnostics remain immutable.


def test_section_warning_matches_whole_ids_and_keeps_unassigned_section_context():
    details = warning_details(
        ["Unknown section_040. Compare section_04 with section_03."],
        [
            {
                "section_id": "section_04",
                "service": "electricity_network",
                "identifiers": [{"kind": "contract", "evidence": {"page": 4}}],
            },
            {"section_id": "section_03", "service": "water", "total_evidence": {"page": 3}},
        ],
        {},
    )
    parts = details[0]["parts"]
    assert parts[0]["text"] == "Unknown section_040. Compare "
    links = [part for part in parts if part.get("anchor")]
    assert [(part["text"], part["anchor"], part["pages"]) for part in links] == [
        ("Allocation 1 · Needs assignment · Electricity network", "sections-0", [4]),
        ("Allocation 2 · Needs assignment · Water", "sections-1", [3]),
    ]
