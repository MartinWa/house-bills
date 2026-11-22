"""Read and propose outside database write transactions; never approve extraction."""

import copy
from dataclasses import asdict

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from filelock import FileLock

from .models import Document, Invoice
from .parsing.api import API_VERSION, configure
from .parsing.common import header
from .parsing.normalization import anchor
from .parsing.reader import PDFReadError, read_pdf
from .parsing.recognition import recognize_vendor
from .parsing.registry import (
    begin_document,
    host_implementation_digest,
    layouts,
    match_layouts,
    parser_identity,
    parser_set_digest,
    record_failure,
    status,
    take_failures,
)
from .parsing.validation import ContractError, safe_candidate, validate_candidate
from .services import StaleReviewError, record_extraction, resolve_section_property
from .storage import open_original, private_path
from .waste_costs import configured_section_configuration

EXTRACTION_FAILED = "Invoice extraction failed. Review the retained source and candidate values."
AMBIGUOUS = "Invoice layout is ambiguous."


def _copied(pages):
    """Give a parser its own page lists; the evidence lines themselves are frozen."""
    return [list(page) for page in pages]


def _identity_agrees(candidate, parser):
    return (
        candidate.layout_family,
        candidate.layout_version,
        candidate.parser_revision,
    ) == (parser.FAMILY, parser.VERSION, parser.REVISION)


def _read_header(parser, pages, candidate):
    """Use format-specific headers, but never replace a validated fallback with junk."""
    if not hasattr(parser, "read_header"):
        return candidate, False
    try:
        proposed = parser.read_header(pages)
        validate_candidate(proposed)
        if not _identity_agrees(proposed, parser):
            raise ContractError("parser metadata mismatch")
    except Exception:
        record_failure(parser, "read_header", "contract_failure")
        return candidate, True
    return proposed, False


def _recovered(source, fallback):
    """Keep valid partial values but never retain a rejected parser identity."""
    recovered = safe_candidate(source, fallback)
    (
        recovered.layout_family,
        recovered.layout_version,
        recovered.parser_revision,
    ) = (fallback.layout_family, fallback.layout_version, fallback.parser_revision)
    return recovered


def _parse(parser, pages, candidate):
    """Invoke parse on a copy so a corrupting or raising parser cannot destroy the fallback."""
    fallback = copy.deepcopy(candidate)
    working = copy.deepcopy(candidate)
    try:
        returned = parser.parse(pages, candidate=working)
        if returned is not working:
            raise ContractError("parse must return the supplied candidate")
        validate_candidate(working)
        if not _identity_agrees(working, parser):
            raise ContractError("parser metadata mismatch")
    except Exception:
        record_failure(parser, "parse", "contract_failure")
        return _recovered(working, fallback), True
    return working, False


def _property_mapping(candidate, routing, vendor):
    for section in candidate.sections:
        try:
            house = resolve_section_property(vendor, section.identifiers) if vendor else None
            routing["section_properties"][section.section_id] = house.pk if house else None
            if house is None:
                candidate.issues.append(f"Missing property mapping in {section.section_id}")
        except ValidationError:
            routing["section_properties"][section.section_id] = None
            candidate.issues.append(f"Conflicting property mappings in {section.section_id}")


def _configured_split(parser, candidate, pages, routing):
    """Invoke the optional operation without privileging any parser family."""
    if not hasattr(parser, "configure_sections"):
        return
    properties = routing["section_properties"]
    selected = frozenset(properties)
    try:
        properties, warnings = configure(
            candidate,
            pages,
            properties,
            family=parser.FAMILY,
            version=parser.VERSION,
            operation="waste_cost_split",
            section_ids=selected,
            configuration=configured_section_configuration(properties),
        )
    except ContractError:
        record_failure(parser, "configure_sections", "contract_failure")
        candidate.issues.append("Configured cost split failed; costs remain combined.")
        return
    routing["section_properties"] = properties
    candidate.issues.extend(warnings)


def _ambiguity_message(matches):
    named = ", ".join(f"{parser.FAMILY}/{parser.VERSION}" for parser in matches[:4])
    if len(matches) > 4:
        named = f"{named}, and more"
    return f"{AMBIGUOUS} Matching parsers: {named}."


