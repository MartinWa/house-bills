"""Report installed parsers, plugin load health and unavailable vendor formats."""

from django.core.management.base import BaseCommand, CommandError

from bills.models import VendorLayoutFamily
from bills.parsing import registry


class Command(BaseCommand):
    help = "List installed parsers, plugin load errors and missing vendor formats."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Fail on plugin load errors or vendor formats with no installed parser.",
        )

    def handle(self, *args, **options):
        registry.reset()
        discovery = registry.status()
        for parser in registry.parsers():
            identity = registry.parser_identity(parser)
            label = getattr(parser, "PLUGIN_FILENAME", "") or parser.__name__.rsplit(".", 1)[-1]
            self.stdout.write(
                "\t".join(
                    [
                        "parser",
                        label,
                        identity["family"],
                        identity["version"],
                        identity["revision"],
                        identity["fingerprint"] or "",
                    ]
                )
            )
        for error in discovery.errors:
            self.stdout.write(f"error\t{error.filename or 'directory'}\t{error.code}")
        unavailable = sorted(
            set(VendorLayoutFamily.objects.values_list("family", flat=True))
            - set(registry.layouts())
        )
        for family in unavailable:
            self.stdout.write(
                f"missing\t{family}\tconfigured vendor references an unavailable format"
            )
        if not options["check"]:
            return
        if discovery.errors:
            raise CommandError("Parser plugin directory load errors reported above.")
        if unavailable:
            raise CommandError(
                "A configured vendor references an invoice format with no installed parser."
            )
