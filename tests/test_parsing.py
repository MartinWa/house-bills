import hashlib
import json
from dataclasses import replace
from datetime import date

import pytest

from bills.categories import waste_cost_subcategory
from bills.parsing import layout_01, layout_02, layout_02_v2
from bills.parsing.common import last_money, reconcile
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


def test_waste_costs_and_partial_reported_weights_are_independent(tmp_path):
    pages = multi_service()
    pages = [
        [
            line.replace(
                "Restavfall 190L kärl 7,5 kg 4,00 kr/kg 30,00 kr", "Fast restavfallsavgift 30,00 kr"
            )
            if isinstance(line, str)
            else line
            for line in page
        ]
        for page in pages
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert candidate.issues == []
    combined, garden = candidate.sections[1:3]
    assert combined.subcategory == "waste_collection" and combined.gross_minor == 13950
    assert [(weight.subcategory, weight.quantity_milli) for weight in combined.usages] == [
        ("matavfall", 12000)
    ]
    assert garden.subcategory == "tradgardsavfall" and garden.gross_minor == 10000
    assert garden.usages == []


@pytest.mark.parametrize("fraction", ["Matavfall", "Restavfall"])
def test_named_waste_block_retains_mixed_fraction_charges_as_combined(tmp_path, fraction):
    pages = [
        [
            line.replace(
                "Hushållsavfall avtal 9902 Hushållsavfall",
                f"Hushållsavfall avtal 9902 {fraction}",
            )
            if isinstance(line, str)
            else line
            for line in page
        ]
        for page in multi_service()
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert candidate.issues == []
    combined = candidate.sections[1]
    assert combined.subcategory == "waste_collection" and combined.gross_minor == 13950
    assert {weight.subcategory for weight in combined.usages} == {"matavfall", "restavfall"}


@pytest.mark.parametrize(
    ("fraction", "key"), [("Matavfall", "matavfall"), ("Restavfall", "restavfall")]
)
def test_consistent_named_fraction_cost_block_is_classified_separately(fraction, key):
    assert (
        waste_cost_subcategory(
            [f"Hushållsavfall avtal 9902 {fraction}", f"Tömning {fraction} 1 st 30,00 kr"]
        )
        == key
    )


def test_multi_service_pdf_preserves_contracts_costs_and_billed_usage(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service())
    assert candidate.issues == []
    assert candidate.sections[0].usages[0].basis == "measured"
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
    assert [[u.subcategory for u in s.usages] for s in candidate.sections] == [
        [""],
        ["matavfall", "restavfall"],
        [],
        [""],
    ]
    assert all(
        (u.start, u.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for s in candidate.sections
        for u in s.usages
    )
    assert candidate.requires_review
    assert candidate.identifiers[0].value == "9900"
    assert all(
        (section.start, section.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for section in candidate.sections
    )
    assert all("charges" not in section for section in candidate.to_dict()["sections"])
    assert json.loads(json.dumps(candidate.to_dict()))["invoice_date"] == "2024-03-05"


def test_network_readings_keep_billed_intervals_without_repeating_price_quantities(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service(split_network_readings=True))
    assert candidate.issues == []
    network = candidate.sections[-1]
    assert [(row.quantity_milli, row.start, row.end, row.basis) for row in network.usages] == [
        (121125, date(2024, 2, 1), date(2024, 2, 15), "measured"),
        (200000, date(2024, 2, 15), date(2024, 3, 1), "measured"),
    ]
    assert sum(row.quantity_milli for row in network.usages) == 321125
    assert all(row.evidence.page == 4 for row in network.usages)
    assert all(row.evidence.text.startswith("Avläsning") for row in network.usages)
    assert (network.net_minor, network.vat_minor, network.gross_minor) == (37846, 9462, 47308)
    assert candidate.parser_revision == "10" and candidate.requires_review


@pytest.mark.parametrize("correction", [False, True])
def test_waste_subcategories_follow_billed_rows_across_container_headers_and_pages(
    tmp_path, correction
):
    pages = multi_service(correction=correction)
    for page in pages:
        for index, line in enumerate(page):
            if isinstance(line, str) and line.startswith("Matavfall 140L"):
                page[index] = line.replace("Matavfall", "MATAVFALL")
            if isinstance(line, str) and line.startswith("Restavfall 190L"):
                page[index] = line.replace("Restavfall", "restavfall")
    candidate = parse_synthetic(tmp_path, pages)
    assert candidate.issues == []
    waste = next(section for section in candidate.sections if section.service == "waste")
    assert waste.subcategory == "waste_collection"
    assert [(row.subcategory, row.quantity_milli) for row in waste.usages] == [
        ("matavfall", 12000),
        ("restavfall", 7500),
    ]
    assert all("Vikt/Volym" not in row.evidence.text for row in waste.usages)
    assert (waste.net_minor, waste.vat_minor, waste.gross_minor) == (11160, 2790, 13950)


@pytest.mark.parametrize(
    "readings,warning",
    [
        ([], "Missing billed electricity consumption"),
        (["Avläsning 15 feb 2024 321,125 kWh 8321,125"], "Incomplete electricity reading periods"),
        (
            [
                "Avläsning 15 feb 2024 121,125 kWh 8121,125",
                "Avläsning 15 feb 2024 200 kWh 8321,125",
            ],
            "Ambiguous electricity reading periods",
        ),
        (["Avläsning 2 mar 2024 321,125 kWh 8321,125"], "Ambiguous electricity reading periods"),
        (
            [
                "Avläsning 1 mar 2024 200 kWh 8321,125",
                "Avläsning 15 feb 2024 121,125 kWh 8121,125",
            ],
            "Ambiguous electricity reading periods",
        ),
        (["Avläsning 31 feb 2024 321,125 kWh 8321,125"], "Invalid billed electricity consumption"),
        (["Avläsning 1 mar 2024 321,125 kg 8321,125"], "Invalid billed electricity consumption"),
        (["Avläsning 1 mar 2024 321,1251 kWh 8321,125"], "Invalid billed electricity consumption"),
    ],
)
def test_invalid_network_readings_name_consumption_and_preserve_valid_costs(
    tmp_path, readings, warning
):
    pages = multi_service()
    pages[-1] = [
        line for line in pages[-1] if not (isinstance(line, str) and line.startswith("Avläsning"))
    ]
    pages[-1].extend(readings)
    candidate = parse_synthetic(tmp_path, pages)
    network = candidate.sections[-1]
    assert candidate.issues == [f"{warning} in section_04"]
    assert network.usages == []
    assert (network.net_minor, network.vat_minor, network.gross_minor) == (37846, 9462, 47308)
    assert (network.start, network.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert candidate.requires_review


@pytest.mark.parametrize(
    "label,old,new",
    [
        ("Avläsning 1 mar", "200 kWh", "201 kWh"),
        ("Energiskatt", "321,125 kWh", "322,125 kWh"),
        ("Energiavgift", "321,125 kWh", "321,1251 kWh"),
        ("Energiavgift", "321,125 kWh", "321,125 kg"),
    ],
)
def test_network_reading_sum_is_checked_against_each_billed_price_quantity(
    tmp_path, label, old, new
):
    pages = multi_service(split_network_readings=True)
    pages[-1] = [
        line.replace(old, new) if isinstance(line, str) and line.startswith(label) else line
        for line in pages[-1]
    ]
    candidate = parse_synthetic(tmp_path, pages)
    assert candidate.issues == ["Inconsistent billed kWh in section_04"]
    assert sum(row.quantity_milli for row in candidate.sections[-1].usages) == (
        322125 if label.startswith("Avläsning") else 321125
    )
    assert candidate.requires_review


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
    assert (water.start, water.end) == (date(2023, 12, 1), date(2024, 3, 1))
    assert water.period_evidence.page == 2
    assert [u.evidence.page for u in candidate.sections[1].usages] == [3, 3]


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
    assert [(s.start, s.end) for s in candidate.sections] == [
        (date(2024, 2, 1), date(2024, 3, 1)),
        (date(2024, 2, 1), date(2024, 3, 1)),
    ]
    assert all(
        (u.start, u.end) == (date(2024, 2, 1), date(2024, 3, 1))
        for s in candidate.sections
        for u in s.usages
    )


@pytest.mark.parametrize("change", ["rename", "remove", "malformed"])
def test_multi_service_fee_details_do_not_affect_totals_or_consumption(tmp_path, change):
    pages = multi_service(correction=True, split_network_readings=True)
    expected = parse_synthetic(tmp_path, pages).to_dict()
    fee_labels = (
        "Grundavgift",
        "Bostadsenhetsavgift",
        "Dagvattenavgift",
        "Effektavgift",
        "Hämtningsavgift",
        "Trädgårdsavfall",
    )
    for page in pages:
        for index in range(len(page) - 1, -1, -1):
            line = page[index]
            if isinstance(line, str) and line.startswith(fee_labels):
                if change == "remove":
                    page.pop(index)
                elif change == "rename":
                    page[index] = "Synthetic renamed fee " + line.split(" ", 1)[1]
                else:
                    page[index] = "Synthetic fee with unreadable amounts ?? kr"
            elif (
                change == "malformed"
                and isinstance(line, str)
                and line.startswith(
                    ("Förbrukning", "Matavfall", "Restavfall", "Energiavgift", "Energiskatt")
                )
            ):
                # Retain required quantities and periods; fee amounts are optional.
                page[index] = line.rsplit(" ", 2)[0] + " ?? kr"
    candidate = parse_synthetic(tmp_path, pages)
    actual = candidate.to_dict()

    def facts(value):
        if isinstance(value, dict):
            return {key: facts(item) for key, item in value.items() if not key.endswith("evidence")}
        if isinstance(value, list):
            return [facts(item) for item in value]
        return value

    # Removing PDF rows changes geometry but must preserve the core billing facts.
    assert facts(actual) == facts(expected)
    assert candidate.issues == []


@pytest.mark.parametrize("parser,version", [(layout_02, "v1"), (layout_02_v2, "v2")])
@pytest.mark.parametrize("change", ["remove_fixed_fee", "malformed_amounts"])
def test_supply_fee_amounts_are_optional_when_totals_and_kwh_are_valid(
    tmp_path, parser, version, change
):
    pages = shared_supply(version=version)
    expected = parse_synthetic(tmp_path, pages, parser)
    for page in pages[1:]:
        for index in range(len(page) - 1, -1, -1):
            line = page[index]
            if not isinstance(line, str) or not line.startswith(
                ("Medelspotpris", "Rörliga kostnader", "Fasta påslag", "Fast avgift")
            ):
                continue
            if change == "remove_fixed_fee" and line.startswith("Fast avgift"):
                page.pop(index)
            elif change == "malformed_amounts":
                page[index] = line.rsplit(" ", 2)[0] + " ?? kr"
    candidate = parse_synthetic(tmp_path, pages, parser)
    assert candidate.issues == []
    assert [
        (s.net_minor, s.vat_minor, s.gross_minor, s.start, s.end) for s in candidate.sections
    ] == [(s.net_minor, s.vat_minor, s.gross_minor, s.start, s.end) for s in expected.sections]
    assert [
        [(u.quantity_milli, u.start, u.end, u.basis) for u in s.usages] for s in candidate.sections
    ] == [
        [(u.quantity_milli, u.start, u.end, u.basis) for u in s.usages] for s in expected.sections
    ]


@pytest.mark.parametrize("parser,version", [(layout_02, "v1"), (layout_02_v2, "v2")])
def test_supply_invalid_consumption_keeps_independent_printed_cost_totals(
    tmp_path, parser, version
):
    pages = shared_supply(version=version)
    pages[1] = [
        line.replace("140,250 kWh", "140,2501 kWh") if isinstance(line, str) else line
        for line in pages[1]
    ]
    candidate = parse_synthetic(tmp_path, pages, parser)
    section = candidate.sections[0]
    assert section.usages == []
    assert (section.net_minor, section.vat_minor, section.gross_minor) == (11055, 2764, 13819)
    assert (section.start, section.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert "Invalid billed electricity consumption in section_01" in candidate.issues


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
    assert "Invalid water quantity or period in section_01" in candidate.issues
    assert candidate.sections[0].usages == []
    assert candidate.sections[0].gross_minor == 55000


def test_printed_vat_is_retained_without_inference_from_individual_fees(tmp_path):
    candidate = parse_synthetic(tmp_path, multi_service())
    water = candidate.sections[0]
    water.vat_minor += 101
    water.gross_minor += 101
    candidate.vat_minor += 101
    candidate.invoice_total_minor += 101
    candidate.amount_due_minor += 101
    assert reconcile(candidate).issues == []
    assert (water.net_minor, water.vat_minor, water.gross_minor) == (44000, 11101, 55101)


def test_mismatched_cost_totals_are_flagged_without_rewriting_printed_values(tmp_path):
    candidate = parse_synthetic(tmp_path, shared_supply(), layout_02)
    section = candidate.sections[0]
    section.net_minor = 10**12
    section.vat_minor = section.net_minor // 4
    result = reconcile(candidate)
    assert "Gross total mismatch in section_01" in result.issues
    assert "Invoice net mismatch" in result.issues and "Invoice VAT mismatch" in result.issues
    assert (section.net_minor, section.vat_minor, section.gross_minor) == (
        10**12,
        10**12 // 4,
        13819,
    )


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
