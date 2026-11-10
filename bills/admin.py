"""Staff-only persistent billing configuration, using Django's permission checks."""

from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.utils import timezone

from .forms import (
    PropertyIdentifierForm,
    PropertyServiceForm,
    ServiceInvoiceExceptionForm,
    ServiceVendorAssignmentForm,
    ServiceVendorAssignmentFormSet,
    VendorIssuerIdentifierForm,
)
from .models import (
    Property,
    PropertyIdentifier,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)


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


class ServiceVendorAssignmentInline(admin.StackedInline):
    model = ServiceVendorAssignment
    form = ServiceVendorAssignmentForm
    formset = ServiceVendorAssignmentFormSet
    verbose_name_plural = "Vendor assignments"
    fields = [
        ("vendor", "is_active"),
        ("start_month", "end_month"),
    ]
    extra = 1


class ServiceInvoiceExceptionInline(admin.StackedInline):
    model = ServiceInvoiceException
    form = ServiceInvoiceExceptionForm
    fields = [("vendor", "month"), "reason"]
    extra = 1


@admin.register(Property)
class PropertyAdmin(admin.ModelAdmin):
    change_form_template = "admin/bills/property/change_form.html"
    list_display = ["name", "slug"]
    search_fields = ["name", "slug"]
    prepopulated_fields = {"slug": ["name"]}

    def change_view(self, request, object_id, form_url="", extra_context=None):
        context = {
            **(extra_context or {}),
            "property_services": PropertyService.objects.filter(
                property=self.get_object(request, object_id)
            )
            .select_related("property")
            .prefetch_related("vendor_assignments__vendor"),
        }
        return super().change_view(request, object_id, form_url, context)


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
    inlines = [
        IssuerIdentifierInline,
        LayoutFamilyInline,
        PropertyIdentifierInline,
    ]

    def change_view(self, request, object_id, form_url="", extra_context=None):
        context = {
            **(extra_context or {}),
            "service_assignments": ServiceVendorAssignment.objects.filter(
                vendor=self.get_object(request, object_id)
            ).select_related("property_service__property"),
        }
        return super().change_view(request, object_id, form_url, context)


@admin.register(PropertyService)
class PropertyServiceAdmin(PropertySelectorMixin, admin.ModelAdmin):
    form = PropertyServiceForm
    list_display = [
        "property",
        "category_label",
        "subcategory_label",
        "vendors",
        "frequency_months",
        "service_starts",
        "service_ends",
        "vendor_since",
        "is_active",
    ]
    list_select_related = ["property"]
    list_filter = ["property", "service", "subcategory", "is_active"]
    search_fields = ["property__name", "vendor_assignments__vendor__name"]
    fieldsets = [
        (None, {"fields": [("property", "is_active"), ("service", "subcategory")]}),
        (
            "Expected invoices",
            {"fields": [("start_month", "end_month"), ("frequency_months", "grace_days")]},
        ),
    ]
    inlines = [ServiceVendorAssignmentInline, ServiceInvoiceExceptionInline]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("vendor_assignments__vendor")

    def _current_assignments(self, obj):
        month = timezone.now().date().replace(day=1)
        return [
            row
            for row in obj.vendor_assignments.all()
            if row.is_active
            and row.start_month <= month
            and (row.end_month is None or row.end_month >= month)
        ]

    @admin.display(description="Current vendor")
    def vendors(self, obj):
        return (
            ", ".join(row.vendor.name for row in self._current_assignments(obj))
            or "No current vendor"
        )

    @admin.display(description="Service starts", ordering="start_month")
    def service_starts(self, obj):
        return obj.start_month

    @admin.display(description="Service ends", ordering="end_month")
    def service_ends(self, obj):
        return obj.end_month

    @admin.display(description="Vendor since")
    def vendor_since(self, obj):
        return (
            ", ".join(row.start_month.strftime("%Y-%m") for row in self._current_assignments(obj))
            or "—"
        )


@admin.register(PropertyIdentifier)
class PropertyIdentifierAdmin(PropertySelectorMixin, admin.ModelAdmin):
    form = PropertyIdentifierForm
    list_display = ["vendor", "kind", "value", "property"]
    list_filter = ["vendor", "kind", "property"]
    search_fields = ["value", "vendor__name", "property__name"]
