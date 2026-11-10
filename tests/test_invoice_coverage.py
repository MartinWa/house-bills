"""Recurring expectations, unapproved receipts, vendor changes and waste cost categories."""

import hashlib
from datetime import date
from itertools import count

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from bills.invoice_coverage import mark_invoice_not_sent, read_invoice_coverage
from bills.models import (
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Vendor,
)
from bills.parsing.types import Candidate, Evidence, Section
from bills.reporting import read_report
from bills.services import approve_review, record_extraction

from .synthetic import write_pdf

pytestmark = pytest.mark.django_db(transaction=True)
EVIDENCE = Evidence(1, "Wholly invented invoice coverage evidence", (0, 0, 100, 20))


@pytest.fixture
def coverage(tmp_path, settings, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "originals"
    vendor = Vendor.objects.create(name="Synthetic Utility", slug="synthetic-utility")
    houses = [
        Property.objects.create(name=f"Synthetic House {i}", slug=f"house-{i}") for i in (1, 2)
    ]
    actor = django_user_model.objects.create_user("synthetic-coverage-reviewer")
    sequence = count(1)

    def candidate(specifications, *, invoice_date=date(2024, 3, 5), negative=False):
        reference = f"synthetic-{next(sequence)}"
        net = -10000 if negative else 10000
        sections = [
            Section(
                section_id=f"allocation-{i}",
                service=service,
                identifiers=[],
                subcategory=subcategory,
                net_minor=net,
                vat_minor=net // 4,
                gross_minor=net + net // 4,
                start=date(2023, 12, 1),
                end=date(2024, 1, 1),
                period_evidence=EVIDENCE,
                tax_evidence=EVIDENCE,
                total_evidence=EVIDENCE,
            )
            for i, (_, service, subcategory) in enumerate(specifications)
        ]
        proposed = Candidate(
            layout_family="synthetic",
            layout_version="v1",
            parser_revision="1",
            reference=reference,
            invoice_date=invoice_date,
            due_date=None,
            currency="SEK",
            net_minor=sum(s.net_minor for s in sections),
            vat_minor=sum(s.vat_minor for s in sections),
            rounding_minor=0,
            invoice_total_minor=sum(s.gross_minor for s in sections),
            amount_due_minor=sum(s.gross_minor for s in sections),
            header_evidence={},
            sections=sections,
        )
        assignments = {
            section.section_id: spec[0]
            for section, spec in zip(sections, specifications, strict=True)
        }
        return proposed, assignments

    def document(reference):
        path = settings.MEDIA_ROOT / "inbox" / f"{reference}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(path, [[f"Wholly invented coverage source {reference}"]])
        return Document.objects.create(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            original_filename=f"{reference}.pdf",
            original_path=f"inbox/{reference}.pdf",
            storage_state=Document.StorageState.INBOX,
        )

    def approve(
        specifications, *, invoice_date=date(2024, 3, 5), kind="invoice", assigned_vendor=None
    ):
        proposed, assignments = candidate(
            specifications, invoice_date=invoice_date, negative=kind == "credit"
        )
        invoice = Invoice.objects.create(document=document(proposed.reference))
        invoice = approve_review(
            invoice.pk,
            expected_revision=0,
            candidate=proposed,
            vendor=assigned_vendor or vendor,
            section_properties=assignments,
            actor=actor,
            kind=kind,
        )
        return invoice, proposed, assignments

    def schedule(house=houses[0], service="water", **changes):
        configured = PropertyService.objects.create(
            property=house,
            service=service,
            **{"start_month": date(2024, 2, 1), "end_month": date(2024, 4, 1), **changes},
        )
        ServiceVendorAssignment.objects.create(
            property_service=configured,
            vendor=vendor,
            start_month=configured.start_month,
            end_month=configured.end_month,
        )
        return configured

    def read(**changes):
        return read_invoice_coverage(
            **{
                "date_from": date(2024, 2, 1),
                "date_to": date(2024, 4, 30),
                "as_of": date(2024, 5, 1),
                **changes,
            }
        )

    return {
        "vendor": vendor,
        "houses": houses,
        "actor": actor,
        "candidate": candidate,
        "document": document,
        "approve": approve,
        "schedule": schedule,
        "read": read,
    }


def test_invoice_month_and_property_service_match_without_counting_allocations_twice(coverage):
    first, second = coverage["houses"]
    for house in (first, second):
        coverage["schedule"](house)
    coverage["schedule"](first, "electricity_network")
    invoice, _, _ = coverage["approve"](
        [
            (first, "water", ""),
            (first, "water", ""),
            (second, "water", ""),
        ]
    )
    report = coverage["read"]()
    received = [row for row in report["coverage_rows"] if row["status"] == "received"]
    assert len(received) == 2
    assert {row["expected_month"] for row in received} == {date(2024, 3, 1)}
    assert {row["invoice_id"] for row in received} == {invoice.pk}
    assert {row["approved_invoice_count"] for row in received} == {1}
    assert report["missing_count"] == 7
    assert all(row["category"] == "water" for row in received)


def test_unapproved_import_is_received_needing_review_and_has_no_reported_costs(coverage):
    house = coverage["houses"][0]
    coverage["schedule"](house, "waste", subcategory="waste_collection")
    proposed, assignments = coverage["candidate"]([(house, "waste", "waste_collection")])
    source = coverage["document"](proposed.reference)
    proposed.net_minor = None
    record_extraction(
        source.pk,
        proposed,
        vendor=coverage["vendor"],
        needs_review=True,
        error="Synthetic incomplete extraction",
        routing={
            "vendor_id": coverage["vendor"].pk,
            "kind": "invoice",
            "section_properties": {key: property.pk for key, property in assignments.items()},
        },
    )
    report = coverage["read"]()
    received = [row for row in report["coverage_rows"] if row["status"] == "needs_review"]
    assert len(received) == 1 and received[0]["review_document_id"] == source.pk
    assert received[0]["review_document_count"] == 1
    assert report["review_count"] == 1 and report["missing_count"] == 2
    assert read_report()["costs"] == []


def legacy_waste_extraction(coverage, section_values):
    """Save invented pre-subcategory JSON without adapting or rewriting its evidence."""
    house = coverage["houses"][0]
    proposed, properties = coverage["candidate"]([(house, "waste", "") for _ in section_values])
    values = proposed.to_dict()
    for section, changes in zip(values["sections"], section_values, strict=True):
        section.pop("subcategory")
        section.update(changes)
    values["routing"] = {
        "vendor_id": coverage["vendor"].pk,
        "kind": "invoice",
        "section_properties": {key: property.pk for key, property in properties.items()},
    }
    source = coverage["document"](proposed.reference)
    extraction = ExtractionAttempt.objects.create(
        document=source,
        revision=1,
        layout_family="synthetic",
        layout_version="v1",
        parser_revision="legacy",
        candidate=values,
        page_evidence=[{"page": 1, "text": "Invented legacy source", "bbox": [0, 0, 1, 1]}],
        state=ExtractionAttempt.State.REVIEW,
    )
    Document.objects.filter(pk=source.pk).update(extraction_revision=1, state=Document.State.REVIEW)
    return source, extraction


def legacy_evidence(text):
    return {"page": 1, "text": text, "bbox": [0, 0, 100, 20]}


@pytest.mark.parametrize(
    "location,text,subcategory",
    [
        ("period_evidence", "Grundavgift permanentbostad 2024-01-01", "waste_collection"),
        ("charges", "Tank emptying: synthetic billed item", "tank_emptying"),
        ("usages", "Restavfall synthetic billed weight", "waste_collection"),
        ("usages", "Matavfall synthetic billed weight", "waste_collection"),
        ("period_evidence", "\tSLAMTÖMNING\u00a0synthetic billed item", "tank_emptying"),
        ("charges", "\u00a0TÖMNING\tMatavfall synthetic billed item", "waste_collection"),
        ("charges", "Tömn \u00a0Restavfall synthetic billed item", "waste_collection"),
        ("period_evidence", "Slamavskiljare", "tank_emptying"),
    ],
)
def test_legacy_pending_waste_receipts_match_retained_evidence(
    coverage, location, text, subcategory
):
    configured = {
        category: coverage["schedule"](service="waste", subcategory=category)
        for category in ("waste_collection", "tank_emptying")
    }
    evidence = legacy_evidence(text)
    changes = (
        {"period_evidence": evidence}
        if location == "period_evidence"
        else {location: [{"evidence": evidence}]}
    )
    source, extraction = legacy_waste_extraction(coverage, [changes])
    before = ExtractionAttempt.objects.values().get(pk=extraction.pk)
    result = coverage["read"](date_from=date(2024, 3, 1), date_to=date(2024, 3, 31))
    pending = [row for row in result["coverage_rows"] if row["status"] == "needs_review"]
    assert len(pending) == result["review_count"] == 1
    assert pending[0]["subcategory"] == subcategory
    assert pending[0]["review_document_id"] == source.pk
    assert result["missing_count"] == 1 and result["received_count"] == 0
    with pytest.raises(ValidationError, match="no longer missing"):
        mark_invoice_not_sent(
            configured[subcategory].pk,
            coverage["vendor"].pk,
            date(2024, 3, 1),
            reason="Synthetic stale not-sent request.",
        )
    assert not ServiceInvoiceException.objects.exists()
    assert read_report()["costs"] == []
    assert ExtractionAttempt.objects.values().get(pk=extraction.pk) == before


@pytest.mark.parametrize(
    "texts",
    [
        ["Grundavgift permanentbostad synthetic fee", "Slamavskiljare synthetic fee"],
        ["Synthetic unspecified waste"],
        ["Restavfall_extra", "Slamavskiljare2", "Tank emptyingly"],
        ["SlamavskiljareΩ", "Tömnings Restavfall", "Grundavgift permanentbostaden"],
    ],
)
def test_legacy_mixed_or_unidentified_waste_receipts_remain_missing(coverage, texts):
    for subcategory in ("waste_collection", "tank_emptying"):
        coverage["schedule"](service="waste", subcategory=subcategory)
    legacy_waste_extraction(
        coverage, [{"charges": [{"evidence": legacy_evidence(text)} for text in texts]}]
    )
    result = coverage["read"](date_from=date(2024, 3, 1), date_to=date(2024, 3, 31))
    assert result["missing_count"] == 2
    assert result["review_count"] == result["received_count"] == 0


@pytest.mark.parametrize("subcategory", ["", "waste_collection", "tank_emptying", None])
def test_explicit_subcategories_override_legacy_waste_evidence(coverage, subcategory):
    for category in ("waste_collection", "tank_emptying"):
        coverage["schedule"](service="waste", subcategory=category)
    legacy_waste_extraction(
        coverage,
        [
            {
                "subcategory": subcategory,
                "period_evidence": legacy_evidence("Grundavgift permanentbostad synthetic fee"),
            }
        ],
    )
    result = coverage["read"](date_from=date(2024, 3, 1), date_to=date(2024, 3, 31))
    pending = [row for row in result["coverage_rows"] if row["status"] == "needs_review"]
    assert [row["subcategory"] for row in pending] == ([subcategory] if subcategory else [])


def test_legacy_waste_classification_is_scoped_to_each_allocation(coverage):
    for subcategory in ("waste_collection", "tank_emptying"):
        coverage["schedule"](service="waste", subcategory=subcategory)
    source, _ = legacy_waste_extraction(
        coverage,
        [
            {"charges": [{"evidence": legacy_evidence("Restavfall synthetic billed item")}]},
            {"period_evidence": legacy_evidence("Slamavskiljare synthetic billed item")},
        ],
    )
    result = coverage["read"](date_from=date(2024, 3, 1), date_to=date(2024, 3, 31))
    assert result["review_count"] == 2 and result["missing_count"] == 0
    assert {row["review_document_id"] for row in result["coverage_rows"]} == {source.pk}
    assert {row["review_document_count"] for row in result["coverage_rows"]} == {1}


def test_multi_service_pdf_matches_waste_collection_before_and_after_approval(coverage, tmp_path):
    from bills.models import VendorIssuerIdentifier, VendorLayoutFamily
    from bills.parsing import layout_01
    from bills.parsing.reader import read_pdf
    from bills.storage import ingest_pdf

    from .synthetic import multi_service

    vendor, house = coverage["vendor"], coverage["houses"][0]
    vendor.default_property = house
    vendor.save()
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000999901")
    VendorLayoutFamily.objects.create(vendor=vendor, family="layout_01")
    for service in ("water", "waste", "electricity_network"):
        coverage["schedule"](
            house, service, subcategory="waste_collection" if service == "waste" else ""
        )
    pages = multi_service()
    pages[0].append("VAT-nr: SE000000999901")
    path = write_pdf(tmp_path / "synthetic-multi-service.pdf", pages)
    with path.open("rb") as stream:
        source, duplicate = ingest_pdf(stream, filename=path.name)
    assert not duplicate
    result = coverage["read"]()
    assert result["review_count"] == 3 and result["received_count"] == 0
    assert {
        row["review_document_id"]
        for row in result["coverage_rows"]
        if row["status"] == "needs_review"
    } == {source.pk}
    assert read_report()["costs"] == []
    invoice, _ = Invoice.objects.get_or_create(document=source)
    candidate = layout_01.parse(read_pdf(path))
    approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={section.section_id: house for section in candidate.sections},
        actor=coverage["actor"],
        extraction=source.extractions.latest("revision"),
    )
    result = coverage["read"]()
    assert result["received_count"] == 3 and result["review_count"] == 0
    assert (
        sum(row["gross_minor"] for row in read_report()["costs"])
        == candidate.net_minor + candidate.vat_minor
    )


