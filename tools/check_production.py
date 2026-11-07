"""Exercise real Waitress startup on disposable storage in the production image."""

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from html.parser import HTMLParser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

import django
from django.conf import settings
from django.core.checks import run_checks

from .check_http import NoRedirect, get, wait_for_server
from .synthetic_pdf import invoice_pdf


class ReviewFields(HTMLParser):
    """Read rendered form values, including signed context, over real HTTP."""

    def __init__(self, page):
        super().__init__()
        self.values = {}
        self.active = False
        self.templates = 0
        self.select = None
        self.textarea = None
        self.feed(page.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.active = attrs.get("id") == "review-form"
        if tag == "template":
            self.templates += 1
        if not self.active or self.templates:
            return
        if tag == "input" and attrs.get("name"):
            if attrs.get("type") != "checkbox" or "checked" in attrs:
                self.values[attrs["name"]] = attrs.get(
                    "value", "on" if attrs.get("type") == "checkbox" else ""
                )
        elif tag == "select":
            self.select = attrs.get("name")
        elif tag == "option" and self.select:
            if self.select not in self.values or "selected" in attrs:
                self.values[self.select] = attrs.get("value", "")
        elif tag == "textarea":
            self.textarea = attrs.get("name")
            if self.textarea:
                self.values[self.textarea] = ""

    def handle_endtag(self, tag):
        if tag == "template":
            self.templates -= 1
        elif tag == "form":
            self.active = False
        elif tag == "select":
            self.select = None
        elif tag == "textarea":
            self.textarea = None

    def handle_data(self, value):
        if self.active and not self.templates and self.textarea:
            self.values[self.textarea] += value


def check_ingestion_http():
    """Exercise the shipped upload/download paths through actual Waitress HTTP."""
    from django.contrib.auth import get_user_model
    from django.templatetags.static import static
    from django.test import Client

    from bills.models import (
        BillingSection,
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

    def request(
        path, data=None, *, content_type="multipart/form-data; boundary=synthetic-boundary"
    ):
        headers = {
            "Host": "ci.invalid",
            "X-Forwarded-Proto": "https",
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies.items()),
        }
        if data is not None:
            headers["Content-Type"] = content_type
            headers["Origin"] = "https://ci.invalid"
        try:
            return build_opener(NoRedirect).open(
                Request(f"http://127.0.0.1:8000{path}", data=data, headers=headers), timeout=5
            )
        except HTTPError as response:
            return response

    def post_review(path, values):
        return request(
            path,
            urlencode(values).encode(),
            content_type="application/x-www-form-urlencoded",
        )

    def review_values(path):
        with request(path) as response:
            assert response.status == 200
            fields = ReviewFields(response.read()).values
            assert "review_token" in fields and "csrfmiddlewaretoken" in fields
            return fields

    for asset in ("bills/app.css", "bills/app.js", "bills/review.js", "bills/favicon.svg"):
        with request(static(asset)) as response:
            assert response.status == 200 and len(response.read()) > 100

    with request("/documents/?upload=1") as response:
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
        "3",
    )
    assert attempt.candidate["issues"] == [] and attempt.candidate["invoice_total_minor"] == 12500
    assert attempt.candidate["routing"]["section_properties"] == {"section_01": house.pk}
    assert [u["quantity_milli"] for u in attempt.candidate["sections"][0]["usages"]] == [100000]
    assert Invoice.objects.get().status == "review"
    with request(location) as response:
        page = response.read()
        assert response.status == 200 and b"Extracted proposal" in page
        assert b"125,00 SEK" in page and b"100,000 kWh" in page
    assert not BillingSection.objects.exists() and not Usage.objects.exists()
    with request(location + "source/") as response:
        assert response.status == 200 and response.read() == original
        assert "no-store" in response.headers["Cache-Control"]
        assert response.headers["Content-Disposition"].startswith("attachment;")
    with get(location + "source/", host="ci.invalid", secure=True) as response:
        assert response.status == 302 and response.headers["Location"].startswith("/login/")
    with request("/private-files/" + document.original_path) as response:
        assert response.status == 404

    review_path = location + "review/"
    values = review_values(review_path)
    with post_review(review_path, values) as response:
        assert response.status == 302
        invoice_location = response.headers["Location"]
    invoice = document.invoices.get()
    assert invoice.status == "approved" and invoice.review_revision == 1
    assert Usage.objects.get().quantity_milli == 100000 and BillingSection.objects.count() == 1
    document.refresh_from_db()
    assert document.storage_state == "approved"
    with request(invoice_location) as response:
        assert response.status == 200 and b"100,000 kWh" in response.read()
    with request("/reports/") as response:
        report = response.read()
        assert response.status == 200 and b"100,000 kWh" in report
        assert b"Electricity supply (fallback)" in report and b"125,00 SEK" in report
        assert "no-store" in response.headers["Cache-Control"]
    corrected = review_values(review_path)
    corrected["usages-0-quantity_milli"] = "101,125"
    corrected["note"] = "Synthetic production consumption correction."
    with post_review(review_path, corrected) as response:
        assert response.status == 302
    with post_review(review_path, corrected) as response:
        assert response.status == 409 and b"Your entries are retained" in response.read()
    invoice.refresh_from_db()
    assert invoice.review_revision == 2 and Usage.objects.get().quantity_milli == 101125
    with request("/reports/") as response:
        report = response.read()
        assert response.status == 200 and b"101,125 kWh" in report and b"100,000 kWh" not in report
    with post_review(
        location + "reparse/",
        {"extraction_revision": 1, "csrfmiddlewaretoken": cookies[settings.CSRF_COOKIE_NAME]},
    ) as response:
        assert response.status == 302
    assert document.extractions.count() == 2 and document.invoices.count() == 1
    assert review_values(review_path)["usages-0-quantity_milli"] == "101,125"
    proposed = review_values(review_path + "?proposal=latest")
    assert proposed["usages-0-quantity_milli"] == "100,000"
    with post_review(review_path, proposed) as response:
        assert response.status == 302
    invoice.refresh_from_db()
    assert invoice.review_revision == 3 and invoice.reviews.count() == 3
    with request(location + "view/") as response:
        assert response.status == 200 and response.read() == original
        assert response.headers["Content-Disposition"].startswith("inline;")
    with request("/upload/", multipart()) as response:
        assert response.status == 302 and response.headers["Location"] == location
    assert document.invoices.count() == 1 and Usage.objects.get().quantity_milli == 100000

    with request("/upload/", multipart(payload=b"synthetic invalid PDF")) as response:
        assert response.status == 400
    with request("/upload/", multipart(payload=b"%PDF-1.7 damaged")) as response:
        assert response.status == 302
        failed_location = response.headers["Location"]
    failed = Document.objects.exclude(pk=document.pk).get()
    assert failed.state == "review" and failed.extractions.get().state == "failed"
    with request(failed_location + "source/") as response:
        assert response.status == 200 and response.read() == b"%PDF-1.7 damaged"
    manual_path = failed_location + "review/"
    manual = review_values(manual_path)
    manual.update(
        {
            "vendor": vendor.pk,
            "reference": "SYNTHETIC-MANUAL-CREDIT-001",
            "kind": "credit",
            "invoice_date": "2024-03-05",
            "net_minor": "-8,00",
            "vat_minor": "-2,00",
            "rounding_minor": "0,00",
            "invoice_total_minor": "-10,00",
            "amount_due_minor": "-10,00",
            "warnings_confirmed": "on",
            "sections-0-property": house.pk,
            "sections-0-service": "water",
            "sections-0-net_minor": "-8,00",
            "sections-0-vat_minor": "-2,00",
            "sections-0-gross_minor": "-10,00",
            "sections-0-start": "2024-02-29",
            "sections-0-end": "2024-02-29",
            "add_row": "usages",
        }
    )
    with post_review(manual_path, manual) as response:
        assert response.status == 200
        manual = ReviewFields(response.read()).values
    manual.update(
        {
            "usages-0-utility": "water",
            "usages-0-unit": "m3",
            "usages-0-quantity_milli": "-1,250",
            "usages-0-start": "2024-02-29",
            "usages-0-end": "2024-02-29",
            "usages-0-basis": "reversal",
        }
    )
    with post_review(manual_path, manual) as response:
        assert response.status == 302
        manual_location = response.headers["Location"]
    assert failed.invoices.get().status == "approved"
    assert Usage.objects.get(section__invoice__document=failed).quantity_milli == -1250
    with request(manual_location) as response:
        assert response.status == 200 and b"-1,250 m" in response.read()
    with request("/reports/") as response:
        report = response.read()
        assert response.status == 200 and b"100,000 kWh" in report and b"-1,250 m" in report
        assert b"-10,00 SEK" in report
    with request("/reports/?date_from=2024-02-15&date_to=2024-02-15") as response:
        report = response.read()
        assert (
            response.status == 200 and b"No approved costs" in report and b"100,000 kWh" in report
        )
    for path in (
        "/reports/",
        "/invoices/",
        invoice_location,
        manual_path,
        failed_location + "view/",
    ):
        with get(path, host="ci.invalid", secure=True) as response:
            assert response.status == 302 and response.headers["Location"].startswith("/login/")
    print(
        "Production upload, manual entry, approval, correction, stale review, "
        "re-parsing, duplicates, reporting, CSRF and private source checks passed."
    )


