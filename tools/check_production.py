"""Exercise real Waitress startup on disposable storage in the production image."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener

import django
from django.conf import settings
from django.core.checks import run_checks

from .check_http import NoRedirect, get, wait_for_server
from .synthetic_pdf import invoice_pdf


def check_ingestion_http():
    """Exercise the shipped upload/download paths through actual Waitress HTTP."""
    from django.contrib.auth import get_user_model
    from django.test import Client

    from bills.models import (
        Charge,
        Document,
        Invoice,
        Property,
        Usage,
        Vendor,
        VendorIssuerIdentifier,
        VendorLayoutFamily,
    )

    house = Property.objects.create(name="Synthetic House", slug="synthetic-house")
    vendor = Vendor.objects.create(
        name="Synthetic Utilities", slug="synthetic-utilities", default_property=house
    )
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000999901")
    VendorLayoutFamily.objects.create(vendor=vendor, family="layout_02")

    user = get_user_model().objects.create_user("synthetic-container-uploader")
    session = Client()
    session.force_login(user)
    cookies = {settings.SESSION_COOKIE_NAME: session.cookies[settings.SESSION_COOKIE_NAME].value}

    def request(path, data=None):
        headers = {
            "Host": "ci.invalid",
            "X-Forwarded-Proto": "https",
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        }
        if data is not None:
            headers["Content-Type"] = "multipart/form-data; boundary=synthetic-boundary"
            headers["Origin"] = "https://ci.invalid"
        try:
            return build_opener(NoRedirect).open(
                Request(f"http://127.0.0.1:8000{path}", data=data, headers=headers), timeout=5
            )
        except HTTPError as response:
            return response

    with request("/upload/") as response:
        assert response.status == 200
        cookie = SimpleCookie(response.headers["Set-Cookie"])
        cookies[settings.CSRF_COOKIE_NAME] = cookie[settings.CSRF_COOKIE_NAME].value
    original = invoice_pdf()

    def multipart(*, csrf=True, payload=original):
        token = (
            (
                "--synthetic-boundary\r\n"
                'Content-Disposition: form-data; name="csrfmiddlewaretoken"\r\n\r\n'
                f"{cookies[settings.CSRF_COOKIE_NAME]}\r\n"
            ).encode()
            if csrf
            else b""
        )
        return (
            token
            + b'--synthetic-boundary\r\nContent-Disposition: form-data; name="pdf"; '
            + b'filename="synthetic.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
            + payload
            + b"\r\n--synthetic-boundary--\r\n"
        )

    with request("/upload/", multipart(csrf=False)) as response:
        assert response.status == 403
    assert not Document.objects.exists()
    with request("/upload/", multipart()) as response:
        assert response.status == 302
        location = response.headers["Location"]
    with request("/upload/", multipart()) as response:
        assert response.status == 302 and response.headers["Location"] == location
    document = Document.objects.get()
    assert document.storage_state == "inbox" and document.state == "review"
    attempt = document.extractions.get()
    assert (attempt.layout_family, attempt.layout_version, attempt.parser_revision) == (
        "layout_02",
        "v1",
        "2",
    )
    assert attempt.candidate["issues"] == [] and attempt.candidate["invoice_total_minor"] == 12500
    assert attempt.candidate["routing"]["section_properties"] == {"section_01": house.pk}
    assert [u["quantity_milli"] for u in attempt.candidate["sections"][0]["usages"]] == [100000]
    assert Invoice.objects.get().status == "review"
    with request(location) as response:
        page = response.read()
        assert response.status == 200 and b"Extracted proposal" in page
        assert b"125,00 SEK" in page and b"100,000 kWh" in page
    assert not Charge.objects.exists() and not Usage.objects.exists()
    with request(location + "source/") as response:
        assert response.status == 200 and response.read() == original
        assert "no-store" in response.headers["Cache-Control"]
        assert response.headers["Content-Disposition"].startswith("attachment;")
    with get(location + "source/", host="ci.invalid", secure=True) as response:
        assert response.status == 302 and response.headers["Location"].startswith("/login/")
    with request("/private-files/" + document.original_path) as response:
        assert response.status == 404
    with request("/upload/", multipart(payload=b"synthetic invalid PDF")) as response:
        assert response.status == 400
    with request("/upload/", multipart(payload=b"%PDF-1.7 damaged")) as response:
        assert response.status == 302
        failed_location = response.headers["Location"]
    failed = Document.objects.exclude(pk=document.pk).get()
    assert failed.state == "review" and failed.extractions.get().state == "failed"
    with request(failed_location + "source/") as response:
        assert response.status == 200 and response.read() == b"%PDF-1.7 damaged"
    print(
        "Production parsing, review proposals, duplicates, CSRF and private source checks passed."
    )


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
                check_ingestion_http()
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
