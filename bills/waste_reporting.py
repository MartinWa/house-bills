"""Waste costs and invoice-reported weights are independent facts."""

from django.core.exceptions import ValidationError

from .models import Usage

MAX_GROUPS = 10_000
MONEY_FIELDS = ("net_minor", "vat_minor", "gross_minor")


def attach_waste_weights(reader, costs):
    """Attach reported quantities to source cost rows without allocating money."""
    sections = {row["allocation_id"]: row for row in costs if row["category"] == "waste"}
    if not sections:
        return costs
    for row in sections.values():
        row["weights"] = []
    weights = reader.execute(
        "SELECT * FROM reporting_waste_usage WHERE allocation_id IN ("
        + ",".join("?" for _ in sections)
        + ") ORDER BY allocation_id, usage_id",
        list(sections),
    )
    for source in weights:
        weight = dict(source)
        section = sections[weight["allocation_id"]]
        weight["currency"] = section["currency"]
        weight["fraction_name"] = weight["fraction_name"] or "General / unspecified waste"
        weight["basis_label"] = Usage.Basis(weight["basis"]).label
        section["weights"].append(weight)
    return costs


def read_waste_details(reader, cost_where, cost_params, *, limit=50):
    count = reader.execute(
        f"SELECT count(*) FROM reporting_waste_costs WHERE {cost_where}",
        cost_params,
    ).fetchone()[0]
    costs = [
        dict(row)
        for row in reader.execute(
            f"SELECT * FROM reporting_waste_costs WHERE {cost_where} "
            "ORDER BY invoice_date DESC, invoice_id DESC, allocation_id LIMIT ?",
            [*cost_params, limit],
        )
    ]
    for row in costs:
        row["name"] = row["fraction_name"] or "General / unspecified waste"
        if row["subcategory"] == "waste_collection":
            row["name"] = "General / combined waste"
    return {"waste_details": attach_waste_weights(reader, costs), "waste_detail_count": count}


class ReportTooBroad(ValidationError):
    def __init__(self, limit):
        self.limit = limit
        super().__init__(f"Report exceeds {limit}.")


def _rows(reader, sql, params):
    rows = [dict(row) for row in reader.execute(sql, params)]
    if len(rows) > MAX_GROUPS:
        raise ReportTooBroad("10,000 waste aggregate groups")
    return rows


def _configured_fractions(reader, filters):
    if filters.get("category") not in (None, "waste"):
        return []
    clauses = ["s.category='waste'", "s.is_active=1", "sc.is_waste_fraction=1", "sc.is_active=1"]
    params = []
    properties = filters.get("property_id") or []
    if properties:
        clauses.append(f"s.property_id IN ({','.join('?' for _ in properties)})")
        params.extend(properties)
    if filters.get("subcategory") is not None:
        clauses.append("s.subcategory=?")
        params.append(filters["subcategory"])
    if filters.get("date_from"):
        clauses.append("(s.end_month IS NULL OR s.end_month>=?)")
        params.append(filters["date_from"].replace(day=1).isoformat())
    if filters.get("date_to"):
        clauses.append("s.start_month<=?")
        params.append(filters["date_to"].replace(day=1).isoformat())
    vendors = filters.get("vendor_id") or []
    if vendors:
        assignment_clauses = [
            "a.property_service_id=s.id",
            "a.is_active=1",
            f"a.vendor_id IN ({','.join('?' for _ in vendors)})",
        ]
        params.extend(vendors)
        if filters.get("date_from"):
            assignment_clauses.append("(a.end_month IS NULL OR a.end_month>=?)")
            params.append(filters["date_from"].replace(day=1).isoformat())
        if filters.get("date_to"):
            assignment_clauses.append("a.start_month<=?")
            params.append(filters["date_to"].replace(day=1).isoformat())
        clauses.append(
            "EXISTS (SELECT 1 FROM bills_servicevendorassignment a WHERE "
            + " AND ".join(assignment_clauses)
            + ")"
        )
    return _rows(
        reader,
        "SELECT DISTINCT s.property_id, p.name AS property_name, s.subcategory, "
        "sc.name AS fraction_name, 1 AS is_waste_fraction, 'SEK' AS currency "
        "FROM bills_propertyservice s JOIN bills_property p ON p.id=s.property_id "
        "JOIN bills_subcategory sc ON sc.key=s.subcategory WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY p.name, sc.name LIMIT {MAX_GROUPS + 1}",
        params,
    )


