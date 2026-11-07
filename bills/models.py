"""Configuration, private proposals and exact approved invoice allocations."""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Length
from django.db.models.lookups import Exact

from .fields import ScaledIntegerField, integer_constraint
from .parsing.registry import layout_family_choices
from .validators import (
    normalize_identifier,
    sha256_validator,
    storage_slug_validator,
    validate_evidence,
    validate_header_evidence,
    validate_pages,
    validate_source_identifiers,
    validate_storage_path,
)


class ValidatedModel(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Property(ValidatedModel):
    name = models.CharField(max_length=120)
    slug = models.CharField(max_length=80, unique=True, validators=[storage_slug_validator])

    class Meta:
        ordering = ["name", "pk"]
        verbose_name_plural = "properties"
        constraints = [
            models.CheckConstraint(
                condition=~Q(slug__in=["inbox", "shared"]), name="property_slug_reserved"
            ),
            models.CheckConstraint(condition=~Q(name="") & ~Q(slug=""), name="property_named"),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        self.name = self.name.strip()


class Vendor(ValidatedModel):
    name = models.CharField(max_length=120)
    slug = models.CharField(max_length=80, unique=True, validators=[storage_slug_validator])
    default_property = models.ForeignKey(
        Property,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="default_for_vendors",
        help_text="Used when no property mapping matches an invoice section.",
    )

    class Meta:
        ordering = ["name", "pk"]
        constraints = [
            models.CheckConstraint(condition=~Q(name="") & ~Q(slug=""), name="vendor_named")
        ]

    def __str__(self):
        return self.name

    def clean(self):
        self.name = self.name.strip()


class VendorIssuerIdentifier(ValidatedModel):
    class Kind(models.TextChoices):
        ORGANIZATION = "organization", "Organization number"
        VAT = "vat", "VAT identifier"

    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="issuer_identifiers")
    kind = models.CharField(max_length=20, choices=Kind)
    value = models.CharField(max_length=160)

    class Meta:
        verbose_name = "vendor identification number"
        verbose_name_plural = "vendor identification"
        constraints = [
            models.UniqueConstraint(fields=["kind", "value"], name="issuer_identity_unique"),
            models.CheckConstraint(
                condition=Q(kind__in=["organization", "vat"]) & ~Q(value=""),
                name="issuer_identity_valid",
            ),
        ]

    def clean(self):
        self.value = normalize_identifier(self.kind, self.value)
        if not self.value:
            raise ValidationError({"value": "Enter a vendor identification number."})

    def __str__(self):
        return f"{self.vendor}: {self.get_kind_display()}"


class VendorLayoutFamily(ValidatedModel):
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name="layout_families")
    family = models.CharField("invoice format", max_length=40, choices=layout_family_choices)

    class Meta:
        verbose_name = "invoice format"
        verbose_name_plural = "invoice formats"
        constraints = [
            models.UniqueConstraint(fields=["vendor", "family"], name="vendor_layout_unique")
        ]

    def __str__(self):
        return f"{self.vendor}: {self.family}"


class PropertyIdentifier(ValidatedModel):
    class Kind(models.TextChoices):
        INSTALLATION = "installation", "Installation"
        CONTRACT = "contract", "Contract"
        DELIVERY_ADDRESS = "delivery_address", "Delivery address fallback"

    vendor = models.ForeignKey(
        Vendor, on_delete=models.CASCADE, related_name="property_identifiers"
    )
    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="identifiers")
    kind = models.CharField(max_length=20, choices=Kind)
    value = models.CharField(max_length=200)

    class Meta:
        verbose_name = "property mapping"
        verbose_name_plural = "property mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "kind", "value"], name="property_identifier_unique"
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["installation", "contract", "delivery_address"])
                & ~Q(value=""),
                name="property_identifier_specific",
            ),
        ]

    def clean(self):
        self.value = normalize_identifier(self.kind, self.value)
        if not self.value:
            raise ValidationError({"value": "A property-specific identifier is required."})

    def __str__(self):
        return f"{self.vendor}: {self.get_kind_display()} → {self.property}"


