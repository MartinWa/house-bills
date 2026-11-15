"""Strict query contract and pinned continuations for reporting projections."""

from datetime import UTC, datetime
from types import SimpleNamespace

from bills.api_reporting import INVOICE_COST_SORT, MAX_MONTHS
from bills.attention_reporting import DEFAULT_SORT, REASONS, SORT_FIELDS, UNIQUE_KEY
from bills.configuration import month_number
from bills.invoice_coverage import STATUS_LABELS
from bills.models import Category
from bills.waste_reporting import ReportTooBroad

from .collections import integer, iso_date, page_response, pagination, query_values
from .errors import ApiError, invalid_parameter

QUERY_FIELDS = {
    "coverage": (
        "property_id",
        "vendor_id",
        "category",
        "subcategory",
        "date_from",
        "date_to",
        "as_of",
        "status",
        "mode",
        "sort",
        "limit",
        "offset",
    ),
    "review": (
        "property_id",
        "reason_key",
        "date_from",
        "date_to",
        "as_of",
        "mode",
        "sort",
        "limit",
        "offset",
    ),
    "attention": (
        "property_id",
        "as_of",
        "coverage_date_from",
        "coverage_date_to",
        "review_date_from",
        "review_date_to",
        "mode",
        "sort",
        "limit",
        "offset",
    ),
    "invoice_costs": (
        "property_id",
        "vendor_id",
        "category",
        "subcategory",
        "currency",
        "date_from",
        "date_to",
        "sort",
        "limit",
        "offset",
    ),
}


def date_bounds(filters, start_key, end_key, *, as_of=None, coverage=False):
    start, end = filters[start_key], filters[end_key]
    if start and end and start > end:
        invalid_parameter(end_key, "End date must be on or after start date.")
    if coverage:
        start = (start or as_of.replace(month=1, day=1)).replace(day=1)
        end = (end or as_of).replace(day=1)
        if start > end:
            invalid_parameter(end_key, "End date must be on or after start date.")
        # Domain month stepping needs the following month's first day.
        if end.year == 9999 and end.month == 12:
            invalid_parameter(end_key, "The last representable month is unsupported.")
    if start and end and month_number(end) - month_number(start) + 1 > MAX_MONTHS:
        raise ReportTooBroad("600 calendar months")
    filters[start_key], filters[end_key] = start, end


def parse_filters(request, kind):
    values = query_values(request, QUERY_FIELDS[kind], repeated=("property_id", "vendor_id"))
    filters = {}
    for name in ("property_id", "vendor_id"):
        if name not in QUERY_FIELDS[kind]:
            continue
        filters[name] = (
            sorted({integer(value, name) for value in values[name]}) if name in values else None
        )
        if len(filters[name] or []) > 100:
            raise ApiError(422, "filter_too_broad", "At most 100 distinct IDs are accepted.")
    for name in QUERY_FIELDS[kind]:
        if name.endswith(("_from", "_to")):
            filters[name] = iso_date(values[name], name) if name in values else None
    if kind != "invoice_costs":
        filters["as_of"] = (
            iso_date(values["as_of"], "as_of") if "as_of" in values else datetime.now(UTC).date()
        )
    for name in ("category", "subcategory", "currency", "reason_key"):
        if name in QUERY_FIELDS[kind]:
            filters[name] = values.get(name)
    for name, allowed in (
        ("category", Category.values),
        ("currency", ("SEK",)),
        ("reason_key", REASONS),
    ):
        if filters.get(name) is not None and filters[name] not in allowed:
            invalid_parameter(name)
    if kind == "coverage":
        filters["status"] = values.get("status", "all")
        if filters["status"] not in (*STATUS_LABELS, "all", "attention"):
            invalid_parameter("status")
    if kind == "attention":
        date_bounds(
            filters, "coverage_date_from", "coverage_date_to", coverage=True, as_of=filters["as_of"]
        )
        date_bounds(filters, "review_date_from", "review_date_to")
    else:
        date_bounds(
            filters, "date_from", "date_to", coverage=kind == "coverage", as_of=filters.get("as_of")
        )
    mode = values.get("mode", "rows")
    if mode not in ("summary", "rows"):
        invalid_parameter("mode")
    for name in ("sort", "limit", "offset"):
        if name in values and mode == "summary":
            invalid_parameter(name, "Summary mode is unpaginated and unsorted.")
    from .reports import keys

    allowed = INVOICE_COST_SORT if kind == "invoice_costs" else SORT_FIELDS[kind]
    default = ("-invoice_date", "invoice_id") if kind == "invoice_costs" else DEFAULT_SORT[kind]
    unique = "invoice_id" if kind == "invoice_costs" else UNIQUE_KEY[kind]
    sort = (
        keys(values["sort"], "sort", allowed, descending=True)
        if "sort" in values
        else list(default)
    )
    if unique not in [key.removeprefix("-") for key in sort]:
        sort.append(unique)
    limit, offset = pagination(request, values)
    return filters, mode, sort, limit, offset


def pinned_page(request, result, mode, sort, limit, offset):
    if mode == "summary":
        return result
    query = request.GET.copy()
    for key, value in result["filters"].items():
        if value is not None and (key == "as_of" or key.endswith(("_from", "_to"))):
            query[key] = value.isoformat()
    if "mode" in request.GET or "mode" in result:
        query["mode"] = mode
    query["sort"] = ",".join(sort)
    pinned = SimpleNamespace(GET=query, path=request.path)
    result.update(page_response(pinned, result["items"], result["count"], limit, offset))
    result["has_more"] = result["next"] is not None
    return result
