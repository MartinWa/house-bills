"""Approved reports support complete aggregates and paginated source detail."""

from datetime import UTC, date, datetime

from django.http import JsonResponse
from ninja import Router

from bills.analytics_reporting import (
    read_availability,
    read_comparisons,
    read_power_aggregates,
    read_unit_costs,
)
from bills.api_reporting import (
    DIMENSIONS,
    INVOICE_COST_SORT,
    MANDATORY_DIMENSIONS,
    SORT_FIELDS,
    read_api_report,
    read_invoice_costs,
    read_selectors,
)
from bills.attention_reporting import REASONS, read_attention_report
from bills.attention_reporting import SORT_FIELDS as ATTENTION_SORT_FIELDS
from bills.invoice_coverage import STATUS_LABELS, read_invoice_coverage
from bills.models import Category

from . import schemas as s
from .attention import QUERY_FIELDS, parse_filters, pinned_page
from .collections import boolean, integer, iso_date, page_response, pagination, query_values
from .contract import documented_query
from .errors import ApiError, invalid_parameter

router = Router(tags=["Read-only reporting"])


def report_filters(request, modes=(), *, raw=False, kind=None, values=None):
    allowed = ["property_id", "vendor_id", "category", "subcategory", "date_from", "date_to"]
    if modes:
        allowed.append("mode")
    if raw:
        allowed += ["limit", "offset", "sort"]
    if kind in DIMENSIONS:
        allowed += ["group_by", "currency" if kind == "costs" else "unit"]
        allowed += ["representation", "include_totals", "include_quality", "year", "years"]
    if kind in ("costs", "summary"):
        allowed += ["include_waste"]
    if kind == "summary":
        allowed += ["include_quality"]
    if kind == "power":
        allowed += ["unit", "year", "years"]
    if values is None:
        values = query_values(request, allowed, repeated=["property_id", "vendor_id"])
    filters = {}
    for name in ("property_id", "vendor_id"):
        ids = [integer(value, name) for value in values.get(name, [])]
        if len(set(ids)) > 100:
            raise ApiError(
                422,
                "filter_too_broad",
                "At most 100 distinct IDs are accepted.",
                [{"path": name, "code": "limit_exceeded", "message": "Limit: 100 IDs."}],
            )
        filters[name] = sorted(set(ids))
    for name in ("date_from", "date_to"):
        filters[name] = iso_date(values[name], name) if name in values else None
    if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
        invalid_parameter("date_to", "End date must be on or after start date.")
    filters["category"] = values.get("category")
    if filters["category"] is not None and filters["category"] not in Category.values:
        invalid_parameter("category")
    filters["subcategory"] = values.get("subcategory")
    if "years" in values and "year" not in values:
        invalid_parameter("years", "Supply a focus year.")
    if "year" in values:
        year = integer(values["year"], "year", minimum=3, maximum=9998)
        years = integer(values.get("years", "3"), "years", maximum=3)
        if "date_from" in values or "date_to" in values:
            invalid_parameter("year", "Choose a year window or explicit dates.")
        filters.update(date_from=date(year - years + 1, 1, 1), date_to=date(year, 12, 31))
    if kind in DIMENSIONS:
        name, supported = (
            ("currency", ("SEK",)) if kind == "costs" else ("unit", ("kWh", "m3", "kg"))
        )
        filters[name] = values.get(name)
        if filters[name] is not None and filters[name] not in supported:
            invalid_parameter(name)
    if kind == "power" and values.get("unit", "kW") != "kW":
        invalid_parameter("unit")
    if kind == "power" and "unit" in values:
        filters["unit"] = values["unit"]
    mode = values.get("mode", "grouped" if "group_by" in values else "rows")
    if modes and mode not in modes:
        invalid_parameter("mode", "Unknown aggregate mode.")
    if mode != "rows" and ("limit" in values or "offset" in values):
        invalid_parameter(
            "limit" if "limit" in values else "offset", "Aggregate groups are not paginated."
        )
    group_by = None
    if mode == "grouped":
        group_by = keys(values.get("group_by", ""), "group_by", DIMENSIONS[kind])
        group_by += [name for name in MANDATORY_DIMENSIONS[kind] if name not in group_by]
    elif "group_by" in values:
        invalid_parameter("group_by", "Use mode=grouped or omit mode when grouping.")
    if "representation" in values:
        if values["representation"] not in ("full", "compact") or mode == "rows":
            invalid_parameter("representation", "Use full or compact for aggregate reports.")
    for name in ("include_totals", "include_waste", "include_quality"):
        if name in values:
            boolean(values[name], name)
            if name != "include_waste" and mode == "rows" and kind != "summary":
                invalid_parameter(name, "This option requires an aggregate report.")
    sort = None
    if "sort" in values:
        if mode != "rows":
            invalid_parameter("sort", "Sorting is only supported for source rows.")
        sort = keys(values["sort"], "sort", SORT_FIELDS[kind], descending=True)
        unique = "allocation_id" if kind == "costs" else "usage_id"
        if unique not in [key.removeprefix("-") for key in sort]:
            sort.append(unique)
    return filters, mode, pagination(request, values) if raw else (50, 0), sort, group_by


