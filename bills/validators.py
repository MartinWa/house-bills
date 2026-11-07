"""Private configuration and structured source-evidence validation."""

import math
from pathlib import PurePosixPath

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator

storage_slug_validator = RegexValidator(
    r"\A[a-z0-9]+(?:-[a-z0-9]+)*\Z", "Use lowercase letters, digits and single hyphens."
)
sha256_validator = RegexValidator(r"\A[0-9a-f]{64}\Z", "Use a lowercase SHA-256 hex digest.")


def validate_storage_path(value):
    path = PurePosixPath(value)
    if value and (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or "\x00" in value
        or str(path) != value
    ):
        raise ValidationError("Use a normalized relative path inside private storage.")


def normalize_identifier(kind, value):
    value = " ".join(value.split())
    if kind in {"installation", "organization", "vat"}:
        return value.replace(" ", "").replace("-", "").upper()
    if kind == "delivery_address":
        return value.casefold()
    return value


def validate_evidence(value):
    if value == {}:
        return
    if not isinstance(value, dict) or set(value) != {"page", "text", "bbox"}:
        raise ValidationError("Evidence needs page, text and bbox.")
    page, bbox = value["page"], value["bbox"]
    if type(page) is not int or page < 1 or not isinstance(value["text"], str):
        raise ValidationError("Evidence needs a one-based page and source text.")
    if (
        not isinstance(bbox, (list, tuple))
        or len(bbox) != 4
        or any(type(n) not in {int, float} or not math.isfinite(n) or n < 0 for n in bbox)
        or bbox[0] > bbox[2]
        or bbox[1] > bbox[3]
    ):
        raise ValidationError("Evidence needs a finite ordered PDF bounding box.")


def validate_pages(value):
    if not isinstance(value, list) or any(type(page) is not int or page < 1 for page in value):
        raise ValidationError("Source pages must be a list of one-based page numbers.")
    if len(set(value)) != len(value):
        raise ValidationError("Source pages must be distinct.")


def validate_header_evidence(value):
    if not isinstance(value, dict):
        raise ValidationError("Header evidence must be keyed by field name.")
    for evidence in value.values():
        validate_evidence(evidence)


def validate_source_identifiers(value):
    if not isinstance(value, list):
        raise ValidationError("Source identifiers must be a list.")
    for identifier in value:
        if (
            not isinstance(identifier, dict)
            or set(identifier) != {"kind", "value", "evidence"}
            or not isinstance(identifier["kind"], str)
            or not isinstance(identifier["value"], str)
            or not identifier["kind"].strip()
            or not identifier["value"].strip()
        ):
            raise ValidationError("Source identifiers need kind, value and evidence.")
        validate_evidence(identifier["evidence"])
