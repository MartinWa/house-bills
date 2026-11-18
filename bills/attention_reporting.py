"""Labelled coverage and content decisions built in one reporting snapshot."""

import json
from collections import Counter
from datetime import UTC, datetime
from types import SimpleNamespace

from .api_reporting import MAX_GROUPS, metadata, report_labels
from .content_review import CONTENT_REVIEW_STATES
from .invoice_coverage import STATUS_LABELS, _date, build_invoice_coverage
from .parsing.compatibility import cost_totals_candidate
from .presentation import proposal_has_changes
from .reporting import reporting_reader
from .waste_reporting import ReportTooBroad

ATTENTION_STATUSES = ("missing", "needs_review", "incomplete", "unassigned", "unscheduled")
REASONS = {
    "manual_entry": "Complete the manual invoice entry.",
    "parse_failed": "Parsing failed; review the document.",
    "proposal_changes": "The latest proposal differs from the approved invoice.",
    "reparse_confirmation": "Confirm completion of the re-parsed invoice.",
    "vendor_unresolved": "Assign the invoice vendor.",
    "property_unresolved": "Assign the invoice properties.",
    "validation_required": "Validate the proposed invoice values.",
    "awaiting_approval": "Review and approve the invoice.",
}
SORT_FIELDS = {
    "coverage": (
        "expected_month",
        "vendor_id",
        "vendor_name",
        "status",
        "missing_after",
        "days_overdue",
        "expectation_key",
    ),
    "review": (
        "attention_since",
        "age_days",
        "invoice_date",
        "vendor_name",
        "reason_key",
        "document_id",
    ),
    "attention": ("attention_date", "kind", "reason_key", "attention_key"),
}
DEFAULT_SORT = {
    "coverage": ("-expected_month", "vendor_name", "expectation_key"),
    "review": ("-attention_since", "document_id"),
    "attention": ("-attention_date", "attention_key"),
}
UNIQUE_KEY = {"coverage": "expectation_key", "review": "document_id", "attention": "attention_key"}


def sorted_rows(rows, sort):
    # Stable multi-key sorts, always placing unknown values after known ones.
    for key in reversed(sort):
        name = key.removeprefix("-")
        known = [row for row in rows if row.get(name) is not None]
        unknown = [row for row in rows if row.get(name) is None]
        rows = sorted(known, key=lambda row: row[name], reverse=key.startswith("-")) + unknown
    return rows


def bounded(rows, description):
    if len(rows) > MAX_GROUPS:
        raise ReportTooBroad(f"10,000 {description}")
    return rows


def singular(values):
    return values[0] if len(values) == 1 else None


def interval(periods):
    valid = {(_date(start), _date(end)) for start, end in periods}
    if len(valid) == 1:
        start, end = next(iter(valid))
        if start and end and start < end:
            return {"period_start": start, "period_end": end}
    return {"period_start": None, "period_end": None}


def selected_properties(services):
    properties = {row["property_id"]: row["property_name"] for row in services}
    return [{"property_id": key, "property_name": properties[key]} for key in sorted(properties)]