def keys(value, parameter, allowed, *, descending=False):
    result, seen = value.split(","), set()
    for key in result:
        name = key.removeprefix("-") if descending else key
        if name not in allowed or name in seen:
            invalid_parameter(parameter, "Use distinct supported keys without empty entries.")
        seen.add(name)
    return result


@router.get("/selectors", response=s.SelectorsOut)
def selectors(request):
    query_values(request, [])
    result = read_selectors()
    result["categories"] = [{"key": key, "name": Category(key).label} for key in Category.values]
    return result


@router.get("/summary", response=s.SummaryOut | s.AnalyticalSummaryOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "include_waste",
    "include_quality",
    repeated=["property_id", "vendor_id"],
)
def summary(request):
    filters, _, _, _, _ = report_filters(request, kind="summary")
    result = read_api_report(filters=filters, kind="summary", **report_options(request))
    if any(name in request.GET for name in ("include_waste", "include_quality")):
        result["analytics_version"] = 1
        return analytical_response(s.AnalyticalSummaryOut, result)
    return result


def report_response(request, kind, modes):
    filters, mode, (limit, offset), sort, group_by = report_filters(
        request, modes, raw=True, kind=kind
    )
    if kind == "power" and mode != "rows":
        return analytical_response(
            s.PowerAggregateOut, read_power_aggregates(filters=filters, mode=mode)
        )
    result = read_api_report(
        filters=filters,
        kind=kind,
        mode=mode,
        limit=limit,
        offset=offset,
        sort=sort,
        group_by=group_by,
        **report_options(request),
    )
    if mode == "rows":
        result.update(page_response(request, result["items"], result["count"], limit, offset))
        result["has_more"] = result["next"] is not None
    if mode != "rows" and (
        any(
            name in request.GET
            for name in ("representation", "include_totals", "include_waste", "include_quality")
        )
        or any(
            name in (group_by or [])
            for name in ("invoice_year", "period_start_month", "period_start_year")
        )
    ):
        result.setdefault("representation", "full")
        result.setdefault("analytics_version", 1)
        return analytical_response(s.AnalyticalAggregateOut, result)
    if "include_waste" in request.GET:
        return analytical_response(s.CostReportPage, result)
    return result


def report_options(request):
    result = {}
    for name in ("include_totals", "include_waste", "include_quality"):
        if name in request.GET:
            result[name] = boolean(request.GET[name], name)
    if "representation" in request.GET:
        result["representation"] = request.GET["representation"]
    return result


def analytical_response(schema, result):
    # Exclude unrequested fields while retaining explicitly selected null metrics.
    return JsonResponse(schema.model_validate(result).model_dump(mode="json", exclude_unset=True))


@router.get("/costs", response=s.CostAggregateOut | s.CostReportPage | s.AnalyticalAggregateOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "group_by",
    "currency",
    "sort",
    "limit",
    "offset",
    "representation",
    "include_totals",
    "include_waste",
    "include_quality",
    "year",
    "years",
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["costs"],
    groupable=DIMENSIONS["costs"],
    mandatory=MANDATORY_DIMENSIONS["costs"],
    choices={
        "representation": ("full", "compact"),
    },
)
def costs(request):
    return report_response(request, "costs", ("rows", "totals", "invoice_month", "grouped"))


@router.get(
    "/consumption", response=s.AggregateOut | s.ConsumptionReportPage | s.AnalyticalAggregateOut
)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "group_by",
    "unit",
    "sort",
    "limit",
    "offset",
    "representation",
    "include_totals",
    "include_quality",
    "year",
    "years",
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["consumption"],
    groupable=DIMENSIONS["consumption"],
    mandatory=MANDATORY_DIMENSIONS["consumption"],
    choices={
        "representation": ("full", "compact"),
    },
)
def consumption(request):
    return report_response(request, "consumption", ("rows", "totals", "billed_period", "grouped"))


@router.get("/billed-power", response=s.ReportPage | s.PowerAggregateOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "sort",
    "limit",
    "offset",
    "unit",
    "year",
    "years",
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["power"],
    choices={
        "mode": ("rows", "totals", "billed_period", "period_start_month", "period_start_year"),
        "unit": ("kW",),
    },
)
def billed_power(request):
    return report_response(
        request,
        "power",
        ("rows", "totals", "billed_period", "period_start_month", "period_start_year"),
    )


@router.get("/availability", response=s.AvailabilityOut)
@documented_query(
    "property_id", "vendor_id", "category", "subcategory", repeated=("property_id", "vendor_id")
)
def availability(request):
    query_values(
        request,
        ["property_id", "vendor_id", "category", "subcategory"],
        repeated=("property_id", "vendor_id"),
    )
    filters, _, _, _, _ = report_filters(request)
    return analytical_response(s.AvailabilityOut, read_availability(filters=filters))


