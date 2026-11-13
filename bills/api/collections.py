"""Strict query parsing and deterministic record pagination."""

from datetime import date, datetime
from urllib.parse import urlencode

from django.db.models import Q

from .errors import invalid_parameter


def query_values(request, allowed, *, repeated=()):
    unknown = set(request.GET) - set(allowed)
    if unknown:
        invalid_parameter(sorted(unknown)[0], "Unknown query parameter.")
    result = {}
    for key in request.GET:
        values = request.GET.getlist(key)
        if key not in repeated and len(values) != 1:
            invalid_parameter(key, "This parameter accepts one value.")
        result[key] = values if key in repeated else values[0]
    return result


def integer(value, name, *, minimum=1, maximum=2**63 - 1):
    try:
        if not str(value).isascii() or not str(value).isdigit():
            raise ValueError
        value = int(value)
        if not minimum <= value <= maximum:
            raise ValueError
    except ValueError, TypeError:
        invalid_parameter(name)
    return value


def boolean(value, name):
    if value not in ("true", "false"):
        invalid_parameter(name, "Use true or false.")
    return value == "true"


def iso_date(value, name, *, month=False):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value or (month and parsed.day != 1):
            raise ValueError
        return parsed
    except ValueError, TypeError:
        invalid_parameter(name, "Use an ISO first-of-month date." if month else "Use an ISO date.")


def iso_datetime(value, name):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError
        return parsed
    except ValueError, TypeError:
        invalid_parameter(name, "Use an ISO timestamp with a timezone.")


def pagination(request, values):
    limit = integer(values.get("limit", "50"), "limit", maximum=200)
    offset = integer(values.get("offset", "0"), "offset", minimum=0)
    return limit, offset


def page_response(request, items, count, limit, offset):
    def link(at):
        query = request.GET.copy()
        query["limit"], query["offset"] = str(limit), str(at)
        return f"{request.path}?{urlencode(list(query.lists()), doseq=True)}"

    return {
        "items": items,
        "count": count,
        "next": link(offset + limit) if offset + limit < count else None,
        "previous": link(max(0, offset - limit)) if offset else None,
    }


def filter_collection(queryset, request, filters, default, sortable, *, choices=None):
    values = query_values(request, [*filters, "sort", "limit", "offset"])
    for name, lookup in filters.items():
        if name not in values:
            continue
        value = values[name]
        if name == "active_month":
            value = iso_date(value, name, month=True)
            queryset = queryset.filter(start_month__lte=value).filter(
                Q(end_month__isnull=True) | Q(end_month__gte=value)
            )
            continue
        if name in ("enabled", "is_waste_fraction"):
            value = boolean(value, name)
        elif name.endswith("_id"):
            value = integer(value, name)
        elif name.startswith("created_"):
            value = iso_datetime(value, name)
        elif "month_" in name:
            value = iso_date(value, name, month=True)
        if choices and name in choices and value not in choices[name]:
            invalid_parameter(name)
        queryset = queryset.filter(**{lookup: value})
    order = values.get("sort", ",".join(default)).split(",")
    for key in order:
        if key.removeprefix("-") not in sortable:
            invalid_parameter("sort", "Unknown sort key.")
    ordering = [("-" if key.startswith("-") else "") + sortable[key.lstrip("-")] for key in order]
    tiebreaker = "key" if "key" in sortable else "id"
    if not any(key.lstrip("-") == tiebreaker for key in order):
        ordering.append(tiebreaker)
    return queryset.order_by(*ordering), pagination(request, values)
