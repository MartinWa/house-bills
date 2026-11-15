"""Bounded approved projections and complete SQL aggregates in one read snapshot."""

from datetime import UTC, date, datetime

from django.core.exceptions import ValidationError

from .categories import CATEGORY_LABELS, SUBCATEGORY_LABELS
from .configuration import month_number
from .reporting import SELECTION_REASONS, reporting_reader
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
CONSUMPTION_DIMENSIONS = {
    name: name
    for name in (
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
    )
}
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
SELECTED = """
WITH selected_usage AS (
    SELECT * FROM reporting_electricity_usage
    UNION ALL
    SELECT u.*, '' AS selection_reason, 'billed' AS source_selection
    FROM reporting_usage u WHERE utility <> 'electricity'
)
"""


def filters_sql(filters, *, usage=False, dates=True):
    clauses, params = [], []
    for name in ("property_id", "vendor_id"):
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


def resolved_filters(reader, filters, kind):
    result = dict(filters)
    if len(set(result.get("property_id", []))) > MAX_PROPERTIES:
        raise ReportTooBroad("100 property IDs")
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
        where, params = filters_sql(filters, usage=usage)
        table = {
            "costs": "reporting_costs",
            "consumption": "selected_usage",
            "issues": "reporting_electricity_selection",
            "power": "reporting_electricity_demand",
        }[projection]
        if projection == "issues":
            where += " AND source_selection = 'needs_review'"
        endpoints = (
            "min(period_start), max(date(period_end, '-1 day'))"
            if usage
            else "min(invoice_date), max(invoice_date)"
        )
        row = reader.execute(
            (SELECTED if table == "selected_usage" else "")
            + f"SELECT {endpoints} FROM {table} WHERE {where}",
            params,
        ).fetchone()
        if row[0] is not None:
            bounds.append((date.fromisoformat(row[0]), date.fromisoformat(row[1])))
    result["date_from"] = result.get("date_from") or (
        min(row[0] for row in bounds) if bounds else None
    )
    result["date_to"] = result.get("date_to") or (max(row[1] for row in bounds) if bounds else None)
    if result["date_from"] and result["date_to"]:
        if result["date_from"] > result["date_to"]:
            raise ValidationError({"date_to": "End date must be on or after start date."})
        if month_number(result["date_to"]) - month_number(result["date_from"]) + 1 > MAX_MONTHS:
            raise ReportTooBroad("600 calendar months")
    return result


def aggregate(reader, filters, kind, mode, group_by=None):
    usage = kind == "consumption"
    where, params = filters_sql(filters, usage=usage)
    if mode == "grouped":
        dimensions = DIMENSIONS[kind]
        columns = [f"{dimensions[name]} AS {name}" for name in group_by]
        group = [dimensions[name] for name in group_by]
        for name in ("property", "vendor"):
            if f"{name}_id" in group_by:
                columns.append(f"{name}_name")
                group.append(f"{name}_name")
    elif usage:
        # Electricity totals combine selected sources; classified water/waste remain separate.
        subcategory = (
            "CASE WHEN utility='electricity' THEN '' ELSE subcategory END AS subcategory"
            if mode == "totals"
            else "subcategory"
        )
        columns = ["property_id", "property_name", "utility", "unit", subcategory]
        group = ["property_id", "property_name", "utility", "unit", "5"]
        if mode == "billed_period":
            columns += ["period_start", "period_end", "basis", "source_service", "source_selection"]
            group += ["period_start", "period_end", "basis", "source_service", "source_selection"]
    else:
        columns = ["property_id", "property_name", "category", "subcategory", "currency"]
        group = list(columns)
        if mode == "invoice_month":
            columns += ["date(invoice_date, 'start of month') AS invoice_month"]
            group += ["date(invoice_date, 'start of month')"]
    if usage:
        summed = (
            "sum(quantity_milli) AS quantity_milli, "
            "count(DISTINCT allocation_id) AS allocation_count, count(*) AS usage_count"
        )
        source, table = SELECTED + "SELECT ", "selected_usage"
    else:
        summed = (
            ", ".join(
                f"CASE WHEN count({field})=count(*) THEN sum({field}) END AS {field}"
                for field in ("net_minor", "vat_minor", "gross_minor")
            )
            + ", count(*) AS allocation_count"
        )
        source, table = "SELECT ", "reporting_costs"
    summed += ", count(DISTINCT invoice_id) AS invoice_count"
    sql = (
        source
        + f"{', '.join(columns)}, {summed} FROM {table} WHERE {where} "
        + f"GROUP BY {', '.join(group)} ORDER BY {', '.join(group)} LIMIT {MAX_GROUPS + 1}"
    )
    groups = [dict(row) for row in reader.execute(sql, params)]
    if len(groups) > MAX_GROUPS:
        raise ReportTooBroad("10,000 aggregate groups")
    return timestamps(groups)


