"""Independent invoice facts and cost presence on shared vendor bills."""

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811

from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.urls import reverse

from bills.configuration import cost_is_expected, proposed_configuration, validate_configuration
from bills.configuration_actions import change_billing_frequency, change_cost_pattern, change_vendor
from bills.invoice_coverage import mark_invoice_not_sent, read_invoice_coverage
from bills.models import (
    BillingSection,
    Document,
    PropertyService,
    ServiceCostSchedule,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Subcategory,
    Vendor,
    VendorBillingSchedule,
)
from bills.reporting import read_report, reporting_reader

from .test_invoice_coverage import coverage  # noqa: F401
from .test_reporting import allocation, ledger, usage  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)
FEB, MAR, APR = (date(2024, month, 1) for month in (2, 3, 4))


def pattern(service, **changes):
    assignment = service.vendor_assignments.get()
    return ServiceCostSchedule.objects.create(
        service_vendor_assignment=assignment,
        **{
            "start_month": assignment.start_month,
            "end_month": assignment.end_month,
            "frequency_months": 2,
            "first_expected_month": MAR,
            **changes,
        },
    )


def bills(**changes):
    return read_invoice_coverage(
        **{"date_from": FEB, "date_to": APR, "as_of": date(2024, 5, 1), **changes},
    )["coverage_rows"]


@pytest.mark.parametrize(
    "quantities", [(12000, 8000), (12000, None), (None, 8000), (None, None), (0, 0)]
)
def test_combined_cost_and_optional_weights_remain_independent(ledger, client, quantities):
    house = ledger["houses"][0]
    weights = [
        usage(quantity, utility="waste", basis="billed_weight", subcategory=fraction)
        for quantity, fraction in zip(quantities, ("matavfall", "restavfall"), strict=True)
        if quantity is not None
    ]
    invoice = ledger["make"](
        [
            allocation(
                house, service="waste", subcategory="waste_collection", net=16000, usages=weights
            ),
            allocation(house, service="waste", subcategory="tradgardsavfall", net=4000),
        ],
        source=Document.Source.MANUAL,
    )
    report = read_report()
    assert report["waste_total"]["gross_minor"] == 25000
    assert report["waste_totals_by_property"][0]["gross_minor"] == 25000
    assert report["waste_general"][0]["cost"]["gross_minor"] == 20000
    assert report["waste_general"][0]["weights"] == []
    fractions = {row["fraction"]: row for row in report["waste_fractions"]}
    assert fractions["tradgardsavfall"]["cost"]["gross_minor"] == 5000
    assert fractions["tradgardsavfall"]["weights"] == []
    for fraction, quantity in zip(("matavfall", "restavfall"), quantities, strict=True):
        if quantity is None:
            assert fraction not in fractions
        else:
            assert fractions[fraction]["cost"] is None
            assert fractions[fraction]["weights"][0]["quantity_milli"] == quantity
    detail = next(
        row for row in report["waste_details"] if row["subcategory"] == "waste_collection"
    )
    assert len(detail["weights"]) == len(weights)
    assert all(row["invoice_id"] == invoice.pk for row in detail["weights"])
    client.force_login(ledger["actor"])
    page = client.get(reverse("bills:reports"))
    assert page.status_code == 200
    assert b"Total waste cost: 250 SEK" in page.content
    assert b"General / combined waste" in page.content
    assert b"Not reported" in page.content
    if quantities == (0, 0):
        assert b"0,000 kg" in page.content


