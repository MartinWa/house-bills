"""Explicit retained-document re-parsing; review/approval remains a separate action."""

from django.core.management.base import BaseCommand, CommandError

from bills.models import Document
from bills.processing import parse_document


class Command(BaseCommand):
    help = "Propose new extraction attempts for selected document IDs, preserving approvals."

    def add_arguments(self, parser):
        parser.add_argument("document_ids", nargs="+", type=int)

    def handle(self, *args, **options):
        ids = list(dict.fromkeys(options["document_ids"]))
        if Document.objects.filter(pk__in=ids).count() != len(ids):
            raise CommandError("Select existing document IDs.")
        for document_id in ids:
            attempt = parse_document(document_id, force=True)
            if attempt is None:
                raise CommandError(f"Document {document_id}: recover original storage first.")
            self.stdout.write(
                f"Document {document_id}: extraction {attempt.revision}; needs review."
            )
