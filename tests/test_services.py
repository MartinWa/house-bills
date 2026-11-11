import copy
import hashlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date
from threading import Barrier

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, connections, transaction

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
from bills.parsing import layout_01, layout_02
from bills.parsing.reader import read_pdf
from bills.parsing.types import Evidence, Identifier
from bills.services import (
    StaleReviewError,
    approve_review,
    record_extraction,
    resolve_section_property,
)

from .synthetic import multi_service, shared_supply, write_pdf

pytestmark = pytest.mark.django_db


@pytest.fixture
def setup(tmp_path, django_user_model):
    path = write_pdf(tmp_path / "synthetic.pdf", shared_supply())
    candidate = layout_02.parse(read_pdf(path))
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    houses = [
        Property.objects.create(name=f"Synthetic House {i}", slug=f"house-{i}") for i in (1, 2)
    ]
    for section, house in zip(candidate.sections, houses, strict=True):
        key = section.identifiers[0]
        PropertyIdentifier.objects.create(
            vendor=vendor, property=house, kind=key.kind, value=key.value
        )
    document = Document.objects.create(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        original_filename="synthetic.pdf",
    )
    return {
        "candidate": candidate,
        "vendor": vendor,
        "houses": houses,
        "document": document,
        "invoice": Invoice.objects.create(document=document),
        "actor": django_user_model.objects.create_user("synthetic-reviewer"),
    }


def approve(setup, **changes):
    return approve_review(
        setup["invoice"].pk,
        **{
            "expected_revision": 0,
            "candidate": setup["candidate"],
            "vendor": setup["vendor"],
            "section_properties": {},
            "actor": setup["actor"],
            **changes,
        },
    )


def test_shared_account_maps_sections_individually_and_preserves_source_evidence(setup):
    attempt = record_extraction(setup["document"].pk, setup["candidate"])
    invoice = approve(setup, extraction=attempt)
    assert invoice.status == "approved" and invoice.review_revision == 1
    assert invoice.approved_by == setup["actor"] and invoice.approved_at is not None
    assert invoice.identifiers[0]["kind"] == "customer_account"
    assert invoice.source_pages == [1, 2, 3]
    assert set(invoice.sections.values_list("property_id", flat=True)) == {
        house.pk for house in setup["houses"]
    }
    assert invoice.sections.count() == 2 and Usage.objects.count() == 2
    assert list(
        Usage.objects.order_by("section__section_id").values_list("quantity_milli", flat=True)
    ) == [140250, 510000]
    assert (
        sum(invoice.sections.values_list("gross_minor", flat=True)) + invoice.rounding_minor
        == 44700
    )
    assert list(invoice.sections.values_list("gross_minor", flat=True)) == [13819, 30875]
    assert invoice.document_id == attempt.document_id and Document.objects.count() == 1
    assert all("charges" not in section for section in attempt.candidate["sections"])
    assert all(section.start == date(2024, 2, 1) for section in invoice.sections.all())
    assert len(attempt.page_evidence) > 0
    assert invoice.reviews.get().snapshot["properties"] == {
        section.section_id: house.pk
        for section, house in zip(setup["candidate"].sections, setup["houses"], strict=True)
    }


def test_mapping_ignores_accounts_and_uses_delivery_address_only_as_fallback(setup):
    vendor, houses = setup["vendor"], setup["houses"]
    evidence = Evidence(1, "Synthetic mapping evidence", (0, 0, 200, 20))
    assert (
        resolve_section_property(vendor, [Identifier("customer_account", "9900", evidence)]) is None
    )
    PropertyIdentifier.objects.create(
        vendor=vendor, property=houses[1], kind="delivery_address", value="Example Street 99"
    )
    address = Identifier("delivery_address", " EXAMPLE STREET 99 ", evidence)
    assert resolve_section_property(vendor, [address]) == houses[1]
    assert (
        resolve_section_property(vendor, [address, *setup["candidate"].sections[0].identifiers])
        == houses[0]
    )
    with pytest.raises(ValidationError, match="different properties"):
        resolve_section_property(
            vendor, [key for section in setup["candidate"].sections for key in section.identifiers]
        )


