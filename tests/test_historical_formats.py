from datetime import date

import pytest

from bills.parsing import (
    layout_01,
    layout_01_v2,
    layout_05,
    layout_05_v2,
    layout_06_v2,
    layout_08,
)
from bills.parsing.reader import read_pdf
from bills.parsing.recognition import recognize_vendor
from bills.parsing.registry import match_layouts
from bills.parsing.types import Evidence

from .current_formats import chart_supply, monthly_network, quarterly_waste
from .synthetic import multi_service, write_pdf


def parse(tmp_path, pages, parser):
    source = read_pdf(write_pdf(tmp_path / "synthetic.pdf", pages))
    assert match_layouts(source, [parser.FAMILY]) == [parser]
    return parser.parse(source)


def meter_supply():
    pages = chart_supply()
    page = pages[1]
    index = page.index("Period Förbrukning")
    page[index : index + 2] = [
        "Datum Mätarställning",
        "1750 2024-01-31 80 000",
        "1500 2024-02-29 80 321,125",
        "1250 Förbrukning 321,125 kWh",
    ]
    pages[0] = [line.replace("Oss tillhanda senast", "Betala senast") for line in pages[0]]
    pages[1] = [
        line.replace("Medelspotpris", "El")
        .replace("Rörliga kostnader", "Spotpåslag")
        .replace("Fast påslag spot", "Elcertifikatskostnad")
        for line in page
    ]
    return pages


def exclusive_network():
    pages = monthly_network()
    pages[1] = [
        line.replace("Antal Pris inkl. moms Summa inkl. moms", "Antal Pris exkl. moms Summa")
        .replace("Överföringsavgift", "Rörlig elnätsavgift")
        .replace("Statlig energiskatt", "Energiskatt")
        for line in pages[1]
    ]
    index = next(i for i, line in enumerate(pages[1]) if line.startswith("Energiskatt 100,125"))
    pages[1][index : index + 1] = [
        "Energiskatt 1 feb-24 40,125 kWh 10,00 öre/kWh 10,00 kr",
        "Energiskatt 15 feb-24 60 kWh 20,00 öre/kWh 20,00 kr",
    ]
    return pages


def compact_supply():
    return [
        [
            "FAKTURA",
            "Fakturadatum: Förfallodag:",
            "Example recipient 2024-03-05 25mars2024*",
            "Kundnummer: OCRNr:",
            "400008 910008",
            "Faktura1februari-29februari2024",
            "Underperiodenharduförbrukat: 80,125kWh",
            "Totalbeloppattbetala: 100,15kr",
            "Oss tillhanda 2024-03-25",
            "VAT-nr SE000000888801",
        ],
        [
            "DITTELAVTAL ANLÄGGNINGSUPPGIFTER MINASIDOR",
            "ID:000000000000000081",
            "Beräknadårsförbrukning:99999kWh",
            "Historical chart: 2000kWh",
            "Öresavrundning:0,15kr",
            "Totalbelopp:100,15kr",
            "(Varavmoms20,00kr)",
        ],
    ]


@pytest.mark.parametrize("meter_first", [False, True])
def test_network_column_order_preserves_grouped_quantity(tmp_path, meter_first):
    pages = multi_service()
    pages[-1] = [
        line.replace("321,125", "1 321,125") if isinstance(line, str) else line
        for line in pages[-1]
    ]
    if meter_first:
        pages[-1] = [
            line.replace("Mätarnummer 909093", "Mätarnummer 909093 Ställning Förbrukning").replace(
                "1 321,125 kWh 8321,125", "8321,125 1 321,125 kWh"
            )
            if isinstance(line, str)
            else line
            for line in pages[-1]
        ]
    candidate = parse(tmp_path, pages, layout_01)
    assert not candidate.issues
    assert candidate.sections[-1].usages[0].quantity_milli == 1321125


