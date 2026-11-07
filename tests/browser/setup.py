"""Prepare a fresh, explicitly disposable browser-check installation and invented PDFs."""

import os
from pathlib import Path

import django

from tests.synthetic import multi_service, shared_supply, write_pdf


def main():
    if os.environ.get("HOUSE_BILLS_BROWSER_CHECK") != "true":
        raise RuntimeError("Use HOUSE_BILLS_BROWSER_CHECK=true on isolated browser-check storage.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "house_bills.settings.development")
    django.setup()
    from django.contrib.auth import get_user_model

    from bills.models import Document, Property, Vendor

    if any(model.objects.exists() for model in (get_user_model(), Document, Property, Vendor)):
        raise RuntimeError(
            "Browser checks require a fresh installation; existing records are retained."
        )
    get_user_model().objects.create_superuser(
        "synthetic-browser-reviewer", password="synthetic-browser-password"
    )
    fixtures = Path(".playwright-cli/fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    pages = shared_supply()
    pages[0].append("VAT-nr SE000000999901")
    write_pdf(fixtures / "shared.pdf", pages)
    write_pdf(fixtures / "unsupported.pdf", [["Wholly synthetic unsupported water credit"]])
    warning_pages = [
        [
            line
            for line in page
            if not (isinstance(line, str) and line.startswith("Avläsning") and "kWh" in line)
        ]
        for page in multi_service()
    ]
    warning_pages[0].append("VAT-nr SE000000999901")
    write_pdf(fixtures / "section-warning.pdf", warning_pages)
    reading_pages = [
        [line.replace("880001", "880005") if isinstance(line, str) else line for line in page]
        for page in multi_service(split_network_readings=True)
    ]
    reading_pages[0].append("VAT-nr SE000000999901")
    write_pdf(fixtures / "multiple-readings.pdf", reading_pages)
    print("Fresh browser-check user and synthetic PDFs prepared.")


if __name__ == "__main__":
    main()
