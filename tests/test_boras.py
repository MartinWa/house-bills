from copy import deepcopy
from dataclasses import replace

import pytest
from django.core.management import call_command

from bills.models import (
    Property,
    PropertyIdentifier,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing import layout_07
from bills.parsing.reader import read_pdf
from bills.parsing.registry import match_layouts
from bills.services import approve_review
from bills.storage import ingest_pdf

from .synthetic import write_pdf


def area_supply(billed="786"):
    """Invented identifiers, dates and amounts in the area-labelled supply structure."""
    return [
        [
            "E-FAKTURA Sida: 1/2",
            "Fakturadatum 5 mar 2024",
            "Faktura/OCR-nummer 910007",
            "VAT-nr SE000000777701",
            "Anl id 000 000 000 000 000 071",
            "Elhandel 100,00 kr",
            "Oss tillhanda Summa exkl moms Moms Öresutjämning",
            "80,00 kr 20,00 kr 0,00 kr 100,00 kr",
            "Att betala (SEK)",
            "25 mar 2024",
        ],
        [
            "FAKTURASPECIFIKATION Sida: 2/2",
            "Faktura/OCR-nummer 910007",
            "Fakturaperiod 1 feb 2024 - 29 feb 2024",
            "Verklig årsförbrukning 98765 kWh",
            "Mätarnummer 400071 Ställning Förbrukning",
            f"Förbrukning Område 3 {billed} kWh 12,34 öre/kWh 100,00 kr",
            "Moms 25 % på 80,00 kr ingår med 20,00 kr",
            "Summa Elhandel 100,00 kr",
        ],
    ]


@pytest.mark.parametrize(
    "billed,expected",
    [
        ("949", 949000),
        ("786", 786000),
        ("741", 741000),
        ("764", 764000),
        ("1344", 1344000),
        ("1155", 1155000),
        ("1 344", 1344000),
        ("1\u00a0344", 1344000),
        ("-786", -786000),
        ("786,125", 786125),
    ],
)
def test_area_label_is_separate_from_consumption_and_preserves_evidence(tmp_path, billed, expected):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", area_supply(billed)))
    assert match_layouts(pages, [layout_07.FAMILY]) == [layout_07]
    candidate = layout_07.parse(pages)
    assert candidate.issues == []
    (section,) = candidate.sections
    (usage,) = section.usages
    assert usage.quantity_milli == expected
    source_row = next(line for line in pages[1] if line.text.startswith("Förbrukning "))
    assert usage.evidence == source_row
    assert usage.evidence.page == 2 and usage.evidence.bbox != (0, 0, 0, 0)
    assert usage.start.isoformat() == "2024-02-01" and usage.end.isoformat() == "2024-03-01"
    assert candidate.invoice_total_minor == section.gross_minor == 10000


@pytest.mark.parametrize(
    "row",
    [
        "Förbrukning Område 12 786 kWh",
        "Förbrukning 786 kWh",
        "förbrukning område 3 786 kwh",
    ],
)
def test_consumption_accepts_other_areas_and_optional_labels(tmp_path, row):
    values = area_supply()
    values[1][5] = row
    candidate = layout_07.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert candidate.issues == []
    assert candidate.sections[0].usages[0].quantity_milli == 786000


@pytest.mark.parametrize(
    "row",
    [
        "Förbrukning Område 3 kWh",
        "Förbrukning Område 3 786 m3",
        "Förbrukning Område 3 786,1234 kWh",
    ],
)
def test_invalid_consumption_keeps_costs_and_requires_review(tmp_path, row):
    values = area_supply()
    values[1][5] = row
    candidate = layout_07.parse(read_pdf(write_pdf(tmp_path / "synthetic.pdf", values)))
    assert candidate.issues and not candidate.sections[0].usages
    assert candidate.sections[0].gross_minor == 10000


@pytest.mark.django_db(transaction=True)
def test_reparse_corrects_approved_consumption_through_a_new_review(
    settings, tmp_path, django_user_model
):
    settings.MEDIA_ROOT = tmp_path / "originals"
    house = Property.objects.create(name="Synthetic House", slug="synthetic-house")
    vendor = Vendor.objects.create(name="Synthetic Supply", slug="synthetic-supply")
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000777701")
    VendorLayoutFamily.objects.create(vendor=vendor, family=layout_07.FAMILY)
    PropertyIdentifier.objects.create(
        vendor=vendor, property=house, kind="installation", value="000000000000000071"
    )
    path = write_pdf(tmp_path / "synthetic.pdf", area_supply())
    with path.open("rb") as source:
        document, _ = ingest_pdf(source, filename="synthetic.pdf")
    original = document.extractions.latest("revision")
    candidate = layout_07.parse(read_pdf(path))
    wrong = deepcopy(candidate)
    wrong.sections[0].usages[0] = replace(wrong.sections[0].usages[0], quantity_milli=3786000)
    actor = django_user_model.objects.create_user("synthetic-reviewer")
    invoice = approve_review(
        document.invoices.get().pk,
        expected_revision=0,
        candidate=wrong,
        vendor=vendor,
        section_properties={"section_01": house},
        actor=actor,
        extraction=original,
    )
    call_command("parse_documents", document.pk)
    attempt = document.extractions.latest("revision")
    assert attempt.candidate["sections"][0]["usages"][0]["quantity_milli"] == 786000
    assert Usage.objects.get().quantity_milli == 3786000
    approve_review(
        invoice.pk,
        expected_revision=1,
        candidate=candidate,
        vendor=vendor,
        section_properties={"section_01": house},
        actor=actor,
        extraction=attempt,
        note="Corrected consumption from the retained source.",
    )
    assert Usage.objects.get().quantity_milli == 786000
    assert (
        invoice.reviews.get(revision=1).snapshot["values"]["sections"][0]["usages"][0][
            "quantity_milli"
        ]
        == 3786000
    )
    assert invoice.reviews.get(revision=2).extraction_id == attempt.pk