def coverage_rows(reader, filters, as_of):
    catalogue = report_labels(reader)
    if filters.get("subcategory") is not None and filters["subcategory"] not in catalogue:
        from .api.errors import invalid_parameter

        invalid_parameter("subcategory")
    facts = build_invoice_coverage(
        reader=reader,
        property_ids=filters.get("property_id"),
        vendor_ids=filters.get("vendor_id"),
        service=filters.get("category") or "",
        subcategory=filters.get("subcategory"),
        date_from=filters["date_from"],
        date_to=filters["date_to"],
        as_of=as_of,
        max_groups=MAX_GROUPS,
        catalogue=catalogue,
    )
    rows = []
    for fact in facts["coverage_rows"]:
        state = fact["status"]
        selection = filters.get("status", "all")
        if selection == "attention" and state not in ATTENTION_STATUSES:
            continue
        if selection not in ("all", "attention") and state != selection:
            continue
        services = []
        for service in [*fact["required_services"], *fact["optional_services"]]:
            services.append(
                {
                    **{
                        key: service[key]
                        for key in (
                            "property_service_id",
                            "property_id",
                            "property_name",
                            "category",
                            "subcategory",
                            "status",
                            "status_label",
                            "cost_required",
                        )
                    },
                    "classification": "required" if service["cost_required"] else "optional",
                    "category_name": service["category_label"],
                    "subcategory_name": catalogue.get(
                        service["subcategory"], service["subcategory_label"]
                    ),
                    "confirmation_reason": service["confirmation_reason"],
                }
            )
        vendor = fact["vendor_id"]
        key = f"vendor:{vendor}" if vendor is not None else f"service:{fact['property_service_id']}"
        approved_ids = sorted(fact["invoice_ids"])
        approved_documents = sorted(fact["approved_document_ids"])
        pending_documents = sorted(fact["review_document_ids"])
        documents = sorted(set(approved_documents + pending_documents))
        rows.append(
            {
                "expectation_key": f"{key}:month:{fact['expected_month']:%Y-%m}",
                **{
                    key: fact[key]
                    for key in (
                        "vendor_id",
                        "expected_month",
                        "status",
                        "status_label",
                        "receipt_status",
                        "missing_after",
                    )
                },
                "vendor_name": fact["vendor_name"] or None,
                "expected_month_time": fact["expected_time"],
                "days_overdue": (as_of - fact["missing_after"]).days
                if state == "missing"
                else None,
                "properties": selected_properties(services),
                "services": services,
                "approved_invoice_ids": approved_ids,
                "approved_document_ids": approved_documents,
                "pending_document_ids": pending_documents,
                "document_ids": documents,
                "invoice_id": singular(approved_ids),
                "document_id": singular(documents),
                **interval(fact["billed_periods"]),
            }
        )
    return bounded(rows, "coverage groups")


def coverage_stats(rows):
    counts = Counter(row["status"] for row in rows)
    missing = [row["expected_month"] for row in rows if row["status"] == "missing"]
    return {
        "expectation_count": len(rows),
        "expected_invoice_count": sum(
            counts[key] for key in ("expected", "missing", "needs_review", "received", "incomplete")
        ),
        "received_invoice_count": sum(row["receipt_status"] == "received" for row in rows),
        "missing_invoice_count": counts["missing"],
        "review_invoice_count": counts["needs_review"],
        "counts_by_status": {key: counts[key] for key in STATUS_LABELS},
        "oldest_missing_month": min(missing, default=None),
    }


