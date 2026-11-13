"""Printed fraction charges, shared fees, VAT conservation and retained corrections."""

import copy
import hashlib
import io
from dataclasses import asdict, replace
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError

from bills.models import Document, Property, PropertyIdentifier, PropertyService, Vendor
from bills.parsing import layout_01, layout_01_v2
from bills.parsing.common import reconcile
from bills.parsing.reader import read_pdf
from bills.parsing.types import Evidence, Usage
from bills.parsing.waste_costs import allocated_vat, split_waste_costs
from bills.reporting import read_report
from bills.services import StaleReviewError, approve_review, record_extraction
from bills.waste_costs import plan_retained_cost_split, reviewed_candidate

from .synthetic import multi_service, write_pdf


@pytest.fixture
def parsed(tmp_path):
    pages = read_pdf(write_pdf(tmp_path / "fractions.pdf", multi_service(demand=False)))
    return layout_01.parse(pages), pages


@pytest.mark.parametrize(
    "amounts,net,vat,expected",
    [
        ([7200, 960, 3000], 11160, 2790, [1800, 240, 750]),
        ([7214, 1236, 8456], 16906, 4227, [1804, 309, 2114]),
        ([-7214, -1236, -8456], -16906, -4227, [-1804, -309, -2114]),
        ([1, 1, 1], 3, 1, [1, 0, 0]),
        ([-1, -1, -1], -3, -1, [-1, 0, 0]),
        ([100, -10, 11], 101, 25, [25, -2, 2]),
        ([0, 0, 0], 0, 0, [0, 0, 0]),
    ],
)
def test_printed_vat_is_conserved_with_signed_costs_and_rounding(amounts, net, vat, expected):
    assert allocated_vat(amounts, net, vat) == expected


def test_cancelling_nonzero_charges_need_explicit_vat_when_contract_net_is_zero():
    with pytest.raises(ValueError, match="zero-net"):
        allocated_vat([100, -100], 0, 0)


def test_fraction_costs_keep_shared_fees_and_move_weights_once(parsed):
    candidate, pages = parsed
    original = copy.deepcopy(candidate.to_dict())
    water, waste, garden, network = candidate.sections
    sources, warnings = split_waste_costs(
        candidate, pages, {waste.section_id: {"matavfall", "restavfall"}}
    )
    assert warnings == []
    assert candidate.sections[0] is water and candidate.sections[-1] is network
    assert garden in candidate.sections
    parts = [section for section in candidate.sections if section.identifiers[0].value == "9902"]
    assert [
        (part.subcategory, part.net_minor, part.vat_minor, part.gross_minor) for part in parts
    ] == [
        ("waste_collection", 7200, 1800, 9000),
        ("matavfall", 960, 240, 1200),
        ("restavfall", 3000, 750, 3750),
    ]
    assert [part.usages for part in parts] == [[], [waste.usages[0]], [waste.usages[1]]]
    assert all(part.tax_evidence == waste.tax_evidence for part in parts)
    assert all((part.start, part.end) == (waste.start, waste.end) for part in parts)
    assert {usage.evidence for part in parts for usage in part.usages} == {
        usage.evidence for usage in waste.usages
    }
    assert sources == {
        "section_02": "section_02",
        "section_02_matavfall": "section_02",
        "section_02_restavfall": "section_02",
    }
    assert [candidate.net_minor, candidate.vat_minor, candidate.invoice_total_minor] == [
        original["net_minor"],
        original["vat_minor"],
        original["invoice_total_minor"],
    ]


def test_missing_charge_never_uses_the_printed_per_kg_price(parsed):
    candidate, pages = parsed
    waste = candidate.sections[1]
    pages = [
        [
            replace(line, text="Matavfall 140L kärl 12 kg 0,80 kr/kg ?? kr")
            if line.text.startswith("Matavfall 140L")
            else line
            for line in page
        ]
        for page in pages
    ]
    sources, warnings = split_waste_costs(
        candidate, pages, {waste.section_id: {"matavfall", "restavfall"}}
    )
    assert sources == {} and "missing or invalid" in warnings[0]
    assert candidate.sections[1] is waste
    assert (waste.net_minor, waste.vat_minor, waste.gross_minor) == (11160, 2790, 13950)


