"""Short validated writes, separate from PDF reading and file publication."""

from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction
from django.utils import timezone
from filelock import Timeout

from .models import (
    BillingSection,
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    PropertyIdentifier,
    ReviewRevision,
    Usage,
)
from .storage import coordinated_write, publish_original
from .validators import normalize_identifier


class StaleReviewError(ValidationError):
    pass


def resolve_section_property(vendor, identifiers):
    """Specific mappings precede the configured default; conflicts need review."""
    specific, addresses = set(), set()
    for identifier in identifiers:
        kind = identifier.kind
        if kind not in PropertyIdentifier.Kind.values:
            continue
        matches = PropertyIdentifier.objects.filter(
            vendor=vendor, kind=kind, value=normalize_identifier(kind, identifier.value)
        ).values_list("property_id", flat=True)
        (addresses if kind == PropertyIdentifier.Kind.DELIVERY_ADDRESS else specific).update(
            matches
        )
    matches = specific or addresses
    if len(matches) > 1:
        raise ValidationError(
            "Section identifiers map to different properties; review the mapping."
        )
    if matches:
        return Property.objects.get(pk=next(iter(matches)))
    # Read the current saved default, even if the caller holds an older Vendor instance.
    return Property.objects.filter(default_for_vendors=vendor).first()


def _evidence_in(value):
    if isinstance(value, dict):
        if set(value) == {"page", "text", "bbox"}:
            yield value
        else:
            for item in value.values():
                yield from _evidence_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _evidence_in(item)


@coordinated_write
@transaction.atomic
def record_extraction(
    document_id,
    candidate=None,
    *,
    error="",
    layout_family="",
    layout_version="",
    parser_revision="",
    page_evidence=None,
    vendor=None,
    kind=Invoice.Kind.INVOICE,
    routing=None,
    needs_review=False,
):
    """Retain even invalid/partial results without changing approved invoice values."""
    document = Document.objects.select_for_update().get(pk=document_id)
    payload = candidate.to_dict() if candidate is not None else {}
    if routing is not None:
        payload["routing"] = routing
    if candidate is not None:
        layout_family = candidate.layout_family
        layout_version = candidate.layout_version
        parser_revision = candidate.parser_revision
    state = ExtractionAttempt.State.FAILED if error else ExtractionAttempt.State.REVIEW
    attempt = ExtractionAttempt.objects.create(
        document=document,
        revision=document.extraction_revision + 1,
        layout_family=layout_family,
        layout_version=layout_version,
        parser_revision=parser_revision,
        candidate=payload,
        page_evidence=list(_evidence_in(payload)) if page_evidence is None else page_evidence,
        state=state,
        error=error,
    )
    document.extraction_revision = attempt.revision
    document.state = Document.State.FAILED if error and not needs_review else Document.State.REVIEW
    document.processing_message = error or (
        "Extracted values need correction before approval."
        if candidate is not None and candidate.issues
        else "Extraction is ready for review."
    )
    document.save(update_fields=["extraction_revision", "state", "processing_message"])
    if (
        candidate is not None
        and vendor is not None
        and candidate.reference
        and len(candidate.reference.strip()) <= Invoice._meta.get_field("reference").max_length
    ):
        register_invoice_identity(
            document_id, vendor=vendor, reference=candidate.reference, kind=kind
        )
    return attempt


def invoice_identity_conflicts(invoice):
    if not invoice.vendor_id or not invoice.reference:
        return Invoice.objects.none()
    return Invoice.objects.filter(
        vendor_id=invoice.vendor_id, reference=invoice.reference, kind=invoice.kind
    ).exclude(document_id=invoice.document_id)


def invoice_for_review(document_id):
    """Current parsers yield one invoice; prefer its retained approved revision."""
    return (
        Invoice.objects.filter(document_id=document_id).order_by("-review_revision", "pk").first()
    )


@coordinated_write
@transaction.atomic
def discard_manual_entry(document_id):
    """Remove an empty manual placeholder, checking again inside the write transaction."""
    document = Document.objects.select_for_update().get(pk=document_id)
    if not document.can_discard_manual_entry:
        raise ValidationError("Only unfinished manual entries can be cancelled.")
    document.delete()


