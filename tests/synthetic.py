"""Wholly invented PDFs. No source files, metadata, names or numbers are copied."""

from pathlib import Path

from reportlab.pdfgen.canvas import Canvas


def write_pdf(path: Path, pages: list[list[str | tuple[str, str]]]) -> Path:
    canvas = Canvas(str(path), pagesize=(595, 842), invariant=1)
    canvas.setTitle("Synthetic utility invoice")
    canvas.setAuthor("House Bills synthetic test generator")
    canvas.setCreator("House Bills tests")
    for lines in pages:
        canvas.setFont("Helvetica", 8)
        y = 805
        for line in lines:
            if isinstance(line, tuple):
                # A different baseline for the amount exercises real PDF line clustering.
                canvas.drawString(50, y, line[0])
                canvas.drawString(500, y + 3.1, line[1])
            else:
                canvas.drawString(50, y, line)
            y -= 14
        canvas.showPage()
    canvas.save()
    return path


def multi_service(
    *, correction: bool = False, split_network_readings: bool = False, demand: bool = True
) -> list[list[str | tuple[str, str]]]:
    reference = "880002" if correction else "880001"
    if correction:
        cover = [
            "Faktura",
            "Fakturadatum 5 mar 2024",
            f"Faktura-/OCR-nummer {reference}",
            "Exempelverk AB",
            "Kundnummer 9900",
            "Anläggnings-ID 000 000 000 000 000 001",
            "Oss tillhanda senast Summa exkl moms Moms Öresutjämning Att betala (SEK)",
            "29 mar 2024 1 080,06 kr 270,02 kr -0,08 kr 1 350,00 kr",
            "Oss tillhanda senast 29 mar 2024",
        ]
    else:
        cover = [
            "Fakturasammanställning",
            "Exempelverk AB",
            "Fakturadatum 2024-03-05",
            "Förfallodatum: 2024-03-29",
            f"OCR-nummer: {reference}",
            "Kundnummer 9900",
            "Summa exkl. moms: 1 010,06 kr",
            "Moms: 252,52 kr",
            "Öresutjämning 0,42 kr",
            "Att betala 1 263,00 kr",
        ]
    water = [
        "FAKTURASPECIFIKATION",
        f"Faktura-/OCR-nummer {reference}",
        "Vatten avtal 9901 Distributionsavtal",
        "Beräknad årsförbrukning 500.00 m3",
        "Mätarnummer 909090",
        "Föregående avläsning 1 feb 2024 0 m3 300",
        f"Avläsning 1 mar 2024 {'45' if correction else '12'} m3 345",
        f"Avstämd period {'1 dec 2023' if correction else '1 feb 2024'} - 1 mar 2024",
        "Grundavgift 1 feb 2024 - 1 mar 2024 29 dgr 1 200,00 kr/år 100,00 kr",
        "Bostadsenhetsavgift 1 feb 2024 - 1 mar 2024 29 dgr 2 400,00 kr/år 200,00 kr",
        "Förbrukning "
        + ("1 dec 2023 - 1 mar 2024 45" if correction else "1 feb 2024 - 1 mar 2024 12")
        + f" m3 10,00 kr/m3 {'450' if correction else '120'},00 kr",
        "Dagvattenavgift gata 1 feb 2024 - 1 mar 2024 29 dgr 240,00 kr/år 20,00 kr",
    ]
    if correction:
        water += [
            "Summa 770,00 kr",
            "Avgår tidigare fakturerat 1 dec 2023 - 1 feb 2024",
            "Förbrukning 1 dec 2023 - 1 jan 2024 -12 m3 10,00 kr/m3 -120,00 kr",
            "Förbrukning 1 jan 2024 - 1 feb 2024 -14 m3 10,00 kr/m3 -140,00 kr",
            "Summa -260,00 kr",
            "Summa avstämd period 510,00 kr",
            "Moms 25 % på 510,00 kr 127,50 kr",
            ("Summa Vatten", "637,50 kr"),
        ]
    else:
        water += [
            "Summa 440,00 kr",
            "Moms 25 % på 440,00 kr 110,00 kr",
            ("Summa Vatten", "550,00 kr"),
        ]
    waste = [
        "Hushållsavfall avtal 9902 Hushållsavfall",
        "Fakturaperiod 1 feb 2024 - 1 mar 2024",
        "Kärl Restavfall 190 liter 9091",
        "Vikt/Volym 5 feb 2024 7,5 kg",
        "Kärl Matavfall 140 liter 9092",
        "Vikt/Volym 5 feb 2024 12 kg",
        "Grundavgift avfall 29 dgr 480,00 kr/år 40,00 kr",
    ]
    waste_end = [
        "Matavfall 140L kärl 12 kg 0,80 kr/kg 9,60 kr",
        "Restavfall 190L kärl 7,5 kg 4,00 kr/kg 30,00 kr",
        "Hämtningsavgift behov 4 st 8,00 kr/st 32,00 kr",
        "Moms 25 % på 111,60 kr 27,90 kr",
    ]
    garden = [
        "Hushållsavfall avtal 9903 Trädgårdsavfall",
        "Fakturaperiod 1 feb 2024 - 1 mar 2024",
        "Trädgårdsavfall 370L kärl 17 ggr/år 29 dgr 800,00 kr/år 80,00 kr",
        "Moms 25 % på 80,00 kr 20,00 kr",
        ("Summa Hushållsavfall", "239,50 kr"),
    ]
    readings = (
        [
            "Avläsning 15 feb 2024 121,125 kWh 8121,125",
            "Avläsning 1 mar 2024 200 kWh 8321,125",
        ]
        if split_network_readings
        else ["Avläsning 1 mar 2024 321,125 kWh 8321,125"]
    )
    network = [
        "Elnät avtal 9904 Nätavtal",
        "Beräknad årsförbrukning 9999.999 kWh",
        "Mätarnummer 909093",
        "Föregående avläsning 1 feb 2024 0 kWh 8000",
        *readings,
        "Avstämd period 1 feb 2024 - 1 mar 2024",
        "Grundavgift 16 A 1 feb 2024 - 1 mar 2024 29 dgr 1200,00 kr/år 100,00 kr",
        ("Effektavgift" if demand else "Network fee")
        + " 1 feb 2024 - 1 mar 2024 3 kW 50,00 kr/kW 150,00 kr",
        "Energiavgift 1 feb 2024 - 1 mar 2024 321,125 kWh 4,00 öre/kWh 12,85 kr",
        "Energiskatt 1 feb 2024 - 1 mar 2024 321,125 kWh 36,00 öre/kWh 115,61 kr",
        "Summa 378,46 kr",
        "Moms 25 % på 378,46 kr 94,62 kr",
        ("Summa Exempelnät AB SE000000000001", "473,08 kr"),
        "Information: historik 4500 kWh, jämförpris 2000 kWh, kärl 190 liter",
    ]
    if correction:
        return [cover, water + waste, ["FAKTURASPECIFIKATION"] + waste_end + garden + network]
    summary = [
        "FAKTURA",
        "Anläggnings-ID 000 000 000 000 000 001",
        "Vatten 550,00 kr",
        "Hushållsavfall 239,50 kr",
        "Elnät 473,08 kr",
    ]
    return [cover, summary, water + waste + waste_end, ["FAKTURASPECIFIKATION"] + garden + network]


