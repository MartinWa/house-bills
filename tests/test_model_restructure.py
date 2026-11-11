"Acceptance checks for shared bills, independent classification and dated changes."

from copy import deepcopy
from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse

from bills.configuration_actions import change_billing_frequency, change_vendor
from bills.forms import VendorBillingScheduleForm
from bills.invoice_coverage import mark_invoice_not_sent, read_invoice_coverage
from bills.models import (
    BillingSection,
    Invoice,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Subcategory,
    Usage,
    Vendor,
    VendorBillingSchedule,
)
from bills.parsing.types import Usage as CandidateUsage
from bills.reporting import read_report
from bills.services import approve_review

from .test_invoice_coverage import EVIDENCE
from .test_invoice_coverage import coverage as coverage
from .test_review import review_data

pytestmark = pytest.mark.django_db(transaction=True)


def read(**kwargs):
    return read_invoice_coverage(
        date_from=date(2024, 1, 1),
        date_to=date(2024, 12, 1),
        as_of=date(2025, 1, 1),
        **kwargs,
    )


def test_combined_bill_counts_once_and_filters_keep_required_allocations(coverage):
    first, second = coverage["houses"]
    for house in (first, second):
        coverage["schedule"](house, "water")
    coverage["schedule"](first, "waste", subcategory="waste_collection")
    invoice, _, _ = coverage["approve"](
        [
            (first, "water", ""),
            (second, "water", ""),
            (first, "waste", "waste_collection"),
        ]
    )
    report = read()
    received = [row for row in report["coverage_rows"] if row["status"] == "received"]
    assert len(received) == report["received_count"] == 1
    assert received[0]["invoice_ids"] == [invoice.pk]
    assert len(received[0]["required_services"]) == 3
    assert report["missing_count"] == 2
    assert read(property_id=first.pk)["received_count"] == 1
    assert read(service="water")["received_count"] == 1
    with connection.cursor() as cursor:
        result = cursor.execute(
            "SELECT approved_invoice_count, required_service_count, received_service_count "
            "FROM reporting_invoice_coverage WHERE expected_month='2024-03-01'"
        ).fetchall()
    assert result == [(1, 3, 3)]


def test_frequency_change_keeps_service_and_assignment_identities(coverage):
    house, vendor = coverage["houses"][0], coverage["vendor"]
    service = PropertyService.objects.create(
        property=house, category="water", start_month=date(2024, 1, 1)
    )
    assignment = ServiceVendorAssignment.objects.create(
        property_service=service, vendor=vendor, start_month=service.start_month
    )
    schedule = VendorBillingSchedule.objects.create(
        vendor=vendor, start_month=service.start_month, first_expected_month=service.start_month
    )
    service_before = PropertyService.objects.values().get(pk=service.pk)
    assignment_before = ServiceVendorAssignment.objects.values().get(pk=assignment.pk)
    replacement = change_billing_frequency(
        schedule.pk,
        month=date(2024, 7, 1),
        frequency_months=3,
        first_expected_month=date(2024, 8, 1),
        grace_days=4,
    )
    assert replacement.pk != schedule.pk
    assert PropertyService.objects.values().get(pk=service.pk) == service_before
    assert ServiceVendorAssignment.objects.values().get(pk=assignment.pk) == assignment_before
    months = {row["expected_month"].month: row for row in read()["coverage_rows"]}
    assert set(months) == {1, 2, 3, 4, 5, 6, 8, 11}
    assert months[6]["schedule_id"] == schedule.pk and months[8]["schedule_id"] == replacement.pk
    assert months[6]["grace_days"] == 0 and months[8]["grace_days"] == 4


