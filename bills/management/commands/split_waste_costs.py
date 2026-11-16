"""Preview or explicitly apply source-backed fraction corrections with review history."""

from django.core.management.base import BaseCommand, CommandError

from bills.correction_batches import correction_reviewer, run_corrections
from bills.models import Invoice, Property
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
        actor = correction_reviewer(
            apply=options["apply"],
            reviewer_id=options["reviewer"],
            error="Applying corrections requires --reviewer with an active staff user ID.",
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
        run_corrections(
            invoices,
            planner=lambda invoice: plan_retained_cost_split(invoice, property_id),
            reviewer=actor,
            note=(
                "Separated printed waste fraction charges. Shared fees remain under "
                "general waste collection. VAT apportioned by net cost with rounding "
                "balanced to the original printed contract VAT. "
                "Invoice headers, totals and reported weights retained."
            ),
            output=self.stdout.write,
            error_output=lambda message: self.stderr.write(self.style.ERROR(message)),
            failure_message=(
                "{failed} invoices could not be corrected. Rerun to retry them; "
                "successful corrections were retained."
            ),
        )
