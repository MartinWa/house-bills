"""Approved reporting semantics through ordinary, read-only SQLite queries."""

import copy
import hashlib
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db import transaction
from django.urls import reverse

from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Property,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing import layout_02
from bills.parsing.reader import read_pdf
from bills.parsing.types import Candidate, Evidence, Section
from bills.parsing.types import Usage as ProposedUsage
from bills.reporting import read_report, reporting_reader
from bills.services import approve_review, record_extraction
from bills.storage import ingest_pdf

from .synthetic import shared_supply, write_pdf

pytestmark = pytest.mark.django_db(transaction=True)

START, END = date(2024, 2, 1), date(2024, 3, 1)
EVIDENCE = Evidence(1, "Wholly synthetic reporting evidence", (0, 0, 100, 20))


def usage(
    quantity=100_125,
    *,
    start=START,
    end=END,
    basis="measured",
    utility="electricity",
    subcategory="",
):
    return ProposedUsage(
        utility=utility,
        unit={"electricity": "kWh", "water": "m3", "waste": "kg"}[utility],
        quantity_milli=quantity,
        start=start,
        end=end,
        basis=basis,
        evidence=EVIDENCE,
        subcategory=subcategory,
    )


def allocation(
    house, *, service="electricity_network", usages=(), net=10_000, start=START, end=END
):
    return {
        "house": house,
        "service": service,
        "usages": usages,
        "net": net,
        "start": start,
        "end": end,
    }