def parse_document(document_id, *, force=False, expected_revision=None):
    """Append an immutable proposal; initial processing is idempotent across workers.

    Explicit re-parsing preserves every approved value and review revision.
    Database/storage failures propagate so the retained original can be retried.
    """
    if transaction.get_connection().in_atomic_block:
        raise RuntimeError("Parse outside database transactions.")
    try:
        source = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        return None
    # Separate from the backup/move lock, so PDF work does not block other intake.
    lock_path = private_path(f".parse-{source.sha256}.lock")
    with FileLock(str(lock_path), timeout=10, mode=0o600):
        try:
            source.refresh_from_db()
        except Document.DoesNotExist:
            return None
        if expected_revision is not None and (
            type(expected_revision) is not int or source.extraction_revision != expected_revision
        ):
            raise StaleReviewError(
                "This document has a newer extraction; reload before re-parsing."
            )
        if source.extraction_revision and not force:
            return source.extractions.order_by("-revision").first()
        if not source.original_path or source.storage_state == Document.StorageState.FAILED:
            return None
        Document.objects.filter(pk=document_id).update(
            state=Document.State.PROCESSING, processing_message="Reading invoice."
        )
        pages, read_error = [], ""
        try:
            handle, _ = open_original(document_id)
            with handle:
                pages = read_pdf(handle)
        except PDFReadError as exc:
            pages, read_error = exc.pages, str(exc)
        except Document.DoesNotExist:
            return None
        except OSError, ValidationError:
            read_error = "Original is unavailable or its checksum does not match. Retry storage."

        # Parser entry points receive copies; the reader's snapshot stays immutable.
        evidence_pages = _copied(pages)
        begin_document()
        recognition = recognize_vendor(evidence_pages)
        families = (
            list(recognition.vendor.layout_families.values_list("family", flat=True))
            if recognition.vendor
            else list(layouts())
        )
        matches = match_layouts(_copied(evidence_pages), families)
        parser = matches[0] if len(matches) == 1 and not read_error else None
        routing = {
            "vendor_id": recognition.vendor.pk if recognition.vendor else None,
            "issuer_identifiers": [asdict(identifier) for identifier in recognition.identifiers],
            "layout_matches": [parser_identity(match) for match in matches],
            "selected_parser": parser_identity(parser) if parser else None,
            "plugin_set_digest": parser_set_digest(settings.APP_VERSION),
            "host_implementation": host_implementation_digest(),
            "api_version": API_VERSION,
            "app_version": settings.APP_VERSION,
            "plugin_errors": [
                {"filename": error.filename, "code": error.code} for error in status().errors
            ],
            "failures": [],
            "section_properties": {},
        }
        fallback = header(
            evidence_pages,
            parser.FAMILY if parser else "",
            parser.VERSION if parser else "",
            parser.REVISION if parser else "",
        )
        candidate = fallback
        error = read_error or recognition.issue
        if not error and not parser:
            error = _ambiguity_message(matches) if matches else "Invoice layout is unsupported."
        if not read_error and not any(line for page in evidence_pages for line in page):
            error = "PDF has no readable text. Manual review is required."
        if parser:
            candidate, header_failed = _read_header(parser, _copied(evidence_pages), fallback)
            candidate, parse_failed = _parse(parser, _copied(evidence_pages), candidate)
            if header_failed or parse_failed:
                # Retain in-progress fields even for an unexpected parser bug.
                error = EXTRACTION_FAILED
            _property_mapping(candidate, routing, recognition.vendor)
            _configured_split(parser, candidate, _copied(evidence_pages), routing)
        electricity = []
        for section in candidate.sections:
            house_id = routing["section_properties"].get(section.section_id)
            for usage in section.usages:
                if usage.utility != "electricity" or usage.unit != "kWh" or house_id is None:
                    continue
                if usage.start is None or usage.end is None:
                    # Validate the contract before comparing; never fail the attempt here.
                    candidate.issues.append(
                        f"Missing usage period in {section.section_id} needs review."
                    )
                    continue
                for other, other_house, other_service in electricity:
                    if (
                        house_id == other_house
                        and section.service == other_service
                        and usage.basis == other.basis
                        and usage.start < other.end
                        and other.start < usage.end
                    ):
                        candidate.issues.append("Overlapping electricity usage needs review.")
                electricity.append((usage, house_id, section.service))
        if error:
            candidate.issues.append(error)
        kind = (
            Invoice.Kind.CREDIT
            if any(
                anchor(line, label)
                for page in evidence_pages
                for line in page
                for label in ("Kreditfaktura", "Kreditnota")
            )
            else Invoice.Kind.INVOICE
        )
        routing["kind"] = kind
        candidate.issues = list(dict.fromkeys(candidate.issues))
        try:
            validate_candidate(candidate)
        except ContractError:
            candidate = _recovered(candidate, fallback)
            candidate.issues.append(EXTRACTION_FAILED)
            error = error or EXTRACTION_FAILED
            routing["section_properties"] = {
                section.section_id: routing["section_properties"].get(section.section_id)
                for section in candidate.sections
            }
        routing["failures"] = take_failures()
        return record_extraction(
            document_id,
            candidate,
            error=error,
            routing=routing,
            needs_review=True,
            page_evidence=[asdict(line) for page in evidence_pages for line in page],
            vendor=recognition.vendor,
            kind=kind,
        )
