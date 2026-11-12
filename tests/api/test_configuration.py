"""HTTP contract checks use invented records and disposable original storage."""

from datetime import date

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.db.backends.utils import CursorWrapper
from django.test import Client

from bills.models import (
    ApiToken,
    InvoicePropertyRule,
    Property,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Vendor,
    VendorBillingSchedule,
    VendorIssuerIdentifier,
)

from .helpers import BASE, created, send, setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_collection_queries_do_not_grow_with_page_size(api_client, monkeypatch):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {
            "category": "water",
            "start_month": "2026-01-01",
        },
    )
    Property.objects.bulk_create(
        [Property(name=f"House {index}", slug=f"house-{index}") for index in range(10)]
    )
    Vendor.objects.bulk_create(
        [
            Vendor(name=f"Vendor {index}", slug=f"vendor-{index}", default_property_id=house["id"])
            for index in range(10)
        ]
    )
    ServiceVendorAssignment.objects.bulk_create(
        [
            ServiceVendorAssignment(
                property_service_id=service["id"], vendor_id=pk, start_month=date(2026, 1, 1)
            )
            for pk in Vendor.objects.values_list("pk", flat=True)
        ]
    )
    queries = []
    original = CursorWrapper.execute

    def collect(cursor, sql, params=None):
        if sql.lstrip().upper().startswith("SELECT"):
            queries.append(sql)
        return original(cursor, sql, params)

    monkeypatch.setattr(CursorWrapper, "execute", collect)
    for path in ["/vendors", "/properties", f"/services/{service['id']}/vendor-assignments"]:
        counts = []
        for limit in (1, 200):
            queries.clear()
            response = client.get(BASE + path + f"?limit={limit}")
            assert response.status_code == 200, response.content
            counts.append(len(queries))
            assert not any('FROM "bills_subcategory"' in sql for sql in queries)
            assert not any('FROM "django_admin_log"' in sql for sql in queries)
            if path == "/vendors":
                assert response.json()["items"][0]["links"]["vendor_assignments"]["count"] == 1
        assert counts[0] == counts[1]


def test_replacement_links_survive_audit_deletion_and_preview_rollbacks(api_client):
    client, _, _, _ = api_client
    _, vendor = setup(client)
    first = created(
        client,
        f"/vendors/{vendor['id']}/billing-schedules",
        {
            "start_month": "2026-01-01",
            "first_expected_month": "2026-01-01",
        },
    )
    payload = {
        "start_month": "2027-01-01",
        "first_expected_month": "2027-01-01",
        "frequency_months": 3,
        "grace_days": 0,
        "expected_end_month": None,
    }
    second = created(client, f"/billing-schedules/{first['id']}/replacement", payload)
    LogEntry.objects.all().delete()
    assert (
        client.get(first["links"]["self"]).json()["links"]["replacement"] == second["links"]["self"]
    )
    assert (
        client.get(second["links"]["self"]).json()["links"]["predecessor"] == first["links"]["self"]
    )
    preview = send(
        client,
        "post",
        f"/billing-schedules/{second['id']}/replacement?dry_run=true",
        {
            **payload,
            "start_month": "2028-01-01",
            "first_expected_month": "2028-01-01",
        },
    )
    assert preview.status_code == 200 and preview.json()["dry_run"]
    assert client.get(second["links"]["self"]).json()["end_month"] is None
    assert "replace_period" not in client.get(second["links"]["self"]).json()["links"]
    assert (
        send(client, "delete", f"/billing-schedules/{first['id']}?dry_run=true").status_code == 200
    )
    assert (
        client.get(second["links"]["self"]).json()["links"]["predecessor"] == first["links"]["self"]
    )
    assert send(client, "delete", f"/billing-schedules/{first['id']}").status_code == 204
    assert "predecessor" not in client.get(second["links"]["self"]).json()["links"]


@pytest.mark.parametrize("period", ["assignment", "schedule"])
def test_historical_split_reconnects_successor_and_rolls_back_failed_writes(
    api_client, monkeypatch, period
):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    if period == "assignment":
        model = ServiceVendorAssignment
        service = created(
            client,
            f"/properties/{house['id']}/services",
            {"category": "water", "start_month": "2026-01-01"},
        )
        first = created(
            client,
            f"/services/{service['id']}/vendor-assignments",
            {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
        )
        later_vendor = created(client, "/vendors", {"name": "Later vendor", "slug": "later"})
        middle_vendor = created(client, "/vendors", {"name": "Middle vendor", "slug": "middle"})
        later_payload = {"vendor_id": later_vendor["id"]}
        middle_payload = {"vendor_id": middle_vendor["id"]}
    else:
        model = VendorBillingSchedule
        first = created(
            client,
            f"/vendors/{vendor['id']}/billing-schedules",
            {"start_month": "2026-01-01", "first_expected_month": "2026-01-01"},
        )
        later_payload = {
            "first_expected_month": "2026-07-01",
            "frequency_months": 3,
            "grace_days": 4,
        }
        middle_payload = {
            "first_expected_month": "2026-04-01",
            "frequency_months": 2,
            "grace_days": 1,
        }
    path = first["links"]["self"].removeprefix(BASE) + "/replacement"
    later = created(
        client,
        path,
        {**later_payload, "start_month": "2026-07-01", "expected_end_month": None},
    )
    payload = {**middle_payload, "start_month": "2026-04-01", "expected_end_month": "2026-06-01"}
    first_before = client.get(first["links"]["self"]).json()
    later_before = client.get(later["links"]["self"]).json()
    audit_count = LogEntry.objects.count()
    preview = send(client, "post", path + "?dry_run=true", payload)
    assert preview.status_code == 200 and preview.json()["dry_run"], preview.content
    assert client.get(first["links"]["self"]).json() == first_before
    assert client.get(later["links"]["self"]).json() == later_before
    assert model.objects.count() == 2 and LogEntry.objects.count() == audit_count

    original_save = model.save

    def fail_insert(obj, *args, **kwargs):
        if obj._state.adding and obj.start_month == date(2026, 4, 1):
            raise ValidationError("Synthetic failure after the successor link was released.")
        return original_save(obj, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(model, "save", fail_insert)
        failed = send(client, "post", path, payload)
    assert failed.status_code == 409, failed.content
    assert client.get(first["links"]["self"]).json() == first_before
    assert client.get(later["links"]["self"]).json() == later_before
    assert model.objects.count() == 2 and LogEntry.objects.count() == audit_count

    middle = created(client, path, payload)
    first_after = client.get(first["links"]["self"]).json()
    later_after = client.get(later["links"]["self"]).json()
    assert first_after["end_month"] == "2026-03-01"
    assert middle["start_month"] == "2026-04-01" and middle["end_month"] == "2026-06-01"
    assert first_after["links"]["replacement"] == middle["links"]["self"]
    assert middle["links"]["predecessor"] == first["links"]["self"]
    assert middle["links"]["replacement"] == later["links"]["self"]
    assert later_after["links"]["predecessor"] == middle["links"]["self"]
    for field in ("id", "start_month", "end_month", "vendor_id", "enabled"):
        assert later_after[field] == later_before[field]
    assert model.objects.count() == 3


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
