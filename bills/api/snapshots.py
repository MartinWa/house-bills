"""A separate DEFERRED, query-only ORM connection for multi-query GET responses."""

from functools import wraps

from django.db import connections


def consistent_reads(view):
    @wraps(view)
    def read(request, *args, **kwargs):
        # File downloads must read the current path after acquiring the storage lock.
        source_download = request.path.startswith("/api/v1/documents/") and request.path.endswith(
            "/source"
        )
        if (
            request.method != "GET"
            or request.path.startswith("/api/v1/reports/")
            or source_download
        ):
            return view(request, *args, **kwargs)
        original = connections["default"]
        reader = original.copy(alias="default")
        reader.settings_dict = {
            **reader.settings_dict,
            "OPTIONS": {
                **reader.settings_dict["OPTIONS"],
                "transaction_mode": "DEFERRED",
            },
        }
        try:
            with reader.cursor() as cursor:
                cursor.execute("PRAGMA query_only=ON")
                cursor.execute("BEGIN DEFERRED")
            connections["default"] = reader
            return view(request, *args, **kwargs)
        finally:
            connections["default"] = original
            reader.close()

    return read
