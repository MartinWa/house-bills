"""Tiny wholly invented PDF for the production probe, without test dependencies."""


def invoice_pdf():
    lines = [
        "Samfaktura.",
        "Fakturadatum: 2024-03-05",
        "OCR-/fakturanummer: 880099",
        "Kundnummer: 9900",
        "Att betala: 125,00 kr",
        "Oss tillhanda senast: 2024-03-29",
        "Momspliktigt belopp 100,00 kr",
        "Moms 25,00 kr",
        "Öresavrundning 0,00 kr",
        "VAT-nr SE000000999901",
        "Anläggningsid: Produkt:",
        "000000000000000001 Månadspris",
        "Fakturaspecifikation.",
        "Medelspotpris 2024-02-01 - 2024-02-29 100 kWh 50,00 öre/kWh 50,00 kr",
        "Rörliga kostnader 2024-02-01 - 2024-02-29 100 kWh 20,00 öre/kWh 20,00 kr",
        "Fasta påslag 2024-02-01 - 2024-02-29 100 kWh 10,00 öre/kWh 10,00 kr",
        "Fast avgift 2024-02-01 - 2024-02-29 29 dagar 20,00 kr/månad 20,00 kr",
        "Summa exkl moms 100,00 kr",
        "Moms (25%) 25,00 kr",
        "Summa inkl moms 125,00 kr",
    ]
    commands = [b"BT /F1 8 Tf 50 805 Td 14 TL"]
    for line in lines:
        # Octal escapes keep the PDF literal syntax ASCII and preserve WinAnsi text.
        literal = b"".join(f"\\{byte:03o}".encode() for byte in line.encode("cp1252"))
        commands.append(b"(" + literal + b") Tj T*")
    commands.append(b"ET")
    stream = b"\n".join(commands)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    result, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)
