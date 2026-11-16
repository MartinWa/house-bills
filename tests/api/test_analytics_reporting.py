"""Calendar, correction, coverage and alignment regressions on invented facts."""

from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pytest

from bills.models import (
    BillingSection,
    Invoice,
    PropertyService,
    ServiceVendorAssignment,
    Usage,
    Vendor,
    VendorBillingSchedule,
)
from bills.power_reporting import summarize_billed_power
from bills.reporting import reporting_reader

from ..test_reporting import allocation, demand_allocation, usage
from .helpers import BASE
from .test_reporting_contract import get, records

pytestmark = pytest.mark.django_db(transaction=True)
COMPACT = "representation=compact&include_totals=false"


def water(ledger, year, month, quantity, *, basis="measured", net=10000):  # noqa: F811
    start, end = date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)
    return ledger["make"](
        [
            allocation(
                ledger["houses"][0],
                service="water",
                net=net,
                start=start,
                end=end,
                usages=[usage(quantity, utility="water", basis=basis, start=start, end=end)],
            )
        ],
        invoice_date=date(year, month, 28),
    )


def test_calendar_keys_whole_intervals_signs_and_context(api_client, ledger):  # noqa: F811
    client = api_client[0]
    start, end = date(2024, 12, 15), date(2025, 3, 1)
    for quantity, basis, net in (
        (10000, "estimated", 10000),
        (-10000, "reversal", -10000),
        (7000, "measured", 10000),
    ):
        ledger["make"](
            [
                allocation(
                    ledger["houses"][0],
                    service="water",
                    net=net,
                    start=start,
                    end=end,
                    usages=[usage(quantity, utility="water", basis=basis, start=start, end=end)],
                )
            ],
            invoice_date=date(2025, 3, 5),
            kind="credit" if quantity < 0 else "invoice",
        )
    response = get(
        client,
        "consumption?group_by=period_start_month,period_start_year&unit=m3&"
        + COMPACT
        + "&include_quality=true&date_from=2025-01-01&date_to=2025-01-31",
    )
    group = response["groups"][0]
    assert group["quantity_milli"] == 7000 and group["usage_count"] == 3
    assert group["period_start_month"] == "2024-12-01"
    assert group["period_start_year"] == 2024 and group["period_start_year_time"] == 1704067200
    assert group["coverage"]["last_billed_day"] == "2025-02-28"
    assert group["coverage"]["duration_days_max"] == 76
    assert group["coverage"]["basis_counts"] == {"estimated": 1, "measured": 1, "reversal": 1}
    assert "totals" not in response and "document_id" not in group
    assert response["filters"]["unit"] == "m3"
    assert (
        get(
            client, "consumption?group_by=period_start_year&date_from=2025-03-01&date_to=2025-03-01"
        )["groups"]
        == []
    )


def test_cost_year_compact_unknown_and_distinct_counts(api_client):
    client, house, _, invoices, sections, _ = records(api_client, count=2)
    Invoice.objects.filter(pk=invoices[0].pk).update(invoice_date=date(2025, 12, 31))
    BillingSection.objects.filter(pk=sections[0].pk).update(net_minor=0, vat_minor=0, gross_minor=0)
    BillingSection.objects.filter(pk=sections[1].pk).update(gross_minor=None)
    response = get(
        client, "costs?group_by=invoice_year&currency=SEK&include_waste=false&" + COMPACT
    )
    assert [group["invoice_year"] for group in response["groups"]] == [2025, 2026]
    assert [group["gross_minor"] for group in response["groups"]] == [0, None]
    assert response["groups"][0]["invoice_year_time"] == 1735689600
    assert "waste_fractions" not in response and "property_id" not in response["groups"][0]
    assert get(client, f"costs?group_by=invoice_year&property_id={house['id']}&year=2025&years=1")[
        "effective_date_bounds"
    ] == {"date_from": "2025-01-01", "date_to": "2025-12-31"}


