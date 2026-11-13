import copy
import io
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError, connection, connections, transaction

from bills import processing, storage
from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Property,
    PropertyIdentifier,
    PropertyService,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing import layout_02
from bills.parsing.reader import PDFReadError, read_pdf
from bills.parsing.recognition import recognize_vendor
from bills.parsing.registry import LAYOUTS
from bills.parsing.types import Evidence
from bills.services import approve_review
from tools.synthetic_pdf import invoice_pdf

from .synthetic import multi_service, shared_supply, write_pdf

pytestmark = pytest.mark.django_db(transaction=True)
ISSUER = "SE000000999901"


@pytest.fixture(autouse=True)
def originals(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "originals"


@pytest.fixture
def configured():
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value=ISSUER)
    for family in LAYOUTS:
        VendorLayoutFamily.objects.create(vendor=vendor, family=family)
    houses = [Property.objects.create(name=f"House {i}", slug=f"house-{i}") for i in (1, 2)]
    for i, house in enumerate(houses, 1):
        PropertyIdentifier.objects.create(
            vendor=vendor, property=house, kind="installation", value=f"{i:018}"
        )
    for key in ("9901", "9902", "9903", "9904"):
        PropertyIdentifier.objects.create(
            vendor=vendor, property=houses[0], kind="contract", value=key
        )
    return vendor, houses


def upload(tmp_path, pages, *, issuer=ISSUER, filename="synthetic.pdf"):
    pages = copy.deepcopy(pages)
    if issuer:
        pages[0].append(f"VAT-nr: {issuer}")
    pdf = write_pdf(tmp_path / filename, pages)
    with pdf.open("rb") as handle:
        document, duplicate = storage.ingest_pdf(handle, filename=filename)
    return document, document.extractions.latest("revision"), pdf, duplicate


@pytest.mark.parametrize(
    "factory,family,version,quantities",
    [
        (multi_service, "layout_01", "v1", [12000, 12000, 7500, 321125, 3000]),
        (
            lambda: multi_service(split_network_readings=True),
            "layout_01",
            "v1",
            [12000, 12000, 7500, 121125, 200000, 3000],
        ),
        (
            lambda: multi_service(correction=True),
            "layout_01",
            "v1",
            [45000, -12000, -14000, 12000, 7500, 321125, 3000],
        ),
        (shared_supply, "layout_02", "v1", [140250, 510000]),
        (lambda: shared_supply(version="v2"), "layout_02", "v2", [140250, 510000]),
    ],
)
def test_supported_uploads_persist_proposals_without_approved_rows(
    tmp_path, configured, factory, family, version, quantities
):
    document, attempt, pdf, duplicate = upload(tmp_path, factory())
    candidate = attempt.candidate
    assert not duplicate and document.state == "review" and attempt.state == "review"
    assert (attempt.layout_family, attempt.layout_version) == (family, version)
    assert attempt.parser_revision == LAYOUTS[family][version]
    assert candidate["issues"] == [] and candidate["requires_review"]
    assert [u["quantity_milli"] for s in candidate["sections"] for u in s["usages"]] == quantities
    assert all(candidate["routing"]["section_properties"].values())
    assert candidate["routing"]["vendor_id"] == configured[0].pk
    assert attempt.page_evidence and {e["page"] for e in attempt.page_evidence} == set(
        range(1, len(factory()) + 1)
    )
    assert document.invoices.get().status == "review"
    assert BillingSection.objects.count() == Usage.objects.count() == 0
    assert storage.private_path(document.original_path).read_bytes() == pdf.read_bytes()


