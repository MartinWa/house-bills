from django.core.management.base import BaseCommand, CommandError

from bills.backups import BackupError, restore_backup


class Command(BaseCommand):
    help = "Restore a verified operational backup into a new, nonexistent data directory."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("backup")
        parser.add_argument("destination")

    def handle(self, *args, **options):
        try:
            restore_backup(options["backup"], options["destination"])
        except BackupError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                "Restore verified. Review recovered-configuration "
                "before starting this clean instance."
            )
        )