def run_server(*, migrate, trust_proxy, restored_data=None):
    environment = {
        **os.environ,
        "HOUSE_BILLS_MIGRATE_ON_START": str(migrate).lower(),
        "HOUSE_BILLS_TRUST_PROXY": str(trust_proxy).lower(),
    }
    if restored_data is not None:
        environment["HOUSE_BILLS_DATA_DIR"] = str(restored_data)
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
                if restored_data is None:
                    check_ingestion_http()
                else:
                    subprocess.run(
                        [sys.executable, "-m", "tools.check_recovery"],
                        env=environment,
                        check=True,
                        timeout=30,
                    )
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
        importlib.util.find_spec(name) is None
        for name in ("pip", "pytest", "reportlab", "ruff", "playwright")
    ), "Development dependencies must not be shipped."
    assert not Path("/usr/local/bin/node").exists(), "The browser toolchain must not be shipped."
    assert Path("LICENSE").is_file() and Path("NOTICE").is_file()
    if "EXPECTED_VERSION" in os.environ:
        assert os.environ["HOUSE_BILLS_VERSION"] == os.environ["EXPECTED_VERSION"]
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
    check_backup_restore(database)
    print("Non-root production startup, migration guards, HTTPS/proxy and static checks passed.")


def database_rows(database):
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as snapshot:
        tables = snapshot.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return {
            name: snapshot.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall()
            for (name,) in tables
        }


