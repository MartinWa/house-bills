import hashlib
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction

from bills.models import (
    BillingSection,
    Document,
    Invoice,
    Property,
    PropertyIdentifier,
    Usage,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def vendor():
    return Vendor.objects.create(name="Synthetic Utilities", slug="synthetic-utilities")


@pytest.fixture
def property():
    return Property.objects.create(name="Synthetic House", slug="synthetic-house")


@pytest.fixture
def document():
    return Document.objects.create(
        sha256=hashlib.sha256(b"synthetic-model-document").hexdigest(),
        original_filename="synthetic.pdf",
    )


@pytest.fixture
def invoice(document):
    return Invoice.objects.create(document=document)


@pytest.fixture
def section(invoice, property):
    return BillingSection.objects.create(
        invoice=invoice,
        property=property,
        section_id="section_01",
        service="water",
        net_minor=100,
        vat_minor=25,
        gross_minor=125,
        start=date(2024, 2, 1),
        end=date(2024, 3, 1),
    )


@pytest.fixture
def usage(section):
    return Usage.objects.create(
        section=section,
        position=0,
        utility="water",
        unit="m3",
        quantity_milli=12000,
        start=date(2024, 2, 1),
        end=date(2024, 3, 1),
        basis="measured",
    )


@pytest.mark.parametrize("subcategory", ["matavfall", "restavfall", "unknown"])
def test_usage_subcategory_cannot_be_assigned_to_water(usage, subcategory):
    usage.subcategory = subcategory
    with pytest.raises(ValidationError):
        usage.save()
    with transaction.atomic(), pytest.raises(IntegrityError):
        Usage.objects.filter(pk=usage.pk).update(subcategory=subcategory)


def test_unknown_waste_subcategory_is_rejected_by_model_and_database(section):
    section.service = "waste"
    section.save()
    row = Usage.objects.create(
        section=section,
        position=0,
        utility="waste",
        unit="kg",
        quantity_milli=7500,
        start=date(2024, 2, 1),
        end=date(2024, 3, 1),
        basis="billed_weight",
        subcategory="restavfall",
    )
    row.subcategory = "unknown"
    with pytest.raises(ValidationError):
        row.save()
    with transaction.atomic(), pytest.raises(IntegrityError):
        Usage.objects.filter(pk=row.pk).update(subcategory="unknown")


@pytest.mark.parametrize("value", [1.5, 1.0, True, "100", Decimal("100")])
def test_money_never_silently_coerces_values(invoice, value):
    invoice.net_minor = value
    with pytest.raises(ValidationError, match="integer"):
        invoice.save()


@pytest.mark.parametrize("value", [-(2**63) - 1, 2**63])
def test_money_outside_storage_range_is_rejected(invoice, value):
    invoice.net_minor = value
    with pytest.raises(ValidationError):
        invoice.save()


def test_unknown_money_is_null_and_signed_integer_aggregation_is_exact(invoice):
    invoice.refresh_from_db()
    assert invoice.net_minor is None and invoice.amount_due_minor is None
    invoice.net_minor = -(2**53) - 1
    invoice.save()
    invoice.refresh_from_db()
    assert invoice.net_minor == -(2**53) - 1
    with connection.cursor() as cursor:
        cursor.execute("SELECT SUM(net_minor), typeof(net_minor) FROM bills_invoice")
        assert cursor.fetchone() == (-(2**53) - 1, "integer")


@pytest.mark.parametrize("value", [1.5, "not-a-number", str(2**63)])
def test_database_rejects_non_integer_money_when_validation_is_bypassed(invoice, value):
    with pytest.raises(IntegrityError), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("UPDATE bills_invoice SET net_minor = %s WHERE id = %s", [value, invoice.pk])


def test_database_guards_header_and_section_totals(invoice, section):
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(
            net_minor=100,
            vat_minor=25,
            rounding_minor=0,
            invoice_total_minor=126,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingSection.objects.filter(pk=section.pk).update(gross_minor=126)


def test_cost_allocation_preserves_negative_totals_and_guards_billing_period(section):
    section.net_minor, section.vat_minor, section.gross_minor = -100, -25, -125
    section.save()
    section.refresh_from_db()
    assert (section.net_minor, section.vat_minor, section.gross_minor) == (-100, -25, -125)
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingSection.objects.filter(pk=section.pk).update(gross_minor=-124)
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingSection.objects.filter(pk=section.pk).update(end=None)
    with pytest.raises(IntegrityError), transaction.atomic():
        BillingSection.objects.filter(pk=section.pk).update(end=section.start)


def test_usage_guards_units_precision_periods_and_reversal_signs(usage):
    with pytest.raises(IntegrityError), transaction.atomic():
        Usage.objects.filter(pk=usage.pk).update(unit="kg")
    with pytest.raises(IntegrityError), transaction.atomic():
        Usage.objects.filter(pk=usage.pk).update(end=usage.start)
    with pytest.raises(IntegrityError), transaction.atomic():
        Usage.objects.filter(pk=usage.pk).update(basis="reversal")
    with pytest.raises(ValidationError):
        usage.quantity_milli = 12.123
        usage.save()
    usage.quantity_milli = -12000
    usage.basis = "reversal"
    usage.save()
    usage.refresh_from_db()
    assert usage.quantity_milli == -12000
    usage.utility, usage.unit = "electricity", "kWh"
    with pytest.raises(ValidationError, match="section's service"):
        usage.save()


def test_approval_requires_complete_identity_and_audit_metadata(invoice, django_user_model):
    actor = django_user_model.objects.create_user("synthetic-reviewer")
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(status="approved")
    with pytest.raises(IntegrityError), transaction.atomic():
        Invoice.objects.filter(pk=invoice.pk).update(
            status="approved",
            review_revision=1,
            approved_by=actor,
            approved_at="2024-03-05T12:00:00Z",
        )
    invoice.status = Invoice.Status.APPROVED
    with pytest.raises(ValidationError, match="review service"):
        invoice.save()


def test_hash_and_original_storage_identity_have_database_guards(document):
    with pytest.raises(IntegrityError), transaction.atomic():
        Document.objects.bulk_create(
            [Document(sha256=document.sha256, original_filename="copy.pdf")]
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Document.objects.filter(pk=document.pk).update(sha256="z" * 64)
    document.sha256 = "b" * 64
    with pytest.raises(ValidationError, match="cannot be changed"):
        document.save()
    document.refresh_from_db()
    document.original_path = "inbox/synthetic.pdf"
    document.save()
    with pytest.raises(ValidationError):
        Document.objects.create(
            sha256="c" * 64,
            original_filename="different.pdf",
            original_path=document.original_path,
        )


@pytest.mark.parametrize("slug", ["../house", "House", "shared", "inbox", "a/b", "a--b"])
def test_property_storage_slugs_are_safe(slug):
    with pytest.raises(ValidationError):
        Property.objects.create(name="Synthetic", slug=slug)


@pytest.mark.parametrize(
    "path", ["../outside.pdf", "/absolute.pdf", "C:/outside.pdf", "a\\b.pdf", "a/./b.pdf"]
)
def test_original_paths_stay_relative_to_private_storage(document, path):
    document.original_path = path
    with pytest.raises(ValidationError):
        document.save()


def test_identifiers_are_normalized_and_vendor_scoped(vendor, property):
    first = PropertyIdentifier.objects.create(
        vendor=vendor,
        property=property,
        kind="installation",
        value="000 000-001",
    )
    assert first.value == "000000001"
    with pytest.raises(ValidationError):
        PropertyIdentifier.objects.create(
            vendor=vendor,
            property=property,
            kind="installation",
            value="000000001",
        )
    other = Vendor.objects.create(name="Other Synthetic Vendor", slug="other-vendor")
    PropertyIdentifier.objects.create(
        vendor=other,
        property=property,
        kind="installation",
        value=first.value,
    )
    with pytest.raises(ValidationError):
        PropertyIdentifier.objects.create(
            vendor=vendor,
            property=property,
            kind="customer_account",
            value="9900",
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        PropertyIdentifier.objects.bulk_create(
            [
                PropertyIdentifier(
                    vendor=vendor, property=property, kind="customer_account", value="9900"
                ),
            ]
        )
    issuer = VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value="se 000-000")
    assert issuer.value == "SE000000"
    with pytest.raises(ValidationError):
        VendorIssuerIdentifier.objects.create(vendor=other, kind="vat", value=issuer.value)


def test_multiple_layout_families_are_configuration_for_one_stable_vendor(vendor):
    for family in ["layout_01", "layout_02"]:
        VendorLayoutFamily.objects.create(vendor=vendor, family=family)
    assert vendor.layout_families.count() == 2
    with pytest.raises(ValidationError):
        VendorLayoutFamily.objects.create(vendor=vendor, family="unknown_layout")
    with pytest.raises(IntegrityError), transaction.atomic():
        VendorLayoutFamily.objects.bulk_create(
            [VendorLayoutFamily(vendor=vendor, family="layout_01")]
        )
