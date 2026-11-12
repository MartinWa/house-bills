"""HTTP contract checks use invented records and disposable original storage."""

import json

import pytest
from django.test import Client

from bills.models import (
    ApiToken,
)

BASE = "/api/v1"


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
