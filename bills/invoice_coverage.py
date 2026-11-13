"""One vendor bill per scheduled month, with separate allocation completeness."""

import json
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import transaction

from .categories import CATEGORY_LABELS, subcategory_label
from .configuration import (
    add_months,
    contains,
    cost_is_expected,
    cost_pattern,
    cost_pattern_label,
    expected,
    tracks_vendor_receipt,
)
from .models import Invoice, PropertyService, ServiceInvoiceException
from .parsing.compatibility import cost_totals_candidate
from .reporting import reporting_reader

STATUS_LABELS = {
    "missing": "Missing",
    "needs_review": "Needs review",
    "received": "Received",
    "expected": "Expected",
    "not_expected": "Not sent",
    "unassigned": "Needs vendor",
    "unscheduled": "Needs billing schedule",
    "incomplete": "Incomplete allocations",
}


def _date(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError, TypeError:
        return None


def _matches(allocation, service):
    return (
        type(allocation.get("property_id")) is int
        and allocation.get("property_id") == service.property_id
        and allocation.get("category") == service.category
        and (not service.subcategory or allocation.get("subcategory") == service.subcategory)
    )


def _snapshot(start, end):
    """Configuration and receipt states belong to the same read-only snapshot."""
    params = (start.isoformat(), add_months(end, 1).isoformat())
    with reporting_reader() as reader:
        services = [
            SimpleNamespace(**dict(row))
            for row in reader.execute(
                "SELECT s.*, p.name AS property_name FROM bills_propertyservice s "
                "JOIN bills_property p ON p.id=s.property_id ORDER BY p.name, s.id"
            )
        ]
        assignments = [
            SimpleNamespace(**dict(row))
            for row in reader.execute(
                "SELECT a.*, v.name AS vendor_name FROM bills_servicevendorassignment a "
                "JOIN bills_vendor v ON v.id=a.vendor_id ORDER BY a.start_month, a.id"
            )
        ]
        schedules = [
            SimpleNamespace(**dict(row))
            for row in reader.execute(
                "SELECT * FROM bills_vendorbillingschedule ORDER BY start_month, id"
            )
        ]
        patterns = [
            SimpleNamespace(**dict(row))
            for row in reader.execute(
                "SELECT * FROM bills_servicecostschedule ORDER BY start_month, id"
            )
        ]
        exceptions = [
            dict(row)
            for row in reader.execute(
                "SELECT * FROM bills_serviceinvoiceexception WHERE month BETWEEN ? AND ?",
                (start.isoformat(), end.isoformat()),
            )
        ]
        invoices = [
            dict(row)
            for row in reader.execute(
                "SELECT * FROM bills_invoice WHERE kind='invoice' "
                "AND invoice_date >= ? AND invoice_date < ?",
                params,
            )
        ]
        sections = [
            dict(row)
            for row in reader.execute(
                "SELECT b.* FROM bills_billingsection b JOIN bills_invoice i ON i.id=b.invoice_id "
                "WHERE i.kind='invoice' AND i.invoice_date >= ? AND i.invoice_date < ?",
                params,
            )
        ]
        pending = [
            dict(row)
            for row in reader.execute(
                "SELECT e.document_id, e.candidate FROM bills_extractionattempt e "
                "JOIN bills_document d ON d.id=e.document_id AND d.extraction_revision=e.revision "
                "WHERE NOT EXISTS (SELECT 1 FROM bills_invoice i "
                "WHERE i.document_id=e.document_id AND i.status='approved')"
            )
        ]
    for row in [*services, *assignments, *schedules, *patterns]:
        row.start_month, row.end_month = _date(row.start_month), _date(row.end_month)
        if hasattr(row, "first_expected_month"):
            row.first_expected_month = _date(row.first_expected_month)
    for pattern in patterns:
        pattern.pattern_label = cost_pattern_label(pattern)
    for item in exceptions:
        item["month"] = _date(item["month"])
    receipts = defaultdict(dict)
    by_invoice = defaultdict(list)
    for section in sections:
        by_invoice[section["invoice_id"]].append(section)
    pending_documents = {attempt["document_id"] for attempt in pending}
    for invoice in invoices:
        if invoice["vendor_id"] is None:
            continue
        if invoice["status"] != "approved" and invoice["document_id"] in pending_documents:
            continue
        invoice["sections"] = by_invoice[invoice["id"]]
        key = (invoice["vendor_id"], _date(invoice["invoice_date"]).replace(day=1))
        receipts[key][invoice["document_id"]] = invoice
    # Pending headers count even when the property routing is unresolved.
    for attempt in pending:
        values = cost_totals_candidate(json.loads(attempt["candidate"]))
        routing = values.get("routing", {})
        vendor = routing.get("vendor_id")
        invoice_date = _date(values.get("invoice_date"))
        if (
            type(vendor) is not int
            or not invoice_date
            or routing.get("kind", "invoice") != "invoice"
        ):
            continue
        month = invoice_date.replace(day=1)
        if not start <= month <= end:
            continue
        allocations = [
            {**row, "property_id": routing.get("section_properties", {}).get(row.get("section_id"))}
            for row in values.get("sections", [])
        ]
        receipts[(vendor, month)][attempt["document_id"]] = {
            "id": None,
            "document_id": attempt["document_id"],
            "status": "review",
            "sections": allocations,
            "review_revision": None,
        }
    return services, assignments, schedules, patterns, exceptions, receipts


def _allocation(configured, assignment, schedule, month, as_of, bills, exceptions, patterns):
    approved = [bill for bill in bills if bill["status"] == "approved"]
    pending = [bill for bill in bills if bill["status"] != "approved"]
    approved_matches = [
        bill
        for bill in approved
        if any(_matches(allocation, configured) for allocation in bill["sections"])
    ]
    pending_matches = [
        bill
        for bill in pending
        if any(_matches(allocation, configured) for allocation in bill["sections"])
    ]
    scoped = next(
        (item for item in exceptions if item["property_service_id"] == configured.id), None
    )
    global_exception = next(
        (item for item in exceptions if item["property_service_id"] is None), None
    )
    reviewed_exception = (
        scoped
        and len(approved) == 1
        and not pending
        and scoped["reviewed_invoice_id"] == approved[0]["id"]
        and scoped["reviewed_revision"] == approved[0]["review_revision"]
    )
    not_sent_exception = any(
        item and item["reviewed_invoice_id"] is None for item in (scoped, global_exception)
    )
    cost_required = cost_is_expected(configured, assignment, month, patterns)
    pattern = cost_pattern(assignment, month, patterns)
    status = (
        "unassigned"
        if assignment is None
        else "unscheduled"
        if schedule is None
        else "received"
        if approved_matches
        else "needs_review"
        if pending_matches
        else "not_due"
        if not cost_required
        else "not_expected"
        if reviewed_exception or (not bills and not_sent_exception)
        else "incomplete"
        if approved
        else "needs_review"
        if pending
        else "missing"
        if as_of >= add_months(month, 1) + timedelta(days=schedule.grace_days)
        else "expected"
    )
    return {
        "property_service_id": configured.id,
        "property_id": configured.property_id,
        "property_name": configured.property_name,
        "category": configured.category,
        "category_label": CATEGORY_LABELS[configured.category],
        "subcategory": configured.subcategory or "",
        "subcategory_label": subcategory_label(configured.category, configured.subcategory)
        if configured.subcategory
        else "",
        "status": status,
        "status_label": "Cost not due" if status == "not_due" else STATUS_LABELS[status],
        "cost_required": cost_required,
        "cost_schedule_id": pattern.id if pattern else None,
        "service_group": configured.service_group,
        "cost_pattern_label": cost_pattern_label(pattern)
        if pattern
        else ("Required on every vendor bill" if configured.mode == "regular" else "As needed"),
        "exception_id": scoped["id"] if scoped else None,
        "exception_reason": scoped["reason"] if scoped else "",
        "approved_invoice_count": len(approved_matches),
        "review_document_count": len(pending_matches),
        "can_confirm_not_sent": status == "incomplete" and len(approved) == 1 and not pending,
    }


def read_invoice_coverage(
    *,
    property_id=None,
    vendor_id=None,
    service="",
    status="",
    date_from=None,
    date_to=None,
    as_of=None,
):
    as_of = as_of or datetime.now(UTC).date()
    start = (date_from or as_of.replace(month=1, day=1)).replace(day=1)
    end = (date_to or as_of).replace(day=1)
    services, assignments, schedules, patterns, exceptions, receipts = _snapshot(start, end)
    grouped = defaultdict(list)
    configured_services = []
    for configured in services:
        history = [row for row in assignments if row.property_service_id == configured.id]
        if vendor_id is not None and not any(row.vendor_id == vendor_id for row in history):
            continue
        if configured.start_month > end or (configured.end_month and configured.end_month < start):
            continue
        configured_services.append(
            {
                **vars(configured),
                "service": configured.category,
                "category_label": CATEGORY_LABELS[configured.category],
                "subcategory_label": subcategory_label(configured.category, configured.subcategory)
                if configured.subcategory
                else "",
                "frequency_label": PropertyService.Mode(configured.mode).label,
                "activity_label": "Disabled"
                if not configured.is_active
                else "Ended"
                if configured.end_month and configured.end_month < as_of.replace(day=1)
                else "Active",
                "vendor_assignments": [
                    {
                        **vars(row),
                        "cost_schedules": [
                            vars(pattern)
                            for pattern in patterns
                            if pattern.service_vendor_assignment_id == row.id
                        ],
                    }
                    for row in history
                ],
            }
        )
        if not configured.is_active:
            continue
        month = max(start, configured.start_month)
        while month <= end and contains(configured, month):
            applicable = [row for row in history if row.is_active and contains(row, month)]
            if not applicable and vendor_id is None and configured.mode == "regular":
                grouped[(None, month, configured.id)].append((configured, None, None))
            for assignment in applicable:
                if vendor_id is not None and assignment.vendor_id != vendor_id:
                    continue
                schedule = next(
                    (
                        row
                        for row in schedules
                        if row.vendor_id == assignment.vendor_id
                        and row.is_active
                        and contains(row, month)
                    ),
                    None,
                )
                if schedule is None or expected(schedule, month):
                    grouped[(assignment.vendor_id, month, None)].append(
                        (configured, assignment, schedule)
                    )
            month = add_months(month, 1)
    configured_services = [
        item
        for item in configured_services
        if (property_id is None or item["property_id"] == property_id)
        and (not service or item["category"] == service)
    ]
    displayed_service_ids = {item["id"] for item in configured_services}
    rows = []
    for (vendor, month, _), required in grouped.items():
        # Receipt eligibility belongs to the whole vendor bill, including hidden services.
        if vendor is not None and not any(
            tracks_vendor_receipt(configured, assignment, month, patterns)
            for configured, assignment, _ in required
        ):
            continue
        required = [item for item in required if item[0].id in displayed_service_ids]
        if not required:
            continue
        bills = list(receipts[(vendor, month)].values())
        approved = [bill for bill in bills if bill["status"] == "approved"]
        pending = [bill for bill in bills if bill["status"] != "approved"]
        relevant_exceptions = [
            item for item in exceptions if item["vendor_id"] == vendor and item["month"] == month
        ]
        global_exception = next(
            (item for item in relevant_exceptions if item["property_service_id"] is None), None
        )
        allocation_rows = [
            _allocation(*item, month, as_of, bills, relevant_exceptions, patterns)
            for item in required
        ]
        required_rows = [item for item in allocation_rows if item["cost_required"]]
        optional_rows = [item for item in allocation_rows if not item["cost_required"]]
        states = {item["status"] for item in required_rows}
        configured, assignment, schedule = required[0]
        missing_after = (
            add_months(month, 1) + timedelta(days=schedule.grace_days) if schedule else None
        )
        row_status = (
            "unassigned"
            if vendor is None
            else "unscheduled"
            if schedule is None or "unscheduled" in states
            else "incomplete"
            if approved and states - {"received", "not_expected"}
            else "received"
            if approved
            else "needs_review"
            if pending
            else "not_expected"
            if states == {"not_expected"}
            or (global_exception and global_exception["reviewed_invoice_id"] is None)
            else "missing"
            if "missing" in states or (missing_after and as_of >= missing_after)
            else "expected"
        )
        rows.append(
            {
                "vendor_id": vendor,
                "vendor_name": assignment.vendor_name if assignment else "",
                "expected_month": month,
                "expected_time": int(datetime(month.year, month.month, 1, tzinfo=UTC).timestamp()),
                "schedule_id": schedule.id if schedule else None,
                "frequency_months": schedule.frequency_months if schedule else None,
                "grace_days": schedule.grace_days if schedule else None,
                "missing_after": missing_after,
                "property_service_id": configured.id if vendor is None else None,
                "property_id": configured.property_id
                if len({item[0].property_id for item in required}) == 1
                else None,
                "property_name": ", ".join(
                    dict.fromkeys(item[0].property_name for item in required)
                ),
                "category": configured.category,
                "service": configured.category,
                "category_label": ", ".join(
                    dict.fromkeys(item["category_label"] for item in allocation_rows)
                ),
                "subcategory_label": "",
                "required_services": required_rows,
                "optional_services": optional_rows,
                "status": row_status,
                "status_label": STATUS_LABELS[row_status],
                "receipt_status": "received"
                if approved
                else "needs_review"
                if pending
                else row_status,
                "approved_invoice_count": len(approved),
                "review_document_count": len(pending),
                "invoice_id": approved[0]["id"] if approved else None,
                "invoice_revision": approved[0]["review_revision"] if approved else None,
                "invoice_ids": [bill["id"] for bill in approved],
                "review_document_id": pending[0]["document_id"] if pending else None,
                "review_document_ids": [bill["document_id"] for bill in pending],
                "exception_id": global_exception["id"] if global_exception else None,
                "exception_reason": global_exception["reason"]
                if global_exception
                else "; ".join(
                    dict.fromkeys(
                        item["exception_reason"]
                        for item in required_rows
                        if item["status"] == "not_expected"
                    )
                ),
            }
        )
    rows.sort(
        key=lambda row: (row["expected_month"], row["vendor_name"], row["property_name"]),
        reverse=True,
    )
    counts = Counter(row["status"] for row in rows)
    if status == "attention":
        rows = [
            row
            for row in rows
            if row["status"]
            in ("missing", "needs_review", "unassigned", "unscheduled", "incomplete")
        ]
    elif status:
        rows = [row for row in rows if row["status"] == status]
    return {
        "coverage_rows": rows,
        "property_services": configured_services,
        **{f"{name}_count": counts[name] for name in STATUS_LABELS},
        "review_count": counts["needs_review"],
        "as_of": as_of,
    }


@transaction.atomic
def mark_invoice_not_sent(
    property_service_id,
    vendor_id,
    month,
    *,
    reason,
    reviewed_invoice_id=None,
    reviewed_revision=None,
):
    report = read_invoice_coverage(vendor_id=vendor_id, date_from=month, date_to=month)
    row = next((item for item in report["coverage_rows"] if item["vendor_id"] == vendor_id), None)
    scoped = (
        next(
            (
                item
                for item in row["required_services"]
                if item["property_service_id"] == property_service_id
            ),
            None,
        )
        if row
        else None
    )
    current_status = (
        scoped["status"]
        if scoped
        else row["status"]
        if row and property_service_id is None
        else None
    )
    reviewed = scoped and scoped["can_confirm_not_sent"]
    if current_status not in ("missing", "not_expected") and not reviewed:
        raise ValidationError("This invoice is no longer missing. Reload invoice coverage.")
    context = {}
    if reviewed:
        if reviewed_invoice_id != row["invoice_id"] or reviewed_revision != row["invoice_revision"]:
            raise ValidationError(
                "The invoice changed. Review its current revision before confirming."
            )
        context = {
            "reviewed_invoice_id": row["invoice_id"],
            "reviewed_revision": Invoice.objects.get(pk=row["invoice_id"]).review_revision,
        }
    existing = ServiceInvoiceException.objects.filter(
        property_service_id=property_service_id, vendor_id=vendor_id, month=month
    ).first()
    if existing:
        if reviewed:
            existing.reason = reason
            for key, value in context.items():
                setattr(existing, key, value)
            existing.save()
        return existing, False
    return ServiceInvoiceException.objects.create(
        property_service_id=property_service_id,
        vendor_id=vendor_id,
        month=month,
        reason=reason,
        **context,
    ), True
