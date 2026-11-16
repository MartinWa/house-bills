"""Approved availability, coverage, comparisons and period-aligned unit costs.

These aggregate contracts never join costs to individual usage rows. Invoice-date
cost reporting and whole-overlapping-interval consumption retain their contracts.
"""

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext

from .categories import CATEGORY_LABELS
from .power_reporting import summarize_billed_power
from .reporting import reporting_reader
from .reporting_data import (
    ReportingSnapshot,
    calendar_value,
    interval_coverage,
    matches,
    surviving_intervals,
)
from .waste_reporting import ReportTooBroad

MAX_GROUPS = 10_000
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
UNITS = {"electricity": "kWh", "water": "m3", "waste": "kg"}
REASON_LABELS = {
    "unknown_amount": "A contributing amount is unknown",
    "missing_observations": "One comparison period has no observations",
    "invoice_coverage_incomplete": "Bill coverage is incomplete or unknown",
    "quantity_coverage_incomplete": "Billed intervals do not cover the comparison window",
    "basis_mismatch": "The comparison periods use different quantity bases",
    "unmatched_credits": "A quantity credit has no matching original",
    "nonpositive_baseline": "A zero or negative baseline has no percentage change",
    "interval_crosses_comparison_boundary": "A whole interval crosses a comparison boundary",
    "excluded_consumption": "Approved electricity quantities were excluded by source selection",
    "no_costs": "No matching separately billed costs are recorded",
    "no_quantities": "No matching billed quantities are recorded",
    "unknown_cost": "A contributing cost is unknown",
    "nonpositive_quantity": "A zero or negative quantity cannot establish a unit cost",
    "combined_or_unrelated_waste_cost": "Combined waste costs cannot be apportioned to weights",
    "unknown_cost_period": "A cost has no approved billed period",
    "estimated_quantity": "The surviving billed quantity is estimated",
    "unmatched_quantity_credit": "A quantity credit has no matching original",
    "quantity_period_gap": "Quantities do not cover the complete cost period",
    "cost_period_gap": "An included service does not cover the complete quantity period",
    "ambiguous_cost_period_overlap": "Different cost periods overlap ambiguously",
    "ambiguous_quantity_overlap": "Different quantity periods overlap ambiguously",
    "interval_crosses_selected_boundary": "A billed interval extends beyond the selected range",
    "vendor_relationship_unestablished": "Costs and quantities have no matching vendor periods",
}


def metadata(filters):
    return {
        "analytics_version": 1,
        "contract_version": 1,
        "generated_at": datetime.now(UTC),
        "filters": filters,
        "effective_date_bounds": {key: filters.get(key) for key in ("date_from", "date_to")},
    }


def validate_catalogue(reader, filters):
    validate_analytics_dates(filters)
    if (
        filters.get("subcategory") is not None
        and not reader.execute(
            "SELECT 1 FROM bills_subcategory WHERE key=?",
            [filters["subcategory"]],
        ).fetchone()
    ):
        from .api.errors import invalid_parameter

        invalid_parameter("subcategory")


def validate_analytics_dates(filters):
    if filters.get("date_to") and filters["date_to"] >= date(9999, 12, 1):
        from .api.errors import invalid_parameter

        invalid_parameter("date_to", "December 9999 has no representable exclusive endpoint.")


def exact_total(rows, field):
    return (
        sum(row[field] for row in rows)
        if rows and all(row[field] is not None for row in rows)
        else None
    )


def grouped(rows, keys):
    result = defaultdict(list)
    for row in rows:
        result[tuple(row.get(key) for key in keys)].append(row)
        if len(result) > MAX_GROUPS:
            raise ReportTooBroad("10,000 analytical groups")
    return result