def test_all_fraction_costs_general_tank_and_inactive_facts_reconcile(ledger):
    first, second = ledger["houses"]
    keys = ("matavfall", "restavfall", "tradgardsavfall", "plastic", "cardboard", "glass")
    specifications = [allocation(first, service="waste", subcategory=key, net=100) for key in keys]
    Subcategory.objects.create(key="legacy_fee", category="waste", name="Legacy fee")
    specifications += [
        allocation(first, service="waste", subcategory="legacy_fee", net=100),
        allocation(first, service="waste", subcategory="tank_emptying", net=100),
        allocation(second, service="waste", net=100),
    ]
    ledger["make"](specifications, source=Document.Source.MANUAL)
    report = read_report()
    assert {row["fraction"] for row in report["waste_fractions"]} == set(keys)
    assert {row["subcategory"] for row in report["waste_general"]} == {"legacy_fee", ""}
    assert len(report["waste_other_services"]) == 1
    entries = report["waste_fractions"] + report["waste_general"] + report["waste_other_services"]
    assert (
        report["waste_total"]["gross_minor"]
        == sum(row["cost"]["gross_minor"] for row in entries)
        == 1125
    )
    assert read_report(property_id=second.pk)["waste_total"]["gross_minor"] == 125
    assert all(
        not Subcategory.objects.get(pk=key).is_active for key in ("plastic", "cardboard", "glass")
    )


def test_signed_weights_retain_their_basis_and_unknown_costs_remain_unknown(ledger):
    house = ledger["houses"][0]
    invoice = ledger["make"](
        [
            allocation(
                house,
                service="waste",
                subcategory="matavfall",
                net=100,
                usages=[
                    usage(12000, utility="waste", subcategory="matavfall", basis="billed_weight"),
                    usage(-2000, utility="waste", subcategory="matavfall", basis="reversal"),
                ],
            ),
        ],
        source=Document.Source.MANUAL,
    )
    row = read_report()["waste_fractions"][0]
    assert {(w["basis"], w["quantity_milli"]) for w in row["weights"]} == {
        ("billed_weight", 12000),
        ("reversal", -2000),
    }
    # Older approved sections may retain incomplete monetary facts.
    BillingSection.objects.filter(invoice=invoice).update(
        net_minor=None, vat_minor=None, gross_minor=None
    )
    report = read_report()
    assert report["waste_total"]["gross_minor"] is None
    assert report["waste_fractions"][0]["cost"]["gross_minor"] is None


def test_report_weights_follow_invoice_dates_and_configuration_never_creates_quantities(ledger):
    house = ledger["houses"][0]
    ledger["make"](
        [
            allocation(
                house,
                service="waste",
                subcategory="waste_collection",
                usages=[
                    usage(125, utility="waste", basis="billed_weight", subcategory="matavfall"),
                ],
            )
        ],
        source=Document.Source.MANUAL,
    )
    PropertyService.objects.create(
        property=house, category="waste", subcategory="restavfall", start_month=FEB
    )
    feb = read_report(date_from=FEB, date_to=date(2024, 2, 29))
    assert feb["waste_total"]["gross_minor"] == 0
    assert feb["waste_general"] == []
    assert feb["waste_fractions"][0]["cost"] is None and feb["waste_fractions"][0]["weights"] == []
    assert feb["consumption"][0]["quantity_milli"] == 125
    march = read_report(date_from=MAR, date_to=date(2024, 3, 31))
    assert march["consumption"] == []
    assert (
        next(row for row in march["waste_fractions"] if row["fraction"] == "matavfall")["weights"][
            0
        ]["quantity_milli"]
        == 125
    )


