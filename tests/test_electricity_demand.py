"""Printed demand costs, independent power readings and retained correction history."""

import copy
import hashlib
import io
from dataclasses import asdict, replace
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.urls import reverse

from bills.electricity_demand import plan_retained_demand_split
from bills.models import ApiToken, Document, Property, PropertyIdentifier, Usage, Vendor
from bills.parsing import layout_01, layout_01_v2
from bills.parsing.electricity_demand import split_electricity_demand
from bills.parsing.reader import read_pdf
from bills.parsing.types import Evidence, Identifier
from bills.reporting import read_report
from bills.services import StaleReviewError, approve_review, record_extraction

from .api.helpers import BASE, api_client, manual_body, send, setup  # noqa: F401
from .synthetic import multi_service, write_pdf


def parse(tmp_path, replacement=None, parser=layout_01):
    source = multi_service(split_network_readings=True)
    if replacement is not None:
        source[-1] = [
            replacement if isinstance(line, str) and line.startswith("Effektavgift") else line
            for line in source[-1]
        ]
    if parser == layout_01_v2:
        source = [
            [
                line.replace("Elnät avtal", "Elnätavgifter prodnr")
                if isinstance(line, str)
                else line
                for line in page
            ]
            for page in source
        ]
    pages = read_pdf(write_pdf(tmp_path / "demand.pdf", source))
    return parser.parse(pages), pages


def combined(candidate):
    """Recreate an older combined network approval without billed-power records."""
    network = next(section for section in candidate.sections if section.section_id == "section_04")
    demands = [
        section for section in candidate.sections if section.subcategory == "electricity_demand"
    ]
    for field in ("net_minor", "vat_minor", "gross_minor"):
        setattr(
            network, field, getattr(network, field) + sum(getattr(part, field) for part in demands)
        )
    candidate.sections = [section for section in candidate.sections if section not in demands]
    return candidate


@pytest.mark.parametrize("parser", [layout_01, layout_01_v2])
def test_contract_and_product_formats_separate_costs_and_power_without_changing_kwh(
    tmp_path, parser
):
    candidate, pages = parse(tmp_path, parser=parser)
    assert candidate.issues == []
    network, demand = candidate.sections[-2:]
    assert (network.net_minor, network.vat_minor, network.gross_minor) == (22846, 5712, 28558)
    assert (demand.net_minor, demand.vat_minor, demand.gross_minor) == (15000, 3750, 18750)
    assert demand.subcategory == "electricity_demand"
    assert demand.identifiers == network.identifiers
    assert [(row.unit, row.quantity_milli) for row in network.usages] == [
        ("kWh", 121125),
        ("kWh", 200000),
    ]
    assert [(row.unit, row.quantity_milli, row.basis) for row in demand.usages] == [
        ("kW", 3000, "billed"),
    ]
    assert demand.tax_evidence == network.tax_evidence
    assert demand.total_evidence.text.startswith("Effektavgift")
    assert (
        sum(section.gross_minor for section in candidate.sections) + candidate.rounding_minor
        == 126300
    )
    previous = candidate.to_dict()
    assert split_electricity_demand(candidate, pages) == ({}, [])
    assert candidate.to_dict() == previous


@pytest.mark.parametrize("unit_price", ["kr/kW", "kr /kW", "kr/ kW", "kr / kW"])
@pytest.mark.parametrize(
    "row,expected_power,warning",
    [
        ("Effektavgift 1 feb 2024 - 1 mar 2024 3,138 kW 50,00 kr/kW 150,00 kr", 3138, None),
        ("Effektavgift 1 feb 2024 - 1 mar 2024 0 kW 50,00 kr/kW 0,00 kr", 0, None),
        ("Effektavgift 1 feb 2024 - 1 mar 2024 150,00 kr", None, None),
        ("Effektavgift 1 feb 2024 - 1 mar 2024 50,00 kr/kW 150,00 kr", None, None),
        (
            "Effektavgift 1 feb 2024 - 1 mar 2024 3,1381 kW 50,00 kr/kW 150,00 kr",
            None,
            "Invalid billed power",
        ),
        (
            "Effektavgift 1 feb 2024 - 1 mar 2024 3 kWh 50,00 kr/kW 150,00 kr",
            None,
            "Invalid billed power",
        ),
        (
            "Effektavgift 1 feb 2024 - 1 mar 2024 ?? kW 50,00 kr/kW 150,00 kr",
            None,
            "Invalid billed power",
        ),
    ],
)
def test_power_uses_only_explicit_exact_billed_kw_and_cost_survives_invalid_power(
    tmp_path, row, expected_power, warning, unit_price
):
    candidate, _ = parse(tmp_path, row.replace("kr/kW", unit_price))
    demand = candidate.sections[-1]
    assert demand.subcategory == "electricity_demand"
    assert sum(section.net_minor for section in candidate.sections) == candidate.net_minor
    assert sum(section.vat_minor for section in candidate.sections) == candidate.vat_minor
    assert [usage.quantity_milli for usage in demand.usages] == (
        [expected_power] if expected_power is not None else []
    )
    assert candidate.issues == ([warning + " in section_04"] if warning else [])


