"""Configured fraction extraction and source-backed corrections of retained invoices."""

from dataclasses import dataclass, fields
from datetime import date

from django.conf import settings

from .models import Property, PropertyService
from .parsing.api import API_VERSION, configure
from .parsing.common import reconcile
from .parsing.compatibility import cost_totals_candidate
from .parsing.registry import (
    installed,
    layouts,
    match_layouts,
    parser_fingerprint,
    parser_source,
)
from .parsing.types import Candidate, Evidence, Identifier, Section, Usage
from .parsing.validation import ContractError

COST_OPERATION = "waste_cost_split"
DEMAND_OPERATION = "electricity_demand_split"


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


def configured_section_configuration(section_properties):
    """Plain host-prepared settings for selected section IDs; no Django objects."""
    tracked = configured_fraction_costs(section_properties.values())
    return {
        section_id: {"enabled_waste_fractions": frozenset(tracked.get(property_id, ()))}
        for section_id, property_id in section_properties.items()
    }


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
            usage = {
                field.name: usage[field.name] for field in fields(Usage) if field.name in usage
            }
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


@dataclass(frozen=True)
class RetainedSource:
    extraction: object
    pages: list
    parser: object
    provenance: dict
    limitation: str = ""


def _pages(extraction):
    pages = {}
    for line in extraction.page_evidence:
        pages.setdefault(line["page"], []).append(
            Evidence(line["page"], line["text"], tuple(line["bbox"]))
        )
    return [pages[page] for page in sorted(pages)]


def _approval_identity(review):
    values = review.snapshot.get("values", {})
    return (
        values.get("layout_family") or "",
        values.get("layout_version") or "",
        values.get("parser_revision") or "",
    )


def _recorded_fingerprint(extraction):
    routing = extraction.candidate.get("routing")
    if not isinstance(routing, dict):
        return None
    selected = routing.get("selected_parser")
    if not isinstance(selected, dict):
        return None
    fingerprint = selected.get("fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _select_extraction(invoice, review):
    """Use the approval-linked extraction; never substitute a later proposal."""
    if review.extraction is not None:
        return review.extraction, ""
    identity = _approval_identity(review)
    candidates = list(invoice.document.extractions.exclude(page_evidence=[]).order_by("-revision"))
    if identity[0]:
        matches = [
            attempt
            for attempt in candidates
            if (attempt.layout_family, attempt.layout_version, attempt.parser_revision) == identity
        ]
        if len(matches) == 1:
            return matches[0], "approval_identity"
        return None, ""
    legacy = [attempt for attempt in candidates if not attempt.layout_family]
    if legacy:
        return legacy[0], "reconstructed"
    return None, ""


def retained_source(invoice, review, operation):
    """Resolve the parser that produced the approval and its retained pages."""
    extraction, limitation = _select_extraction(invoice, review)
    if extraction is None:
        return None, [
            "The approval has no unambiguous retained extraction; re-parse and review "
            "before correcting costs."
        ]
    pages = _pages(extraction)
    if not pages:
        return None, ["No retained source evidence for itemized costs."]
    if extraction.layout_family:
        parser = installed(extraction.layout_family, extraction.layout_version)
        if parser is None:
            return None, ["The recorded parser version is not installed; costs unchanged."]
        if parser.REVISION != extraction.parser_revision:
            return None, [
                "The recorded parser revision differs from the installed code; re-parse and "
                "review before correcting costs."
            ]
        if parser_source(parser) == "plugin":
            recorded = _recorded_fingerprint(extraction)
            if not recorded or recorded != parser_fingerprint(parser):
                return None, [
                    "The recorded plugin source is unavailable; re-parse and review before "
                    "correcting costs."
                ]
    else:
        matches = match_layouts(pages, list(layouts()))
        if (
            len(matches) != 1
            or parser_source(matches[0]) != "builtin"
            or not hasattr(matches[0], "configure_sections")
        ):
            return None, [
                "Retained source has no unambiguous installed parser; re-parse and review "
                "before correcting costs."
            ]
        parser = matches[0]
        limitation = "reconstructed"
    if not hasattr(parser, "configure_sections"):
        return None, [
            f"The recorded parser {parser.FAMILY}/{parser.VERSION} has no {operation} "
            "operation; costs unchanged."
        ]
    provenance = {
        "extraction_id": extraction.pk,
        "parser": {
            "family": parser.FAMILY,
            "version": parser.VERSION,
            "revision": parser.REVISION,
            "source": parser_source(parser),
            "fingerprint": parser_fingerprint(parser),
        },
        "api_version": API_VERSION,
        "app_version": settings.APP_VERSION,
        "operation": operation,
        "section_ids": [],
        "limitation": limitation or None,
    }
    return RetainedSource(extraction, pages, parser, provenance, limitation), []


def _configured(candidate, source, properties, selected, operation, configuration, *, warning):
    provenance = {
        **source.provenance,
        "section_ids": sorted(selected),
    }
    try:
        properties, warnings = configure(
            candidate,
            source.pages,
            properties,
            family=source.parser.FAMILY,
            version=source.parser.VERSION,
            operation=operation,
            section_ids=selected,
            configuration=configuration,
        )
    except ContractError:
        return None, [warning]
    return (properties, warnings, provenance), []


def plan_retained_cost_split(invoice, property_id):
    """Plan against one approval revision without writing or reparsing other values."""
    review = invoice.reviews.get(revision=invoice.review_revision)
    source, warnings = retained_source(invoice, review, COST_OPERATION)
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
    configuration = configured_section_configuration(
        {section_id: properties.get(section_id) for section_id in selected}
    )
    result, warnings = _configured(
        candidate,
        source,
        properties,
        selected,
        COST_OPERATION,
        configuration,
        warning="Retained parser cannot apply the waste cost split; costs unchanged.",
    )
    if result is None:
        return None, warnings
    properties, warnings, provenance = result
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
        "extraction": source.extraction,
        "kind": invoice.kind,
        "amount_due_confirmed": review.amount_due_confirmed,
        "provenance": provenance,
    }, warnings