class Document(ValidatedModel):
    class StorageState(models.TextChoices):
        PENDING = "pending", "Awaiting storage"
        INBOX = "inbox", "In inbox"
        PUBLISHING = "publishing", "Publishing original"
        APPROVED = "approved", "Approved storage"
        FAILED = "failed", "Storage failed"

    class State(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSING = "processing", "Processing"
        REVIEW = "review", "Needs review"
        FAILED = "failed", "Failed"
        READY = "ready", "Original stored"

    sha256 = models.CharField(max_length=64, unique=True, validators=[sha256_validator])
    original_filename = models.CharField(max_length=255)
    original_path = models.CharField(max_length=500, blank=True, validators=[validate_storage_path])
    staging_path = models.CharField(max_length=500, blank=True, validators=[validate_storage_path])
    publication_path = models.CharField(
        max_length=500, blank=True, validators=[validate_storage_path]
    )
    storage_state = models.CharField(
        max_length=20, choices=StorageState, default=StorageState.PENDING
    )
    storage_error = models.TextField(blank=True)
    processing_message = models.TextField(blank=True)
    state = models.CharField(max_length=20, choices=State, default=State.PENDING)
    extraction_revision = models.PositiveBigIntegerField(default=0, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(
                    storage_state__in=["pending", "inbox", "publishing", "approved", "failed"]
                ),
                name="document_storage_state_valid",
            ),
            models.CheckConstraint(
                condition=~Q(storage_state__in=["inbox", "approved"]) | ~Q(original_path=""),
                name="document_stored_path_present",
            ),
            models.CheckConstraint(
                condition=Exact(Length("sha256"), models.Value(64))
                & ~models.Func(
                    models.Value("*[^0-9a-f]*"),
                    F("sha256"),
                    function="glob",
                    output_field=models.BooleanField(),
                ),
                name="document_sha256_valid",
            ),
            models.CheckConstraint(
                condition=Q(state__in=["pending", "processing", "review", "failed", "ready"]),
                name="document_state_valid",
            ),
            models.CheckConstraint(
                condition=~Q(original_filename=""), name="document_filename_present"
            ),
            models.UniqueConstraint(
                fields=["original_path"],
                condition=~Q(original_path=""),
                name="document_path_unique",
            ),
        ]

    def clean(self):
        if self.pk:
            digest = type(self).objects.filter(pk=self.pk).values_list("sha256", flat=True).first()
            if digest is not None and digest != self.sha256:
                raise ValidationError({"sha256": "An original's hash cannot be changed."})

    def __str__(self):
        return self.original_filename


class ExtractionAttempt(ValidatedModel):
    class State(models.TextChoices):
        REVIEW = "review", "Needs review"
        FAILED = "failed", "Failed"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="extractions")
    revision = models.PositiveBigIntegerField(editable=False)
    layout_family = models.CharField(max_length=40, blank=True)
    layout_version = models.CharField(max_length=40, blank=True)
    parser_revision = models.CharField(max_length=80, blank=True)
    candidate = models.JSONField(default=dict, blank=True)
    page_evidence = models.JSONField(default=list, blank=True)
    state = models.CharField(max_length=20, choices=State)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["document", "revision"], name="extraction_revision_unique"
            ),
            models.CheckConstraint(
                condition=Q(revision__gte=1), name="extraction_revision_positive"
            ),
            models.CheckConstraint(
                condition=Q(state__in=["review", "failed"]), name="extraction_state_valid"
            ),
            models.CheckConstraint(
                condition=Q(state="failed")
                | (~Q(layout_family="") & ~Q(layout_version="") & ~Q(parser_revision="")),
                name="extraction_parser_identity",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Extraction attempts are immutable; record a new attempt.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"Document {self.document_id}, extraction {self.revision}"

    def delete(self, *args, **kwargs):
        raise ValidationError("Extraction attempts are immutable; retain the source history.")


class Service(models.TextChoices):
    ELECTRICITY_SUPPLY = "electricity_supply", "Electricity supply"
    ELECTRICITY_NETWORK = "electricity_network", "Electricity network"
    WATER = "water", "Water"
    WASTE = "waste", "Waste"
    OTHER = "other", "Other charges"


def total_constraint(name):
    return models.CheckConstraint(
        name=name,
        condition=Q(net_minor__isnull=True)
        | Q(vat_minor__isnull=True)
        | Q(gross_minor__isnull=True)
        | Q(gross_minor=F("net_minor") + F("vat_minor")),
    )


class Invoice(ValidatedModel):
    class Kind(models.TextChoices):
        INVOICE = "invoice", "Invoice"
        CREDIT = "credit", "Credit invoice"

    class Status(models.TextChoices):
        REVIEW = "review", "Needs review"
        APPROVED = "approved", "Approved"

    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="invoices")
    vendor = models.ForeignKey(
        Vendor, on_delete=models.PROTECT, related_name="invoices", null=True, blank=True
    )
    reference = models.CharField(max_length=120, blank=True)
    kind = models.CharField(max_length=20, choices=Kind, default=Kind.INVOICE)
    invoice_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    currency = models.CharField(max_length=3, default="SEK", choices=[("SEK", "SEK")])
    net_minor = ScaledIntegerField(null=True, blank=True)
    vat_minor = ScaledIntegerField(null=True, blank=True)
    rounding_minor = ScaledIntegerField(null=True, blank=True)
    invoice_total_minor = ScaledIntegerField(null=True, blank=True)
    amount_due_minor = ScaledIntegerField(null=True, blank=True)
    source_pages = models.JSONField(default=list, blank=True, validators=[validate_pages])
    header_evidence = models.JSONField(
        default=dict, blank=True, validators=[validate_header_evidence]
    )
    identifiers = models.JSONField(
        default=list, blank=True, validators=[validate_source_identifiers]
    )
    status = models.CharField(max_length=20, choices=Status, default=Status.REVIEW, editable=False)
    review_revision = models.PositiveBigIntegerField(default=0, editable=False)
    approved_at = models.DateTimeField(null=True, blank=True, editable=False)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="approved_invoices",
        editable=False,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["vendor", "reference", "kind"],
                condition=Q(status="approved"),
                name="approved_invoice_identity_unique",
            ),
            models.CheckConstraint(
                condition=Q(kind__in=["invoice", "credit"]), name="invoice_kind_valid"
            ),
            models.CheckConstraint(condition=Q(currency="SEK"), name="invoice_currency_valid"),
            models.CheckConstraint(
                condition=(
                    Q(status="review", approved_at__isnull=True, approved_by__isnull=True)
                    | Q(
                        status="approved",
                        approved_at__isnull=False,
                        approved_by__isnull=False,
                        review_revision__gte=1,
                    )
                ),
                name="invoice_approval_state_valid",
            ),
            models.CheckConstraint(
                condition=~Q(status="approved")
                | (
                    Q(
                        vendor__isnull=False,
                        invoice_date__isnull=False,
                        net_minor__isnull=False,
                        vat_minor__isnull=False,
                        rounding_minor__isnull=False,
                        invoice_total_minor__isnull=False,
                        amount_due_minor__isnull=False,
                    )
                    & ~Q(reference="")
                ),
                name="approved_invoice_complete",
            ),
            models.CheckConstraint(
                condition=Q(net_minor__isnull=True)
                | Q(vat_minor__isnull=True)
                | Q(rounding_minor__isnull=True)
                | Q(invoice_total_minor__isnull=True)
                | Q(invoice_total_minor=F("net_minor") + F("vat_minor") + F("rounding_minor")),
                name="invoice_header_reconciles",
            ),
            *[
                integer_constraint(field, f"invoice_{field}_integer")
                for field in [
                    "net_minor",
                    "vat_minor",
                    "rounding_minor",
                    "invoice_total_minor",
                    "amount_due_minor",
                ]
            ],
        ]

    def clean(self):
        self.reference = self.reference.strip() if self.reference is not None else ""
        if self.status == self.Status.APPROVED and not self.reference:
            raise ValidationError({"reference": "An approved invoice needs a reference."})

    def save(self, *args, **kwargs):
        if self.status == self.Status.APPROVED or (
            self.pk and type(self).objects.filter(pk=self.pk, status=self.Status.APPROVED).exists()
        ):
            raise ValidationError("Approved values can only be changed through the review service.")
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.reference or f"Review {self.pk}"


