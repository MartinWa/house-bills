"""HTTP contract checks use invented records and disposable original storage."""

import hashlib
import json
import re
from datetime import timedelta

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from bills.api.auth import REPORTING_PATHS
from bills.models import (
    ApiToken,
    Document,
)

from .helpers import BASE, created

pytestmark = pytest.mark.django_db(transaction=True)


def test_business_requests_verify_the_credential_once_including_uploads(api_client, monkeypatch):
    from bills.api import workflow

    client, _, _, _ = api_client
    document = Document.objects.create(
        original_filename="synthetic.pdf", sha256="a" * 64, state=Document.State.REVIEW
    )
    monkeypatch.setattr(workflow, "ingest_pdf", lambda *args, **kwargs: (document, False))
    for upload in (False, True):
        with CaptureQueriesContext(connection) as queries:
            response = (
                client.post(BASE + "/documents", {"pdf": SimpleUploadedFile("test.pdf", b"%PDF-")})
                if upload
                else client.get(BASE + "/me")
            )
        assert response.status_code == (201 if upload else 200), response.content
        credential_queries = [q for q in queries if 'FROM "bills_apitoken"' in q["sql"]]
        assert len(credential_queries) == 1


def test_audit_entries_distinguish_tokens_owned_by_the_same_user(api_client):
    client, owner, first, _ = api_client
    second, secret = ApiToken.issue(name="second automation", owner=owner, profile="automation")
    for number, (actor, token) in enumerate(
        [(client, first), (Client(HTTP_AUTHORIZATION=f"Bearer {secret}"), second)]
    ):
        obj = created(actor, "/properties", {"name": f"House {number}", "slug": f"house-{number}"})
        entry = LogEntry.objects.get(object_id=str(obj["id"]), content_type__model="property")
        assert entry.user_id == owner.pk
        assert {"api_token": {"id": token.pk, "name": token.name}} in json.loads(
            entry.change_message
        )


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
            concrete = re.sub(r"\{[^}]+\}", "1", path)
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
