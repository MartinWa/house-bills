"""Normalize configuration keys before admin formsets check for duplicates."""

from django import forms

from .models import Property, PropertyIdentifier, Service, VendorIssuerIdentifier
from .validators import normalize_identifier


class UploadForm(forms.Form):
    pdf = forms.FileField(
        label="PDF invoice", widget=forms.ClearableFileInput(attrs={"accept": ".pdf"})
    )


class InvoiceFilterForm(forms.Form):
    property = forms.ModelChoiceField(
        label="Property",
        queryset=Property.objects.all(),
        required=False,
        empty_label="All properties",
    )
    service = forms.ChoiceField(
        label="Service", choices=[("", "All services"), *Service.choices], required=False
    )


class ReparseForm(forms.Form):
    extraction_revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)


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