def test_vendor_change_has_independent_phase_and_a_transition_month(coverage):
    service = coverage["schedule"](frequency_months=3, end_month=date(2024, 11, 1))
    vendor = Vendor.objects.create(name="Synthetic Replacement", slug="synthetic-replacement")
    VendorBillingSchedule.objects.create(
        vendor=vendor,
        start_month=date(2024, 5, 1),
        first_expected_month=date(2024, 5, 1),
        frequency_months=6,
    )
    old = service.vendor_assignments.get()
    new = change_vendor(old.pk, month=date(2024, 5, 1), vendor=vendor, transition_month=True)
    assert new.property_service_id == service.pk and new.pk != old.pk
    pairs = {(row["vendor_id"], row["expected_month"].month) for row in read()["coverage_rows"]}
    assert pairs == {
        (coverage["vendor"].pk, 2),
        (coverage["vendor"].pk, 5),
        (vendor.pk, 5),
        (vendor.pk, 11),
    }


def test_missing_vendor_and_schedule_remain_visible_and_occasional_services_do_not_recur(coverage):
    configured = coverage["schedule"]()
    coverage["vendor"].billing_schedules.all().delete()
    PropertyService.objects.create(
        property=coverage["houses"][1],
        category="water",
        start_month=date(2024, 2, 1),
        end_month=date(2024, 4, 1),
    )
    coverage["schedule"](service="waste", subcategory="tank_emptying", frequency_months=0)
    report = read(status="attention")
    assert report["unscheduled_count"] == report["unassigned_count"] == 3
    assert {row["status"] for row in report["coverage_rows"]} == {"unassigned", "unscheduled"}
    assert len(report["property_services"]) == 3
    assert configured.pk in {
        item["property_service_id"]
        for row in report["coverage_rows"]
        for item in row["required_services"]
    }


def test_incomplete_combined_bill_needs_review_then_can_confirm_scoped_not_sent(coverage):
    water = coverage["schedule"]()
    waste = coverage["schedule"](service="waste", subcategory="waste_collection")
    coverage["schedule"](service="waste", subcategory="tank_emptying", frequency_months=0)
    old_annotation = ServiceInvoiceException.objects.create(
        property_service=waste,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic prior no-invoice annotation",
    )
    invoice, candidate, properties = coverage["approve"](
        [
            (water.property, "water", ""),
            (waste.property, "waste", "tank_emptying"),
        ]
    )
    row = next(row for row in read()["coverage_rows"] if row["expected_month"].month == 3)
    assert row["receipt_status"] == "received" and row["status"] == "incomplete"
    assert {item["status"] for item in row["required_services"]} == {"received", "incomplete"}
    with pytest.raises(ValidationError, match="current revision"):
        mark_invoice_not_sent(waste.pk, invoice.vendor_id, date(2024, 3, 1), reason="Unreviewed")
    mark_invoice_not_sent(
        waste.pk,
        invoice.vendor_id,
        date(2024, 3, 1),
        reason="Reviewed source confirms no waste collection",
        reviewed_invoice_id=invoice.pk,
        reviewed_revision=invoice.review_revision,
    )
    assert read()["received_count"] == 1
    old_annotation.refresh_from_db()
    assert old_annotation.property_service_id == waste.pk and old_annotation.reviewed_revision == 1
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=deepcopy(candidate),
        vendor=invoice.vendor,
        section_properties=properties,
        actor=coverage["actor"],
    )
    assert read()["incomplete_count"] == 1
    with pytest.raises(ValidationError, match="current revision"):
        mark_invoice_not_sent(
            waste.pk,
            invoice.vendor_id,
            date(2024, 3, 1),
            reason="Stale review",
            reviewed_invoice_id=invoice.pk,
            reviewed_revision=1,
        )
    # A later receipt with an explicit collection allocation takes precedence.
    coverage["approve"](
        [(waste.property, "waste", "waste_collection")], invoice_date=date(2024, 3, 20)
    )
    assert read()["received_count"] == 1


