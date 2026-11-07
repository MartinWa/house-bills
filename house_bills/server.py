"""One non-root Waitress process with serialized startup migrations."""

import os
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from waitress import serve

from .settings.base import env_bool


def server_options():
    options = {
        "listen": "0.0.0.0:8000",
        "threads": 2,
        "connection_limit": 20,
        "clear_untrusted_proxy_headers": True,
        "max_request_body_size": max(
            settings.MAX_UPLOAD_REQUEST_BYTES, settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        ),
    }
    if env_bool("HOUSE_BILLS_TRUST_PROXY"):
        # The operator's private network and header-replacing proxy establish trust.
        options.update(
            trusted_proxy="*",
            trusted_proxy_count=1,
            trusted_proxy_headers={"x-forwarded-proto"},
        )
    return options


def assert_current_schema():
    from django.db import connections
    from django.db.migrations.executor import MigrationExecutor

    connection = connections["default"]
    database = Path(connection.settings_dict["NAME"])
    if not database.is_file():
        raise ImproperlyConfigured("Database does not exist. Run `python manage.py migrate`.")
    executor = MigrationExecutor(connection)
    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
        raise ImproperlyConfigured(
            "Database has pending migrations. Run `python manage.py migrate`."
        )


def migrate_on_start():
    from django.core.management import call_command
    from django.db import connections
    from filelock import FileLock, Timeout

    database = Path(connections["default"].settings_dict["NAME"])
    if not database.parent.is_dir():
        raise ImproperlyConfigured("Create the private data directory before starting the server.")
    try:
        with FileLock(
            database.parent / ".migrate.lock", timeout=settings.MIGRATION_LOCK_TIMEOUT, mode=0o600
        ):
            call_command("migrate", interactive=False, verbosity=1)
            assert_current_schema()
    except Timeout as exc:
        raise ImproperlyConfigured("Timed out waiting for another startup migration.") from exc


def main():
    from .wsgi import application

    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise ImproperlyConfigured("Run the production application as a non-root user.")
    if settings.DEBUG:
        raise ImproperlyConfigured("The production server requires production settings.")
    if settings.MIGRATE_ON_START:
        migrate_on_start()
    else:
        assert_current_schema()
    serve(application, **server_options())


if __name__ == "__main__":
    main()