@pytest.fixture
def ledger(tmp_path, settings, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "originals"
    actor = django_user_model.objects.create_user("synthetic-report-reviewer")
    houses = [
        Property.objects.create(name=f"Synthetic House {i}", slug=f"house-{i}") for i in (1, 2)
    ]
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    candidates, assignments = {}, {}

    def make(
        specifications, *, invoice_date=date(2024, 3, 5), rounding=0, kind="invoice", issuer=None
    ):
        number = Document.objects.count() + 1
        document = Document.objects.create(
            sha256=hashlib.sha256(f"wholly-synthetic-report-{number}".encode()).hexdigest(),
            original_filename=f"synthetic-{number}.pdf",
        )
        invoice = Invoice.objects.create(document=document)
        sections = [
            Section(
                section_id=f"section-{position}",
                service=spec["service"],
                identifiers=[],
                usages=list(spec["usages"]),
                start=spec["start"],
                end=spec["end"],
                net_minor=spec["net"],
                vat_minor=spec["net"] // 4,
                gross_minor=spec["net"] + spec["net"] // 4,
                period_evidence=EVIDENCE,
                tax_evidence=EVIDENCE,
                total_evidence=EVIDENCE,
            )
            for position, spec in enumerate(specifications)
        ]
        net = sum(section.net_minor for section in sections)
        vat = sum(section.vat_minor for section in sections)
        candidate = Candidate(
            layout_family="layout_01",
            layout_version="v1",
            parser_revision="synthetic-report",
            reference=f"SYNTHETIC-REPORT-{number}",
            invoice_date=invoice_date,
            due_date=None,
            currency="SEK",
            net_minor=net,
            vat_minor=vat,
            rounding_minor=rounding,
            invoice_total_minor=net + vat + rounding,
            amount_due_minor=net + vat + rounding,
            header_evidence={},
            sections=sections,
        )
        properties = {
            section.section_id: spec["house"]
            for section, spec in zip(sections, specifications, strict=True)
        }
        candidates[invoice.pk], assignments[invoice.pk] = candidate, properties
        return approve_review(
            invoice.pk,
            expected_revision=0,
            candidate=candidate,
            vendor=issuer or vendor,
            section_properties=properties,
            actor=actor,
            kind=kind,
        )

    def correct(invoice, candidate):
        return approve_review(
            invoice.pk,
            expected_revision=invoice.review_revision,
            candidate=candidate,
            vendor=invoice.vendor,
            section_properties=assignments[invoice.pk],
            actor=actor,
            kind=invoice.kind,
        )

    return {
        "make": make,
        "correct": correct,
        "houses": houses,
        "vendor": vendor,
        "actor": actor,
        "candidates": candidates,
    }


def rows(view):
    with reporting_reader() as reader:
        return [dict(row) for row in reader.execute(f"SELECT * FROM {view}")]


def test_review_records_and_private_proposals_are_absent_from_all_reporting_views(ledger):
    draft = Invoice.objects.create(
        document=Document.objects.create(sha256="a" * 64, original_filename="synthetic-draft.pdf")
    )
    section = BillingSection.objects.create(
        invoice=draft,
        section_id="draft",
        property=ledger["houses"][0],
        service="electricity_network",
        net_minor=100,
        vat_minor=25,
        gross_minor=125,
        start=START,
        end=END,
    )
    Usage.objects.create(
        section=section,
        position=0,
        utility="electricity",
        unit="kWh",
        quantity_milli=1000,
        start=START,
        end=END,
        basis="measured",
        evidence={"page": 1, "text": "Private synthetic candidate", "bbox": [0, 0, 1, 1]},
    )
    record_extraction(draft.document_id, error="Synthetic unsupported PDF")
    for view in (
        "reporting_invoices",
        "reporting_costs",
        "reporting_usage",
        "reporting_electricity_selection",
        "reporting_electricity_usage",
    ):
        assert rows(view) == []
    assert read_report() == {"costs": [], "consumption": [], "breakdown": [], "issues": []}


def test_costs_and_usage_have_independent_grains_exact_values_and_native_timestamps(ledger):
    house = ledger["houses"][0]
    split = date(2024, 2, 15)
    invoice = ledger["make"](
        [
            allocation(house, usages=[usage(25_125, end=split), usage(75_000, start=split)]),
            allocation(
                house, service="water", usages=[usage(-1500, utility="water", basis="reversal")]
            ),
            allocation(house, service="other", net=2000),
        ],
        rounding=42,
    )
    costs, billed = rows("reporting_costs"), rows("reporting_usage")
    assert len(costs) == 3 and len(billed) == 3
    assert sum(row["gross_minor"] for row in costs) == 27_500
    header = rows("reporting_invoices")[0]
    assert header["invoice_total_minor"] == 27_542 and header["rounding_minor"] == 42
    assert header["amount_due_minor"] == invoice.amount_due_minor
    assert all("rounding_minor" not in row and "invoice_total_minor" not in row for row in costs)
    assert costs[0]["invoice_time"] == int(datetime(2024, 3, 5, tzinfo=UTC).timestamp())
    assert costs[0]["period_start_time"] == int(datetime(2024, 2, 1, tzinfo=UTC).timestamp())
    assert costs[0]["period_end"] == "2024-03-01"
    assert all(type(row["approved_time"]) is int for row in costs)
    assert {(row["period_start"], row["period_end"]) for row in billed} == {
        ("2024-02-01", "2024-02-15"),
        ("2024-02-15", "2024-03-01"),
        ("2024-02-01", "2024-03-01"),
    }
    assert next(row for row in billed if row["utility"] == "water")["quantity_milli"] == -1500
    assert all(row["source_page"] == 1 for row in billed)
    assert not {"identifiers", "evidence", "original_path", "approved_by_id"} & billed[0].keys()
    report = read_report()
    assert sum(row["gross_minor"] for row in report["costs"]) == 27_500
    assert {(row["utility"], row["quantity_milli"]) for row in report["consumption"]} == {
        ("electricity", 100_125),
        ("water", -1500),
    }


def test_shared_invoice_stays_one_header_with_distinct_property_service_costs(ledger):
    first, second = ledger["houses"]
    ledger["make"](
        [
            allocation(first, usages=[usage()]),
            allocation(
                first,
                service="electricity_supply",
                net=8000,
                usages=[usage(100_125, basis="billed")],
            ),
            allocation(
                second,
                service="electricity_supply",
                net=4000,
                usages=[usage(50_000, basis="billed")],
            ),
        ],
        rounding=-1,
    )
    with reporting_reader() as reader:
        totals = reader.execute(
            """
            SELECT property_id, sum(gross_minor) FROM reporting_costs
            WHERE service IN ('electricity_network', 'electricity_supply')
            GROUP BY property_id ORDER BY property_id
            """
        ).fetchall()
        assert [tuple(row) for row in totals] == [(first.pk, 22_500), (second.pk, 5000)]
        assert reader.execute("SELECT count(*) FROM reporting_invoices").fetchone()[0] == 1
    assert len(rows("reporting_usage")) == 3
    selected = rows("reporting_electricity_usage")
    assert {
        (row["property_id"], row["source_selection"], row["quantity_milli"]) for row in selected
    } == {(first.pk, "network", 100_125), (second.pk, "supply_fallback", 50_000)}


def test_network_preference_handles_multiple_readings_and_later_invoice_dates(ledger):
    house = ledger["houses"][0]
    split = date(2024, 2, 15)
    ledger["make"](
        [allocation(house, service="electricity_supply", usages=[usage(99_000, basis="billed")])]
    )
    ledger["make"](
        [allocation(house, usages=[usage(25_125, end=split), usage(75_000, start=split)])],
        invoice_date=date(2024, 4, 5),
    )
    selected = rows("reporting_electricity_selection")
    assert [
        row["source_selection"] for row in selected if row["source_service"] == "electricity_supply"
    ] == ["network_preferred"]
    assert sum(row["quantity_milli"] for row in rows("reporting_electricity_usage")) == 100_125
    report = read_report(date_from=date(2024, 2, 10), date_to=date(2024, 2, 20))
    assert report["costs"] == [] and report["consumption"][0]["quantity_milli"] == 100_125
    assert report["issues"] == []


@pytest.mark.parametrize(
    "network_periods",
    [
        [(date(2024, 2, 10), END)],
        [(START, date(2024, 2, 20))],
        [(START, date(2024, 2, 10)), (date(2024, 2, 15), END)],
    ],
)
def test_partial_coverage_and_internal_gaps_exclude_supply_without_proration(
    ledger, network_periods
):
    house = ledger["houses"][0]
    ledger["make"]([allocation(house, service="electricity_supply", usages=[usage()])])
    ledger["make"](
        [
            allocation(
                house,
                usages=[usage(20_000, start=start, end=end) for start, end in network_periods],
            )
        ]
    )
    report = read_report()
    assert len(report["issues"]) == 1
    assert report["issues"][0]["selection_reason"] == "partial_network_coverage"
    assert report["consumption"][0]["quantity_milli"] == 20_000 * len(network_periods)
    assert all(
        row["source_service"] == "electricity_network"
        for row in rows("reporting_electricity_usage")
    )


def test_covering_network_period_suppresses_supply_and_adjacent_supply_is_fallback(ledger):
    house = ledger["houses"][0]
    ledger["make"]([allocation(house, usages=[usage(200_000, start=date(2024, 1, 1))])])
    ledger["make"]([allocation(house, service="electricity_supply", usages=[usage(100_000)])])
    ledger["make"](
        [
            allocation(
                house,
                service="electricity_supply",
                start=END,
                end=date(2024, 4, 1),
                usages=[usage(150_000, start=END, end=date(2024, 4, 1))],
            )
        ]
    )
    assert {
        (row["source_selection"], row["quantity_milli"])
        for row in rows("reporting_electricity_usage")
    } == {("network", 200_000), ("supply_fallback", 150_000)}
    assert read_report()["issues"] == []


@pytest.mark.parametrize("service", ["electricity_network", "electricity_supply"])
def test_uncancelled_same_source_overlap_is_visible_and_never_summed(ledger, service):
    house = ledger["houses"][0]
    ledger["make"]([allocation(house, service=service, usages=[usage(basis="estimated")])])
    duplicate = ledger["make"]([allocation(house, service=service, usages=[usage(110_000)])])
    if service == "electricity_network":
        ledger["make"]([allocation(house, service="electricity_supply", usages=[usage(100_000)])])
    report = read_report()
    assert report["consumption"] == []
    assert {row["selection_reason"] for row in report["issues"]} == (
        {"same_source_overlap", "network_source_overlap"}
        if service == "electricity_network"
        else {"same_source_overlap"}
    )
    candidate = copy.deepcopy(ledger["candidates"][duplicate.pk])
    candidate.sections[0].usages[0] = replace(
        candidate.sections[0].usages[0], start=END, end=date(2024, 4, 1)
    )
    ledger["correct"](duplicate, candidate)
    assert read_report()["issues"] == []
    assert len(rows("reporting_electricity_usage")) == 2


@pytest.mark.parametrize("service", ["electricity_network", "electricity_supply"])
def test_full_signed_reversal_and_replacement_keep_all_rows_and_one_net_quantity(ledger, service):
    house = ledger["houses"][0]
    ledger["make"]([allocation(house, service=service, usages=[usage(100_000, basis="estimated")])])
    ledger["make"](
        [
            allocation(
                house,
                service=service,
                net=-10_000,
                usages=[usage(-100_000, basis="reversal"), usage(110_125)],
            )
        ],
        kind="credit",
    )
    selected = rows("reporting_electricity_usage")
    assert len(selected) == 3
    assert sorted(row["quantity_milli"] for row in selected) == [-100_000, 100_000, 110_125]
    assert read_report()["consumption"][0]["quantity_milli"] == 110_125
    assert read_report()["issues"] == []
    assert sum(row["gross_minor"] for row in rows("reporting_costs")) == 0


def test_one_reversal_cannot_cancel_multiple_positive_records(ledger):
    house = ledger["houses"][0]
    for _ in range(3):
        ledger["make"]([allocation(house, usages=[usage(100_000)])])
    ledger["make"](
        [allocation(house, net=-1000, usages=[usage(-100_000, basis="reversal")])], kind="credit"
    )
    assert read_report()["consumption"] == []
    assert len(read_report()["issues"]) == 4


def test_reversal_from_another_vendor_does_not_resolve_ambiguous_positive_records(ledger):
    house = ledger["houses"][0]
    other = Vendor.objects.create(name="Another Synthetic Vendor", slug="another-vendor")
    for quantity in (100_000, 110_000):
        ledger["make"]([allocation(house, usages=[usage(quantity)])])
    ledger["make"](
        [allocation(house, net=-1000, usages=[usage(-100_000, basis="reversal")])],
        kind="credit",
        issuer=other,
    )
    assert read_report()["consumption"] == []
    assert len(read_report()["issues"]) == 3


def test_standalone_signed_credits_and_different_utilities_keep_units_and_bases(ledger):
    house = ledger["houses"][0]
    ledger["make"](
        [
            allocation(house, net=-1000, usages=[usage(-5000, basis="reversal")]),
            allocation(
                house, service="water", usages=[usage(12_500, utility="water", basis="billed")]
            ),
            allocation(
                house,
                service="waste",
                usages=[usage(19_500, utility="waste", basis="billed_weight")],
            ),
        ],
        kind="credit",
    )
    report = read_report()
    assert {
        (row["utility"], row["unit"], row["quantity_milli"]) for row in report["consumption"]
    } == {("electricity", "kWh", -5000), ("water", "m3", 12_500), ("waste", "kg", 19_500)}
    assert {row["basis"] for row in report["breakdown"]} == {"reversal", "billed", "billed_weight"}


def test_waste_subcategories_keep_separate_signed_totals_and_electricity_categories(client, ledger):
    house = ledger["houses"][0]
    ledger["make"](
        [
            allocation(house, usages=[usage(100_125)]),
            allocation(house, service="electricity_supply", usages=[usage(99_000, basis="billed")]),
            allocation(
                house,
                service="waste",
                usages=[
                    usage(7500, utility="waste", basis="billed_weight", subcategory="restavfall"),
                    usage(12000, utility="waste", basis="billed_weight", subcategory="matavfall"),
                    usage(-2000, utility="waste", basis="reversal", subcategory="matavfall"),
                    usage(1500, utility="waste", basis="billed_weight"),
                ],
            ),
        ]
    )
    report = read_report()
    assert {
        (row["subcategory"], row["quantity_milli"])
        for row in report["consumption"]
        if row["utility"] == "waste"
    } == {
        ("restavfall", 7500),
        ("matavfall", 10000),
        ("", 1500),
    }
    electricity = [row for row in report["consumption"] if row["utility"] == "electricity"]
    assert len(electricity) == 1 and electricity[0]["quantity_milli"] == 100_125
    assert {
        (row["category"], row["subcategory"], row["subcategory_label"]) for row in report["costs"]
    } == {
        ("electricity", "electricity_network", "Elnät"),
        ("electricity", "electricity_supply", "Elhandel"),
        ("waste", "", ""),
    }
    assert {
        (row["subcategory"], row["basis"], row["quantity_milli"])
        for row in report["breakdown"]
        if row["utility"] == "waste"
    } == {
        ("restavfall", "billed_weight", 7500),
        ("matavfall", "billed_weight", 12000),
        ("matavfall", "reversal", -2000),
        ("", "billed_weight", 1500),
    }
    assert {row["subcategory"] for row in rows("reporting_usage")} == {
        "electricity_network",
        "electricity_supply",
        "restavfall",
        "matavfall",
        "",
    }
    client.force_login(ledger["actor"])
    body = client.get(reverse("bills:reports")).content.decode()
    assert all(label in body for label in ("Restavfall", "Matavfall", "Elnät", "Elhandel"))


def test_approval_correction_and_failed_save_refresh_without_repeating_history(ledger):
    invoice = ledger["make"]([allocation(ledger["houses"][0], usages=[usage()])])
    old_usage_id = rows("reporting_usage")[0]["usage_id"]
    candidate = copy.deepcopy(ledger["candidates"][invoice.pk])
    candidate.sections[0].usages[0] = replace(
        candidate.sections[0].usages[0], quantity_milli=111_125
    )
    candidate.sections[0].net_minor += 100
    candidate.sections[0].gross_minor += 100
    candidate.net_minor += 100
    candidate.invoice_total_minor += 100
    candidate.amount_due_minor += 100
    approved = ledger["correct"](invoice, candidate)
    assert approved.review_revision == 2 and approved.reviews.count() == 2
    assert rows("reporting_usage")[0]["usage_id"] != old_usage_id
    assert read_report()["consumption"][0]["quantity_milli"] == 111_125
    assert read_report()["costs"][0]["gross_minor"] == 12_600
    assert rows("reporting_costs")[0]["review_revision"] == 2
    record_extraction(invoice.document_id, candidate)
    assert len(rows("reporting_costs")) == 1 and len(rows("reporting_usage")) == 1
    candidate.sections[0].gross_minor += 1
    before = read_report()
    with pytest.raises(ValidationError):
        ledger["correct"](approved, candidate)
    assert read_report() == before


def test_exact_duplicates_and_different_file_identity_conflicts_do_not_repeat_reports(
    ledger, tmp_path
):
    vendor = ledger["vendor"]
    vendor.default_property = ledger["houses"][0]
    vendor.save()
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000999901")
    VendorLayoutFamily.objects.create(vendor=vendor, family="layout_02")
    pages = shared_supply()
    pages[0].append("VAT-nr SE000000999901")
    path = write_pdf(tmp_path / "synthetic-shared.pdf", pages)
    candidate = layout_02.parse(read_pdf(path))
    with path.open("rb") as source:
        document, duplicate = ingest_pdf(source, filename="house-one/synthetic.pdf")
    assert not duplicate
    invoice = Invoice.objects.get(document=document)
    approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={
            section.section_id: house
            for section, house in zip(candidate.sections, ledger["houses"], strict=True)
        },
        actor=ledger["actor"],
        extraction=document.extractions.latest("revision"),
    )
    before = read_report()
    with path.open("rb") as source:
        repeated, duplicate = ingest_pdf(source, filename="house-two/renamed.pdf")
    assert duplicate and repeated.pk == document.pk and read_report() == before
    pages[0].append("Wholly synthetic alternate file with the same invoice identity")
    changed = write_pdf(tmp_path / "synthetic-conflict.pdf", pages)
    with changed.open("rb") as source:
        conflict, duplicate = ingest_pdf(source, filename="synthetic-conflict.pdf")
    assert not duplicate and conflict.pk != document.pk
    assert Invoice.objects.get(document=conflict).status == "review"
    assert len(rows("reporting_invoices")) == 1
    assert len(rows("reporting_costs")) == len(rows("reporting_usage")) == 2
    assert read_report() == before