@pytest.mark.parametrize("unmatched", ["property", "vendor", "service", "month", "credit"])
def test_uncertain_or_different_receipts_do_not_hide_a_missing_invoice(coverage, unmatched):
    house = coverage["houses"][0]
    coverage["schedule"](end_month=date(2024, 2, 1))
    proposed, assignments = coverage["candidate"](
        [(house, "water", "")], invoice_date=date(2024, 2, 5)
    )
    routing = {
        "vendor_id": coverage["vendor"].pk,
        "kind": "invoice",
        "section_properties": {key: property.pk for key, property in assignments.items()},
    }
    if unmatched == "property":
        routing["section_properties"] = {key: None for key in assignments}
    elif unmatched == "vendor":
        routing["vendor_id"] = None
    elif unmatched == "service":
        proposed.sections[0].service = "waste"
    elif unmatched == "month":
        proposed.invoice_date = date(2024, 3, 5)
    else:
        routing["kind"] = "credit"
    record_extraction(coverage["document"](proposed.reference).pk, proposed, routing=routing)
    assert coverage["read"]()["coverage_rows"][0]["status"] == "missing"


def test_approved_revision_wins_over_new_extraction_and_duplicate_proposals(coverage):
    first, second = coverage["houses"]
    for house in (first, second):
        coverage["schedule"](house)
    invoice, proposed, _ = coverage["approve"]([(first, "water", "")])
    proposed.invoice_date = date(2024, 4, 5)
    routing = {
        "vendor_id": coverage["vendor"].pk,
        "kind": "invoice",
        "section_properties": {proposed.sections[0].section_id: second.pk},
    }
    record_extraction(invoice.document_id, proposed, routing=routing)
    rows = coverage["read"]()["coverage_rows"]
    assert [
        (row["property_id"], row["expected_month"]) for row in rows if row["status"] == "received"
    ] == [(first.pk, date(2024, 3, 1))]
    assert not any(row["status"] == "needs_review" for row in rows)
    proposed.invoice_date = date(2024, 3, 5)
    routing["section_properties"] = {proposed.sections[0].section_id: first.pk}
    record_extraction(coverage["document"]("synthetic-duplicate").pk, proposed, routing=routing)
    row = next(row for row in coverage["read"]()["coverage_rows"] if row["status"] == "received")
    assert row["approved_invoice_count"] == 1 and row["review_document_count"] == 1