def read_waste_report(reader, cost_where, cost_params, *, filters=None):
    """Costs and attached weights share invoice-date filters and one snapshot."""
    totals = ", ".join(
        f"CASE WHEN count({field})=count(*) THEN sum({field}) END AS {field}"
        for field in MONEY_FIELDS
    )
    costs = _rows(
        reader,
        f"""SELECT property_id, property_name, subcategory, fraction_name,
                   is_waste_fraction, currency, {totals}
            FROM reporting_waste_costs WHERE {cost_where}
            GROUP BY property_id, property_name, subcategory, fraction_name,
                     is_waste_fraction, currency
            ORDER BY property_name, subcategory LIMIT {MAX_GROUPS + 1}""",
        cost_params,
    )
    weights = _rows(
        reader,
        f"""WITH selected_costs AS (
                SELECT allocation_id, currency FROM reporting_waste_costs WHERE {cost_where}
            )
            SELECT u.property_id, u.property_name, u.subcategory, u.fraction_name,
                   u.is_waste_fraction, c.currency, u.unit, u.basis,
                   sum(u.quantity_milli) AS quantity_milli
            FROM reporting_waste_usage u JOIN selected_costs c ON c.allocation_id=u.allocation_id
            GROUP BY u.property_id, u.property_name, u.subcategory, u.fraction_name,
                     u.is_waste_fraction, c.currency, u.unit, u.basis
            ORDER BY u.property_name, u.subcategory, u.unit, u.basis LIMIT {MAX_GROUPS + 1}""",
        cost_params,
    )
    entries = {}

    def entry(row):
        key = (row["property_id"], row["subcategory"], row["currency"])
        if key not in entries:
            fraction = bool(row["is_waste_fraction"])
            name = row["fraction_name"] or "General / unspecified waste"
            if row["subcategory"] == "waste_collection":
                name = "General / combined waste"
            entries[key] = {
                "property_id": row["property_id"],
                "property_name": row["property_name"],
                "subcategory": row["subcategory"],
                "fraction": row["subcategory"] if fraction else None,
                "fraction_name": name,
                "name": name,
                "currency": row["currency"],
                "is_waste_fraction": fraction,
                "cost": None,
                "weights": [],
                "reason": "combined_cost"
                if row["subcategory"] == "waste_collection"
                else "unspecified_cost"
                if not row["subcategory"]
                else "reported_cost",
            }
        return entries[key]

    total = {"currency": "SEK", **dict.fromkeys(MONEY_FIELDS, 0)}
    property_totals = {}
    for row in costs:
        target = entry(row)
        target["cost"] = {
            "currency": row["currency"],
            **{field: row[field] for field in MONEY_FIELDS},
        }
        prop = property_totals.setdefault(
            (row["property_id"], row["currency"]),
            {
                "property_id": row["property_id"],
                "property_name": row["property_name"],
                "currency": row["currency"],
                **dict.fromkeys(MONEY_FIELDS, 0),
            },
        )
        for field in MONEY_FIELDS:
            value = row[field]
            total[field] = (
                total[field] + value if total[field] is not None and value is not None else None
            )
            prop[field] = (
                prop[field] + value if prop[field] is not None and value is not None else None
            )
    for row in weights:
        entry(row)["weights"].append(
            {
                "unit": row["unit"],
                "quantity_milli": row["quantity_milli"],
                "basis": row["basis"],
                "basis_label": Usage.Basis(row["basis"]).label,
            }
        )
    for row in _configured_fractions(reader, filters or {}):
        entry(row)
    if (
        len(entries) + len(property_totals) + sum(len(row["weights"]) for row in entries.values())
        > MAX_GROUPS
    ):
        raise ReportTooBroad("10,000 waste aggregate groups")
    ordered = sorted(
        entries.values(), key=lambda row: (row["property_name"], row["name"], row["subcategory"])
    )
    return {
        "waste_total": total,
        "waste_totals_by_property": list(property_totals.values()),
        "waste_fractions": [row for row in ordered if row["is_waste_fraction"]],
        "waste_general": [
            row
            for row in ordered
            if not row["is_waste_fraction"] and row["subcategory"] != "tank_emptying"
        ],
        "waste_other_services": [row for row in ordered if row["subcategory"] == "tank_emptying"],
    }
