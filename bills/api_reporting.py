"""Bounded approved projections and complete SQL aggregates in one read snapshot."""

import json
from datetime import UTC, date, datetime, timedelta

from django.core.exceptions import ValidationError

from .categories import CATEGORY_LABELS, SUBCATEGORY_LABELS
from .configuration import month_number
from .reporting import SELECTION_REASONS, reporting_reader
from .reporting_data import ReportingSnapshot, consumption_buckets, consumption_groups
from .waste_reporting import ReportTooBroad, attach_waste_weights, read_waste_report

MAX_PROPERTIES = 100
MAX_MONTHS = 600
MAX_GROUPS = 10_000
CONTRACT_VERSION = 1
UNIT_LABELS = {"kWh": "kWh", "m3": "m³", "kg": "kg", "kW": "kW"}
COST_DIMENSIONS = {
    name: name
    for name in (
        "property_id",
        "vendor_id",
        "category",
        "subcategory",
        "currency",
        "period_start",
        "period_end",
    )
}
COST_DIMENSIONS["invoice_month"] = "date(invoice_date, 'start of month')"
COST_DIMENSIONS["invoice_year"] = "CAST(strftime('%Y', invoice_date) AS INTEGER)"
CONSUMPTION_DIMENSIONS = (
    "property_id",
    "vendor_id",
    "utility",
    "unit",
    "subcategory",
    "period_start",
    "period_end",
    "basis",
    "source_service",
    "source_selection",
    "period_start_month",
    "period_start_year",
)
DIMENSIONS = {"costs": COST_DIMENSIONS, "consumption": CONSUMPTION_DIMENSIONS}
MANDATORY_DIMENSIONS = {"costs": ("currency",), "consumption": ("utility", "unit")}
COST_SORT = {
    name: f"r.{name}"
    for name in (
        "invoice_date",
        "invoice_id",
        "allocation_id",
        "property_id",
        "vendor_id",
        "category",
        "subcategory",
        "currency",
        "period_start",
        "period_end",
        "net_minor",
        "vat_minor",
        "gross_minor",
        "invoice_kind",
        "review_revision",
    )
}
USAGE_SORT = {
    name: f"r.{name}"
    for name in (
        "period_start",
        "period_end",
        "invoice_date",
        "invoice_id",
        "allocation_id",
        "usage_id",
        "property_id",
        "vendor_id",
        "utility",
        "subcategory",
        "unit",
        "quantity_milli",
        "basis",
        "source_service",
    )
}
SORT_FIELDS = {
    "costs": COST_SORT,
    "consumption": {**USAGE_SORT, "source_selection": "r.source_selection"},
    "issues": {
        **USAGE_SORT,
        "selection_reason": "r.selection_reason",
        "source_selection": "r.source_selection",
    },
    "power": USAGE_SORT,
}


def require_snapshot(reader, kind):
    """Costs support a SQL reader; usage and combined reports require cached selection."""
    if kind not in ("costs", "consumption", "issues", "power", "summary"):
        raise ValueError(f"Unsupported report kind: {kind}")
    if kind != "costs" and not isinstance(reader, ReportingSnapshot):
        raise TypeError(f"{kind} reporting requires a ReportingSnapshot")


def filters_sql(filters, *, usage=False, dates=True, strict_ids=False):
    clauses, params = [], []
    for name in ("property_id", "vendor_id"):
        if strict_ids and filters.get(name) is not None and not filters[name]:
            clauses.append("0=1")
        if filters.get(name):
            clauses.append(f"{name} IN ({','.join('?' for _ in filters[name])})")
            params.extend(filters[name])
    for name in ("category", "subcategory"):
        if filters.get(name) is not None:
            column = "utility" if usage and name == "category" else name
            clauses.append(f"{column} = ?")
            params.append(filters[name])
    dimension = "unit" if usage else "currency"
    if filters.get(dimension) is not None:
        clauses.append(f"{dimension} = ?")
        params.append(filters[dimension])
    if dates:
        if filters.get("date_from"):
            clauses.append("period_end > ?" if usage else "invoice_date >= ?")
            params.append(filters["date_from"].isoformat())
        if filters.get("date_to"):
            clauses.append("period_start <= ?" if usage else "invoice_date <= ?")
            params.append(filters["date_to"].isoformat())
    return " AND ".join(clauses) or "1=1", params


