"""Signed form context binds edits to immutable evidence and observed revisions."""

from dataclasses import asdict, dataclass

from django.core import signing
from django.core.exceptions import ValidationError

from .models import ExtractionAttempt, Invoice, ReviewRevision
from .services import invoice_for_review

REVIEW_SALT = "bills.document-review.cost-subcategories"


@dataclass(frozen=True)
class ReviewContext:
    document_id: int
    invoice_id: int | None
    review_revision: int
    extraction_revision: int
    review_id: int | None
    extraction_id: int | None

    def token(self):
        return signing.dumps(asdict(self), salt=REVIEW_SALT)

    @classmethod
    def from_token(cls, token, document_id):
        try:
            context = cls(**signing.loads(token, salt=REVIEW_SALT))
            if context.document_id != document_id:
                raise ValueError
            return context
        except signing.BadSignature, TypeError, ValueError:
            raise ValidationError("The review form is invalid; reload it before saving.") from None

    def source(self):
        if self.review_id is not None:
            review = ReviewRevision.objects.filter(
                pk=self.review_id, invoice_id=self.invoice_id
            ).first()
            if review is None or review.invoice.document_id != self.document_id:
                raise ValidationError("The approved review source is unavailable; reload the form.")
            return {
                "values": review.snapshot["values"],
                "vendor_id": review.snapshot["vendor_id"],
                "properties": review.snapshot["properties"],
                "kind": review.snapshot["kind"],
                "warnings": [],
                "extraction": review.extraction,
            }
        attempt = (
            ExtractionAttempt.objects.filter(
                pk=self.extraction_id, document_id=self.document_id
            ).first()
            if self.extraction_id is not None
            else None
        )
        if self.extraction_id is not None and attempt is None:
            raise ValidationError("The extraction source is unavailable; reload the form.")
        values = attempt.candidate if attempt else {}
        routing = values.get("routing", {})
        warnings = list(values.get("issues", []))
        if attempt and attempt.error and attempt.error not in warnings:
            warnings.append(attempt.error)
        return {
            "values": values,
            "vendor_id": routing.get("vendor_id"),
            "properties": routing.get("section_properties", {}),
            "kind": routing.get("kind", Invoice.Kind.INVOICE),
            "warnings": warnings,
            "extraction": attempt,
        }


def current_review_context(document, *, proposal=False):
    invoice = invoice_for_review(document.pk)
    review = (
        invoice.reviews.order_by("-revision").first()
        if invoice and invoice.status == Invoice.Status.APPROVED and not proposal
        else None
    )
    attempt = document.extractions.order_by("-revision").first()
    return ReviewContext(
        document_id=document.pk,
        invoice_id=invoice.pk if invoice else None,
        review_revision=invoice.review_revision if invoice else 0,
        extraction_revision=document.extraction_revision,
        review_id=review.pk if review else None,
        extraction_id=review.extraction_id if review else (attempt.pk if attempt else None),
    )