def available_metrics(reader, filters):
    properties = [
        dict(row)
        for row in reader.execute("SELECT id, name FROM bills_property ORDER BY id")
        if not filters.get("property_id") or row["id"] in filters["property_id"]
    ]
    configured = [
        dict(row)
        for row in reader.execute(
            "SELECT property_id, category, coalesce(subcategory, '') AS subcategory, is_active "
            "FROM bills_propertyservice ORDER BY property_id, category, subcategory"
        )
    ]
    cost_filters = {key: value for key, value in filters.items() if key != "unit"}
    usage_filters = {key: value for key, value in filters.items() if key != "currency"}
    costs = reader.selected(cost_filters, "costs", dates=False)
    usage = reader.selected(usage_filters, "consumption", dates=False)
    power = reader.selected(usage_filters, "power", dates=False)
    excluded = reader.selected(usage_filters, "issues", dates=False)
    metrics = []
    for property_row in properties:
        house = property_row["id"]
        utilities = (
            {row["category"] for row in costs if row["property_id"] == house}
            | {row["category"] for row in configured if row["property_id"] == house}
            | {row["utility"] for row in [*usage, *power, *excluded] if row["property_id"] == house}
        )
        if filters.get("category"):
            utilities &= {filters["category"]}
        for utility in sorted(utilities):
            units = (
                ["kWh", "kW"]
                if utility == "electricity"
                else [UNITS[utility]]
                if utility in UNITS
                else sorted(
                    {
                        row["unit"]
                        for row in usage
                        if row["property_id"] == house and row["utility"] == utility
                    }
                )
                or [None]
            )
            for unit in units:
                if filters.get("unit") and filters["unit"] != unit:
                    continue
                cost_rows = [
                    row
                    for row in costs
                    if row["property_id"] == house
                    and row["category"] == utility
                    and (unit != "kW" or row["subcategory"] == "electricity_demand")
                ]
                quantity_rows = [
                    row
                    for row in (power if unit == "kW" else usage)
                    if row["property_id"] == house
                    and row["utility"] == utility
                    and row["unit"] == unit
                ]
                issue_rows = [
                    row for row in excluded if row["property_id"] == house and row["unit"] == unit
                ]
                metrics.append(
                    {
                        "property_id": house,
                        "property_name": property_row["name"],
                        "utility": utility,
                        "utility_name": CATEGORY_LABELS.get(utility, utility),
                        "unit": unit,
                        "unit_label": "m³" if unit == "m3" else unit,
                        "configured": any(
                            row["property_id"] == house
                            and row["category"] == utility
                            and row["is_active"]
                            for row in configured
                        ),
                        "has_costs": bool(cost_rows),
                        "has_quantities": bool(quantity_rows),
                        "quantity_status": "recorded"
                        if quantity_rows
                        else "excluded"
                        if issue_rows
                        else "not_recorded",
                        "cost_years": sorted({int(row["invoice_date"][:4]) for row in cost_rows}),
                        "quantity_years": sorted(
                            {int(row["period_start"][:4]) for row in quantity_rows}
                        ),
                        "cost_date_from": min(
                            (row["invoice_date"] for row in cost_rows), default=None
                        ),
                        "cost_date_to": max(
                            (row["invoice_date"] for row in cost_rows), default=None
                        ),
                        "quantity_period_start": min(
                            (row["period_start"] for row in quantity_rows), default=None
                        ),
                        "quantity_period_end": max(
                            (row["period_end"] for row in quantity_rows), default=None
                        ),
                        "allocation_count": len(cost_rows),
                        "usage_count": len(quantity_rows),
                        "exclusion_count": len(issue_rows),
                    }
                )
    return metrics


def read_availability(*, filters):
    with reporting_reader() as database:
        reader = ReportingSnapshot(database, filters)
        validate_catalogue(reader, filters)
        metrics = available_metrics(reader, filters)
        return {
            **metadata(filters),
            "metrics": metrics,
            "available_cost_years": sorted({year for row in metrics for year in row["cost_years"]}),
            "available_quantity_years": sorted(
                {year for row in metrics for year in row["quantity_years"]}
            ),
        }


def cost_coverage(reader, filters):
    """Invoice expectations describe current receipt facts, separately from quantities."""
    from .invoice_coverage import build_invoice_coverage

    start, end = filters.get("date_from"), filters.get("date_to")
    as_of = datetime.now(UTC).date()
    if not start or not end:
        return {
            "status": "unknown",
            "as_of": as_of,
            "expectation_count": 0,
            "counts_by_status": {},
            "missing_invoice_count": 0,
        }
    key = (
        tuple(filters.get("property_id") or []),
        tuple(filters.get("vendor_id") or []),
        filters.get("category"),
        filters.get("subcategory"),
        start,
        end,
        as_of,
    )
    if key not in reader.cache:
        facts = build_invoice_coverage(
            reader=reader,
            property_ids=filters.get("property_id") or None,
            vendor_ids=filters.get("vendor_id") or None,
            service=filters.get("category") or "",
            subcategory=filters.get("subcategory"),
            date_from=start,
            date_to=end,
            as_of=as_of,
            max_groups=MAX_GROUPS,
        )
        rows = facts["coverage_rows"]
        counts = Counter(row["status"] for row in rows)
        complete = rows and all(row["status"] in ("received", "not_expected") for row in rows)
        reader.cache[key] = {
            "status": "complete"
            if complete
            else "unknown"
            if not rows or counts["unassigned"] or counts["unscheduled"]
            else "partial",
            "as_of": as_of,
            "expectation_count": len(rows),
            "counts_by_status": dict(sorted(counts.items())),
            "missing_invoice_count": counts["missing"],
        }
    return reader.cache[key]


