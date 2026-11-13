"""One catalogue for costs and consumption, filtered by category in the browser."""

from django import forms
from django.db.models import Q

from .models import Subcategory


class SubcategorySelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if hasattr(value, "instance"):
            option["attrs"]["data-category"] = value.instance.category
            option["attrs"]["data-waste-fraction"] = (
                "true" if value.instance.is_waste_fraction else "false"
            )
        return option


def catalogue_field(current=None):
    return forms.ModelChoiceField(
        label="Subcategory",
        queryset=Subcategory.objects.filter(Q(is_active=True) | Q(pk=current)),
        required=False,
        empty_label="Unspecified",
        widget=SubcategorySelect,
    )