def resolved_filters(reader, filters, kind, *, strict_ids=False):
    require_snapshot(reader, kind)
    result = dict(filters)
    if len(set(result.get("property_id") or [])) > MAX_PROPERTIES:
        raise ReportTooBroad("100 property IDs")
    if result.get("date_from") and result.get("date_to"):
        validate_date_bounds(result)
        return result
    bounds = []
    projections = (
        ["power"]
        if kind == "power"
        else ["costs"]
        if kind == "costs"
        else ["consumption", "issues"]
        if kind in ("consumption", "issues")
        else ["costs", "consumption", "issues"]
    )
    for projection in projections:
        usage = projection != "costs"
        if usage:
            sources = reader.selected(filters, projection)
            if sources:
                bounds.append(
                    (
                        min(date.fromisoformat(row["period_start"]) for row in sources),
                        max(date.fromisoformat(row["period_end"]) for row in sources)
                        - timedelta(days=1),
                    )
                )
            continue
        where, params = filters_sql(filters, strict_ids=strict_ids)
        row = reader.execute(
            f"SELECT min(invoice_date), max(invoice_date) FROM reporting_costs WHERE {where}",
            params,
        ).fetchone()
        if row[0] is not None:
            bounds.append((date.fromisoformat(row[0]), date.fromisoformat(row[1])))
    result["date_from"] = result.get("date_from") or (
        min(row[0] for row in bounds) if bounds else None
    )
    result["date_to"] = result.get("date_to") or (max(row[1] for row in bounds) if bounds else None)
    validate_date_bounds(result)
    return result


def validate_date_bounds(result):
    if result.get("date_from") and result.get("date_to"):
        if result["date_from"] > result["date_to"]:
            raise ValidationError({"date_to": "End date must be on or after start date."})
        if month_number(result["date_to"]) - month_number(result["date_from"]) + 1 > MAX_MONTHS:
            raise ReportTooBroad("600 calendar months")


def aggregate(reader, filters, kind, mode, group_by=None, *, buckets=None):
    require_snapshot(reader, kind)
    if kind == "consumption":
        if buckets is None:
            buckets = consumption_buckets(
                reader.selected(filters, kind), mode, group_by, MAX_GROUPS
            )
        return timestamps(consumption_groups(buckets))
    if kind != "costs":
        raise ValueError(f"Unsupported aggregate kind: {kind}")
    where, params = filters_sql(filters)
    if mode == "grouped":
        dimensions = DIMENSIONS[kind]
        columns = [f"{dimensions[name]} AS {name}" for name in group_by]
        group = [dimensions[name] for name in group_by]
        for name in ("property", "vendor"):
            if f"{name}_id" in group_by:
                columns.append(f"{name}_name")
                group.append(f"{name}_name")
    else:
        columns = ["property_id", "property_name", "category", "subcategory", "currency"]
        group = list(columns)
        if mode == "invoice_month":
            columns += ["date(invoice_date, 'start of month') AS invoice_month"]
            group += ["date(invoice_date, 'start of month')"]
    summed = (
        ", ".join(
            f"CASE WHEN count({field})=count(*) THEN sum({field}) END AS {field}"
            for field in ("net_minor", "vat_minor", "gross_minor")
        )
        + ", count(*) AS allocation_count"
    )
    summed += ", count(DISTINCT invoice_id) AS invoice_count"
    sql = (
        f"SELECT {', '.join(columns)}, {summed} FROM reporting_costs WHERE {where} "
        + f"GROUP BY {', '.join(group)} ORDER BY {', '.join(group)} LIMIT {MAX_GROUPS + 1}"
    )
    groups = [dict(row) for row in reader.execute(sql, params)]
    if len(groups) > MAX_GROUPS:
        raise ReportTooBroad("10,000 aggregate groups")
    return timestamps(groups)


def timestamps(rows):
    for row in rows:
        for name in (
            "invoice_date",
            "invoice_month",
            "period_start",
            "period_end",
            "period_start_month",
            "invoice_year",
            "period_start_year",
        ):
            if row.get(name) is not None:
                value = (
                    date(row[name], 1, 1)
                    if name.endswith("_year")
                    else date.fromisoformat(row[name])
                    if isinstance(row[name], str)
                    else row[name]
                )
                time_name = "invoice_time" if name == "invoice_date" else f"{name}_time"
                row[time_name] = (value - date(1970, 1, 1)).days * 86400
    return rows


def report_labels(reader):
    return {
        row["key"]: row["name"] for row in reader.execute("SELECT key, name FROM bills_subcategory")
    }


def labels(rows, catalogue, *, category=None):
    for row in rows:
        for field, display in (("category", "category_name"), ("utility", "utility_name")):
            if row.get(field) is not None:
                row[display] = CATEGORY_LABELS.get(row[field], row[field])
        if row.get("subcategory") is not None:
            key = row["subcategory"]
            row["subcategory_name"] = (
                catalogue.get(key, SUBCATEGORY_LABELS.get(key, key))
                if key
                else "Unspecified"
                if (row.get("category") or row.get("utility") or category) == "waste"
                else ""
            )
        if row.get("unit") is not None:
            row["unit_label"] = UNIT_LABELS.get(row["unit"], row["unit"])
        if row.get("selection_reason"):
            row["reason"] = SELECTION_REASONS.get(row["selection_reason"], row["selection_reason"])
    return rows


