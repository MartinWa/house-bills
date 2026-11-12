"""Create manual placeholders, with optional retry identity for HTTP clients."""

from django.db import transaction

from .models import Document


@transaction.atomic
def create_manual_entry(*, idempotency_key=None):
    if idempotency_key is not None:
        existing = Document.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, True
    return Document.objects.create(
        source=Document.Source.MANUAL,
        state=Document.State.REVIEW,
        processing_message="Manual entry; no original PDF.",
        idempotency_key=idempotency_key,
    ), False