class AllocationModel(ValidatedModel):
    class Meta:
        abstract = True

    def assert_editable(self):
        invoices = [self._invoice()]
        if not self._state.adding:
            previous = type(self).objects.get(pk=self.pk)
            invoices.append(previous._invoice())
        if any(invoice.status == Invoice.Status.APPROVED for invoice in invoices):
            raise ValidationError(
                "Approved allocations can only be changed through the review service."
            )

    def save(self, *args, **kwargs):
        self.assert_editable()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        self.assert_editable()
        return super().delete(*args, **kwargs)


class BillingSection(AllocationModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="sections")
    section_id = models.CharField(max_length=120)
    property = models.ForeignKey(
        Property, on_delete=models.PROTECT, related_name="billing_sections", null=True, blank=True
    )
    service = models.CharField(max_length=24, choices=Service)
    identifiers = models.JSONField(
        default=list, blank=True, validators=[validate_source_identifiers]
    )
    net_minor = ScaledIntegerField(null=True, blank=True)
    vat_minor = ScaledIntegerField(null=True, blank=True)
    gross_minor = ScaledIntegerField(null=True, blank=True)
    tax_evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])
    total_evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "section_id"], name="invoice_section_unique"
            ),
            models.CheckConstraint(
                condition=Q(service__in=Service.values) & ~Q(section_id=""),
                name="section_service_valid",
            ),
            total_constraint("section_total_reconciles"),
            *[
                integer_constraint(field, f"section_{field}_integer")
                for field in ["net_minor", "vat_minor", "gross_minor"]
            ],
        ]

    def _invoice(self):
        return Invoice.objects.get(pk=self.invoice_id)

    def __str__(self):
        return f"{self.invoice}: {self.section_id}"