def metadata():
    return {"generated_at": datetime.now(UTC), "contract_version": CONTRACT_VERSION}


def read_api_report(
    *,
    filters,
    kind,
    mode="rows",
    limit=50,
    offset=0,
    sort=None,
    group_by=None,
    representation="full",
    include_totals=True,
    include_waste=True,
    include_quality=False,
):
    with reporting_reader() as database:
        reader = ReportingSnapshot(
            database, filters, full_history=include_quality and kind != "costs"
        )
        catalogue = report_labels(reader)
        generated = metadata()
        if filters.get("subcategory") is not None and filters["subcategory"] not in catalogue:
            from .api.errors import invalid_parameter

            invalid_parameter("subcategory")
        filters = resolved_filters(reader, filters, kind)
        base = {
            **generated,
            "filters": filters,
            "effective_date_bounds": {name: filters.get(name) for name in ("date_from", "date_to")},
        }
        if representation == "compact":
            base["analytics_version"] = 1
        if include_quality:
            from .analytics_reporting import report_quality

            base["quality"] = report_quality(reader, filters, kind)
        if kind in ("summary", "costs") and include_waste:
            cost_where, cost_params = filters_sql(filters)
            base.update(read_waste_report(reader, cost_where, cost_params, filters=filters))
        if kind == "summary":
            costs = aggregate(reader, filters, "costs", "totals")
            consumption = aggregate(reader, filters, "consumption", "totals")
            if len(costs) + len(consumption) > MAX_GROUPS:
                raise ReportTooBroad("10,000 aggregate groups")
            cost_where, cost_params = filters_sql(filters)
            selected_ids = {
                row[0]
                for row in reader.execute(
                    f"SELECT DISTINCT invoice_id FROM reporting_costs WHERE {cost_where}",
                    cost_params,
                )
            }
            selected_usage = reader.selected(filters, "consumption")
            issues = reader.selected(filters, "issues")
            selected_ids.update(row["invoice_id"] for row in [*selected_usage, *issues])
            sources = reader.execute(
                """
                SELECT count(*) AS total_invoices,
                       coalesce(sum(d.source='manual'), 0) AS manual_invoices,
                       max(i.approved_at) AS latest_approval
                FROM bills_invoice i JOIN bills_document d ON d.id=i.document_id
                WHERE i.id IN (SELECT value FROM json_each(?))
                """,
                [json.dumps(sorted(selected_ids))],
            ).fetchone()
            exclusions = len(issues)
            return {
                **base,
                "costs": labels(costs, catalogue),
                "consumption": labels(consumption, catalogue),
                "total_invoices": sources[0],
                "manual_invoices": sources[1],
                "pdf_invoices": sources[0] - sources[1],
                "exclusion_count": exclusions,
                "latest_approval": datetime.fromisoformat(sources[2]).replace(tzinfo=UTC)
                if sources[2] is not None
                else None,
            }
        if mode != "rows":
            buckets = (
                consumption_buckets(reader.selected(filters, kind), mode, group_by, MAX_GROUPS)
                if kind == "consumption"
                else None
            )
            groups = (
                aggregate(reader, filters, kind, mode, group_by, buckets=buckets)
                if buckets is not None
                else aggregate(reader, filters, kind, mode, group_by)
            )
            totals = (
                (groups if mode == "totals" else aggregate(reader, filters, kind, "totals"))
                if include_totals
                else []
            )
            result = {
                **base,
                "mode": mode,
                "groups": labels(groups, catalogue, category=filters.get("category")),
                "totals": labels(totals, catalogue),
                "group_count": len(groups),
                **({"group_by": group_by} if mode == "grouped" else {}),
            }
            if not include_totals:
                result.pop("totals")
            if representation == "compact":
                result["representation"] = "compact"
                if include_quality and kind == "consumption":
                    from .analytics_reporting import group_quality

                    group_quality(result["groups"], buckets)
            return result
        if kind in ("consumption", "issues"):
            all_rows = reader.selected(filters, kind)
            order = sort or ["period_start", "period_end", "invoice_id", "usage_id"]
            for key in reversed(order):
                field = key.removeprefix("-")
                all_rows.sort(
                    key=lambda row: (row.get(field) is not None, row.get(field)),
                    reverse=key.startswith("-"),
                )
            rows = all_rows[offset : offset + limit]
            documents = {row["document_id"] for row in rows}
            sources = (
                {
                    row[0]: row[1]
                    for row in reader.execute(
                        "SELECT id, source FROM bills_document WHERE id IN ("
                        + ",".join("?" for _ in documents)
                        + ")",
                        list(documents),
                    )
                }
                if documents
                else {}
            )
            for row in rows:
                row["source"] = sources[row["document_id"]]
            return {
                **base,
                "mode": "rows",
                "items": labels(timestamps(rows), catalogue),
                "count": len(all_rows),
            }
        usage = kind != "costs"
        where, params = filters_sql(filters, usage=usage)
        table = {
            "costs": "reporting_costs",
            "power": "reporting_electricity_demand",
        }[kind]
        count = reader.execute(f"SELECT count(*) FROM {table} WHERE {where}", params).fetchone()[0]
        order = (
            "invoice_date, invoice_id, allocation_id"
            if kind == "costs"
            else "period_start, period_end, invoice_id, usage_id"
        )
        if sort:
            order = ", ".join(
                SORT_FIELDS[kind][key.removeprefix("-")]
                + (" DESC" if key.startswith("-") else " ASC")
                for key in sort
            )
        rows = [
            dict(row)
            for row in reader.execute(
                f"SELECT r.*, d.source FROM {table} r "
                + "JOIN bills_document d ON d.id=r.document_id "
                + f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
        if kind == "costs" and include_waste:
            attach_waste_weights(reader, rows)
        return {
            **base,
            "mode": "rows",
            "items": labels(timestamps(rows), catalogue),
            "count": count,
        }


def read_selectors():
    with reporting_reader() as reader:
        subcategories = [
            dict(row)
            for row in reader.execute(
                "SELECT key, name, category, is_active, is_waste_fraction "
                "FROM bills_subcategory ORDER BY key"
            )
        ]
        return {
            **metadata(),
            "units": [{"key": key, "name": name} for key, name in UNIT_LABELS.items()],
            "properties": [
                dict(row)
                for row in reader.execute("SELECT id, name FROM bills_property ORDER BY name, id")
            ],
            "vendors": [
                dict(row)
                for row in reader.execute("SELECT id, name FROM bills_vendor ORDER BY name, id")
            ],
            "subcategories": subcategories,
        }


INVOICE_COST_SORT = (
    "invoice_date",
    "invoice_id",
    "vendor_name",
    "currency",
    "invoice_kind",
    "review_revision",
    "net_minor",
    "vat_minor",
    "gross_minor",
)


def read_invoice_costs(*, filters, limit=50, offset=0, sort=None):
    """Paginate complete invoice shares, without returning allocation details."""
    with reporting_reader() as reader:
        catalogue = report_labels(reader)
        if filters.get("subcategory") is not None and filters["subcategory"] not in catalogue:
            from .api.errors import invalid_parameter

            invalid_parameter("subcategory")
        filters = resolved_filters(reader, filters, "costs", strict_ids=True)
        base = {
            **metadata(),
            "filters": filters,
            "effective_date_bounds": {key: filters.get(key) for key in ("date_from", "date_to")},
        }
        where, params = filters_sql(filters, strict_ids=True)
        cte = f"WITH selected AS (SELECT * FROM reporting_costs WHERE {where}), grouped AS (" + (
            "SELECT invoice_id, currency, document_id, vendor_id, vendor_name, invoice_date, "
            "invoice_kind, review_revision, "
            + ", ".join(
                f"CASE WHEN count({field})=count(*) THEN sum({field}) END AS {field}"
                for field in ("net_minor", "vat_minor", "gross_minor")
            )
            + ", count(*) AS allocation_count FROM selected GROUP BY invoice_id, currency) "
        )
        count = reader.execute(cte + "SELECT count(*) FROM grouped", params).fetchone()[0]
        if count > MAX_GROUPS:
            raise ReportTooBroad("10,000 invoice cost groups")
        order = []
        for key in sort or ("-invoice_date", "invoice_id"):
            name = key.removeprefix("-")
            if name not in INVOICE_COST_SORT:
                raise ValueError("Unsupported invoice sort key")
            order += [
                f"g.{name} IS NULL",
                f"g.{name}" + (" DESC" if key.startswith("-") else " ASC"),
            ]
        rows = [
            dict(row)
            for row in reader.execute(
                cte
                + "SELECT g.*, d.source FROM grouped g JOIN bills_document d ON d.id=g.document_id "
                + f"ORDER BY {', '.join(order)} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
        properties = {}
        if rows:
            ids = [row["invoice_id"] for row in rows]
            for property_row in reader.execute(
                cte
                + "SELECT DISTINCT invoice_id, property_id, property_name FROM selected "
                + f"WHERE invoice_id IN ({','.join('?' for _ in ids)}) ORDER BY property_id",
                [*params, *ids],
            ):
                properties.setdefault(property_row["invoice_id"], []).append(
                    {key: property_row[key] for key in ("property_id", "property_name")}
                )
        for row in rows:
            row["properties"] = properties.get(row["invoice_id"], [])
        return {**base, "count": count, "items": timestamps(rows)}