def test_power_statistics_are_complete_and_cancel_exact_credits(api_client):
    client, _, _, invoices, _, usages = records(
        api_client, count=211, utility="electricity", subcategory="electricity_demand"
    )
    for index, amount in enumerate((5000, -5000, 3000, 0, -2000)):
        Usage.objects.filter(pk=usages[index].pk).update(quantity_milli=amount)
        if amount < 0:
            Invoice.objects.filter(pk=invoices[index].pk).update(kind="credit")
    with reporting_reader() as reader:
        native = summarize_billed_power(
            [dict(row) for row in reader.execute("SELECT * FROM reporting_electricity_demand")]
        )[0]
    assert get(client, "billed-power?limit=200")["count"] == 211
    response = get(client, "billed-power?mode=totals")
    group = response["groups"][0]
    assert group["usage_count"] == 211 and group["highest_milli"] == 3000
    assert group["lowest_milli"] == 0 and group["average_milli"] == native["average_milli"]
    assert group["value_count"] == native["value_count"]
    assert group["cancelled_positive_count"] == group["unmatched_credit_count"] == 1
    assert "quantity_milli" not in group
    monthly = get(client, "billed-power?mode=period_start_month")["groups"]
    assert [row["period_start_month"] for row in monthly] == ["2026-01-01", "2026-02-01"]


def test_availability_and_empty_range_are_distinct(api_client, ledger):  # noqa: F811
    client = api_client[0]
    house = ledger["houses"][0]
    ledger["make"](
        [allocation(house, service="waste", subcategory="waste_collection")],
        invoice_date=date(2014, 3, 5),
    )
    water(ledger, 2024, 1, 0)
    availability = get(client, f"availability?property_id={house.pk}")
    assert availability["available_cost_years"] == [2014, 2024]
    metrics = {row["utility"]: row for row in availability["metrics"]}
    assert metrics["waste"]["has_costs"] and metrics["waste"]["quantity_status"] == "not_recorded"
    assert metrics["water"]["has_quantities"] and metrics["water"]["quantity_status"] == "recorded"
    assert get(client, "availability?property_id=999999")["metrics"] == []
    quality = get(
        client,
        f"consumption?group_by=utility,unit&property_id={house.pk}&{COMPACT}&include_quality=true&date_from=2025-01-01&date_to=2025-12-31",
    )["quality"]
    assert {row["utility"]: row["selected_status"] for row in quality["quantities"]} == {
        "water": "empty_range",
        "waste": "not_recorded",
    }


def test_consumption_cutoff_coverage_zero_and_basis(api_client, ledger):  # noqa: F811
    client = api_client[0]
    for year, quantity in ((2023, 0), (2024, 1000)):
        for month in (1, 2):
            water(ledger, year, month, quantity)
    response = get(client, "comparisons?metric=consumption&year=2024&category=water")
    comparison = response["comparisons"][0]
    assert comparison["cutoff"] == "2024-02-29" and comparison["comparison_cutoff"] == "2023-02-28"
    assert comparison["comparable"] and comparison["absolute_change"] == 2000
    assert comparison["percentage_change"] is None and comparison["percentage_reasons"] == [
        "nonpositive_baseline"
    ]
    assert [row["year"] for row in response["annuals"]] == [2022, 2023, 2024]
    Usage.objects.filter(start=date(2024, 1, 1)).delete()
    comparison = get(client, "comparisons?metric=consumption&year=2024&category=water")[
        "comparisons"
    ][0]
    assert not comparison["comparable"] and comparison["absolute_change"] is None
    assert "quantity_coverage_incomplete" in comparison["reasons"]
    water(ledger, 2024, 1, 1000, basis="estimated")
    comparison = get(client, "comparisons?metric=consumption&year=2024&category=water")[
        "comparisons"
    ][0]
    assert "basis_mismatch" in comparison["reasons"]


def test_cost_comparisons_use_expectations_and_unknown_cumulative(api_client, ledger):  # noqa: F811
    client = api_client[0]
    house, vendor = ledger["houses"][0], ledger["vendor"]
    service = PropertyService.objects.create(
        property=house, category="water", start_month=date(2023, 1, 1)
    )
    ServiceVendorAssignment.objects.create(
        property_service=service, vendor=vendor, start_month=date(2023, 1, 1)
    )
    VendorBillingSchedule.objects.create(
        vendor=vendor, start_month=date(2023, 1, 1), first_expected_month=date(2023, 1, 1)
    )
    for year, net in ((2023, 10000), (2024, 12000)):
        for month in (1, 2):
            water(ledger, year, month, 1000, net=net)
    response = get(
        client,
        f"comparisons?metric=costs&year=2024&category=water&property_id={house.pk}&history=true",
    )
    comparison = response["comparisons"][0]
    assert comparison["comparable"] and comparison["absolute_change"] == 5000
    assert Decimal(comparison["percentage_change"]) == 20
    assert comparison["current_coverage"]["expectation_count"] == 2
    BillingSection.objects.filter(invoice__invoice_date=date(2024, 1, 28)).update(gross_minor=None)
    response = get(client, "comparisons?metric=costs&year=2024&category=water")
    assert "unknown_amount" in response["comparisons"][0]["reasons"]
    assert all(
        row["cumulative_gross_minor"] is None for row in response["monthly"] if row["year"] == 2024
    )


