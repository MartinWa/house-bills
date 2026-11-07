from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from filelock import Timeout

from bills.storage import recover_originals


class Command(BaseCommand):
    help = "Recover completed uploads and interrupted original publication."

    def handle(self, *args, **options):
        try:
            documents = recover_originals()
        except Timeout as exc:
            raise CommandError("Original storage is busy. Retry recovery shortly.") from exc
        except ValidationError as exc:
            raise CommandError(" ".join(exc.messages)) from exc
        except (OSError, DatabaseError) as exc:
            raise CommandError(
                "Recovery could not finish. Check private storage and retry."
            ) from exc
        failed = sum(document.storage_state == "failed" for document in documents)
        self.stdout.write(f"Checked {len(documents)} documents; {failed} storage failures remain.")
        if failed:
            raise CommandError("Check private storage or re-upload missing originals, then retry.")
