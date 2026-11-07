"""Read-only invoice proposal summaries, with exact stored money and quantities."""

from datetime import date, timedelta

from .models import Property, Service, Usage, Vendor


def scaled_value(value, scale, precision):
    if type(value) is not int:
        return "Unknown"
    whole, fraction = divmod(abs(value), scale)
    sign = "-" if value < 0 else ""
    return f"{sign}{whole:,}".replace(",", " ") + f",{fraction:0{precision}d}"


def extraction_summary(attempt):
    if attempt is None:
        return None
    candidate = attempt.candidate
    routing = candidate.get("routing", {})
    vendor_id = routing.get("vendor_id")
    vendor = Vendor.objects.filter(pk=vendor_id).first() if type(vendor_id) is int else None
    assignments = routing.get("section_properties", {})
    properties = Property.objects.in_bulk(
        {value for value in assignments.values() if type(value) is int}
    )
    services, utilities, bases = (
        dict(Service.choices),
        dict(Usage.Utility.choices),
        dict(Usage.Basis.choices),
    )
    sections, usages = [], []
    for section in candidate.get("sections", []):
        house = properties.get(assignments.get(section["section_id"]))
        service = services.get(section.get("service"), "Unknown service")
        sections.append(
            {
                "property": house.name if house else "Needs assignment",
                "service": service,
                "gross": scaled_value(section.get("gross_minor"), 100, 2),
            }
        )
        for usage in section.get("usages", []):
            # Source periods are end-exclusive. Show their final billed day.
            try:
                final_day = (date.fromisoformat(usage["end"]) - timedelta(days=1)).isoformat()
            except ValueError, KeyError, TypeError:
                final_day = "Unknown"
            usages.append(
                {
                    "property": house.name if house else "Needs assignment",
                    "service": service,
                    "utility": utilities.get(usage.get("utility"), "Unknown utility"),
                    "quantity": scaled_value(usage.get("quantity_milli"), 1000, 3),
                    "unit": "m³" if usage.get("unit") == "m3" else usage.get("unit", ""),
                    "start": usage.get("start", "Unknown"),
                    "final_day": final_day,
                    "basis": bases.get(usage.get("basis"), "Unknown basis"),
                }
            )
    issues = list(candidate.get("issues", []))
    if attempt.error and attempt.error not in issues:
        issues.append(attempt.error)
    return {
        "vendor": vendor.name if vendor else "Unknown vendor",
        "reference": candidate.get("reference") or "Unknown",
        "invoice_date": candidate.get("invoice_date") or "Unknown",
        "due_date": candidate.get("due_date") or "Unknown",
        "currency": candidate.get("currency") or "",
        "total": scaled_value(candidate.get("invoice_total_minor"), 100, 2),
        "amount_due": scaled_value(candidate.get("amount_due_minor"), 100, 2),
        "rounding": scaled_value(candidate.get("rounding_minor"), 100, 2),
        "sections": sections,
        "usages": usages,
        "issues": issues,
    }