def report_quality(reader, filters, kind):
    validate_analytics_dates(filters)
    selected = reader.selected(filters, "consumption") if kind != "costs" else []
    metrics = available_metrics(reader, filters) if kind != "costs" else []
    quantities = []
    for metric in metrics:
        if metric["unit"] in (None, "kW"):
            continue
        sources = [
            row
            for row in selected
            if row["property_id"] == metric["property_id"]
            and row["utility"] == metric["utility"]
            and row["unit"] == metric["unit"]
        ]
        coverage = interval_coverage(sources, filters.get("date_from"), filters.get("date_to"))
        issues = [
            row
            for row in reader.selected(filters, "issues")
            if row["property_id"] == metric["property_id"] and row["unit"] == metric["unit"]
        ]
        quantities.append(
            {
                "property_id": metric["property_id"],
                "utility": metric["utility"],
                "unit": metric["unit"],
                "availability": metric["quantity_status"],
                "selected_status": "recorded"
                if sources
                else "excluded"
                if issues
                else "empty_range"
                if metric["has_quantities"]
                else "not_recorded",
                "exclusion_count": len(issues),
                "coverage": coverage,
            }
        )
    costs = reader.selected(filters, "costs") if kind in ("costs", "summary") else []
    return {
        "cost_invoice_date_from": min((row["invoice_date"] for row in costs), default=None),
        "cost_invoice_date_to": max((row["invoice_date"] for row in costs), default=None),
        "unknown_cost_count": sum(row["gross_minor"] is None for row in costs),
        "invoice_coverage": cost_coverage(reader, filters)
        if kind in ("costs", "summary")
        else None,
        "quantities": quantities,
    }


def group_quality(reader, filters, groups, mode, group_by):
    dimensions = (
        group_by if mode == "grouped" else ["property_id", "utility", "unit", "subcategory"]
    )
    if mode == "billed_period":
        dimensions = [
            *dimensions,
            "period_start",
            "period_end",
            "basis",
            "source_service",
            "source_selection",
        ]
    sources = defaultdict(list)
    for row in reader.selected(filters, "consumption"):
        key = tuple(
            ""
            if name == "subcategory" and mode == "totals" and row["utility"] == "electricity"
            else calendar_value(row, name)
            for name in dimensions
        )
        sources[key].append(row)
    for group in groups:
        coverage = interval_coverage(sources[tuple(group[name] for name in dimensions)], None, None)
        group["coverage"] = coverage
        group["basis_summary"] = ", ".join(
            f"{key}: {value}" for key, value in coverage["basis_counts"].items()
        )
        group["period_label"] = f"{coverage['period_start']} – {coverage['last_billed_day']}"


def read_power_aggregates(*, filters, mode):
    from .api_reporting import labels, report_labels, resolved_filters, timestamps

    with reporting_reader() as database:
        reader = ReportingSnapshot(database, filters, full_history=False)
        validate_catalogue(reader, filters)
        filters = resolved_filters(reader, filters, "power")
        sources = reader.selected(filters, "power")
        keys = ["property_id", "unit"]
        if mode == "billed_period":
            keys += ["period_start", "period_end"]
        elif mode in ("period_start_month", "period_start_year"):
            keys.append(mode)
        buckets = defaultdict(list)
        for row in sources:
            buckets[tuple(calendar_value(row, key) for key in keys)].append(row)
        if len(buckets) > MAX_GROUPS:
            raise ReportTooBroad("10,000 power aggregate groups")
        groups = []
        for key, values in sorted(buckets.items()):
            native = summarize_billed_power(values)[0]
            highest, lowest = native["highest"], native["lowest"]
            groups.append(
                {
                    **dict(zip(keys, key, strict=True)),
                    "property_name": native["property_name"],
                    "highest_milli": highest["quantity_milli"] if highest else None,
                    "lowest_milli": lowest["quantity_milli"] if lowest else None,
                    "average_milli": native["average_milli"],
                    "value_count": native["value_count"],
                    "usage_count": len(values),
                    "invoice_count": len({row["invoice_id"] for row in values}),
                    "allocation_count": len({row["allocation_id"] for row in values}),
                    "cancelled_positive_count": sum(row["quantity_milli"] >= 0 for row in values)
                    - native["value_count"],
                    "unmatched_credit_count": native["unmatched_credit_count"],
                    "excluded_count": len(values) - native["value_count"],
                    "highest_period_start": highest["period_start"] if highest else None,
                    "highest_last_billed_day": (
                        date.fromisoformat(highest["period_end"]) - timedelta(days=1)
                    )
                    if highest
                    else None,
                    "lowest_period_start": lowest["period_start"] if lowest else None,
                }
            )
        return {
            **metadata(filters),
            "mode": mode,
            "groups": labels(timestamps(groups), report_labels(reader)),
            "group_by": keys,
            "group_count": len(groups),
        }


