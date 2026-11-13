"""Read and propose outside database write transactions; never approve extraction."""

from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import transaction
from filelock import FileLock

from .models import Document, Invoice
from .parsing.common import header
from .parsing.normalization import anchor
from .parsing.reader import PDFReadError, read_pdf
from .parsing.recognition import recognize_vendor
from .parsing.registry import LAYOUTS, match_layouts
from .services import StaleReviewError, record_extraction, resolve_section_property
from .storage import open_original, private_path
from .waste_costs import split_configured_costs


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

        recognition = recognize_vendor(pages)
        families = (
            list(recognition.vendor.layout_families.values_list("family", flat=True))
            if recognition.vendor
            else list(LAYOUTS)
        )
        matches = match_layouts(pages, families)
        parser = matches[0] if len(matches) == 1 and not read_error else None
        candidate = header(
            pages,
            parser.FAMILY if parser else "",
            parser.VERSION if parser else "",
            parser.REVISION if parser else "",
        )
        routing = {
            "vendor_id": recognition.vendor.pk if recognition.vendor else None,
            "issuer_identifiers": [asdict(identifier) for identifier in recognition.identifiers],
            "layout_matches": [
                {"family": p.FAMILY, "version": p.VERSION, "revision": p.REVISION} for p in matches
            ],
            "section_properties": {},
        }
        error = read_error or recognition.issue
        if not error and not parser:
            error = "Invoice layout is ambiguous." if matches else "Invoice layout is unsupported."
        if not read_error and not any(line for page in pages for line in page):
            error = "PDF has no readable text. Manual review is required."
        if parser:
            try:
                if hasattr(parser, "read_header"):
                    candidate = parser.read_header(pages)
                parser.parse(pages, candidate=candidate)
            except Exception:
                # Retain in-progress fields even for an unexpected parser bug.
                error = (
                    "Invoice extraction failed. Review the retained source and candidate values."
                )
            for section in candidate.sections:
                try:
                    house = (
                        resolve_section_property(recognition.vendor, section.identifiers)
                        if recognition.vendor
                        else None
                    )
                    routing["section_properties"][section.section_id] = house.pk if house else None
                    if house is None:
                        candidate.issues.append(f"Missing property mapping in {section.section_id}")
                except ValidationError:
                    routing["section_properties"][section.section_id] = None
                    candidate.issues.append(
                        f"Conflicting property mappings in {section.section_id}"
                    )
            if parser.FAMILY == "layout_01":
                properties, warnings = split_configured_costs(
                    candidate,
                    pages,
                    routing["section_properties"],
                )
                routing["section_properties"] = properties
                candidate.issues.extend(warnings)
        electricity = []
        for section in candidate.sections:
            house_id = routing["section_properties"].get(section.section_id)
            for usage in section.usages:
                if usage.utility != "electricity" or house_id is None:
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
                for page in pages
                for line in page
                for label in ("Kreditfaktura", "Kreditnota")
            )
            else Invoice.Kind.INVOICE
        )
        routing["kind"] = kind
        candidate.issues = list(dict.fromkeys(candidate.issues))
        return record_extraction(
            document_id,
            candidate,
            error=error,
            routing=routing,
            needs_review=True,
            page_evidence=[asdict(line) for page in pages for line in page],
            vendor=recognition.vendor,
            kind=kind,
        )