def test_missing_allocation_confirmation_requires_review_and_returns_to_received_bill(
    coverage, client
):
    water = coverage["schedule"]()
    waste = coverage["schedule"](service="waste", subcategory="waste_collection")
    invoice, _, _ = coverage["approve"]([(water.property, "water", "")])
    url = reverse("bills:invoice_not_sent", args=[waste.pk, invoice.vendor_id, "2024-03"])
    actor = coverage["actor"]
    client.force_login(actor)
    assert client.get(url).status_code == 403
    actor.user_permissions.add(Permission.objects.get(codename="add_serviceinvoiceexception"))
    response = client.get(url)
    assert response.status_code == 200
    assert b'name="reviewed_allocation"' in response.content
    assert b'name="reviewed_invoice_id"' in response.content
    assert b'name="reviewed_revision"' in response.content
    assert reverse("bills:invoice", args=[invoice.pk]).encode() in response.content
    data = {
        "reason": "Reviewed invoice confirms waste was not billed",
        "reviewed_invoice_id": invoice.pk,
        "reviewed_revision": invoice.review_revision,
    }
    assert client.post(url, data).status_code == 400
    assert not ServiceInvoiceException.objects.exists()
    history = list(invoice.reviews.values())
    before = list(BillingSection.objects.filter(invoice=invoice).values())
    response = client.post(url, {**data, "reviewed_allocation": "on"}, follow=True)
    assert response.status_code == 200
    assert response.request["QUERY_STRING"].endswith("status=received")
    assert b"Reviewed invoice confirms waste was not billed" in response.content
    assert b"Waste collection" in response.content
    exception = ServiceInvoiceException.objects.get()
    assert exception.property_service_id == waste.pk
    assert exception.reviewed_invoice_id == invoice.pk and exception.reviewed_revision == 1
    assert list(invoice.reviews.values()) == history
    assert list(BillingSection.objects.filter(invoice=invoice).values()) == before


def test_vendor_wide_exception_is_one_action_and_preserves_existing_scopes(coverage):
    first = coverage["schedule"]()
    second = coverage["schedule"](coverage["houses"][1])
    legacy = ServiceInvoiceException.objects.create(
        property_service=first,
        vendor=coverage["vendor"],
        month=date(2024, 2, 1),
        reason="Synthetic service annotation",
    )
    assert read()["missing_count"] == 3
    mark_invoice_not_sent(
        None, coverage["vendor"].pk, date(2024, 2, 1), reason="Synthetic combined bill was not sent"
    )
    assert read()["not_expected_count"] == 1
    legacy.refresh_from_db()
    assert (
        legacy.property_service_id == first.pk and legacy.reason == "Synthetic service annotation"
    )
    assert second.invoice_exceptions.count() == 0
    coverage["approve"](
        [(first.property, "water", ""), (second.property, "water", "")],
        invoice_date=date(2024, 2, 7),
    )
    assert read()["received_count"] == 1 and read()["not_expected_count"] == 0


def test_catalogue_supports_independent_cost_and_usage_selections_without_splitting_money(coverage):
    plastic = Subcategory.objects.create(key="plastic", category="waste", name="Plastic")
    candidate, properties = coverage["candidate"](
        [(coverage["houses"][0], "waste", "waste_collection")]
    )
    section = candidate.sections[0]
    section.usages = [
        CandidateUsage(
            "waste", "kg", 1250, section.start, section.end, "billed_weight", EVIDENCE, "plastic"
        ),
        CandidateUsage(
            "waste", "kg", -250, section.start, section.end, "reversal", EVIDENCE, "matavfall"
        ),
    ]
    from bills.models import Invoice

    invoice = Invoice.objects.create(document=coverage["document"](candidate.reference))
    approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=coverage["vendor"],
        section_properties=properties,
        actor=coverage["actor"],
    )
    report = read_report()
    assert sum(row["gross_minor"] for row in report["costs"]) == 12500
    assert {(row["subcategory"], row["quantity_milli"]) for row in report["consumption"]} == {
        ("plastic", 1250),
        ("matavfall", -250),
    }
    plastic.name, plastic.is_active = "Packaging plastic", False
    plastic.save()
    assert any(
        row["subcategory_label"] == "Packaging plastic" for row in read_report()["consumption"]
    )
    with pytest.raises(ProtectedError):
        plastic.delete()
    plastic.category = "water"
    with pytest.raises(ValidationError):
        plastic.save()
    plastic.category, plastic.key = "waste", "changed-key"
    with pytest.raises(ValidationError):
        plastic.save()
    section = BillingSection.objects.get(invoice=invoice)
    with transaction.atomic(), pytest.raises(IntegrityError):
        BillingSection.objects.filter(pk=section.pk).update(subcategory="electricity_network")
    with transaction.atomic(), pytest.raises(IntegrityError):
        Usage.objects.filter(section=section).update(subcategory="electricity_supply")


