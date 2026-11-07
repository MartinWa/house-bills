"""Read live approved reports in a single, read-only SQLite snapshot."""

import sqlite3
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path

from django.db import connection

from .categories import CATEGORY_LABELS, subcategory_label
from .models import Service, Usage

SELECTION_REASONS = {
    "same_source_overlap": "Overlapping consumption from the same source needs review.",
    "network_source_overlap": "Overlapping network consumption prevents source selection.",
    "partial_network_coverage": "Network readings cover only part of this supply period.",
}

SELECTED_USAGE = """
WITH selected_usage AS (
    SELECT property_id, property_name, utility, unit, quantity_milli,
           source_service, subcategory, basis, period_start, period_end, source_selection
    FROM reporting_electricity_usage
    UNION ALL
    SELECT property_id, property_name, utility, unit, quantity_milli,
           source_service, subcategory, basis, period_start, period_end,
           'billed' AS source_selection
    FROM reporting_usage WHERE utility <> 'electricity'
)
"""


@contextmanager
def reporting_reader():
    """DEFERRED reads keep one snapshot without acquiring an IMMEDIATE write lock."""
    database = Path(connection.settings_dict["NAME"]).resolve().as_uri()
    with closing(sqlite3.connect(f"{database}?mode=ro", uri=True, timeout=10)) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        reader.execute("BEGIN")
        yield reader


def _filters(property_id, date_from, date_to, *, usage=False):
    clauses, params = [], []
    if property_id is not None:
        clauses.append("property_id = ?")
        params.append(property_id)
    if date_from is not None:
        clauses.append("period_end > ?" if usage else "invoice_date >= ?")
        params.append(date_from.isoformat())
    if date_to is not None:
        clauses.append("period_start <= ?" if usage else "invoice_date <= ?")
        params.append(date_to.isoformat())
    return " AND ".join(clauses) or "1 = 1", params


def _rows(reader, sql, params):
    return [dict(row) for row in reader.execute(sql, params)]


def read_report(*, property_id=None, date_from=None, date_to=None):
    """Filter after source selection; whole usage intervals are never prorated."""
    cost_where, cost_params = _filters(property_id, date_from, date_to)
    usage_where, usage_params = _filters(property_id, date_from, date_to, usage=True)
    with reporting_reader() as reader:
        costs = _rows(
            reader,
            f"""
            SELECT property_id, property_name, category, service, subcategory, currency,
                   sum(net_minor) AS net_minor, sum(vat_minor) AS vat_minor,
                   sum(gross_minor) AS gross_minor
            FROM reporting_costs WHERE {cost_where}
            GROUP BY property_id, property_name, category, service, subcategory, currency
            ORDER BY property_name, property_id, category, subcategory
            """,
            cost_params,
        )
        consumption = _rows(
            reader,
            SELECTED_USAGE
            + f"""
            SELECT property_id, property_name, utility, unit,
                   CASE WHEN utility = 'waste' THEN subcategory ELSE '' END AS subcategory,
                   sum(quantity_milli) AS quantity_milli
            FROM selected_usage WHERE {usage_where}
            GROUP BY property_id, property_name, utility, unit,
                     CASE WHEN utility = 'waste' THEN subcategory ELSE '' END
            ORDER BY property_name, property_id, utility, subcategory
            """,
            usage_params,
        )
        breakdown = _rows(
            reader,
            SELECTED_USAGE
            + f"""
            SELECT property_id, property_name, utility, unit, source_service, subcategory, basis,
                   source_selection, sum(quantity_milli) AS quantity_milli
            FROM selected_usage WHERE {usage_where}
            GROUP BY property_id, property_name, utility, unit, source_service, subcategory,
                     basis, source_selection
            ORDER BY property_name, property_id, utility, source_service, subcategory, basis
            """,
            usage_params,
        )
        issues = _rows(
            reader,
            f"""
            SELECT * FROM reporting_electricity_selection
            WHERE source_selection = 'needs_review' AND {usage_where}
            ORDER BY property_name, property_id, period_start, invoice_id, usage_id
            """,
            usage_params,
        )
    for row in costs:
        row["service_label"] = Service(row["service"]).label
        row["category_label"] = CATEGORY_LABELS[row["service"]]
        row["subcategory_label"] = (
            subcategory_label(row["service"]) if row["service"] != "waste" else ""
        )
    for row in [*consumption, *breakdown]:
        row["utility_label"] = Usage.Utility(row["utility"]).label
        row["unit_label"] = "m³" if row["unit"] == "m3" else row["unit"]
        row["subcategory_label"] = subcategory_label(
            row.get("source_service", row["utility"]), row["subcategory"]
        )
    for row in breakdown:
        row["source_label"] = Service(row["source_service"]).label
        row["basis_label"] = Usage.Basis(row["basis"]).label
    for row in issues:
        row["source_label"] = Service(row["source_service"]).label
        row["reason"] = SELECTION_REASONS[row["selection_reason"]]
        row["period_start"] = date.fromisoformat(row["period_start"])
        row["period_end"] = date.fromisoformat(row["period_end"])
    return {"costs": costs, "consumption": consumption, "breakdown": breakdown, "issues": issues}
