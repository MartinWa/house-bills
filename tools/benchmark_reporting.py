"""Measure reporting on a repeatable synthetic dataset in disposable Linux state."""

import argparse
import json
import os
import statistics
import tempfile
import time
from datetime import UTC, date, datetime
from pathlib import Path


def seed():
    from django.contrib.auth import get_user_model

    from bills.models import BillingSection, Document, Invoice, Property, Usage, Vendor

    owner = get_user_model().objects.create_user("synthetic-benchmark")
    houses = Property.objects.bulk_create(
        [Property(name=f"Benchmark house {i}", slug=f"benchmark-{i}") for i in (1, 2)]
    )
    vendor = Vendor.objects.create(name="Synthetic utility", slug="synthetic-utility")
    periods = [
        (year, month, house)
        for year in range(2014, 2027)
        for month in range(1, 13)
        for house in houses
    ]
    documents = Document.objects.bulk_create(
        [Document(source="manual", storage_state="none", state="ready") for _ in periods]
    )
    invoices = Invoice.objects.bulk_create(
        [
            Invoice(
                document=document,
                vendor=vendor,
                reference=f"BENCHMARK-{i}",
                invoice_date=date(year, month, 28),
                status="approved",
                review_revision=1,
                approved_at=datetime(2026, 11, 1, tzinfo=UTC),
                approved_by=owner,
                net_minor=50000,
                vat_minor=12500,
                rounding_minor=0,
                invoice_total_minor=62500,
                amount_due_minor=62500,
            )
            for i, (document, (year, month, _)) in enumerate(zip(documents, periods, strict=True))
        ]
    )
    sections = []
    for invoice, (year, month, house) in zip(invoices, periods, strict=True):
        end = date(year + (month == 12), month % 12 + 1, 1)
        for category, subcategory in (
            ("electricity", "electricity_network"),
            ("electricity", "electricity_supply"),
            ("electricity", "electricity_demand"),
            ("water", None),
            ("waste", "waste_collection"),
        ):
            sections.append(
                BillingSection(
                    invoice=invoice,
                    property=house,
                    section_id=f"{category}-{subcategory}",
                    category=category,
                    subcategory_id=subcategory,
                    start=date(year, month, 1),
                    end=end,
                    net_minor=10000,
                    vat_minor=2500,
                    gross_minor=12500,
                )
            )
    sections = BillingSection.objects.bulk_create(sections)
    Usage.objects.bulk_create(
        [
            Usage(
                section=section,
                position=0,
                utility=section.category,
                subcategory_id=section.subcategory_id
                if section.category == "electricity"
                else None,
                unit="kW"
                if section.subcategory_id == "electricity_demand"
                else {"electricity": "kWh", "water": "m3", "waste": "kg"}[section.category],
                quantity_milli=5000 if section.subcategory_id == "electricity_demand" else 100000,
                basis="billed" if section.subcategory_id == "electricity_demand" else "measured",
                start=section.start,
                end=section.end,
            )
            for section in sections
        ]
    )
    return houses[0].pk


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reporting-benchmark-", dir="/state/tmp") as state:
        os.environ["HOUSE_BILLS_DATA_DIR"] = state
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "house_bills.settings.development")
        import django

        django.setup()
        from django.core.management import call_command

        from bills.api import schemas
        from bills.api_reporting import read_api_report

        call_command("migrate", verbosity=0, interactive=False)
        house = seed()
        cases = [
            ("overview", "summary", None, "totals", date(2026, 1, 1)),
            ("three_year_electricity", "consumption", "electricity", "grouped", date(2024, 1, 1)),
            ("full_history_monthly_costs", "costs", None, "grouped", date(2014, 1, 1)),
        ]
        results = []
        for name, kind, category, mode, start in cases:
            options = dict(
                filters={
                    "property_id": [house],
                    "vendor_id": [],
                    "date_from": start,
                    "date_to": date(2026, 11, 15),
                    "category": category,
                    "subcategory": None,
                },
                kind=kind,
                mode=mode,
            )
            if mode == "grouped":
                options["group_by"] = (
                    ["period_start", "utility", "unit"]
                    if kind == "consumption"
                    else ["invoice_month", "category", "subcategory", "currency"]
                )
            if args.compact and kind != "summary":
                options.update(representation="compact", include_totals=False, include_waste=False)
            timings = []
            for _ in range(6):
                before = time.perf_counter()
                result = read_api_report(**options)
                timings.append((time.perf_counter() - before) * 1000)
            if args.compact and kind != "summary":
                payload = (
                    schemas.AnalyticalAggregateOut.model_validate(result)
                    .model_dump_json(exclude_unset=True)
                    .encode()
                )
            else:
                schema = {
                    "summary": schemas.SummaryOut,
                    "costs": schemas.CostAggregateOut,
                    "consumption": schemas.AggregateOut,
                }[kind]
                payload = schema.model_validate(result).model_dump_json().encode()
            results.append(
                {
                    "request": name,
                    "median_ms": round(statistics.median(timings[1:]), 2),
                    "payload_bytes": len(payload),
                    "group_count": result.get("group_count"),
                    "samples_ms": [round(value, 2) for value in timings[1:]],
                }
            )
        report = {
            "dataset": "312 invoices / 1560 allocations / 1560 usage rows; two houses, 2014–2026",
            "representation": "compact" if args.compact else "default",
            "results": results,
        }
        encoded = json.dumps(report, indent=2) + "\n"
        print(encoded)
        if args.output:
            args.output.write_text(encoded)


if __name__ == "__main__":
    main()