def test_printed_zero_fraction_cost_is_retained_even_without_weight(parsed):
    candidate, pages = parsed
    waste = candidate.sections[1]
    pages = [
        [
            replace(line, text=line.text.replace("9,60 kr", "0,00 kr"))
            if line.text.startswith("Matavfall 140L")
            else replace(line, text="Moms 25 % på 102,00 kr 25,50 kr")
            if line.text.startswith("Moms 25 % på 111,60")
            else line
            for line in page
        ]
        for page in pages
    ]
    waste.net_minor, waste.vat_minor, waste.gross_minor = 10200, 2550, 12750
    waste.usages = [waste.usages[1]]
    _, warnings = split_waste_costs(
        candidate, pages, {waste.section_id: {"matavfall", "restavfall"}}
    )
    assert warnings == []
    food = next(section for section in candidate.sections if section.subcategory == "matavfall")
    assert (food.net_minor, food.vat_minor, food.gross_minor, food.usages) == (0, 0, 0, [])


def test_named_garden_contract_keeps_its_printed_zero_without_a_charge_row(parsed):
    candidate, pages = parsed
    garden = candidate.sections[2]
    garden.subcategory = ""
    garden.net_minor = garden.vat_minor = garden.gross_minor = 0
    pages = [
        [
            replace(line, text="Trädgårdsavfall 370L kärl: uppehåll")
            if line.text.startswith("Trädgårdsavfall 370L")
            else replace(line, text="Moms 25 % på 0,00 kr 0,00 kr")
            if line.text.startswith("Moms 25 % på 80,00")
            else line
            for line in page
        ]
        for page in pages
    ]
    _, warnings = split_waste_costs(candidate, pages, {garden.section_id: {"tradgardsavfall"}})
    assert warnings == []
    updated = candidate.sections[2]
    assert updated.section_id == garden.section_id and updated.subcategory == "tradgardsavfall"
    assert (updated.net_minor, updated.vat_minor, updated.gross_minor, updated.usages) == (
        0,
        0,
        0,
        [],
    )


def test_legacy_avfall_product_headings_classify_their_own_contract_costs(tmp_path):
    source = multi_service(demand=False)
    for page in source:
        for index, line in enumerate(page):
            if isinstance(line, str) and line.startswith("Hushållsavfall avtal 9902"):
                page[index] = "Avfall prodnr 9902 Kärl 180l Restavfall 1 st 40 g/år"
            elif isinstance(line, str) and line.startswith("Hushållsavfall avtal 9903"):
                page[index] = "Avfall prodnr 9903 Kärl 360l Trädgårdsavfall 1 st 16 g/år"
    pages = read_pdf(write_pdf(tmp_path / "legacy-fractions.pdf", source))
    assert layout_01_v2.matches(pages) and not layout_01.matches(pages)
    candidate = layout_01_v2.parse(pages)
    assert candidate.sections[2].subcategory == "tradgardsavfall"
    candidate.sections[2].subcategory = ""
    _, warnings = split_waste_costs(
        candidate,
        pages,
        {
            section.section_id: {"matavfall", "restavfall", "tradgardsavfall"}
            for section in candidate.sections
        },
    )
    assert warnings == []
    garden = next(
        section for section in candidate.sections if section.subcategory == "tradgardsavfall"
    )
    assert (garden.net_minor, garden.vat_minor, garden.gross_minor) == (8000, 2000, 10000)
    assert (
        next(
            section for section in candidate.sections if section.subcategory == "matavfall"
        ).net_minor
        == 960
    )


