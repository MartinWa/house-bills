"""HTTP contract checks use invented records and disposable original storage."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from django.utils import timezone

from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Usage,
    Vendor,
)

from ..test_reporting import allocation, ledger, usage  # noqa: F401
from .helpers import BASE, setup

pytestmark = pytest.mark.django_db(transaction=True)


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
