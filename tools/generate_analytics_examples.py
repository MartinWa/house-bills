"""Schema-validated, deterministic analytical examples using invented source facts."""

from datetime import UTC, date, datetime
from decimal import Decimal


def generate():
    from bills.analytics_reporting import REASON_LABELS, comparison_value
    from bills.api import schemas as s
    from bills.power_reporting import summarize_billed_power
    from bills.reporting_data import interval_coverage

    metadata = {
        "contract_version": 1,
        "analytics_version": 1,
        "generated_at": datetime(2026, 3, 1, tzinfo=UTC),
    }
    filters = {
        "property_id": [1],
        "vendor_id": [],
        "category": None,
        "subcategory": None,
        "date_from": date(2024, 1, 1),
        "date_to": date(2024, 1, 31),
    }
    examples = []

    def add(name, endpoint, query, schema, payload, selected=filters):
        response = {
            **metadata,
            "filters": selected,
            "effective_date_bounds": {key: selected[key] for key in ("date_from", "date_to")},
            **payload,
        }
        examples.append(
            {
                "name": name,
                "endpoint": endpoint,
                "query": query,
                "response": schema.model_validate(response).model_dump(
                    mode="json", exclude_unset=True
                ),
            }
        )

    def fact(pk, quantity, *, start="2024-01-01", end="2024-02-01", basis="measured", unit="m3"):
        return {
            "property_id": 1,
            "property_name": "Synthetic house 1",
            "vendor_id": 1,
            "source_service": "electricity_demand" if unit == "kW" else "water",
            "source_selection": "billed",
            "unit": unit,
            "subcategory": "electricity_demand" if unit == "kW" else "",
            "invoice_date": "2026-02-20",
            "invoice_id": pk,
            "allocation_id": pk,
            "usage_id": pk,
            "period_start": start,
            "period_end": end,
            "quantity_milli": quantity,
            "basis": basis,
        }

    cost_groups = [
        {
            "invoice_year": year,
            "invoice_year_time": int(datetime(year, 1, 1, tzinfo=UTC).timestamp()),
            "category": category,
            "category_name": category.title(),
            "currency": "SEK",
            "net_minor": amount,
            "vat_minor": 0 if amount is not None else None,
            "gross_minor": amount,
            "invoice_count": 1,
            "allocation_count": 1,
        }
        for year, category, amount in (
            (2023, "electricity", -2000),
            (2024, "electricity", None),
            (2024, "water", 0),
        )
    ]
    add(
        "calendar-costs-credit-unknown-zero",
        "costs",
        "property_id=1&group_by=invoice_year,category&year=2024&years=3&"
        "representation=compact&include_totals=false&include_waste=false",
        s.AnalyticalAggregateOut,
        {
            "representation": "compact",
            "mode": "grouped",
            "group_by": ["invoice_year", "category", "currency"],
            "group_count": len(cost_groups),
            "groups": cost_groups,
        },
        {
            **filters,
            "date_from": date(2022, 1, 1),
            "date_to": date(2024, 12, 31),
            "currency": "SEK",
        },
    )
    add(
        "empty-quantity-window",
        "consumption",
        "property_id=1&group_by=period_start_month&year=2025&years=1&"
        "representation=compact&include_totals=false",
        s.AnalyticalAggregateOut,
        {
            "representation": "compact",
            "mode": "grouped",
            "group_by": ["period_start_month", "utility", "unit"],
            "group_count": 0,
            "groups": [],
        },
        {**filters, "date_from": date(2025, 1, 1), "date_to": date(2025, 12, 31)},
    )
    long_rows = [
        fact(pk, amount, start="2024-12-15", end="2025-03-01", basis=basis)
        for pk, amount, basis in (
            (1, 10000, "estimated"),
            (2, -10000, "reversal"),
            (3, 7000, "measured"),
        )
    ]
    long_coverage = interval_coverage(long_rows, None, None)
    add(
        "whole-water-interval-with-reversal",
        "consumption",
        "property_id=1&category=water&group_by=period_start_month&representation=compact&"
        "include_totals=false&include_quality=true&date_from=2025-01-01&date_to=2025-01-31",
        s.AnalyticalAggregateOut,
        {
            "representation": "compact",
            "mode": "grouped",
            "group_by": ["period_start_month", "utility", "unit"],
            "group_count": 1,
            "groups": [
                {
                    "period_start_month": "2024-12-01",
                    "period_start_month_time": 1733011200,
                    "utility": "water",
                    "utility_name": "Water",
                    "unit": "m3",
                    "unit_label": "m³",
                    "quantity_milli": 7000,
                    "invoice_count": 3,
                    "allocation_count": 3,
                    "usage_count": 3,
                    "coverage": long_coverage,
                    "basis_summary": "estimated: 1, measured: 1, reversal: 1",
                    "period_label": "2024-12-15 – 2025-02-28",
                }
            ],
            "quality": {
                "cost_invoice_date_from": None,
                "cost_invoice_date_to": None,
                "unknown_cost_count": 0,
                "invoice_coverage": None,
                "quantities": [
                    {
                        "property_id": 1,
                        "utility": "water",
                        "unit": "m3",
                        "availability": "recorded",
                        "selected_status": "recorded",
                        "exclusion_count": 0,
                        "coverage": interval_coverage(
                            long_rows, date(2025, 1, 1), date(2025, 1, 31)
                        ),
                    }
                ],
            },
        },
        {
            **filters,
            "category": "water",
            "date_from": date(2025, 1, 1),
            "date_to": date(2025, 1, 31),
        },
    )
    power_rows = [
        fact(pk, amount, unit="kW", basis="billed")
        for pk, amount in enumerate((5000, -5000, 3000, 0, -2000), 1)
    ]
    power = summarize_billed_power(power_rows)[0]
    add(
        "power-cancellation-replacement-zero-and-unmatched-credit",
        "billed-power",
        "property_id=1&mode=period_start_month&year=2024&years=1",
        s.PowerAggregateOut,
        {
            "mode": "period_start_month",
            "group_by": ["property_id", "unit", "period_start_month"],
            "group_count": 1,
            "groups": [
                {
                    "property_id": 1,
                    "property_name": "Synthetic house 1",
                    "unit": "kW",
                    "unit_label": "kW",
                    "period_start_month": "2024-01-01",
                    "period_start_month_time": 1704067200,
                    "highest_milli": power["highest"]["quantity_milli"],
                    "lowest_milli": power["lowest"]["quantity_milli"],
                    "average_milli": power["average_milli"],
                    "value_count": power["value_count"],
                    "usage_count": 5,
                    "invoice_count": 5,
                    "allocation_count": 5,
                    "cancelled_positive_count": 1,
                    "unmatched_credit_count": 1,
                    "excluded_count": 3,
                    "highest_period_start": "2024-01-01",
                    "highest_last_billed_day": "2024-01-31",
                    "lowest_period_start": "2024-01-01",
                }
            ],
        },
        {**filters, "date_to": date(2024, 12, 31)},
    )
    availability = {
        "property_id": 1,
        "property_name": "Synthetic house 1",
        "utility": "waste",
        "utility_name": "Waste",
        "unit": "kg",
        "unit_label": "kg",
        "configured": True,
        "has_costs": True,
        "has_quantities": False,
        "quantity_status": "not_recorded",
        "cost_years": [2021, 2024],
        "quantity_years": [],
        "cost_date_from": "2021-03-05",
        "cost_date_to": "2024-01-28",
        "quantity_period_start": None,
        "quantity_period_end": None,
        "allocation_count": 2,
        "usage_count": 0,
        "exclusion_count": 0,
    }
    add(
        "cost-only-utility",
        "availability",
        "property_id=1&category=waste",
        s.AvailabilityOut,
        {
            "metrics": [availability],
            "available_cost_years": [2021, 2024],
            "available_quantity_years": [],
        },
        {**filters, "category": "waste", "date_from": None, "date_to": None},
    )
    identity = {"property_id": 1, "property_name": "Synthetic house 1", "currency": "SEK"}
    bill_coverage = {
        "status": "complete",
        "as_of": "2026-03-01",
        "expectation_count": 3,
        "counts_by_status": {"received": 3},
        "missing_invoice_count": 0,
    }
    unknown_comparison = {
        **identity,
        "focus_year": 2024,
        "comparison_year": 2023,
        "current_date_from": "2024-01-01",
        "cutoff": "2024-03-31",
        "comparison_date_from": "2023-01-01",
        "comparison_cutoff": "2023-03-31",
        "current_value": None,
        "comparison_value": 8000,
        "current_coverage": bill_coverage,
        "comparison_coverage": bill_coverage,
        **comparison_value(None, 8000, ["unknown_amount"]),
    }
    monthly = []
    for year in (2022, 2023, 2024):
        cumulative, known = 0, True
        for month in range(1, 13):
            values = (
                {1: 10000, 2: -2000, 3: None}
                if year == 2024
                else {1: 8000, 2: 0, 3: 0}
                if year == 2023
                else {}
            )
            value = values.get(month)
            known = known and value is not None
            if known:
                cumulative += value
            monthly.append(
                {
                    **identity,
                    "year": year,
                    "month": month,
                    "month_name": date(year, month, 1).strftime("%b"),
                    "value": value,
                    "state": "absent"
                    if month not in values
                    else "unknown"
                    if value is None
                    else "recorded",
                    "cumulative_gross_minor": cumulative if known else None,
                }
            )
    cost_annuals = [
        {
            **identity,
            "year": year,
            "value": None if year != 2023 else 8000,
            "invoice_count": 0 if year == 2022 else 3,
            "coverage": {
                **bill_coverage,
                "status": "unknown" if year == 2022 else "partial",
                "expectation_count": 0 if year == 2022 else 12,
                "counts_by_status": {} if year == 2022 else {"received": 3, "missing": 9},
                "missing_invoice_count": 0 if year == 2022 else 9,
            },
            "status": "no_data"
            if year == 2022
            else "unknown_amount"
            if year == 2024
            else "partial",
            "contributing_date_from": None if year == 2022 else date(year, 1, 28),
            "contributing_date_to": None if year == 2022 else date(year, 3, 31),
        }
        for year in (2022, 2023, 2024)
    ]
    add(
        "unknown-cumulative-cost-with-signed-adjustment",
        "comparisons",
        "property_id=1&metric=costs&year=2024",
        s.ComparisonsOut,
        {
            "metric": "costs",
            "focus_year": 2024,
            "comparison_year": 2023,
            "available_years": [2023, 2024],
            "comparisons": [unknown_comparison],
            "annuals": cost_annuals,
            "monthly": monthly,
        },
        {**filters, "date_to": date(2024, 12, 31)},
    )
    current = [fact(1, 10000, end="2024-03-01")]
    previous = [fact(2, 8000, start="2023-01-01", end="2023-03-01")]
    quantity_identity = {
        **identity,
        "utility": "water",
        "utility_name": "Water",
        "unit": "m3",
        "unit_label": "m³",
        "subcategory": "",
        "subcategory_name": "",
    }
    quantity_identity.pop("currency")
    quantity_annuals, quantity_months = [], []
    for year, values in ((2022, []), (2023, previous), (2024, current)):
        coverage = interval_coverage(values, date(year, 1, 1), date(year, 12, 31))
        value = values[0]["quantity_milli"] if values else None
        quantity_annuals.append(
            {
                **quantity_identity,
                "year": year,
                "value": value,
                "invoice_count": len(values),
                "coverage": coverage,
                "status": "no_data" if not values else coverage["status"],
                "contributing_date_from": values[0]["period_start"] if values else None,
                "contributing_date_to": values[0]["period_end"] if values else None,
            }
        )
        for month in range(1, 13):
            quantity_months.append(
                {
                    **quantity_identity,
                    "year": year,
                    "month": month,
                    "month_name": date(year, month, 1).strftime("%b"),
                    "value": value if month == 1 else None,
                    "state": "recorded" if values and month == 1 else "absent",
                }
            )
    add(
        "equivalent-leap-year-cutoff",
        "comparisons",
        "property_id=1&category=water&metric=consumption&date_from=2024-01-01&date_to=2024-02-29",
        s.ComparisonsOut,
        {
            "metric": "consumption",
            "focus_year": 2024,
            "comparison_year": 2023,
            "available_years": [2023, 2024],
            "comparisons": [
                {
                    **quantity_identity,
                    "focus_year": 2024,
                    "comparison_year": 2023,
                    "current_date_from": "2024-01-01",
                    "cutoff": "2024-02-29",
                    "comparison_date_from": "2023-01-01",
                    "comparison_cutoff": "2023-02-28",
                    "current_value": 10000,
                    "comparison_value": 8000,
                    "current_coverage": interval_coverage(
                        current, date(2024, 1, 1), date(2024, 2, 29)
                    ),
                    "comparison_coverage": interval_coverage(
                        previous, date(2023, 1, 1), date(2023, 2, 28)
                    ),
                    **comparison_value(10000, 8000, []),
                }
            ],
            "annuals": quantity_annuals,
            "monthly": quantity_months,
        },
        {**filters, "category": "water", "date_to": date(2024, 2, 29)},
    )
    rows = [fact(1, 5000), fact(2, 2000)]
    aligned = {
        **quantity_identity,
        "currency": "SEK",
        "period_start": "2024-01-01",
        "period_end": "2024-02-01",
        "last_billed_day": "2024-01-31",
        "gross_minor": 12500,
        "quantity_milli": 7000,
        "rate_minor_per_unit": str(Decimal(12500) / 7),
        "status": "comparable",
        "comparable": True,
        "reasons": [],
        "reason_labels": [],
        "coverage": interval_coverage(rows, date(2024, 1, 1), date(2024, 1, 31)),
        "included_services": [{"key": "", "name": "Water"}],
        "allocation_count": 1,
        "usage_count": 2,
        "invoice_count": 1,
        "numerator": "Approved gross costs, including fixed and demand charges",
        "denominator": "Selected signed billed m3 over the same whole periods",
    }
    add(
        "independent-aligned-water-cost-and-readings",
        "unit-costs",
        "property_id=1&category=water&date_from=2024-01-01&date_to=2024-01-31",
        s.UnitCostsOut,
        {
            "alignment_policy": "whole_periods_no_proration_v1",
            "groups": [aligned],
            "group_count": 1,
        },
        {**filters, "category": "water"},
    )
    reasons = ["combined_or_unrelated_waste_cost", "no_quantities", "quantity_period_gap"]
    combined = {
        **aligned,
        "utility": "waste",
        "utility_name": "Waste",
        "unit": "kg",
        "unit_label": "kg",
        "subcategory": "waste_collection",
        "subcategory_name": "Collection",
        "quantity_milli": None,
        "rate_minor_per_unit": None,
        "status": "unavailable",
        "comparable": False,
        "reasons": reasons,
        "reason_labels": [REASON_LABELS[key] for key in reasons],
        "coverage": interval_coverage([], date(2024, 1, 1), date(2024, 1, 31)),
        "included_services": [{"key": "waste_collection", "name": "Collection"}],
        "usage_count": 0,
        "denominator": "Selected signed billed kg over the same whole periods",
    }
    add(
        "combined-waste-cost-never-apportioned",
        "unit-costs",
        "property_id=1&category=waste&date_from=2024-01-01&date_to=2024-01-31",
        s.UnitCostsOut,
        {
            "alignment_policy": "whole_periods_no_proration_v1",
            "groups": [combined],
            "group_count": 1,
        },
        {**filters, "category": "waste"},
    )
    return examples
