"""Revision-aware correction batches; each approval owns its own transaction."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import IntegrityError

from .services import approve_document_review


def correction_reviewer(*, apply, reviewer_id, error):
    if not apply:
        return None
    actor = get_user_model().objects.filter(pk=reviewer_id, is_active=True, is_staff=True).first()
    if actor is None:
        raise CommandError(error)
    return actor


def run_corrections(invoices, *, planner, reviewer, note, output, error_output, failure_message):
    changed, untouched, failed = 0, 0, 0
    for invoice in invoices:
        try:
            plan, warnings = planner(invoice)
            for warning in warnings:
                output(f"Invoice {invoice.pk}: {warning}")
            if plan is None:
                untouched += 1
                continue
            if reviewer is not None:
                approve_document_review(
                    invoice.document_id,
                    expected_invoice_id=invoice.pk,
                    expected_extraction_revision=invoice.document.extraction_revision,
                    **plan,
                    actor=reviewer,
                    note=note,
                )
        except ValidationError as exc:
            failed += 1
            error_output(f"Invoice {invoice.pk}: {'; '.join(exc.messages)}")
            continue
        except IntegrityError as exc:
            # A concurrent approval can take the invoice identity first; this invoice
            # counts as failed and the batch continues with the remaining invoices.
            failed += 1
            error_output(f"Invoice {invoice.pk}: {exc}")
            continue
        changed += 1
    action = "Updated" if reviewer is not None else "Would update"
    output(
        f"{action} {changed} invoices; {untouched} unchanged; {failed} failed. "
        "Invoice totals retained."
    )
    if failed:
        raise CommandError(failure_message.format(failed=failed))
