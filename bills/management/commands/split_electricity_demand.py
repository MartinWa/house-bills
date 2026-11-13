"""Preview or apply demand charge corrections from retained invoice evidence."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from bills.electricity_demand import plan_retained_demand_split
from bills.models import Invoice, Property
from bills.services import approve_document_review


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
        actor = None
        if options["apply"]:
            actor = (
                get_user_model()
                .objects.filter(pk=options["reviewer"], is_active=True, is_staff=True)
                .first()
            )
            if actor is None:
                raise CommandError("Applying corrections requires an active staff --reviewer ID.")
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
        changed, untouched, failed = 0, 0, 0
        for invoice in invoices:
            try:
                plan, warnings = plan_retained_demand_split(invoice, property_id)
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
                            "Separated printed Effektavgift charges and billed kW from Elnät. "
                            "VAT apportioned by net cost and balanced to the printed contract VAT. "
                            "Invoice totals, approved corrections and kWh readings retained."
                        ),
                    )
            except ValidationError as exc:
                failed += 1
                self.stderr.write(
                    self.style.ERROR(f"Invoice {invoice.pk}: {'; '.join(exc.messages)}")
                )
                continue
            changed += 1
        action = "Updated" if actor is not None else "Would update"
        self.stdout.write(
            f"{action} {changed} invoices; {untouched} unchanged; {failed} failed. "
            "Invoice totals retained."
        )
        if failed:
            raise CommandError(
                f"{failed} invoices could not be corrected; successful corrections were retained."
            )