def test_unit_costs_aggregate_costs_and_usage_independently(api_client, ledger):  # noqa: F811
    client = api_client[0]
    start, end = date(2024, 1, 1), date(2024, 2, 1)
    ledger["make"](
        [
            allocation(
                ledger["houses"][0],
                service="water",
                start=start,
                end=end,
                usages=[
                    usage(5000, utility="water", start=start, end=end),
                    usage(1000, utility="water", start=start, end=end),
                ],
            )
        ]
    )
    group = get(client, "unit-costs?category=water&date_from=2024-01-01&date_to=2024-01-31")[
        "groups"
    ][0]
    assert group["gross_minor"] == 12500 and group["quantity_milli"] == 6000
    assert group["allocation_count"] == 1 and group["usage_count"] == 2 and group["comparable"]
    assert abs(Decimal(group["rate_minor_per_unit"]) - Decimal(12500) / 6) < Decimal("1e-20")
    assert group["last_billed_day"] == "2024-01-31"
    Usage.objects.all().update(basis="estimated")
    group = get(client, "unit-costs?category=water")["groups"][0]
    assert "estimated_quantity" in group["reasons"] and group["rate_minor_per_unit"] is None


def test_electricity_rate_includes_supply_fixed_and_demand_without_double_counting(
    api_client, ledger
):  # noqa: F811
    client = api_client[0]
    house = ledger["houses"][0]
    ledger["make"]([allocation(house, service="electricity_network", usages=[usage(100000)])])
    ledger["make"]([allocation(house, service="electricity_supply", usages=[usage(100000)])])
    ledger["make"]([demand_allocation(house, 5000)])
    group = get(client, "unit-costs?category=electricity")["groups"][0]
    assert (
        group["comparable"] and group["gross_minor"] == 37500 and group["quantity_milli"] == 100000
    )
    assert Decimal(group["rate_minor_per_unit"]) == 375
    assert {row["key"] for row in group["included_services"]} == {
        "electricity_network",
        "electricity_supply",
        "electricity_demand",
    }


def test_combined_waste_costs_and_missing_alignment_never_create_rates(api_client, ledger):  # noqa: F811
    client = api_client[0]
    house = ledger["houses"][0]
    ledger["make"](
        [
            allocation(
                house,
                service="waste",
                subcategory="waste_collection",
                usages=[
                    usage(10000, utility="waste", subcategory="restavfall", basis="billed_weight")
                ],
            )
        ]
    )
    groups = get(client, "unit-costs?category=waste")["groups"]
    assert all(row["rate_minor_per_unit"] is None for row in groups)
    assert (
        next(row for row in groups if row["subcategory"] == "waste_collection")["quantity_milli"]
        is None
    )
    assert next(row for row in groups if row["subcategory"] == "restavfall")["gross_minor"] is None


def test_one_selection_per_snapshot_and_no_supplied_bound_inference(
    api_client, ledger, monkeypatch
):  # noqa: F811
    import bills.api_reporting as reporting

    client = api_client[0]
    ledger["make"]([allocation(ledger["houses"][0], usages=[usage()])])
    statements = []
    original = reporting.reporting_reader

    @contextmanager
    def traced():
        with original() as reader:
            reader.set_trace_callback(statements.append)
            yield reader

    monkeypatch.setattr(reporting, "reporting_reader", traced)
    result = get(
        client, "consumption?group_by=period_start_year&date_from=2024-01-01&date_to=2024-12-31"
    )
    assert result["groups"][0]["quantity_milli"] == result["totals"][0]["quantity_milli"]
    assert (
        len([sql for sql in statements if "SELECT * FROM reporting_electricity_selection" in sql])
        == 1
    )
    assert not any("SELECT min(" in sql for sql in statements)