def test_documented_sql_examples_preserve_totals_and_periods_in_plain_sqlite(ledger):
    first, second = ledger["houses"]
    ledger["make"](
        [
            allocation(first, usages=[usage(100_125)]),
            allocation(first, service="electricity_supply", usages=[usage(99_000, basis="billed")]),
            allocation(
                second,
                service="water",
                net=-1000,
                usages=[usage(-1250, utility="water", basis="reversal")],
            ),
        ],
        rounding=42,
    )
    sql = Path("docs/reporting-examples.sql").read_text(encoding="utf-8")
    results, statement = [], ""
    with reporting_reader() as reader:
        for line in sql.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                results.append([dict(row) for row in reader.execute(statement)])
                statement = ""
    assert not statement.strip() and len(results) == 6
    assert sum(row["gross_minor"] for row in results[0]) == 23_750
    assert results[1][0]["gross_minor"] == 25_000
    assert results[2][0]["quantity_milli"] == -1250
    assert results[3][0]["quantity_milli"] == 100_125
    assert results[4] == []
    assert results[5][0]["allocated_gross_minor"] + results[5][0]["rounding_minor"] == 23_792
    assert results[5][0]["reconciles"] == 1


def test_reader_is_read_only_and_holds_one_snapshot_during_normal_approval_writes(ledger):
    invoice = ledger["make"]([allocation(ledger["houses"][0], usages=[usage(100_000)])])
    candidate = copy.deepcopy(ledger["candidates"][invoice.pk])
    candidate.sections[0].usages[0] = replace(
        candidate.sections[0].usages[0], quantity_milli=120_000
    )
    with reporting_reader() as reader:
        assert reader.execute("SELECT quantity_milli FROM reporting_usage").fetchone()[0] == 100_000
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("UPDATE bills_invoice SET reference = 'SYNTHETIC-UNAUTHORIZED'")
        with transaction.atomic():
            ledger["correct"](invoice, candidate)
            assert read_report()["consumption"][0]["quantity_milli"] == 100_000
        assert reader.execute("SELECT quantity_milli FROM reporting_usage").fetchone()[0] == 100_000
        assert read_report()["consumption"][0]["quantity_milli"] == 120_000
    with reporting_reader() as reader:
        with pytest.raises(sqlite3.OperationalError, match="view"):
            reader.execute("DELETE FROM reporting_usage")


