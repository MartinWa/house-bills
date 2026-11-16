"""Deterministic, schema-validated synthetic reporting responses and Infinity queries."""

import argparse
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path


def generate():
    from bills.api import schemas as s
    from bills.attention_reporting import REASONS, attention_rows, coverage_stats, review_stats

    from .generate_analytics_examples import generate as analytics_examples

    at = date(2026, 3, 1)
    metadata = {"contract_version": 1, "generated_at": datetime(2026, 3, 1, tzinfo=UTC)}
    bounds = {"date_from": date(2026, 2, 1), "date_to": date(2026, 2, 1)}
    properties = [{"property_id": pk, "property_name": f"Synthetic house {pk}"} for pk in (1, 2)]
    services = [
        {
            **house,
            "property_service_id": house["property_id"],
            "category": "water",
            "category_name": "Water",
            "subcategory": "",
            "subcategory_name": "",
            "classification": "required",
            "cost_required": True,
            "status": "missing",
            "status_label": "Missing",
            "confirmation_reason": None,
        }
        for house in properties
    ]
    coverage = s.CoverageRow(
        expectation_key="vendor:1:month:2026-02",
        vendor_id=1,
        vendor_name="Synthetic utility",
        expected_month=date(2026, 2, 1),
        expected_month_time=1769904000,
        status="missing",
        status_label="Missing",
        receipt_status="missing",
        missing_after=at,
        days_overdue=0,
        properties=properties,
        services=services,
        approved_invoice_ids=[],
        approved_document_ids=[],
        pending_document_ids=[],
        document_ids=[],
        invoice_id=None,
        document_id=None,
        period_start=None,
        period_end=None,
    ).model_dump()
    review = s.ReviewQueueRow(
        document_id=3,
        invoice_id=None,
        vendor_id=None,
        vendor_name=None,
        invoice_date=None,
        invoice_kind=None,
        net_minor=None,
        vat_minor=None,
        gross_minor=None,
        currency=None,
        value_source="empty",
        routing_status="unresolved",
        properties=[],
        reason_key="manual_entry",
        reason=REASONS["manual_entry"],
        reason_details=[{"reason_key": "manual_entry", "reason": REASONS["manual_entry"]}],
        attention_since=datetime(2026, 2, 28, tzinfo=UTC),
        age_days=1,
        period_start=None,
        period_end=None,
    ).model_dump()
    invoice = s.InvoiceCostRow(
        invoice_id=4,
        document_id=4,
        vendor_id=1,
        vendor_name="Synthetic utility",
        invoice_date=date(2026, 2, 20),
        invoice_time=1771545600,
        invoice_kind="invoice",
        review_revision=1,
        source="manual",
        properties=properties,
        currency="SEK",
        net_minor=200,
        vat_minor=50,
        gross_minor=250,
        allocation_count=2,
    ).model_dump()
    coverage_filters = {
        "property_id": None,
        "vendor_id": None,
        "category": None,
        "subcategory": None,
        **bounds,
        "as_of": at,
        "status": "all",
    }
    review_filters = {
        "property_id": None,
        "reason_key": None,
        "date_from": None,
        "date_to": None,
        "as_of": at,
    }
    attention_filters = {
        "property_id": None,
        "as_of": at,
        "coverage_date_from": bounds["date_from"],
        "coverage_date_to": bounds["date_to"],
        "review_date_from": None,
        "review_date_to": None,
    }
    cost_filters = {
        "property_id": None,
        "vendor_id": None,
        "category": None,
        "subcategory": None,
        "currency": "SEK",
        "date_from": date(2026, 2, 20),
        "date_to": date(2026, 2, 20),
    }
    examples = []

    def add(name, endpoint, schema, filters, rows, stats=None, *, limit=None):
        query = {key: value for key, value in filters.items() if value is not None}
        if endpoint != "invoice-costs":
            query["mode"] = "rows"
        from urllib.parse import urlencode

        query = {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in query.items()
        }
        page = {
            "count": len(rows),
            "items": rows if limit is None else rows[:limit],
            "next": None,
            "previous": None,
            "has_more": limit is not None and len(rows) > limit,
        }
        if page["has_more"]:
            query.update(limit=limit, offset=limit)
            page["next"] = f"/api/v1/reports/{endpoint}?{urlencode(query)}"
        effective = {key: value for key, value in filters.items() if key.endswith(("_from", "_to"))}
        payload = {
            **metadata,
            "filters": filters,
            "effective_date_bounds": effective,
            **(stats or {}),
            **page,
        }
        if endpoint != "invoice-costs":
            payload.update(mode="rows", as_of=at)
        examples.append(
            {
                "name": name,
                "endpoint": endpoint,
                "response": schema.model_validate(payload).model_dump(mode="json"),
            }
        )

    for rows, name in (([], "empty"), ([coverage], "missing-shared")):
        add(
            name,
            "missing-invoices",
            s.MissingInvoicesPage,
            coverage_filters,
            rows,
            coverage_stats(rows),
        )
    for rows, name in (([], "empty"), ([review], "unknown-manual")):
        add(name, "review-queue", s.ReviewQueuePage, review_filters, rows, review_stats(rows))
    for coverage_rows, review_rows, name in (
        ([], [], "empty"),
        ([coverage], [review], "missing-and-unknown"),
    ):
        rows = attention_rows(coverage_rows, review_rows)
        stats = {
            "attention_count": len(rows),
            "review_document_count": len(review_rows),
            "coverage_attention_count": len(coverage_rows),
            "missing_invoice_count": len(coverage_rows),
            "oldest_missing_month": bounds["date_from"] if coverage_rows else None,
        }
        add(
            name,
            "attention",
            s.AttentionPage,
            attention_filters,
            rows,
            stats,
            limit=1 if rows else None,
        )
    for rows, name in (
        ([], "empty"),
        ([invoice], "shared-complete"),
        ([{**invoice, "net_minor": None, "gross_minor": None}], "unknown-amount"),
        (
            [
                invoice,
                {
                    **invoice,
                    "invoice_id": 5,
                    "document_id": 5,
                    "invoice_kind": "credit",
                    "net_minor": -200,
                    "vat_minor": -50,
                    "gross_minor": -250,
                },
            ],
            "paginated-credit",
        ),
    ):
        add(
            name,
            "invoice-costs",
            s.InvoiceCostsPage,
            cost_filters,
            rows,
            limit=1 if len(rows) > 1 else None,
        )
    targets = []
    for endpoint, columns in (
        ("missing-invoices", ("expectation_key", "expected_month", "status_label")),
        ("review-queue", ("document_id", "reason", "vendor_name")),
        ("attention", ("attention_key", "reason", "expected_month", "review_path")),
        (
            "invoice-costs",
            ("invoice_id", "vendor_name", "invoice_date", "gross_minor", "currency"),
        ),
    ):
        targets.append(
            {
                "endpoint": endpoint,
                "target": {
                    "refId": "A",
                    "type": "json",
                    "source": "url",
                    "parser": "backend",
                    "format": "table",
                    "url": f"http://app:8000/api/v1/reports/{endpoint}?property_id=1&limit=200",
                    "root_selector": (
                        "(contract_version = 1 ? ($p := $; "
                        '$message := "Showing " & $string($count(items)) & " of " & $string(count) '
                        '& " matching" & (has_more ? "; more pages available" : ""); '
                        '$count(items) = 0 ? [{"page_message": (count = 0 ? "No matching records" '
                        ": $message)}] : $map(items, function($r) { $merge([$r, "
                        '{"page_message": $message, "cost_display": '
                        '($type($r.gross_minor) = "number" ? $string($r.gross_minor / 100) '
                        '& " " & $r.currency : "Unknown")}]) })) '
                        ": $unsupported_reporting_contract())"
                    ),
                    "url_options": {"method": "GET"},
                    "columns": [
                        {
                            "selector": key,
                            "text": key,
                            "type": "number"
                            if key in ("document_id", "invoice_id", "gross_minor")
                            else "string",
                        }
                        for key in (
                            *columns,
                            "page_message",
                            *(("cost_display",) if endpoint == "invoice-costs" else ()),
                        )
                    ],
                },
            }
        )
    return (
        json.dumps(
            {
                "contract_version": 1,
                "analytics_version": 1,
                "examples": examples,
                "infinity_queries": targets,
                "analytics_examples": analytics_examples(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "house_bills.settings.test")
    import django

    django.setup()
    target = Path("docs/api/reporting-examples.json")
    content = generate()
    if args.check:
        if not target.exists() or target.read_text(encoding="utf-8") != content:
            raise SystemExit(
                "Reporting examples differ; run python -m tools.generate_reporting_examples."
            )
        print("Reporting examples match deterministic regeneration.")
    else:
        target.write_text(content, encoding="utf-8")
        print(f"Generated {target}.")


if __name__ == "__main__":
    main()
