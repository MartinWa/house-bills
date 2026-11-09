"""Exact display values and semantic differences between approval and extraction."""

import re
from datetime import date, timedelta

from .categories import CATEGORY_LABELS, subcategory_label
from .models import Property, Service, Usage, Vendor
from .parsing.compatibility import cost_totals_candidate


def scaled_value(value, scale, precision):
    if type(value) is not int:
        return "Unknown"
    whole, fraction = divmod(abs(value), scale)
    sign = "-" if value < 0 else ""
    return f"{sign}{whole:,}".replace(",", " ") + f",{fraction:0{precision}d}"


def rounded_value(value, scale):
    """Round exact aggregated units to whole numbers, with halves away from zero."""
    if type(value) is not int:
        return "Unknown"
    whole = (abs(value) + scale // 2) // scale
    sign = "-" if value < 0 and whole else ""
    return f"{sign}{whole:,}".replace(",", " ")


def extraction_summary(attempt):
    if attempt is None:
        return None
    return candidate_summary(attempt.candidate, error=attempt.error)


def warning_details(warnings, sections, assignments):
    """Name the visible allocation and source pages without changing parser evidence."""
    properties = Property.objects.in_bulk(
        {value for value in assignments.values() if type(value) is int}
    )
    contexts = {}
    services = dict(Service.choices)
    for index, section in enumerate(sections):
        key = section.get("section_id")
        if not isinstance(key, str) or not key:
            continue
        house = properties.get(assignments.get(key))
        property_name = house.name if house else "Needs assignment"
        service = services.get(section.get("service"), "Unknown service")
        evidence = [
            section.get("period_evidence"),
            section.get("tax_evidence"),
            section.get("total_evidence"),
        ]
        evidence.extend(
            row.get("evidence") for group in ("charges", "usages") for row in section.get(group, [])
        )
        if not any(evidence):
            identifiers = section.get("identifiers", [])
            specific = [
                identifier for identifier in identifiers if identifier.get("kind") == "contract"
            ]
            evidence = [identifier.get("evidence") for identifier in specific or identifiers]
        pages = sorted(
            {
                item["page"]
                for item in evidence
                if isinstance(item, dict) and type(item.get("page")) is int and item["page"] > 0
            }
        )
        contexts[key] = {
            "text": f"Allocation {index + 1} · {property_name} · {service}",
            "anchor": f"sections-{index}",
            "pages": pages,
        }
    pattern = (
        re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(key) for key in sorted(contexts, key=len, reverse=True))
            + r")(?!\w)"
        )
        if contexts
        else None
    )
    details = []
    for warning in warnings:
        parts, position = [], 0
        for match in pattern.finditer(warning) if pattern else []:
            if match.start() > position:
                parts.append({"text": warning[position : match.start()]})
            parts.append(contexts[match[0]])
            position = match.end()
        if position < len(warning) or not parts:
            parts.append({"text": warning[position:]})
        details.append({"parts": parts})
    return details


def candidate_summary(candidate, *, error=""):
    candidate = cost_totals_candidate(candidate)
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
                "category": CATEGORY_LABELS.get(section.get("service"), "Unknown service"),
                "subcategory": (
                    subcategory_label(section.get("service"))
                    if section.get("service") != "waste"
                    else ""
                ),
                "gross": scaled_value(section.get("gross_minor"), 100, 2),
                "start": section.get("start") or "Unknown",
                "final_day": final_day(section.get("end")),
            }
        )
        for usage in section.get("usages", []):
            usages.append(
                {
                    "property": house.name if house else "Needs assignment",
                    "service": service,
                    "subcategory": subcategory_label(
                        section.get("service"), usage.get("subcategory", "")
                    ),
                    "utility": utilities.get(usage.get("utility"), "Unknown utility"),
                    "quantity": scaled_value(usage.get("quantity_milli"), 1000, 3),
                    "unit": "m³" if usage.get("unit") == "m3" else usage.get("unit", ""),
                    "start": usage.get("start", "Unknown"),
                    "final_day": final_day(usage.get("end")),
                    "basis": bases.get(usage.get("basis"), "Unknown basis"),
                }
            )
    issues = list(candidate.get("issues", []))
    if error and error not in issues:
        issues.append(error)
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
        "warning_details": warning_details(issues, candidate.get("sections", []), assignments),
    }


def final_day(value):
    if value is None:
        return "Unknown"
    try:
        parsed = date.fromisoformat(value) if isinstance(value, str) else value
        return (parsed - timedelta(days=1)).isoformat()
    except ValueError, TypeError:
        return "Unknown"


