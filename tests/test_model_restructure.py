"Acceptance checks for shared bills, independent classification and dated changes."

from copy import deepcopy
from dataclasses import replace
from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.models.deletion import ProtectedError
from django.urls import reverse

from bills.configuration import proposed_configuration, validate_configuration
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


def admin_form_data(page):
    forms = [page.context["adminform"].form]
    for inline in page.context["inline_admin_formsets"]:
        forms.extend([inline.formset.management_form, *inline.formset.initial_forms])
    data = {
        field.html_name: field.value() if field.value() is not None else ""
        for form in forms
        for field in form
    }
    for inline in page.context["inline_admin_formsets"]:
        data[f"{inline.formset.prefix}-TOTAL_FORMS"] = inline.formset.initial_form_count()
    return data


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
    plastic = Subcategory.objects.get(key="plastic")
    plastic.name, plastic.is_active = "Plastic", True
    plastic.save()
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


@pytest.mark.parametrize(
    "target,changes",
    [
        ("service", {"is_active": False}),
        ("service", {"mode": "occasional"}),
        ("assignment", {"is_active": False}),
        ("schedule", {"is_active": False}),
        ("schedule", {"frequency_months": 3}),
    ],
)
def test_historical_annotations_survive_configuration_changes(coverage, target, changes):
    service = coverage["schedule"]()
    annotation = ServiceInvoiceException.objects.create(
        property_service=service,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic historical no-invoice annotation",
    )
    before = ServiceInvoiceException.objects.values().get(pk=annotation.pk)
    assignment = service.vendor_assignments.get()
    row = {
        "service": service,
        "assignment": assignment,
        "schedule": assignment.vendor.billing_schedules.get(),
    }[target]
    for field, value in changes.items():
        setattr(row, field, value)
    row.save()
    validate_configuration(proposed_configuration())
    assert ServiceInvoiceException.objects.values().get(pk=annotation.pk) == before

    annotation.reason = "Synthetic corrected historical explanation"
    annotation.save()
    annotation.refresh_from_db()
    assert annotation.reason == "Synthetic corrected historical explanation"

    # History does not authorize new annotations or changes to their original scope.
    with pytest.raises(ValidationError, match="scheduled invoice month"):
        ServiceInvoiceException.objects.create(
            property_service=service,
            vendor=coverage["vendor"],
            month=date(2024, 4, 1),
            reason="Synthetic invalid new annotation",
        )
    annotation.month = date(2024, 4, 1)
    with pytest.raises(ValidationError, match="scheduled invoice month"):
        annotation.save()
    with transaction.atomic(), pytest.raises(IntegrityError):
        ServiceInvoiceException.objects.filter(pk=annotation.pk).update(month=date(2024, 4, 1))
    with transaction.atomic(), pytest.raises(IntegrityError):
        ServiceInvoiceException.objects.bulk_create(
            [
                ServiceInvoiceException(
                    property_service=service,
                    vendor=coverage["vendor"],
                    month=date(2024, 4, 1),
                    reason="Synthetic invalid SQL annotation",
                )
            ]
        )
    annotation.refresh_from_db()
    assert annotation.month == date(2024, 3, 1)


