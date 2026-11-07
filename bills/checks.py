import sqlite3

from django.core.checks import Error, Tags, register

from house_bills.runtime import wal_runtime_is_fixed


@register(Tags.database)
def sqlite_runtime_check(app_configs, **kwargs):
    if wal_runtime_is_fixed(sqlite3.sqlite_version_info):
        return []
    return [
        Error(
            "The linked SQLite runtime lacks the WAL-reset fix.",
            hint="Use the Linux image's tested SQLite runtime; see docs/deployment.md.",
            id="bills.E001",
        )
    ]