@coordinated_write
@transaction.atomic
def register_invoice_identity(document_id, *, vendor, reference, kind=Invoice.Kind.INVOICE):
    """Register a reviewed/recognized identity without promoting costs or usage."""
    reference = reference.strip()
    if not reference:
        raise ValidationError("An invoice reference is required for identity matching.")
    Document.objects.select_for_update().get(pk=document_id)
    invoice = invoice_for_review(document_id)
    if invoice is None:
        invoice = Invoice.objects.create(
            document_id=document_id, vendor=vendor, reference=reference, kind=kind
        )
    elif invoice.status != Invoice.Status.APPROVED:
        invoice.vendor, invoice.reference, invoice.kind = vendor, reference, kind
        invoice.save(update_fields=["vendor", "reference", "kind"])
    # A changed identity from re-parsing is a proposal for the same invoice. It
    # must neither rewrite an approval nor create another bill for its original.
    if (
        Invoice.objects.filter(vendor=vendor, reference=reference, kind=kind)
        .exclude(document_id=document_id)
        .exists()
    ):
        Document.objects.filter(pk=document_id).update(
            state=Document.State.REVIEW,
            processing_message="Different files share this invoice identity. Review the sources.",
        )
    return invoice


@coordinated_write
@transaction.atomic
def approve_document_review(
    document_id,
    *,
    expected_invoice_id,
    expected_extraction_revision,
    expected_revision,
    **review,
):
    """Create a manual invoice and approve once, or correct the observed invoice."""
    document = Document.objects.select_for_update().get(pk=document_id)
    if (
        type(expected_extraction_revision) is not int
        or document.extraction_revision != expected_extraction_revision
    ):
        raise StaleReviewError("This document has a newer extraction; reload before saving.")
    invoice = invoice_for_review(document_id)
    if (invoice.pk if invoice else None) != expected_invoice_id:
        raise StaleReviewError("This document has a newer invoice review; reload before saving.")
    if invoice is None:
        invoice = Invoice.objects.create(document=document)
    if (
        Invoice.objects.filter(
            vendor=review["vendor"],
            reference=review["candidate"].reference.strip(),
            kind=review.get("kind", Invoice.Kind.INVOICE),
            status=Invoice.Status.APPROVED,
        )
        .exclude(pk=invoice.pk)
        .exists()
    ):
        raise ValidationError(
            {"reference": "An approved invoice already uses this vendor, reference and type."}
        )
    return approve_review(invoice.pk, expected_revision=expected_revision, **review)


def validate_allocations(invoice):
    """Cross-row checks run before promotion, inside the same write transaction."""
    invoice.full_clean()
    sections = list(invoice.sections.prefetch_related("usages"))
    if not sections:
        raise ValidationError("An approved invoice needs at least one billing section.")
    totals = {"net_minor": 0, "vat_minor": 0, "gross_minor": 0}
    electricity = []
    for section in sections:
        section.full_clean()
        if section.property_id is None:
            raise ValidationError("Every billing section needs a property assignment.")
        if section.start is None or section.end is None:
            raise ValidationError("Every billing section needs its billing period.")
        for field in totals:
            amount = getattr(section, field)
            if amount is None:
                raise ValidationError(f"Section {section.section_id} needs complete cost totals.")
            totals[field] += amount
        for usage in section.usages.all():
            usage.full_clean()
            if usage.utility == Usage.Utility.ELECTRICITY:
                # A reversal and its replacement can overlap. Two positive-source
                # measurements with the same basis cannot silently become a sum.
                for other, property_id, service in electricity:
                    if (
                        section.property_id == property_id
                        and section.service == service
                        and usage.basis == other.basis
                        and usage.start < other.end
                        and other.start < usage.end
                    ):
                        raise ValidationError(
                            "Overlapping electricity usage for this property/source needs review."
                        )
                electricity.append((usage, section.property_id, section.service))
    if (
        totals["net_minor"] != invoice.net_minor
        or totals["vat_minor"] != invoice.vat_minor
        or totals["gross_minor"] + invoice.rounding_minor != invoice.invoice_total_minor
    ):
        raise ValidationError(
            "Property/service allocations and rounding do not reconcile to the invoice."
        )


