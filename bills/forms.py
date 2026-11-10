"""Normalize configuration keys before admin formsets check for duplicates."""

from django import forms
from django.core.exceptions import ValidationError

from .categories import subcategory_label
from .models import (
    Document,
    Property,
    PropertyIdentifier,
    PropertyService,
    Service,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Vendor,
    VendorIssuerIdentifier,
)
from .validators import normalize_identifier


class UploadForm(forms.Form):
    pdf = forms.FileField(
        label="PDF invoice",
        widget=forms.ClearableFileInput(
            attrs={"accept": ".pdf", "aria-describedby": "upload-hint"}
        ),
    )


class DateRangeFilterForm(forms.Form):
    date_from = forms.DateField(
        label="Start date",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )
    date_to = forms.DateField(
        label="End date",
        required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("date_from"), cleaned.get("date_to")
        if start and end and end < start:
            self.add_error("date_to", "End date must be on or after start date.")
        return cleaned


class InvoiceFilterForm(DateRangeFilterForm):
    field_order = ["property", "service", "date_from", "date_to"]

    property = forms.ModelChoiceField(
        label="Property",
        queryset=Property.objects.all(),
        required=False,
        empty_label="All properties",
    )
    service = forms.ChoiceField(
        label="Service", choices=[("", "All services"), *Service.choices], required=False
    )


class ReportFilterForm(DateRangeFilterForm):
    field_order = ["property", "date_from", "date_to"]

    property = forms.ModelChoiceField(
        label="Property",
        queryset=Property.objects.all(),
        required=False,
        empty_label="All properties",
    )


class CoverageFilterForm(InvoiceFilterForm):
    field_order = ["property", "vendor", "service", "status", "date_from", "date_to"]
    vendor = forms.ModelChoiceField(
        queryset=Vendor.objects.all(),
        required=False,
        empty_label="All vendors",
    )
    status = forms.ChoiceField(
        choices=[
            ("attention", "Missing or needs review"),
            ("", "All"),
            ("missing", "Missing"),
            ("needs_review", "Needs review"),
            ("received", "Received"),
            ("expected", "Expected"),
            ("not_expected", "Not sent"),
            ("unassigned", "Needs vendor"),
        ],
        required=False,
    )


class InvoiceMonthField(forms.DateField):
    def __init__(self, **kwargs):
        super().__init__(
            input_formats=["%Y-%m", "%Y-%m-%d"],
            widget=forms.DateInput(format="%Y-%m", attrs={"type": "month"}),
            **kwargs,
        )

    def to_python(self, value):
        result = super().to_python(value)
        return result.replace(day=1) if result else result


class PropertyServiceForm(forms.ModelForm):
    start_month = InvoiceMonthField(label="First invoice month")
    end_month = InvoiceMonthField(label="Last invoice month", required=False)
    service = forms.ChoiceField(
        label="Category / service",
        choices=[
            (
                service.value,
                f"Electricity / {subcategory_label(service)}"
                if service in (Service.ELECTRICITY_NETWORK, Service.ELECTRICITY_SUPPLY)
                else service.label,
            )
            for service in Service
        ],
    )

    class Meta:
        model = PropertyService
        fields = [
            "property",
            "service",
            "subcategory",
            "is_active",
            "start_month",
            "end_month",
            "frequency_months",
            "grace_days",
        ]
        help_texts = {
            "start_month": "First expected invoice month; recurring bills repeat from this month.",
            "end_month": "Inclusive. Leave blank while the property needs this service.",
            "subcategory": "Waste collection or tank emptying. Blank matches any waste cost.",
            "frequency_months": (
                "As needed records vendor activity without expecting regular invoices."
            ),
        }

    def clean(self):
        values = super().clean()
        if not all(values.get(field) for field in ("property", "service", "start_month")):
            return values
        if not values.get("is_active"):
            return values
        existing = PropertyService.objects.filter(
            property=values["property"],
            service=values["service"],
            subcategory=values.get("subcategory", ""),
            is_active=True,
        ).exclude(pk=self.instance.pk)
        for service in existing:
            if (values.get("end_month") is None or service.start_month <= values["end_month"]) and (
                service.end_month is None or values["start_month"] <= service.end_month
            ):
                raise ValidationError(
                    "This property already has an enabled service for these invoice months. "
                    "Change its vendor assignments, or end it before starting a replacement."
                )
        return values


class InvoiceNotSentForm(forms.Form):
    reason = forms.CharField(
        max_length=300,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="For example, the vendor confirmed that no invoice was issued.",
    )


class ServiceInvoiceExceptionForm(forms.ModelForm):
    month = InvoiceMonthField(label="Invoice month")

    class Meta:
        model = ServiceInvoiceException
        fields = ["vendor", "month", "reason"]


class ServiceVendorAssignmentForm(forms.ModelForm):
    start_month = InvoiceMonthField(label="First invoice month")
    end_month = InvoiceMonthField(label="Last invoice month", required=False)

    class Meta:
        model = ServiceVendorAssignment
        fields = ["vendor", "is_active", "start_month", "end_month"]
        help_texts = {
            "start_month": "First month in which this vendor should send an invoice.",
            "end_month": (
                "Inclusive. End the former vendor and add its replacement to retain history. "
                "Both vendors may bill in the transition month."
            ),
        }


class ServiceVendorAssignmentFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        assignments = {}
        for form in self.forms:
            values = form.cleaned_data
            if not values or values.get("DELETE") or not values.get("is_active"):
                continue
            key = values["vendor"].pk
            start, end = values["start_month"], values.get("end_month")
            service = self.instance
            if service.start_month and start < service.start_month:
                raise ValidationError("Vendor assignments must start within the service's months.")
            if service.end_month and (end is None or end > service.end_month):
                raise ValidationError("Vendor assignments must end within the service's months.")
            for other_start, other_end in assignments.get(key, []):
                if (end is None or other_start <= end) and (
                    other_end is None or start <= other_end
                ):
                    raise ValidationError(
                        "Enabled assignments for the same vendor must not overlap. "
                        "End the earlier assignment before starting its replacement."
                    )
            assignments.setdefault(key, []).append((start, end))
        if self.instance.is_active and not assignments:
            raise ValidationError(
                "Attach at least one enabled vendor to an enabled property service."
            )


class DocumentFilterForm(forms.Form):
    ATTENTION = "attention"

    processing = forms.ChoiceField(
        label="Status",
        choices=[(ATTENTION, "Needs attention"), ("", "All"), *Document.State.choices],
        required=False,
    )


class ReparseForm(forms.Form):
    extraction_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


class DeleteDocumentForm(forms.Form):
    token = forms.CharField(widget=forms.HiddenInput)


class IdentifierForm(forms.ModelForm):
    def clean_value(self):
        return normalize_identifier(self.cleaned_data.get("kind", ""), self.cleaned_data["value"])


class PropertyIdentifierForm(IdentifierForm):
    class Meta:
        model = PropertyIdentifier
        fields = ["vendor", "property", "kind", "value"]


class VendorIssuerIdentifierForm(IdentifierForm):
    class Meta:
        model = VendorIssuerIdentifier
        fields = ["vendor", "kind", "value"]
