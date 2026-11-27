"""Reporting consumers get complete, labelled aggregates and honest source pages."""

from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.db import connections

from bills.api import schemas
from bills.models import BillingSection, Document, Invoice, Property, Subcategory, Usage, Vendor

from .helpers import BASE, editable_allocations, setup

pytestmark = pytest.mark.django_db(transaction=True)


def records(api_client, *, count=1, utility="water", subcategory=None):
    client, owner, _, _ = api_client
    house, vendor = setup(client)
    documents = Document.objects.bulk_create(
        [Document(source="manual", storage_state="none", state="ready") for _ in range(count)]
    )
    invoices = Invoice.objects.bulk_create(
        [
            Invoice(
                document=document,
                vendor_id=vendor["id"],
                reference=f"SYNTHETIC-{i}",
                invoice_date=date(2026, 1 if i < count // 2 else 2, 28),
                net_minor=100,
                vat_minor=25,
                rounding_minor=0,
                invoice_total_minor=125,
                amount_due_minor=125,
                status="approved",
                approved_at=datetime(2026, 3, 1, tzinfo=UTC),
                approved_by=owner,
                review_revision=1,
            )
            for i, document in enumerate(documents)
        ]
    )
    sections = BillingSection.objects.bulk_create(
        [
            BillingSection(
                invoice=invoice,
                section_id="source",
                property_id=house["id"],
                category=utility,
                subcategory_id=subcategory,
                net_minor=100,
                vat_minor=25,
                gross_minor=125,
                start=invoice.invoice_date.replace(day=1),
                end=date(2026, invoice.invoice_date.month + 1, 1),
            )
            for invoice in invoices
        ]
    )
    usages = Usage.objects.bulk_create(
        [
            Usage(
                section=section,
                position=0,
                utility=utility,
                subcategory_id=subcategory,
                unit="kW"
                if subcategory == "electricity_demand"
                else {"water": "m3", "waste": "kg", "electricity": "kWh"}[utility],
                quantity_milli=1000,
                start=section.start,
                end=section.end,
                basis="billed",
            )
            for section in sections
        ]
    )
    return client, house, vendor, invoices, sections, usages


def get(client, path):
    response = client.get(BASE + "/reports/" + path)
    assert response.status_code == 200, response.content
    assert response["Cache-Control"] == "no-store"
    result = response.json()
    assert result["contract_version"] == 1
    assert datetime.fromisoformat(result["generated_at"]).utcoffset() == timedelta(0)
    return result


@pytest.mark.parametrize(
    ("kind", "utility", "subcategory", "sort", "unique"),
    [
        ("costs", "water", None, "invoice_date", "allocation_id"),
        ("consumption", "water", None, "period_start", "usage_id"),
        ("consumption-issues", "electricity", None, "invoice_date", "usage_id"),
        ("billed-power", "electricity", "electricity_demand", "period_start", "usage_id"),
    ],
)
def test_newest_pages_ties_and_continuations(api_client, kind, utility, subcategory, sort, unique):
    client, house, _, _, sections, usages = records(
        api_client, count=211, utility=utility, subcategory=subcategory
    )
    ids = [row.pk for row in sections if unique == "allocation_id"] or [row.pk for row in usages]
    expected = ids[105:] + ids[:105]
    query = f"{kind}?sort=-{sort}&property_id={house['id']}&category={utility}&limit=200"
    first = get(client, query)
    assert first["count"] == 211 and first["has_more"] and first["previous"] is None
    assert [row[unique] for row in first["items"]] == expected[:200]
    continuation = parse_qs(urlsplit(first["next"]).query)
    assert continuation["sort"] == [f"-{sort}"]
    assert continuation["property_id"] == [str(house["id"])]
    assert continuation["category"] == [utility]
    final = client.get(first["next"]).json()
    assert [row[unique] for row in final["items"]] == expected[200:]
    assert final["count"] == 211 and not final["has_more"] and final["next"] is None
    assert final["previous"]
    empty = get(client, query + "&offset=211")
    assert empty["count"] == 211 and empty["items"] == [] and not empty["has_more"]
    default = get(client, kind)
    assert len(default["items"]) == 50 and default["items"][0][unique] == ids[0]


@pytest.mark.parametrize(
    "path",
    [
        "costs?sort=",
        "costs?sort=invoice_date,",
        "costs?sort=--invoice_date",
        "costs?sort=invoice_date,-invoice_date",
        "costs?sort=invoice_date&sort=invoice_date",
        "costs?sort=invoice_date;DROP TABLE bills_invoice",
        "costs?sort=absent",
        "costs?mode=totals&sort=invoice_date",
        "costs?mode=grouped",
        "costs?group_by=",
        "costs?group_by=category,",
        "costs?group_by=category,category",
        "costs?group_by=property_name",
        "costs?group_by=invoice_kind",
        "costs?mode=rows&group_by=currency",
        "costs?mode=totals&group_by=currency",
        "costs?group_by=currency&limit=1",
        "costs?group_by=currency&offset=0",
        "costs?group_by=currency&group_by=currency",
        "costs?currency=EUR",
        "consumption?unit=kW",
        "consumption?currency=SEK",
        "costs?unit=kg",
        "summary?unit=kg",
        "summary?currency=SEK",
        "summary?group_by=property_id",
        "summary?sort=invoice_date",
        "selectors?currency=SEK",
        "billed-power?group_by=period_start",
        "consumption-issues?unit=kWh",
    ],
)
def test_strict_reporting_parameters(api_client, path):
    client, _, _, _ = api_client
    response = client.get(BASE + "/reports/" + path)
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "invalid_parameter"


def test_custom_groups_counts_and_credit_arithmetic(api_client):
    client, house, vendor, invoices, sections, _ = records(api_client, count=2)
    other_house = Property.objects.create(name="Another synthetic house", slug="another-house")
    other_vendor = Vendor.objects.create(name="Another synthetic vendor", slug="another-vendor")
    Invoice.objects.filter(pk=invoices[1].pk).update(kind="credit", vendor=other_vendor)
    with editable_allocations(invoices[1]):
        BillingSection.objects.filter(pk=sections[1].pk).update(
            property=other_house, net_minor=-100, vat_minor=-25, gross_minor=-125
        )
    extra = BillingSection.objects.bulk_create(
        [
            BillingSection(
                invoice=invoices[0],
                section_id="second",
                property_id=house["id"],
                category="water",
                net_minor=0,
                vat_minor=0,
                gross_minor=0,
                start=date(2026, 1, 1),
                end=date(2026, 2, 1),
            )
        ]
    )[0]
    Usage.objects.bulk_create(
        [
            Usage(
                section=sections[0],
                position=1,
                utility="water",
                unit="m3",
                quantity_milli=-1000,
                basis="reversal",
                start=date(2025, 12, 1),
                end=date(2026, 1, 1),
            ),
            Usage(
                section=extra,
                position=0,
                utility="water",
                unit="m3",
                quantity_milli=0,
                basis="billed",
                start=date(2026, 1, 1),
                end=date(2026, 2, 1),
            ),
        ]
    )
    costs = get(client, "costs?group_by=currency")
    assert costs["mode"] == "grouped" and costs["group_by"] == ["currency"]
    group = costs["groups"][0]
    assert group["property_id"] is None and group["property_name"] is None
    assert group["gross_minor"] == 0 and group["invoice_count"] == 2
    assert group["allocation_count"] == 3 and costs["group_count"] == 1
    consumption = get(client, "consumption?group_by=period_start")
    assert consumption["group_by"] == ["period_start", "utility", "unit"]
    assert len(consumption["groups"]) == 3
    january = next(row for row in consumption["groups"] if row["period_start"] == "2026-01-01")
    assert january["invoice_count"] == 1 and january["allocation_count"] == 2
    assert january["usage_count"] == 2 and january["period_start_time"] == 1767225600
    all_usage = get(client, "consumption?group_by=utility,unit")["groups"][0]
    assert (
        all_usage["invoice_count"],
        all_usage["allocation_count"],
        all_usage["usage_count"],
    ) == (2, 3, 4)
    assert sum(row["invoice_count"] for row in consumption["groups"]) > all_usage["invoice_count"]
    for kind in ("costs", "consumption"):
        grouped = get(client, kind + "?group_by=property_id,vendor_id")
        assert {row["property_id"] for row in grouped["groups"]} == {house["id"], other_house.pk}
        assert {row["vendor_id"] for row in grouped["groups"]} == {vendor["id"], other_vendor.pk}
        assert all(row["property_name"] and row["vendor_name"] for row in grouped["groups"])
    months = get(client, "costs?group_by=invoice_month")
    assert {row["invoice_month_time"] for row in months["groups"]} == {1767225600, 1769904000}
    assert schemas.ReportRow.model_fields["property_id"].is_required()


@pytest.mark.parametrize("mode", ["totals", "invoice_month", "grouped&group_by=currency"])
def test_unknown_money_does_not_turn_into_a_partial_total(api_client, mode):
    client, _, _, invoices, sections, _ = records(api_client, count=2)
    Invoice.objects.filter(pk=invoices[1].pk).update(invoice_date=invoices[0].invoice_date)
    with editable_allocations(invoices[1]):
        BillingSection.objects.filter(pk=sections[1].pk).update(net_minor=None, gross_minor=None)
    result = get(client, "costs?mode=" + mode)
    assert result["groups"][0]["net_minor"] is None
    assert result["groups"][0]["gross_minor"] is None
    assert result["groups"][0]["vat_minor"] == 50
    assert result["totals"][0]["gross_minor"] is None
    assert get(client, "summary")["costs"][0]["gross_minor"] is None


def test_unit_currency_filters_labels_and_empty_data(api_client):
    client, house, _, invoices, _, _ = records(api_client)
    assert get(client, "consumption?unit=kg&group_by=utility")["groups"] == []
    empty = get(client, "consumption?unit=kg")
    assert empty["items"] == [] and empty["count"] == 0 and not empty["has_more"]
    assert empty["filters"]["unit"] == "kg"
    assert empty["effective_date_bounds"] == {"date_from": None, "date_to": None}
    water = get(client, "consumption?unit=m3&mode=totals")["groups"][0]
    assert water["unit"] == "m3" and water["unit_label"] == "m³"
    assert water["utility_name"] == "Water"
    cost = get(client, "costs?currency=SEK")["items"][0]
    assert cost["category_name"] == "Water" and cost["invoice_kind"] == "invoice"
    assert cost["review_revision"] == 1
    selectors = get(client, "selectors")
    assert {row["key"]: row["name"] for row in selectors["units"]}["m3"] == "m³"
    summary = get(client, "summary")
    assert len(summary["consumption"]) == 1
    assert datetime.fromisoformat(summary["latest_approval"]) == invoices[0].approved_at
    absent = get(client, f"summary?property_id={house['id'] + 999}")
    assert absent["latest_approval"] is None and absent["total_invoices"] == 0


@pytest.mark.parametrize("kind", ["costs", "consumption"])
@pytest.mark.parametrize(("utility", "label"), [("waste", "Unspecified"), ("water", "")])
def test_empty_subcategory_labels_in_filtered_custom_groups(api_client, kind, utility, label):
    client, *_ = records(api_client, utility=utility)
    result = get(client, f"{kind}?category={utility}&group_by=subcategory")
    group = result["groups"][0]
    assert group["subcategory"] == "" and group["subcategory_name"] == label
    assert group["category"] is None and group["category_name"] is None
    assert result["totals"][0]["subcategory_name"] == label


def test_historical_catalogue_labels_and_issue_reasons(api_client):
    Subcategory.objects.create(
        key="synthetic-fraction",
        name="Historical fraction",
        category="waste",
        is_active=False,
        is_waste_fraction=True,
    )
    client, _, _, invoices, sections, _ = records(
        api_client, utility="waste", subcategory="synthetic-fraction"
    )
    for kind in ("costs", "consumption"):
        result = get(client, kind + "?group_by=subcategory")
        assert result["groups"][0]["subcategory_name"] == "Historical fraction"
    with editable_allocations(invoices[0]):
        BillingSection.objects.filter(pk=sections[0].pk).update(subcategory=None)
    assert get(client, "costs")["items"][0]["subcategory_name"] == "Unspecified"
    with editable_allocations(invoices[0]):
        Usage.objects.filter(section=sections[0]).delete()
        BillingSection.objects.filter(pk=sections[0].pk).update(category="electricity")
    Usage.objects.bulk_create(
        [
            Usage(
                section=sections[0],
                position=0,
                utility="electricity",
                unit="kWh",
                quantity_milli=1000,
                basis="billed",
                start=sections[0].start,
                end=sections[0].end,
            )
        ]
    )
    issue = get(client, "consumption-issues")["items"][0]
    assert issue["selection_reason"] == "unspecified_source"
    assert issue["reason"] == "The electricity source needs review before consumption selection."


def test_rows_groups_totals_and_labels_share_a_snapshot(api_client, monkeypatch):
    import bills.api_reporting as reporting

    Subcategory.objects.create(key="synthetic-water", category="water", name="Original label")
    client, _, _, invoices, sections, _ = records(api_client, subcategory="synthetic-water")
    original = reporting.aggregate
    writer = connections["default"].copy(alias="reporting_correction_writer")
    calls = []

    def correction(reader, filters, kind, mode, group_by=None):
        result = original(reader, filters, kind, mode, group_by)
        calls.append(mode)
        if len(calls) == 1:
            with writer.cursor() as cursor:
                with editable_allocations(invoices[0]):
                    cursor.execute(
                        "UPDATE bills_billingsection SET net_minor=200, vat_minor=50, "
                        "gross_minor=250 WHERE id=%s",
                        [sections[0].pk],
                    )
                cursor.execute(
                    "UPDATE bills_property SET name='Corrected name' WHERE id=%s",
                    [sections[0].property_id],
                )
                cursor.execute(
                    "UPDATE bills_subcategory SET name='Corrected label' WHERE key=%s",
                    ["synthetic-water"],
                )
                cursor.execute(
                    "UPDATE bills_invoice SET approved_at=%s WHERE id=%s",
                    [datetime(2026, 4, 1, tzinfo=UTC), invoices[0].pk],
                )
        return result

    monkeypatch.setattr(reporting, "aggregate", correction)
    try:
        result = get(client, "costs?group_by=currency")
        assert result["groups"][0]["gross_minor"] == result["totals"][0]["gross_minor"] == 125
        assert result["totals"][0]["property_name"] != "Corrected name"
        assert result["totals"][0]["subcategory_name"] == "Original label"
        result = get(client, "summary")
        assert result["costs"][0]["gross_minor"] == 250
        assert result["costs"][0]["property_name"] == "Corrected name"
        assert result["costs"][0]["subcategory_name"] == "Corrected label"
    finally:
        writer.close()


def test_custom_totals_group_guard_is_not_bypassed(api_client, monkeypatch):
    import bills.api_reporting as reporting

    client, _, _, invoices, sections, _ = records(api_client, count=2)
    other = Property.objects.create(name="Another house", slug="another-house")
    with editable_allocations(invoices[1]):
        BillingSection.objects.filter(pk=sections[1].pk).update(property=other)
    monkeypatch.setattr(reporting, "MAX_GROUPS", 1)
    response = client.get(BASE + "/reports/costs?group_by=currency")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "filter_too_broad"


def test_latest_approval_uses_filtered_invoice_union(api_client):
    client, _, _, invoices, _, _ = records(api_client, count=2)
    later = datetime(2026, 4, 1, tzinfo=UTC)
    Invoice.objects.filter(pk=invoices[1].pk).update(approved_at=later)
    assert datetime.fromisoformat(get(client, "summary")["latest_approval"]) == later
    # No invoice is dated in these five days, but the first invoice's usage overlaps.
    january = get(client, "summary?date_from=2026-01-01&date_to=2026-01-05")
    assert january["costs"] == [] and january["total_invoices"] == 1
    assert datetime.fromisoformat(january["latest_approval"]) == invoices[0].approved_at
    assert get(client, "summary?category=waste")["latest_approval"] is None
