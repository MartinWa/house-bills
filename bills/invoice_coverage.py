"""Expected invoice months; costs remain governed by explicit approval."""

from collections import Counter
from datetime import UTC, date, datetime

from django.core.exceptions import ValidationError
from django.db import transaction

from .categories import CATEGORY_LABELS, subcategory_label
from .models import PropertyService, Service, ServiceInvoiceException
from .reporting import reporting_reader

STATUS_LABELS = {
    "missing": "Missing",
    "needs_review": "Needs review",
    "received": "Received",
    "expected": "Expected",
    "not_expected": "Not sent",
    "unassigned": "Needs vendor",
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
    """Read one snapshot; unapproved receipts never enter cost or consumption reports."""
    as_of = as_of or datetime.now(UTC).date()
    start = (date_from or as_of.replace(month=1, day=1)).replace(day=1)
    end = (date_to or as_of).replace(day=1)
    clauses, parameters = (
        ["expected_month >= ?", "expected_month <= ?"],
        [
            start.isoformat(),
            end.isoformat(),
        ],
    )
    service_clauses, service_parameters = (
        [
            "s.start_month <= ?",
            "(s.end_month IS NULL OR s.end_month >= ?)",
        ],
        [end.isoformat(), start.isoformat()],
    )
    for column, value in (
        ("property_id", property_id),
        ("vendor_id", vendor_id),
        ("service", service),
    ):
        if value is not None and value != "":
            clauses.append(f"{column} = ?")
            parameters.append(value)
            if column != "vendor_id":
                service_clauses.append(f"s.{column} = ?")
                service_parameters.append(value)
    if vendor_id is not None:
        service_clauses.append(
            "EXISTS (SELECT 1 FROM bills_servicevendorassignment AS a "
            "WHERE a.property_service_id = s.id AND a.vendor_id = ? "
            "AND a.start_month <= ? AND (a.end_month IS NULL OR a.end_month >= ?))"
        )
        service_parameters.extend([vendor_id, end.isoformat(), start.isoformat()])
    with reporting_reader() as reader:
        rows = [
            dict(row)
            for row in reader.execute(
                f"SELECT * FROM reporting_invoice_coverage WHERE {' AND '.join(clauses)} "
                "ORDER BY expected_month DESC, property_name, vendor_name, service, subcategory",
                parameters,
            )
        ]
        services = [
            dict(row)
            for row in reader.execute(
                "SELECT s.*, p.name AS property_name "
                "FROM bills_propertyservice AS s "
                "JOIN bills_property AS p ON p.id = s.property_id "
                f"WHERE {' AND '.join(service_clauses)} "
                "ORDER BY property_name, service, subcategory, start_month",
                service_parameters,
            )
        ]
        assignments = []
        if services:
            assignments = [
                dict(row)
                for row in reader.execute(
                    "SELECT a.*, v.name AS vendor_name FROM bills_servicevendorassignment AS a "
                    "JOIN bills_vendor AS v ON v.id = a.vendor_id "
                    f"WHERE a.property_service_id IN ({','.join('?' for _ in services)}) "
                    "ORDER BY a.start_month, vendor_name, a.id",
                    [item["id"] for item in services],
                )
            ]
    for row in rows:
        row["expected_month"] = date.fromisoformat(row["expected_month"])
        row["missing_after"] = date.fromisoformat(row["missing_after"])
        row["status"] = (
            "unassigned"
            if row["vendor_id"] is None
            else "received"
            if row["approved_invoice_count"]
            else "needs_review"
            if row["review_document_count"]
            else "not_expected"
            if row["exception_id"] is not None
            else "missing"
            if as_of >= row["missing_after"]
            else "expected"
        )
        row["status_label"] = STATUS_LABELS[row["status"]]
        row["category_label"] = CATEGORY_LABELS[row["service"]]
        row["subcategory_label"] = (
            subcategory_label(row["service"], row["subcategory"]) if row["subcategory"] else ""
        )
    by_service = {}
    for configured in services:
        configured["category_label"] = CATEGORY_LABELS[configured["service"]]
        configured["subcategory_label"] = (
            subcategory_label(configured["service"], configured["subcategory"])
            if configured["service"] != Service.WASTE or configured["subcategory"]
            else ""
        )
        configured["frequency_label"] = PropertyService.Frequency(
            configured["frequency_months"]
        ).label
        configured["start_month"] = date.fromisoformat(configured["start_month"])
        configured["end_month"] = (
            date.fromisoformat(configured["end_month"]) if configured["end_month"] else None
        )
        configured["activity_label"] = (
            "Disabled"
            if not configured["is_active"]
            else "Ended"
            if configured["end_month"] and configured["end_month"] < as_of.replace(day=1)
            else "Scheduled"
            if configured["start_month"] > as_of.replace(day=1)
            else "Active"
        )
        configured["vendor_assignments"] = []
        by_service[configured["id"]] = configured
    for assignment in assignments:
        assignment["start_month"] = date.fromisoformat(assignment["start_month"])
        assignment["end_month"] = (
            date.fromisoformat(assignment["end_month"]) if assignment["end_month"] else None
        )
        by_service[assignment["property_service_id"]]["vendor_assignments"].append(assignment)
    counts = Counter(row["status"] for row in rows)
    if status == "attention":
        rows = [row for row in rows if row["status"] in ("missing", "needs_review", "unassigned")]
    elif status:
        rows = [row for row in rows if row["status"] == status]
    return {
        "coverage_rows": rows,
        "property_services": services,
        "missing_count": counts["missing"],
        "review_count": counts["needs_review"],
        "received_count": counts["received"],
        "expected_count": counts["expected"],
        "not_expected_count": counts["not_expected"],
        "unassigned_count": counts["unassigned"],
        "as_of": as_of,
    }


@transaction.atomic
def mark_invoice_not_sent(property_service_id, vendor_id, month, *, reason):
    """Recheck receipt state under the write lock; never overwrite an earlier reason."""
    report = read_invoice_coverage(vendor_id=vendor_id, date_from=month, date_to=month)
    row = next(
        (
            item
            for item in report["coverage_rows"]
            if item["property_service_id"] == property_service_id
        ),
        None,
    )
    if row is None or row["status"] not in ("missing", "not_expected"):
        raise ValidationError("This invoice is no longer missing. Reload invoice coverage.")
    return ServiceInvoiceException.objects.get_or_create(
        property_service_id=property_service_id,
        vendor_id=vendor_id,
        month=month,
        defaults={"reason": reason},
    )
