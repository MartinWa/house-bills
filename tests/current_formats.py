"""Wholly invented current-layout examples; no source invoice fields or identifiers."""


def quarterly_waste():
    return [
        [
            "FAKTURA",
            "Fakturanr 910003 (1 av 2)",
            "OCR-nr 91000399",
            "Fakturadatum 2024-10-05",
            "Förfallodatum 2024-11-05",
            "Renhållning Kundnummer 400003",
            "VAT-nr SE000000333301",
            "Anläggning 400300 Imaginary delivery address",
            "Grundavgift permanentbostad 2024-10-01 - 2024-12-31 1,00 st à 800,00 kr / år 70,00",
            "Enfamiljshus, 240 liter restavfall",
            "Tömn Restavfall 2024-10-01 - 2024-12-31 0,50 st à 900,00 kr / år 34,05",
            "Tömn Matavfall 2024-10-01 - 2024-12-31 0,50 st à 0,00 kr / år 0,00",
        ],
        [
            "Fakturanr 910003 (2 av 2)",
            "Fakturadatum 2024-10-05",
            "Förfallodatum 2024-11-05",
            "Moms 25% beräknat på 104,05 26,01",
            "Öresavrundning -0,06",
            "Summa oss tillhanda senast 2024-11-05",
            "Betalning oss tillhanda",
            "Betalningsavsändare senast 2024-11-05 130,00",
        ],
    ]


def cover_supply():
    return [
        [
            "FAKTURA",
            "Fakturadatum 2024-03-05",
            "Kundnr 400004",
            "Fakturanr/OCR-nr 910004",
            "VAT-nr SE000000444401",
            "Fakturaperiod: 2024-02-01 - 2024-02-29",
            "Anläggningsinformation Avtalsinformation",
            "AnläggningsID: 000000000000000041",
            "Total",
            "Elhandel 100,00 SEK",
            "Oss tillhanda Netto Moms total Öresutjämning Att betala",
            "2024-03-25 80,00 20,00 0,00 100,00",
        ],
        [
            "Specifikation:",
            "Elhandel 2024-02-01 - 2024-02-29",
            "Medelspotpris 80,125 kWh 0,60000 SEK/kWh 48,07 SEK",
            "*Rörliga kostnader 80,125 kWh 0,20000 SEK/kWh 16,03 SEK",
            "Fasta påslag 80,125 kWh 0,10000 SEK/kWh 8,01 SEK",
            "Månadsavgift 1 Mån 7,89 SEK",
            "Moms 25,00 % 20,00 SEK",
            "Totalt 100,00 SEK",
            "Summa anläggning 100,00SEK",
            "Mätarnummer: 400041",
            "Energiförbrukning 2024-02-29 0,00 80,125 kWh 98 765 kWh",
        ],
        ["Kundinformation", "Historical chart: 99 999 kWh", "AnläggningsID: 000000000000000041"],
    ]


def chart_supply():
    return [
        [
            "Faktura el",
            "100,00 kr",
            "Belopp att betala:",
            "Oss tillhanda senast: 2024-03-25",
            "OCR-nummer: 91000599",
            "Kundnummer: 400005",
            "Fakturanummer: 910005",
            "Fakturadatum: 2024-03-05",
            "VAT-nr SE000000555501",
        ],
        [
            "Fakturaspecifikation el Fakturanummer: 910005",
            "Fakturadatum: 2024-03-05",
            "Din anläggning Ditt avtal Din energikälla",
            "Anläggnings-ID: 000000 000 000 000 051",
            "Beräknad årsförbrukning: 98 765 kWh",
            "Din förbrukning kWh 2023 2024",
            "Period Förbrukning",
            "1750 2024-02-01 - 2024-02-29 321,125 kWh",
            "Historical diagram 99 999 kWh",
            "Det här betalar du för:",
            "Specificering Period Kvantitet Pris Summa",
            "Medelspotpris 2024-02-01 - 2024-02-29 321,125kWh 10,00öre/kWh 32,11 kr",
            "Rörliga kostnader 2024-02-01 - 2024-02-29 321,125kWh 2,00öre/kWh 6,42 kr",
            "Fast påslag spot 2024-02-01 - 2024-02-29 321,125kWh 3,00öre/kWh 9,63 kr",
            "Årsavgift 2024-02-01 - 2024-02-29 29dygn 400,00kr/år 31,84 kr",
            "Moms 25 % av 80,00 kr 20,00 kr",
            "Summa Synthetic Supply 100,00 kr",
        ],
    ]


def monthly_network():
    return [
        [
            "Faktura elnät",
            "5 mars 2024",
            "Kundnummer: 400006",
            "Belopp att betala: 100,15 kr",
            "inkl. moms (öresutjämning 0,15 kr)",
            "Betala senast: 25 mars 2024",
            "Faktura/OCR-nummer: 910006",
            "Momsgrundande: 80,00 kr Moms 25%: 20,00 kr Ej momsgrundande: 0,00 kr",
            "Momsreg.nr. SE000000666601",
        ],
        [
            "Kostnad 1 feb 2024 t o m 29 feb 2024",
            "Antal Pris inkl. moms Summa inkl. moms",
            "Fast avgift 29 dagar 50,00 kr/mån 50,00 kr",
            "Överföringsavgift 100,125 kWh 20,00 öre/kWh 20,00 kr",
            "Summa 70,00 kr",
            "Elanvändning, månad för månad (kWh) 2022 2023 2024",
            "Beräknad årsanvändning: 98 765 kWh",
            "Statlig energiskatt 1 feb 2024 t o m 29 feb 2024",
            "Antal Pris inkl. moms Summa inkl. moms",
            "Energiskatt 100,125 kWh 30,00 öre/kWh 30,00 kr",
            "Summa som Synthetic Grid betalar vidare till Skatteverket 30,00 kr",
            "Anläggningsinformation",
            "Anläggnings-id: 000 000 000 000 000 061",
            "Mätmetod: Kvartsmätt",
        ],
    ]
