"""Optional parser configuration hook: host-side dispatch and effect validation."""

import copy
from collections import Counter

from .types import Section
from .validation import ContractError, validate_candidate

OPERATIONS = frozenset({"waste_cost_split", "electricity_demand_split"})
OPERATION_SCOPES = {
    "waste_cost_split": {
        "existing": {"waste"},
        "new_services": {"waste"},
        "new_subcategories": {
            "waste_collection",
            "matavfall",
            "restavfall",
            "tradgardsavfall",
        },
    },
    "electricity_demand_split": {
        "existing": {"electricity_network"},
        "new_services": {"electricity"},
        "new_subcategories": {"electricity_demand"},
    },
}
MAX_WARNINGS = 20
MAX_WARNING_LENGTH = 500
HEADER_FIELDS = (
    "layout_family",
    "layout_version",
    "parser_revision",
    "reference",
    "invoice_date",
    "due_date",
    "currency",
    "net_minor",
    "vat_minor",
    "rounding_minor",
    "invoice_total_minor",
    "amount_due_minor",
    "header_evidence",
    "identifiers",
    "issues",
    "requires_review",
)


def _header(candidate):
    return {name: getattr(candidate, name) for name in HEADER_FIELDS}


def _usage_fields(usage):
    return (
        usage.utility,
        usage.unit,
        usage.quantity_milli,
        usage.start,
        usage.end,
        usage.basis,
        usage.subcategory,
        usage.evidence,
    )


def _usage_counts(candidate):
    return Counter(
        _usage_fields(usage) for section in candidate.sections for usage in section.usages
    )


def _check_usage(candidate, before_counts, before_ids):
    """Existing usages must survive; added usage belongs only to new sections."""
    after_counts = _usage_counts(candidate)
    if any(count > after_counts[key] for key, count in before_counts.items()):
        raise ContractError("hook changed unrelated usage")
    added = after_counts - before_counts
    new_ids = {section.section_id for section in candidate.sections} - before_ids
    for section in candidate.sections:
        for usage in section.usages:
            key = _usage_fields(usage)
            if added[key] > 0:
                if section.section_id not in new_ids:
                    raise ContractError("hook changed unrelated usage")
                added[key] -= 1


def _totals(sections):
    return tuple(
        sum(getattr(section, name) or 0 for section in sections)
        for name in ("net_minor", "vat_minor", "gross_minor")
    )


def _restore(candidate, snapshot):
    candidate.__dict__.clear()
    candidate.__dict__.update(copy.deepcopy(snapshot.__dict__))


def _warnings(value):
    if not isinstance(value, (list, tuple)) or len(value) > MAX_WARNINGS:
        raise ContractError("invalid hook warnings")
    result = []
    for warning in value:
        if (
            not isinstance(warning, str)
            or not warning
            or len(warning) > MAX_WARNING_LENGTH
            or "\x00" in warning
        ):
            raise ContractError("invalid hook warning")
        result.append(warning)
    return result


def _sections(candidate):
    """Return sections by ID, rejecting malformed shapes without attribute access."""
    if not isinstance(candidate.sections, list):
        raise ContractError("invalid hook sections")
    sections = {}
    for section in candidate.sections:
        if not isinstance(section, Section):
            raise ContractError("invalid hook section")
        if not isinstance(section.section_id, str) or section.section_id in sections:
            raise ContractError("invalid hook section id")
        sections[section.section_id] = section
    return sections


def _check_scope(operation, before_sections, after_sections, selected):
    """A hook may only repartition the operation's own service."""
    scope = OPERATION_SCOPES[operation]
    for section_id, before in before_sections.items():
        after = after_sections.get(section_id)
        if after is None:
            raise ContractError("hook removed an existing section")
        if section_id not in selected:
            if after != before:
                raise ContractError("hook changed an unselected section")
            continue
        if before.service not in scope["existing"]:
            if after != before:
                raise ContractError("hook changed an unrelated section")
            continue
        if after.service != before.service or after.identifiers != before.identifiers:
            raise ContractError("hook changed an unrelated section")
    allowed_identifiers = {
        tuple(before_sections[section_id].identifiers)
        for section_id in selected
        if before_sections[section_id].service in scope["existing"]
    }
    for section_id, section in after_sections.items():
        if section_id in before_sections:
            continue
        if section.service not in scope["new_services"]:
            raise ContractError("hook created an unrelated section")
        if section.subcategory not in scope["new_subcategories"]:
            raise ContractError("hook created an unrelated section")
        if tuple(section.identifiers) not in allowed_identifiers:
            raise ContractError("hook created a section outside its source")