@router.get("/comparisons", response=s.ComparisonsOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "metric",
    "year",
    "comparison_year",
    "history",
    repeated=("property_id", "vendor_id"),
    choices={"metric": ("costs", "consumption")},
)
def comparisons(request):
    values = query_values(
        request,
        [
            "property_id",
            "vendor_id",
            "category",
            "subcategory",
            "date_from",
            "date_to",
            "metric",
            "year",
            "comparison_year",
            "history",
        ],
        repeated=("property_id", "vendor_id"),
    )
    metric = values.get("metric", "costs")
    if metric not in ("costs", "consumption"):
        invalid_parameter("metric")
    year = integer(values["year"], "year", minimum=3, maximum=9998) if "year" in values else None
    previous = (
        integer(values["comparison_year"], "comparison_year", minimum=1, maximum=9998)
        if "comparison_year" in values
        else None
    )
    if year and ("date_from" in values or "date_to" in values):
        invalid_parameter("year", "Choose a focus year or explicit dates.")
    if previous == year and year:
        invalid_parameter("comparison_year", "Choose a different comparison year.")
    filter_names = ("property_id", "vendor_id", "category", "subcategory", "date_from", "date_to")
    filters, _, _, _, _ = report_filters(
        request, values={key: value for key, value in values.items() if key in filter_names}
    )
    result = read_comparisons(
        filters=filters,
        metric=metric,
        focus_year=year,
        comparison_year=previous,
        history=boolean(values.get("history", "false"), "history"),
    )
    return analytical_response(s.ComparisonsOut, result)


@router.get("/unit-costs", response=s.UnitCostsOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    repeated=("property_id", "vendor_id"),
)
def unit_costs(request):
    filters, _, _, _, _ = report_filters(request)
    return analytical_response(s.UnitCostsOut, read_unit_costs(filters=filters))


@router.get("/consumption-issues", response=s.ReportPage)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "sort",
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["issues"],
)
def consumption_issues(request):
    return report_response(request, "issues", ("rows",))


@router.get("/invoice-coverage", response=s.CoverageOut)
@documented_query("property_id", "vendor_id", "category", "status", "date_from", "date_to", "as_of")
def invoice_coverage(request):
    values = query_values(
        request, ["property_id", "vendor_id", "category", "status", "date_from", "date_to", "as_of"]
    )
    for name in ("property_id", "vendor_id"):
        if name in values:
            values[name] = integer(values[name], name)
    for name in ("date_from", "date_to", "as_of"):
        if name in values:
            values[name] = iso_date(values[name], name)
    if "category" in values:
        if values["category"] not in Category.values:
            invalid_parameter("category")
        values["service"] = values.pop("category")
    from bills.invoice_coverage import STATUS_LABELS

    if "status" in values and values["status"] not in (*STATUS_LABELS, "attention"):
        invalid_parameter("status")
    values.setdefault("as_of", datetime.now(UTC).date())
    if (
        values.get("date_from")
        and values.get("date_to")
        and values["date_from"] > values["date_to"]
    ):
        invalid_parameter("date_to")
    result = read_invoice_coverage(**values)
    return {
        "as_of": result["as_of"],
        "coverage_rows": result["coverage_rows"],
        "property_services": result["property_services"],
        "counts": {key: value for key, value in result.items() if key.endswith("_count")},
    }


def attention_response(request, kind):
    filters, mode, sort, limit, offset = parse_filters(request, kind)
    result = read_attention_report(
        kind=kind, filters=filters, mode=mode, sort=sort, limit=limit, offset=offset
    )
    return pinned_page(request, result, mode, sort, limit, offset)


@router.get("/missing-invoices", response=s.MissingInvoicesPage | s.CoverageSummary)
@documented_query(
    *QUERY_FIELDS["coverage"],
    repeated=("property_id", "vendor_id"),
    sortable=ATTENTION_SORT_FIELDS["coverage"],
    choices={"mode": ("rows", "summary"), "status": (*STATUS_LABELS, "all", "attention")},
)
def missing_invoices(request):
    return attention_response(request, "coverage")


@router.get("/review-queue", response=s.ReviewQueuePage | s.ReviewQueueSummary)
@documented_query(
    *QUERY_FIELDS["review"],
    repeated=("property_id",),
    sortable=ATTENTION_SORT_FIELDS["review"],
    choices={"mode": ("rows", "summary"), "reason_key": REASONS},
)
def review_queue(request):
    return attention_response(request, "review")


@router.get("/attention", response=s.AttentionPage | s.AttentionSummary)
@documented_query(
    *QUERY_FIELDS["attention"],
    repeated=("property_id",),
    sortable=ATTENTION_SORT_FIELDS["attention"],
    choices={"mode": ("rows", "summary")},
)
def attention(request):
    return attention_response(request, "attention")


@router.get("/invoice-costs", response=s.InvoiceCostsPage)
@documented_query(
    *QUERY_FIELDS["invoice_costs"],
    repeated=("property_id", "vendor_id"),
    sortable=INVOICE_COST_SORT,
)
def invoice_costs(request):
    filters, mode, sort, limit, offset = parse_filters(request, "invoice_costs")
    result = read_invoice_costs(filters=filters, sort=sort, limit=limit, offset=offset)
    return pinned_page(request, result, mode, sort, limit, offset)