def test_contract_specification_accepts_page_suffix_and_separate_fee_periods(tmp_path):
    pages = multi_service()
    pages = [
        [
            line.replace("FAKTURASPECIFIKATION", "FAKTURASPECIFIKATION Sida: 2/4")
            if isinstance(line, str)
            else line
            for line in page
        ]
        for page in pages
    ]
    pages[2] = [line for line in pages[2] if line != "Fakturaperiod 1 feb 2024 - 1 mar 2024"]
    pages[2] = [
        line.replace(
            "Grundavgift avfall 29 dgr", "Grundavgift avfall 1 feb 2024 - 1 mar 2024 29 dgr"
        )
        if isinstance(line, str)
        else line
        for line in pages[2]
    ]
    pages[2] = [
        line.replace("Matavfall 140L kärl 12", "Matavfall 140L kärl 15 jan 2024 - 15 feb 2024 12")
        if isinstance(line, str)
        else line
        for line in pages[2]
    ]
    candidate = parse(tmp_path, pages, layout_01)
    assert not candidate.issues
    waste = candidate.sections[1]
    assert (waste.start, waste.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert (waste.usages[0].start, waste.usages[0].end) == (date(2024, 1, 15), date(2024, 2, 15))


def test_product_number_sections_and_kbm_keep_source_evidence(tmp_path):
    pages = multi_service()
    pages[0] = [line.replace("OCR-nummer:", "Fakturanummer") for line in pages[0]]
    pages[2] = [
        line.replace("Vatten avtal 9901", "Vatten & Avlopp prodnr 9901").replace("m3", "kbm")
        if isinstance(line, str)
        else line
        for line in pages[2]
    ]
    pages[-1] = [
        line.replace("Elnät avtal 9904", "Elnätavgifter prodnr 9904")
        if isinstance(line, str)
        else line
        for line in pages[-1]
    ]
    candidate = parse(tmp_path, pages, layout_01_v2)
    assert not candidate.issues
    assert candidate.reference == "880001"
    water = candidate.sections[0]
    assert water.identifiers[0].kind == "product"
    assert water.usages[0].unit == "m3" and water.usages[0].quantity_milli == 12000
    assert "kbm" in water.usages[0].evidence.text


def test_network_price_periods_reconcile_separate_readings(tmp_path):
    pages = multi_service(split_network_readings=True)
    pages[-1] = [
        line
        for line in pages[-1]
        if not (isinstance(line, str) and line.startswith(("Energiavgift", "Energiskatt")))
    ]
    pages[-1].extend(
        [
            "Energiskatt 1 feb 2024 - 15 feb 2024 121,125 kWh 10,00 öre/kWh 12,11 kr",
            "Energiskatt 15 feb 2024 - 1 mar 2024 200 kWh 10,00 öre/kWh 20,00 kr",
        ]
    )
    candidate = parse(tmp_path, pages, layout_01)
    assert not candidate.issues
    assert [row.quantity_milli for row in candidate.sections[-1].usages] == [121125, 200000]


def test_missing_reversal_period_retains_costs_and_requires_review(tmp_path):
    pages = multi_service(correction=True)
    pages[1] = [
        line.replace("Förbrukning 1 dec 2023 - 1 jan 2024", "Förbrukning Faktura 910099")
        if isinstance(line, str)
        else line
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_01)
    assert any("Invalid water quantity or period" in issue for issue in candidate.issues)
    assert candidate.sections[0].gross_minor == 63750
    assert [row.quantity_milli for row in candidate.sections[0].usages] == [45000, -14000]


def test_waste_emptying_is_one_day_cost_without_container_volume_usage(tmp_path):
    pages = quarterly_waste()
    pages[0] = [line for line in pages[0] if not line.startswith(("Grundavgift", "Tömn"))]
    pages[0].append(
        "Slamavskiljare 1.1-2 m³ ordinarie tömning 2024-10-08 - 2024-10-08 1 ST 104,05 kr"
    )
    from bills.parsing import layout_03

    candidate = parse(tmp_path, pages, layout_03)
    assert not candidate.issues
    assert (candidate.sections[0].start, candidate.sections[0].end) == (
        date(2024, 10, 8),
        date(2024, 10, 9),
    )
    assert not candidate.sections[0].usages


@pytest.mark.parametrize(
    "parser,fixture,quantity_milli",
    [
        (layout_05_v2, meter_supply, 321125),
        (layout_06_v2, exclusive_network, 100125),
        (layout_08, compact_supply, 80125),
    ],
)
def test_added_structures_keep_totals_one_consumption_and_evidence(
    tmp_path, parser, fixture, quantity_milli
):
    candidate = parse(tmp_path, fixture(), parser)
    assert not candidate.issues
    assert (candidate.net_minor, candidate.vat_minor) == (8000, 2000)
    assert candidate.sections[0].gross_minor == 10000
    assert [row.quantity_milli for row in candidate.sections[0].usages] == [quantity_milli]
    assert (candidate.sections[0].start, candidate.sections[0].end) == (
        date(2024, 2, 1),
        date(2024, 3, 1),
    )
    assert candidate.requires_review


@pytest.mark.parametrize(
    "parser,fixture,billed",
    [
        (layout_05_v2, meter_supply, "321,125"),
        (layout_06_v2, exclusive_network, "100,125"),
        (layout_08, compact_supply, "80,125"),
    ],
)
def test_added_structures_reject_overprecise_consumption_independently_of_costs(
    tmp_path, parser, fixture, billed
):
    pages = [[line.replace(billed, billed + "1") for line in page] for page in fixture()]
    candidate = parse(tmp_path, pages, parser)
    assert any("Invalid billed electricity consumption" in issue for issue in candidate.issues)
    assert candidate.sections[0].gross_minor == 10000
    assert not candidate.sections[0].usages


def test_meter_consumption_disagreement_is_not_silently_replaced_by_price_quantity(tmp_path):
    pages = meter_supply()
    pages[1] = [line.replace("Förbrukning 321,125", "Förbrukning 322,125") for line in pages[1]]
    candidate = parse(tmp_path, pages, layout_05_v2)
    assert "Inconsistent billed kWh in section_01" in candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 322125


def test_compact_supply_keeps_partial_month_and_exact_header(tmp_path):
    pages = compact_supply()
    pages[0] = [
        line.replace("Faktura1februari-29februari2024", "Faktura1feb.-14feb.2024")
        for line in pages[0]
    ]
    candidate = parse(tmp_path, pages, layout_08)
    assert not candidate.issues
    assert (candidate.reference, candidate.invoice_date, candidate.due_date) == (
        "910008",
        date(2024, 3, 5),
        date(2024, 3, 25),
    )
    assert (candidate.rounding_minor, candidate.invoice_total_minor) == (15, 10015)
    assert candidate.sections[0].end == date(2024, 2, 15)


@pytest.mark.django_db
@pytest.mark.parametrize("compact_footer", [False, True])
def test_issuer_legal_footer_is_bounded_when_wrapped_or_compact(compact_footer):
    from bills.models import Vendor, VendorIssuerIdentifier

    vendor = Vendor.objects.create(name="Synthetic Supplier", slug="synthetic-supplier")
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="SE000000999901")
    marker = "GodkändförF-skatt" if compact_footer else "F-skatt."
    pages = [
        [
            Evidence(1, marker, (50, 510, 150, 520)),
            Evidence(1, "Synthetic Supplier SE000000999901", (50, 545, 400, 555)),
        ]
    ]
    assert recognize_vendor(pages).vendor == vendor
    pages[0][0] = Evidence(1, marker, (50, 300, 150, 310))
    assert recognize_vendor(pages).vendor is None


