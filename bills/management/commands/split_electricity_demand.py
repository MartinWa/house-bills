"""Preview or apply demand charge corrections from retained invoice evidence."""

from django.core.management.base import BaseCommand, CommandError

from bills.correction_batches import correction_reviewer, run_corrections
from bills.electricity_demand import plan_retained_demand_split
from bills.models import Invoice, Property


class Command(BaseCommand):
    help = "Separate printed Effektavgift costs and billed kW; preview by default."

    def add_arguments(self, parser):
        parser.add_argument("--property", dest="property_id", type=int)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument(
            "--reviewer", type=int, help="Active staff user ID required with --apply."
        )

    def handle(self, *args, **options):
        property_id = options["property_id"]
        if property_id is not None and not Property.objects.filter(pk=property_id).exists():
            raise CommandError("Select an existing property ID.")
        actor = correction_reviewer(
            apply=options["apply"],
            reviewer_id=options["reviewer"],
            error="Applying corrections requires an active staff --reviewer ID.",
        )
        filters = {
            "status": Invoice.Status.APPROVED,
            "document__source": "upload",
            "sections__subcategory_id": "electricity_network",
        }
        if property_id is not None:
            filters["sections__property_id"] = property_id
        invoices = (
            Invoice.objects.filter(**filters)
            .select_related("document", "vendor")
            .distinct()
            .order_by("pk")
        )
        run_corrections(
            invoices,
            planner=lambda invoice: plan_retained_demand_split(invoice, property_id),
            reviewer=actor,
            note=(
                "Separated printed Effektavgift charges and billed kW from Elnät. "
                "VAT apportioned by net cost and balanced to the printed contract VAT. "
                "Invoice totals, approved corrections and kWh readings retained."
            ),
            output=self.stdout.write,
            error_output=lambda message: self.stderr.write(self.style.ERROR(message)),
            failure_message=(
                "{failed} invoices could not be corrected; successful corrections were retained."
            ),
        )
