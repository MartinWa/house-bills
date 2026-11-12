"""HTTP contract checks use invented records and disposable original storage."""

import copy
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError, connections
from django.test import Client, RequestFactory
from django.utils import timezone

from bills.api.auth import REPORTING_PATHS
from bills.api.middleware import JSON_LIMIT, ApiContractMiddleware
from bills.api.schemas import ReviewEditsIn
from bills.api.snapshots import consistent_reads
from bills.models import (
    ApiToken,
    BillingSection,
    Document,
    Invoice,
    InvoicePropertyRule,
    Property,
    PropertyService,
    ReviewRevision,
    ServiceInvoiceException,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
)
from bills.review_edits import ReviewEdits, build_review
from bills.review_forms import ReviewForms
from bills.reviewing import current_review_context
from bills.storage import private_path, publish_original

from .synthetic import shared_supply, write_pdf
from .test_reporting import allocation, ledger, usage  # noqa: F401
from .test_review import posted_fields

pytestmark = pytest.mark.django_db(transaction=True)
BASE = "/api/v1"


def test_consistent_reads_are_query_only_and_leave_the_writer_available(api_client):
    client, _, _, _ = api_client
    setup(client)
    original = connections["default"]
    writer = original.copy(alias="independent_writer")

    @consistent_reads
    def read(request):
        reader = connections["default"]
        assert reader is not original
        assert Property.objects.count() == 1
        with reader.cursor() as cursor:
            assert cursor.execute("PRAGMA query_only").fetchone()[0] == 1
        # Acquiring a reserved writer lock succeeds while this GET snapshot is open.
        with writer.cursor() as cursor:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("ROLLBACK")
        return "read"

    try:
        assert read(RequestFactory().get(BASE + "/properties")) == "read"
        assert connections["default"] is original
        with original.cursor() as cursor:
            assert cursor.execute("PRAGMA query_only").fetchone()[0] == 0
    finally:
        writer.close()


def test_sql_programming_errors_are_not_reported_as_transient_database_failures(
    api_client, monkeypatch
):
    import bills.api.reports as reports

    client, _, _, _ = api_client

    def invalid_sql(**kwargs):
        with sqlite3.connect(":memory:") as database:
            database.execute("SELECT missing_column FROM absent_table")

    monkeypatch.setattr(reports, "read_api_report", invalid_sql)
    client.raise_request_exception = False
    result = client.get(BASE + "/reports/summary")
    assert result.status_code == 500
    assert result.json()["error"]["code"] == "internal_error"
    assert b"missing_column" not in result.content and b"absent_table" not in result.content
    assert result["Cache-Control"] == "no-store" and "Retry-After" not in result