def test_configured_fraction_services_extract_separate_costs_and_keep_shared_fees(
    tmp_path, configured
):
    house = configured[1][0]
    for fraction in ("matavfall", "restavfall"):
        PropertyService.objects.create(
            property=house, category="waste", subcategory_id=fraction, start_month=date(2024, 1, 1)
        )
    _, attempt, _, _ = upload(tmp_path, multi_service())
    assert attempt.candidate["issues"] == []
    sections = {section["section_id"]: section for section in attempt.candidate["sections"]}
    assert sections["section_02"]["gross_minor"] == 9000
    assert sections["section_02"]["usages"] == []
    assert sections["section_02_matavfall"]["gross_minor"] == 1200
    assert sections["section_02_restavfall"]["gross_minor"] == 3750
    assert sections["section_02_matavfall"]["usages"][0]["quantity_milli"] == 12000
    assert sections["section_02_restavfall"]["usages"][0]["quantity_milli"] == 7500
    assert all(
        attempt.candidate["routing"]["section_properties"][key] == house.pk for key in sections
    )
    assert (
        sum(section["net_minor"] for section in sections.values()) == attempt.candidate["net_minor"]
    )
    assert (
        sum(section["vat_minor"] for section in sections.values()) == attempt.candidate["vat_minor"]
    )


def test_cosmetic_case_spacing_and_period_dash_keep_version_and_source_evidence(
    tmp_path, configured
):
    pages = shared_supply()
    pages = [
        ["  ".join(line.lower().replace(" - ", " – ").split()) for line in page] for page in pages
    ]
    document, attempt, _, _ = upload(tmp_path, pages)
    assert attempt.layout_version == "v1" and attempt.candidate["issues"] == []
    assert attempt.candidate["sections"][0]["usages"][0]["quantity_milli"] == 140250
    assert "fakturadatum" in attempt.candidate["header_evidence"]["invoice_date"]["text"]
    assert document.state == "review"


def test_header_totals_alongside_other_columns_keep_their_own_amounts(tmp_path, configured):
    pages = shared_supply()
    for i in (6, 7, 8):
        pages[0][i] = "Supporting information " + pages[0][i]
    _, attempt, _, _ = upload(tmp_path, pages)
    assert attempt.candidate["issues"] == []
    assert [attempt.candidate[key] for key in ("net_minor", "vat_minor", "rounding_minor")] == [
        35755,
        8939,
        6,
    ]


def test_replacement_version_and_later_uploaded_old_invoice_coexist(tmp_path, configured):
    _, new, _, _ = upload(tmp_path, shared_supply(version="v2"), filename="new.pdf")
    _, old, _, _ = upload(tmp_path, shared_supply(), filename="old.pdf")
    assert new.layout_version == "v2" and old.layout_version == "v1"
    assert new.candidate["routing"]["vendor_id"] == old.candidate["routing"]["vendor_id"]
    assert [s["usages"] for s in new.candidate["sections"]][0][0]["end"] == "2024-03-01"
    assert Invoice.objects.count() == 2 and BillingSection.objects.count() == 0


def test_invalid_long_reference_still_persists_extraction(tmp_path, configured):
    pages = shared_supply()
    pages[0][2] = "OCR-/fakturanummer: " + "9" * 121
    document, attempt, _, _ = upload(tmp_path, pages)
    assert "Invalid reference length" in attempt.candidate["issues"]
    assert document.state == "review" and not Invoice.objects.exists()


@pytest.mark.parametrize(
    "mode,reason",
    [("unknown", "unsupported"), ("mixed", "ambiguous"), ("incompatible", "unsupported")],
)
def test_unsupported_and_ambiguous_versions_preserve_header_for_review(
    tmp_path, configured, mode, reason
):
    pages = shared_supply()
    if mode == "unknown":
        for page in pages[1:]:
            page[page.index("Fakturaspecifikation.")] = "Ny fakturatabell v3"
    elif mode == "mixed":
        pages += shared_supply(version="v2")[1:]
    else:
        configured[0].layout_families.filter(family="layout_02").delete()
    document, attempt, _, _ = upload(tmp_path, pages)
    assert reason in attempt.error and attempt.state == "failed" and document.state == "review"
    assert (
        attempt.candidate["reference"] == "880003"
        and attempt.candidate["invoice_total_minor"] == 44700
    )
    assert attempt.page_evidence and not BillingSection.objects.exists()


