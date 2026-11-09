"""Disposable Linux development state with a persistent local secret."""

import os
import secrets

from house_bills.version import checkout_version

from .base import *  # noqa: F403
from .base import BASE_DIR, DATA_DIR

if not os.environ.get("HOUSE_BILLS_VERSION", "").strip():
    APP_VERSION = checkout_version(BASE_DIR)

DATA_DIR.mkdir(parents=True, exist_ok=True)
secret_file = DATA_DIR / ".development-secret"
try:
    descriptor = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(secrets.token_urlsafe(64))
SECRET_KEY = secret_file.read_text(encoding="utf-8").strip()
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
