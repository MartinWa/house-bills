"""Bounded structural checks for candidates returned by installed parsers.

A parser may leave uncertain values null and report issues; it may not return a
malformed object that would fail later during property mapping, overlap checks or
serialization. These checks are deliberately structural, never an acceptance step.
"""

import json
import math
from dataclasses import fields
from datetime import date, datetime

from .types import Candidate, Evidence, Identifier, Section, Usage

MAX_SECTIONS = 20
MAX_USAGES = 60
MAX_IDENTIFIERS = 60
MAX_ISSUES = 50
MAX_ISSUE_LENGTH = 500
MAX_HEADER_EVIDENCE = 20
MAX_TEXT_LENGTH = 2000
MAX_IDENTIFIER_LENGTH = 200
MAX_SECTION_ID_LENGTH = 80
MAX_CONTEXT_LENGTH = 40
MAX_MONEY = 10**15
MAX_QUANTITY = 10**15
SERVICES = {"electricity", "electricity_network", "electricity_supply", "water", "waste", "other"}
UNITS = {"kWh", "kW", "m3", "kg"}
HEADER_LIMITS = {
    "layout_family": 40,
    "layout_version": 40,
    "parser_revision": 80,
    "reference": MAX_TEXT_LENGTH,
    "currency": 40,
}
HEADER_STRINGS = tuple(HEADER_LIMITS)
HEADER_MONEY = (
    "net_minor",
    "vat_minor",
    "rounding_minor",
    "invoice_total_minor",
    "amount_due_minor",
)


class ContractError(ValueError):
    """A parser returned an object outside the documented candidate contract."""


def _text(value, limit, label, errors):
    if not isinstance(value, str) or not value or len(value) > limit:
        errors.append(f"invalid {label}")
        return False
    if any(character in value for character in ("\x00", "\r", "\n")):
        errors.append(f"invalid {label}")
        return False
    return True


def _money(value, label, errors):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > MAX_MONEY:
        errors.append(f"invalid {label}")


def _date(value, label, errors):
    if value is None:
        return
    if isinstance(value, datetime) or not isinstance(value, date):
        errors.append(f"invalid {label}")


def _evidence(value, label, errors):
    if value is None:
        return
    if not isinstance(value, Evidence):
        errors.append(f"invalid {label}")
        return
    if isinstance(value.page, bool) or not isinstance(value.page, int) or not 1 <= value.page:
        errors.append(f"invalid {label} page")
    if not isinstance(value.text, str) or len(value.text) > MAX_TEXT_LENGTH:
        errors.append(f"invalid {label} text")
    bbox = value.bbox
    if (
        not isinstance(bbox, (tuple, list))
        or len(bbox) != 4
        or any(
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            for number in bbox
        )
    ):
        errors.append(f"invalid {label} bbox")


def _identifiers(values, budget, label, errors):
    if not isinstance(values, list):
        errors.append(f"invalid {label} list")
        return
    for item in values:
        budget[0] += 1
        if not isinstance(item, Identifier):
            errors.append(f"invalid {label} entry")
            continue
        _text(item.kind, MAX_CONTEXT_LENGTH, f"{label} kind", errors)
        _text(item.value, MAX_IDENTIFIER_LENGTH, f"{label} value", errors)
        _evidence(item.evidence, f"{label} evidence", errors)


def _usages(values, budget, section_id, errors):
    if not isinstance(values, list):
        errors.append(f"invalid usages in {section_id}")
        return
    for usage in values:
        budget[0] += 1
        if not isinstance(usage, Usage):
            errors.append(f"invalid usage in {section_id}")
            continue
        if not isinstance(usage.utility, str) or usage.utility not in SERVICES:
            errors.append(f"invalid usage utility in {section_id}")
        if not isinstance(usage.unit, str) or usage.unit not in UNITS:
            errors.append(f"invalid usage unit in {section_id}")
        if (
            isinstance(usage.quantity_milli, bool)
            or not isinstance(usage.quantity_milli, int)
            or abs(usage.quantity_milli) > MAX_QUANTITY
        ):
            errors.append(f"invalid usage quantity in {section_id}")
        if usage.start is None or usage.end is None:
            errors.append(f"missing usage period in {section_id}")
        _date(usage.start, f"usage start in {section_id}", errors)
        _date(usage.end, f"usage end in {section_id}", errors)
        _text(usage.basis, MAX_CONTEXT_LENGTH, f"usage basis in {section_id}", errors)
        if not isinstance(usage.subcategory, str) or len(usage.subcategory) > MAX_CONTEXT_LENGTH:
            errors.append(f"invalid usage subcategory in {section_id}")
        _evidence(usage.evidence, f"usage evidence in {section_id}", errors)


def _section(section, identifiers, usages, seen, errors):
    if not isinstance(section, Section):
        errors.append("invalid section")
        return
    if not _text(section.section_id, MAX_SECTION_ID_LENGTH, "section id", errors):
        return
    if section.section_id in seen:
        errors.append(f"duplicate section {section.section_id}")
    seen.add(section.section_id)
    if not isinstance(section.service, str) or section.service not in SERVICES:
        errors.append(f"invalid service in {section.section_id}")
    if not isinstance(section.subcategory, str) or len(section.subcategory) > MAX_CONTEXT_LENGTH:
        errors.append(f"invalid subcategory in {section.section_id}")
    for name in ("net_minor", "vat_minor", "gross_minor"):
        _money(getattr(section, name), f"{name} in {section.section_id}", errors)
    for name in ("start", "end"):
        _date(getattr(section, name), f"{name} in {section.section_id}", errors)
    for name in ("period_evidence", "tax_evidence", "total_evidence"):
        _evidence(getattr(section, name), f"{name} in {section.section_id}", errors)
    _identifiers(section.identifiers, identifiers, f"identifiers in {section.section_id}", errors)
    _usages(section.usages, usages, section.section_id, errors)