def test_single_page_production_fixture_separates_header_and_billing_totals(configured):
    document, _ = storage.ingest_pdf(io.BytesIO(invoice_pdf()), filename="single-page.pdf")
    candidate = document.extractions.get().candidate
    assert candidate["issues"] == [] and candidate["invoice_total_minor"] == 12500
    assert candidate["sections"][0]["usages"][0]["quantity_milli"] == 100000
    assert document.extractions.get().page_evidence and not BillingSection.objects.exists()


@pytest.mark.parametrize("issuer,reason", [(None, "unknown"), ("SE000000999902", "unknown")])
def test_unknown_vendor_keeps_structurally_extractable_values(tmp_path, configured, issuer, reason):
    document, attempt, _, _ = upload(tmp_path, shared_supply(), issuer=issuer)
    assert reason in attempt.error and document.state == "review"
    assert len(attempt.candidate["sections"]) == 2
    assert attempt.candidate["routing"]["vendor_id"] is None and not Invoice.objects.exists()


def test_issuer_scope_ignores_named_service_company_and_ambiguous_cover_is_not_guessed(
    tmp_path, configured
):
    other = Vendor.objects.create(name="Synthetic Service Company", slug="service-company")
    VendorIssuerIdentifier.objects.create(vendor=other, kind="vat", value="SE000000999902")
    pages = shared_supply()
    pages[1].append("VAT-nr SE000000999902")
    _, attempt, _, _ = upload(tmp_path, pages)
    assert attempt.candidate["routing"]["vendor_id"] == configured[0].pk
    pages[0].append("VAT-nr SE000000999902")
    document, attempt, _, _ = upload(tmp_path, pages, filename="ambiguous.pdf")
    assert "ambiguous" in attempt.error and document.state == "review"
    assert attempt.candidate["routing"]["vendor_id"] is None


def test_organization_identifier_normalizes_label_and_private_value(configured):
    VendorIssuerIdentifier.objects.create(
        vendor=configured[0], kind="organization", value="000000-9999"
    )
    result = recognize_vendor([[Evidence(1, "Org.nr: 000 000-9999", (0, 0, 500, 20))]])
    assert result.vendor == configured[0] and result.identifiers[0].value == "0000009999"


def test_bounded_tax_footer_recognizes_issuer_and_ignores_unlabelled_body_tokens(configured):
    body = Evidence(1, f"Unlabelled service {ISSUER}", (0, 500, 500, 520))
    assert recognize_vendor([[body]]).vendor is None
    footer = Evidence(2, f"Synthetic Utilities {ISSUER} Godkänd för F-skatt", (0, 790, 500, 810))
    assert recognize_vendor([[body], [footer]]).vendor == configured[0]


@pytest.mark.parametrize(
    "mode,issue",
    [
        ("missing_date", "invoice_date"),
        ("invalid_money", "amount_due_minor"),
        ("wrong_total", "Header total mismatch"),
        ("wrong_unit", "Invalid electricity unit"),
        ("precision", "Invalid billed electricity consumption"),
        ("conflicting_usage", "Inconsistent billed kWh"),
        ("duplicate_reference", "Ambiguous reference"),
        ("foreign_currency", "Unsupported currency"),
    ],
)
def test_invalid_fields_keep_candidates_and_cannot_be_approved(
    tmp_path, configured, django_user_model, mode, issue
):
    pages = shared_supply()
    if mode == "missing_date":
        pages[0] = [line for line in pages[0] if not line.startswith("Fakturadatum")]
    elif mode == "invalid_money":
        pages[0][4] = "Att betala: 447,001 kr"
    elif mode == "wrong_total":
        pages[0][4] = "Att betala: 448,00 kr"
    elif mode == "wrong_unit":
        pages[1] = [line.replace("140,250 kWh", "140,250 kg") for line in pages[1]]
    elif mode == "precision":
        pages[1] = [line.replace("140,250 kWh", "140,2501 kWh") for line in pages[1]]
    elif mode == "conflicting_usage":
        pages[1][7] = pages[1][7].replace("140,250 kWh", "141 kWh")
    elif mode == "duplicate_reference":
        pages[1].append("OCR-/fakturanummer: 880004")
    else:
        pages[0][4] = "Att betala (EUR): 447,00 kr"
    document, attempt, pdf, _ = upload(tmp_path, pages)
    assert any(issue in item for item in attempt.candidate["issues"])
    assert document.state == "review" and len(attempt.candidate["sections"]) == 2
    candidate = layout_02.parse(read_pdf(pdf))
    with pytest.raises(ValidationError, match="Resolve candidate issues"):
        approve_review(
            document.invoices.get().pk,
            expected_revision=0,
            candidate=candidate,
            vendor=configured[0],
            section_properties={},
            actor=django_user_model.objects.create_user("reviewer"),
        )
    assert not BillingSection.objects.exists() and not Usage.objects.exists()


