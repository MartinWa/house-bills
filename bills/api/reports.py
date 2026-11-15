"""Approved reports support complete aggregates and paginated source detail."""

from datetime import UTC, datetime

from ninja import Router

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
from .collections import integer, iso_date, page_response, pagination, query_values
from .contract import documented_query
from .errors import ApiError, invalid_parameter

router = Router(tags=["Read-only reporting"])


def report_filters(request, modes=(), *, raw=False, kind=None):
    allowed = ["property_id", "vendor_id", "category", "subcategory", "date_from", "date_to"]
    if modes:
        allowed.append("mode")
    if raw:
        allowed += ["limit", "offset", "sort"]
    if kind in DIMENSIONS:
        allowed += ["group_by", "currency" if kind == "costs" else "unit"]
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
    if kind in DIMENSIONS:
        name, supported = (
            ("currency", ("SEK",)) if kind == "costs" else ("unit", ("kWh", "m3", "kg"))
        )
        filters[name] = values.get(name)
        if filters[name] is not None and filters[name] not in supported:
            invalid_parameter(name)
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


@router.get("/summary", response=s.SummaryOut)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    repeated=["property_id", "vendor_id"],
)
def summary(request):
    filters, _, _, _, _ = report_filters(request)
    return read_api_report(filters=filters, kind="summary")


def report_response(request, kind, modes):
    filters, mode, (limit, offset), sort, group_by = report_filters(
        request, modes, raw=True, kind=kind
    )
    result = read_api_report(
        filters=filters,
        kind=kind,
        mode=mode,
        limit=limit,
        offset=offset,
        sort=sort,
        group_by=group_by,
    )
    if mode == "rows":
        result.update(page_response(request, result["items"], result["count"], limit, offset))
        result["has_more"] = result["next"] is not None
    return result


@router.get("/costs", response=s.CostAggregateOut | s.CostReportPage)
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
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["costs"],
    groupable=DIMENSIONS["costs"],
    mandatory=MANDATORY_DIMENSIONS["costs"],
)
def costs(request):
    return report_response(request, "costs", ("rows", "totals", "invoice_month", "grouped"))


@router.get("/consumption", response=s.AggregateOut | s.ConsumptionReportPage)
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
    repeated=["property_id", "vendor_id"],
    sortable=SORT_FIELDS["consumption"],
    groupable=DIMENSIONS["consumption"],
    mandatory=MANDATORY_DIMENSIONS["consumption"],
)
def consumption(request):
    return report_response(request, "consumption", ("rows", "totals", "billed_period", "grouped"))


@router.get("/billed-power", response=s.ReportPage)
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
    sortable=SORT_FIELDS["power"],
)
def billed_power(request):
    return report_response(request, "power", ("rows",))


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
