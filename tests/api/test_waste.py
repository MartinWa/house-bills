"""Waste additions preserve report pages, independent facts and configuration history."""

# Imported pytest fixtures are injected by name.
# ruff: noqa: F811

from datetime import date

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission

from bills.models import (
    ApiToken,
    Document,
    ServiceCostSchedule,
    ServiceVendorAssignment,
    Subcategory,
)

from ..test_reporting import allocation, ledger, usage  # noqa: F401
from .helpers import BASE, created, manual_body, send, setup

pytestmark = pytest.mark.django_db(transaction=True)


def test_waste_aggregates_and_source_weights_are_independent_of_pages_and_filters(
    api_client, ledger
):
    client, owner, _, _ = api_client
    house = ledger["houses"][0]
    invoice = ledger["make"](
        [
            allocation(
                house,
                service="waste",
                subcategory="waste_collection",
                net=16000,
                usages=[
                    usage(12000, utility="waste", basis="billed_weight", subcategory="matavfall"),
                    usage(0, utility="waste", basis="billed_weight", subcategory="restavfall"),
                ],
            ),
            allocation(house, service="waste", subcategory="tradgardsavfall", net=4000),
        ],
        source=Document.Source.MANUAL,
    )
    query = f"?property_id={house.pk}&date_from=2024-03-01&date_to=2024-03-31"
    summary = client.get(BASE + "/reports/summary" + query).json()
    assert summary["waste_total"]["gross_minor"] == 25000
    assert summary["consumption"] == []
    fractions = {row["fraction"]: row for row in summary["waste_fractions"]}
    assert fractions["matavfall"]["cost"] is None
    assert fractions["matavfall"]["weights"][0]["quantity_milli"] == 12000
    assert fractions["restavfall"]["weights"][0]["quantity_milli"] == 0
    assert fractions["tradgardsavfall"]["weights"] == []
    response = client.get(BASE + "/reports/costs" + query + "&limit=1")
    assert response.status_code == 200, response.content
    page = response.json()
    assert page["count"] == 2 and len(page["items"]) == 1
    assert page["waste_total"] == summary["waste_total"]
    assert page["waste_fractions"] == summary["waste_fractions"]
    cost = page["items"][0]
    assert cost["allocation_id"] == invoice.sections.get(subcategory_id="waste_collection").pk
    assert cost["gross_minor"] == 20000 and len(cost["weights"]) == 2
    weight = cost["weights"][0]
    assert weight["invoice_id"] == invoice.pk and weight["allocation_id"] == cost["allocation_id"]
    assert (
        weight["usage_id"]
        and weight["vendor_id"] == invoice.vendor_id
        and weight["property_id"] == house.pk
    )
    assert weight["invoice_date"] == "2024-03-05" and weight["period_start"] == "2024-02-01"
    assert weight["currency"] == "SEK"
    combined = client.get(BASE + "/reports/costs" + query + "&subcategory=waste_collection").json()
    assert combined["waste_total"]["gross_minor"] == 20000
    assert {row["fraction"] for row in combined["waste_fractions"]} == {"matavfall", "restavfall"}
    fraction_cost = client.get(BASE + "/reports/summary" + query + "&subcategory=matavfall").json()
    assert (
        fraction_cost["waste_total"]["gross_minor"] == 0 and fraction_cost["waste_fractions"] == []
    )
    for mode in ("rows", "totals", "billed_period"):
        consumption = client.get(BASE + f"/reports/consumption?category=waste&mode={mode}").json()
        assert not any(key.startswith("waste_") for key in consumption)
        reported = consumption["items"] if mode == "rows" else consumption["groups"]
        assert len(reported) == 2
        assert all("weights" not in row for row in reported)
    token, secret = ApiToken.issue(name="Waste reporting", owner=owner, profile="reporting")
    from django.test import Client

    reader = Client(HTTP_AUTHORIZATION=f"Bearer {secret}")
    assert (
        reader.get(BASE + "/reports/summary" + query).json()["waste_total"]
        == summary["waste_total"]
    )
    assert reader.get(BASE + "/reports/costs" + query).status_code == 200
    assert reader.get(BASE + "/cost-schedules/1").status_code == 403
    assert token.profile == "reporting"


