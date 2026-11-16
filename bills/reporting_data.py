"""Reuse approved selections within a query-only reporting snapshot."""

import re
from collections import defaultdict
from datetime import date, timedelta

from .waste_reporting import ReportTooBroad

MAX_FACTS = 100_000


def bounded_facts(rows):
    result = list(rows)
    if len(result) > MAX_FACTS:
        raise ReportTooBroad("100,000 analytical source facts")
    return result


class ReportingSnapshot:
    """Cache source selection before applying vendor, classification or date filters.

    The house constraint can enter the canonical selection's source CTE because
    every overlap and reversal partition is house-local. Date constraints apply
    only to its output; vendor and classification filters apply to cached rows.
    Historical metadata requests retain all dates. The connection stays query-only.
    """

    def __init__(self, reader, filters, *, full_history=True):
        self.reader = reader
        self.properties = filters.get("property_id") or []
        self.utility = filters.get("category")
        self.date_scope = {} if full_history else filters
        self.cache = {}

    def execute(self, *args, **kwargs):
        return self.reader.execute(*args, **kwargs)

    def load(self, key, table, condition="1=1"):
        if key not in self.cache:
            params = list(self.properties)
            source = table
            if self.properties:
                house_filter = f"property_id IN ({','.join('?' for _ in self.properties)})"
                if table == "reporting_electricity_selection":
                    # Read the installed canonical definition, retaining every rule.
                    # An outer predicate cannot reliably reach its materialized CTE.
                    definition = self.execute(
                        "SELECT sql FROM sqlite_schema WHERE type='view' AND name=?", [table]
                    ).fetchone()[0]
                    query = re.sub(r"^CREATE VIEW\s+\w+\s+AS\s+", "", definition, flags=re.I)
                    anchor = "FROM reporting_usage WHERE"
                    if query.count(anchor) != 1:
                        raise RuntimeError("Unsupported canonical electricity selection definition")
                    query = query.replace(anchor, f"{anchor} {house_filter} AND", 1)
                    source = f"({query})"
                else:
                    condition += f" AND {house_filter}"
            usage = table != "reporting_costs"
            for name, clause in (
                ("date_from", "period_end > ?" if usage else "invoice_date >= ?"),
                ("date_to", "period_start <= ?" if usage else "invoice_date <= ?"),
            ):
                if self.date_scope.get(name):
                    condition += f" AND {clause}"
                    params.append(self.date_scope[name].isoformat())
            self.cache[key] = bounded_facts(
                dict(row)
                for row in self.execute(
                    f"SELECT * FROM {source} WHERE {condition} LIMIT {MAX_FACTS + 1}", params
                )
            )
        return self.cache[key]

    def facts(self, kind):
        if kind in ("selection", "issues"):
            rows = (
                self.load("selection", "reporting_electricity_selection")
                if self.utility in (None, "electricity")
                else []
            )
            return (
                rows
                if kind == "selection"
                else [row for row in rows if row["source_selection"] == "needs_review"]
            )
        if kind == "consumption":
            if kind not in self.cache:
                electricity = [
                    row
                    for row in self.facts("selection")
                    if row["source_selection"] in ("network", "supply_fallback")
                ]
                other = self.load("other_usage", "reporting_usage", "utility <> 'electricity'")
                self.cache[kind] = bounded_facts(
                    [
                        *electricity,
                        *(
                            {**row, "source_selection": "billed", "selection_reason": ""}
                            for row in other
                        ),
                    ]
                )
            return self.cache[kind]
        return self.load(
            kind, "reporting_electricity_demand" if kind == "power" else "reporting_costs"
        )

    def selected(self, filters, kind, *, dates=True, billed_costs=False):
        return [
            dict(row)
            for row in self.facts(kind)
            if matches(row, filters, usage=kind != "costs" or billed_costs, dates=dates)
        ]


def matches(row, filters, *, usage=False, dates=True):
    for name in ("property_id", "vendor_id"):
        if filters.get(name) and row[name] not in filters[name]:
            return False
    for parameter, column in (
        ("category", "utility" if usage and "utility" in row else "category"),
        ("subcategory", "subcategory"),
        ("unit", "unit"),
        ("currency", "currency"),
    ):
        if filters.get(parameter) is not None and row.get(column) != filters[parameter]:
            return False
    if dates:
        start = row["period_start"] if usage else row["invoice_date"]
        end = row["period_end"] if usage else row["invoice_date"]
        lower, upper = filters.get("date_from"), filters.get("date_to")
        if lower:
            if end is None or (end <= lower.isoformat() if usage else end < lower.isoformat()):
                return False
        if upper and (start is None or start > upper.isoformat()):
            return False
    return True


