"""HTTP contract checks use invented records and disposable original storage."""

import json
import sqlite3
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import OperationalError, connections
from django.test import RequestFactory

from bills.api.middleware import JSON_LIMIT, ApiContractMiddleware
from bills.api.snapshots import consistent_reads
from bills.models import (
    ApiToken,
    Property,
)

from .helpers import BASE, setup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (ValidationError("Configuration changed.", code="configuration_conflict"), 409),
        (ValidationError("An unrelated overlap explanation.", code="invalid_value"), 422),
        (
            ValidationError(
                {"reference": ValidationError("Identity is taken.", code="configuration_conflict")}
            ),
            409,
        ),
    ],
)
def test_validation_status_uses_codes_independently_of_message_wording(
    api_client, monkeypatch, error, status
):
    from bills.api import configuration

    client, _, _, _ = api_client

    def fail(*args):
        raise error

    monkeypatch.setattr(configuration, "get_record", fail)
    response = client.get(BASE + "/properties/1")
    assert response.status_code == status, response.content


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