@pytest.mark.parametrize("data", [b"%PDF-1.7 damaged", None])
def test_unreadable_and_textless_pdf_keep_original_and_visible_review_reason(tmp_path, data):
    data = data or write_pdf(tmp_path / "blank.pdf", [[]]).read_bytes()
    document, _ = storage.ingest_pdf(io.BytesIO(data), filename="unreadable.pdf")
    attempt = document.extractions.get()
    assert document.state == "review" and attempt.state == "failed" and attempt.error
    assert storage.private_path(document.original_path).read_bytes() == data
    assert attempt.candidate["invoice_total_minor"] is None and not Invoice.objects.exists()


def test_password_protected_pdf_retains_exact_original(tmp_path):
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen.canvas import Canvas

    path = tmp_path / "locked.pdf"
    canvas = Canvas(str(path), encrypt=StandardEncryption("synthetic-password"))
    canvas.drawString(50, 800, "Synthetic encrypted invoice")
    canvas.save()
    with path.open("rb") as source:
        document, _ = storage.ingest_pdf(source, filename=path.name)
    assert "password-protected" in document.processing_message and document.state == "review"
    assert storage.private_path(document.original_path).read_bytes() == path.read_bytes()


def test_page_limit_leaves_a_review_proposal_and_preserves_source(tmp_path):
    path = write_pdf(tmp_path / "too-many-pages.pdf", [[] for _ in range(101)])
    with path.open("rb") as source:
        document, _ = storage.ingest_pdf(source, filename=path.name)
    assert "100-page" in document.processing_message and document.state == "review"
    assert storage.private_path(document.original_path).read_bytes() == path.read_bytes()


def test_reader_failure_retains_prior_page_evidence(tmp_path, configured, monkeypatch):
    pages = [[Evidence(1, "Fakturadatum: 2024-03-05", (0, 0, 400, 20))]]

    def broken_reader(handle):
        raise PDFReadError("Synthetic later-page failure.", pages)

    monkeypatch.setattr(processing, "read_pdf", broken_reader)
    _, attempt, _, _ = upload(tmp_path, shared_supply())
    assert attempt.candidate["invoice_date"] == "2024-03-05" and len(attempt.page_evidence) == 1


def test_parser_bug_keeps_mutated_candidate_and_original(tmp_path, configured, monkeypatch):
    def broken_parse(pages, *, candidate):
        layout_02_original(pages, candidate=candidate)
        raise RuntimeError("Synthetic error must not leak private details")

    layout_02_original = layout_02.parse
    monkeypatch.setattr(layout_02, "parse", broken_parse)
    document, attempt, _, _ = upload(tmp_path, shared_supply())
    assert "extraction failed" in attempt.error and "private details" not in attempt.error
    assert len(attempt.candidate["sections"]) == 2 and document.state == "review"


