"""Verify Infinity backend queries using HTTP only, with disposable Compose state."""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAFANA = "http://127.0.0.1:3002"
PLUGIN = "yesoreyeram-infinity-datasource"
DATASOURCE_UID = "house-bills-api"
COMPOSE = ["docker", "compose", "-f", "compose.api-check.yaml"]


def request(path, body=None):
    credential = base64.b64encode(b"synthetic:synthetic-grafana-check").decode()
    headers = {"Authorization": f"Basic {credential}", "Content-Type": "application/json"}
    try:
        with urlopen(
            Request(
                GRAFANA + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers=headers,
            ),
            timeout=20,
        ) as response:
            return response.status, json.load(response)
    except HTTPError as response:
        return response.status, json.load(response)


def query(kind, mode, property_ids=(), *, date_from="2026-01-01", date_to="2026-02-01"):
    params = "&".join(f"property_id={key}" for key in property_ids)
    url = f"http://app:8000/api/v1/reports/{kind}?mode={mode}&date_from={date_from}&date_to={date_to}&{params}"
    target = {
        "refId": "A",
        "datasource": {"type": PLUGIN, "uid": DATASOURCE_UID},
        "type": "json",
        "source": "url",
        "url": url,
        "parser": "backend",
        "format": "table",
        "root_selector": "groups",
        "columns": [
            {"selector": "property_id", "text": "property_id", "type": "number"},
            {
                "selector": "gross_minor" if kind == "costs" else "quantity_milli",
                "text": "value",
                "type": "number",
            },
            {
                "selector": "invoice_month_time" if kind == "costs" else "period_end_time",
                "text": "time",
                "type": "timestamp_epoch_s",
                "timestampFormat": "unix_s",
            },
        ],
        "url_options": {"method": "GET"},
    }
    status, result = request(
        "/api/ds/query", {"queries": [target], "from": "1767225600000", "to": "1772323200000"}
    )
    return status, result