def shared_supply(*, version="v1") -> list[list[str | tuple[str, str]]]:
    cover = [
        "Samfaktura.",
        "Fakturadatum: 2024-03-05",
        "OCR-/fakturanummer: 880003",
        "Kundnummer: 9900",
        "Att betala: 447,00 kr",
        "Oss tillhanda senast: 2024-03-29",
        "Momspliktigt belopp 357,55 kr",
        "Moms 89,39 kr",
        "Öresavrundning 0,06 kr",
    ]
    specifications = []
    for identifier, qty, spot, fee, discount, net, vat, gross in [
        ("000000000000000001", "140,250", "70,13", "8,42", "0,00", "110,55", "27,64", "138,19"),
        ("000000000000000002", "510", "200,00", "20,00", "-5,00", "247,00", "61,75", "308,75"),
    ]:
        specifications.append(
            [
                "Avtalsuppgifter.",
                "Beräknad årsförbrukning: 99999 kWh",
                "Anläggningsid: Produkt: Uppsägningstid:",
                f"{identifier} Månadspris 0 mån",
                "Fakturaspecifikation.",
                "Exempelvägen 99, Elområde SE3 Antal Pris Belopp",
                f"Medelspotpris 2024-02-01 - 2024-02-29 {qty} kWh 50,00 öre/kWh {spot} kr",
                f"Rörliga kostnader 2024-02-01 - 2024-02-29 {qty} kWh 6,00 öre/kWh {fee} kr",
                f"Fasta påslag 2024-02-01 - 2024-02-29 {qty} kWh 0,00 öre/kWh {discount} kr",
                "Fast avgift 2024-02-01 - 2024-02-29 29 dagar 32,00 kr/månad 32,00 kr",
                f"Summa exkl moms {net} kr",
                f"Moms (25%) {vat} kr",
                f"Summa inkl moms {gross} kr",
                "Mätare:verk Ingående mätarställning Slutlig mätarställning",
                "909099:1 12345 12855",
                "Jämförpris februari 2 000 kWh: 123,45 öre/kWh",
                "Statistik för föregående månad: 50000 kWh",
            ]
        )
    if version == "v2":
        for page in specifications:
            label = page.index("Anläggningsid: Produkt: Uppsägningstid:")
            identifier = page[label + 1].split()[0]
            page[label : label + 2] = [f"Anläggnings-ID: {identifier}"]
            page[page.index("Fakturaspecifikation.")] = "Fakturaspecifikation elhandel"
    return [cover, *specifications]