@pytest.fixture
def retained(parsed, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = tmp_path / "originals"
    candidate, pages = parsed
    house = Property.objects.create(name="Synthetic House", slug="synthetic-house")
    vendor = Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")
    for section in candidate.sections:
        identifier = section.identifiers[0]
        PropertyIdentifier.objects.create(
            vendor=vendor, property=house, kind=identifier.kind, value=identifier.value
        )
    actor = django_user_model.objects.create_user("synthetic-cost-reviewer", is_staff=True)
    document = Document.objects.create(
        sha256=hashlib.sha256((tmp_path / "fractions.pdf").read_bytes()).hexdigest(),
        original_filename="fractions.pdf",
    )
    attempt = record_extraction(
        document.pk,
        candidate,
        vendor=vendor,
        page_evidence=[asdict(line) for page in pages for line in page],
    )
    candidate.reference = "reviewed-reference"
    candidate.invoice_date = date(2024, 3, 6)
    candidate.sections[0].usages[0] = replace(
        candidate.sections[0].usages[0],
        quantity_milli=13000,
        evidence=Evidence(3, "Manually reviewed water usage", (0, 0, 0, 0)),
    )
    invoice = approve_review(
        document.invoices.get().pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={},
        actor=actor,
        extraction=attempt,
    )
    for fraction in ("matavfall", "restavfall", "tradgardsavfall"):
        PropertyService.objects.create(
            property=house, category="waste", subcategory_id=fraction, start_month=date(2024, 1, 1)
        )
    return invoice, house, actor, attempt


@pytest.mark.django_db(transaction=True)
def test_retained_cost_split_previews_then_preserves_headers_history_and_reports(retained):
    invoice, house, actor, attempt = retained
    original_review = invoice.reviews.get()
    original_snapshot = copy.deepcopy(original_review.snapshot)
    original_extraction = copy.deepcopy(attempt.candidate)
    output = io.StringIO()
    call_command("split_waste_costs", property_id=house.pk, stdout=output)
    assert "Would update 1 invoices" in output.getvalue()
    invoice.refresh_from_db()
    assert invoice.review_revision == 1 and invoice.sections.count() == 4
    call_command("split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    assert (invoice.reference, invoice.invoice_date, invoice.review_revision) == (
        "reviewed-reference",
        date(2024, 3, 6),
        2,
    )
    assert invoice.sections.count() == 6
    original_review.refresh_from_db()
    attempt.refresh_from_db()
    assert (
        original_review.snapshot == original_snapshot and attempt.candidate == original_extraction
    )
    assert invoice.sections.get(category="water").usages.get().quantity_milli == 13000
    assert sum(invoice.sections.values_list("net_minor", flat=True)) == invoice.net_minor
    assert sum(invoice.sections.values_list("vat_minor", flat=True)) == invoice.vat_minor
    assert (
        sum(invoice.sections.values_list("gross_minor", flat=True)) + invoice.rounding_minor
        == invoice.invoice_total_minor
    )
    report = read_report(property_id=house.pk)
    fractions = {row["fraction"]: row for row in report["waste_fractions"]}
    assert fractions["matavfall"]["cost"]["gross_minor"] == 1200
    assert fractions["restavfall"]["cost"]["gross_minor"] == 3750
    assert report["waste_general"][0]["cost"]["gross_minor"] == 9000
    assert report["waste_total"]["gross_minor"] == 23950
    output = io.StringIO()
    call_command(
        "split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk, stdout=output
    )
    invoice.refresh_from_db()
    assert invoice.review_revision == 2 and "Updated 0 invoices" in output.getvalue()
    assert "differ from the printed" not in output.getvalue()


@pytest.mark.django_db(transaction=True)
def test_cost_split_plan_cannot_replace_a_newer_approval(retained):
    invoice, house, actor, _ = retained
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is not None and warnings == []
    approve_review(invoice.pk, **plan, actor=actor)
    with pytest.raises(StaleReviewError):
        approve_review(invoice.pk, **plan, actor=actor)
    invoice.refresh_from_db()
    assert invoice.review_revision == 2


@pytest.mark.django_db(transaction=True)
def test_reviewed_waste_totals_are_not_overwritten_by_original_amounts(retained):
    invoice, house, actor, _ = retained
    from bills.waste_costs import reviewed_candidate

    candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
    candidate.sections[1].net_minor += 1
    candidate.sections[1].gross_minor += 1
    candidate.net_minor += 1
    candidate.invoice_total_minor += 1
    candidate.amount_due_minor += 1
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={},
        actor=actor,
    )
    invoice.refresh_from_db()
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is None and "Reviewed waste totals differ" in warnings[0]
    assert invoice.sections.get(subcategory_id="waste_collection").net_minor == 11161
    assert invoice.review_revision == 2


@pytest.mark.django_db(transaction=True)
def test_older_failed_extraction_can_supply_original_charge_evidence(retained):
    invoice, house, _, attempt = retained
    from bills.waste_costs import reviewed_candidate

    candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
    candidate.layout_family = candidate.layout_version = candidate.parser_revision = ""
    legacy = record_extraction(
        invoice.document_id,
        candidate,
        error="Older unsupported layout.",
        page_evidence=attempt.page_evidence,
    )
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is not None and warnings == [] and plan["extraction"] == legacy