def test_electricity_consumption_uses_its_own_source_within_a_combined_cost(coverage, client):
    candidate, properties = coverage["candidate"]([(coverage["houses"][0], "electricity", "")])
    section = candidate.sections[0]
    section.usages = [
        CandidateUsage(
            "electricity", "kWh", 12000, section.start, section.end, "measured", EVIDENCE, source
        )
        for source in ("electricity_network", "electricity_supply")
    ]
    invoice = Invoice.objects.create(document=coverage["document"](candidate.reference))
    approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=coverage["vendor"],
        section_properties=properties,
        actor=coverage["actor"],
    )
    assert invoice.sections.get().subcategory_id is None
    assert invoice.sections.get().usages.count() == 2
    report = read_report()
    assert report["consumption"][0]["quantity_milli"] == 12000
    assert sum(row["gross_minor"] for row in report["costs"]) == 12500
    client.force_login(coverage["actor"])
    path, data = review_data(client, invoice.document)
    assert data["usages-0-subcategory"] == "electricity_network"
    assert data["usages-1-subcategory"] == "electricity_supply"
    assert client.post(path, data).status_code == 302
    assert read_report()["consumption"][0]["quantity_milli"] == 12000
    invoice.refresh_from_db()
    history = list(invoice.reviews.values())
    candidate.sections[0].usages.append(deepcopy(section.usages[0]))
    with pytest.raises(ValidationError):
        approve_review(
            invoice.pk,
            expected_revision=invoice.review_revision,
            candidate=candidate,
            vendor=coverage["vendor"],
            section_properties=properties,
            actor=coverage["actor"],
        )
    assert list(invoice.reviews.values()) == history
    assert read_report()["consumption"][0]["quantity_milli"] == 12000


def test_direct_overlaps_and_schedule_forms_share_validation(coverage):
    service = coverage["schedule"]()
    assignment = service.vendor_assignments.get()
    with pytest.raises(ValidationError, match="must not overlap"):
        ServiceVendorAssignment.objects.create(
            property_service=service,
            vendor=assignment.vendor,
            start_month=date(2024, 3, 1),
            end_month=date(2024, 4, 1),
        )
    schedule = assignment.vendor.billing_schedules.get()
    data = {
        "vendor": assignment.vendor_id,
        "start_month": "2024-03",
        "end_month": "2024-04",
        "first_expected_month": "2024-03",
        "frequency_months": 1,
        "grace_days": 0,
        "is_active": True,
    }
    form = VendorBillingScheduleForm(data)
    assert not form.is_valid() and "must not overlap" in str(form.errors)
    with pytest.raises(ValidationError, match="must not overlap"):
        VendorBillingSchedule.objects.create(
            vendor=assignment.vendor,
            start_month=date(2024, 3, 1),
            first_expected_month=date(2024, 3, 1),
        )
    with transaction.atomic(), pytest.raises(IntegrityError):
        VendorBillingSchedule.objects.bulk_create(
            [
                VendorBillingSchedule(
                    vendor=assignment.vendor,
                    start_month=date(2024, 3, 1),
                    first_expected_month=date(2024, 3, 1),
                )
            ]
        )
    assert schedule.vendor.billing_schedules.count() == 1
