"""Initialize disposable development state, then run Django's reloader."""

import os
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "house_bills.settings.development")

import django  # noqa: E402

django.setup()

from house_bills.server import migrate_on_start  # noqa: E402

if __name__ == "__main__":
    migrate_on_start()
    os.execv(sys.executable, [sys.executable, "manage.py", "runserver", "0.0.0.0:8000"])