@pytest.mark.parametrize(
    "row,warning",
    [
        (
            "Effektavgift 1 feb 2024 - 1 mar 2024 3 kW 50,00 kr/kW ?? kr",
            "charge is missing or invalid",
        ),
        ("Effektavgift 31 feb 2024 - 1 mar 2024 3 kW 50,00 kr/kW 150,00 kr", "day"),
    ],
)
def test_invalid_charge_or_period_keeps_the_combined_network_cost(tmp_path, row, warning):
    candidate, _ = parse(tmp_path, row)
    assert not any(section.subcategory == "electricity_demand" for section in candidate.sections)
    assert candidate.sections[-1].gross_minor == 47308
    assert any(warning in issue for issue in candidate.issues)


def test_multiple_demand_rows_keep_their_own_periods_and_power(tmp_path):
    candidate, pages = parse(tmp_path)
    combined(candidate)
    source = [
        [
            replace(line, text="Effektavgift 1 feb 2024 - 15 feb 2024 2 kW 50,00 kr/kW 50,00 kr")
            if line.text.startswith("Effektavgift")
            else line
            for line in page
        ]
        for page in pages
    ]
    index = next(i for i, line in enumerate(source[-1]) if line.text.startswith("Effektavgift"))
    source[-1].insert(
        index + 1,
        Evidence(
            4, "Effektavgift 15 feb 2024 - 1 mar 2024 4 kW 50,00 kr/kW 100,00 kr", (0, 0, 100, 20)
        ),
    )
    _, warnings = split_electricity_demand(candidate, source)
    assert warnings == []
    assert [
        (part.net_minor, part.usages[0].quantity_milli, part.start, part.end)
        for part in candidate.sections
        if part.subcategory == "electricity_demand"
    ] == [
        (5000, 2000, date(2024, 2, 1), date(2024, 2, 15)),
        (10000, 4000, date(2024, 2, 15), date(2024, 3, 1)),
    ]
    assert sum(part.vat_minor for part in candidate.sections) == candidate.vat_minor


def test_signed_demand_credit_and_vat_are_preserved(tmp_path):
    candidate, pages = parse(tmp_path)
    combined(candidate)
    network = candidate.sections[-1]
    network.net_minor, network.vat_minor, network.gross_minor = -37846, -9462, -47308
    pages = [
        [
            replace(
                line, text=line.text.replace("3 kW", "-3 kW").replace("150,00 kr", "-150,00 kr")
            )
            if line.text.startswith("Effektavgift")
            else replace(line, text="Moms 25 % på -378,46 kr -94,62 kr")
            if line.text.startswith("Moms 25 % på 378,46")
            else line
            for line in page
        ]
        for page in pages
    ]
    _, warnings = split_electricity_demand(candidate, pages)
    assert warnings == []
    demand = candidate.sections[-1]
    assert (demand.net_minor, demand.vat_minor, demand.gross_minor) == (-15000, -3750, -18750)
    assert demand.usages[0].quantity_milli == -3000
    assert candidate.sections[-2].gross_minor + demand.gross_minor == -47308


def test_unmatched_reviewed_network_allocation_is_retained_beside_a_known_split(tmp_path):
    candidate, pages = parse(tmp_path)
    combined(candidate)
    manual = replace(
        candidate.sections[-1],
        section_id="manual_network",
        identifiers=[
            Identifier("contract", "888888", Evidence(1, "Manual allocation", (0, 0, 0, 0)))
        ],
        usages=[],
        net_minor=100,
        vat_minor=25,
        gross_minor=125,
    )
    water = candidate.sections[0]
    water.net_minor -= 100
    water.vat_minor -= 25
    water.gross_minor -= 125
    candidate.sections.append(manual)
    _, warnings = split_electricity_demand(candidate, pages)
    assert warnings == []
    assert candidate.sections[-1] is manual
    assert candidate.sections[-2].subcategory == "electricity_demand"
    assert sum(section.net_minor for section in candidate.sections) == candidate.net_minor
    assert sum(section.vat_minor for section in candidate.sections) == candidate.vat_minor