def configure(
    candidate,
    pages,
    section_properties,
    *,
    family,
    version,
    operation,
    section_ids,
    configuration,
):
    """Invoke one optional ``configure_sections`` hook and validate its effects.

    The exact recorded family/version is resolved; there is no newest-version or
    family-only fallback. Hooks may only repartition the requested operation's
    costs and usage inside the selected sections. A failure restores the supplied
    candidate and mapping.
    """
    from .registry import installed

    if operation not in OPERATIONS:
        raise ContractError("unsupported parser operation")
    parser = installed(family, version)
    if parser is None:
        raise ContractError("recorded parser is not installed")
    hook = getattr(parser, "configure_sections", None)
    if not callable(hook):
        raise ContractError("recorded parser has no configuration hook")
    validate_candidate(candidate)
    if not isinstance(section_ids, (set, frozenset)):
        raise ContractError("invalid section selection")
    selected = frozenset(section_ids)
    if not all(isinstance(section_id, str) for section_id in selected):
        raise ContractError("invalid section selection")
    before_sections = _sections(candidate)
    if not selected <= set(before_sections):
        raise ContractError("invalid section selection")
    if not isinstance(configuration, dict) or not set(configuration) <= selected:
        raise ContractError("invalid hook configuration")
    if not isinstance(section_properties, dict):
        raise ContractError("invalid section properties")
    original_properties = dict(section_properties)
    if not set(original_properties) <= set(before_sections):
        raise ContractError("invalid section properties")
    if any(
        isinstance(property_id, bool) or not isinstance(property_id, int | None)
        for property_id in original_properties.values()
    ):
        raise ContractError("invalid section properties")
    hook_properties = dict(original_properties)
    snapshot = copy.deepcopy(candidate)
    before_header = copy.deepcopy(_header(candidate))
    before_sections = copy.deepcopy(before_sections)
    before_usage = _usage_counts(candidate)
    before_totals = _totals(candidate.sections)
    try:
        result = hook(
            candidate,
            pages,
            hook_properties,
            operation=operation,
            section_ids=selected,
            configuration=configuration,
        )
    except Exception:
        _restore(candidate, snapshot)
        raise ContractError("parser configuration hook failed") from None
    try:
        if not isinstance(result, tuple) or len(result) != 2:
            raise ContractError("invalid hook result")
        new_properties, warnings = result
        warnings = _warnings(warnings)
        if not isinstance(new_properties, dict):
            raise ContractError("invalid hook properties")
        after_sections = _sections(candidate)
        if set(new_properties) != set(after_sections):
            raise ContractError("hook property keys do not match candidate sections")
        allowed_properties = {original_properties.get(section_id) for section_id in selected}
        for section_id, property_id in new_properties.items():
            if isinstance(property_id, bool) or not isinstance(property_id, int | None):
                raise ContractError("invalid hook property value")
            if section_id in before_sections:
                if property_id != original_properties.get(section_id):
                    raise ContractError("hook changed an existing property assignment")
            elif property_id not in allowed_properties:
                raise ContractError("hook assigned a new section outside its scope")
        validate_candidate(candidate)
        if _header(candidate) != before_header:
            raise ContractError("hook changed header fields")
        _check_usage(candidate, before_usage, set(before_sections))
        if _totals(candidate.sections) != before_totals:
            raise ContractError("hook changed section cost totals")
        _check_scope(operation, before_sections, after_sections, selected)
    except ContractError:
        _restore(candidate, snapshot)
        raise
    except Exception:
        _restore(candidate, snapshot)
        raise ContractError("invalid parser hook result") from None
    return new_properties, warnings
