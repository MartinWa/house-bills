"""Exercise real Waitress startup on disposable storage in the production image."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import django
from django.conf import settings
from django.core.checks import run_checks

from .check_http import get, wait_for_server


def run_server(*, migrate, trust_proxy):
    environment = {
        **os.environ,
        "HOUSE_BILLS_MIGRATE_ON_START": str(migrate).lower(),
        "HOUSE_BILLS_TRUST_PROXY": str(trust_proxy).lower(),
    }
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "house_bills.server"],
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            wait_for_server(host="ci.invalid", secure=trust_proxy, process=process)
            with get("/", host="ci.invalid", secure=trust_proxy) as response:
                assert response.status == (302 if trust_proxy else 301)
                assert response.headers["Location"].startswith(
                    "/login/" if trust_proxy else "https://ci.invalid/"
                )
            if trust_proxy:
                with get("/login/", host="ci.invalid", secure=True) as response:
                    assert response.status == 200
                    assert b"csrfmiddlewaretoken" in response.read()
                with get("/static/admin/css/base.css", host="ci.invalid", secure=True) as response:
                    assert response.status == 200
                    assert len(response.read()) > 100
            else:
                # An untrusted forwarding header cannot suppress the HTTPS redirect.
                with get("/", host="ci.invalid", secure=True) as response:
                    assert response.status == 301
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            log.seek(0)
            print(log.read().decode(), end="")


def main():
    assert os.geteuid() != 0, "Production must run as non-root."
    assert all(
        not Path(name).exists() for name in ("planning", "test-data", "private", "tests", ".env")
    ), "Research, tests and private state must not be shipped in production."
    assert all(
        importlib.util.find_spec(name) is None for name in ("pip", "pytest", "reportlab", "ruff")
    ), "Development dependencies must not be shipped."
    django.setup()
    assert not settings.DEBUG
    database = Path(settings.DATABASES["default"]["NAME"])
    assert not database.exists(), "Use fresh, disposable storage for this probe."
    environment = {**os.environ, "HOUSE_BILLS_MIGRATE_ON_START": "false"}
    result = subprocess.run(
        [sys.executable, "-m", "house_bills.server"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0 and "Database does not exist" in result.stderr, result
    assert not database.exists()
    run_server(migrate=True, trust_proxy=True)
    assert database.is_file()
    issues = run_checks(include_deployment_checks=True, databases=["default"])
    assert {issue.id for issue in issues} <= {"security.W005", "security.W021"}, issues
    for issue in issues:
        print(issue)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("SELECT COUNT(*) FROM django_migrations").fetchone()[0] > 0
    run_server(migrate=False, trust_proxy=False)
    print("Non-root production startup, migration guards, HTTPS/proxy and static checks passed.")


if __name__ == "__main__":
    main()