def check_backup_restore(database):
    from django.core.management import call_command
    from django.db import connection

    from bills.backups import create_backup, restore_backup

    expected = database_rows(database)
    with tempfile.TemporaryDirectory(prefix="house-bills-recovery-") as scratch:
        root = Path(scratch)
        configuration = root / "operator.env"
        configuration.write_text(
            "\n".join(
                f"{key}={os.environ[key]}"
                for key in ("HOUSE_BILLS_SECRET_KEY", "HOUSE_BILLS_ALLOWED_HOSTS")
            )
            + "\n",
            encoding="utf-8",
        )
        configuration.chmod(0o600)
        backup, restored = root / "backup", root / "restored"
        create_backup(backup, [configuration])
        call_command("verify_backup", str(backup))
        restore_backup(backup, restored)
        assert database_rows(backup / "database.sqlite3") == expected
        assert database_rows(restored / "db.sqlite3") == expected
        assert (
            restored / "recovered-configuration/001-operator.env"
        ).read_bytes() == configuration.read_bytes()
        connection.close()
        # The same release image starts this separate instance with migrations disabled.
        run_server(migrate=False, trust_proxy=True, restored_data=restored)
        assert database_rows(database) == expected
    print("Verified database/original backup and clean production HTTP restoration passed.")


if __name__ == "__main__":
    main()
