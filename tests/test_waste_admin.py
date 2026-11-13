"""Cost-pattern editors validate unsaved parents and commit child periods atomically."""

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811

from datetime import date

import pytest
from django.urls import reverse

from bills.models import ServiceCostSchedule, ServiceVendorAssignment

from .test_configuration import admin_user  # noqa: F401
from .test_invoice_coverage import coverage  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def assignment_data(service, vendor, *, assignment=None, pattern=None):
    data = {
        "property_service": service.pk,
        "vendor": vendor.pk,
        "is_active": "on",
        "start_month": "2024-02",
        "end_month": "2024-04",
        "_save": "Save",
        "cost_schedules-TOTAL_FORMS": 1,
        "cost_schedules-INITIAL_FORMS": int(pattern is not None),
        "cost_schedules-0-frequency_months": 1,
        "cost_schedules-0-is_active": "on",
        "cost_schedules-0-start_month": "2024-02",
        "cost_schedules-0-end_month": "2024-04",
        "cost_schedules-0-first_expected_month": "2024-02",
    }
    if assignment:
        data["cost_schedules-0-service_vendor_assignment"] = assignment.pk
    if pattern:
        data["cost_schedules-0-id"] = pattern.pk
    return data


def test_add_cost_pattern_inline_with_unsaved_assignment(coverage, admin_user, client):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    service.vendor_assignments.all().delete()
    client.force_login(admin_user)
    response = client.post(
        reverse("admin:bills_servicevendorassignment_add"),
        assignment_data(service, coverage["vendor"]),
    )
    assert response.status_code == 302, response.content
    pattern = ServiceCostSchedule.objects.get()
    assert pattern.service_vendor_assignment.property_service_id == service.pk
    assert pattern.first_expected_month == date(2024, 2, 1)


def test_inline_shrinking_parent_and_child_dates_commits_the_checked_graph(
    coverage, admin_user, client
):
    service = coverage["schedule"](service="waste", subcategory="tradgardsavfall")
    assignment = service.vendor_assignments.get()
    pattern = ServiceCostSchedule.objects.create(
        service_vendor_assignment=assignment,
        start_month=assignment.start_month,
        end_month=assignment.end_month,
        frequency_months=1,
        first_expected_month=assignment.start_month,
    )
    data = assignment_data(service, coverage["vendor"], assignment=assignment, pattern=pattern)
    data["end_month"] = data["cost_schedules-0-end_month"] = "2024-03"
    client.force_login(admin_user)
    response = client.post(
        reverse("admin:bills_servicevendorassignment_change", args=[assignment.pk]), data
    )
    assert response.status_code == 302, response.content
    assignment.refresh_from_db()
    pattern.refresh_from_db()
    assert assignment.end_month == pattern.end_month == date(2024, 3, 1)
    assert assignment.is_active and pattern.is_active


def test_cost_pattern_dated_editor_preserves_the_assignment(coverage, admin_user, client):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    assignment = service.vendor_assignments.get()
    first = ServiceCostSchedule.objects.create(
        service_vendor_assignment=assignment,
        start_month=assignment.start_month,
        end_month=assignment.end_month,
        frequency_months=1,
        first_expected_month=assignment.start_month,
    )
    before = ServiceVendorAssignment.objects.values().get(pk=assignment.pk)
    client.force_login(admin_user)
    response = client.post(
        reverse("admin:bills_servicecostschedule_change_pattern", args=[first.pk]),
        {"month": "2024-03", "frequency_months": 0},
    )
    assert response.status_code == 302, response.content
    second = ServiceCostSchedule.objects.get(predecessor_id=first.pk)
    assert second.frequency_months == 0 and second.start_month == date(2024, 3, 1)
    assert ServiceVendorAssignment.objects.values().get(pk=assignment.pk) == before