def timestamps(rows):
    for row in rows:
        for name in ("invoice_date", "invoice_month", "period_start", "period_end"):
            if row.get(name) is not None:
                value = date.fromisoformat(row[name]) if isinstance(row[name], str) else row[name]
                time_name = "invoice_time" if name == "invoice_date" else f"{name}_time"
                row[time_name] = (value - date(1970, 1, 1)).days * 86400
    return rows


def report_labels(reader):
    return {
        row["key"]: row["name"] for row in reader.execute("SELECT key, name FROM bills_subcategory")
    }


def labels(rows, catalogue):
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
                if row.get("category", row.get("utility")) == "waste"
                else ""
            )
        if row.get("unit") is not None:
            row["unit_label"] = UNIT_LABELS.get(row["unit"], row["unit"])
        if row.get("selection_reason"):
            row["reason"] = SELECTION_REASONS.get(row["selection_reason"], row["selection_reason"])
    return rows


def metadata():
    return {"generated_at": datetime.now(UTC), "contract_version": CONTRACT_VERSION}


def read_api_report(*, filters, kind, mode="rows", limit=50, offset=0, sort=None, group_by=None):
    with reporting_reader() as reader:
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
        if kind in ("summary", "costs"):
            cost_where, cost_params = filters_sql(filters)
            base.update(read_waste_report(reader, cost_where, cost_params, filters=filters))
        if kind == "summary":
            costs = aggregate(reader, filters, "costs", "totals")
            consumption = aggregate(reader, filters, "consumption", "totals")
            if len(costs) + len(consumption) > MAX_GROUPS:
                raise ReportTooBroad("10,000 aggregate groups")
            cost_where, cost_params = filters_sql(filters)
            usage_where, usage_params = filters_sql(filters, usage=True)
            sources = reader.execute(
                SELECTED
                + f"""
                SELECT count(*) AS total_invoices,
                       coalesce(sum(d.source='manual'), 0) AS manual_invoices,
                       max(i.approved_at) AS latest_approval
                FROM bills_invoice i JOIN bills_document d ON d.id=i.document_id
                WHERE i.id IN (
                    SELECT invoice_id FROM reporting_costs WHERE {cost_where}
                    UNION SELECT invoice_id FROM selected_usage WHERE {usage_where}
                    UNION SELECT invoice_id FROM reporting_electricity_selection
                    WHERE source_selection='needs_review' AND {usage_where}
                )
                """,
                [*cost_params, *usage_params, *usage_params],
            ).fetchone()
            exclusions = reader.execute(
                "SELECT count(*) FROM reporting_electricity_selection "
                f"WHERE source_selection='needs_review' AND {usage_where}",
                usage_params,
            ).fetchone()[0]
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
            groups = aggregate(reader, filters, kind, mode, group_by)
            totals = groups if mode == "totals" else aggregate(reader, filters, kind, "totals")
            return {
                **base,
                "mode": mode,
                "groups": labels(groups, catalogue),
                "totals": labels(totals, catalogue),
                "group_count": len(groups),
                **({"group_by": group_by} if mode == "grouped" else {}),
            }
        usage = kind != "costs"
        where, params = filters_sql(filters, usage=usage)
        table = {
            "costs": "reporting_costs",
            "consumption": "selected_usage",
            "issues": "reporting_electricity_selection",
            "power": "reporting_electricity_demand",
        }[kind]
        cte = SELECTED if table == "selected_usage" else ""
        if kind == "issues":
            where += " AND source_selection='needs_review'"
        count = reader.execute(
            cte + f"SELECT count(*) FROM {table} WHERE {where}", params
        ).fetchone()[0]
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
                cte
                + f"SELECT r.*, d.source FROM {table} r "
                + "JOIN bills_document d ON d.id=r.document_id "
                + f"WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
                [*params, limit, offset],
            )
        ]
        if kind == "costs":
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