def test_period_table_and_meter_table_versions_remain_distinct(tmp_path):
    assert parse(tmp_path, chart_supply(), layout_05).layout_version == "v1"
    assert parse(tmp_path, meter_supply(), layout_05_v2).layout_version == "v2"


def test_contract_supply_is_an_independent_service_with_single_billed_consumption(tmp_path):
    pages = multi_service()
    pages[-1] = [
        line.replace("Elnät avtal", "Elhandel avtal") if isinstance(line, str) else line
        for line in pages[-1]
    ]
    candidate = parse(tmp_path, pages, layout_01)
    assert not candidate.issues
    supply = candidate.sections[-1]
    assert supply.service == "electricity_supply"
    assert [(row.quantity_milli, row.basis) for row in supply.usages] == [(321125, "billed")]


def test_header_totals_can_be_recovered_from_later_explicit_table(tmp_path):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", multi_service()))
    pages[0] = [
        line
        for line in pages[0]
        if not line.text.startswith(("Summa exkl", "Moms:", "Öresutjämning", "Att betala"))
    ]
    pages[1].extend(
        [
            Evidence(2, "Summa exkl moms Moms Öresutjämning Att betala", (50, 300, 550, 310)),
            Evidence(2, "1 010,06 kr", (100, 314, 180, 324)),
            Evidence(2, "252,52 kr", (200, 314, 280, 324)),
            Evidence(2, "0,42 kr", (300, 314, 380, 324)),
            Evidence(2, "1 263,00 kr", (400, 314, 480, 324)),
        ]
    )
    candidate = layout_01.parse(pages)
    assert not candidate.issues
    assert (candidate.net_minor, candidate.vat_minor, candidate.rounding_minor) == (
        101006,
        25252,
        42,
    )
    assert candidate.header_evidence["vat"].page == 2