def check_live_updates(credential):
    def call(method, path, body=None):
        with urlopen(
            Request(
                "http://127.0.0.1:8002" + path,
                method=method,
                headers={
                    "Authorization": f"Bearer {credential}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body).encode() if body is not None else None,
            ),
            timeout=20,
        ) as response:
            return json.load(response)

    def check_totals(extra):
        for kind, mode, expected in (
            ("costs", "invoice_month", 551 * 125 + extra * 125),
            ("consumption", "billed_period", 551 * 123 + extra * 123),
        ):
            status, result = query(kind, mode, [1])
            assert status == 200, result
            total = 0
            for frame in result["results"]["A"]["frames"]:
                index = next(
                    i for i, f in enumerate(frame["schema"]["fields"]) if f["name"] == "value"
                )
                total += sum(frame["data"]["values"][index])
            assert total == expected, result

    document = call("POST", "/api/v1/manual-invoices", {})["document"]
    check_totals(0)
    source = call("GET", document["links"]["review"])
    edits = {
        "header": {
            "vendor_id": 1,
            "reference": "SYNTHETIC-LIVE-UPDATE",
            "kind": "invoice",
            "invoice_date": "2026-01-31",
            "net_minor": 100,
            "vat_minor": 25,
            "rounding_minor": 0,
            "invoice_total_minor": 125,
            "amount_due_minor": 125,
        },
        "sections": [
            {
                "section_id": "water",
                "property_id": 1,
                "category": "water",
                "net_minor": 100,
                "vat_minor": 25,
                "gross_minor": 125,
                "period_start": "2026-01-01",
                "period_end": "2026-02-01",
            }
        ],
        "usages": [
            {
                "section_id": "water",
                "utility": "water",
                "unit": "m3",
                "quantity_milli": 123,
                "basis": "billed",
                "period_start": "2026-01-01",
                "period_end": "2026-02-01",
            }
        ],
    }
    call(
        "POST",
        document["links"]["reviews"],
        {"review_token": source["review_token"], "edits": edits},
    )
    check_totals(1)
    source = call("GET", document["links"]["review"])
    edits = source["edits"]
    for name in ("net_minor", "vat_minor", "invoice_total_minor", "amount_due_minor"):
        edits["header"][name] *= 2
    for name in ("net_minor", "vat_minor", "gross_minor"):
        edits["sections"][0][name] *= 2
    edits["usages"][0]["quantity_milli"] *= 2
    call(
        "POST",
        document["links"]["reviews"],
        {"review_token": source["review_token"], "edits": edits},
    )
    check_totals(2)


def check_sequential_example(credential, environment):
    script = """
import os
import subprocess
import sys
from pathlib import Path
from tests.synthetic import shared_supply, write_pdf

pages = shared_supply()[:2]
replacements = {'447,00': '138,19', '357,55': '110,55', '89,39': '27,64', '0,06': '0,00'}
pages[0] = [next((line.replace(old, new) for old, new in replacements.items() if old in line),
                 line) for line in pages[0]]
pages[0].append('VAT-nr SE000000999901')
pdf = write_pdf(Path('/state/api-check/example.pdf'), pages)
os.environ['HOUSE_BILLS_API_TOKEN'] = sys.stdin.readline().strip()
result = subprocess.run([sys.executable, 'docs/api/example.py',
                         '--base-url', 'http://localhost:8000/api/v1', '--pdf', str(pdf)],
                        input='APPROVE\\n', text=True, capture_output=True)
assert result.returncode == 0, result.stdout + result.stderr
print('Documented sequential setup, upload, approval, correction and reports passed.')
"""
    subprocess.run(
        [*COMPOSE, "exec", "-T", "app", "python", "-c", script],
        input=credential + "\n",
        env=environment,
        text=True,
        check=True,
    )


def check(credentials, environment):
    deadline = time.monotonic() + 55
    while True:
        try:
            status, _ = request("/api/health")
            if status == 200:
                break
        except URLError, TimeoutError, HTTPException:
            pass
        if time.monotonic() >= deadline:
            raise RuntimeError("Grafana did not become ready.")
        time.sleep(0.5)
    status, _ = request(
        "/api/datasources",
        {
            "name": "House Bills API",
            "uid": DATASOURCE_UID,
            "type": PLUGIN,
            "access": "proxy",
            "jsonData": {"auth_method": "bearerToken", "allowedHosts": ["http://app:8000"]},
            "secureJsonData": {"bearerToken": credentials["reporting"]},
        },
    )
    assert status == 200, "Datasource creation failed."
    for ids, expected in (
        ([], 551 * 125 + 550 * 250),
        ([1], 551 * 125),
        ([2], 550 * 250),
        ([1, 2], 551 * 125 + 550 * 250),
    ):
        for kind, mode, total in (
            ("costs", "invoice_month", expected),
            ("consumption", "billed_period", expected // 125 * 123),
        ):
            status, result = query(kind, mode, ids)
            assert status == 200, result
            frames = result["results"]["A"]["frames"]
            amounts, timestamps = [], []
            for frame in frames:
                fields = {
                    field["name"]: index for index, field in enumerate(frame["schema"]["fields"])
                }
                amounts.extend(frame["data"]["values"][fields["value"]])
                timestamps.extend(frame["data"]["values"][fields["time"]])
            assert all(value is not None for value in amounts), result
            assert sum(amounts) == total, result
            assert all(value in (1767225600000, 1769904000000) for value in timestamps), timestamps
    status, result = query("costs", "totals", date_from="1900-01-01")
    assert status != 200 or result["results"]["A"].get("error"), "Broad filters must fail visibly."
    dashboard = json.loads(Path("docs/api/grafana-dashboard.json").read_text(encoding="utf-8"))
    status, _ = request("/api/dashboards/db", {"dashboard": dashboard, "overwrite": True})
    assert status == 200, "Dashboard creation failed."
    assert dashboard["timezone"] == "utc" and len(dashboard["panels"]) == 2
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "house-bills-api-check_default",
            "--mount",
            f"type=bind,source={Path.cwd()},target=/app,readonly",
            "--entrypoint",
            "node",
            "house-bills:browser",
            "tests/browser/api_grafana.cjs",
        ],
        check=True,
    )
    check_live_updates(credentials["automation"])
    check_sequential_example(credentials["automation"], environment)
    print(
        "Grafana 13.2.4 / Infinity 4.1.6: complete totals, property filters, "
        "UTC billing endpoints, live approvals/corrections and visible filter limits passed."
    )


def main():
    credentials = {profile: secrets.token_urlsafe(32) for profile in ("reporting", "automation")}
    # Docker metadata may persist environment values: send only digests to Compose.
    environment = os.environ | {
        f"HOUSE_BILLS_CHECK_{profile.upper()}_TOKEN_DIGEST": hashlib.sha256(
            credential.encode("ascii")
        ).hexdigest()
        for profile, credential in credentials.items()
    }
    try:
        subprocess.run([*COMPOSE, "up", "-d"], env=environment, check=True)
        check(credentials, environment)
    finally:
        # Only the explicitly named check project's containers and volume are removed.
        subprocess.run([*COMPOSE, "down", "--volumes"], env=environment, check=True)


if __name__ == "__main__":
    main()