def test_missing_and_conflicting_properties_keep_independent_proposals(tmp_path, configured):
    vendor, houses = configured
    vendor.property_identifiers.filter(kind="installation", value=f"{2:018}").delete()
    _, attempt, _, _ = upload(tmp_path, shared_supply())
    assert list(attempt.candidate["routing"]["section_properties"].values()) == [houses[0].pk, None]
    assert "Missing property mapping in section_02" in attempt.candidate["issues"]
    vendor.property_identifiers.filter(kind="contract", value="9904").update(property=houses[1])
    _, attempt, _, _ = upload(tmp_path, multi_service(), filename="conflict.pdf")
    assert "Conflicting property mappings in section_04" in attempt.candidate["issues"]


def test_specific_then_delivery_address_then_default_mapping(tmp_path, configured):
    vendor, houses = configured
    vendor.default_property = houses[0]
    vendor.save()
    vendor.property_identifiers.filter(kind="installation", value=f"{2:018}").delete()
    PropertyIdentifier.objects.create(
        vendor=vendor, property=houses[1], kind="delivery_address", value="Synthetic Road 2"
    )
    pages = shared_supply()
    pages[1].append("Leveransadress: Synthetic Road 2")
    pages[2].append("Leveransadress: Synthetic Road 2")
    _, attempt, _, _ = upload(tmp_path, pages)
    assert list(attempt.candidate["routing"]["section_properties"].values()) == [
        houses[0].pk,
        houses[1].pk,
    ]
    _, fallback, _, _ = upload(tmp_path, shared_supply(), filename="fallback.pdf")
    assert list(fallback.candidate["routing"]["section_properties"].values()) == [
        houses[0].pk,
        houses[0].pk,
    ]
    assert "Overlapping electricity usage needs review." in fallback.candidate["issues"]


def test_duplicate_and_simultaneous_initial_processing_make_one_attempt(tmp_path, configured):
    document, initial, pdf, _ = upload(tmp_path, shared_supply())
    with pdf.open("rb") as handle:
        same, duplicate = storage.ingest_pdf(handle, filename="another-house.pdf")
    assert duplicate and same.pk == document.pk and same.extractions.count() == 1

    def process():
        connections.close_all()
        try:
            return processing.parse_document(document.pk).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda _: process(), range(2))) == [initial.pk, initial.pk]


def test_competing_initial_parsers_serialize_one_attempt(tmp_path, configured, monkeypatch):
    parse = processing.parse_document
    monkeypatch.setattr(processing, "parse_document", lambda *args, **kwargs: None)
    pages = shared_supply()
    pages[0].append(f"VAT-nr {ISSUER}")
    path = write_pdf(tmp_path / "competing.pdf", pages)
    with path.open("rb") as source:
        document, _ = storage.ingest_pdf(source, filename=path.name)
    monkeypatch.setattr(processing, "parse_document", parse)

    def process(_):
        connections.close_all()
        try:
            return parse(document.pk).pk
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(process, range(2)))
    assert attempts[0] == attempts[1] and document.extractions.count() == 1


def test_extraction_database_failure_is_recovered_by_duplicate_retry(
    tmp_path, configured, monkeypatch
):
    record = processing.record_extraction
    monkeypatch.setattr(
        processing,
        "record_extraction",
        lambda *a, **k: (_ for _ in ()).throw(OperationalError("interrupted")),
    )
    with pytest.raises(OperationalError):
        upload(tmp_path, shared_supply())
    document = Document.objects.get()
    assert document.state == "processing" and document.extraction_revision == 0
    monkeypatch.setattr(processing, "record_extraction", record)
    _, attempt, _, duplicate = upload(tmp_path, shared_supply())
    assert duplicate and attempt.revision == 1 and attempt.candidate["issues"] == []


