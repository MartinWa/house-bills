"""Configuration, private proposals and exact approved invoice allocations."""

from builtins import property as computed_property

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator
from django.db import models
from django.db.models import F, Q
from django.db.models.functions import Length, Substr
from django.db.models.lookups import Exact

from .categories import (
    CATEGORY_LABELS,
    WASTE_COST_SUBCATEGORIES,
    WASTE_SUBCATEGORIES,
    subcategory_label,
)
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
        PRODUCT = "product", "Product number"
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
                condition=Q(kind__in=["installation", "contract", "product", "delivery_address"])
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
    class Source(models.TextChoices):
        UPLOAD = "upload", "Uploaded PDF"
        MANUAL = "manual", "Manual entry"

    class StorageState(models.TextChoices):
        NONE = "none", "No original"
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
        READY = "ready", "Done"

    source = models.CharField(max_length=20, choices=Source, default=Source.UPLOAD)
    sha256 = models.CharField(max_length=64, blank=True, validators=[sha256_validator])
    original_filename = models.CharField(max_length=255, blank=True)
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
    legacy_charge_details = models.JSONField(default=list, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(source__in=["upload", "manual"]), name="document_source_valid"
            ),
            models.CheckConstraint(
                condition=Q(
                    storage_state__in=[
                        "none",
                        "pending",
                        "inbox",
                        "publishing",
                        "approved",
                        "failed",
                    ]
                ),
                name="document_storage_state_valid",
            ),
            models.CheckConstraint(
                condition=~Q(storage_state__in=["inbox", "approved"]) | ~Q(original_path=""),
                name="document_stored_path_present",
            ),
            models.CheckConstraint(
                condition=(
                    Q(source="manual", sha256="")
                    | Q(source="upload")
                    & Exact(Length("sha256"), models.Value(64))
                    & ~models.Func(
                        models.Value("*[^0-9a-f]*"),
                        F("sha256"),
                        function="glob",
                        output_field=models.BooleanField(),
                    )
                ),
                name="document_sha256_valid",
            ),
            models.CheckConstraint(
                condition=~Q(source="manual")
                | Q(
                    original_filename="",
                    original_path="",
                    staging_path="",
                    publication_path="",
                    storage_state="none",
                ),
                name="document_manual_has_no_original",
            ),
            models.CheckConstraint(
                condition=Q(source="manual") | ~Q(original_filename=""),
                name="document_filename_present",
            ),
            models.CheckConstraint(
                condition=Q(state__in=["pending", "processing", "review", "failed", "ready"]),
                name="document_state_valid",
            ),
            models.UniqueConstraint(
                fields=["original_path"],
                condition=~Q(original_path=""),
                name="document_path_unique",
            ),
            models.UniqueConstraint(
                fields=["sha256"],
                condition=~Q(sha256=""),
                name="document_sha256_unique",
            ),
        ]

    def clean(self):
        if self.pk:
            previous = type(self).objects.filter(pk=self.pk).values("sha256", "source").first()
            if previous is not None:
                if previous["sha256"] != self.sha256:
                    raise ValidationError({"sha256": "An original's hash cannot be changed."})
                if previous["source"] != self.source:
                    raise ValidationError({"source": "A document's source cannot be changed."})

    def save(self, *args, **kwargs):
        if self.source == self.Source.MANUAL:
            self.storage_state = self.StorageState.NONE
        return super().save(*args, **kwargs)

    @computed_property
    def display_name(self):
        return self.original_filename or f"Manual entry #{self.pk}"

    @computed_property
    def can_discard_manual_entry(self):
        return (
            self.source == self.Source.MANUAL
            and self.state == self.State.REVIEW
            and self.extraction_revision == 0
            and not self.invoices.exists()
            and not self.extractions.exists()
        )

    @computed_property
    def storage_needs_attention(self):
        return bool(
            self.storage_error
            or self.staging_path
            or self.publication_path
            or self.storage_state
            in (self.StorageState.PENDING, self.StorageState.PUBLISHING, self.StorageState.FAILED)
            # Approval can commit before its publication callback starts.
            or (self.state == self.State.READY and self.storage_state == self.StorageState.INBOX)
        )

    def __str__(self):
        return self.display_name


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


