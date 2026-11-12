"""Serve invented approved data for the disposable Grafana transport probe."""

import os
from datetime import date


def seed():
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.utils import timezone

    from bills.models import ApiToken, BillingSection, Document, Invoice, Property, Usage, Vendor

    with transaction.atomic():
        if Document.objects.exists():
            return
        owner = get_user_model().objects.create_superuser(
            "synthetic-api", password="synthetic-only"
        )
        for profile in ("reporting", "automation"):
            ApiToken.objects.create(
                name=f"Synthetic {profile}",
                owner=owner,
                profile=profile,
                digest=os.environ[f"HOUSE_BILLS_CHECK_{profile.upper()}_TOKEN_DIGEST"],
            )
        houses = [
            Property.objects.create(name=f"Example house {i}", slug=f"example-house-{i}")
            for i in (1, 2)
        ]
        vendor = Vendor.objects.create(name="Example water", slug="example-water")
        docs = Document.objects.bulk_create(
            [Document(source="manual", state="ready", storage_state="none") for _ in range(1101)]
        )
        invoices = Invoice.objects.bulk_create(
            [
                Invoice(
                    document=doc,
                    vendor=vendor,
                    reference=f"SYNTHETIC-{i}",
                    invoice_date=date(2026, 1, 31) if i % 2 == 0 else date(2026, 2, 1),
                    net_minor=100 * (i % 2 + 1),
                    vat_minor=25 * (i % 2 + 1),
                    rounding_minor=0,
                    invoice_total_minor=125 * (i % 2 + 1),
                    amount_due_minor=125 * (i % 2 + 1),
                    status="approved",
                    approved_at=timezone.now(),
                    approved_by=owner,
                    review_revision=1,
                )
                for i, doc in enumerate(docs)
            ]
        )
        sections = BillingSection.objects.bulk_create(
            [
                BillingSection(
                    invoice=invoice,
                    section_id="water",
                    property=houses[i % 2],
                    category="water",
                    net_minor=invoice.net_minor,
                    vat_minor=invoice.vat_minor,
                    gross_minor=invoice.invoice_total_minor,
                    start=date(2026, 1, 1),
                    end=date(2026, 2, 1),
                )
                for i, invoice in enumerate(invoices)
            ]
        )
        Usage.objects.bulk_create(
            [
                Usage(
                    section=section,
                    position=0,
                    utility="water",
                    unit="m3",
                    quantity_milli=123 * (i % 2 + 1),
                    start=date(2026, 1, 1),
                    end=date(2026, 2, 1),
                    basis="billed",
                )
                for i, section in enumerate(sections)
            ]
        )


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "house_bills.settings.development")
    import django

    django.setup()
    from django.conf import settings

    settings.ALLOWED_HOSTS = ["app", "localhost", "127.0.0.1"]
    from django.core.management import call_command
    from django.core.wsgi import get_wsgi_application
    from waitress import serve

    call_command("migrate", interactive=False, verbosity=0)
    seed()
    print("Synthetic API/Grafana check server ready.", flush=True)
    serve(get_wsgi_application(), host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