def shift_year(value, year):
    if not 1 <= year <= 9998:
        from .api.errors import invalid_parameter

        invalid_parameter("comparison_year", "The shifted window exceeds the supported calendar.")
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def comparison_value(current, previous, reasons):
    comparable = not reasons and current is not None and previous is not None
    delta = current - previous if comparable else None
    percentage = (Decimal(delta) * 100 / Decimal(previous)) if comparable and previous > 0 else None
    percentage_reasons = (
        [] if percentage is not None else ["nonpositive_baseline"] if comparable else reasons
    )
    return {
        "comparable": comparable,
        "status": "comparable" if comparable else "unavailable",
        "reasons": sorted(set(reasons)),
        "absolute_change": delta,
        "percentage_change": str(percentage) if percentage is not None else None,
        "percentage_reasons": sorted(set(percentage_reasons)),
        "reason_labels": [REASON_LABELS[key] for key in sorted(set(reasons + percentage_reasons))],
    }


def within_dates(row, window, metric):
    return matches(
        row, {key: window.get(key) for key in ("date_from", "date_to")}, usage=metric != "costs"
    )


def read_comparisons(*, filters, metric, focus_year=None, comparison_year=None, history=False):
    from .api_reporting import labels, report_labels, resolved_filters, validate_date_bounds

    with reporting_reader() as database:
        reader = ReportingSnapshot(database, filters)
        validate_catalogue(reader, filters)
        catalogue = report_labels(reader)
        focus = focus_year or (filters.get("date_to") or datetime.now(UTC).date()).year
        previous_year = comparison_year or focus - 1
        if not 3 <= focus <= 9998 or previous_year == focus:
            from .api.errors import invalid_parameter

            invalid_parameter("year" if not 3 <= focus <= 9998 else "comparison_year")
        requested = {
            **filters,
            "date_from": filters.get("date_from") or date(focus, 1, 1),
            "date_to": filters.get("date_to") or date(focus, 12, 31),
        }
        resolved_filters(reader, requested, metric)
        sources = reader.selected(filters, metric, dates=False)
        fields = (
            ["property_id", "currency"]
            if metric == "costs"
            else ["property_id", "utility", "unit", "subcategory"]
        )
        if metric == "consumption":
            sources = [
                {
                    **row,
                    "subcategory": "" if row["utility"] == "electricity" else row["subcategory"],
                }
                for row in sources
            ]
        populations = grouped(sources, fields)
        comparisons, annuals, monthly = [], [], []
        available_years = sorted(
            {
                calendar_value(row, "invoice_year" if metric == "costs" else "period_start_year")
                for row in sources
            }
        )
        if history and available_years:
            validate_date_bounds(
                {
                    "date_from": date(available_years[0], 1, 1),
                    "date_to": date(available_years[-1], 12, 31),
                }
            )
        years = available_years if history else [focus - 2, focus - 1, focus]
        for identity, population in sorted(populations.items()):
            identity = dict(zip(fields, identity, strict=True))
            identity["property_name"] = population[0]["property_name"]
            group_filters = {**requested, "property_id": [identity["property_id"]]}
            if metric == "consumption":
                group_filters.update(category=identity["utility"], unit=identity["unit"])
            current_rows = [row for row in population if within_dates(row, requested, metric)]
            if metric == "costs":
                cutoff = min(
                    requested["date_to"],
                    max(
                        (date.fromisoformat(row["invoice_date"]) for row in current_rows),
                        default=requested["date_to"],
                    ),
                )
            else:
                surviving, _ = surviving_intervals(current_rows)
                cutoff = min(
                    requested["date_to"],
                    max(
                        (
                            date.fromisoformat(row["period_end"]) - timedelta(days=1)
                            for row in surviving
                        ),
                        default=requested["date_to"],
                    ),
                )
            current_window = {**group_filters, "date_to": cutoff}
            previous_window = {
                **current_window,
                "date_from": shift_year(
                    requested["date_from"], requested["date_from"].year + previous_year - focus
                ),
                "date_to": shift_year(cutoff, cutoff.year + previous_year - focus),
            }
            current_rows = [row for row in population if within_dates(row, current_window, metric)]
            previous_rows = [
                row for row in population if within_dates(row, previous_window, metric)
            ]
            field = "gross_minor" if metric == "costs" else "quantity_milli"
            current, previous = exact_total(current_rows, field), exact_total(previous_rows, field)
            reasons = []
            if current is None or previous is None:
                reasons.append(
                    "unknown_amount"
                    if metric == "costs"
                    and any(row[field] is None for row in [*current_rows, *previous_rows])
                    else "missing_observations"
                )
            if metric == "costs":
                current_coverage, previous_coverage = (
                    cost_coverage(reader, current_window),
                    cost_coverage(reader, previous_window),
                )
                if (
                    current_coverage["status"] != "complete"
                    or previous_coverage["status"] != "complete"
                ):
                    reasons.append("invoice_coverage_incomplete")
            else:
                current_coverage = interval_coverage(
                    current_rows, current_window["date_from"], cutoff
                )
                previous_coverage = interval_coverage(
                    previous_rows, previous_window["date_from"], previous_window["date_to"]
                )
                if (
                    current_coverage["status"] != "complete"
                    or previous_coverage["status"] != "complete"
                ):
                    reasons.append("quantity_coverage_incomplete")
                if current_coverage["surviving_basis"] != previous_coverage["surviving_basis"]:
                    reasons.append("basis_mismatch")
                if (
                    current_coverage["unmatched_credit_count"]
                    or previous_coverage["unmatched_credit_count"]
                ):
                    reasons.append("unmatched_credits")
                if reader.selected(current_window, "issues") or reader.selected(
                    previous_window, "issues"
                ):
                    reasons.append("excluded_consumption")
                for window, values in (
                    (current_window, current_rows),
                    (previous_window, previous_rows),
                ):
                    if any(
                        row["period_start"] < window["date_from"].isoformat()
                        or row["period_end"] > (window["date_to"] + timedelta(days=1)).isoformat()
                        for row in values
                    ):
                        reasons.append("interval_crosses_comparison_boundary")
            comparisons.append(
                {
                    **identity,
                    "focus_year": focus,
                    "comparison_year": previous_year,
                    "current_date_from": current_window["date_from"],
                    "cutoff": cutoff,
                    "comparison_date_from": previous_window["date_from"],
                    "comparison_cutoff": previous_window["date_to"],
                    "current_value": current,
                    "comparison_value": previous,
                    "current_coverage": current_coverage,
                    "comparison_coverage": previous_coverage,
                    **comparison_value(current, previous, reasons),
                }
            )
            for year in years:
                annual_rows = [
                    row
                    for row in population
                    if calendar_value(
                        row, "invoice_year" if metric == "costs" else "period_start_year"
                    )
                    == year
                ]
                annual_window = {
                    **group_filters,
                    "date_from": date(year, 1, 1),
                    "date_to": date(year, 12, 31),
                }
                coverage = (
                    cost_coverage(reader, annual_window)
                    if metric == "costs"
                    else interval_coverage(
                        annual_rows, annual_window["date_from"], annual_window["date_to"]
                    )
                )
                annuals.append(
                    {
                        **identity,
                        "year": year,
                        "value": exact_total(annual_rows, field),
                        "invoice_count": len({row["invoice_id"] for row in annual_rows}),
                        "coverage": coverage,
                        "status": "no_data"
                        if not annual_rows
                        else "unknown_amount"
                        if exact_total(annual_rows, field) is None
                        else coverage["status"],
                        "contributing_date_from": min(
                            (
                                row["invoice_date" if metric == "costs" else "period_start"]
                                for row in annual_rows
                            ),
                            default=None,
                        ),
                        "contributing_date_to": max(
                            (
                                row["invoice_date" if metric == "costs" else "period_end"]
                                for row in annual_rows
                            ),
                            default=None,
                        ),
                    }
                )
                if year not in (focus, focus - 1, focus - 2):
                    continue
                cumulative, known = 0, True
                for month in range(1, 13):
                    month_rows = [
                        row
                        for row in annual_rows
                        if int(row["invoice_date" if metric == "costs" else "period_start"][5:7])
                        == month
                    ]
                    value = exact_total(month_rows, field)
                    known = known and value is not None
                    if known:
                        cumulative += value
                    monthly.append(
                        {
                            **identity,
                            "year": year,
                            "month": month,
                            "month_name": MONTH_NAMES[month - 1],
                            "value": value,
                            "state": "absent"
                            if not month_rows
                            else "unknown"
                            if value is None
                            else "recorded",
                            **(
                                {"cumulative_gross_minor": cumulative if known else None}
                                if metric == "costs"
                                else {}
                            ),
                        }
                    )
        if len(annuals) + len(monthly) + len(comparisons) > MAX_GROUPS:
            raise ReportTooBroad("10,000 comparison groups")
        return {
            **metadata(requested),
            "metric": metric,
            "focus_year": focus,
            "comparison_year": previous_year,
            "available_years": available_years,
            "comparisons": labels(comparisons, catalogue),
            "annuals": labels(annuals, catalogue),
            "monthly": labels(monthly, catalogue),
        }


