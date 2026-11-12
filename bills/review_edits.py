"""Normalized edits and evidence reconstruction shared by HTML and HTTP adapters."""

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.core.validators import MaxLengthValidator

from .configuration import validate_classification
from .models import Property, Vendor
from .parsing.compatibility import cost_totals_candidate
from .parsing.types import Candidate, Evidence, Identifier, Section
from .parsing.types import Usage as CandidateUsage
from .validators import normalize_identifier

REVIEW_LIMITS = {"sections": 20, "usages": 60, "identifiers": 60}
REVIEW_IDENTIFIER_MAX_LENGTH = 200
SECTION_MONEY = ("net_minor", "vat_minor", "gross_minor")
HEADER_MONEY = (
    "net_minor",
    "vat_minor",
    "rounding_minor",
    "invoice_total_minor",
    "amount_due_minor",
)
INVOICE_SCOPE = "invoice"


def normalize_review_identifier(kind, value):
    """Validate the persisted value after Unicode case conversion can expand it."""
    value = normalize_identifier(kind, value)
    if not value:
        raise ValidationError("An identifier value is required.")
    MaxLengthValidator(REVIEW_IDENTIFIER_MAX_LENGTH)(value)
    return value


@dataclass(frozen=True)
class ReviewEdits:
    header: dict
    sections: list[dict]
    usages: list[dict]
    identifiers: list[dict]
    amount_due_confirmed: bool = False
    warnings_confirmed: bool = False
    note: str = ""


def evidence_page(evidence):
    return evidence.get("page", 1) if isinstance(evidence, dict) else 1


def reviewed_evidence(original, *, page, unchanged, label):
    if unchanged and original and evidence_page(original) == page:
        return Evidence(original["page"], original["text"], tuple(original["bbox"]))
    return Evidence(page, f"Manually reviewed: {label}", (0, 0, 0, 0))


def source_rows(values):
    values = cost_totals_candidate(values)
    usages, identifiers = [], []
    identifiers.extend({**row, "scope": INVOICE_SCOPE} for row in values.get("identifiers", []))
    for section in values.get("sections", []):
        key = section["section_id"]
        identifiers.extend({**row, "scope": key} for row in section.get("identifiers", []))
        usages.extend({**row, "section_id": key} for row in section.get("usages", []))
    return usages, identifiers


def build_review(edits, *, values, warnings=()):
    """Build from the token-bound source; submitted row order never identifies evidence."""
    values = cost_totals_candidate(values)
    if warnings and not edits.warnings_confirmed:
        raise ValidationError(
            {"warnings_confirmed": "Confirm that extraction warnings were resolved."}
        )
    for name, limit in REVIEW_LIMITS.items():
        if len(getattr(edits, name)) > limit:
            raise ValidationError({name: f"At most {limit} rows are accepted."})
    raw_usages, raw_identifiers = source_rows(values)
    for name, raw in (("usages", raw_usages), ("identifiers", raw_identifiers)):
        indexes = [
            row["source_index"] for row in getattr(edits, name) if row["source_index"] is not None
        ]
        if len(set(indexes)) != len(indexes) or any(
            type(i) is not int or not 0 <= i < len(raw) for i in indexes
        ):
            raise ValidationError(
                {name: "Source indexes must be distinct indexes in the observed source."}
            )
    header = edits.header
    Vendor.objects.get(pk=header["vendor_id"])
    evidence = dict(values.get("header_evidence", {}))
    aliases = {
        "net_minor": "net",
        "vat_minor": "vat",
        "rounding_minor": "rounding",
        "amount_due_minor": "amount_due",
    }
    for field in ("reference", "invoice_date", "due_date", "currency", *HEADER_MONEY):
        value = header[field]
        comparison = value.isoformat() if isinstance(value, date) else value
        key = aliases.get(field, field)
        evidence[key] = reviewed_evidence(
            values.get("header_evidence", {}).get(key),
            page=header["source_page"],
            unchanged=comparison == values.get(field),
            label=field,
        )
    candidate = Candidate(
        layout_family=values.get("layout_family") or "manual",
        layout_version=values.get("layout_version") or "",
        parser_revision=values.get("parser_revision") or "",
        reference=header["reference"],
        invoice_date=header["invoice_date"],
        due_date=header["due_date"],
        currency=header["currency"],
        **{field: header[field] for field in HEADER_MONEY},
        header_evidence=evidence,
    )
    originals = {section["section_id"]: section for section in values.get("sections", [])}
    sections, properties = {}, {}
    for data in edits.sections:
        key = data["section_id"]
        if key in sections or key == INVOICE_SCOPE:
            raise ValidationError({"sections": "Each allocation must be distinct."})
        validate_classification(data["category"], data["subcategory"])
        original = originals.get(key, {})
        section = Section(
            key,
            data["category"],
            [],
            subcategory=data["subcategory"] or "",
            **{f: data[f] for f in SECTION_MONEY},
            start=data["period_start"],
            end=data["period_end"],
        )
        section.period_evidence = reviewed_evidence(
            original.get("period_evidence"),
            page=data["source_page"],
            unchanged=(section.start.isoformat(), section.end.isoformat())
            == (original.get("start"), original.get("end")),
            label="billing period",
        )
        for field in ("tax_evidence", "total_evidence"):
            setattr(
                section,
                field,
                reviewed_evidence(
                    original.get(field),
                    page=data["source_page"],
                    unchanged=all(data[f] == original.get(f) for f in SECTION_MONEY),
                    label=field,
                ),
            )
        candidate.sections.append(section)
        sections[key], properties[key] = section, Property.objects.get(pk=data["property_id"])
    for data in edits.usages:
        if data["section_id"] not in sections:
            raise ValidationError({"usages": "Consumption must refer to a retained allocation."})
        index = data["source_index"]
        original = raw_usages[index] if index is not None else {}
        row = {
            field: data[field]
            for field in ("utility", "subcategory", "unit", "quantity_milli", "basis")
        }
        row["subcategory"] = row["subcategory"] or ""
        row.update(start=data["period_start"], end=data["period_end"])
        unchanged = data["section_id"] == original.get("section_id") and all(
            (value.isoformat() if isinstance(value, date) else value) == original.get(field)
            for field, value in row.items()
        )
        row["evidence"] = reviewed_evidence(
            original.get("evidence"), page=data["source_page"], unchanged=unchanged, label="usages"
        )
        sections[data["section_id"]].usages.append(CandidateUsage(**row))
    for data in edits.identifiers:
        if data["scope"] != INVOICE_SCOPE and data["scope"] not in sections:
            raise ValidationError(
                {"identifiers": "Identifiers must refer to a retained allocation or invoice."}
            )
        index = data["source_index"]
        original = raw_identifiers[index] if index is not None else {}
        try:
            normalized = normalize_review_identifier(data["kind"], data["value"])
        except ValidationError as exc:
            raise ValidationError({"identifiers": exc}) from exc
        unchanged = all(
            data[field] == original.get(field) for field in ("kind", "scope")
        ) and normalized == normalize_identifier(data["kind"], original.get("value", ""))
        identifier = Identifier(
            data["kind"],
            normalized,
            reviewed_evidence(
                original.get("evidence"),
                page=data["source_page"],
                unchanged=unchanged,
                label="identifier",
            ),
        )
        destination = (
            candidate.identifiers
            if data["scope"] == INVOICE_SCOPE
            else sections[data["scope"]].identifiers
        )
        destination.append(identifier)
    return candidate, properties
