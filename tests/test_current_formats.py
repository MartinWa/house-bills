import re
from copy import deepcopy

import pytest

from bills.models import (
    Document,
    Invoice,
    Property,
    PropertyIdentifier,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing import layout_03, layout_04, layout_05, layout_06
from bills.parsing.reader import read_pdf
from bills.parsing.registry import match_layouts
from bills.processing import parse_document
from bills.services import approve_review
from bills.storage import ingest_pdf, private_path

from .current_formats import chart_supply, cover_supply, monthly_network, quarterly_waste
from .synthetic import write_pdf

FORMATS = (
    (layout_03, quarterly_waste, 10405, 2601, -6, 13000, None),
    (layout_04, cover_supply, 8000, 2000, 0, 10000, 80125),
    (layout_05, chart_supply, 8000, 2000, 0, 10000, 321125),
    (layout_06, monthly_network, 8000, 2000, 15, 10015, 100125),
)


@pytest.mark.parametrize("parser,fixture,net,vat,rounding,total,billed", FORMATS)
def test_current_formats_match_reconcile_and_keep_one_consumption(
    tmp_path, parser, fixture, net, vat, rounding, total, billed
):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", fixture()))
    assert match_layouts(pages, [row[0].FAMILY for row in FORMATS]) == [parser]
    candidate = parser.parse(pages)
    assert not candidate.issues
    assert (
        candidate.net_minor,
        candidate.vat_minor,
        candidate.rounding_minor,
        candidate.invoice_total_minor,
    ) == (net, vat, rounding, total)
    assert (
        candidate.reference
        == {
            "layout_03": "910003",
            "layout_04": "910004",
            "layout_05": "910005",
            "layout_06": "910006",
        }[parser.FAMILY]
    )
    (section,) = candidate.sections
    assert (section.net_minor, section.vat_minor, section.gross_minor) == (net, vat, net + vat)
    assert section.start.isoformat() == ("2024-10-01" if parser is layout_03 else "2024-02-01")
    assert section.end.isoformat() == ("2025-01-01" if parser is layout_03 else "2024-03-01")
    assert [usage.quantity_milli for usage in section.usages] == (
        [] if billed is None else [billed]
    )
    assert candidate.requires_review


@pytest.mark.parametrize("parser,fixture,net,vat,rounding,total,billed", FORMATS)
def test_current_formats_preserve_signed_totals_and_consumption(
    tmp_path, parser, fixture, net, vat, rounding, total, billed
):
    values = fixture()
    monetary = re.compile(r"(?<![\d,])(-?\d+,\d{2})(?!\d|\s*%)")
    for page in values:
        for index, line in enumerate(page):
            page[index] = monetary.sub(
                lambda match: match[1][1:] if match[1].startswith("-") else "-" + match[1],
                line,
            )
            if billed is not None:
                quantity_text = f"{billed // 1000},{billed % 1000:03d}"
                page[index] = page[index].replace(quantity_text, "-" + quantity_text)
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic-credit.pdf", values)))
    assert not candidate.issues
    assert (
        candidate.net_minor,
        candidate.vat_minor,
        candidate.rounding_minor,
        candidate.invoice_total_minor,
        candidate.amount_due_minor,
    ) == (-net, -vat, -rounding, -total, -total)
    assert [usage.quantity_milli for usage in candidate.sections[0].usages] == (
        [] if billed is None else [-billed]
    )
    assert candidate.requires_review


@pytest.mark.parametrize("parser,fixture,net,vat,rounding,total,billed", FORMATS)
def test_invalid_current_billing_period_keeps_costs_without_inferred_usage(
    tmp_path, parser, fixture, net, vat, rounding, total, billed
):
    replacements = {
        layout_03: ("2024-10-01 - 2024-12-31", "2024-12-31 - 2024-10-01"),
        layout_04: ("2024-02-01 - 2024-02-29", "2024-02-29 - 2024-02-01"),
        layout_05: ("2024-02-01 - 2024-02-29", "2024-02-29 - 2024-02-01"),
        layout_06: ("1 feb 2024 t o m 29 feb 2024", "29 feb 2024 t o m 1 feb 2024"),
    }
    old, new = replacements[parser]
    values = [[line.replace(old, new) for line in page] for page in fixture()]
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert candidate.sections[0].gross_minor == net + vat
    assert any("billing period" in issue for issue in candidate.issues)
    assert not candidate.sections[0].usages


@pytest.mark.parametrize(
    "parser,fixture,billed",
    [
        (layout_04, cover_supply, "80,125"),
        (layout_05, chart_supply, "321,125"),
        (layout_06, monthly_network, "100,125"),
    ],
)
def test_invalid_current_consumption_precision_never_truncates_or_uses_a_chart(
    tmp_path, parser, fixture, billed
):
    values = [[line.replace(billed, billed + "1") for line in page] for page in fixture()]
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert candidate.sections[0].gross_minor == 10000
    assert not candidate.sections[0].usages
    assert any("Invalid billed electricity consumption" in issue for issue in candidate.issues)


@pytest.mark.parametrize("parser,fixture,net,vat,rounding,total,billed", FORMATS)
def test_invalid_current_money_precision_remains_a_review_problem(
    tmp_path, parser, fixture, net, vat, rounding, total, billed
):
    net_text = f"{net // 100},{net % 100:02d}"
    values = [[line.replace(net_text, net_text + "1") for line in page] for page in fixture()]
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert candidate.net_minor is None
    assert any("invalid net_minor" in issue for issue in candidate.issues)
    assert candidate.requires_review


@pytest.mark.parametrize("parser,fixture", [(row[0], row[1]) for row in FORMATS])
def test_current_format_costs_are_independent_of_individual_fee_amounts(tmp_path, parser, fixture):
    values = deepcopy(fixture())
    for page in values:
        for i, line in enumerate(page):
            if line.startswith(("Medelspotpris", "Grundavgift permanentbostad", "Fast avgift")):
                # Fee amounts are source detail; authoritative service/header totals are unchanged.
                page[i] = line.rsplit(" ", 2)[0] + " 999999,00 kr"
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert not candidate.issues


@pytest.mark.parametrize(
    "parser,fixture,billed",
    [
        (layout_04, cover_supply, "80,125"),
        (layout_05, chart_supply, "321,125"),
        (layout_06, monthly_network, "100,125"),
    ],
)
def test_conflicting_repeated_price_quantities_keep_costs_and_require_review(
    tmp_path, parser, fixture, billed
):
    values = deepcopy(fixture())
    for page in values:
        for i, line in enumerate(page):
            if line.startswith(("*Rörliga", "Rörliga", "Energiskatt")):
                page[i] = line.replace(billed, "999,125")
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert any("Inconsistent billed kWh" in issue for issue in candidate.issues)
    assert candidate.sections[0].gross_minor == 10000
    assert candidate.requires_review


def test_missing_consumption_table_never_uses_annual_chart_or_fee_quantities(tmp_path):
    values = chart_supply()
    values[1] = [line for line in values[1] if not line.startswith("1750 ")]
    candidate = layout_05.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert not candidate.sections[0].usages
    assert any("Missing billed consumption table" in issue for issue in candidate.issues)
    assert candidate.sections[0].gross_minor == 10000


def test_network_cid_repair_is_bounded_and_preserves_evidence(tmp_path):
    values = monthly_network()
    values = [
        [
            line.replace("ä", "(cid:228)").replace("ö", "(cid:246)").replace("Ö", "(cid:214)")
            for line in page
        ]
        for page in values
    ]
    candidate = layout_06.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert not candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 100125
    assert "(cid:214)" in candidate.sections[0].usages[0].evidence.text


@pytest.mark.parametrize("parser,fixture", [(row[0], row[1]) for row in FORMATS])
def test_ambiguous_installations_never_assign_a_single_house(tmp_path, parser, fixture):
    values = fixture()
    if parser is layout_03:
        values[0].append("Anläggning 499300 Another imaginary delivery address")
    elif parser is layout_04:
        values[0].append("AnläggningsID: 000000000000000099")
    else:
        values[1].append("Anläggnings-ID: 000000000000000099")
    candidate = parser.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert not candidate.sections[0].identifiers
    assert any("ambiguous installation" in issue for issue in candidate.issues)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("parser,fixture,net,vat,rounding,total,billed", FORMATS)
def test_current_formats_ingest_approve_deduplicate_and_preserve_correction(
    settings, tmp_path, django_user_model, parser, fixture, net, vat, rounding, total, billed
):
    settings.MEDIA_ROOT = tmp_path / "originals"
    house = Property.objects.create(name="Synthetic Current House", slug="current-house")
    vendor = Vendor.objects.create(name="Synthetic Current Vendor", slug="current-vendor")
    id_values = {
        "layout_03": "SE000000333301",
        "layout_04": "SE000000444401",
        "layout_05": "SE000000555501",
        "layout_06": "SE000000666601",
    }
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value=id_values[parser.FAMILY])
    VendorLayoutFamily.objects.create(vendor=vendor, family=parser.FAMILY)
    pdf = write_pdf(tmp_path / "synthetic.pdf", fixture())
    candidate = parser.parse(read_pdf(pdf))
    PropertyIdentifier.objects.create(
        vendor=vendor,
        property=house,
        kind="installation",
        value=candidate.sections[0].identifiers[0].value,
    )
    with pdf.open("rb") as source:
        document, duplicate = ingest_pdf(source, filename="synthetic.pdf")
    assert not duplicate
    attempt = document.extractions.get()
    assert not attempt.error and not attempt.candidate["issues"]
    assert attempt.layout_family == parser.FAMILY
    assert not Usage.objects.exists()
    invoice = document.invoices.get()
    user = django_user_model.objects.create_user("synthetic-current-reviewer")
    approved = approve_review(
        invoice.pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={"section_01": house},
        actor=user,
        extraction=attempt,
    )
    assert approved.status == Invoice.Status.APPROVED
    assert approved.invoice_total_minor == total
    assert list(Usage.objects.values_list("quantity_milli", flat=True)) == (
        [] if billed is None else [billed]
    )
    approved = approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=vendor,
        section_properties={"section_01": house},
        actor=user,
        note="Synthetic accepted correction.",
    )
    parse_document(document.pk, force=True)
    approved.refresh_from_db()
    assert approved.review_revision == 2
    with pdf.open("rb") as source:
        same, duplicate = ingest_pdf(source, filename="another-house/same.pdf")
    assert duplicate and same.pk == document.pk and Document.objects.count() == 1
    assert approved.reviews.count() == 2
    document.refresh_from_db()
    assert private_path(document.original_path).read_bytes() == pdf.read_bytes()
