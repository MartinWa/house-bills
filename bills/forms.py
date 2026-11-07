"""Normalize configuration keys before admin formsets check for duplicates."""

from django import forms

from .models import PropertyIdentifier, VendorIssuerIdentifier
from .validators import normalize_identifier


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
