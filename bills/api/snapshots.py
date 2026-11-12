"""A separate DEFERRED, query-only ORM connection for multi-query GET responses."""

from functools import wraps

from django.db import connections


def consistent_reads(view):
    @wraps(view)
    def read(request, *args, **kwargs):
        if request.method != "GET" or request.path.startswith("/api/v1/reports/"):
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
