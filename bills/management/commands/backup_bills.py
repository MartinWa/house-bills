from django.core.management.base import BaseCommand, CommandError
from filelock import Timeout

from bills.backups import BackupError, create_backup


class Command(BaseCommand):
    help = "Create a verified private backup of the database, originals and recovery configuration."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("destination")
        parser.add_argument("--config", action="append", required=True, dest="configuration")

    def handle(self, *args, **options):
        try:
            manifest = create_backup(options["destination"], options["configuration"])
        except (BackupError, Timeout) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(f"Verified backup created: {len(manifest['files'])} files.")
        )