def _comparison_values(values, *, vendor_id, properties, kind):
    """Compare values that affect the bill, excluding extraction text and geometry."""
    values = cost_totals_candidate(values)
    result = {}
    vendor = Vendor.objects.filter(pk=vendor_id).first() if vendor_id else None
    houses = Property.objects.in_bulk({pk for pk in properties.values() if type(pk) is int})

    def put(key, label, value, *, display=None):
        comparison = "Unknown" if value is None or value == "" else str(value)
        result[key] = (label, comparison, comparison if display is None else display)

    put(
        "vendor",
        "Vendor",
        vendor_id,
        display=f"{vendor.name} ({vendor.slug})" if vendor else "Unknown",
    )
    put("kind", "Invoice type", "Credit invoice" if kind == "credit" else "Invoice")
    for field, label in (
        ("reference", "Invoice reference"),
        ("invoice_date", "Invoice date"),
        ("due_date", "Due date"),
        ("currency", "Currency"),
    ):
        put(field, label, values.get(field))
    for field, label in (
        ("net_minor", "Net"),
        ("vat_minor", "VAT"),
        ("rounding_minor", "Rounding"),
        ("invoice_total_minor", "Invoice total"),
        ("amount_due_minor", "Amount due"),
    ):
        put(field, label, scaled_value(values.get(field), 100, 2))

    def identifiers(rows, prefix, label):
        for index, row in enumerate(rows):
            put(
                f"{prefix}.identifier.{index}",
                f"{label} identifier {index + 1}",
                (f"{row.get('kind', 'Unknown')}: {row.get('value', 'Unknown')}"),
            )

    identifiers(values.get("identifiers", []), "header", "Invoice")
    for index, section in enumerate(values.get("sections", [])):
        key = section["section_id"]
        prefix, label = f"section.{key}", f"Allocation {index + 1}"
        house = houses.get(properties.get(key))
        put(
            f"{prefix}.property",
            f"{label} property",
            properties.get(key),
            display=f"{house.name} ({house.slug})" if house else "Unknown",
        )
        put(
            f"{prefix}.service",
            f"{label} service",
            dict(Service.choices).get(section.get("service")),
        )
        for field, title in (("net_minor", "net"), ("vat_minor", "VAT"), ("gross_minor", "cost")):
            put(f"{prefix}.{field}", f"{label} {title}", scaled_value(section.get(field), 100, 2))
        put(
            f"{prefix}.period",
            f"{label} billing period",
            f"{section.get('start') or 'Unknown'} to {final_day(section.get('end'))}",
        )
        identifiers(section.get("identifiers", []), prefix, label)
        for row_index, usage in enumerate(section.get("usages", [])):
            row_key, row_label = (
                f"{prefix}.usage.{row_index}",
                f"{label} consumption {row_index + 1}",
            )
            put(
                f"{row_key}.utility",
                f"{row_label} utility",
                dict(Usage.Utility.choices).get(usage.get("utility")),
            )
            put(
                f"{row_key}.quantity",
                f"{row_label} quantity",
                (scaled_value(usage.get("quantity_milli"), 1000, 3) + " " + usage.get("unit", "")),
            )
            put(
                f"{row_key}.subcategory",
                f"{row_label} subcategory",
                subcategory_label(section.get("service"), usage.get("subcategory", "")),
            )
            put(
                f"{row_key}.period",
                f"{row_label} period",
                (f"{usage.get('start', 'Unknown')} to {final_day(usage.get('end'))}"),
            )
            put(
                f"{row_key}.basis",
                f"{row_label} basis",
                dict(Usage.Basis.choices).get(usage.get("basis")),
            )
    return result


def proposal_differences(invoice, attempt, *, pending_only=False):
    if invoice is None or not invoice.review_revision or attempt is None:
        return []
    review = invoice.reviews.get(revision=invoice.review_revision)
    if pending_only and attempt.created_at <= review.created_at:
        return []
    snapshot = review.snapshot
    approved = _comparison_values(
        snapshot["values"],
        vendor_id=snapshot["vendor_id"],
        properties=snapshot["properties"],
        kind=snapshot["kind"],
    )
    routing = attempt.candidate.get("routing", {})
    proposed = _comparison_values(
        attempt.candidate,
        vendor_id=routing.get("vendor_id"),
        properties=routing.get("section_properties", {}),
        kind=routing.get("kind", "invoice"),
    )
    differences = []
    for key in dict.fromkeys([*approved, *proposed]):
        before, after = approved.get(key), proposed.get(key)
        if before is None or after is None or before[1] != after[1]:
            differences.append(
                {
                    "field": (after or before)[0],
                    "approved": before[2] if before else "Not present",
                    "proposed": after[2] if after else "Not present",
                }
            )
    return differences
