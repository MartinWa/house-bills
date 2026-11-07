"""File-backed WAL tests isolated from development and production data."""

from .base import *  # noqa: F403
from .base import DATABASES, STATE_DIR

TEST_DATA_DIR = STATE_DIR / "test-state"
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASES["default"]["NAME"] = TEST_DATA_DIR / "db.sqlite3"
DATABASES["default"]["TEST"] = {"NAME": TEST_DATA_DIR / "test.sqlite3"}
SECRET_KEY = "synthetic-test-secret-not-for-real-installations-" * 2
DEBUG = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
STATIC_ROOT = TEST_DATA_DIR / "staticfiles"
STATIC_ROOT.mkdir(exist_ok=True)
MEDIA_ROOT = TEST_DATA_DIR / "invoices"
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