def test_credit_alone_does_not_satisfy_a_regular_invoice_expectation(coverage):
    coverage["schedule"]()
    coverage["approve"]([(coverage["houses"][0], "water", "")], kind="credit")
    assert coverage["read"]()["received_count"] == 0


def test_service_without_a_vendor_remains_visible_in_the_attention_queue(coverage):
    configured = coverage["schedule"]()
    configured.vendor_assignments.all().delete()
    result = coverage["read"](status="attention")
    assert result["unassigned_count"] == 3
    assert {row["status"] for row in result["coverage_rows"]} == {"unassigned"}
    assert {row["property_service_id"] for row in result["coverage_rows"]} == {configured.pk}
    assert all(row["vendor_id"] is None for row in result["coverage_rows"])
    assert result["property_services"][0]["vendor_assignments"] == []


def test_vendor_changes_keep_prior_receipts_and_allow_both_vendors_in_transition_month(coverage):
    configured = coverage["schedule"]()
    original = configured.vendor_assignments.get()
    original.end_month = date(2024, 3, 1)
    original.save()
    replacement = Vendor.objects.create(name="Synthetic Replacement", slug="replacement")
    ServiceVendorAssignment.objects.create(
        property_service=configured,
        vendor=replacement,
        start_month=date(2024, 3, 1),
        end_month=date(2024, 4, 1),
    )
    house = coverage["houses"][0]
    old, _, _ = coverage["approve"]([(house, "water", "")])
    new, _, _ = coverage["approve"](
        [(house, "water", "")],
        invoice_date=date(2024, 3, 25),
        assigned_vendor=replacement,
    )
    result = coverage["read"]()
    rows = [row for row in result["coverage_rows"] if row["status"] == "received"]
    assert {row["invoice_id"] for row in rows} == {old.pk, new.pk}
    assert {row["property_service_id"] for row in rows} == {configured.pk}
    assert all(row["expected_month"] == date(2024, 3, 1) for row in rows)
    assert all(row["approved_invoice_count"] == 1 for row in rows)
    assert sum(row["gross_minor"] for row in read_report()["costs"]) == 25000
    filtered = coverage["read"](vendor_id=replacement.pk)
    assert {row["vendor_id"] for row in filtered["coverage_rows"]} == {replacement.pk}
    old.refresh_from_db()
    assert old.vendor_id == coverage["vendor"].pk and old.review_revision == 1


