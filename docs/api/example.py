"""Sequential automation example for disposable installations and invented invoices."""

import argparse
import json
import os
from datetime import date
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8001/api/v1")
    parser.add_argument(
        "--pdf", type=Path, required=True, help="An invented supported PDF with complete values"
    )
    args = parser.parse_args()
    secret = os.environ["HOUSE_BILLS_API_TOKEN"]

    def call(method, path, body=None, *, content_type="application/json"):
        payload = (
            json.dumps(body).encode()
            if body is not None and content_type == "application/json"
            else body
        )
        headers = {"Authorization": f"Bearer {secret}"}
        if payload is not None:
            headers["Content-Type"] = content_type
        url = (
            args.base_url.rstrip("/") + path
            if not path.startswith("/api/")
            else args.base_url.split("/api/")[0] + path
        )
        with urlopen(
            Request(url, method=method, data=payload, headers=headers), timeout=60
        ) as response:
            return json.load(response)

    house = call("POST", "/properties", {"name": "Example house", "slug": "example-house"})
    vendor = call(
        "POST",
        "/vendors",
        {
            "name": "Example utilities",
            "slug": "example-utilities",
            "default_property_id": house["id"],
        },
    )
    call("GET", "/metadata")
    call("PUT", f"/vendors/{vendor['id']}/invoice-formats/layout_02")
    call("POST", f"/vendors/{vendor['id']}/identifiers", {"kind": "vat", "value": "SE000000999901"})
    service = call(
        "POST",
        f"/properties/{house['id']}/services",
        {
            "category": "electricity",
            "subcategory": "electricity_supply",
            "start_month": "2026-01-01",
        },
    )
    call(
        "POST",
        f"/services/{service['id']}/vendor-assignments",
        {"vendor_id": vendor["id"], "start_month": "2026-01-01"},
    )
    call(
        "POST",
        f"/vendors/{vendor['id']}/billing-schedules",
        {"start_month": "2026-01-01", "first_expected_month": "2026-01-01"},
    )
    boundary = "house-bills-example-boundary"
    multipart = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="pdf"; '
            'filename="example.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
        ).encode()
        + args.pdf.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    document = call(
        "POST", "/documents", multipart, content_type=f"multipart/form-data; boundary={boundary}"
    )["document"]
    source = call("GET", document["links"]["review"])
    print(json.dumps(source["edits"], indent=2))
    if source["warnings"] or not source["edits"]["sections"]:
        raise SystemExit("Resolve the proposal warnings/missing allocations before approval.")
    if input("Approve these reviewed values from the invented PDF? Type APPROVE: ") != "APPROVE":
        return
    review = call(
        "POST",
        document["links"]["reviews"],
        {"review_token": source["review_token"], "edits": source["edits"]},
    )
    call("GET", review["links"]["self"])
    source = call("GET", document["links"]["review"])
    source["edits"]["note"] = "Example retained correction history; values unchanged."
    call(
        "POST",
        document["links"]["reviews"],
        {"review_token": source["review_token"], "edits": source["edits"]},
    )
    print(
        json.dumps(
            call(
                "GET",
                f"/reports/costs?mode=totals&property_id={house['id']}&date_to={date.today().isoformat()}",
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