@pytest.mark.parametrize("correction", ["month", "vendor"])
def test_corrected_header_preserves_confirmation_without_excusing_missing_bill(
    coverage, correction
):
    service = coverage["schedule"]()
    coverage["schedule"](service="waste", subcategory="tank_emptying", frequency_months=0)
    invoice, candidate, properties = coverage["approve"](
        [(service.property, "waste", "tank_emptying")]
    )
    annotation, _ = mark_invoice_not_sent(
        service.pk,
        invoice.vendor_id,
        date(2024, 3, 1),
        reason="Synthetic review confirms no water allocation",
        reviewed_invoice_id=invoice.pk,
        reviewed_revision=invoice.review_revision,
    )
    before = ServiceInvoiceException.objects.values().get(pk=annotation.pk)
    assert read(vendor_id=annotation.vendor_id)["received_count"] == 1
    corrected = deepcopy(candidate)
    vendor = invoice.vendor
    if correction == "month":
        corrected.invoice_date = date(2024, 4, 5)
    else:
        vendor = Vendor.objects.create(name="Synthetic Corrected Vendor", slug="corrected-vendor")
    approve_review(
        invoice.pk,
        expected_revision=invoice.review_revision,
        candidate=corrected,
        vendor=vendor,
        section_properties=properties,
        actor=coverage["actor"],
    )
    PropertyService.objects.create(
        property=coverage["houses"][1], category="other", start_month=date(2024, 1, 1)
    )
    validate_configuration(proposed_configuration())
    assert ServiceInvoiceException.objects.values().get(pk=annotation.pk) == before
    row = next(
        row
        for row in read(vendor_id=annotation.vendor_id)["coverage_rows"]
        if row["expected_month"] == annotation.month
    )
    assert row["status"] == row["required_services"][0]["status"] == "missing"
    assert row["required_services"][0]["exception_id"] == annotation.pk
    with connection.cursor() as cursor:
        bill_status = cursor.execute(
            "SELECT status FROM reporting_invoice_coverage WHERE vendor_id=%s "
            "AND expected_month='2024-03-01'",
            [annotation.vendor_id],
        ).fetchall()
        service_status = cursor.execute(
            "SELECT status, exception_id FROM reporting_service_coverage "
            "WHERE property_service_id=%s AND expected_month='2024-03-01'",
            [service.pk],
        ).fetchall()
    assert bill_status == [("missing",)]
    assert service_status == [("missing", annotation.pk)]

    annotation.reason = "Synthetic corrected explanation of the original review"
    annotation.save()
    invoice.refresh_from_db()
    assert invoice.reviews.count() == invoice.review_revision == 2
    annotation.reviewed_revision = invoice.review_revision
    with pytest.raises(ValidationError, match="reviewed invoice must match"):
        annotation.save()
    with transaction.atomic(), pytest.raises(IntegrityError):
        ServiceInvoiceException.objects.filter(pk=annotation.pk).update(reviewed_revision=2)


@pytest.mark.parametrize(
    "inline_permission", [None, "view_servicevendorassignment", "view_serviceinvoiceexception"]
)
def test_restricted_service_editor_cannot_shorten_dates_outside_assignments(
    coverage, client, inline_permission
):
    service = coverage["schedule"]()
    before = ServiceVendorAssignment.objects.values().get(property_service=service)
    actor = coverage["actor"]
    actor.is_staff = True
    actor.save()
    actor.user_permissions.add(Permission.objects.get(codename="change_propertyservice"))
    if inline_permission:
        actor.user_permissions.add(Permission.objects.get(codename=inline_permission))
    client.force_login(actor)
    path = reverse("admin:bills_propertyservice_change", args=[service.pk])
    page = client.get(path)
    data = {
        "property": service.property_id,
        "category": service.category,
        "subcategory": "",
        "mode": service.mode,
        "is_active": "on",
        "start_month": "2024-02",
        "end_month": "2024-03",
    }
    for inline in page.context["inline_admin_formsets"]:
        formset = inline.formset
        data.update(
            {
                f"{formset.prefix}-TOTAL_FORMS": formset.total_form_count(),
                f"{formset.prefix}-INITIAL_FORMS": formset.initial_form_count(),
            }
        )
        for index, form in enumerate(formset.forms):
            if form.instance.pk:
                data[f"{formset.prefix}-{index}-id"] = form.instance.pk
    response = client.post(path, data)
    assert response.status_code == 200
    assert b"within the service tracking dates" in response.content
    service.refresh_from_db()
    assert service.end_month == date(2024, 4, 1)

    response = client.post(path, {**data, "end_month": "2024-04", "mode": "occasional"})
    assert response.status_code == 302
    service.refresh_from_db()
    assert service.mode == "occasional"
    assert ServiceVendorAssignment.objects.values().get(property_service=service) == before