def utc_datetime(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def integer_value(value):
    return value if type(value) is int and -(2**63) <= value < 2**63 else None


def review_rows(reader, filters, as_of):
    if filters.get("property_id") is not None and not filters["property_id"]:
        return []
    # Keep private candidates internal. Only the explicit projection below is serialized.
    lookups = {
        name: {
            row["id"]: SimpleNamespace(**dict(row))
            for row in reader.execute(f"SELECT id, name, slug FROM bills_{table}")
        }
        for name, table in (("properties", "property"), ("vendors", "vendor"))
    }
    lookups["subcategories"] = report_labels(reader)
    rows = []
    # One document retains at most one invoice; report that row directly.
    records = reader.execute(
        "SELECT d.id AS document_id, d.source, d.state, d.created_at, "
        "e.id AS extraction_id, e.candidate, e.state AS extraction_state, "
        "e.created_at AS extraction_created_at, "
        "i.id AS invoice_id, i.status AS invoice_status, r.snapshot, "
        "(SELECT json_group_array(DISTINCT b.property_id) FROM bills_billingsection b "
        "WHERE b.invoice_id=i.id AND i.status='approved') AS approved_properties "
        "FROM bills_document d LEFT JOIN bills_extractionattempt e "
        "ON e.document_id=d.id AND e.revision=d.extraction_revision "
        "LEFT JOIN bills_invoice i ON i.document_id=d.id "
        "LEFT JOIN bills_reviewrevision r ON r.invoice_id=i.id AND r.revision=i.review_revision "
        f"WHERE d.state IN ({','.join('?' for _ in CONTENT_REVIEW_STATES)}) ORDER BY d.id",
        CONTENT_REVIEW_STATES,
    )
    for record in records:
        values = (
            cost_totals_candidate(json.loads(record["candidate"]))
            if record["extraction_id"]
            else {}
        )
        routing = values.get("routing", {})
        vendor = lookups["vendors"].get(integer_value(routing.get("vendor_id")))
        proposed_ids = {
            pk
            for pk in routing.get("section_properties", {}).values()
            if type(pk) is int and pk in lookups["properties"]
        }
        approved_ids = set(json.loads(record["approved_properties"]))
        known_ids = proposed_ids | approved_ids
        if filters.get("property_id") is not None and not known_ids.intersection(
            filters["property_id"]
        ):
            continue
        approved = record["invoice_status"] == "approved"
        since = utc_datetime(
            record["extraction_created_at"]
            if approved and record["extraction_id"]
            else record["created_at"]
        )
        attention_date = since.date()
        if filters.get("date_from") and attention_date < filters["date_from"]:
            continue
        if filters.get("date_to") and attention_date > filters["date_to"]:
            continue
        sections = values.get("sections", [])
        resolved_count = sum(
            type(routing.get("section_properties", {}).get(section.get("section_id"))) is int
            and routing["section_properties"][section.get("section_id")] in lookups["properties"]
            for section in sections
        )
        routing_status = (
            "resolved"
            if sections and resolved_count == len(sections)
            else ("partial" if resolved_count else "unresolved")
        )
        details = []
        if record["source"] == "manual" and not approved:
            details.append("manual_entry")
        if record["state"] == "failed" or record["extraction_state"] == "failed":
            details.append("parse_failed")
        if (
            approved
            and record["snapshot"]
            and record["extraction_id"]
            and proposal_has_changes(
                json.loads(record["snapshot"]),
                values,
                lookups=lookups,
            )
        ):
            details.append("proposal_changes")
        if approved:
            details.append("reparse_confirmation")
        if vendor is None:
            details.append("vendor_unresolved")
        if routing_status != "resolved":
            details.append("property_unresolved")
        if values.get("issues"):
            details.append("validation_required")
        details.append("awaiting_approval")
        reason = details[0]
        if filters.get("reason_key") is not None and reason != filters["reason_key"]:
            continue
        net, vat = (integer_value(values.get(field)) for field in ("net_minor", "vat_minor"))
        rows.append(
            {
                "document_id": record["document_id"],
                "invoice_id": record["invoice_id"],
                "vendor_id": vendor.id if vendor else None,
                "vendor_name": vendor.name if vendor else None,
                "invoice_date": _date(values.get("invoice_date")),
                "invoice_kind": routing.get("kind", "invoice")
                if record["extraction_id"]
                and routing.get("kind", "invoice") in ("invoice", "credit")
                else None,
                "currency": values.get("currency") if values.get("currency") == "SEK" else None,
                "net_minor": net,
                "vat_minor": vat,
                "gross_minor": integer_value(net + vat)
                if net is not None and vat is not None
                else None,
                "value_source": "proposal" if record["extraction_id"] else "empty",
                "routing_status": routing_status,
                "properties": [
                    {
                        "property_id": pk,
                        "property_name": lookups["properties"][pk].name,
                        "association": "both"
                        if pk in proposed_ids and pk in approved_ids
                        else "proposed"
                        if pk in proposed_ids
                        else "approved",
                    }
                    for pk in sorted(known_ids)
                    if filters.get("property_id") is None or pk in filters["property_id"]
                ],
                "reason_key": reason,
                "reason": REASONS[reason],
                "reason_details": [{"reason_key": key, "reason": REASONS[key]} for key in details],
                "attention_since": since,
                "age_days": max(0, (as_of - attention_date).days),
                **interval([(section.get("start"), section.get("end")) for section in sections]),
            }
        )
        bounded(rows, "review documents")
    return rows


def review_stats(rows):
    counts = Counter(row["reason_key"] for row in rows)
    return {
        "review_document_count": len(rows),
        "counts_by_reason": {key: counts[key] for key in REASONS},
        "oldest_attention_since": min((row["attention_since"] for row in rows), default=None),
    }


def coverage_path(row):
    from urllib.parse import urlencode

    params = {"date_from": row["expected_month"], "date_to": row["expected_month"], "status": ""}
    if row["vendor_id"] is not None:
        params["vendor_id"] = row["vendor_id"]
    if len(row["properties"]) == 1:
        params["property_id"] = row["properties"][0]["property_id"]
    return "/invoices/coverage/?" + urlencode(params)


def attention_rows(coverage, review, *, related_coverage=None):
    by_document = {}
    for row in coverage if related_coverage is None else related_coverage:
        for document_id in row["document_ids"]:
            by_document.setdefault(document_id, []).append(row)
    visible_documents = {row["document_id"] for row in review}
    rows = []
    for document in review:
        related = by_document.get(document["document_id"], [])
        months = sorted({row["expected_month"] for row in related})
        rows.append(
            {
                "attention_key": f"document:{document['document_id']}",
                "kind": "document",
                "attention_date": document["attention_since"].date(),
                **{
                    key: document[key]
                    for key in (
                        "properties",
                        "reason_key",
                        "reason",
                        "document_id",
                        "invoice_id",
                        "period_start",
                        "period_end",
                    )
                },
                "services": list(
                    {
                        service["property_service_id"]: service
                        for row in related
                        for service in row["services"]
                    }.values()
                ),
                "related_expectation_keys": sorted(row["expectation_key"] for row in related),
                "expected_month": singular(months),
                "expected_months": months,
                "document_ids": sorted(
                    {
                        document["document_id"],
                        *(pk for row in related for pk in row["document_ids"]),
                    }
                ),
                "review_path": f"/documents/{document['document_id']}/review/",
                "coverage_path": coverage_path(related[0]) if len(related) == 1 else None,
            }
        )
    for row in coverage:
        if visible_documents.intersection(row["document_ids"]):
            continue
        rows.append(
            {
                "attention_key": f"coverage:{row['expectation_key']}",
                "kind": "coverage",
                "attention_date": row["expected_month"],
                **{
                    key: row[key]
                    for key in (
                        "properties",
                        "services",
                        "expected_month",
                        "document_id",
                        "invoice_id",
                        "document_ids",
                        "period_start",
                        "period_end",
                    )
                },
                "reason_key": row["status"],
                "reason": row["status_label"],
                "related_expectation_keys": [row["expectation_key"]],
                "expected_months": [row["expected_month"]],
                "review_path": f"/documents/{row['document_id']}/review/"
                if row["document_id"]
                else None,
                "coverage_path": coverage_path(row),
            }
        )
        bounded(rows, "attention items")
    return bounded(rows, "attention items")


def read_attention_report(*, kind, filters, mode="rows", sort=None, limit=50, offset=0):
    with reporting_reader() as reader:
        base = {**metadata(), "filters": filters, "as_of": filters["as_of"], "mode": mode}
        as_of = filters["as_of"]
        if kind == "coverage":
            rows = coverage_rows(reader, filters, as_of)
            stats = coverage_stats(rows)
        elif kind == "review":
            rows = review_rows(reader, filters, as_of)
            stats = review_stats(rows)
        else:
            coverage_filters = {
                "property_id": filters.get("property_id"),
                "status": "all",
                "date_from": filters["coverage_date_from"],
                "date_to": filters["coverage_date_to"],
            }
            review_filters = {
                "property_id": filters.get("property_id"),
                "date_from": filters["review_date_from"],
                "date_to": filters["review_date_to"],
            }
            all_coverage = coverage_rows(reader, coverage_filters, as_of)
            coverage = [row for row in all_coverage if row["status"] in ATTENTION_STATUSES]
            review = review_rows(reader, review_filters, as_of)
            rows = attention_rows(coverage, review, related_coverage=all_coverage)
            source = coverage_stats(coverage)
            stats = {
                "attention_count": len(rows),
                "review_document_count": len(review),
                "coverage_attention_count": len(coverage),
                "missing_invoice_count": source["missing_invoice_count"],
                "oldest_missing_month": source["oldest_missing_month"],
            }
        bounds = (
            ("coverage_date_from", "coverage_date_to", "review_date_from", "review_date_to")
            if kind == "attention"
            else ("date_from", "date_to")
        )
        base.update(effective_date_bounds={key: filters.get(key) for key in bounds}, **stats)
        if mode == "rows":
            rows = sorted_rows(rows, sort or DEFAULT_SORT[kind])
            base.update(count=len(rows), items=rows[offset : offset + limit])
        return base