def alignment_group(reader, filters, identity, cost_rows, quantity_rows, catalogue):
    """Require complete whole-period alignment; never prorate or apportion."""
    utility, unit = identity["utility"], identity["unit"]
    reasons = []
    gross, quantity = (
        exact_total(cost_rows, "gross_minor"),
        exact_total(quantity_rows, "quantity_milli"),
    )
    if not cost_rows:
        reasons.append("no_costs")
    if not quantity_rows:
        reasons.append("no_quantities")
    if cost_rows and gross is None:
        reasons.append("unknown_cost")
    if quantity is not None and quantity <= 0:
        reasons.append("nonpositive_quantity")
    if utility == "waste" and not catalogue.get(identity["subcategory"], {}).get(
        "is_waste_fraction"
    ):
        reasons.append("combined_or_unrelated_waste_cost")
    invalid_costs = [row for row in cost_rows if not row["period_start"] or not row["period_end"]]
    if invalid_costs:
        reasons.append("unknown_cost_period")
    periods = [
        (date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"]))
        for row in [*cost_rows, *quantity_rows]
        if row["period_start"] and row["period_end"]
    ]
    start, end = (
        min((row[0] for row in periods), default=None),
        max((row[1] for row in periods), default=None),
    )
    coverage = interval_coverage(quantity_rows, start, end - timedelta(days=1) if end else None)
    if "estimated" in coverage["surviving_basis"]:
        reasons.append("estimated_quantity")
    if coverage["unmatched_credit_count"]:
        reasons.append("unmatched_quantity_credit")
    if utility == "electricity" and reader.selected(
        {**filters, "property_id": [identity["property_id"]], "category": utility}, "issues"
    ):
        reasons.append("excluded_consumption")
    if coverage["status"] != "complete":
        reasons.append("quantity_period_gap")
    services = grouped(cost_rows, ["subcategory"])
    for service_rows in services.values():
        spans = sorted(
            {
                (row["period_start"], row["period_end"])
                for row in service_rows
                if row["period_start"] and row["period_end"]
            }
        )
        cursor = start.isoformat() if start else None
        for lower, upper in spans:
            if lower != cursor:
                reasons.append(
                    "cost_period_gap"
                    if cursor is None or lower > cursor
                    else "ambiguous_cost_period_overlap"
                )
            cursor = max(cursor or upper, upper)
        if not end or cursor != end.isoformat():
            reasons.append("cost_period_gap")
    surviving, _ = surviving_intervals(quantity_rows)
    if utility != "electricity" and cost_rows and quantity_rows:
        vendors = {row["vendor_id"] for row in [*cost_rows, *quantity_rows]}
        if any(
            merged_periods([row for row in cost_rows if row["vendor_id"] == vendor])
            != merged_periods([row for row in surviving if row["vendor_id"] == vendor])
            for vendor in vendors
        ):
            reasons.append("vendor_relationship_unestablished")
    spans = sorted({(row["period_start"], row["period_end"]) for row in surviving})
    if any(
        lower < previous_end
        for (_, previous_end), (lower, _) in zip(spans, spans[1:], strict=False)
    ):
        reasons.append("ambiguous_quantity_overlap")
    if (
        start
        and filters.get("date_from")
        and start < filters["date_from"]
        or end
        and filters.get("date_to")
        and end > filters["date_to"] + timedelta(days=1)
    ):
        reasons.append("interval_crosses_selected_boundary")
    rate = None
    if not reasons:
        with localcontext() as context:
            context.prec = 28
            rate = str(Decimal(gross) * 1000 / Decimal(quantity))
    return {
        **identity,
        "period_start": start,
        "period_end": end,
        "last_billed_day": end - timedelta(days=1) if end else None,
        "gross_minor": gross,
        "quantity_milli": quantity,
        "rate_minor_per_unit": rate,
        "status": "comparable" if not reasons else "unavailable",
        "comparable": not reasons,
        "reasons": sorted(set(reasons)),
        "reason_labels": [REASON_LABELS[key] for key in sorted(set(reasons))],
        "coverage": coverage,
        "included_services": [
            {
                "key": key[0],
                "name": catalogue.get(key[0], {}).get(
                    "name", key[0] or CATEGORY_LABELS.get(utility, utility)
                ),
            }
            for key in sorted(services)
        ],
        "allocation_count": len(cost_rows),
        "usage_count": len(quantity_rows),
        "invoice_count": len({row["invoice_id"] for row in cost_rows}),
        "numerator": "Approved gross costs, including fixed and demand charges",
        "denominator": f"Selected signed billed {unit} over the same whole periods",
    }


def merged_periods(rows):
    merged = []
    for lower, upper in sorted(
        {
            (row["period_start"], row["period_end"])
            for row in rows
            if row["period_start"] and row["period_end"]
        }
    ):
        if merged and lower <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(upper, merged[-1][1]))
        else:
            merged.append((lower, upper))
    return merged