@pytest.mark.parametrize("kind", [None, "customer_account", "installation", "contract"])
def test_vendor_default_applies_when_no_specific_mapping_matches(setup, kind):
    vendor, house = setup["vendor"], setup["houses"][0]
    vendor.default_property = house
    vendor.save()
    identifiers = (
        []
        if kind is None
        else [
            Identifier(
                kind, "synthetic-unmapped-key", Evidence(1, "Synthetic identifier", (0, 0, 200, 20))
            )
        ]
    )
    assert resolve_section_property(vendor, identifiers) == house


def test_specific_and_address_mappings_override_default_and_conflicts_still_fail(setup):
    vendor, houses = setup["vendor"], setup["houses"]
    vendor.default_property = houses[0]
    vendor.save()
    assert resolve_section_property(vendor, setup["candidate"].sections[1].identifiers) == houses[1]
    address = Identifier(
        "delivery_address", "Synthetic Address 99", Evidence(1, "Address", (0, 0, 200, 20))
    )
    PropertyIdentifier.objects.create(
        vendor=vendor,
        property=houses[1],
        kind=address.kind,
        value=address.value,
    )
    assert resolve_section_property(vendor, [address]) == houses[1]
    with pytest.raises(ValidationError, match="different properties"):
        resolve_section_property(
            vendor, [key for section in setup["candidate"].sections for key in section.identifiers]
        )
    second_address = Identifier("delivery_address", "Synthetic Address 100", address.evidence)
    PropertyIdentifier.objects.create(
        vendor=vendor,
        property=houses[0],
        kind=second_address.kind,
        value=second_address.value,
    )
    with pytest.raises(ValidationError, match="different properties"):
        resolve_section_property(vendor, [address, second_address])


def test_mapping_uses_current_saved_default_and_clearing_it_restores_unknown(setup):
    vendor, houses = setup["vendor"], setup["houses"]
    vendor.default_property = houses[0]
    vendor.save()
    changed = Vendor.objects.get(pk=vendor.pk)
    changed.default_property = houses[1]
    changed.save()
    assert resolve_section_property(vendor, []) == houses[1]
    changed.default_property = None
    changed.save()
    assert resolve_section_property(vendor, []) is None


def test_approval_uses_vendor_default_and_preserves_assignments_after_config_changes(
    setup, tmp_path
):
    vendor, houses = setup["vendor"], setup["houses"]
    vendor.default_property = houses[0]
    vendor.save()
    candidate = layout_01.parse(read_pdf(write_pdf(tmp_path / "multi.pdf", multi_service())))
    approved = approve(setup, candidate=candidate)
    assert approved.sections.count() == 4
    assert set(approved.sections.values_list("property_id", flat=True)) == {houses[0].pk}
    assert set(approved.reviews.get().snapshot["properties"].values()) == {houses[0].pk}
    vendor.default_property = houses[1]
    vendor.save()
    assert set(approved.sections.values_list("property_id", flat=True)) == {houses[0].pk}
    assert set(approved.reviews.get().snapshot["properties"].values()) == {houses[0].pk}


def test_failed_and_invalid_extractions_are_retained_without_promotion(setup):
    bad = copy.deepcopy(setup["candidate"])
    bad.issues.append("Synthetic extraction mismatch")
    bad.invoice_total_minor += 100
    attempt = record_extraction(setup["document"].pk, bad)
    assert attempt.candidate["issues"] == ["Synthetic extraction mismatch"]
    assert attempt.state == "review" and Invoice.objects.get().status == "review"
    with pytest.raises(ValidationError, match="candidate issues"):
        approve(setup, candidate=bad, extraction=attempt)
    failed = record_extraction(setup["document"].pk, bad, error="Synthetic read failure")
    assert failed.revision == 2 and failed.state == "failed"
    assert failed.candidate == attempt.candidate and failed.error == "Synthetic read failure"
    empty = record_extraction(setup["document"].pk, error="Synthetic unsupported document")
    assert empty.revision == 3 and empty.candidate == {}
    assert BillingSection.objects.count() == 0 and ReviewRevision.objects.count() == 0
    corrected = approve(setup, extraction=attempt, note="Manually corrected candidate")
    assert corrected.status == "approved"
    attempt.refresh_from_db()
    assert attempt.candidate["invoice_total_minor"] == bad.invoice_total_minor


