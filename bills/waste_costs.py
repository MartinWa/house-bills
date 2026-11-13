"""Configured fraction extraction and source-backed corrections of retained invoices."""

from dataclasses import fields
from datetime import date

from .models import Property, PropertyService
from .parsing.common import reconcile
from .parsing.compatibility import cost_totals_candidate
from .parsing.types import Candidate, Evidence, Identifier, Section, Usage
from .parsing.waste_costs import split_waste_costs


def configured_fraction_costs(property_ids):
    tracked = {}
    for house_id, fraction in PropertyService.objects.filter(
        property_id__in=set(property_ids) - {None},
        category="waste",
        is_active=True,
        subcategory__is_waste_fraction=True,
        subcategory__is_active=True,
    ).values_list("property_id", "subcategory_id"):
        tracked.setdefault(house_id, set()).add(fraction)
    return tracked


def split_configured_costs(candidate, pages, section_properties, *, property_id=None):
    tracked = configured_fraction_costs(section_properties.values())
    source_ids, warnings = split_waste_costs(
        candidate,
        pages,
        {
            section.section_id: tracked.get(section_properties.get(section.section_id), ())
            for section in candidate.sections
            if property_id is None or section_properties.get(section.section_id) == property_id
        },
    )
    properties = {
        section.section_id: section_properties.get(
            source_ids.get(section.section_id, section.section_id)
        )
        for section in candidate.sections
    }
    return properties, warnings


def _evidence(value):
    return Evidence(value["page"], value["text"], tuple(value["bbox"])) if value else None


def _identifiers(values):
    return [Identifier(row["kind"], row["value"], _evidence(row["evidence"])) for row in values]


def reviewed_candidate(values):
    """Reconstruct the exact current approval, including manual corrections."""
    values = cost_totals_candidate(values)
    data = {field.name: values[field.name] for field in fields(Candidate) if field.name in values}
    for name in ("invoice_date", "due_date"):
        data[name] = date.fromisoformat(data[name]) if data.get(name) else None
    data["header_evidence"] = {
        key: _evidence(value) for key, value in data["header_evidence"].items()
    }
    data["identifiers"] = _identifiers(data.get("identifiers", []))
    sections = []
    for source in data.get("sections", []):
        row = {field.name: source[field.name] for field in fields(Section) if field.name in source}
        row["identifiers"] = _identifiers(row.get("identifiers", []))
        for name in ("start", "end"):
            row[name] = date.fromisoformat(row[name]) if row.get(name) else None
        for name in ("period_evidence", "tax_evidence", "total_evidence"):
            row[name] = _evidence(row.get(name))
        usages = []
        for usage in row.get("usages", []):
            usages.append(
                Usage(
                    **{
                        **usage,
                        "start": date.fromisoformat(usage["start"]),
                        "end": date.fromisoformat(usage["end"]),
                        "evidence": _evidence(usage["evidence"]),
                    }
                )
            )
        row["usages"] = usages
        sections.append(Section(**row))
    data["sections"] = sections
    return Candidate(**data)


def plan_retained_cost_split(invoice, property_id):
    """Plan against one approval revision without writing or reparsing other values."""
    review = invoice.reviews.get(revision=invoice.review_revision)
    candidate = reviewed_candidate(review.snapshot["values"])
    original = candidate.to_dict()
    extraction = (
        invoice.document.extractions.all().exclude(page_evidence=[]).order_by("-revision").first()
    )
    if extraction is None:
        return None, ["No retained source evidence for itemized waste costs."]
    pages = {}
    for line in extraction.page_evidence:
        pages.setdefault(line["page"], []).append(_evidence(line))
    properties, warnings = split_configured_costs(
        candidate,
        [pages[page] for page in sorted(pages)],
        review.snapshot["properties"],
        property_id=property_id,
    )
    if candidate.to_dict() == original:
        return None, warnings
    reconcile(candidate)
    if review.amount_due_confirmed:
        candidate.issues = [
            issue for issue in candidate.issues if issue != "Amount due differs from invoice total"
        ]
    if candidate.issues:
        return None, [*warnings, *candidate.issues]
    houses = Property.objects.in_bulk(properties.values())
    return {
        "expected_revision": review.revision,
        "candidate": candidate,
        "vendor": invoice.vendor,
        "section_properties": {key: houses[house_id] for key, house_id in properties.items()},
        "extraction": extraction,
        "kind": invoice.kind,
        "amount_due_confirmed": review.amount_due_confirmed,
    }, warnings