@pytest.mark.parametrize("include_quality", (False, True))
def test_scoped_selection_matches_canonical_before_vendor_and_date_filters(
    api_client, ledger, include_quality
):
    house, other_house = ledger["houses"]
    supply_vendor = Vendor.objects.create(name="Synthetic supplier", slug="synthetic-supplier")
    ledger["make"]([allocation(house, usages=[usage(100000)])])
    ledger["make"](
        [allocation(house, service="electricity_supply", usages=[usage(100000)])],
        issuer=supply_vendor,
    )
    # Conflicts in another house do not alter this house's source partition.
    for _ in range(2):
        ledger["make"]([allocation(other_house, usages=[usage(100000)])])
    # An adjacent older interval retains its whole-period semantics at the boundary.
    ledger["make"](
        [
            allocation(
                house,
                start=date(2024, 1, 1),
                end=date(2024, 2, 1),
                usages=[usage(70000, start=date(2024, 1, 1), end=date(2024, 2, 1))],
            )
        ]
    )
    with reporting_reader() as reader:
        expected = [
            dict(row)
            for row in reader.execute(
                "SELECT * FROM reporting_electricity_usage WHERE property_id=? "
                "AND period_end>'2024-02-01' AND period_start<='2024-02-29'",
                [house.pk],
            )
        ]
    path = (
        f"consumption?property_id={house.pk}&date_from=2024-02-01&date_to=2024-02-29"
        f"&mode=totals&representation=compact&include_quality={str(include_quality).lower()}"
    )
    result = get(api_client[0], path)
    assert result["groups"][0]["quantity_milli"] == sum(row["quantity_milli"] for row in expected)
    assert result["groups"][0]["usage_count"] == len(expected) == 1
    assert get(api_client[0], path + f"&vendor_id={supply_vendor.pk}")["groups"] == []


def test_unit_cost_exclusions_are_scoped_to_each_house(api_client, ledger):
    house, other_house = ledger["houses"]
    ledger["make"]([allocation(house, usages=[usage(100000)])])
    for _ in range(2):
        ledger["make"]([allocation(other_house, usages=[usage(100000)])])
    groups = get(api_client[0], "unit-costs?category=electricity")["groups"]
    by_house = {row["property_id"]: row for row in groups}
    assert by_house[house.pk]["comparable"]
    assert "excluded_consumption" in by_house[other_house.pk]["reasons"]


def test_unknown_cost_period_cannot_be_excluded_by_invoice_date(api_client, ledger):
    water(ledger, 2024, 1, 10000)
    later = water(ledger, 2025, 5, 10000)
    BillingSection.objects.filter(invoice=later).update(start=None, end=None)
    result = get(
        api_client[0], "unit-costs?category=water&date_from=2024-01-01&date_to=2024-01-31"
    )["groups"][0]
    assert "unknown_cost_period" in result["reasons"] and result["rate_minor_per_unit"] is None
    assert result["allocation_count"] == 2


def test_vendor_names_alone_do_not_establish_period_alignment(api_client, ledger):
    other = Vendor.objects.create(name="Synthetic second vendor", slug="synthetic-second-vendor")
    for month, quantity_month, vendor in ((1, 2, ledger["vendor"]), (2, 1, other)):
        ledger["make"](
            [
                allocation(
                    ledger["houses"][0],
                    service="water",
                    start=date(2024, month, 1),
                    end=date(2024, month + 1, 1),
                    usages=[
                        usage(
                            10000,
                            utility="water",
                            start=date(2024, quantity_month, 1),
                            end=date(2024, quantity_month + 1, 1),
                        )
                    ],
                )
            ],
            issuer=vendor,
        )
    result = get(api_client[0], "unit-costs?category=water")["groups"][0]
    assert "vendor_relationship_unestablished" in result["reasons"]
    assert result["rate_minor_per_unit"] is None


@pytest.mark.parametrize(
    "path",
    [
        "costs?representation=compact",
        "costs?group_by=invoice_year&representation=tiny",
        "costs?group_by=invoice_year&include_totals=0",
        "costs?years=2",
        "costs?year=2",
        "costs?year=2025&years=4",
        "costs?year=2025&date_from=2025-01-01",
        "billed-power?mode=period_start_month&limit=200",
        "billed-power?unit=kWh",
        "availability?date_from=2024-01-01",
        "availability?property_id=",
        "comparisons?year=2024&comparison_year=2024",
        "comparisons?metric=power",
        "comparisons?history=yes",
        "unit-costs?mode=rows",
        "unit-costs?subcategory=missing",
    ],
)
def test_analytical_validation(api_client, path):
    response = api_client[0].get(BASE + "/reports/" + path)
    assert (
        response.status_code == 422 and response.json()["error"]["code"] == "invalid_parameter"
    ), response.content