def test_ending_a_vendor_preserves_service_expectations_and_reported_costs(coverage):
    configured = coverage["schedule"]()
    house = coverage["houses"][0]
    coverage["approve"]([(house, "water", "")])
    before = read_report()
    assignment = configured.vendor_assignments.get()
    assignment.end_month = date(2024, 2, 1)
    assignment.save()
    result = coverage["read"]()
    assert result["unassigned_count"] == 2 and result["missing_count"] == 1
    assert read_report() == before


def test_property_frequency_keeps_its_anchor_when_the_vendor_changes(coverage):
    configured = coverage["schedule"](
        service="waste",
        subcategory="waste_collection",
        frequency_months=3,
        end_month=date(2024, 11, 1),
    )
    first = configured.vendor_assignments.get()
    first.end_month = date(2024, 6, 1)
    first.save()
    replacement = Vendor.objects.create(name="Synthetic New Waste Vendor", slug="new-waste")
    ServiceVendorAssignment.objects.create(
        property_service=configured,
        vendor=replacement,
        start_month=date(2024, 7, 1),
        end_month=date(2024, 11, 1),
    )
    result = coverage["read"](date_to=date(2024, 11, 30))
    assert {row["expected_month"].month for row in result["coverage_rows"]} == {2, 5, 8, 11}
    assert {row["expected_month"].month: row["vendor_id"] for row in result["coverage_rows"]} == {
        2: coverage["vendor"].pk,
        5: coverage["vendor"].pk,
        8: replacement.pk,
        11: replacement.pk,
    }


