"""Normalize configuration keys before admin formsets check for duplicates."""

from django import forms

from .models import Document, Property, PropertyIdentifier, Service, VendorIssuerIdentifier
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