def test_reparse_proposes_changes_and_correction_keeps_prior_approved_revision(setup):
    original = record_extraction(setup["document"].pk, setup["candidate"])
    approved = approve(setup, extraction=original)
    proposed = copy.deepcopy(setup["candidate"])
    section = proposed.sections[0]
    section.usages[0] = replace(section.usages[0], quantity_milli=141000)
    proposed.parser_revision = "4"
    attempt = record_extraction(setup["document"].pk, proposed)
    approved.refresh_from_db()
    assert approved.review_revision == 1
    assert Usage.objects.get(section__section_id=section.section_id).quantity_milli == 140250
    corrected = approve(
        setup,
        candidate=proposed,
        expected_revision=1,
        extraction=attempt,
        note="Corrected billed usage",
    )
    assert corrected.review_revision == 2
    assert Usage.objects.get(section__section_id=section.section_id).quantity_milli == 141000
    assert corrected.reviews.count() == 2
    assert (
        corrected.reviews.get(revision=1).snapshot["values"]["sections"][0]["usages"][0][
            "quantity_milli"
        ]
        == 140250
    )
    original.refresh_from_db()
    assert original.parser_revision == "3" and attempt.parser_revision == "4"
    with pytest.raises(StaleReviewError):
        approve(setup, expected_revision=1)
    assert corrected.reviews.count() == 2


@pytest.mark.parametrize(
    "fault", ["unit", "total", "property", "usage_overlap", "incomplete_cost", "period"]
)
def test_invalid_corrections_roll_back_all_previously_approved_values(setup, fault):
    approved = approve(setup)
    proposed = copy.deepcopy(setup["candidate"])
    section = proposed.sections[0]
    properties = {}
    if fault == "unit":
        section.usages[0] = replace(section.usages[0], unit="kg")
    elif fault == "total":
        section.net_minor = 100
    elif fault == "property":
        properties[section.section_id] = None
    elif fault == "usage_overlap":
        proposed.sections[1].identifiers = section.identifiers
    elif fault == "period":
        section.end = section.start
    else:
        section.vat_minor = None
    old_ids = list(approved.sections.values_list("pk", flat=True))
    with pytest.raises(ValidationError):
        approve(setup, candidate=proposed, expected_revision=1, section_properties=properties)
    approved.refresh_from_db()
    assert approved.status == "approved" and approved.review_revision == 1
    assert list(approved.sections.values_list("pk", flat=True)) == old_ids
    assert approved.reviews.count() == 1 and Usage.objects.count() == 2


def test_different_documents_with_conflicting_identity_stay_in_review(setup):
    approved = approve(setup)
    other_doc = Document.objects.create(sha256="f" * 64, original_filename="synthetic-conflict.pdf")
    conflict = Invoice.objects.create(document=other_doc)
    with pytest.raises(ValidationError):
        approve({**setup, "invoice": conflict})
    conflict.refresh_from_db()
    assert conflict.status == "review" and conflict.sections.count() == 0
    approved.refresh_from_db()
    assert approved.status == "approved" and approved.review_revision == 1
    # The partial unique index also protects callers bypassing model/service checks.
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=conflict.pk).update(
            vendor=approved.vendor,
            reference=approved.reference,
            kind=approved.kind,
            invoice_date=approved.invoice_date,
            net_minor=approved.net_minor,
            vat_minor=approved.vat_minor,
            rounding_minor=approved.rounding_minor,
            invoice_total_minor=approved.invoice_total_minor,
            amount_due_minor=approved.amount_due_minor,
            status="approved",
            review_revision=1,
            approved_at=approved.approved_at,
            approved_by=approved.approved_by,
        )


def test_unrecognized_manual_layout_can_be_approved_and_balance_difference_is_explicit(setup):
    manual = copy.deepcopy(setup["candidate"])
    manual.layout_family, manual.layout_version, manual.parser_revision = "manual", "", ""
    manual.amount_due_minor += 1000
    with pytest.raises(ValidationError, match="difference"):
        approve(setup, candidate=manual)
    approved = approve(setup, candidate=manual, amount_due_confirmed=True)
    assert approved.invoice_total_minor == 44700 and approved.amount_due_minor == 45700
    assert approved.reviews.get().amount_due_confirmed


