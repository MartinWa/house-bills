"""Deterministic preflight for the legacy service-owned schedule configuration."""

from collections import defaultdict
from datetime import date


def number(value):
    return value.year * 12 + value.month - 1


def month(value):
    year, offset = divmod(value, 12)
    return date(year, offset + 1, 1)


def audit_legacy_configuration(services, assignments, exceptions=()):
    by_service = {row["id"]: row for row in services}
    for rows, key, label in (
        (
            services,
            lambda r: (r["property_id"], r.get("service", r.get("category")), r["subcategory"]),
            "services",
        ),
        (assignments, lambda r: (r["property_service_id"], r["vendor_id"]), "assignments"),
    ):
        for index, row in enumerate(rows):
            if not row["is_active"]:
                continue
            for other in rows[:index]:
                if (
                    other["is_active"]
                    and key(row) == key(other)
                    and (row["end_month"] is None or other["start_month"] <= row["end_month"])
                    and (other["end_month"] is None or row["start_month"] <= other["end_month"])
                ):
                    raise ValueError(
                        f"Overlapping enabled {label} {other['id']} and {row['id']}. "
                        "Reconcile explicitly before migrating."
                    )
    for assignment in assignments:
        service = by_service[assignment["property_service_id"]]
        if assignment["start_month"] < service["start_month"] or (
            service["end_month"]
            and (not assignment["end_month"] or assignment["end_month"] > service["end_month"])
        ):
            raise ValueError(f"Assignment {assignment['id']} is outside service tracking dates.")
    proposed = plan_vendor_schedules(services, assignments)
    for exception in exceptions:
        service = by_service[exception["property_service_id"]]
        frequency = service["frequency_months"]
        offset = number(exception["month"]) - number(service["start_month"])
        if (
            not frequency
            or offset < 0
            or offset % frequency
            or (service["end_month"] and exception["month"] > service["end_month"])
            or not any(
                assignment["property_service_id"] == service["id"]
                and assignment["vendor_id"] == exception["vendor_id"]
                and assignment["start_month"] <= exception["month"]
                and (
                    assignment["end_month"] is None or exception["month"] <= assignment["end_month"]
                )
                for assignment in assignments
            )
        ):
            raise ValueError(
                f"Exception {exception['id']} has an invalid vendor, invoice month or service "
                "scope. Reconcile explicitly before migrating."
            )
    return proposed


def plan_vendor_schedules(services, assignments):
    by_service = {row["id"]: row for row in services}
    by_vendor = defaultdict(list)
    for assignment in assignments:
        service = by_service[assignment["property_service_id"]]
        start = max(service["start_month"], assignment["start_month"])
        ends = [value for value in (service["end_month"], assignment["end_month"]) if value]
        end = min(ends) if ends else None
        if end and end < start:
            raise ValueError(
                f"Assignment {assignment['id']} lies outside its service tracking dates."
            )
        frequency = service["frequency_months"]
        by_vendor[assignment["vendor_id"]].append(
            {
                "start": number(start),
                "end": number(end) + 1 if end else None,
                "frequency": frequency,
                "phase": number(service["start_month"]) % frequency if frequency else 0,
                "grace": service["grace_days"],
                "active": service["is_active"] and assignment["is_active"],
                "service": service["id"],
                "assignment": assignment["id"],
            }
        )
    result = []
    for vendor, rows in sorted(by_vendor.items()):
        regular = [row for row in rows if row["frequency"] and row["active"]]
        boundaries = sorted(
            {value for row in regular for value in (row["start"], row["end"]) if value is not None}
        )
        periods = []
        for index, start in enumerate(boundaries):
            applicable = [
                row
                for row in regular
                if row["start"] <= start and (row["end"] is None or row["end"] > start)
            ]
            if not applicable:
                continue
            signatures = {(row["frequency"], row["phase"], row["grace"]) for row in applicable}
            if len(signatures) != 1:
                ids = sorted({row["service"] for row in applicable})
                raise ValueError(
                    f"Vendor {vendor} has conflicting frequency, phase or grace policy in "
                    f"services {ids} from {month(start)}. Reconcile explicitly before migrating."
                )
            signature = next(iter(signatures))
            end = boundaries[index + 1] if index + 1 < len(boundaries) else None
            if periods and periods[-1]["end"] == start and periods[-1]["signature"] == signature:
                periods[-1]["end"] = end
            else:
                periods.append({"start": start, "end": end, "signature": signature, "active": True})
        # Keep disabled regular configurations; occasional services never override regular billing.
        for row in rows:
            if row["frequency"] and not row["active"]:
                periods.append(
                    {
                        "start": row["start"],
                        "end": row["end"],
                        "signature": (row["frequency"], row["phase"], row["grace"]),
                        "active": False,
                    }
                )
        if not any(row["frequency"] for row in rows):
            periods.append(
                {
                    "start": min(row["start"] for row in rows),
                    "end": None
                    if any(row["end"] is None for row in rows)
                    else max(row["end"] for row in rows),
                    "signature": (0, 0, 0),
                    "active": any(row["active"] for row in rows),
                }
            )
        for period in periods:
            frequency, phase, grace = period["signature"]
            anchor = period["start"] + ((phase - period["start"]) % frequency if frequency else 0)
            result.append(
                {
                    "vendor_id": vendor,
                    "start_month": month(period["start"]),
                    "end_month": month(period["end"] - 1) if period["end"] else None,
                    "first_expected_month": month(anchor),
                    "frequency_months": frequency,
                    "grace_days": grace,
                    "is_active": period["active"],
                }
            )
    return result
