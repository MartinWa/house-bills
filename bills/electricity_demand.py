"""Source-backed demand cost corrections that retain earlier approvals."""

from .models import Property
from .parsing.common import reconcile
from .parsing.electricity_demand import split_electricity_demand
from .parsing.registry import match_layouts
from .parsing.types import Evidence
from .waste_costs import reviewed_candidate


def plan_retained_demand_split(invoice, property_id=None):
    review = invoice.reviews.get(revision=invoice.review_revision)
    extraction = (
        invoice.document.extractions.exclude(page_evidence=[]).order_by("-revision").first()
    )
    if extraction is None:
        return None, ["No retained source evidence for itemized demand costs."]
    unsupported = "Retained source is not a recognized Layout 01 invoice; costs unchanged."
    if extraction.layout_family and extraction.layout_family != "layout_01":
        return None, [unsupported]
    pages = {}
    for line in extraction.page_evidence:
        pages.setdefault(line["page"], []).append(
            Evidence(line["page"], line["text"], tuple(line["bbox"]))
        )
    source_pages = [pages[page] for page in sorted(pages)]
    if not extraction.layout_family and len(match_layouts(source_pages, ["layout_01"])) != 1:
        return None, [unsupported]
    candidate = reviewed_candidate(review.snapshot["values"])
    original = candidate.to_dict()
    properties = review.snapshot["properties"]
    selected = {
        section.section_id
        for section in candidate.sections
        if property_id is None or properties.get(section.section_id) == property_id
    }
    sources, warnings = split_electricity_demand(candidate, source_pages, selected)
    if candidate.to_dict() == original or warnings:
        return None, warnings
    reconcile(candidate)
    if review.amount_due_confirmed:
        candidate.issues = [
            issue for issue in candidate.issues if issue != "Amount due differs from invoice total"
        ]
    if candidate.issues:
        return None, candidate.issues
    properties = {
        section.section_id: properties.get(sources.get(section.section_id, section.section_id))
        for section in candidate.sections
    }
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