def test_reparse_command_preserves_approved_corrections_and_history(
    tmp_path, configured, django_user_model
):
    document, attempt, pdf, _ = upload(tmp_path, shared_supply())
    candidate = layout_02.parse(read_pdf(pdf))
    candidate.sections[0].usages[0] = replace(
        candidate.sections[0].usages[0], quantity_milli=141000
    )
    invoice = approve_review(
        document.invoices.get().pk,
        expected_revision=0,
        candidate=candidate,
        vendor=configured[0],
        section_properties={},
        actor=django_user_model.objects.create_user("reviewer"),
        extraction=attempt,
    )
    call_command("parse_documents", document.pk, stdout=io.StringIO())
    invoice.refresh_from_db()
    document.refresh_from_db()
    assert (
        invoice.status == "approved"
        and invoice.review_revision == 1
        and invoice.reviews.count() == 1
    )
    assert Usage.objects.get(section__section_id="section_01").quantity_milli == 141000
    assert document.extractions.count() == 2 and document.state == "review"
    assert (
        document.extractions.latest("revision").candidate["sections"][0]["usages"][0][
            "quantity_milli"
        ]
        == 140250
    )
    with pytest.raises(CommandError, match="existing document"):
        call_command("parse_documents", document.pk, 999999, stdout=io.StringIO())
    assert document.extractions.count() == 2


def test_reading_is_outside_write_transaction_and_transaction_call_is_rejected(
    tmp_path, configured, monkeypatch
):
    reader = processing.read_pdf

    def checked_reader(handle):
        assert not connection.in_atomic_block
        return reader(handle)

    monkeypatch.setattr(processing, "read_pdf", checked_reader)
    document, _, _, _ = upload(tmp_path, shared_supply())
    with transaction.atomic(), pytest.raises(RuntimeError, match="outside database transactions"):
        processing.parse_document(document.pk, force=True)


def test_authenticated_web_upload_runs_parsing_and_shows_review_status(
    tmp_path, configured, client, django_user_model
):
    pages = shared_supply()
    pages[0].append(f"VAT-nr {ISSUER}")
    pdf = write_pdf(tmp_path / "web.pdf", pages)
    client.force_login(django_user_model.objects.create_user("uploader"))
    response = client.post("/upload/", {"pdf": SimpleUploadedFile("web.pdf", pdf.read_bytes())})
    document = Document.objects.get()
    assert response.status_code == 302 and document.extractions.get().candidate["issues"] == []
    assert b"ready for review" in client.get(response.url).content
    assert document.invoices.get().status == "review" and not BillingSection.objects.exists()


def test_document_displays_exact_allocations_and_signed_correction_usage(
    tmp_path, configured, client, django_user_model
):
    document, _, _, _ = upload(tmp_path, multi_service(correction=True))
    client.force_login(django_user_model.objects.create_user("proposal-viewer"))
    response = client.get(f"/documents/{document.pk}/")
    assert response.status_code == 200
    for value in (
        "Extracted proposal",
        "Synthetic Utilities",
        "House 1",
        "1 350,00 SEK",
        "637,50 SEK",
        "-12,000 m³",
        "-14,000 m³",
        "2023-12-31",
        "Reversal",
    ):
        assert value in response.content.decode()
    assert "VAT-nr" not in response.content.decode()
    assert not BillingSection.objects.exists() and not Usage.objects.exists()
    client.logout()
    assert client.get(f"/documents/{document.pk}/").status_code == 302


def test_document_displays_latest_failed_proposal_with_unknown_values_and_escaped_issues(
    tmp_path, configured, client, django_user_model
):
    from bills.services import record_extraction

    document, _, _, _ = upload(tmp_path, shared_supply())
    record_extraction(document.pk, error="Synthetic failure <script>private</script>")
    client.force_login(django_user_model.objects.create_user("proposal-viewer"))
    response = client.get(f"/documents/{document.pk}/")
    page = response.content.decode()
    assert response.status_code == 200 and "Extracted proposal" in page
    assert "Unknown vendor" in page and "No billing sections could be extracted." in page
    assert "Needs attention" in page and "&lt;script&gt;private&lt;/script&gt;" in page
    assert "<script>private</script>" not in page and "447,00 SEK" not in page
