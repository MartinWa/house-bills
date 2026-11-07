import hashlib
import json
from dataclasses import replace
from datetime import date

import pytest

from bills.parsing import layout_01, layout_02
from bills.parsing.common import allocate_vat, last_money, reconcile
from bills.parsing.normalization import money, quantity, source_date, source_period
from bills.parsing.reader import read_pdf
from bills.parsing.types import Evidence

from .synthetic import multi_service, shared_supply, write_pdf


def parse_synthetic(tmp_path, pages, parser=layout_01):
    path = write_pdf(tmp_path / "synthetic.pdf", pages)
    original_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    candidate = parser.parse(read_pdf(path))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original_hash
    return candidate


def test_multi_service_pdf_preserves_contracts_costs_and_billed_usage(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service())
    assert candidate.issues == []
    assert (candidate.reference, candidate.invoice_date, candidate.due_date) == (
        "880001",
        date(2024, 3, 5),
        date(2024, 3, 29),
    )
    assert (
        candidate.net_minor,
        candidate.vat_minor,
        candidate.rounding_minor,
        candidate.invoice_total_minor,
    ) == (101006, 25252, 42, 126300)
    assert [
        (s.service, s.identifiers[0].value, s.net_minor, s.vat_minor, s.gross_minor)
        for s in candidate.sections
    ] == [
        ("water", "9901", 44000, 11000, 55000),
        ("waste", "9902", 11160, 2790, 13950),
        ("waste", "9903", 8000, 2000, 10000),
        ("electricity_network", "9904", 37846, 9462, 47308),
    ]
    assert [[u.quantity_milli for u in s.usages] for s in candidate.sections] == [
        [12000],
        [12000, 7500],
        [],
        [321125],
    ]
    assert all(
        (u.start, u.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for s in candidate.sections
        for u in s.usages
    )
    assert candidate.requires_review
    assert candidate.identifiers[0].value == "9900"
    for section in candidate.sections:
        assert sum(c.gross_minor for c in section.charges) == section.gross_minor
        assert sum(c.vat_minor for c in section.charges) == section.vat_minor
    assert json.loads(json.dumps(candidate.to_dict()))["invoice_date"] == "2024-03-05"


def test_water_correction_keeps_signed_usage_and_separate_periods(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service(correction=True))
    assert candidate.issues == []
    assert (candidate.invoice_total_minor, candidate.rounding_minor) == (135000, -8)
    water = candidate.sections[0]
    assert (water.net_minor, water.vat_minor, water.gross_minor) == (51000, 12750, 63750)
    assert [(u.quantity_milli, u.basis, u.start, u.end) for u in water.usages] == [
        (45000, "measured", date(2023, 12, 1), date(2024, 3, 1)),
        (-12000, "reversal", date(2023, 12, 1), date(2024, 1, 1)),
        (-14000, "reversal", date(2024, 1, 1), date(2024, 2, 1)),
    ]
    assert [c.net_minor for c in water.charges] == [10000, 20000, 45000, 2000, -12000, -14000]
    assert [c.gross_minor for c in water.charges][-2:] == [-15000, -17500]
    assert candidate.sections[1].charges[0].evidence.page == 2
    assert candidate.sections[1].charges[-1].evidence.page == 3


@pytest.mark.parametrize("reading_label,basis", [("Beräknad", "estimated"), ("Okänd", "billed")])
def test_water_price_quantity_does_not_imply_measured_usage(tmp_path, reading_label, basis):
    pages = multi_service()
    pages[2] = [
        line.replace("Avläsning 1 mar 2024", f"{reading_label} 1 mar 2024")
        if isinstance(line, str)
        else line
        for line in pages[2]
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert candidate.sections[0].usages[0].basis == basis


def test_missing_required_header_does_not_return_a_valid_candidate(tmp_path):
    pages = shared_supply()
    pages[0] = [line for line in pages[0] if not line.startswith("Fakturadatum")]
    candidate = parse_synthetic(tmp_path, pages, layout_02)
    assert candidate.invoice_date is None
    assert candidate.reference == "880003" and len(candidate.sections) == 2
    assert "Missing or invalid invoice_date" in candidate.issues
    assert candidate.requires_review


def test_shared_invoice_keeps_installations_and_counts_repeated_kwh_once(tmp_path):
    candidate = parse_synthetic(tmp_path, shared_supply(), layout_02)
    assert candidate.issues == []
    assert (
        candidate.reference,
        candidate.net_minor,
        candidate.vat_minor,
        candidate.rounding_minor,
        candidate.invoice_total_minor,
    ) == ("880003", 35755, 8939, 6, 44700)
    assert [s.identifiers[0].value for s in candidate.sections] == [
        "000000000000000001",
        "000000000000000002",
    ]
    assert [s.gross_minor for s in candidate.sections] == [13819, 30875]
    assert [[u.quantity_milli for u in s.usages] for s in candidate.sections] == [
        [140250],
        [510000],
    ]
    assert candidate.sections[1].charges[2].net_minor == -500
    assert all(
        (u.start, u.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for s in candidate.sections
        for u in s.usages
    )


def test_conflicting_supply_quantities_require_review(tmp_path):
    pages = shared_supply()
    pages[1] = [
        line.replace("140,250 kWh", "141 kWh")
        if isinstance(line, str) and line.startswith("Rörliga kostnader")
        else line
        for line in pages[1]
    ]
    candidate = parse_synthetic(tmp_path, pages, layout_02)
    assert "Inconsistent billed kWh in section_01" in candidate.issues
    assert candidate.requires_review


def test_incorrect_totals_remain_review_candidates(tmp_path):
    pages = multi_service()
    pages[0] = [
        line.replace("1 263,00 kr", "1 264,00 kr") if isinstance(line, str) else line
        for line in pages[0]
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert "Invoice allocation mismatch" in candidate.issues
    assert "Header total mismatch" in candidate.issues
    assert candidate.requires_review


def test_wrong_units_are_rejected_during_reconciliation(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service())
    water = candidate.sections[0]
    water.usages[0] = replace(water.usages[0], unit="kg")
    assert "Invalid usage in section_01" in reconcile(candidate).issues


def test_water_pdf_with_wrong_unit_preserves_cost_and_flags_usage(tmp_path):
    pages = multi_service()
    pages[2] = [
        line.replace("12 m3", "12 kg")
        if isinstance(line, str) and line.startswith("Förbrukning")
        else line
        for line in pages[2]
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert "Invalid water quantity in section_01" in candidate.issues
    assert candidate.sections[0].usages == []
    assert candidate.sections[0].gross_minor == 55000


def test_printed_vat_rounding_is_retained_but_large_discrepancies_fail(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service())
    water = candidate.sections[0]
    water.vat_minor += 1
    water.gross_minor += 1
    assert "VAT mismatch in section_01" not in reconcile(candidate).issues
    water.vat_minor += 100
    water.gross_minor += 100
    assert "VAT mismatch in section_01" in reconcile(candidate).issues


def test_mismatched_section_net_does_not_allocate_a_large_vat_difference(tmp_path):
    candidate = parse_synthetic(tmp_path, shared_supply(), layout_02)
    section = candidate.sections[0]
    section.net_minor = 10**12
    section.vat_minor = section.net_minor // 4
    section.charges = [replace(c, vat_minor=None, gross_minor=None) for c in section.charges]
    result = reconcile(allocate_vat(candidate))
    assert "Net charge mismatch in section_01" in result.issues
    assert all(c.vat_minor is None for c in section.charges)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1 234,56", 123456),
        ("1\u00a0234,56", 123456),
        ("1\u202f234,56", 123456),
        ("−12,50", -1250),
        ("0,00", 0),
        ("1234.56", 123456),
    ],
)
def test_exact_money(text, expected):
    assert money(text) == expected


@pytest.mark.parametrize("text", ["1,234", "NaN", "1,2,3", "12 34,56", "1.234,56", ""])
def test_ambiguous_or_overprecise_money_fails(text):
    with pytest.raises(ValueError):
        money(text)


def test_quantity_precision_and_date_conventions():
    assert quantity("123,456") == 123456
    assert quantity("-12,5") == -12500
    with pytest.raises(ValueError):
        quantity("0,0001")
    assert source_date("29 februari 2024") == date(2024, 2, 29)
    assert source_period("1 feb 2024 - 1 mar 2024") == (date(2024, 2, 1), date(2024, 3, 1))
    assert source_period("2024-02-01 - 2024-02-29", end_inclusive=True) == (
        date(2024, 2, 1),
        date(2024, 3, 1),
    )
    with pytest.raises(ValueError):
        source_period("1 mar 2024 - 1 feb 2024")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Förbrukning 12 m3 10,00 kr/m3 120,00 kr", 12000),
        ("Summa Exempelnät AB SE000000000001 473,08 kr", 47308),
        ("Förbrukning -12 m3 10,00 kr/m3 -120,00 kr", -12000),
    ],
)
def test_amounts_do_not_absorb_unit_or_identifier_digits(text, expected):
    assert last_money(Evidence(1, text, (0, 0, 500, 20))) == expected
