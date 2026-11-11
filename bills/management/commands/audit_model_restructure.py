"""Read-only migration preflight; output only configuration IDs and schedule policy."""

import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from bills.configuration import proposed_configuration, validate_configuration
from bills.schedule_migration import audit_legacy_configuration


class Command(BaseCommand):
    help = "Inspect configuration for model-restructure migration conflicts without writing data."

    def handle(self, **options):
        with connection.cursor() as cursor:
            columns = {row[1] for row in cursor.execute("PRAGMA table_info(bills_propertyservice)")}
        if "frequency_months" in columns:
            apps = (
                MigrationExecutor(connection)
                .loader.project_state([("bills", "0018_expected_invoice_month_labels")])
                .apps
            )
            services = list(apps.get_model("bills", "PropertyService").objects.values())
            assignments = list(apps.get_model("bills", "ServiceVendorAssignment").objects.values())
            try:
                exceptions = list(
                    apps.get_model("bills", "ServiceInvoiceException").objects.values()
                )
                periods = audit_legacy_configuration(services, assignments, exceptions)
            except ValueError as exc:
                raise CommandError(str(exc)) from exc
            result = {
                "services": len(services),
                "assignments": len(assignments),
                "vendor_schedule_periods": periods,
            }
        else:
            try:
                graph = proposed_configuration()
                validate_configuration(graph)
            except ValidationError as exc:
                raise CommandError(str(exc)) from exc
            result = {model.__name__: len(rows) for model, rows in graph.items()}
        self.stdout.write(json.dumps({"conflicts": 0, **result}, default=str))