def test_reports_require_login_reject_post_and_never_cache(client, ledger):
    url = reverse("bills:reports")
    assert client.get(url).status_code == 302
    client.force_login(ledger["actor"])
    response = client.get(url)
    assert response.status_code == 200
    assert "no-store" in response["Cache-Control"] and "private" in response["Cache-Control"]
    assert b"No approved costs" in response.content
    assert client.post(url).status_code == 405


def test_invoice_date_filters_are_inclusive_and_combine_with_allocations(client, ledger):
    first, second = ledger["houses"]
    wanted = ledger["make"](
        [allocation(first, service="water"), allocation(first, service="waste")],
        invoice_date=date(2024, 3, 5),
    )
    ledger["make"]([allocation(first, service="water")], invoice_date=date(2024, 3, 4))
    ledger["make"]([allocation(second, service="water")], invoice_date=date(2024, 3, 5))
    ledger["make"]([allocation(first, service="waste")], invoice_date=date(2024, 3, 6))
    client.force_login(ledger["actor"])
    filters = {
        "property": first.pk,
        "service": "water",
        "date_from": "2024-03-05",
        "date_to": "2024-03-05",
    }
    response = client.get(reverse("bills:invoices"), filters)
    assert [row.pk for row in response.context["page"]] == [wanted.pk]
    # Both allocations remain visible under one property heading.
    body = response.content.decode()
    assert body.count(f"<strong>{first.name}</strong>") == 1
    assert "Water" in body and "Waste" in body
    assert (
        client.get(reverse("bills:invoices"), {"date_to": "2024-03-04"})
        .context["page"]
        .paginator.count
        == 1
    )
    invalid = client.get(
        reverse("bills:invoices"), {"date_from": "2024-03-06", "date_to": "2024-03-05"}
    )
    assert invalid.status_code == 400 and invalid.context["form"].errors
    assert invalid.context["page"].paginator.count == 0