@coordinated_write
@transaction.atomic
def approve_review(
    invoice_id,
    *,
    expected_revision,
    candidate,
    vendor,
    section_properties,
    actor,
    extraction=None,
    kind=Invoice.Kind.INVOICE,
    note="",
    amount_due_confirmed=False,
):
    """Promote an explicitly reviewed proposal, retaining earlier approved revisions.

    `section_properties` is keyed by section ID. Missing keys use saved identifier
    mappings; a human can explicitly correct a mapping for this invoice only.
    The web forms and other explicit review clients use this persistence boundary.
    """
    if not actor.is_authenticated or not actor.is_active:
        raise ValidationError("An active authenticated reviewer is required.")
    invoice = Invoice.objects.select_for_update().get(pk=invoice_id)
    if type(expected_revision) is not int or expected_revision != invoice.review_revision:
        raise StaleReviewError("This invoice has a newer review revision; reload before saving.")
    if extraction is not None and extraction.document_id != invoice.document_id:
        raise ValidationError("The extraction belongs to another document.")
    if candidate.issues:
        raise ValidationError("Resolve candidate issues before approval.")
    if candidate.amount_due_minor != candidate.invoice_total_minor and not amount_due_confirmed:
        raise ValidationError(
            "Confirm the difference between invoice total and amount due before approval."
        )
    section_ids = [section.section_id for section in candidate.sections]
    if len(set(section_ids)) != len(section_ids) or set(section_properties) - set(section_ids):
        raise ValidationError("Section assignments must refer to distinct candidate sections.")

    # Demotion, replacement and promotion commit together; failures restore the
    # previous approval and all its rows. SQLite uses BEGIN IMMEDIATE here.
    Invoice.objects.filter(pk=invoice.pk).update(
        status=Invoice.Status.REVIEW, approved_at=None, approved_by=None
    )
    invoice.status = Invoice.Status.REVIEW
    invoice.approved_at = invoice.approved_by = None
    invoice.sections.all().delete()
    invoice.vendor = vendor
    invoice.kind = kind
    for field in [
        "reference",
        "invoice_date",
        "due_date",
        "currency",
        "net_minor",
        "vat_minor",
        "rounding_minor",
        "invoice_total_minor",
        "amount_due_minor",
    ]:
        setattr(invoice, field, getattr(candidate, field))
    payload = candidate.to_dict()
    invoice.header_evidence = payload["header_evidence"]
    invoice.identifiers = payload["identifiers"]
    invoice.source_pages = sorted({item["page"] for item in _evidence_in(payload)})
    invoice.save()
    payload["reference"] = invoice.reference
    for source, source_payload in zip(candidate.sections, payload["sections"], strict=True):
        property = section_properties.get(source.section_id)
        if source.section_id not in section_properties:
            property = resolve_section_property(vendor, source.identifiers)
        section = BillingSection.objects.create(
            invoice=invoice,
            section_id=source.section_id,
            property=property,
            service=source.service,
            subcategory=source.subcategory,
            identifiers=source_payload["identifiers"],
            net_minor=source.net_minor,
            vat_minor=source.vat_minor,
            gross_minor=source.gross_minor,
            start=source.start,
            end=source.end,
            period_evidence=source_payload["period_evidence"] or {},
            tax_evidence=source_payload["tax_evidence"] or {},
            total_evidence=source_payload["total_evidence"] or {},
        )
        for position, usage in enumerate(source.usages):
            Usage.objects.create(
                section=section,
                position=position,
                utility=usage.utility,
                subcategory=usage.subcategory,
                unit=usage.unit,
                quantity_milli=usage.quantity_milli,
                start=usage.start,
                end=usage.end,
                basis=usage.basis,
                evidence=asdict(usage.evidence),
            )
    invoice.status = Invoice.Status.APPROVED
    invoice.review_revision += 1
    invoice.approved_by = actor
    invoice.approved_at = timezone.now()
    validate_allocations(invoice)
    snapshot = {
        "values": payload,
        "vendor_id": vendor.pk,
        "kind": kind,
        "properties": dict(invoice.sections.values_list("section_id", "property_id")),
    }
    ReviewRevision.objects.create(
        invoice=invoice,
        revision=invoice.review_revision,
        extraction=extraction,
        actor=actor,
        note=note,
        snapshot=snapshot,
        amount_due_confirmed=amount_due_confirmed,
    )
    Invoice.objects.filter(pk=invoice.pk).update(
        status=invoice.status,
        review_revision=invoice.review_revision,
        approved_at=invoice.approved_at,
        approved_by=actor,
    )
    Document.objects.filter(pk=invoice.document_id).update(
        state=Document.State.READY, processing_message=""
    )
    if invoice.document.original_path:
        # Approval commits before publication. A failed move leaves approved
        # values intact, with durable storage state for the retry service.
        def publish_when_approved():
            try:
                if (
                    not Invoice.objects.filter(document_id=invoice.document_id)
                    .exclude(status="approved")
                    .exists()
                ):
                    publish_original(invoice.document_id)
            except OSError, ValidationError, Timeout, DatabaseError:
                # The approval has already committed. Recovery will publish any
                # fully approved inbox original once storage/database access returns.
                try:
                    Document.objects.filter(pk=invoice.document_id).update(
                        storage_state=Document.StorageState.FAILED,
                        storage_error="Original publication is pending. Check storage and retry.",
                    )
                except DatabaseError:
                    pass

        transaction.on_commit(publish_when_approved)
    return invoice