def test_quarterly_anchor_end_month_as_needed_and_disabled_schedules(coverage):
    house = coverage["houses"][0]
    coverage["schedule"](
        house,
        "waste",
        subcategory="waste_collection",
        frequency_months=3,
        end_month=date(2024, 11, 1),
    )
    coverage["schedule"](house, "waste", subcategory="tank_emptying", frequency_months=0)
    coverage["schedule"](house, "water", is_active=False)
    report = coverage["read"](date_to=date(2024, 12, 31), as_of=date(2024, 9, 15))
    assert {row["expected_month"].month for row in report["coverage_rows"]} == {2, 5, 8, 11}
    assert report["missing_count"] == 3 and report["expected_count"] == 1
    assert {row["subcategory"] for row in report["coverage_rows"]} == {"waste_collection"}
    assert len(report["property_services"]) == 3
    assert {s["frequency_label"] for s in report["property_services"]} == {
        "As needed",
        "Monthly",
        "Quarterly",
    }


def test_leap_year_month_end_and_grace_period(coverage):
    coverage["schedule"](grace_days=2, end_month=date(2024, 2, 1))
    report = coverage["read"](as_of=date(2024, 3, 2))
    assert report["coverage_rows"][0]["missing_after"] == date(2024, 3, 3)
    assert report["coverage_rows"][0]["status"] == "expected"
    assert coverage["read"](as_of=date(2024, 3, 3))["coverage_rows"][0]["status"] == "missing"