def test_dated_water_reversals_survive_model_persistence(setup, tmp_path):
    candidate = layout_01.parse(
        read_pdf(write_pdf(tmp_path / "correction.pdf", multi_service(correction=True)))
    )
    approved = approve(
        setup,
        candidate=candidate,
        section_properties={
            section.section_id: setup["houses"][0] for section in candidate.sections
        },
    )
    assert approved.invoice_total_minor == 135000 and approved.rounding_minor == -8
    reversals = Usage.objects.filter(basis="reversal").order_by("start")
    assert list(reversals.values_list("quantity_milli", "start", "end")) == [
        (usage.quantity_milli, usage.start, usage.end) for usage in candidate.sections[0].usages[1:]
    ]
    water = approved.sections.get(category="water")
    assert (water.net_minor, water.vat_minor, water.gross_minor) == (51000, 12750, 63750)
    assert (water.start, water.end) == (date(2023, 12, 1), date(2024, 3, 1))


def test_extraction_cannot_be_attached_to_a_different_document(setup):
    document = Document.objects.create(sha256="d" * 64, original_filename="different.pdf")
    attempt = record_extraction(document.pk, setup["candidate"])
    with pytest.raises(ValidationError, match="another document"):
        approve(setup, extraction=attempt)


def test_approved_rows_and_audit_attempts_reject_direct_model_edits(setup):
    attempt = record_extraction(setup["document"].pk, setup["candidate"])
    approved = approve(setup, extraction=attempt)
    rows = [approved, approved.sections.first(), Usage.objects.first()]
    for row in rows:
        with pytest.raises(ValidationError, match="review service"):
            row.save()
    with pytest.raises(ValidationError, match="immutable"):
        attempt.save()
    with pytest.raises(ValidationError, match="immutable"):
        approved.reviews.get().save()
    draft = Invoice.objects.create(document=setup["document"])
    section = BillingSection.objects.create(
        invoice=draft, section_id="draft", service="electricity_supply"
    )
    old_section = approved.sections.first()
    old_section.invoice = draft
    with pytest.raises(ValidationError, match="review service"):
        old_section.save()
    old_usage = Usage.objects.first()
    old_usage.section = section
    with pytest.raises(ValidationError, match="review service"):
        old_usage.save()
    with pytest.raises(ValidationError, match="immutable"):
        attempt.delete()
    with pytest.raises(ValidationError, match="immutable"):
        approved.reviews.get().delete()


@pytest.mark.django_db(transaction=True)
def test_competing_reviews_commit_once_and_reject_the_stale_save(setup):
    ready = Barrier(2)

    def review():
        try:
            ready.wait(timeout=5)
            try:
                approve(setup)
                return "approved"
            except StaleReviewError:
                return "stale"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(review) for _ in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == ["approved", "stale"]
    setup["invoice"].refresh_from_db()
    assert setup["invoice"].review_revision == 1
    assert ReviewRevision.objects.count() == 1 and Usage.objects.count() == 2


@pytest.mark.django_db(transaction=True)
def test_configuration_is_available_after_a_new_process_starts(setup):
    vendor = setup["vendor"]
    vendor.default_property = setup["houses"][0]
    vendor.save()
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000000099")
    for family in ["layout_01", "layout_02"]:
        VendorLayoutFamily.objects.create(vendor=vendor, family=family)
    # A fresh application process reads the committed file, not the ORM cache.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django, os, sys, sqlite3; "
                "os.environ['DJANGO_SETTINGS_MODULE'] = 'house_bills.settings.test'; "
                "from django.conf import settings; "
                "settings.DATABASES['default']['NAME'] = sys.argv[1]; "
                "django.setup(); "
                "from bills.models import Property, Vendor, PropertyIdentifier; "
                "assert Property.objects.count() == 2; "
                "vendor = Vendor.objects.get(); "
                "assert vendor.default_property.slug == 'house-1'; "
                "assert vendor.layout_families.count() == 2; "
                "assert PropertyIdentifier.objects.count() == 2; "
                "assert vendor.issuer_identifiers.get().value == 'SE000000000099'; "
                "db = sqlite3.connect(sys.argv[1]); "
                "assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)"
            ),
            str(connection.settings_dict["NAME"]),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
