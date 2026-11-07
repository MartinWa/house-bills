"""Short validated writes, separate from PDF reading and file publication."""

from dataclasses import asdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    BillingSection,
    Charge,
    Document,
    ExtractionAttempt,
    Invoice,
    Property,
    PropertyIdentifier,
    ReviewRevision,
    Usage,
)
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
):
    """Retain even invalid/partial results without changing approved invoice values."""
    document = Document.objects.select_for_update().get(pk=document_id)
    payload = candidate.to_dict() if candidate is not None else {}
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
    document.state = Document.State.FAILED if error else Document.State.REVIEW
    document.save(update_fields=["extraction_revision", "state"])
    return attempt


def validate_allocations(invoice):
    """Cross-row checks run before promotion, inside the same write transaction."""
    invoice.full_clean()
    sections = list(invoice.sections.prefetch_related("charges", "usages"))
    if not sections:
        raise ValidationError("An approved invoice needs at least one billing section.")
    totals = {"net_minor": 0, "vat_minor": 0, "gross_minor": 0}
    electricity = []
    for section in sections:
        section.full_clean()
        if section.property_id is None:
            raise ValidationError("Every billing section needs a property assignment.")
        charges = list(section.charges.all())
        if not charges:
            raise ValidationError("Every billing section needs charges.")
        for charge in charges:
            charge.full_clean()
            if None in (charge.net_minor, charge.vat_minor, charge.gross_minor):
                raise ValidationError("Every approved charge needs net, VAT and gross values.")
        for field in totals:
            amount = getattr(section, field)
            if amount is None or sum(getattr(charge, field) for charge in charges) != amount:
                raise ValidationError(f"Section {section.section_id} does not reconcile ({field}).")
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
    This is the persistence boundary for the later review screens, not auto-approval.
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
            identifiers=source_payload["identifiers"],
            net_minor=source.net_minor,
            vat_minor=source.vat_minor,
            gross_minor=source.gross_minor,
            tax_evidence=source_payload["tax_evidence"] or {},
            total_evidence=source_payload["total_evidence"] or {},
        )
        for position, charge in enumerate(source.charges):
            Charge.objects.create(
                section=section,
                position=position,
                description=charge.description,
                start=charge.start,
                end=charge.end,
                net_minor=charge.net_minor,
                vat_minor=charge.vat_minor,
                gross_minor=charge.gross_minor,
                vat_basis=charge.vat_basis,
                evidence=asdict(charge.evidence),
            )
        for position, usage in enumerate(source.usages):
            Usage.objects.create(
                section=section,
                position=position,
                utility=usage.utility,
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
    return invoice