def test_report_filters_separate_invoice_dates_from_whole_usage_periods(client, ledger):
    first, second = ledger["houses"]
    ledger["make"](
        [allocation(first, usages=[usage()]), allocation(second, usages=[usage(200_000)])]
    )
    client.force_login(ledger["actor"])
    response = client.get(
        reverse("bills:reports"),
        {"property": first.pk, "date_from": "2024-02-15", "date_to": "2024-02-15"},
    )
    assert response.status_code == 200
    assert response.context["costs"] == []
    assert response.context["consumption"][0]["quantity_milli"] == 100_125
    assert len(response.context["consumption"]) == 1
    response = client.get(
        reverse("bills:reports"), {"date_from": "2024-03-01", "date_to": "2024-03-05"}
    )
    assert response.context["consumption"] == [] and len(response.context["costs"]) == 2


@pytest.mark.parametrize(
    "filters",
    [
        {"date_from": "2024-03-05", "date_to": "2024-02-01"},
        {"date_from": "invalid"},
        {"property": "999999"},
    ],
)
def test_invalid_report_filters_show_errors_and_no_totals(client, ledger, filters):
    ledger["make"]([allocation(ledger["houses"][0], usages=[usage()])])
    client.force_login(ledger["actor"])
    response = client.get(reverse("bills:reports"), filters)
    assert response.status_code == 400 and response.context["form"].errors
    assert response.context["costs"] == response.context["consumption"] == []