def test_confirmed_no_invoice_month_is_scoped_to_service_vendor_and_month(coverage):
    configured = coverage["schedule"]()
    coverage["schedule"](coverage["houses"][1])
    original = configured.vendor_assignments.get()
    original.end_month = date(2024, 3, 1)
    original.save()
    replacement = Vendor.objects.create(name="Synthetic New Utility", slug="new-utility")
    ServiceVendorAssignment.objects.create(
        property_service=configured,
        vendor=replacement,
        start_month=date(2024, 3, 1),
        end_month=date(2024, 4, 1),
    )
    exception = ServiceInvoiceException.objects.create(
        property_service=configured,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic confirmed month with no invoice.",
    )
    before = read_report()
    result = coverage["read"]()
    skipped = [row for row in result["coverage_rows"] if row["status"] == "not_expected"]
    assert len(skipped) == result["not_expected_count"] == 1
    assert skipped[0]["exception_id"] == exception.pk
    assert skipped[0]["exception_reason"] == exception.reason
    assert skipped[0]["property_service_id"] == configured.pk
    assert skipped[0]["vendor_id"] == coverage["vendor"].pk
    assert result["missing_count"] == 6
    assert all(
        row["status"] == "missing" for row in coverage["read"](status="attention")["coverage_rows"]
    )
    assert coverage["read"](status="not_expected")["coverage_rows"] == skipped
    assert read_report() == before


def test_reviewed_and_approved_receipts_take_precedence_over_no_invoice_exception(coverage):
    configured = coverage["schedule"]()
    ServiceInvoiceException.objects.create(
        property_service=configured,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic former confirmation; invoice subsequently arrived.",
    )
    candidate, properties = coverage["candidate"]([(coverage["houses"][0], "water", "")])
    document = coverage["document"](candidate.reference)
    extraction = record_extraction(
        document.pk,
        candidate,
        routing={
            "vendor_id": coverage["vendor"].pk,
            "section_properties": {key: house.pk for key, house in properties.items()},
        },
        vendor=coverage["vendor"],
        needs_review=True,
    )
    pending = coverage["read"]()
    assert pending["review_count"] == 1 and pending["not_expected_count"] == 0
    assert read_report()["costs"] == []
    invoice = document.invoices.get()
    approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=coverage["vendor"],
        section_properties=properties,
        actor=coverage["actor"],
        extraction=extraction,
    )
    received = coverage["read"]()
    assert received["received_count"] == 1 and received["not_expected_count"] == 0
    assert sum(row["gross_minor"] for row in read_report()["costs"]) == 12500


@pytest.mark.parametrize(
    "month,frequency,reason",
    [
        (date(2024, 2, 2), 1, "Synthetic reason"),
        (date(2024, 1, 1), 1, "Synthetic reason"),
        (date(2024, 5, 1), 1, "Synthetic reason"),
        (date(2024, 3, 1), 3, "Synthetic reason"),
        (date(2024, 2, 1), 0, "Synthetic reason"),
        (date(2024, 2, 1), 1, "   "),
    ],
)
def test_no_invoice_exception_rejects_unscheduled_months_and_empty_reasons(
    coverage, month, frequency, reason
):
    configured = coverage["schedule"](frequency_months=frequency)
    with pytest.raises(ValidationError):
        ServiceInvoiceException.objects.create(
            property_service=configured, vendor=coverage["vendor"], month=month, reason=reason
        )


