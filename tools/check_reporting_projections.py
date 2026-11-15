"""Verify application-owned reporting examples through the real Infinity backend."""

import json
from pathlib import Path

from .check_api_grafana import DATASOURCE_UID, PLUGIN, request


def run():
    artifact = json.loads(Path("docs/api/reporting-examples.json").read_text(encoding="utf-8"))
    targets = {item["endpoint"]: item["target"] for item in artifact["infinity_queries"]}

    def query(target, payload=None):
        target = {**target, "datasource": {"type": PLUGIN, "uid": DATASOURCE_UID}}
        if payload is not None:
            target.update(source="inline", data=json.dumps(payload))
            target.pop("url", None)
            target.pop("url_options", None)
        status, result = request(
            "/api/ds/query", {"queries": [target], "from": "1767225600000", "to": "1772323200000"}
        )
        return status, result["results"]["A"]

    def columns(result):
        values = {}
        for frame in result.get("frames", []):
            for field, data in zip(frame["schema"]["fields"], frame["data"]["values"], strict=True):
                values.setdefault(field["name"], []).extend(data)
        return values

    for endpoint, target in targets.items():
        status, result = query(target)
        assert status == 200 and not result.get("error"), (endpoint, result)
        live = columns(result)
        assert live["page_message"], (endpoint, result)
        if endpoint == "invoice-costs":
            assert len(live["invoice_id"]) == 200, result
            assert set(live["page_message"]) == {
                "Showing 200 of 551 matching; more pages available"
            }, result
            assert set(live["gross_minor"]) == {125}, result
    for example in artifact["examples"]:
        payload, endpoint = example["response"], example["endpoint"]
        status, result = query(targets[endpoint], payload)
        assert status == 200 and not result.get("error"), (example["name"], result)
        values = columns(result)
        if payload["count"] == 0:
            assert values["page_message"] == ["No matching records"], result
        elif payload["has_more"]:
            assert set(values["page_message"]) == {
                f"Showing {len(payload['items'])} of {payload['count']} matching; "
                "more pages available"
            }, result
        if endpoint == "invoice-costs" and payload["items"]:
            assert values["gross_minor"] == [row["gross_minor"] for row in payload["items"]], result
            if payload["items"][0]["gross_minor"] is None:
                assert values["cost_display"] == ["Unknown"], result
        status, result = query(targets[endpoint], {**payload, "contract_version": 999})
        assert status != 200 or result.get("error"), "Unsupported contracts must fail visibly."
    print(
        "Reporting projections: live pages, empty/unknown amounts, signed shares, "
        "page messages and unsupported contracts passed."
    )
