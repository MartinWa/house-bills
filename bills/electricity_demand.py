"""Source-backed demand cost corrections that retain earlier approvals."""

from .models import Property
from .parsing.api import configure
from .parsing.common import reconcile
from .parsing.validation import ContractError
from .waste_costs import DEMAND_OPERATION, retained_source, reviewed_candidate


def plan_retained_demand_split(invoice, property_id=None):
    review = invoice.reviews.get(revision=invoice.review_revision)
    source, warnings = retained_source(invoice, review, DEMAND_OPERATION)
    if source is None:
        return None, warnings
    candidate = reviewed_candidate(review.snapshot["values"])
    original = candidate.to_dict()
    properties = dict(review.snapshot["properties"])
    selected = frozenset(
        section.section_id
        for section in candidate.sections
        if property_id is None or properties.get(section.section_id) == property_id
    )
    provenance = {**source.provenance, "section_ids": sorted(selected)}
    try:
        properties, warnings = configure(
            candidate,
            source.pages,
            properties,
            family=source.parser.FAMILY,
            version=source.parser.VERSION,
            operation=DEMAND_OPERATION,
            section_ids=selected,
            configuration={},
        )
    except ContractError:
        return None, ["Retained parser cannot apply the demand cost split; costs unchanged."]
    if candidate.to_dict() == original or warnings:
        return None, warnings
    reconcile(candidate)
    if review.amount_due_confirmed:
        candidate.issues = [
            issue for issue in candidate.issues if issue != "Amount due differs from invoice total"
        ]
    if candidate.issues:
        return None, candidate.issues
    houses = Property.objects.in_bulk(properties.values())
    return {
        "expected_revision": review.revision,
        "candidate": candidate,
        "vendor": invoice.vendor,
        "section_properties": {key: houses[house_id] for key, house_id in properties.items()},
        "extraction": source.extraction,
        "kind": invoice.kind,
        "amount_due_confirmed": review.amount_due_confirmed,
        "provenance": provenance,
    }, warnings