def calendar_value(row, name):
    if name in ("invoice_year", "period_start_year"):
        return int(row["invoice_date" if name == "invoice_year" else "period_start"][:4])
    if name in ("invoice_month", "period_start_month"):
        return row["invoice_date" if name == "invoice_month" else "period_start"][:7] + "-01"
    return row.get(name)


def consumption_groups(rows, mode, group_by, max_groups):
    dimensions = (
        group_by if mode == "grouped" else ["property_id", "utility", "unit", "subcategory"]
    )
    if mode == "billed_period":
        dimensions += ["period_start", "period_end", "basis", "source_service", "source_selection"]
    grouped = defaultdict(list)
    for row in rows:
        key = tuple(
            ""
            if name == "subcategory" and mode == "totals" and row["utility"] == "electricity"
            else calendar_value(row, name)
            for name in dimensions
        )
        grouped[key].append(row)
        if len(grouped) > max_groups:
            raise ReportTooBroad("10,000 aggregate groups")
    result = []
    for key, sources in sorted(
        grouped.items(), key=lambda pair: tuple((value is not None, value) for value in pair[0])
    ):
        group = dict(zip(dimensions, key, strict=True))
        for name in ("property", "vendor"):
            if f"{name}_id" in dimensions:
                group[f"{name}_name"] = sources[0][f"{name}_name"]
        group.update(
            quantity_milli=sum(row["quantity_milli"] for row in sources),
            invoice_count=len({row["invoice_id"] for row in sources}),
            allocation_count=len({row["allocation_id"] for row in sources}),
            usage_count=len(sources),
        )
        result.append(group)
    return result


def surviving_intervals(rows):
    """Exact signed corrections cancel observations for physical coverage only."""
    positives, credits = defaultdict(list), defaultdict(list)
    for row in sorted(
        rows, key=lambda row: (row["invoice_date"], row["invoice_id"], row["usage_id"])
    ):
        key = (
            row["property_id"],
            row["vendor_id"],
            row["source_service"],
            row["unit"],
            row["subcategory"],
            row["period_start"],
            row["period_end"],
            abs(row["quantity_milli"]),
        )
        (credits if row["quantity_milli"] < 0 else positives)[key].append(row)
    surviving, unmatched = [], 0
    for key in positives.keys() | credits.keys():
        count = min(len(positives[key]), len(credits[key]))
        surviving.extend(positives[key][count:])
        unmatched += len(credits[key]) - count
    return surviving, unmatched


def interval_coverage(rows, start, end):
    """Coverage of surviving whole intervals; the supplied end is inclusive."""
    values, unmatched = surviving_intervals(rows)
    spans = sorted(
        {
            (date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"]))
            for row in values
        }
    )
    merged = []
    for lower, upper in spans:
        if merged and lower <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(upper, merged[-1][1]))
        else:
            merged.append((lower, upper))
    gaps, covered, cursor = [], 0, start
    exclusive_end = end + timedelta(days=1) if end else None
    if start and exclusive_end:
        for lower, upper in merged:
            lower, upper = max(start, lower), min(exclusive_end, upper)
            if lower >= upper:
                continue
            if lower > cursor:
                gaps.append({"date_from": cursor, "date_to": lower - timedelta(days=1)})
            covered += (upper - max(cursor, lower)).days
            cursor = max(cursor, upper)
        if cursor < exclusive_end:
            gaps.append({"date_from": cursor, "date_to": end})
    bounds = [
        (date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"]))
        for row in rows
    ]
    durations = [(upper - lower).days for lower, upper in bounds]
    basis_counts = {
        basis: sum(row["basis"] == basis for row in rows)
        for basis in sorted({row["basis"] for row in rows})
    }
    return {
        "period_start": min((lower for lower, _ in bounds), default=None),
        "period_end": max((upper for _, upper in bounds), default=None),
        "last_billed_day": max((upper - timedelta(days=1) for _, upper in bounds), default=None),
        "interval_count": len(set(bounds)),
        "usage_count": len(rows),
        "duration_days_min": min(durations, default=None),
        "duration_days_max": max(durations, default=None),
        "basis_counts": basis_counts,
        "estimated_count": basis_counts.get("estimated", 0),
        "reversal_count": sum(row["quantity_milli"] < 0 for row in rows),
        "unmatched_credit_count": unmatched,
        "source_counts": {
            source: sum(row["source_selection"] == source for row in rows)
            for source in sorted({row["source_selection"] for row in rows})
        },
        "covered_days": covered,
        "gaps": gaps,
        "status": "empty"
        if not values
        else "partial"
        if gaps
        else "complete"
        if start and end
        else "unknown",
        "surviving_basis": sorted({row["basis"] for row in values}),
    }