def test_periodic_costs_filter_shared_bills_and_do_not_suppress_receipts(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    configured = pattern(service)
    rows = bills()
    assert len(rows) == 3 and {row["status"] for row in rows} == {"missing"}
    for row in rows:
        required = row["expected_month"] == MAR
        assert len(row["required_services"]) == int(required)
        assert len(row["optional_services"]) == int(not required)
        assert (row["required_services"] or row["optional_services"])[0][
            "cost_schedule_id"
        ] == configured.pk
    # A real bill without this optional cost is complete in February.
    coverage["approve"]([(service.property, "water", "")], invoice_date=date(2024, 2, 5))
    assert bills(date_from=FEB, date_to=FEB)[0]["status"] == "received"
    # The same omission requires a direct cost allocation when due in March.
    coverage["approve"]([(service.property, "water", "")])
    assert bills(date_from=MAR, date_to=MAR)[0]["status"] == "incomplete"


@pytest.mark.parametrize(("start", "end", "due"), [(3, 3, {MAR}), (11, 2, {FEB}), (4, 10, {APR})])
def test_billing_seasons_include_wrapping_seasons_and_match_sql(coverage, start, end, due):
    service = coverage["schedule"](service="waste", subcategory="tradgardsavfall")
    pattern(
        service,
        frequency_months=1,
        first_expected_month=FEB,
        season_start_month=start,
        season_end_month=end,
    )
    report = bills()
    assert {row["expected_month"] for row in report if row["required_services"]} == due
    with reporting_reader() as reader:
        sql = [
            dict(row)
            for row in reader.execute(
                "SELECT * FROM reporting_service_coverage "
                "WHERE expected_month BETWEEN '2024-02-01' AND '2024-04-01'"
            )
        ]
        shared = [
            dict(row)
            for row in reader.execute(
                "SELECT * FROM reporting_invoice_coverage "
                "WHERE expected_month BETWEEN '2024-02-01' AND '2024-04-01'"
            )
        ]
    assert {date.fromisoformat(row["expected_month"]) for row in sql if row["cost_required"]} == due
    assert len(shared) == 3 and all(row["status"] == "missing" for row in shared)


def test_occasional_optional_costs_join_other_services_shared_bill(coverage):
    coverage["schedule"](service="water")
    service = coverage["schedule"](
        service="waste", subcategory="tradgardsavfall", mode="occasional"
    )
    pattern(service, frequency_months=0, first_expected_month=None)
    assert all(
        len(row["required_services"]) == len(row["optional_services"]) == 1 for row in bills()
    )
    with reporting_reader() as reader:
        assert (
            reader.execute(
                "SELECT optional_service_count FROM reporting_invoice_coverage "
                "WHERE expected_month='2024-03-01'"
            ).fetchone()[0]
            == 1
        )
    coverage["approve"](
        [(service.property, "water", ""), (service.property, "waste", "tradgardsavfall")]
    )
    row = bills(date_from=MAR, date_to=MAR)[0]
    assert row["status"] == row["optional_services"][0]["status"] == "received"


@pytest.mark.parametrize("filtered_by", ["service", "property", "both"])
@pytest.mark.parametrize("received", [False, True])
def test_filters_preserve_shared_bills_with_only_optional_displayed_costs(
    coverage, filtered_by, received
):
    coverage["schedule"](coverage["houses"][0], "water")
    service = coverage["schedule"](
        coverage["houses"][1], "waste", subcategory="tradgardsavfall", mode="occasional"
    )
    pattern(service, frequency_months=0, first_expected_month=None)
    if received:
        coverage["approve"](
            [(coverage["houses"][0], "water", ""), (service.property, "waste", "tradgardsavfall")]
        )
    filters = {}
    if filtered_by in ("service", "both"):
        filters["service"] = "waste"
    if filtered_by in ("property", "both"):
        filters["property_id"] = service.property_id
    unfiltered = bills(date_from=MAR, date_to=MAR)[0]
    report = read_invoice_coverage(date_from=MAR, date_to=MAR, as_of=date(2024, 5, 1), **filters)
    (row,) = report["coverage_rows"]
    assert row["vendor_id"] == unfiltered["vendor_id"]
    assert row["status"] == unfiltered["status"] == ("received" if received else "missing")
    assert row["required_services"] == []
    assert row["optional_services"] == unfiltered["optional_services"]
    assert [item["id"] for item in report["property_services"]] == [service.pk]


def test_periodic_pattern_overrides_occasional_mode_and_fallback_dates(coverage):
    Subcategory.objects.filter(pk="plastic").update(is_active=True)
    service = coverage["schedule"](service="waste", subcategory="plastic", mode="occasional")
    pattern(service, start_month=MAR, end_month=MAR, frequency_months=1, first_expected_month=MAR)
    assert [row["expected_month"] for row in bills()] == [MAR]
    assert bills()[0]["required_services"][0]["subcategory"] == "plastic"


def test_weights_cannot_satisfy_a_due_fraction_cost_and_zero_cost_can(ledger):
    house = ledger["houses"][0]
    vendor = ledger["vendor"]
    service = PropertyService.objects.create(
        property=house, category="waste", subcategory="matavfall", start_month=FEB, end_month=APR
    )
    ServiceVendorAssignment.objects.create(
        property_service=service, vendor=vendor, start_month=FEB, end_month=APR
    )
    VendorBillingSchedule.objects.create(vendor=vendor, start_month=FEB, first_expected_month=FEB)
    pattern(service, frequency_months=1, first_expected_month=FEB)
    ledger["make"](
        [
            allocation(
                house,
                service="waste",
                subcategory="waste_collection",
                usages=[
                    usage(12000, utility="waste", subcategory="matavfall", basis="billed_weight")
                ],
            )
        ],
        source=Document.Source.MANUAL,
    )
    assert bills(date_from=MAR, date_to=MAR)[0]["status"] == "incomplete"
    ledger["make"](
        [allocation(house, service="waste", subcategory="matavfall", net=0)],
        source=Document.Source.MANUAL,
    )
    assert bills(date_from=MAR, date_to=MAR)[0]["status"] == "received"


def test_exceptions_use_cost_due_months_and_vendor_receipt_can_be_not_sent(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    pattern(service)
    with pytest.raises(ValidationError):
        ServiceInvoiceException.objects.create(
            property_service=service, vendor=coverage["vendor"], month=FEB, reason="Cost not due"
        )
    mark_invoice_not_sent(None, coverage["vendor"].pk, FEB, reason="Vendor confirms no bill")
    assert bills(date_from=FEB, date_to=FEB)[0]["status"] == "not_expected"
    scoped, _ = mark_invoice_not_sent(
        service.pk, coverage["vendor"].pk, MAR, reason="Cost not billed"
    )
    assert bills(date_from=MAR, date_to=MAR)[0]["status"] == "not_expected"
    changed = change_cost_pattern(
        service.vendor_assignments.get().cost_schedules.get().pk, month=MAR, frequency_months=0
    )
    scoped.reason = "Retained history"
    scoped.save()
    assert changed.frequency_months == 0 and scoped.reason == "Retained history"


def test_vendor_not_sent_reason_survives_when_all_costs_are_optional(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    pattern(service)
    reason = "Synthetic vendor confirms no February bill"
    mark_invoice_not_sent(None, coverage["vendor"].pk, FEB, reason=reason)
    (row,) = bills(date_from=FEB, date_to=FEB)
    assert row["status"] == "not_expected" and row["exception_reason"] == reason
    assert row["required_services"] == []
    assert row["optional_services"][0]["status"] == "not_due"
    with reporting_reader() as reader:
        assert (
            reader.execute(
                "SELECT status FROM reporting_service_coverage WHERE expected_month='2024-02-01'"
            ).fetchone()[0]
            == "not_due"
        )
        # The documented no-invoice query must retain the vendor's explanation.
        rows = reader.execute(
            "SELECT expected_month, vendor_name, exception_reason "
            "FROM reporting_invoice_coverage WHERE status='not_expected' "
            "ORDER BY expected_month, vendor_name"
        ).fetchall()
    assert [tuple(row) for row in rows] == [(FEB.isoformat(), coverage["vendor"].name, reason)]


@pytest.mark.parametrize(
    "changes",
    [
        {"start_month": date(2024, 1, 1)},
        {"start_month": date(2024, 2, 2)},
        {"end_month": date(2024, 5, 1)},
        {"first_expected_month": None},
        {"frequency_months": 0},
        {"season_start_month": 4},
        {"season_start_month": 0, "season_end_month": 13},
    ],
)
def test_cost_pattern_dates_anchor_and_seasons_are_validated(coverage, changes):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    with pytest.raises(ValidationError):
        pattern(service, **changes)
    assert not ServiceCostSchedule.objects.exists()


def test_cost_alignment_and_parent_changes_are_validated_by_model_and_sql(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall", frequency_months=2)
    with pytest.raises(ValidationError, match="align"):
        pattern(service)  # March is not one of the vendor's February/April bills.
    configured = pattern(service, first_expected_month=FEB)
    with pytest.raises(ValidationError, match="overlap"):
        pattern(service, first_expected_month=FEB)
    with pytest.raises(IntegrityError), transaction.atomic():
        ServiceCostSchedule.objects.filter(pk=configured.pk).update(frequency_months=1)
    schedule = coverage["vendor"].billing_schedules.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        VendorBillingSchedule.objects.filter(pk=schedule.pk).update(frequency_months=3)
    with pytest.raises(ValidationError):
        change_billing_frequency(
            schedule.pk, month=MAR, frequency_months=3, first_expected_month=MAR, grace_days=0
        )
    schedule.refresh_from_db()
    assert schedule.end_month is None
    assignment = service.vendor_assignments.get()
    with pytest.raises(IntegrityError), transaction.atomic():
        ServiceVendorAssignment.objects.filter(pk=assignment.pk).update(end_month=MAR)


def test_pattern_replacement_retains_lineage_and_vendor_change_closes_children(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    first = pattern(service, frequency_months=1, first_expected_month=FEB)
    second = change_cost_pattern(first.pk, month=MAR, frequency_months=2, first_expected_month=MAR)
    first.refresh_from_db()
    assert first.end_month == FEB and second.predecessor_id == first.pk
    third = change_cost_pattern(second.pk, month=APR, frequency_months=0)
    assert third.predecessor_id == second.pk
    assignment = service.vendor_assignments.get()
    new_vendor = Vendor.objects.create(name="Replacement", slug="replacement")
    replacement = change_vendor(assignment.pk, month=APR, vendor=new_vendor, transition_month=True)
    for child in assignment.cost_schedules.all():
        assert child.end_month <= APR
    assert replacement.cost_schedules.count() == 0
    assert service.pk == replacement.property_service_id


def test_unsaved_assignments_keep_independent_patterns_in_staged_configuration(coverage):
    service = coverage["schedule"](service="waste", subcategory="restavfall")
    other = PropertyService(
        property=service.property,
        category="waste",
        subcategory="matavfall",
        start_month=FEB,
        end_month=APR,
    )
    one = ServiceVendorAssignment(
        property_service=other, vendor=coverage["vendor"], start_month=FEB, end_month=MAR
    )
    two = ServiceVendorAssignment(
        property_service=other, vendor=coverage["vendor"], start_month=APR, end_month=APR
    )
    optional = ServiceCostSchedule(
        service_vendor_assignment=one, start_month=FEB, end_month=MAR, frequency_months=0
    )
    assert not cost_is_expected(other, one, FEB, [optional])
    assert cost_is_expected(other, two, APR, [optional])
    validate_configuration(proposed_configuration([other, one, two, optional]))


def test_fraction_kind_is_stable_and_group_defaults_include_garden(coverage):
    service = coverage["schedule"](service="waste", subcategory="tradgardsavfall")
    assert service.service_group == "waste_collection"
    fraction = Subcategory.objects.get(pk="tradgardsavfall")
    fraction.is_waste_fraction = False
    with pytest.raises(ValidationError, match="referenced subcategory"):
        fraction.save()
    with pytest.raises(IntegrityError), transaction.atomic():
        Subcategory.objects.filter(pk=fraction.pk).update(is_waste_fraction=False)
    for key in ("waste_collection", "tank_emptying", "electricity_network"):
        with pytest.raises(IntegrityError), transaction.atomic():
            Subcategory.objects.filter(pk=key).update(is_waste_fraction=True)
    service.delete()
    assert not ServiceVendorAssignment.objects.exists()