def test_largest_unicode_review_fits_body_limit_and_preserves_all_rows(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document = send(client, "post", "/manual-invoices", {}).json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["header"]["reference"] = "\U0001f30a" * 120
    edits["header"].update(
        net_minor=200000, vat_minor=50000, invoice_total_minor=250000, amount_due_minor=250000
    )
    sections = []
    for index in range(20):
        section = copy.deepcopy(edits["sections"][0])
        section["section_id"] = "\U0001f30a" * 118 + f"{index:02}"
        sections.append(section)
    edits["sections"] = sections
    edits["usages"] = [
        {**edits["usages"][0], "section_id": sections[index % 20]["section_id"]}
        for index in range(60)
    ]
    edits["identifiers"] = [
        {
            "scope": sections[index % 20]["section_id"],
            "kind": "delivery_address",
            "value": "\U0001f30a" * 200,
        }
        for index in range(60)
    ]
    edits["note"] = "\U0001f30a" * 2000
    payload = {"review_token": source["review_token"], "edits": edits}
    assert len(json.dumps(payload).encode()) <= JSON_LIMIT
    result = send(client, "post", f"/documents/{document['id']}/reviews", payload)
    assert result.status_code == 201, result.content
    assert BillingSection.objects.count() == 20 and Usage.objects.count() == 60
    assert result.json()["note"] == edits["note"]


def test_review_retains_late_source_evidence_when_large_extraction_is_trimmed(api_client):
    from dataclasses import replace

    from bills.parsing.types import Evidence, Identifier
    from bills.services import record_extraction

    client, _, _, _ = api_client
    house, vendor = setup(client)
    candidate, properties = build_review(
        ReviewEdits(**ReviewEditsIn.model_validate(manual_body(house, vendor)).model_dump()),
        values={},
    )
    candidate.layout_family, candidate.layout_version, candidate.parser_revision = (
        "layout_01",
        "v2",
        "synthetic",
    )
    source_count = 64
    original_usage = candidate.sections[0].usages[0]
    candidate.sections[0].usages = [
        replace(original_usage, evidence=Evidence(1, f"Observed usage {index}", (1, 2, 3, 4)))
        for index in range(source_count)
    ]
    candidate.identifiers = [
        Identifier(
            "contract",
            f"CONTRACT-{index}",
            Evidence(1, f"Observed identifier {index}", (2, 3, 4, 5)),
        )
        for index in range(source_count)
    ]
    document = Document.objects.create(
        original_filename="synthetic.pdf", sha256="a" * 64, state=Document.State.REVIEW
    )
    attempt = record_extraction(
        document.pk,
        candidate,
        vendor=Vendor.objects.get(pk=vendor["id"]),
        routing={
            "vendor_id": vendor["id"],
            "section_properties": {key: value.pk for key, value in properties.items()},
        },
    )
    source = client.get(BASE + f"/documents/{document.pk}/review").json()
    edits = source["edits"]
    edits["usages"] = [edits["usages"][63], edits["usages"][60]]
    edits["identifiers"] = [edits["identifiers"][62], edits["identifiers"][60]]
    for group in ("usages", "identifiers"):
        invalid = copy.deepcopy(edits)
        invalid[group][0]["source_index"] = source_count
        result = send(
            client,
            "post",
            f"/documents/{document.pk}/reviews",
            {"review_token": source["review_token"], "edits": invalid},
        )
        assert result.status_code == 422, result.content
        assert not ReviewRevision.objects.exists()
    result = send(
        client,
        "post",
        f"/documents/{document.pk}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 201, result.content
    values = result.json()["snapshot"]["values"]
    assert [row["evidence"] for row in values["sections"][0]["usages"]] == [
        attempt.candidate["sections"][0]["usages"][index]["evidence"] for index in (63, 60)
    ]
    assert [row["evidence"] for row in values["identifiers"]] == [
        attempt.candidate["identifiers"][index]["evidence"] for index in (62, 60)
    ]
    assert Usage.objects.count() == 2
    original = copy.deepcopy(attempt.candidate)
    attempt.refresh_from_db()
    assert attempt.candidate == original


@pytest.mark.parametrize("indexes", [[0, 0], [59], [-1], [True], ["0"], [0.0]])
def test_invalid_review_source_indexes_do_not_change_approved_rows(api_client, indexes):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, _ = approve_manual(client, house, vendor)
    source = client.get(document["links"]["review"]).json()
    edits = source["edits"]
    edits["usages"] = [{**edits["usages"][0], "source_index": value} for value in indexes]
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 422, result.content
    assert ReviewRevision.objects.count() == 1 and Usage.objects.count() == 1


def test_committed_deletion_returns_pending_cleanup_and_recovery_finishes(api_client, monkeypatch):
    import bills.deleting as deleting

    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, _ = approve_manual(client, house, vendor)
    token = client.get(document["links"]["self"]).json()["deletion_token"]

    def interrupted(path):
        raise OSError("Synthetic interrupted cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(deleting, "_finish_deletion", interrupted)
        result = client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token)
    assert result.status_code == 200 and result.json()["cleanup_pending"] is True
    assert not Document.objects.exists() and not Invoice.objects.exists()
    deleting.recover_document_deletions()
    assert client.get(document["links"]["self"]).status_code == 404
    assert client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token).status_code == 404


@pytest.fixture
def api_client(django_user_model, settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "originals"
    owner = django_user_model.objects.create_superuser("api-owner", password="synthetic-secret")
    token, secret = ApiToken.issue(name="synthetic automation", owner=owner, profile="automation")
    client = Client(HTTP_AUTHORIZATION=f"Bearer {secret}")
    return client, owner, token, secret


def send(client, method, path, body=None):
    return getattr(client, method)(
        BASE + path,
        data=json.dumps(body) if body is not None else "",
        content_type="application/json",
    )


def created(client, path, body):
    response = send(client, "post", path, body)
    assert response.status_code == 201, response.content
    assert response["Location"] == response.json()["links"]["self"]
    assert client.get(response["Location"]).status_code == 200
    return response.json()


def setup(client):
    house = created(client, "/properties", {"name": "Example house", "slug": "example-house"})
    vendor = created(
        client,
        "/vendors",
        {"name": "Example utility", "slug": "example-utility", "default_property_id": house["id"]},
    )
    return house, vendor


def manual_body(house, vendor):
    return {
        "header": {
            "vendor_id": vendor["id"],
            "reference": "EXAMPLE-1",
            "kind": "invoice",
            "invoice_date": "2026-01-31",
            "net_minor": 10000,
            "vat_minor": 2500,
            "rounding_minor": 0,
            "invoice_total_minor": 12500,
            "amount_due_minor": 12500,
        },
        "sections": [
            {
                "section_id": "water",
                "property_id": house["id"],
                "category": "water",
                "net_minor": 10000,
                "vat_minor": 2500,
                "gross_minor": 12500,
                "period_start": "2026-01-01",
                "period_end": "2026-02-01",
            }
        ],
        "usages": [
            {
                "section_id": "water",
                "utility": "water",
                "unit": "m3",
                "quantity_milli": 12345,
                "basis": "billed",
                "period_start": "2026-01-01",
                "period_end": "2026-02-01",
            }
        ],
    }


def approve_manual(client, house, vendor):
    response = send(client, "post", "/manual-invoices", {"idempotency_key": "synthetic-paper-1"})
    assert response.status_code == 201, response.content
    document = response.json()["document"]
    source = client.get(document["links"]["review"]).json()
    response = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": manual_body(house, vendor)},
    )
    assert response.status_code == 201, response.content
    return document, response.json()


def test_token_authentication_profiles_schema_and_session_boundary(api_client):
    client, owner, token, secret = api_client
    assert token.digest == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in str(token.__dict__)
    assert client.get(BASE + "/me").json()["account_id"] == owner.pk
    assert client.get(BASE + "/openapi.json").status_code == 200
    assert client.get(BASE + "/docs").status_code == 302
    cookie = Client()
    cookie.force_login(owner)
    assert cookie.get(BASE + "/openapi.json").status_code == 200
    assert cookie.get(BASE + "/docs").status_code == 200
    for response in [Client().get(BASE + "/me"), cookie.get(BASE + "/me")]:
        assert response.status_code == 401
        assert response["WWW-Authenticate"] == "Bearer"
        assert response["Cache-Control"] == "no-store"
    for header in ("Bearer", "Bearer too many words", "Basic hidden", "bearer " + secret + " "):
        assert Client(HTTP_AUTHORIZATION=header).get(BASE + "/me").status_code == 401
    _, report_secret = ApiToken.issue(name="reporting", owner=owner, profile="reporting")
    reports = Client(HTTP_AUTHORIZATION=f"Bearer {report_secret}")
    assert reports.get(BASE + "/openapi.json").status_code == 200
    for route in REPORTING_PATHS:
        assert reports.get(BASE + route).status_code == 200, route
    schema = client.get(BASE + "/openapi.json").json()
    for path, operations in schema["paths"].items():
        relative = path.removeprefix(BASE)
        for method in operations:
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if method == "get" and relative in REPORTING_PATHS:
                continue
            concrete = (
                path.replace("{owner_id}", "1")
                .replace("{vendor_id}", "1")
                .replace("{key}", "1")
                .replace("{revision}", "1")
            )
            response = reports.generic(
                method.upper(), concrete, data="{}", content_type="application/json"
            )
            assert response.status_code == 403, (path, method, response.content)
    token.delete()
    assert client.get(BASE + "/me").status_code == 401


def test_expired_inactive_and_immutable_tokens(api_client):
    client, owner, token, _ = api_client
    with pytest.raises(ValidationError):
        token.save()
    _, secret = ApiToken.issue(
        name="expired",
        owner=owner,
        profile="automation",
        expires_at=timezone.now() - timedelta(seconds=1),
    )
    assert Client(HTTP_AUTHORIZATION=f"Bearer {secret}").get(BASE + "/me").status_code == 401
    owner.is_active = False
    owner.save()
    assert client.get(BASE + "/me").status_code == 401


@pytest.mark.parametrize(
    ("collection", "model", "kind", "maximum", "normalized_pair"),
    [
        ("identifiers", VendorIssuerIdentifier, "vat", 160, "SS"),
        ("property-rules", InvoicePropertyRule, "delivery_address", 200, "ss"),
    ],
)
def test_configuration_validates_normalized_identifier_length(
    api_client, collection, model, kind, maximum, normalized_pair
):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    path = f"/vendors/{vendor['id']}/{collection}"
    body = {"kind": kind, "value": "ß" * (maximum // 2)}
    if collection == "property-rules":
        body["property_id"] = house["id"]
    resource = created(client, path, body)
    expected = normalized_pair * (maximum // 2)
    assert resource["value"] == expected
    before = LogEntry.objects.count()
    oversized = "ß" * maximum
    for method, target, payload in (
        ("post", path, {**body, "value": oversized}),
        ("patch", resource["links"]["self"].removeprefix(BASE), {"value": oversized}),
    ):
        for suffix in ("", "?dry_run=true"):
            response = send(client, method, target + suffix, payload)
            assert response.status_code == 422, response.content
            error = response.json()["error"]
            assert error["code"] == "validation_error"
            assert any(
                detail["path"] == "value" and detail["code"] == "max_length"
                for detail in error["details"]
            )
            assert model.objects.count() == 1
            assert model.objects.get(pk=resource["id"]).value == expected
            assert LogEntry.objects.count() == before
    assert client.get(resource["links"]["self"]).json()["value"] == expected
    page = client.get(BASE + path)
    assert page.status_code == 200, page.content
    assert page.json()["count"] == 1 and page.json()["items"][0]["value"] == expected


@pytest.mark.parametrize("kind", ["installation", "delivery_address"])
def test_property_rule_kind_change_revalidates_normalized_length(api_client, kind):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    resource = created(
        client,
        f"/vendors/{vendor['id']}/property-rules",
        {"kind": "contract", "value": "ß" * 200, "property_id": house["id"]},
    )
    before = LogEntry.objects.count()
    for suffix in ("", "?dry_run=true"):
        response = send(
            client, "patch", resource["links"]["self"].removeprefix(BASE) + suffix, {"kind": kind}
        )
        assert response.status_code == 422, response.content
        assert LogEntry.objects.count() == before
        assert client.get(resource["links"]["self"]).json() == resource


def test_fresh_configuration_patch_dry_run_filters_and_replacements(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    identifier = created(
        client, f"/vendors/{vendor['id']}/identifiers", {"kind": "vat", "value": "SE 000-000999901"}
    )
    assert identifier["value"] == "SE000000999901"
    assert identifier["links"]["owner"] == vendor["links"]["self"]
    created(
        client,
        f"/vendors/{vendor['id']}/property-rules",
        {"kind": "contract", "value": " 9901 ", "property_id": house["id"]},
    )
    format_url = f"/vendors/{vendor['id']}/invoice-formats/layout_01"
    assert send(client, "put", format_url).status_code == 201
    assert send(client, "put", format_url).status_code == 200
    assert send(client, "delete", format_url).status_code == 204
    assert send(client, "delete", format_url).status_code == 204
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {"category": "water", "start_month": "2026-01-01"},
    )
    assignment = created(
        client,
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
    )
    schedule = created(
        client,
        f"/vendors/{vendor['id']}/billing-schedules",
        {"start_month": "2026-01-01", "first_expected_month": "2026-01-01"},
    )
    assert (
        client.get(BASE + f"/vendors/{vendor['id']}/vendor-assignments").json()["items"][0]["id"]
        == assignment["id"]
    )
    other = created(client, "/vendors", {"name": "Replacement vendor", "slug": "replacement"})
    replacement = created(
        client,
        f"/vendor-assignments/{assignment['id']}/replacement",
        {"vendor_id": other["id"], "start_month": "2027-01-01", "expected_end_month": None},
    )
    assert client.get(assignment["links"]["self"]).json()["end_month"] == "2026-12-01"
    assert replacement["links"]["predecessor"] == assignment["links"]["self"]
    assert (
        client.get(replacement["links"]["self"]).json()["links"]["predecessor"]
        == (assignment["links"]["self"])
    )
    assert (
        client.get(assignment["links"]["self"]).json()["links"]["replacement"]
        == (replacement["links"]["self"])
    )
    assert assignment["category_name"] == "Water"
    before = LogEntry.objects.count()
    retry = send(
        client,
        "post",
        f"/vendor-assignments/{assignment['id']}/replacement",
        {"vendor_id": other["id"], "start_month": "2027-01-01", "expected_end_month": None},
    )
    assert retry.status_code == 409
    assert LogEntry.objects.count() == before
    schedule_replacement = created(
        client,
        f"/billing-schedules/{schedule['id']}/replacement",
        {
            "start_month": "2027-01-01",
            "first_expected_month": "2027-03-01",
            "frequency_months": 3,
            "grace_days": 4,
            "expected_end_month": None,
        },
    )
    assert (
        client.get(schedule["links"]["self"]).json()["links"]["replacement"]
        == (schedule_replacement["links"]["self"])
    )
    assert (
        client.get(schedule_replacement["links"]["self"]).json()["links"]["predecessor"]
        == (schedule["links"]["self"])
    )
    later = created(
        client,
        f"/billing-schedules/{schedule_replacement['id']}/replacement",
        {
            "start_month": "2028-01-01",
            "first_expected_month": "2028-02-01",
            "frequency_months": 2,
            "grace_days": 4,
            "expected_end_month": None,
        },
    )
    middle = client.get(schedule_replacement["links"]["self"]).json()
    assert middle["links"]["predecessor"] == schedule["links"]["self"]
    assert middle["links"]["replacement"] == later["links"]["self"]
    before = LogEntry.objects.count()
    preview = send(client, "patch", f"/properties/{house['id']}?dry_run=true", {"name": "Preview"})
    assert preview.status_code == 200 and preview.json()["dry_run"]
    assert Property.objects.get(pk=house["id"]).name == "Example house"
    assert LogEntry.objects.count() == before
    assert (
        send(client, "patch", f"/vendors/{vendor['id']}", {"default_property_id": None}).status_code
        == 200
    )
    assert send(client, "patch", f"/properties/{house['id']}", {"name": None}).status_code == 422
    assert (
        send(
            client, "patch", f"/services/{service['id']}", {"property_id": house["id"]}
        ).status_code
        == 422
    )
    assert (
        send(
            client,
            "post",
            f"/properties/{house['id']}/services",
            {"category": "waste", "start_month": "2026-01-02"},
        ).status_code
        == 422
    )
    assert (
        send(
            client,
            "post",
            f"/services/{service['id']}/vendor-assignments",
            {"vendor_id": other["id"], "start_month": "2027-01-01"},
        ).status_code
        == 409
    )
    for query in ("unknown=1", "limit=201", "offset=-1", "sort=secret", "name=a&name=b"):
        response = client.get(BASE + "/properties?" + query)
        assert (
            response.status_code == 422 and response.json()["error"]["code"] == "invalid_parameter"
        )
    for index in range(4):
        created(client, "/properties", {"name": f"Example {index}", "slug": f"example-{index}"})
    first = client.get(BASE + "/properties?name=Example&limit=2&sort=-name").json()
    assert first["count"] == 5
    second = client.get(first["next"]).json()
    last = client.get(second["next"]).json()
    assert last["next"] is None
    assert "name=Example" in second["previous"] and "sort=-name" in second["previous"]


def test_cascade_permissions_and_protected_references(api_client, django_user_model):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {"category": "water", "start_month": "2026-01-01"},
    )
    created(
        client,
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
    )
    staff = django_user_model.objects.create_user("limited", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="delete_propertyservice"))
    _, secret = ApiToken.issue(name="limited", owner=staff, profile="automation")
    limited = Client(HTTP_AUTHORIZATION=f"Bearer {secret}")
    assert send(limited, "delete", f"/services/{service['id']}").status_code == 403
    assert PropertyService.objects.filter(pk=service["id"]).exists()
    staff.user_permissions.add(Permission.objects.get(codename="delete_servicevendorassignment"))
    assert send(limited, "delete", f"/services/{service['id']}?dry_run=true").status_code == 200
    assert PropertyService.objects.filter(pk=service["id"]).exists()
    assert send(limited, "delete", f"/services/{service['id']}").status_code == 204
    assert send(client, "delete", f"/properties/{house['id']}").status_code == 409


def test_manual_approval_correction_history_reporting_and_deletion(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    document, review = approve_manual(client, house, vendor)
    assert client.get(review["links"]["self"]).json()["snapshot"] == review["snapshot"]
    assert send(client, "patch", review["links"]["self"].removeprefix(BASE), {}).status_code == 405
    method = send(client, "delete", review["links"]["self"].removeprefix(BASE))
    assert method.status_code == 405 and "GET" in method["Allow"]
    duplicate = send(client, "post", "/manual-invoices", {"idempotency_key": "synthetic-paper-1"})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    assert Document.objects.count() == 1
    source = client.get(document["links"]["review"]).json()
    source["edits"]["header"]["reference"] = "CORRECTED-1"
    payload = {"review_token": source["review_token"], "edits": source["edits"]}
    correction = send(client, "post", f"/documents/{document['id']}/reviews", payload)
    assert correction.status_code == 201, correction.content
    assert correction.json()["revision"] == 2
    assert send(client, "post", f"/documents/{document['id']}/reviews", payload).status_code == 409
    assert ReviewRevision.objects.count() == 2
    summary = client.get(BASE + "/reports/summary").json()
    assert summary["costs"][0]["gross_minor"] == 12500
    assert summary["consumption"][0]["quantity_milli"] == 12345
    assert summary["manual_invoices"] == 1
    series = client.get(BASE + "/reports/costs?mode=invoice_month").json()
    assert series["group_count"] == 1 and "next" not in series
    assert series["groups"][0]["invoice_month_time"] == 1767225600
    usage = client.get(BASE + "/reports/consumption?mode=billed_period").json()
    assert usage["groups"][0]["period_end"] == "2026-02-01"
    raw = client.get(BASE + "/reports/costs?limit=1").json()
    assert raw["count"] == 1 and raw["next"] is None
    for query in ("mode=invalid", "mode=totals&limit=1", "date_from=1900-01-01&date_to=2026-01-01"):
        assert client.get(BASE + "/reports/costs?" + query).status_code == 422
    assert send(client, "delete", f"/documents/{document['id']}").status_code == 400
    token = client.get(document["links"]["self"]).json()["deletion_token"]
    result = client.delete(document["links"]["self"], HTTP_DELETION_TOKEN=token)
    assert result.status_code == 200 and result.json() == {
        "document_id": document["id"],
        "deleted": True,
        "cleanup_pending": False,
    }
    assert client.get(document["links"]["self"]).status_code == 404


@pytest.mark.parametrize("value", [True, "10000", 10000.0, 2**63])
def test_scaled_json_integers_are_strict_and_bad_reviews_roll_back(api_client, value):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    response = send(client, "post", "/manual-invoices", {})
    document = response.json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["header"]["net_minor"] = value
    response = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert response.status_code == 422
    assert ReviewRevision.objects.count() == 0


def test_media_json_limits_errors_and_database_contention(api_client, monkeypatch):
    client, _, _, _ = api_client
    assert (
        client.post(BASE + "/properties", data="{}", content_type="text/plain").status_code == 415
    )
    assert (
        client.post(
            BASE + "/properties", data="{broken", content_type="application/json"
        ).status_code
        == 400
    )
    assert (
        client.post(
            BASE + "/properties", data=" " * (JSON_LIMIT + 1), content_type="application/json"
        ).status_code
        == 413
    )
    factory = RequestFactory()
    for length in ("", "1"):
        request = factory.post(
            BASE + "/properties", data="x" * (JSON_LIMIT + 1), content_type="application/json"
        )
        request.META["CONTENT_LENGTH"] = length
        request.META["HTTP_AUTHORIZATION"] = client.defaults["HTTP_AUTHORIZATION"]
        response = ApiContractMiddleware(lambda request: None).process_view(request, None, [], {})
        assert response.status_code == 413

    def locked(*args, **kwargs):
        raise OperationalError("database is locked")

    monkeypatch.setattr(ApiToken.objects, "select_related", locked)
    response = client.get(BASE + "/me")
    assert response.status_code == 503 and response["Retry-After"] == "1"


def test_schema_artifact_matches_live_contract(api_client):
    client, _, _, _ = api_client
    schema = client.get(BASE + "/openapi.json").json()
    assert schema == json.loads(Path("docs/api/openapi.json").read_text())


@pytest.mark.parametrize("sort", ["--id", "--invoice_date", "id,--invoice_date"])
def test_invoice_sort_rejects_repeated_minus_signs(api_client, sort):
    client, _, _, _ = api_client
    client.raise_request_exception = False
    response = client.get(BASE + "/invoices", {"sort": sort})
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "invalid_parameter"
    assert response.json()["error"]["details"][0]["path"] == "sort"


def test_original_download_uses_current_path_after_concurrent_publication(
    api_client, tmp_path, monkeypatch
):
    import bills.api.workflow as workflow
    import bills.services as services

    client, _, _, _ = api_client
    house, vendor = setup(client)
    pdf = write_pdf(tmp_path / "synthetic.pdf", [["Wholly synthetic original"]]).read_bytes()
    uploaded = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("synthetic.pdf", pdf)})
    assert uploaded.status_code == 201, uploaded.content
    document = uploaded.json()["document"]
    source = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["warnings_confirmed"] = True
    # Keep approval committed while original publication is still pending.
    with monkeypatch.context() as patch:
        patch.setattr(services, "publish_original", lambda key: None)
        approved = send(
            client,
            "post",
            f"/documents/{document['id']}/reviews",
            {"review_token": source["review_token"], "edits": edits},
        )
    assert approved.status_code == 201, approved.content
    old_path = Document.objects.get(pk=document["id"]).original_path
    assert old_path.startswith("inbox/")
    open_current_original = workflow.open_original

    def publish_from_other_connection():
        try:
            published = publish_original(document["id"])
            assert published.storage_state == Document.StorageState.APPROVED
            assert published.original_path != old_path
            assert not private_path(old_path).exists()
            assert private_path(published.original_path).read_bytes() == pdf
        finally:
            connections.close_all()

    def open_after_publication(key):
        # Publish after the endpoint's first document read, before it opens the file.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(publish_from_other_connection).result(timeout=10)
        return open_current_original(key)

    monkeypatch.setattr(workflow, "open_original", open_after_publication)
    response = client.get(document["links"]["source"])
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "private, no-store"
    assert b"".join(response.streaming_content) == pdf


def test_upload_review_evidence_equivalence_reordering_and_reparse(api_client, tmp_path):
    client, owner, _, _ = api_client
    house, vendor = setup(client)
    other = created(client, "/properties", {"name": "Other house", "slug": "other-house"})
    created(
        client, f"/vendors/{vendor['id']}/identifiers", {"kind": "vat", "value": "SE000000999901"}
    )
    send(client, "put", f"/vendors/{vendor['id']}/invoice-formats/layout_02")
    for index, property_id in ((1, house["id"]), (2, other["id"])):
        created(
            client,
            f"/vendors/{vendor['id']}/property-rules",
            {"property_id": property_id, "kind": "installation", "value": f"{index:018}"},
        )
    pages = copy.deepcopy(shared_supply())
    pages[0].append("VAT-nr SE000000999901")
    pdf = write_pdf(tmp_path / "synthetic.pdf", pages).read_bytes()
    response = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("synthetic.pdf", pdf)})
    assert response.status_code == 201, response.content
    document = response.json()["document"]
    duplicate = client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("renamed.pdf", pdf)})
    assert duplicate.status_code == 200 and duplicate.json()["duplicate"]
    source = client.get(document["links"]["review"]).json()
    edits = copy.deepcopy(source["edits"])
    edits["warnings_confirmed"] = True
    context = current_review_context(Document.objects.get(pk=document["id"]))
    origin = context.source()
    client.force_login(owner)
    html = client.get(f"/documents/{document['id']}/review/")
    forms = ReviewForms(
        **{key: origin[key] for key in ("values", "properties", "vendor_id", "kind", "warnings")},
        token=context.token(),
        data=posted_fields(html),
    )
    assert forms.is_valid(), forms.header.errors
    html_candidate, html_properties = forms.candidate()
    candidate, properties = build_review(
        ReviewEdits(**ReviewEditsIn.model_validate(edits).model_dump()),
        values=origin["values"],
        warnings=origin["warnings"],
    )
    assert candidate.to_dict() == html_candidate.to_dict()
    assert properties == html_properties
    edits["usages"].reverse()
    edits["identifiers"].reverse()
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": source["review_token"], "edits": edits},
    )
    assert result.status_code == 201, result.content
    for section in result.json()["snapshot"]["values"]["sections"]:
        original_section = next(
            item
            for item in candidate.to_dict()["sections"]
            if item["section_id"] == section["section_id"]
        )
        assert section["usages"] == original_section["usages"]
    original = client.get(document["links"]["source"])
    assert original["Cache-Control"] == "private, no-store"
    assert b"".join(original.streaming_content) == pdf
    reparse = send(
        client,
        "post",
        f"/documents/{document['id']}/reparse",
        {"extraction_revision": document["extraction_revision"]},
    )
    assert reparse.status_code == 200
    assert ReviewRevision.objects.count() == 1
    assert (
        send(
            client,
            "post",
            f"/documents/{document['id']}/reparse",
            {"extraction_revision": document["extraction_revision"]},
        ).status_code
        == 409
    )
    stale_delete = client.delete(
        document["links"]["self"], HTTP_DELETION_TOKEN=document["deletion_token"]
    )
    assert stale_delete.status_code == 409


