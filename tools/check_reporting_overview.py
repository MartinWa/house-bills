"""Exercise a generated household overview through the actual Infinity backend."""

import json
from urllib.parse import urlsplit

from .check_api_grafana import request


def run(dashboard):
    def query(target, payload=None):
        target = dict(target)
        if payload is not None:
            target.update(source="inline", data=json.dumps(payload, ensure_ascii=False))
            target.pop("url", None)
            target.pop("url_options", None)
        else:
            source = urlsplit(target["url"])
            target["url"] = (
                source._replace(scheme="http", netloc="app:8000")
                .geturl()
                .replace("$__combineValues(property_id=,,&,${property:csv})", "property_id=1")
                .replace("${__timeFrom:date:YYYY-MM-DD}", "2026-01-01")
                .replace("${__timeTo:date:YYYY-MM-DD}", "2026-03-01")
            )
        status, response = request(
            "/api/ds/query",
            {
                "queries": [target],
                "from": "1767225600000",
                "to": "1772323200000",
            },
        )
        return status, response["results"][target["refId"]]

    def columns(target, payload=None):
        status, result = query(target, payload)
        assert status == 200 and not result.get("error"), result
        values = {}
        for frame in result.get("frames", []):
            for field, data in zip(frame["schema"]["fields"], frame["data"]["values"], strict=True):
                values.setdefault(field["name"], []).extend(data)
        return values, result

    base = {
        "contract_version": 1,
        "generated_at": "2026-03-01T00:00:00Z",
        "count": 551,
        "has_more": True,
        "latest_approval": "2026-03-01T00:00:00Z",
    }
    common = {
        "property_id": 1,
        "property_name": "Synthetic house",
        "vendor_name": "Synthetic vendor",
        "invoice_date": "2026-02-28",
        "invoice_month_time": 1769904000,
        "period_start_time": 1769904000,
        "period_start": "2026-02-01",
        "period_end": "2026-03-01",
        "utility": "water",
        "utility_name": "Water",
        "unit": "m3",
        "unit_label": "m³",
        "category": "water",
        "category_name": "Water",
        "subcategory": "",
        "subcategory_name": "",
        "currency": "SEK",
        "net_minor": 80,
        "vat_minor": 20,
        "gross_minor": 100,
        "quantity_milli": 1000,
        "source_selection": "billed",
        "source_service": "water",
        "basis": "billed",
        "reference": "SYNTHETIC",
        "invoice_id": 1,
        "document_id": 1,
        "invoice_kind": "credit",
        "review_revision": 2,
        "reason": "Synthetic review explanation.",
    }
    by_id = {panel["id"]: panel["targets"][0] for panel in dashboard["panels"]}
    for target in by_id.values():
        status, result = query(target)
        assert status == 200 and not result.get("error"), result
        columns(
            target,
            {
                **base,
                "groups": [],
                "items": [],
                "count": 0,
                "has_more": False,
                "latest_approval": None,
            },
        )
        status, result = query(target, {**base, "contract_version": 999, "groups": [], "items": []})
        assert status != 200 or result.get("error"), "Unsupported contract must fail visibly."
        assert "unsupported_reporting_contract" in str(result), result

    live_intervals, _ = columns(by_id[12])
    assert set(live_intervals["Unit"]) == {"m³"}, live_intervals

    costs = [
        common,
        {**common, "gross_minor": -20},
        {
            **common,
            "category": "electricity",
            "category_name": "Electricity",
            "subcategory": "electricity_demand",
            "subcategory_name": "Effektavgift",
            "gross_minor": 50,
        },
        {
            **common,
            "category": "electricity",
            "category_name": "Electricity",
            "subcategory": "historical-electricity",
            "subcategory_name": "Historical electricity",
            "gross_minor": 30,
        },
    ]
    payload = {**base, "groups": costs}
    total, _ = columns(by_id[1], payload)
    monthly, _ = columns(by_id[4], payload)
    assert total["Incl. VAT"] == [1.6], total
    assert monthly["Effektavgift"] == [0.5] and monthly["Electricity other"] == [0.3], monthly
    assert sum(v[0] for k, v in monthly.items() if k != "Time" and v[0] is not None) == 1.6
    unknown = {**base, "groups": [common, {**common, "gross_minor": None}]}
    assert columns(by_id[1], unknown)[0]["Incl. VAT"] == [None]
    assert columns(by_id[4], unknown)[0]["Water"] == [None]
    assert columns(by_id[5], unknown)[0]["Cost including VAT"] == [None]
    water, _ = columns(by_id[2], {**base, "groups": [common]})
    assert water["Electricity kWh"] == [None] and water["Waste kg"] == [None], water
    assert water["Water m³"] == [1], water
    zero, _ = columns(by_id[2], {**base, "groups": [{**common, "quantity_milli": 0}]})
    assert zero["Water m³"] == [0], zero
    fractions = [
        {**common, "subcategory": key, "subcategory_name": name, "quantity_milli": quantity}
        for key, name, quantity in (
            ("restavfall", "Restavfall", 1000),
            ("historical", "Historical fraction", -2000),
            ("", "Unspecified", 0),
        )
    ]
    _, waste = columns(by_id[8], {**base, "groups": fractions})
    names = {
        field.get("labels", {}).get("Fraction")
        for frame in waste["frames"]
        for field in frame["schema"]["fields"]
        if field["type"] == "number"
    }
    assert names == {"Restavfall", "Historical fraction", "Unspecified"}, waste
    for panel_id in (10, 11, 12):
        data, _ = columns(by_id[panel_id], {**base, "items": [common] * 200})
        assert len(data["Returned / matching records"]) == 200
        assert all(
            "200 of 551" in v and "more records" in v for v in data["Returned / matching records"]
        )
    allocations, _ = columns(by_id[11], {**base, "items": [common]})
    assert allocations["Kind"] == ["credit"] and allocations["Revision"] == [2]
    issues, _ = columns(by_id[10], {**base, "items": [common]})
    assert issues["Why review is needed"] == ["Synthetic review explanation."]
    intervals, _ = columns(
        by_id[12],
        {
            **base,
            "items": [
                {**common, "utility_name": "Other", "subcategory_name": "Historical fraction"}
            ],
        },
    )
    assert intervals["Utility"] == ["Other"] and intervals["Unit"] == ["m³"]
    print(
        "Household overview: live reporting requests, contract failures, demand/fallback costs, "
        "unknown money, missing/zero utilities, historical fractions and page visibility passed."
    )