def test_no_invoice_exception_is_visible_with_its_reason_on_coverage_screen(client, coverage):
    configured = coverage["schedule"]()
    ServiceInvoiceException.objects.create(
        property_service=configured,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic vendor confirmed no invoice was issued.",
    )
    client.force_login(coverage["actor"])
    response = client.get(
        reverse("bills:invoice_coverage"),
        {"date_from": "2024-03-01", "date_to": "2024-03-31", "status": "not_expected"},
    )
    assert response.status_code == 200
    assert b"Not sent" in response.content
    assert b"Synthetic vendor confirmed no invoice was issued." in response.content
    assert response.context["missing_count"] == 0 and len(response.context["page"]) == 1


def not_sent_url(coverage, configured):
    return reverse(
        "bills:invoice_not_sent",
        args=[configured.pk, coverage["vendor"].pk, "2024-03"],
    )


def allow_not_sent(coverage):
    from django.contrib.auth.models import Permission

    coverage["actor"].user_permissions.add(
        Permission.objects.get(
            content_type__app_label="bills", codename="add_serviceinvoiceexception"
        )
    )


def test_missing_row_can_be_marked_not_sent_without_mutating_reports(client, coverage):
    configured = coverage["schedule"]()
    path = not_sent_url(coverage, configured)
    allow_not_sent(coverage)
    client.force_login(coverage["actor"])
    page = client.get(
        reverse("bills:invoice_coverage"),
        {"date_from": "2024-03-01", "date_to": "2024-03-31"},
    )
    assert path.encode() in page.content and b"Not sent this month" in page.content
    assert client.get(path).status_code == 200 and not ServiceInvoiceException.objects.exists()
    assert client.post(path, {"reason": " "}).status_code == 400
    assert not ServiceInvoiceException.objects.exists()
    before = read_report()
    response = client.post(path, {"reason": "Synthetic vendor confirmed no invoice was sent."})
    assert response.status_code == 302 and "status=not_expected" in response.url
    exception = ServiceInvoiceException.objects.get()
    assert exception.property_service == configured and exception.vendor == coverage["vendor"]
    assert exception.month == date(2024, 3, 1)
    assert (
        client.post(
            path, {"reason": "A repeated request must retain the saved reason."}
        ).status_code
        == 302
    )
    assert ServiceInvoiceException.objects.count() == 1
    exception.refresh_from_db()
    assert exception.reason == "Synthetic vendor confirmed no invoice was sent."
    assert coverage["read"]()["not_expected_count"] == 1
    assert coverage["read"]()["missing_count"] == 2 and read_report() == before


def test_not_sent_action_requires_permission_and_csrf_and_rejects_wrong_targets(client, coverage):
    from django.test import Client

    configured = coverage["schedule"]()
    path = not_sent_url(coverage, configured)
    assert client.get(path).status_code == 302
    client.force_login(coverage["actor"])
    assert client.get(path).status_code == 403
    assert client.post(path, {"reason": "Synthetic unauthorized annotation."}).status_code == 403
    allow_not_sent(coverage)
    protected = Client(enforce_csrf_checks=True)
    protected.force_login(coverage["actor"])
    assert protected.get(path).status_code == 200
    assert protected.post(path, {"reason": "Synthetic missing CSRF token."}).status_code == 403
    wrong_vendor = Vendor.objects.create(name="Synthetic Unassigned Vendor", slug="unassigned")
    for target in (
        reverse("bills:invoice_not_sent", args=[configured.pk, wrong_vendor.pk, "2024-03"]),
        reverse("bills:invoice_not_sent", args=[configured.pk, coverage["vendor"].pk, "2024-01"]),
        reverse("bills:invoice_not_sent", args=[configured.pk, coverage["vendor"].pk, "invalid"]),
    ):
        assert protected.get(target).status_code == 404
    assert not ServiceInvoiceException.objects.exists()


