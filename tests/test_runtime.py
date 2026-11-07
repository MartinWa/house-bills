import sqlite3
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from bills.checks import sqlite_runtime_check
from bills.db.backends.sqlite3.base import DatabaseWrapper
from house_bills import server
from house_bills.runtime import wal_runtime_is_fixed


@pytest.mark.parametrize(
    ("version", "fixed"),
    [
        ((3, 44, 5), False),
        ((3, 44, 6), True),
        ((3, 45, 0), False),
        ((3, 50, 6), False),
        ((3, 50, 7), True),
        ((3, 51, 0), False),
        ((3, 51, 2), False),
        ((3, 51, 3), True),
        ((3, 52, 0), True),
    ],
)
def test_wal_runtime_policy_includes_only_fixed_backports(version, fixed):
    assert wal_runtime_is_fixed(version) is fixed


def test_unsafe_linked_sqlite_is_reported_and_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 2))
    assert [issue.id for issue in sqlite_runtime_check(None)] == ["bills.E001"]
    backend = DatabaseWrapper({"NAME": tmp_path / "unsafe.sqlite3"})
    with pytest.raises(ImproperlyConfigured, match="WAL-reset fix"):
        backend.get_new_connection({"database": str(tmp_path / "unsafe.sqlite3")})
    assert not (tmp_path / "unsafe.sqlite3").exists()


def test_proxy_trust_is_explicit_and_only_accepts_forwarded_scheme(monkeypatch):
    monkeypatch.delenv("HOUSE_BILLS_TRUST_PROXY", raising=False)
    options = server.server_options()
    assert options["threads"] == 2
    assert options["clear_untrusted_proxy_headers"] is True
    assert "trusted_proxy" not in options
    monkeypatch.setenv("HOUSE_BILLS_TRUST_PROXY", "true")
    options = server.server_options()
    assert options["trusted_proxy"] == "*"
    assert options["trusted_proxy_headers"] == {"x-forwarded-proto"}
    monkeypatch.setenv("HOUSE_BILLS_TRUST_PROXY", "invalid")
    with pytest.raises(ImproperlyConfigured):
        server.server_options()


@pytest.mark.django_db(transaction=True)
def test_startup_guard_detects_current_missing_and_pending_schema(monkeypatch, tmp_path):
    from django.db import connection

    server.assert_current_schema()
    connection.close()
    monkeypatch.setitem(connection.settings_dict, "NAME", tmp_path / "fresh.sqlite3")
    with pytest.raises(ImproperlyConfigured, match="does not exist"):
        server.assert_current_schema()
    assert not (tmp_path / "fresh.sqlite3").exists()
    (tmp_path / "fresh.sqlite3").touch()
    try:
        with pytest.raises(ImproperlyConfigured, match="pending migrations"):
            server.assert_current_schema()
    finally:
        connection.close()


def test_startup_migration_lock_timeout_prevents_serving(monkeypatch, tmp_path, settings):
    from django.db import connection
    from filelock import FileLock

    monkeypatch.setitem(connection.settings_dict, "NAME", tmp_path / "locked.sqlite3")
    settings.MIGRATION_LOCK_TIMEOUT = 0
    with FileLock(tmp_path / ".migrate.lock"):
        with pytest.raises(ImproperlyConfigured, match="Timed out"):
            server.migrate_on_start()
    assert not (tmp_path / "locked.sqlite3").exists()


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("HOUSE_BILLS_SECRET_KEY", "short", "random HOUSE_BILLS_SECRET_KEY"),
        ("HOUSE_BILLS_SECRET_KEY", "REPLACE" + "x" * 60, "random HOUSE_BILLS_SECRET_KEY"),
        ("HOUSE_BILLS_ALLOWED_HOSTS", "", "explicit HOUSE_BILLS_ALLOWED_HOSTS"),
        ("HOUSE_BILLS_ALLOWED_HOSTS", "*.invalid", "explicit HOUSE_BILLS_ALLOWED_HOSTS"),
        ("HOUSE_BILLS_ALLOWED_HOSTS", "https://bills.invalid", "without schemes"),
        ("HOUSE_BILLS_DATA_DIR", "", "absolute, private host-local"),
        ("HOUSE_BILLS_DATA_DIR", "relative", "absolute, private host-local"),
        ("HOUSE_BILLS_CSRF_TRUSTED_ORIGINS", "http://bills.invalid", "exact HTTPS"),
        ("HOUSE_BILLS_TRUST_PROXY", "invalid", "must be true or false"),
    ],
)
def test_production_refuses_invalid_installation_configuration(
    monkeypatch, tmp_path, name, value, message
):
    monkeypatch.setenv("HOUSE_BILLS_SECRET_KEY", "synthetic-production-test-key-" * 3)
    monkeypatch.setenv("HOUSE_BILLS_ALLOWED_HOSTS", "bills.invalid")
    monkeypatch.setenv("HOUSE_BILLS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("HOUSE_BILLS_CSRF_TRUSTED_ORIGINS", "https://bills.invalid")
    monkeypatch.setenv("HOUSE_BILLS_TRUST_PROXY", "false")
    monkeypatch.setenv(name, value)
    result = subprocess.run(
        [sys.executable, "-c", "import house_bills.settings.production"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert message in result.stderr
