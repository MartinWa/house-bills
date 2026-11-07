"""Staff-only persistent billing configuration, using Django's permission checks."""

from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper

from .forms import PropertyIdentifierForm, VendorIssuerIdentifierForm
from .models import Property, PropertyIdentifier, Vendor, VendorIssuerIdentifier, VendorLayoutFamily


class PropertySelectorMixin:
    """Select houses here; manage them on the Properties screen."""

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        formfield = super().formfield_for_dbfield(db_field, request, **kwargs)
        if (
            formfield is not None
            and isinstance(formfield.widget, RelatedFieldWidgetWrapper)
            and db_field.remote_field.model is Property
        ):
            formfield.widget.can_add_related = False
            formfield.widget.can_change_related = False
            formfield.widget.can_delete_related = False
            formfield.widget.can_view_related = False
        return formfield


class IssuerIdentifierInline(admin.TabularInline):
    model = VendorIssuerIdentifier
    form = VendorIssuerIdentifierForm
    extra = 1


class LayoutFamilyInline(admin.TabularInline):
    model = VendorLayoutFamily
    extra = 1


class PropertyIdentifierInline(PropertySelectorMixin, admin.TabularInline):
    model = PropertyIdentifier
    form = PropertyIdentifierForm
    verbose_name = "specific mapping"
    verbose_name_plural = "specific mappings"
    extra = 1


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Vendor)
class VendorAdmin(PropertySelectorMixin, admin.ModelAdmin):
    change_form_template = "admin/bills/vendor/change_form.html"
    fieldsets = [
        (None, {"fields": ["name", "slug"]}),
        (None, {"fields": ["default_property"], "classes": ["property-mappings"]}),
    ]
    list_display = ["name", "slug", "default_property"]
    list_select_related = ["default_property"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [IssuerIdentifierInline, LayoutFamilyInline, PropertyIdentifierInline]


@admin.register(PropertyIdentifier)
class PropertyIdentifierAdmin(PropertySelectorMixin, admin.ModelAdmin):
    form = PropertyIdentifierForm
    list_display = ["vendor", "kind", "value", "property"]
    list_filter = ["vendor", "kind", "property"]
    search_fields = ["value", "vendor__name", "property__name"]