class PropertyService(ValidatedModel):
    class Frequency(models.IntegerChoices):
        AS_NEEDED = 0, "As needed"
        MONTHLY = 1, "Monthly"
        EVERY_TWO_MONTHS = 2, "Every two months"
        QUARTERLY = 3, "Quarterly"
        HALF_YEARLY = 6, "Every six months"
        YEARLY = 12, "Yearly"

    property = models.ForeignKey(Property, on_delete=models.PROTECT, related_name="services")
    service = models.CharField(max_length=24, choices=Service)
    subcategory = models.CharField(
        max_length=20, choices=WASTE_COST_SUBCATEGORIES, blank=True, default=""
    )
    is_active = models.BooleanField("enabled", default=True)
    start_month = models.DateField("first invoice month")
    end_month = models.DateField("last invoice month", null=True, blank=True)
    frequency_months = models.PositiveSmallIntegerField(
        "frequency", choices=Frequency, default=Frequency.MONTHLY
    )
    grace_days = models.PositiveSmallIntegerField(
        default=0,
        validators=[MaxValueValidator(60)],
        help_text="Days after the invoice month ends before marking an invoice missing.",
    )

    class Meta:
        ordering = ["property__name", "service", "subcategory", "start_month", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Q(service__in=Service.values), name="property_service_type_valid"
            ),
            models.CheckConstraint(
                condition=Q(subcategory="")
                | Q(service="waste", subcategory__in=["waste_collection", "tank_emptying"]),
                name="property_service_subcategory_valid",
            ),
            models.CheckConstraint(
                condition=Q(frequency_months__in=[0, 1, 2, 3, 6, 12]) & Q(grace_days__lte=60),
                name="property_service_frequency_valid",
            ),
            models.CheckConstraint(
                condition=Exact(Substr("start_month", 9, 2), models.Value("01"))
                & (
                    Q(end_month__isnull=True)
                    | Exact(Substr("end_month", 9, 2), models.Value("01"))
                    & Q(end_month__gte=F("start_month"))
                ),
                name="property_service_months_valid",
            ),
            models.UniqueConstraint(
                fields=["property", "service", "subcategory", "start_month"],
                name="property_service_start_unique",
            ),
        ]

    def clean(self):
        if self.subcategory and self.service != Service.WASTE:
            raise ValidationError({"subcategory": "Choose a cost subcategory only for waste."})
        errors = {}
        for field in ("start_month", "end_month"):
            value = getattr(self, field)
            if value and value.day != 1:
                errors[field] = "Use the first day of the invoice month."
        if self.start_month and self.end_month and self.end_month < self.start_month:
            errors["end_month"] = "The last invoice month must not precede the first."
        if errors:
            raise ValidationError(errors)

    @computed_property
    def category_label(self):
        return CATEGORY_LABELS[self.service]

    @computed_property
    def subcategory_label(self):
        return (
            subcategory_label(self.service, self.subcategory)
            if self.service != "waste" or self.subcategory
            else ""
        )

    def __str__(self):
        label = self.subcategory_label or self.get_service_display()
        return f"{self.property}: {label}"


class ServiceVendorAssignment(ValidatedModel):
    property_service = models.ForeignKey(
        PropertyService, on_delete=models.CASCADE, related_name="vendor_assignments"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="service_assignments")
    is_active = models.BooleanField("enabled", default=True)
    start_month = models.DateField("first invoice month")
    end_month = models.DateField("last invoice month", null=True, blank=True)

    class Meta:
        ordering = ["property_service__property__name", "start_month", "pk"]
        constraints = [
            models.CheckConstraint(
                condition=Exact(Substr("start_month", 9, 2), models.Value("01"))
                & (
                    Q(end_month__isnull=True)
                    | Exact(Substr("end_month", 9, 2), models.Value("01"))
                    & Q(end_month__gte=F("start_month"))
                ),
                name="service_vendor_months_valid",
            ),
            models.UniqueConstraint(
                fields=["property_service", "vendor", "start_month"],
                name="service_vendor_start_unique",
            ),
        ]

    def clean(self):
        errors = {}
        for field in ("start_month", "end_month"):
            value = getattr(self, field)
            if value and value.day != 1:
                errors[field] = "Use the first day of the invoice month."
        if self.start_month and self.end_month and self.end_month < self.start_month:
            errors["end_month"] = "The last invoice month must not precede the first."
        if self.property_service_id:
            service = self.property_service
            if self.start_month and self.start_month < service.start_month:
                errors["start_month"] = "The vendor must start within the service's invoice months."
            if service.end_month and (self.end_month is None or self.end_month > service.end_month):
                errors["end_month"] = (
                    "End the vendor assignment within the service's invoice months."
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.property_service} → {self.vendor}"


