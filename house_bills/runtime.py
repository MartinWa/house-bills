"""Accepted SQLite releases containing the reference's WAL-reset fix."""


def wal_runtime_is_fixed(version):
    return (
        version >= (3, 51, 3)
        or (3, 50, 7) <= version < (3, 51, 0)
        or (3, 44, 6) <= version < (3, 45, 0)
    )
