"""Approved reports support complete aggregates and paginated source detail."""

from datetime import UTC, datetime

from ninja import Router

from bills.api_reporting import read_api_report, read_selectors
from bills.invoice_coverage import read_invoice_coverage
from bills.models import Category, Subcategory

from . import schemas as s
from .collections import integer, iso_date, page_response, pagination, query_values
from .contract import documented_query
from .errors import ApiError, invalid_parameter

router = Router(tags=["Approved reporting"])


def report_filters(request, modes=(), *, raw=False):
    allowed = ["property_id", "vendor_id", "category", "subcategory", "date_from", "date_to"]
    if modes:
        allowed.append("mode")
    if raw:
        allowed += ["limit", "offset"]
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
    if (
        filters["subcategory"] is not None
        and not Subcategory.objects.filter(pk=filters["subcategory"]).exists()
    ):
        invalid_parameter("subcategory")
    mode = values.get("mode", "rows")
    if modes and mode not in modes:
        invalid_parameter("mode", "Unknown aggregate mode.")
    if mode != "rows" and ("limit" in values or "offset" in values):
        invalid_parameter(
            "limit" if "limit" in values else "offset", "Aggregate groups are not paginated."
        )
    return filters, mode, pagination(request, values) if raw else (50, 0)


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
    filters, _, _ = report_filters(request)
    return read_api_report(filters=filters, kind="summary")


def report_response(request, kind, modes):
    filters, mode, (limit, offset) = report_filters(request, modes, raw=True)
    result = read_api_report(filters=filters, kind=kind, mode=mode, limit=limit, offset=offset)
    if mode == "rows":
        result.update(page_response(request, result["items"], result["count"], limit, offset))
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
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
)
def costs(request):
    return report_response(request, "costs", ("rows", "totals", "invoice_month"))


@router.get("/consumption", response=s.AggregateOut | s.ReportPage)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
)
def consumption(request):
    return report_response(request, "consumption", ("rows", "totals", "billed_period"))


@router.get("/billed-power", response=s.ReportPage)
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "date_from",
    "date_to",
    "mode",
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
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
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
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
