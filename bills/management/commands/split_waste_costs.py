"""Preview or explicitly apply source-backed fraction corrections with review history."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from bills.models import Invoice, Property
from bills.services import approve_document_review
from bills.waste_costs import plan_retained_cost_split


class Command(BaseCommand):
    help = "Split configured waste fraction charges from retained evidence; preview by default."

    def add_arguments(self, parser):
        parser.add_argument("--property", dest="property_id", type=int, required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--reviewer", type=int, help="Active staff user ID required with --apply."
        )

    def handle(self, *args, **options):
        property_id = options["property_id"]
        if not Property.objects.filter(pk=property_id).exists():
            raise CommandError("Select an existing property ID.")
        actor = None
        if options["apply"]:
            actor = (
                get_user_model()
                .objects.filter(pk=options["reviewer"], is_active=True, is_staff=True)
                .first()
            )
            if actor is None:
                raise CommandError(
                    "Applying corrections requires --reviewer with an active staff user ID."
                )
        invoices = (
            Invoice.objects.filter(
                status=Invoice.Status.APPROVED,
                sections__property_id=property_id,
                sections__category="waste",
                document__source="upload",
            )
            .select_related("document", "vendor")
            .distinct()
            .order_by("pk")
        )
        changed, untouched = 0, 0
        for invoice in invoices:
            plan, warnings = plan_retained_cost_split(invoice, property_id)
            for warning in warnings:
                self.stdout.write(f"Invoice {invoice.pk}: {warning}")
            if plan is None:
                untouched += 1
                continue
            if actor is not None:
                approve_document_review(
                    invoice.document_id,
                    expected_invoice_id=invoice.pk,
                    expected_extraction_revision=invoice.document.extraction_revision,
                    **plan,
                    actor=actor,
                    note=(
                        "Separated printed waste fraction charges. Shared fees remain under "
                        "general waste collection. VAT apportioned by net cost with rounding "
                        "balanced to the original printed contract VAT. "
                        "Invoice headers, totals and reported weights retained."
                    ),
                )
            changed += 1
        action = "Updated" if actor is not None else "Would update"
        self.stdout.write(
            f"{action} {changed} invoices; {untouched} unchanged. Invoice totals retained."
        )