class Charge(AllocationModel):
    class VATBasis(models.TextChoices):
        UNALLOCATED = "unallocated", "Unknown"
        ALLOCATED = "allocated_from_section", "Allocated from section"
        PRINTED = "printed", "Printed"
        MANUAL = "manual", "Manual"

    section = models.ForeignKey(BillingSection, on_delete=models.CASCADE, related_name="charges")
    position = models.PositiveIntegerField()
    description = models.CharField(max_length=300)
    start = models.DateField()
    end = models.DateField()
    net_minor = ScaledIntegerField(null=True, blank=True)
    vat_minor = ScaledIntegerField(null=True, blank=True)
    gross_minor = ScaledIntegerField(null=True, blank=True)
    vat_basis = models.CharField(max_length=24, choices=VATBasis, default=VATBasis.UNALLOCATED)
    evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["section", "position"], name="charge_position_unique"),
            models.CheckConstraint(condition=Q(start__lt=F("end")), name="charge_period_valid"),
            models.CheckConstraint(
                condition=Q(
                    vat_basis__in=["unallocated", "allocated_from_section", "printed", "manual"]
                ),
                name="charge_vat_basis_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(vat_basis="unallocated", vat_minor__isnull=True, gross_minor__isnull=True)
                    | (
                        ~Q(vat_basis="unallocated")
                        & Q(
                            net_minor__isnull=False,
                            vat_minor__isnull=False,
                            gross_minor__isnull=False,
                        )
                    )
                ),
                name="charge_vat_allocation_valid",
            ),
            total_constraint("charge_total_reconciles"),
            *[
                integer_constraint(field, f"charge_{field}_integer")
                for field in ["net_minor", "vat_minor", "gross_minor"]
            ],
        ]

    def _invoice(self):
        return Invoice.objects.get(sections__pk=self.section_id)


class Usage(AllocationModel):
    class Utility(models.TextChoices):
        ELECTRICITY = "electricity", "Electricity"
        WATER = "water", "Water"
        WASTE = "waste", "Waste"

    class Basis(models.TextChoices):
        MEASURED = "measured", "Measured"
        ESTIMATED = "estimated", "Estimated"
        REVERSAL = "reversal", "Reversal"
        BILLED = "billed", "Billed"
        BILLED_WEIGHT = "billed_weight", "Billed weight"

    section = models.ForeignKey(BillingSection, on_delete=models.CASCADE, related_name="usages")
    position = models.PositiveIntegerField()
    utility = models.CharField(max_length=20, choices=Utility)
    unit = models.CharField(max_length=8, choices=[("kWh", "kWh"), ("m3", "m³"), ("kg", "kg")])
    quantity_milli = ScaledIntegerField()
    start = models.DateField()
    end = models.DateField()
    basis = models.CharField(max_length=20, choices=Basis)
    evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.UniqueConstraint(fields=["section", "position"], name="usage_position_unique"),
            models.UniqueConstraint(
                fields=["section", "utility", "start", "end", "basis"],
                condition=Q(utility="electricity"),
                name="electricity_usage_once_per_basis",
            ),
            models.CheckConstraint(condition=Q(start__lt=F("end")), name="usage_period_valid"),
            models.CheckConstraint(
                condition=Q(utility="electricity", unit="kWh")
                | Q(utility="water", unit="m3")
                | Q(utility="waste", unit="kg"),
                name="usage_unit_matches_utility",
            ),
            models.CheckConstraint(
                condition=Q(
                    basis__in=["measured", "estimated", "reversal", "billed", "billed_weight"]
                ),
                name="usage_basis_valid",
            ),
            models.CheckConstraint(
                condition=~Q(basis="reversal") | Q(quantity_milli__lte=0),
                name="usage_reversal_signed",
            ),
            integer_constraint("quantity_milli", "usage_quantity_integer"),
        ]

    def clean(self):
        if self.section_id:
            service = self.section.service
            expected = {
                Service.ELECTRICITY_SUPPLY: "electricity",
                Service.ELECTRICITY_NETWORK: "electricity",
                Service.WATER: "water",
                Service.WASTE: "waste",
            }.get(service)
            if self.utility != expected:
                raise ValidationError({"utility": "Usage must match its section's service."})

    def _invoice(self):
        return Invoice.objects.get(sections__pk=self.section_id)


class ReviewRevision(ValidatedModel):
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="reviews")
    revision = models.PositiveBigIntegerField()
    extraction = models.ForeignKey(
        ExtractionAttempt, on_delete=models.PROTECT, null=True, blank=True, related_name="reviews"
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="invoice_reviews"
    )
    note = models.TextField(blank=True)
    snapshot = models.JSONField()
    amount_due_confirmed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["invoice", "revision"], name="review_revision_unique"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="review_revision_positive"),
        ]

    def clean(self):
        if self.extraction_id and self.extraction.document_id != self.invoice.document_id:
            raise ValidationError(
                {"extraction": "The extraction must belong to this invoice's document."}
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Review revisions are immutable; record a new revision.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Review revisions are immutable; retain the approval history.")