def test_report_issues_link_to_invoice_review_and_source_and_escape_configured_names(
    client, ledger
):
    house = ledger["houses"][0]
    house.name = "Synthetic <script>house</script>"
    house.save()
    ledger["make"]([allocation(house, usages=[usage()])])
    ledger["make"](
        [allocation(house, service="electricity_supply", usages=[usage(start=date(2024, 1, 1))])]
    )
    client.force_login(ledger["actor"])
    response = client.get(reverse("bills:reports"))
    body = response.content.decode()
    assert response.context["issues"][0]["selection_reason"] == "partial_network_coverage"
    assert (
        "Synthetic &lt;script&gt;house&lt;/script&gt;" in body and "Synthetic <script>" not in body
    )
    assert "2024-01-01 – 2024-02-29" in body
    issue = response.context["issues"][0]
    assert reverse("bills:invoice", args=[issue["invoice_id"]]) in body
    assert reverse("bills:review", args=[issue["document_id"]]) in body
    assert f"{reverse('bills:source_view', args=[issue['document_id']])}#page=1" in body


def test_report_database_failure_shows_retry_feedback_without_partial_totals(
    client, ledger, monkeypatch
):
    def fail(**kwargs):
        raise sqlite3.OperationalError("Synthetic unavailable database")

    monkeypatch.setattr("bills.views.read_report", fail)
    client.force_login(ledger["actor"])
    response = client.get(reverse("bills:reports"))
    assert response.status_code == 503 and b"Reports are unavailable" in response.content
    assert response.context["costs"] == response.context["consumption"] == []