def test_deleted_inline_errors_do_not_skip_remaining_configuration_validation(
    coverage, client, admin_user
):
    service = coverage["schedule"]()
    assignment = service.vendor_assignments.get()
    annotation = ServiceInvoiceException.objects.create(
        property_service=service,
        vendor=coverage["vendor"],
        month=date(2024, 3, 1),
        reason="Synthetic annotation to remove",
    )
    before = {
        model: list(model.objects.order_by("pk").values())
        for model in (PropertyService, ServiceVendorAssignment, ServiceInvoiceException)
    }
    client.force_login(admin_user)
    path = reverse("admin:bills_propertyservice_change", args=[service.pk])
    data = admin_form_data(client.get(path))
    data.update(
        {
            "end_month": "2024-03",
            "invoice_exceptions-0-reason": "",
            "invoice_exceptions-0-DELETE": "on",
        }
    )
    response = client.post(path, data)
    assert response.status_code == 200
    assert b"within the service tracking dates" in response.content
    for model, rows in before.items():
        assert list(model.objects.order_by("pk").values()) == rows

    # A deleted row's field errors must not block an otherwise valid edit.
    response = client.post(path, {**data, "vendor_assignments-0-end_month": "2024-03"})
    assert response.status_code == 302
    service.refresh_from_db()
    assignment.refresh_from_db()
    assert service.end_month == assignment.end_month == date(2024, 3, 1)
    assert assignment.is_active
    assert not ServiceInvoiceException.objects.filter(pk=annotation.pk).exists()
    validate_configuration(proposed_configuration())


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"end_month": "2024-01"}, "end_month"),
        ({"category": "waste", "subcategory": "electricity_network"}, "subcategory"),
    ],
)
def test_invalid_parent_configuration_renders_field_errors(
    coverage, client, admin_user, changes, field
):
    service = coverage["schedule"]()
    before = PropertyService.objects.values().get(pk=service.pk)
    before_assignments = list(ServiceVendorAssignment.objects.values())
    client.force_login(admin_user)
    path = reverse("admin:bills_propertyservice_change", args=[service.pk])
    data = admin_form_data(client.get(path))
    response = client.post(path, {**data, **changes})
    assert response.status_code == 200
    assert field in response.context["adminform"].form.errors
    assert PropertyService.objects.values().get(pk=service.pk) == before
    assert list(ServiceVendorAssignment.objects.values()) == before_assignments


@pytest.mark.parametrize("period", ["schedule", "assignment"])
def test_adjacent_inline_period_edits_preserve_identities_and_enabled_flags(
    coverage, client, admin_user, period
):
    service = coverage["schedule"](end_month=date(2024, 12, 1))
    vendor = coverage["vendor"]
    annotation = ServiceInvoiceException.objects.create(
        property_service=service,
        vendor=vendor,
        month=date(2024, 3, 1),
        reason="Synthetic retained no-invoice annotation",
    )
    before_annotation = ServiceInvoiceException.objects.values().get(pk=annotation.pk)
    if period == "schedule":
        first = vendor.billing_schedules.get()
        second = change_billing_frequency(
            first.pk,
            month=date(2024, 7, 1),
            frequency_months=3,
            first_expected_month=date(2024, 7, 1),
            grace_days=5,
        )
        prefix = "billing_schedules"
        path = reverse("admin:bills_vendor_change", args=[vendor.pk])
    else:
        first = service.vendor_assignments.get()
        first.end_month = date(2024, 6, 1)
        first.save()
        second = ServiceVendorAssignment.objects.create(
            property_service=service,
            vendor=vendor,
            start_month=date(2024, 7, 1),
            end_month=service.end_month,
        )
        prefix = "vendor_assignments"
        path = reverse("admin:bills_propertyservice_change", args=[service.pk])
    client.force_login(admin_user)
    data = admin_form_data(client.get(path))
    data[f"{prefix}-0-end_month"] = "2024-07"
    data[f"{prefix}-1-start_month"] = "2024-08"
    if period == "schedule":
        data[f"{prefix}-1-first_expected_month"] = "2024-08"
    response = client.post(path, data)
    assert response.status_code == 302
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.end_month == date(2024, 7, 1)
    assert second.start_month == date(2024, 8, 1)
    assert first.is_active and second.is_active
    assert set(type(first).objects.values_list("pk", flat=True)) == {first.pk, second.pk}
    assert ServiceInvoiceException.objects.values().get(pk=annotation.pk) == before_annotation
    if period == "schedule":
        assert second.first_expected_month == date(2024, 8, 1)
        assert (second.frequency_months, second.grace_days) == (3, 5)
        assert {row["expected_month"].month for row in read()["coverage_rows"]} == {
            2,
            3,
            4,
            5,
            6,
            7,
            8,
            11,
        }
    validate_configuration(proposed_configuration())