def read_unit_costs(*, filters):
    from .api_reporting import labels, report_labels, resolved_filters

    with reporting_reader() as database:
        reader = ReportingSnapshot(database, filters)
        validate_catalogue(reader, filters)
        filters = resolved_filters(reader, filters, "consumption")
        catalogue = {
            row["key"]: dict(row)
            for row in reader.execute("SELECT key, name, is_waste_fraction FROM bills_subcategory")
        }
        costs = [
            row
            for row in reader.selected(filters, "costs", dates=False)
            if matches(row, filters, usage=True)
            # Without an approved period, an allocation cannot safely be placed
            # outside the selected interval, even when its invoice date is later.
            or not row["period_start"]
            or not row["period_end"]
        ]
        quantity_rows = reader.selected(filters, "consumption")
        identities = {}
        cost_groups, quantity_groups = defaultdict(list), defaultdict(list)
        for row in costs:
            utility = row["category"]
            if utility not in UNITS:
                continue
            fraction = row["subcategory"] if utility == "waste" else ""
            key = (row["property_id"], utility, UNITS[utility], fraction, row["currency"])
            identities[key] = {
                "property_id": row["property_id"],
                "property_name": row["property_name"],
                "utility": utility,
                "unit": UNITS[utility],
                "subcategory": fraction,
                "currency": row["currency"],
            }
            cost_groups[key].append(row)
        for row in quantity_rows:
            fraction = row["subcategory"] if row["utility"] == "waste" else ""
            key = (row["property_id"], row["utility"], row["unit"], fraction, "SEK")
            identities.setdefault(
                key,
                {
                    "property_id": row["property_id"],
                    "property_name": row["property_name"],
                    "utility": row["utility"],
                    "unit": row["unit"],
                    "subcategory": fraction,
                    "currency": "SEK",
                },
            )
            quantity_groups[key].append(row)
        if len(identities) > MAX_GROUPS:
            raise ReportTooBroad("10,000 aligned unit-cost groups")
        groups = [
            alignment_group(
                reader, filters, identities[key], cost_groups[key], quantity_groups[key], catalogue
            )
            for key in sorted(identities)
        ]
        return {
            **metadata(filters),
            "alignment_policy": "whole_periods_no_proration_v1",
            "groups": labels(groups, report_labels(reader)),
            "group_count": len(groups),
        }