def test_invoice_arriving_before_not_sent_submission_rejects_stale_annotation(client, coverage):
    configured = coverage["schedule"]()
    path = not_sent_url(coverage, configured)
    allow_not_sent(coverage)
    client.force_login(coverage["actor"])
    assert client.get(path).status_code == 200
    coverage["approve"]([(coverage["houses"][0], "water", "")])
    before = read_report()
    response = client.post(path, {"reason": "Synthetic stale annotation."})
    assert response.status_code == 409 and b"no longer missing" in response.content
    assert not ServiceInvoiceException.objects.exists()
    assert coverage["read"]()["received_count"] == 1 and read_report() == before


def test_tank_emptying_is_a_waste_cost_and_cannot_satisfy_waste_collection(coverage):
    house = coverage["houses"][0]
    coverage["schedule"](house, "waste", subcategory="waste_collection")
    invoice, proposed, assignments = coverage["approve"]([(house, "waste", "tank_emptying")])
    report = read_report()
    assert [
        (row["category"], row["subcategory"], row["subcategory_label"]) for row in report["costs"]
    ] == [("waste", "tank_emptying", "Tank emptying")]
    assert coverage["read"]()["received_count"] == 0
    proposed.sections[0].subcategory = "waste_collection"
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=proposed,
        vendor=coverage["vendor"],
        section_properties=assignments,
        actor=coverage["actor"],
        note="Synthetic subcategory correction",
    )
    assert coverage["read"]()["received_count"] == 1
    assert (
        invoice.reviews.get(revision=1).snapshot["values"]["sections"][0]["subcategory"]
        == "tank_emptying"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"start_month": date(2024, 2, 2)},
        {"end_month": date(2024, 1, 1)},
        {"subcategory": "tank_emptying"},
        {"frequency_months": 5},
        {"grace_days": 61},
    ],
)
def test_invalid_schedule_values_are_rejected(coverage, changes):
    with pytest.raises(ValidationError):
        coverage["schedule"](**changes)


def test_schedule_subcategory_is_also_enforced_by_sqlite(coverage):
    schedule = coverage["schedule"]()
    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyService.objects.filter(pk=schedule.pk).update(subcategory="tank_emptying")


def test_coverage_filters_authentication_and_no_mutation(client, coverage):
    path = reverse("bills:invoice_coverage")
    assert client.get(path).status_code == 302
    client.force_login(coverage["actor"])
    coverage["schedule"]()
    response = client.get(
        path, {"date_from": "2024-03-15", "date_to": "2024-03-31", "status": "missing"}
    )
    assert response.status_code == 200 and response.context["missing_count"] == 1
    assert len(response.context["page"]) == 1
    assert b"2024-03" in response.content
    assert client.get(path, {"date_from": "2024-03-31", "date_to": "2024-03-01"}).status_code == 400
    assert client.get(path, {"status": "invalid"}).status_code == 400
    assert client.post(path).status_code == 405


def test_waste_cost_subcategory_can_be_corrected_through_the_review_screen(client, coverage):
    from .test_review import posted_fields

    invoice, _, _ = coverage["approve"]([(coverage["houses"][0], "waste", "tank_emptying")])
    client.force_login(coverage["actor"])
    path = reverse("bills:review", args=[invoice.document_id])
    data = posted_fields(client.get(path))
    assert data["sections-0-subcategory"] == "tank_emptying"
    data["sections-0-subcategory"] = "waste_collection"
    data["note"] = "Synthetic waste cost classification correction."
    response = client.post(path, data)
    assert response.status_code == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 2
    assert invoice.sections.get().subcategory == "waste_collection"
    assert (
        invoice.reviews.get(revision=1).snapshot["values"]["sections"][0]["subcategory"]
        == "tank_emptying"
    )


def test_review_forms_from_before_cost_subcategories_must_be_reloaded():
    from dataclasses import asdict

    from django.core import signing

    from bills.reviewing import ReviewContext

    context = ReviewContext(1, None, 0, 0, None, None)
    old_token = signing.dumps(asdict(context), salt="bills.document-review.cost-totals")
    with pytest.raises(ValidationError, match="reload"):
        ReviewContext.from_token(old_token, 1)