@pytest.mark.parametrize("action", ["vendor", "frequency"])
def test_dated_change_validation_returns_feedback_and_preserves_history(coverage, client, action):
    service = coverage["schedule"]()
    assignment = service.vendor_assignments.get()
    before_assignments = list(ServiceVendorAssignment.objects.values())
    before_schedules = list(VendorBillingSchedule.objects.values())
    actor = coverage["actor"]
    actor.is_staff = actor.is_superuser = True
    actor.save()
    client.force_login(actor)
    if action == "vendor":
        replacement = Vendor.objects.create(name="Synthetic Replacement Vendor", slug="replacement")
        path = reverse("admin:bills_servicevendorassignment_change_vendor", args=[assignment.pk])
        data = {"month": "2024-02", "vendor": replacement.pk}
        message = "The last month must not precede the first."
    else:
        schedule = assignment.vendor.billing_schedules.get()
        path = reverse("admin:bills_vendorbillingschedule_change_frequency", args=[schedule.pk])
        data = {
            "month": "2024-03",
            "first_expected_month": "2024-02",
            "frequency_months": 3,
            "grace_days": 0,
        }
        message = "The first expected month must not precede the effective first month."
    response = client.post(path, data)
    assert response.status_code == 200
    assert message in response.context["form"].non_field_errors()
    assert list(ServiceVendorAssignment.objects.values()) == before_assignments
    assert list(VendorBillingSchedule.objects.values()) == before_schedules


@pytest.mark.parametrize("cost_source", ["", "electricity_network", "electricity_supply"])
@pytest.mark.parametrize("usage_source", ["", "solar"])
def test_unspecified_electricity_source_is_reviewed_independently_of_cost(
    coverage, cost_source, usage_source
):
    if usage_source:
        Subcategory.objects.create(key=usage_source, category="electricity", name="Synthetic Solar")
    candidate, properties = coverage["candidate"](
        [(coverage["houses"][0], "electricity", cost_source)]
    )
    section = candidate.sections[0]
    section.usages = [
        CandidateUsage(
            "electricity",
            "kWh",
            12000,
            section.start,
            section.end,
            "measured",
            EVIDENCE,
            usage_source,
        )
    ]
    invoice = Invoice.objects.create(document=coverage["document"](candidate.reference))
    invoice = approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=coverage["vendor"],
        section_properties=properties,
        actor=coverage["actor"],
    )
    allocation = invoice.sections.get()
    assert allocation.subcategory_id == (cost_source or None)
    assert allocation.usages.get().subcategory_id == (usage_source or None)
    report = read_report()
    assert sum(row["gross_minor"] for row in report["costs"]) == 12500
    assert report["consumption"] == []
    assert [row["selection_reason"] for row in report["issues"]] == ["unspecified_source"]
    with connection.cursor() as cursor:
        selection = cursor.execute(
            "SELECT source_service, subcategory, source_selection, selection_reason "
            "FROM reporting_electricity_selection WHERE invoice_id=%s",
            [invoice.pk],
        ).fetchall()
    assert selection == [("electricity", usage_source, "needs_review", "unspecified_source")]

    section.usages[0] = replace(section.usages[0], subcategory="electricity_network")
    approve_review(
        invoice.pk,
        expected_revision=invoice.review_revision,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties=properties,
        actor=coverage["actor"],
    )
    corrected = read_report()
    assert corrected["consumption"][0]["quantity_milli"] == 12000
    assert corrected["issues"] == []
    assert corrected["costs"] == report["costs"]