@pytest.fixture
def retained(tmp_path, settings, django_user_model, request):
    settings.MEDIA_ROOT = tmp_path / "originals"
    candidate, pages = parse(tmp_path, replacement=getattr(request, "param", None))
    combined(candidate)
    house = Property.objects.create(name="Synthetic House", slug="synthetic-house")
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    for section in candidate.sections:
        identifier = section.identifiers[0]
        PropertyIdentifier.objects.create(
            vendor=vendor, property=house, kind=identifier.kind, value=identifier.value
        )
    actor = django_user_model.objects.create_user("synthetic-demand-reviewer", is_staff=True)
    document = Document.objects.create(
        sha256=hashlib.sha256((tmp_path / "demand.pdf").read_bytes()).hexdigest(),
        original_filename="demand.pdf",
    )
    extraction = record_extraction(
        document.pk,
        candidate,
        vendor=vendor,
        page_evidence=[asdict(line) for page in pages for line in page],
    )
    candidate.reference = "reviewed-reference"
    candidate.invoice_date = date(2024, 3, 6)
    candidate.sections[0].usages[0] = replace(candidate.sections[0].usages[0], quantity_milli=13000)
    invoice = approve_review(
        document.invoices.get().pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={},
        actor=actor,
        extraction=extraction,
    )
    return invoice, house, actor, extraction