def test_cost_pattern_crud_previews_lineage_and_vendor_alignment(api_client):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    metadata = client.get(BASE + "/metadata").json()
    entries = {row["key"]: row for row in metadata["subcategories"]}
    assert entries["plastic"]["is_waste_fraction"] and not entries["plastic"]["is_active"]
    assert not entries["waste_collection"]["is_waste_fraction"]
    only_fractions = client.get(BASE + "/subcategories?is_waste_fraction=true").json()["items"]
    assert {row["key"] for row in only_fractions} == {
        "matavfall",
        "restavfall",
        "tradgardsavfall",
        "plastic",
        "cardboard",
        "glass",
    }
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {"category": "waste", "subcategory": "restavfall", "start_month": "2024-01-01"},
    )
    assert service["service_group"] == "waste_collection"
    assignment = created(
        client,
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2024-01-01"},
    )
    schedule = created(
        client,
        f"/vendors/{vendor['id']}/billing-schedules",
        {"start_month": "2024-01-01", "first_expected_month": "2024-01-01", "frequency_months": 2},
    )
    path = f"/vendor-assignments/{assignment['id']}/cost-schedules"
    invalid = send(
        client,
        "post",
        path,
        {"start_month": "2024-01-01", "first_expected_month": "2024-02-01", "frequency_months": 2},
    )
    assert invalid.status_code == 422, invalid.content
    payload = {
        "start_month": "2024-01-01",
        "first_expected_month": "2024-01-01",
        "frequency_months": 2,
        "season_start_month": 3,
        "season_end_month": 10,
    }
    audit = LogEntry.objects.count()
    preview = send(client, "post", path + "?dry_run=true", payload)
    assert preview.status_code == 200 and preview.json()["dry_run"]
    assert ServiceCostSchedule.objects.count() == 0 and LogEntry.objects.count() == audit
    first = created(client, path, payload)
    assert first["assignment_id"] == assignment["id"] and first["vendor_id"] == vendor["id"]
    assert client.get(assignment["links"]["self"]).json()["links"]["cost_schedules"]["count"] == 1
    replacement = {"start_month": "2024-06-01", "expected_end_month": None, "frequency_months": 0}
    second = created(client, f"/cost-schedules/{first['id']}/replacement", replacement)
    assert client.get(first["links"]["self"]).json()["end_month"] == "2024-05-01"
    assert second["links"]["predecessor"] == first["links"]["self"]
    assert (
        send(client, "post", f"/cost-schedules/{first['id']}/replacement", replacement).status_code
        == 409
    )
    LogEntry.objects.all().delete()
    assert (
        client.get(first["links"]["self"]).json()["links"]["replacement"] == second["links"]["self"]
    )
    conflict = send(
        client, "patch", schedule["links"]["self"].removeprefix(BASE), {"frequency_months": 3}
    )
    assert conflict.status_code in (409, 422)
    assert send(client, "delete", f"/cost-schedules/{second['id']}").status_code == 204
    assert ServiceCostSchedule.objects.count() == 1


def test_manual_review_accepts_future_fraction_costs_without_weights_and_combined_partial_weights(
    api_client,
):
    client, _, _, _ = api_client
    house, vendor = setup(client)
    Subcategory.objects.filter(pk="plastic").update(is_active=True)
    result = send(client, "post", "/manual-invoices", {})
    assert result.status_code == 201, result.content
    document = result.json()["document"]
    observed = client.get(document["links"]["review"]).json()
    edits = manual_body(house, vendor)
    edits["sections"][0].update(category="waste", subcategory="plastic")
    edits["usages"] = []
    response = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": observed["review_token"], "edits": edits},
    )
    assert response.status_code == 201, response.content
    row = client.get(BASE + "/reports/summary").json()["waste_fractions"][0]
    assert (
        row["fraction"] == "plastic"
        and row["cost"]["gross_minor"] == 12500
        and row["weights"] == []
    )


def test_cost_patterns_require_their_own_model_permissions(api_client):
    client, owner, _, _ = api_client
    house, vendor = setup(client)
    service = created(
        client,
        f"/properties/{house['id']}/services",
        {"category": "waste", "start_month": "2026-01-01"},
    )
    assignment = created(
        client,
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
    )
    owner.is_superuser = False
    owner.save()
    owner.user_permissions.add(Permission.objects.get(codename="view_servicevendorassignment"))
    assert (
        client.get(BASE + f"/vendor-assignments/{assignment['id']}/cost-schedules").status_code
        == 403
    )
    owner.user_permissions.add(Permission.objects.get(codename="view_servicecostschedule"))
    assert (
        client.get(BASE + f"/vendor-assignments/{assignment['id']}/cost-schedules").status_code
        == 200
    )
    assert (
        send(
            client,
            "post",
            f"/vendor-assignments/{assignment['id']}/cost-schedules",
            {"start_month": "2026-01-01", "frequency_months": 0},
        ).status_code
        == 403
    )
    assert ServiceVendorAssignment.objects.get(pk=assignment["id"]).start_month == date(2026, 1, 1)
