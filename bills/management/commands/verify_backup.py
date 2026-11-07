from django.core.management.base import BaseCommand, CommandError

from bills.backups import BackupError, verify_backup


class Command(BaseCommand):
    help = "Verify backup checksums, SQLite integrity and recoverable document originals."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("backup")

    def handle(self, *args, **options):
        try:
            manifest = verify_backup(options["backup"])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Backup verified: {len(manifest['files'])} files."))