@pytest.mark.django_db(transaction=True)
def test_retained_split_previews_applies_once_and_keeps_corrections_history_and_reports(
    retained, client
):
    invoice, house, actor, extraction = retained
    old_review = invoice.reviews.get()
    snapshot, original = copy.deepcopy(old_review.snapshot), copy.deepcopy(extraction.candidate)
    output = io.StringIO()
    call_command("split_electricity_demand", property_id=house.pk, stdout=output)
    assert "Would update 1 invoices" in output.getvalue()
    invoice.refresh_from_db()
    assert invoice.review_revision == 1
    call_command("split_electricity_demand", property_id=house.pk, apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    assert (invoice.reference, invoice.invoice_date, invoice.review_revision) == (
        "reviewed-reference",
        date(2024, 3, 6),
        2,
    )
    assert invoice.sections.get(category="water").usages.get().quantity_milli == 13000
    old_review.refresh_from_db()
    extraction.refresh_from_db()
    assert old_review.snapshot == snapshot and extraction.candidate == original
    report = read_report(property_id=house.pk)
    costs = {row["subcategory"]: row for row in report["costs"]}
    assert costs["electricity_network"]["gross_minor"] == 28558
    assert costs["electricity_demand"]["gross_minor"] == 18750
    assert [
        (row["unit"], row["quantity_milli"])
        for row in report["consumption"]
        if row["utility"] == "electricity"
    ] == [("kWh", 321125)]
    assert [row["quantity_milli"] for row in report["billed_power"]] == [3000]
    assert report["issues"] == []
    assert report["total_invoices"] == 1
    client.force_login(actor)
    response = client.get(reverse("bills:reports"))
    assert response.status_code == 200
    assert b"Effektavgift" in response.content and b"3,000 kW" in response.content
    detail = client.get(reverse("bills:invoice", args=[invoice.pk]))
    assert detail.status_code == 200 and b"<h4>Billed power</h4>" in detail.content
    output = io.StringIO()
    call_command("split_electricity_demand", apply=True, reviewer=actor.pk, stdout=output)
    invoice.refresh_from_db()
    assert invoice.review_revision == 2 and "Updated 0 invoices" in output.getvalue()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "retained",
    ["Effektavgift 1 feb 2024 - 1 mar 2024 50,00 kr / kW 150,00 kr"],
    indirect=True,
)
def test_retained_split_applies_spaced_unit_price_without_inventing_billed_power(retained):
    invoice, house, actor, extraction = retained
    plan, warnings = plan_retained_demand_split(invoice, house.pk)
    assert plan is not None and warnings == []
    call_command("split_electricity_demand", apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    extraction.refresh_from_db()
    assert invoice.review_revision == 2
    demand = invoice.sections.get(subcategory_id="electricity_demand")
    assert (demand.net_minor, demand.vat_minor, demand.gross_minor) == (15000, 3750, 18750)
    assert not demand.usages.exists()
    assert invoice.sections.get(subcategory_id="electricity_network").gross_minor == 28558
    assert demand.total_evidence == next(
        row for row in extraction.page_evidence if row["text"].startswith("Effektavgift")
    )


@pytest.mark.django_db(transaction=True)
def test_split_plan_rejects_stale_approvals_and_preserves_reviewed_costs(retained):
    invoice, house, actor, _ = retained
    plan, warnings = plan_retained_demand_split(invoice, house.pk)
    assert plan is not None and warnings == []
    approve_review(invoice.pk, **plan, actor=actor)
    with pytest.raises(StaleReviewError):
        approve_review(invoice.pk, **plan, actor=actor)


@pytest.mark.django_db(transaction=True)
def test_reviewed_network_costs_are_never_overwritten_by_source(retained):
    invoice, house, _, _ = retained
    from bills.waste_costs import reviewed_candidate

    candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
    network = candidate.sections[-1]
    network.net_minor += 1
    network.gross_minor += 1
    _, warnings = split_electricity_demand(
        candidate,
        [
            [
                Evidence(row["page"], row["text"], tuple(row["bbox"]))
                for row in invoice.document.extractions.get().page_evidence
            ]
        ],
    )
    assert "Reviewed network totals differ" in warnings[0]
    assert network.net_minor == 37847
    assert len(candidate.sections) == 4


@pytest.mark.django_db(transaction=True)
def test_apply_requires_a_staff_reviewer_and_existing_property(retained):
    _, house, _, _ = retained
    with pytest.raises(CommandError, match="staff"):
        call_command("split_electricity_demand", property_id=house.pk, apply=True)
    with pytest.raises(CommandError, match="existing property"):
        call_command("split_electricity_demand", property_id=house.pk + 100)


@pytest.mark.django_db(transaction=True)
def test_billed_power_endpoint_is_separate_from_consumption_and_does_not_sum_kw(api_client):  # noqa: F811
    client, _, token, _ = api_client
    house, vendor = setup(client)
    document = send(
        client, "post", "/manual-invoices", {"idempotency_key": "demand-example"}
    ).json()["document"]
    review = client.get(document["links"]["review"]).json()
    body = manual_body(house, vendor)
    body["sections"][0].update(category="electricity", subcategory="electricity_demand")
    body["usages"][0].update(
        utility="electricity", subcategory="electricity_demand", unit="kW", quantity_milli=3138
    )
    result = send(
        client,
        "post",
        f"/documents/{document['id']}/reviews",
        {"review_token": review["review_token"], "edits": body},
    )
    assert result.status_code == 201, result.content
    power = client.get(BASE + f"/reports/billed-power?property_id={house['id']}&limit=1")
    assert power.status_code == 200, power.content
    assert power.json()["count"] == 1
    assert (power.json()["items"][0]["unit"], power.json()["items"][0]["quantity_milli"]) == (
        "kW",
        3138,
    )
    assert client.get(BASE + "/reports/consumption").json()["count"] == 0
    assert client.get(BASE + "/reports/consumption-issues").json()["count"] == 0
    assert client.get(BASE + "/reports/billed-power?mode=totals").status_code == 422
    assert "kW" in client.get(BASE + "/metadata").json()["units"]
    _, secret = ApiToken.issue(name="power reporting", owner=token.owner, profile="reporting")
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {secret}"
    assert client.get(BASE + "/reports/billed-power").status_code == 200


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "subcategory,unit", [("electricity_network", "kW"), (None, "kW"), ("electricity_demand", "kWh")]
)
def test_kw_requires_demand_classification_and_demand_cannot_use_kwh(retained, subcategory, unit):
    invoice, _, _, _ = retained
    row = Usage(
        section=invoice.sections.get(section_id="section_04"),
        position=99,
        utility="electricity",
        subcategory_id=subcategory,
        unit=unit,
        quantity_milli=1000,
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        basis="billed",
    )
    with pytest.raises(ValidationError, match="usage_unit_matches_utility"):
        row.full_clean()