def test_upload_is_bounded_and_authorized_before_parsing(api_client, settings, monkeypatch):
    client, _, _, _ = api_client
    settings.MAX_PDF_UPLOAD_BYTES = 20
    response = client.post(
        BASE + "/documents", {"pdf": SimpleUploadedFile("large.pdf", b"%PDF-" + b"x" * 30)}
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "body_too_large"
    assert Document.objects.count() == 0
    from bills.uploads import BoundedPdfUploadHandler

    def parsed(*args, **kwargs):
        raise AssertionError("Unauthorized uploads must not be parsed")

    monkeypatch.setattr(BoundedPdfUploadHandler, "new_file", parsed)
    anonymous = Client().post(
        BASE + "/documents", {"pdf": SimpleUploadedFile("large.pdf", b"%PDF-" + b"x" * 30)}
    )
    assert anonymous.status_code == 401 and anonymous["WWW-Authenticate"] == "Bearer"


def test_complete_aggregates_are_independent_of_record_pages(api_client):
    client, owner, _, _ = api_client
    house, vendor = setup(client)
    count = 1101
    documents = Document.objects.bulk_create(
        [Document(source="manual", storage_state="none", state="ready") for _ in range(count)]
    )
    invoices = Invoice.objects.bulk_create(
        [
            Invoice(
                document=doc,
                vendor_id=vendor["id"],
                reference=f"BULK-{i}",
                invoice_date=date(2026, 1, 31),
                net_minor=100,
                vat_minor=25,
                rounding_minor=0,
                invoice_total_minor=125,
                amount_due_minor=125,
                status="approved",
                approved_at=timezone.now(),
                approved_by=owner,
                review_revision=1,
            )
            for i, doc in enumerate(documents)
        ]
    )
    sections = BillingSection.objects.bulk_create(
        [
            BillingSection(
                invoice=invoice,
                section_id="water",
                property_id=house["id"],
                category="water",
                net_minor=100,
                vat_minor=25,
                gross_minor=125,
                start=date(2026, 1, 1),
                end=date(2026, 2, 1),
            )
            for invoice in invoices
        ]
    )
    Usage.objects.bulk_create(
        [
            Usage(
                section=section,
                position=0,
                utility="water",
                unit="m3",
                quantity_milli=123,
                start=date(2026, 1, 1),
                end=date(2026, 2, 1),
                basis="billed",
            )
            for section in sections
        ]
    )
    rows = client.get(BASE + "/reports/costs?limit=2").json()
    assert rows["count"] == count and len(rows["items"]) == 2
    assert (
        client.get(BASE + "/reports/costs?mode=totals").json()["groups"][0]["gross_minor"]
        == count * 125
    )
    assert (
        client.get(BASE + "/reports/consumption?mode=billed_period").json()["groups"][0][
            "quantity_milli"
        ]
        == count * 123
    )
    summary = client.get(BASE + "/reports/summary").json()
    assert summary["total_invoices"] == summary["manual_invoices"] == count
    import bills.api_reporting as reporting

    reporting.MAX_GROUPS = 0
    try:
        response = client.get(BASE + "/reports/costs?mode=totals")
        assert (
            response.status_code == 422 and response.json()["error"]["code"] == "filter_too_broad"
        )
    finally:
        reporting.MAX_GROUPS = 10000


def test_water_dashboard_excludes_other_utility_units(api_client, ledger):  # noqa: F811
    client, _, _, _ = api_client
    house = ledger["houses"][0]
    start, end = date(2026, 1, 1), date(2026, 2, 1)
    for service, utility, basis, quantity in (
        ("water", "water", "billed", 12345),
        ("waste", "waste", "billed_weight", 57000),
        ("electricity_network", "electricity", "measured", 99000),
    ):
        ledger["make"](
            [
                allocation(
                    house,
                    service=service,
                    usages=[
                        usage(
                            quantity,
                            utility=utility,
                            basis=basis,
                            subcategory=service if utility == "electricity" else "",
                            start=start,
                            end=end,
                        )
                    ],
                    start=start,
                    end=end,
                )
            ],
            invoice_date=date(2026, 1, 31),
        )
    unfiltered = client.get(BASE + "/reports/consumption?mode=billed_period").json()
    assert {row["unit"] for row in unfiltered["groups"]} == {"m3", "kg", "kWh"}

    dashboard = json.loads(Path("docs/api/grafana-dashboard.json").read_text())
    panel = next(panel for panel in dashboard["panels"] if "Billed water" in panel["title"])
    url = panel["targets"][0]["url"]
    url = (
        url.replace("$__combineValues(property_id=,,&,${property:csv})", f"property_id={house.pk}")
        .replace("${__from:date:YYYY-MM-DD}", "2026-01-01")
        .replace("${__to:date:YYYY-MM-DD}", "2026-02-02")
    )
    parsed = urlsplit(url)
    response = client.get(parsed.path + "?" + parsed.query)
    assert response.status_code == 200, response.content
    groups = response.json()["groups"]
    assert len(groups) == 1
    assert groups[0]["utility"] == "water" and groups[0]["unit"] == "m3"
    assert groups[0]["quantity_milli"] == 12345


def test_electricity_selection_precedes_vendor_subcategory_filters(api_client, ledger):  # noqa: F811
    client, _, _, _ = api_client
    house = ledger["houses"][0]
    supply_vendor = Vendor.objects.create(name="Supply", slug="supply")
    ledger["make"]([allocation(house, service="electricity_network", usages=[usage(1000)])])
    ledger["make"](
        [allocation(house, service="electricity_supply", usages=[usage(1000)])],
        issuer=supply_vendor,
    )
    for query in (f"vendor_id={supply_vendor.pk}", "subcategory=electricity_supply"):
        result = client.get(BASE + "/reports/consumption?mode=totals&" + query)
        assert result.status_code == 200, result.content
        assert result.json()["groups"] == []
    assert (
        client.get(BASE + "/reports/consumption?mode=totals").json()["groups"][0]["quantity_milli"]
        == 1000
    )
    assert len(client.get(BASE + "/reports/costs?mode=totals").json()["groups"]) == 2


def test_annotation_retries_reason_maintenance_and_history(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {"category": "water", "start_month": "2026-01-01"},
    )
    created(
        client,
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
    )
    created(
        client,
        f"/vendors/{vendor['id']}/billing-schedules",
        {"start_month": "2026-01-01", "first_expected_month": "2026-01-01"},
    )
    payload = {"vendor_id": vendor["id"], "month": "2026-01-01", "reason": "No bill was issued"}
    before = LogEntry.objects.count()
    preview = send(client, "post", "/invoice-exceptions?dry_run=true", payload)
    assert preview.status_code == 200 and preview.json()["dry_run"]
    assert not ServiceInvoiceException.objects.exists()
    assert LogEntry.objects.count() == before
    result = send(client, "post", "/invoice-exceptions", payload)
    assert result.status_code == 201, result.content
    obj = result.json()
    before = LogEntry.objects.count()
    preview = send(
        client, "patch", f"/invoice-exceptions/{obj['id']}?dry_run=true", {"reason": "Preview"}
    )
    assert preview.status_code == 200 and preview.json()["reason"] == "Preview"
    assert ServiceInvoiceException.objects.get(pk=obj["id"]).reason == "No bill was issued"
    preview = send(client, "delete", f"/invoice-exceptions/{obj['id']}?dry_run=true")
    assert preview.status_code == 200 and preview.json()["dry_run"]
    assert ServiceInvoiceException.objects.filter(pk=obj["id"]).exists()
    assert LogEntry.objects.count() == before
    assert send(client, "patch", f"/invoice-exceptions/{obj['id']}", {}).json()["reason"] == (
        "No bill was issued"
    )
    assert (
        send(client, "patch", f"/invoice-exceptions/{obj['id']}", {"reason": None}).status_code
        == 422
    )
    payload["reason"] = "Retry must not edit reason"
    assert send(client, "post", "/invoice-exceptions", payload).status_code == 200
    assert ServiceInvoiceException.objects.get(pk=obj["id"]).reason == "No bill was issued"
    assert (
        send(
            client, "patch", f"/invoice-exceptions/{obj['id']}", {"reason": "Corrected explanation"}
        ).status_code
        == 200
    )
    assert (
        send(
            client, "patch", f"/invoice-exceptions/{obj['id']}", {"vendor_id": vendor["id"]}
        ).status_code
        == 422
    )
    assert send(client, "delete", f"/invoice-exceptions/{obj['id']}").status_code == 204
    assert (
        client.get(
            BASE
            + "/reports/invoice-coverage?as_of=2026-02-15&date_from=2026-01-01&date_to=2026-01-31"
        ).json()["as_of"]
        == "2026-02-15"
    )


def test_admin_token_secret_once_and_server_bound_ownership(api_client, django_user_model):
    _, _, _, _ = api_client
    staff = django_user_model.objects.create_user("token-staff", is_staff=True)
    staff.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["add_apitoken", "view_apitoken", "delete_apitoken"]
        )
    )
    other = django_user_model.objects.create_user("other-owner", is_staff=True)
    client = Client()
    client.force_login(staff)
    response = client.post(
        "/admin/bills/apitoken/add/",
        {
            "name": "One-time example",
            "owner": other.pk,
            "profile": "reporting",
            "expires_at_0": "",
            "expires_at_1": "",
        },
    )
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    secret = response.context_data["secret"]
    token = ApiToken.objects.get(name="One-time example")
    assert token.owner == staff and token.digest == hashlib.sha256(secret.encode()).hexdigest()
    assert secret not in str(list(LogEntry.objects.values()))
    assert secret not in str(client.session.items())
    later = client.get(f"/admin/bills/apitoken/{token.pk}/change/")
    assert secret.encode() not in later.content
    assert b"digest" not in later.content
    foreign, _ = ApiToken.issue(name="Foreign", owner=other, profile="automation")
    assert (
        client.post(
            "/admin/bills/apitoken/",
            {
                "action": "delete_selected",
                "_selected_action": [token.pk, foreign.pk],
                "post": "yes",
            },
        ).status_code
        == 302
    )
    assert ApiToken.objects.filter(pk=foreign.pk).exists()