@pytest.mark.django_db(transaction=True)
def test_confirmed_carried_balance_is_preserved_when_costs_are_split(retained):
    invoice, house, actor, _ = retained
    from bills.waste_costs import reviewed_candidate

    candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
    candidate.amount_due_minor -= 100
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={},
        actor=actor,
        amount_due_confirmed=True,
    )
    invoice.refresh_from_db()
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is not None and warnings == [] and plan["amount_due_confirmed"]
    assert plan["candidate"].amount_due_minor == invoice.amount_due_minor
    approve_review(invoice.pk, **plan, actor=actor)
    invoice.refresh_from_db()
    assert invoice.amount_due_minor == invoice.invoice_total_minor - 100


@pytest.mark.django_db(transaction=True)
def test_new_extraction_keeps_its_proposal_when_a_cost_update_is_stale(retained, monkeypatch):
    invoice, house, actor, attempt = retained
    from bills.management.commands import split_waste_costs as command

    original_planner = command.plan_retained_cost_split

    def append_proposal_after_planning(current_invoice, property_id):
        plan, warnings = original_planner(current_invoice, property_id)
        record_extraction(
            current_invoice.document_id,
            plan["candidate"],
            page_evidence=attempt.page_evidence,
        )
        return plan, warnings

    monkeypatch.setattr(command, "plan_retained_cost_split", append_proposal_after_planning)
    output, errors = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError, match="1 invoices could not be corrected"):
        call_command(
            "split_waste_costs",
            property_id=house.pk,
            apply=True,
            reviewer=actor.pk,
            stdout=output,
            stderr=errors,
        )
    assert "Updated 0 invoices" in output.getvalue() and "1 failed" in output.getvalue()
    assert "newer extraction" in errors.getvalue()
    invoice.refresh_from_db()
    invoice.document.refresh_from_db()
    assert invoice.review_revision == 1 and invoice.sections.count() == 4
    assert invoice.document.extraction_revision == 2 and invoice.document.state == "review"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "service,unit", [("other", "kg"), ("other", "m3"), ("other", "kWh"), ("electricity", "kWh")]
)
def test_cost_split_preserves_all_valid_reviewed_categories(retained, service, unit):
    invoice, house, actor, _ = retained
    candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
    water = candidate.sections[0]
    evidence = Evidence(3, "Manually reviewed allocation and consumption", (0, 0, 0, 0))
    candidate.sections.append(
        replace(
            water,
            section_id="reviewed_allocation",
            service=service,
            identifiers=[],
            net_minor=100,
            vat_minor=25,
            gross_minor=125,
            tax_evidence=evidence,
            total_evidence=evidence,
            usages=[Usage(service, unit, 12345, water.start, water.end, "billed", evidence)],
        )
    )
    water.net_minor -= 100
    water.vat_minor -= 25
    water.gross_minor -= 125
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={"reviewed_allocation": house},
        actor=actor,
    )
    invoice.refresh_from_db()
    previous = copy.deepcopy(invoice.reviews.get(revision=2).snapshot)
    output = io.StringIO()
    call_command("split_waste_costs", property_id=house.pk, stdout=output)
    assert "Would update 1 invoices" in output.getvalue()
    invoice.refresh_from_db()
    assert invoice.review_revision == 2
    call_command("split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    assert invoice.review_revision == 3
    assert invoice.reviews.get(revision=2).snapshot == previous
    current = invoice.reviews.get(revision=3).snapshot
    assert current["values"]["sections"][-1] == previous["values"]["sections"][-1]
    assert current["properties"]["reviewed_allocation"] == house.pk
    assert invoice.sections.get(subcategory_id="restavfall").gross_minor == 3750


@pytest.mark.django_db(transaction=True)
def test_newly_enabled_fraction_extends_split_and_preserves_reviewed_parts(retained):
    invoice, house, actor, _ = retained
    rest = PropertyService.objects.get(property=house, subcategory_id="restavfall")
    rest.is_active = False
    rest.save()
    call_command("split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    assert invoice.review_revision == 2
    assert not invoice.sections.filter(subcategory_id="restavfall").exists()
    candidate = reviewed_candidate(invoice.reviews.get(revision=2).snapshot["values"])
    food = next(section for section in candidate.sections if section.subcategory == "matavfall")
    general = next(
        section for section in candidate.sections if section.subcategory == "waste_collection"
    )
    food.net_minor += 1
    food.vat_minor += 1
    food.gross_minor += 2
    general.net_minor -= 1
    general.vat_minor -= 1
    general.gross_minor -= 2
    food.start, food.end = date(2024, 1, 1), date(2024, 2, 1)
    food.usages[0] = replace(food.usages[0], quantity_milli=15000, start=food.start, end=food.end)
    approve_review(
        invoice.pk,
        expected_revision=2,
        candidate=candidate,
        vendor=invoice.vendor,
        section_properties={},
        actor=actor,
    )
    invoice.refresh_from_db()
    previous = copy.deepcopy(invoice.reviews.get(revision=3).snapshot)
    rest.is_active = True
    rest.save()
    output = io.StringIO()
    call_command("split_waste_costs", property_id=house.pk, stdout=output)
    assert "Would update 1 invoices" in output.getvalue()
    invoice.refresh_from_db()
    assert invoice.review_revision == 3
    call_command("split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk)
    invoice.refresh_from_db()
    assert invoice.review_revision == 4
    current = invoice.reviews.get(revision=4).snapshot
    previous_food = next(
        section
        for section in previous["values"]["sections"]
        if section["subcategory"] == "matavfall"
    )
    current_food = next(
        section
        for section in current["values"]["sections"]
        if section["subcategory"] == "matavfall"
    )
    assert current_food == previous_food
    assert invoice.reviews.get(revision=3).snapshot == previous
    assert invoice.sections.get(subcategory_id="restavfall").gross_minor == 3750
    assert invoice.sections.get(subcategory_id="waste_collection").net_minor == 7199
    assert sum(invoice.sections.values_list("vat_minor", flat=True)) == invoice.vat_minor
    report = read_report(property_id=house.pk)
    fractions = {row["fraction"]: row for row in report["waste_fractions"]}
    assert fractions["matavfall"]["cost"]["gross_minor"] == 1202
    assert fractions["restavfall"]["cost"]["gross_minor"] == 3750
    assert report["waste_total"]["gross_minor"] == 23950
    output = io.StringIO()
    call_command(
        "split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk, stdout=output
    )
    invoice.refresh_from_db()
    assert invoice.review_revision == 4 and "Updated 0 invoices" in output.getvalue()


def test_extending_partial_split_retains_parts_when_new_charge_is_missing(parsed):
    candidate, pages = parsed
    split_waste_costs(candidate, pages, {"section_02": {"matavfall"}})
    original = copy.deepcopy(candidate.to_dict())
    pages = [
        [
            replace(line, text="Restavfall 190L kärl 7,5 kg 4,00 kr/kg ?? kr")
            if line.text.startswith("Restavfall 190L")
            else line
            for line in page
        ]
        for page in pages
    ]
    sources, warnings = split_waste_costs(
        candidate, pages, {"section_02": {"matavfall", "restavfall"}}
    )
    assert sources == {} and "missing or invalid" in warnings[0]
    assert candidate.to_dict() == original


def test_extending_partial_split_rejects_changed_contract_totals(parsed):
    candidate, pages = parsed
    split_waste_costs(candidate, pages, {"section_02": {"matavfall"}})
    general = next(
        section for section in candidate.sections if section.subcategory == "waste_collection"
    )
    general.net_minor += 1
    general.gross_minor += 1
    original = copy.deepcopy(candidate.to_dict())
    sources, warnings = split_waste_costs(
        candidate, pages, {"section_02": {"matavfall", "restavfall"}}
    )
    assert sources == {} and "Reviewed waste totals differ" in warnings[0]
    assert candidate.to_dict() == original


def test_incomplete_contract_costs_keep_the_original_proposal(parsed):
    candidate, pages = parsed
    candidate.sections[1].net_minor = None
    original = copy.deepcopy(candidate.to_dict())
    sources, warnings = split_waste_costs(candidate, pages, {"section_02": {"matavfall"}})
    assert sources == {} and "complete reviewed cost totals" in warnings[0]
    assert candidate.to_dict() == original


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("family", ["layout_03", "layout_06", ""])
def test_retained_corrections_require_layout_01_evidence(retained, family):
    invoice, house, actor, attempt = retained
    original = copy.deepcopy(invoice.reviews.get().snapshot)
    candidate = reviewed_candidate(original["values"])
    candidate.layout_family = family
    evidence = attempt.page_evidence
    if not family:
        candidate.layout_version = candidate.parser_revision = ""
        # Generic contract and VAT rows alone do not establish a supported layout.
        evidence = [row for row in evidence if not row["text"].startswith("FAKTURASPECIFIKATION")]
    record_extraction(
        invoice.document_id,
        candidate,
        error="Older unsupported layout." if not family else "",
        page_evidence=evidence,
    )
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is None and "Layout 01" in warnings[0]
    output = io.StringIO()
    call_command(
        "split_waste_costs", property_id=house.pk, apply=True, reviewer=actor.pk, stdout=output
    )
    invoice.refresh_from_db()
    assert invoice.review_revision == 1 and invoice.sections.count() == 4
    assert invoice.reviews.get().snapshot == original
    assert "Updated 0 invoices" in output.getvalue()


def test_reviewed_candidate_ignores_extra_legacy_usage_fields(parsed):
    candidate, _ = parsed
    original = candidate.to_dict()
    legacy = copy.deepcopy(original)
    legacy["sections"][0]["usages"][0]["legacy_metadata"] = {"description": "Older usage field"}
    retained = copy.deepcopy(legacy)
    assert reviewed_candidate(legacy).to_dict() == original
    assert legacy == retained


@pytest.mark.parametrize("service,unit", [("electricity", "kg"), ("other", "litres")])
def test_reviewed_category_reconciliation_still_rejects_invalid_units(parsed, service, unit):
    candidate, _ = parsed
    section = candidate.sections[0]
    section.service = service
    section.usages[0] = replace(section.usages[0], utility=service, unit=unit)
    assert "Invalid usage in section_01" in reconcile(candidate).issues


@pytest.mark.django_db(transaction=True)
def test_invalid_correction_rolls_back_one_invoice_and_continues_the_batch(retained, monkeypatch):
    from bills import services

    invoice, house, actor, attempt = retained
    invoices = [invoice]
    for index in (2, 3):
        document = Document.objects.create(
            sha256=hashlib.sha256(f"synthetic-batch-{index}".encode()).hexdigest(),
            original_filename=f"synthetic-batch-{index}.pdf",
        )
        candidate = reviewed_candidate(invoice.reviews.get().snapshot["values"])
        candidate.reference = f"synthetic-batch-{index}"
        extraction = record_extraction(
            document.pk, candidate, vendor=invoice.vendor, page_evidence=attempt.page_evidence
        )
        invoices.append(
            approve_review(
                document.invoices.get().pk,
                expected_revision=0,
                candidate=candidate,
                vendor=invoice.vendor,
                section_properties={},
                actor=actor,
                extraction=extraction,
            )
        )
    invalid_invoice = invoices[1]
    previous = copy.deepcopy(invalid_invoice.reviews.get().snapshot)
    validate = services.validate_allocations

    def reject_one_correction(current_invoice):
        validate(current_invoice)
        if current_invoice.pk == invalid_invoice.pk:
            raise ValidationError("Synthetic invalid correction.")

    monkeypatch.setattr(services, "validate_allocations", reject_one_correction)
    output, errors = io.StringIO(), io.StringIO()
    with pytest.raises(CommandError, match="1 invoices could not be corrected"):
        call_command(
            "split_waste_costs",
            property_id=house.pk,
            apply=True,
            reviewer=actor.pk,
            stdout=output,
            stderr=errors,
        )
    assert "Updated 2 invoices; 0 unchanged; 1 failed" in output.getvalue()
    assert f"Invoice {invalid_invoice.pk}" in errors.getvalue()
    assert "Synthetic invalid correction" in errors.getvalue()
    for current in invoices:
        current.refresh_from_db()
        if current.pk == invalid_invoice.pk:
            assert current.review_revision == 1 and current.sections.count() == 4
            assert current.reviews.get().snapshot == previous
        else:
            assert current.review_revision == 2 and current.sections.count() == 6