def validate_candidate(candidate) -> None:
    """Raise ContractError unless the candidate shape is bounded and serializable."""
    errors = []
    if not isinstance(candidate, Candidate):
        raise ContractError("invalid candidate")
    for name in HEADER_STRINGS:
        value = getattr(candidate, name)
        if not isinstance(value, str) or len(value) > HEADER_LIMITS[name]:
            errors.append(f"invalid {name}")
    for name in HEADER_MONEY:
        _money(getattr(candidate, name), name, errors)
    for name in ("invoice_date", "due_date"):
        _date(getattr(candidate, name), name, errors)
    if (
        not isinstance(candidate.header_evidence, dict)
        or len(candidate.header_evidence) > MAX_HEADER_EVIDENCE
        or not all(
            isinstance(key, str) and isinstance(value, Evidence)
            for key, value in candidate.header_evidence.items()
        )
    ):
        errors.append("invalid header_evidence")
    else:
        for key, value in candidate.header_evidence.items():
            _text(key, MAX_CONTEXT_LENGTH, "header evidence key", errors)
            _evidence(value, f"header evidence {key}", errors)
    if candidate.requires_review is not True:
        errors.append("requires_review must be True")
    if not isinstance(candidate.issues, list) or len(candidate.issues) > MAX_ISSUES:
        errors.append("invalid issues")
    else:
        for issue in candidate.issues:
            if not isinstance(issue, str) or len(issue) > MAX_ISSUE_LENGTH:
                errors.append("invalid issue")
    identifiers = [0]
    usages = [0]
    _identifiers(candidate.identifiers, identifiers, "identifiers", errors)
    seen = set()
    if not isinstance(candidate.sections, list):
        errors.append("invalid sections")
    else:
        if len(candidate.sections) > MAX_SECTIONS:
            errors.append("too many sections")
        for section in candidate.sections:
            _section(section, identifiers, usages, seen, errors)
    if identifiers[0] > MAX_IDENTIFIERS:
        errors.append("too many identifiers")
    if usages[0] > MAX_USAGES:
        errors.append("too many usages")
    if not errors:
        try:
            json.dumps(candidate.to_dict(), allow_nan=False)
        except Exception:
            errors.append("unserializable candidate")
    if errors:
        raise ContractError("; ".join(dict.fromkeys(errors)))


def _safe_section(section):
    try:
        validate_candidate(
            Candidate(
                "",
                "",
                "",
                "",
                None,
                None,
                "SEK",
                None,
                None,
                None,
                None,
                None,
                {},
                sections=[section],
            )
        )
    except Exception:
        return None
    return section


def _safe_identifier(identifier):
    if not isinstance(identifier, Identifier):
        return False
    errors = []
    _identifiers([identifier], [0], "identifier", errors)
    return not errors


def safe_candidate(source, fallback: Candidate) -> Candidate:
    """Rebuild a reviewable candidate from independently valid retained values.

    Header fields from a malformed proposal are kept one by one; headers retain the
    last validated snapshot. Malformed sections, identifiers and usages are dropped
    with a visible issue instead of surfacing as later attribute or JSON errors.
    """
    header = {field.name: getattr(fallback, field.name) for field in fields(Candidate)}
    if isinstance(source, Candidate):
        for name in (*HEADER_STRINGS, *HEADER_MONEY, "invoice_date", "due_date"):
            errors = []
            value = getattr(source, name)
            if name in HEADER_MONEY:
                _money(value, name, errors)
            elif name in ("invoice_date", "due_date"):
                _date(value, name, errors)
            elif isinstance(value, str) and len(value) <= HEADER_LIMITS[name]:
                pass
            else:
                errors.append(f"invalid {name}")
            if not errors:
                header[name] = value
        header["header_evidence"] = dict(
            list(
                {
                    key: value
                    for key, value in (
                        source.header_evidence.items()
                        if isinstance(source.header_evidence, dict)
                        else ()
                    )
                    if isinstance(key, str)
                    and isinstance(value, Evidence)
                    and _safe_evidence(value)
                }.items()
            )[:MAX_HEADER_EVIDENCE]
        )
        header["identifiers"] = [
            identifier
            for identifier in (source.identifiers if isinstance(source.identifiers, list) else [])
            if _safe_identifier(identifier)
        ][:MAX_IDENTIFIERS]
        sections = [
            section
            for section in (source.sections if isinstance(source.sections, list) else [])
            if isinstance(section, Section) and _safe_section(section) is not None
        ][:MAX_SECTIONS]
        header["sections"] = sections
        issues = [
            issue
            for issue in (source.issues if isinstance(source.issues, list) else [])
            if isinstance(issue, str) and len(issue) <= MAX_ISSUE_LENGTH
        ]
    else:
        issues = []
    header["issues"] = issues
    header["requires_review"] = True
    candidate = Candidate(**header)
    try:
        validate_candidate(candidate)
    except ContractError:
        header["sections"] = []
        header["identifiers"] = []
        header["issues"] = [*issues, "Discarded malformed extraction values."][:MAX_ISSUES]
        candidate = Candidate(**header)
    return candidate


def _safe_evidence(value):
    errors = []
    _evidence(value, "header evidence", errors)
    return not errors