class ServiceInvoiceException(ValidatedModel):
    property_service = models.ForeignKey(
        PropertyService, on_delete=models.CASCADE, related_name="invoice_exceptions"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="invoice_exceptions")
    month = models.DateField("invoice month")
    reason = models.CharField(
        max_length=300,
        help_text="Explain why this vendor issued no invoice in this month.",
    )

    class Meta:
        ordering = ["month", "vendor__name", "pk"]
        verbose_name = "month with no invoice"
        verbose_name_plural = "months with no invoice"
        constraints = [
            models.UniqueConstraint(
                fields=["property_service", "vendor", "month"],
                name="service_invoice_exception_unique",
            ),
            models.CheckConstraint(
                condition=Exact(Substr("month", 9, 2), models.Value("01")) & ~Q(reason=""),
                name="service_invoice_exception_valid",
            ),
        ]

    def clean(self):
        self.reason = self.reason.strip()
        errors = {}
        if not self.reason:
            errors["reason"] = "Explain why no invoice was issued."
        if self.month and self.month.day != 1:
            errors["month"] = "Use the first day of the invoice month."
        try:
            service = self.property_service
        except PropertyService.DoesNotExist:
            service = None
        if service is not None and self.month:
            if not service.frequency_months:
                errors["month"] = "As-needed services do not create invoice expectations."
            elif service.start_month:
                offset = (
                    (self.month.year - service.start_month.year) * 12
                    + self.month.month
                    - service.start_month.month
                )
                if (
                    offset < 0
                    or (service.end_month and self.month > service.end_month)
                    or offset % service.frequency_months
                ):
                    errors["month"] = "Choose a recurring invoice month within this service."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.property_service}: {self.vendor}, {self.month:%Y-%m}"


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
    subcategory = models.CharField(
        max_length=20, choices=WASTE_COST_SUBCATEGORIES, blank=True, default=""
    )
    identifiers = models.JSONField(
        default=list, blank=True, validators=[validate_source_identifiers]
    )
    net_minor = ScaledIntegerField(null=True, blank=True)
    vat_minor = ScaledIntegerField(null=True, blank=True)
    gross_minor = ScaledIntegerField(null=True, blank=True)
    start = models.DateField(null=True, blank=True)
    end = models.DateField(null=True, blank=True)
    period_evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])
    tax_evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])
    total_evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])

    class Meta:
        indexes = [models.Index(fields=["property", "service"], name="section_property_service")]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice", "section_id"], name="invoice_section_unique"
            ),
            models.CheckConstraint(
                condition=Q(service__in=Service.values) & ~Q(section_id=""),
                name="section_service_valid",
            ),
            models.CheckConstraint(
                condition=Q(subcategory="")
                | Q(service="waste", subcategory__in=["waste_collection", "tank_emptying"]),
                name="section_subcategory_valid",
            ),
            total_constraint("section_total_reconciles"),
            models.CheckConstraint(
                condition=Q(start__isnull=True, end__isnull=True)
                | Q(start__isnull=False, end__isnull=False, start__lt=F("end")),
                name="section_billing_period_valid",
            ),
            *[
                integer_constraint(field, f"section_{field}_integer")
                for field in ["net_minor", "vat_minor", "gross_minor"]
            ],
        ]

    def _invoice(self):
        return Invoice.objects.get(pk=self.invoice_id)

    def clean(self):
        if self.subcategory and self.service != Service.WASTE:
            raise ValidationError({"subcategory": "Choose a cost subcategory only for waste."})

    @computed_property
    def category_label(self):
        return CATEGORY_LABELS[self.service]

    @computed_property
    def subcategory_label(self):
        return (
            subcategory_label(self.service, self.subcategory)
            if self.service != Service.WASTE or self.subcategory
            else ""
        )

    def __str__(self):
        return f"{self.invoice}: {self.section_id}"


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
    subcategory = models.CharField(
        max_length=20, choices=WASTE_SUBCATEGORIES, blank=True, default=""
    )
    unit = models.CharField(max_length=8, choices=[("kWh", "kWh"), ("m3", "m³"), ("kg", "kg")])
    quantity_milli = ScaledIntegerField()
    start = models.DateField()
    end = models.DateField()
    basis = models.CharField(max_length=20, choices=Basis)
    evidence = models.JSONField(default=dict, blank=True, validators=[validate_evidence])

    class Meta:
        ordering = ["position", "pk"]
        indexes = [models.Index(fields=["utility", "start", "end"], name="usage_utility_period")]
        constraints = [
            models.UniqueConstraint(fields=["section", "position"], name="usage_position_unique"),
            models.UniqueConstraint(
                fields=["section", "utility", "start", "end", "basis"],
                condition=Q(utility="electricity"),
                name="electricity_usage_once_per_basis",
            ),
            models.CheckConstraint(condition=Q(start__lt=F("end")), name="usage_period_valid"),
            models.CheckConstraint(
                condition=Q(subcategory="")
                | Q(utility="waste", subcategory__in=["restavfall", "matavfall"]),
                name="usage_subcategory_matches_utility",
            ),
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
        if self.subcategory and self.utility != self.Utility.WASTE:
            raise ValidationError({"subcategory": "Only waste consumption has a waste subtype."})
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

    @computed_property
    def subcategory_label(self):
        return subcategory_label(self.section.service, self.subcategory)


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