@pytest.mark.parametrize(
    "parser,fixture",
    [(layout_05_v2, meter_supply), (layout_06_v2, exclusive_network), (layout_08, compact_supply)],
)
def test_new_structures_keep_signed_credits(tmp_path, parser, fixture):
    import re

    pages = [
        [re.sub(r"(?<![\d,])\d+,\d{2}(?!\d)", lambda match: "-" + match[0], line) for line in page]
        for page in fixture()
    ]
    if parser is layout_05_v2:
        pages = [[line.replace("321,125", "-321,125") for line in page] for page in pages]
    elif parser is layout_06_v2:
        pages = [
            [
                line.replace("100,125", "-100,125")
                .replace("40,125", "-40,125")
                .replace("60 kWh", "-60 kWh")
                for line in page
            ]
            for page in pages
        ]
    else:
        pages = [[line.replace("80,125", "-80,125") for line in page] for page in pages]
    candidate = parse(tmp_path, pages, parser)
    assert not candidate.issues
    assert candidate.sections[0].gross_minor == -10000
    assert candidate.sections[0].usages[0].quantity_milli < 0


def test_network_rate_year_is_not_part_of_three_digit_consumption(tmp_path):
    pages = exclusive_network()
    pages[1] = [
        line.replace("100,125", "1 000").replace("40,125", "400").replace("60 kWh", "600 kWh")
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert not candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 1000000


def test_network_changed_rates_are_bounded_to_billing_tables(tmp_path):
    pages = exclusive_network()
    pages[0][0] = "Faktura elnät Sid 1 av 2"
    pages[1] = [
        line.replace(
            "Antal Pris exkl. moms Summa", "Pris from Antal Pris inkl. moms Summa inkl. moms"
        )
        for line in pages[1]
    ]
    index = next(i for i, line in enumerate(pages[1]) if line.startswith("Rörlig elnätsavgift"))
    pages[1][index : index + 1] = [
        "Rörlig elnätsavgift 1 feb-24 40,125 kWh 1,00 öre/kWh 10,00 kr",
        "Rörlig elnätsavgift 15 feb-24 60 kWh 2,00 öre/kWh 10,00 kr",
    ]
    pages.append(["Price examples", "Rörlig elnätsavgift 99999 kWh 99,00 kr"])
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert not candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 100125


@pytest.mark.parametrize(
    "parser,fixture,old,new",
    [
        (
            layout_06_v2,
            exclusive_network,
            "1 feb 2024 t o m 29 feb 2024",
            "29 feb 2024 t o m 1 feb 2024",
        ),
        (
            layout_08,
            compact_supply,
            "Faktura1februari-29februari2024",
            "Faktura29februari-1februari2024",
        ),
    ],
)
def test_new_structures_retain_costs_when_billing_period_is_invalid(
    tmp_path, parser, fixture, old, new
):
    pages = [[line.replace(old, new) for line in page] for page in fixture()]
    candidate = parse(tmp_path, pages, parser)
    assert candidate.sections[0].gross_minor == 10000
    assert not candidate.sections[0].usages
    assert any("billing period" in issue for issue in candidate.issues)


@pytest.mark.parametrize("dated_header", ["network", "tax", "both", "neither"])
def test_dated_network_rows_route_to_v2_with_mixed_price_headers(tmp_path, dated_header):
    pages = monthly_network()
    header_indices = [
        i for i, line in enumerate(pages[1]) if line == "Antal Pris inkl. moms Summa inkl. moms"
    ]
    for kind, index in zip(("network", "tax"), header_indices, strict=True):
        if dated_header in (kind, "both"):
            pages[1][index] = "Pris from " + pages[1][index]
    pages[1] = [
        line.replace("Överföringsavgift 100,125", "Rörlig elnätsavgift 1 feb-24 600").replace(
            "Energiskatt 100,125", "Energiskatt 1 feb-24 600"
        )
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert not candidate.issues
    usage = candidate.sections[0].usages[0]
    assert usage.quantity_milli == 600000
    assert "1 feb-24 600 kWh" in usage.evidence.text


def test_compact_supply_keeps_detail_total_when_payment_amount_disagrees(tmp_path):
    pages = compact_supply()
    pages[0] = [line.replace("100,15kr", "200,15kr") for line in pages[0]]
    candidate = parse(tmp_path, pages, layout_08)
    assert candidate.amount_due_minor == 20015
    assert candidate.invoice_total_minor == 10015
    assert (candidate.net_minor, candidate.vat_minor) == (8000, 2000)
    assert candidate.sections[0].gross_minor == 10000
    assert candidate.issues == ["Amount due differs from invoice total"]
    assert candidate.header_evidence["invoice_total"].text == "Totalbelopp:100,15kr"
    assert candidate.sections[0].total_evidence.page == 2


@pytest.mark.parametrize("detail_total", ["", "Totalbelopp:100,151kr"])
def test_compact_supply_does_not_infer_costs_from_payment_when_detail_total_is_invalid(
    tmp_path, detail_total
):
    pages = compact_supply()
    pages[1] = [detail_total if line.startswith("Totalbelopp:") else line for line in pages[1]]
    candidate = parse(tmp_path, pages, layout_08)
    assert candidate.amount_due_minor == 10015
    assert candidate.invoice_total_minor is None
    assert candidate.net_minor is None
    assert candidate.sections[0].gross_minor is None
    assert "Missing or invalid invoice_total_minor" in candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 80125


@pytest.mark.parametrize("effective", ["1 jan-24", "1 dec-23"])
def test_network_prices_can_take_effect_before_billed_period(tmp_path, effective):
    pages = monthly_network()
    pages[1] = [
        line.replace(
            "Antal Pris inkl. moms Summa inkl. moms", "Pris from Antal Pris exkl. moms Summa"
        )
        .replace("Överföringsavgift 100,125", f"Rörlig elnätsavgift {effective} 100,125")
        .replace("Energiskatt 100,125", f"Energiskatt {effective} 100,125")
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert not candidate.issues
    usage = candidate.sections[0].usages[0]
    assert usage.quantity_milli == 100125
    assert (usage.start, usage.end) == (date(2024, 2, 1), date(2024, 3, 1))
    assert effective in usage.evidence.text


@pytest.mark.parametrize("effective", ["1 mar-24", "31 feb-24"])
def test_network_future_or_invalid_rate_dates_keep_costs_and_require_review(tmp_path, effective):
    pages = exclusive_network()
    pages[1] = [
        line.replace("Rörlig elnätsavgift 100,125", f"Rörlig elnätsavgift {effective} 100,125")
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert candidate.sections[0].gross_minor == 10000
    assert not candidate.sections[0].usages
    assert "Invalid billed electricity consumption in section_01" in candidate.issues


@pytest.mark.parametrize(
    "tax_rows",
    [
        [("1", "100,125")],
        [("1", "30"), ("10", "70,125")],
        [("1", "30"), ("10", "10,125"), ("15", "50"), ("20", "10")],
    ],
)
def test_network_and_tax_quantities_allow_independent_rate_changes(tmp_path, tax_rows):
    pages = exclusive_network()
    index = next(i for i, line in enumerate(pages[1]) if line.startswith("Rörlig elnätsavgift"))
    pages[1][index : index + 1] = [
        "Rörlig elnätsavgift 1 feb-24 40,125 kWh 1,00 öre/kWh 10,00 kr",
        "Rörlig elnätsavgift 15 feb-24 60 kWh 2,00 öre/kWh 10,00 kr",
    ]
    index = next(i for i, line in enumerate(pages[1]) if line.startswith("Energiskatt 1 feb-24"))
    pages[1][index : index + 2] = [
        f"Energiskatt {day} feb-24 {billed} kWh 10,00 öre/kWh 10,00 kr" for day, billed in tax_rows
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert not candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 100125


def test_network_and_tax_matching_totals_still_reconcile_shared_rate_intervals(tmp_path):
    pages = exclusive_network()
    index = next(i for i, line in enumerate(pages[1]) if line.startswith("Rörlig elnätsavgift"))
    pages[1][index : index + 1] = [
        "Rörlig elnätsavgift 1 feb-24 40,125 kWh 1,00 öre/kWh 10,00 kr",
        "Rörlig elnätsavgift 15 feb-24 60 kWh 2,00 öre/kWh 10,00 kr",
    ]
    pages[1] = [
        line.replace("1 feb-24 40,125", "1 feb-24 50,125").replace(
            "15 feb-24 60 kWh", "15 feb-24 50 kWh"
        )
        if line.startswith("Energiskatt ")
        else line
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_06_v2)
    assert "Inconsistent billed kWh in section_01" in candidate.issues
    assert candidate.sections[0].usages[0].quantity_milli == 100125


@pytest.mark.parametrize("rounding", ["0,42 kr", "0,421 kr"])
def test_split_cover_totals_clear_warning_only_after_complete_recovery(tmp_path, rounding):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", multi_service()))
    pages[0] = [
        line
        for line in pages[0]
        if not line.text.startswith(("Summa exkl", "Moms:", "Öresutjämning", "Att betala"))
    ]
    pages[0].append(
        Evidence(1, "Summa exkl moms Moms Öresutjämning Att betala", (50, 300, 550, 310))
    )
    pages[0].extend(
        Evidence(1, value, (x, 314, x + 80, 324))
        for x, value in [
            (100, "1 010,06 kr"),
            (200, "252,52 kr"),
            (300, rounding),
            (400, "1 263,00 kr"),
        ]
    )
    candidate = layout_01.parse(pages)
    assert (candidate.net_minor, candidate.vat_minor, candidate.amount_due_minor) == (
        101006,
        25252,
        126300,
    )
    if rounding == "0,42 kr":
        assert candidate.rounding_minor == 42
        assert not candidate.issues
    else:
        assert candidate.rounding_minor is None
        assert "Missing or invalid header totals row" in candidate.issues


@pytest.mark.parametrize("offset", [-15, 10])
@pytest.mark.parametrize("sidebar", [False, True])
def test_compact_supply_recovers_split_detail_total_with_sidebar_and_different_font(
    tmp_path, offset, sidebar
):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", compact_supply()))
    pages[1] = [line for line in pages[1] if not line.text.startswith("Totalbelopp:")]
    label = "Totalbelopp: Synthetic sidebar text" if sidebar else "Totalbelopp:"
    amount = "100,15 kr Synthetic sidebar text" if sidebar else "100,15 kr"
    pages[1].extend(
        [
            Evidence(2, amount, (180, 400 + offset, 530, 428 + offset)),
            Evidence(2, label, (40, 400, 520 if sidebar else 130, 410)),
        ]
    )
    assert match_layouts(pages, [layout_08.FAMILY]) == [layout_08]
    candidate = layout_08.parse(pages)
    assert not candidate.issues
    assert (candidate.net_minor, candidate.invoice_total_minor) == (8000, 10015)
    assert candidate.sections[0].gross_minor == 10000
    assert candidate.header_evidence["invoice_total"].text == amount


def test_compact_supply_split_total_retains_disagreement_with_cover(tmp_path):
    pages = compact_supply()
    pages[0] = [line.replace("100,15kr", "200,15kr") for line in pages[0]]
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", pages))
    pages[1] = [line for line in pages[1] if not line.text.startswith("Totalbelopp:")]
    pages[1].extend(
        [
            Evidence(2, "100,15 kr", (180, 385, 300, 413)),
            Evidence(2, "Totalbelopp: Synthetic sidebar", (40, 400, 530, 410)),
        ]
    )
    candidate = layout_08.parse(pages)
    assert candidate.issues == ["Amount due differs from invoice total"]
    assert (candidate.invoice_total_minor, candidate.amount_due_minor) == (10015, 20015)
    assert candidate.sections[0].gross_minor == 10000


@pytest.mark.parametrize("page,x,top", [(3, 180, 385), (2, 10, 385), (2, 180, 450)])
def test_compact_supply_does_not_use_unrelated_amounts_for_missing_split_total(
    tmp_path, page, x, top
):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", compact_supply()))
    pages[1] = [line for line in pages[1] if not line.text.startswith("Totalbelopp:")]
    pages[1].extend(
        [
            Evidence(2, "Totalbelopp:", (40, 400, 130, 410)),
            Evidence(page, "100,15 kr", (x, top, x + 100, top + 28)),
        ]
    )
    candidate = layout_08.parse(pages)
    assert candidate.invoice_total_minor is None
    assert candidate.sections[0].gross_minor is None
    assert "Missing or invalid invoice_total_minor" in candidate.issues


def test_compact_supply_ambiguous_split_total_requires_review(tmp_path):
    pages = read_pdf(write_pdf(tmp_path / "synthetic.pdf", compact_supply()))
    pages[1] = [line for line in pages[1] if not line.text.startswith("Totalbelopp:")]
    pages[1].extend(
        [
            Evidence(2, "Totalbelopp:", (40, 400, 130, 410)),
            Evidence(2, "100,15 kr", (180, 385, 280, 413)),
            Evidence(2, "200,15 kr", (300, 410, 400, 438)),
        ]
    )
    candidate = layout_08.parse(pages)
    assert "Ambiguous invoice_total_minor" in candidate.issues


def test_compact_supply_inline_total_can_share_line_with_vat(tmp_path):
    pages = compact_supply()
    pages[1] = [
        line.replace("Totalbelopp:100,15kr", "Totalbelopp:100,15kr (Varavmoms20,00kr)")
        for line in pages[1]
    ]
    candidate = parse(tmp_path, pages, layout_08)
    assert not candidate.issues
    assert candidate.invoice_total_minor == 10015
