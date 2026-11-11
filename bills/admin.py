"""Staff-only persistent billing configuration, using Django's permission checks."""

from django.contrib import admin
from django.contrib.admin.widgets import RelatedFieldWidgetWrapper
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils import timezone

from .configuration import staged_configuration
from .configuration_actions import change_billing_frequency, change_vendor
from .forms import (
    ChangeBillingFrequencyForm,
    ChangeVendorForm,
    ConfigurationInlineFormSet,
    InvoicePropertyRuleForm,
    PropertyServiceForm,
    ServiceInvoiceExceptionForm,
    ServiceVendorAssignmentForm,
    VendorBillingScheduleForm,
    VendorIssuerIdentifierForm,
)
from .models import (
    InvoicePropertyRule,
    Property,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Subcategory,
    Vendor,
    VendorBillingSchedule,
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


class InvoicePropertyRuleInline(PropertySelectorMixin, admin.TabularInline):
    model = InvoicePropertyRule
    form = InvoicePropertyRuleForm
    verbose_name = "specific rule"
    verbose_name_plural = "specific rules"
    extra = 1


class ServiceVendorAssignmentInline(admin.StackedInline):
    model = ServiceVendorAssignment
    form = ServiceVendorAssignmentForm
    formset = ConfigurationInlineFormSet
    verbose_name_plural = "Service vendors"
    fields = [
        ("vendor", "is_active"),
        ("start_month", "end_month"),
    ]
    extra = 1
    show_change_link = True

    def get_formset(self, request, obj=None, **kwargs):
        base = super().get_formset(request, obj, **kwargs)

        class DefaultDatesFormSet(base):
            def _construct_form(self, index, **form_kwargs):
                form = super()._construct_form(index, **form_kwargs)
                if obj and not form.instance.pk:
                    form.initial.setdefault("start_month", obj.start_month)
                    form.initial.setdefault("end_month", obj.end_month)
                return form

        return DefaultDatesFormSet


class ServiceInvoiceExceptionInline(admin.StackedInline):
    model = ServiceInvoiceException
    form = ServiceInvoiceExceptionForm
    formset = ConfigurationInlineFormSet
    fields = [("vendor", "month"), "reason"]
    extra = 1


class ConfigurationAdmin(admin.ModelAdmin):
    class Media:
        js = ["bills/classification.js"]

    def get_form(self, request, obj=None, change=False, **kwargs):
        form = super().get_form(request, obj, change, **kwargs)
        if hasattr(form, "defer_graph") and self.inlines:
            form.defer_graph = True
        return form

    def _create_formsets(self, request, obj, change):
        formsets, inlines = super()._create_formsets(request, obj, change)
        group = [formset for formset in formsets if isinstance(formset, ConfigurationInlineFormSet)]
        for formset in group:
            formset.configuration_group = group
            formset.form.defer_graph = True
        return formsets, inlines

    def save_model(self, request, obj, form, change):
        with staged_configuration():
            super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        with staged_configuration():
            super().save_related(request, form, formsets, change)

    def delete_model(self, request, obj):
        obj.delete()

    def dated_change(self, request, object_id, *, frequency=False):
        obj = self.get_object(request, object_id)
        if obj is None:
            raise Http404
        if not self.has_change_permission(request, obj) or not self.has_add_permission(request):
            raise PermissionDenied
        if request.method not in ("GET", "POST"):
            raise PermissionDenied
        form_class = ChangeBillingFrequencyForm if frequency else ChangeVendorForm
        initial = {"grace_days": getattr(obj, "grace_days", 0)}
        form = form_class(request.POST if request.method == "POST" else None, initial=initial)
        if request.method == "POST" and form.is_valid():
            try:
                replacement = (change_billing_frequency if frequency else change_vendor)(
                    obj.pk,
                    **form.cleaned_data,
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                self.log_change(request, obj, "Ended period through dated configuration change.")
                self.log_addition(request, replacement, "Created replacement dated period.")
                self.message_user(request, "Configuration history saved.")
                return HttpResponseRedirect(
                    reverse(
                        f"admin:bills_{self.model._meta.model_name}_change",
                        args=[replacement.pk],
                    )
                )
        return TemplateResponse(
            request,
            "admin/bills/dated_change.html",
            {
                **self.admin_site.each_context(request),
                "form": form,
                "original": obj,
                "title": "Change billing frequency" if frequency else "Change vendor",
                "opts": self.model._meta,
            },
        )


class VendorBillingScheduleInline(admin.StackedInline):
    model = VendorBillingSchedule
    form = VendorBillingScheduleForm
    formset = ConfigurationInlineFormSet
    extra = 1
    show_change_link = True


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
class VendorAdmin(PropertySelectorMixin, ConfigurationAdmin):
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
        VendorBillingScheduleInline,
        InvoicePropertyRuleInline,
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
class PropertyServiceAdmin(PropertySelectorMixin, ConfigurationAdmin):
    form = PropertyServiceForm
    list_display = [
        "property",
        "category_label",
        "subcategory_label",
        "vendors",
        "mode",
        "service_starts",
        "service_ends",
        "vendor_since",
        "is_active",
    ]
    list_select_related = ["property"]
    list_filter = ["property", "category", "subcategory", "is_active"]
    search_fields = ["property__name", "vendor_assignments__vendor__name"]
    fieldsets = [
        (None, {"fields": [("property", "is_active"), ("category", "subcategory")]}),
        (
            "Tracking period",
            {"fields": [("start_month", "end_month"), "mode"]},
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


@admin.register(InvoicePropertyRule)
class InvoicePropertyRuleAdmin(PropertySelectorMixin, admin.ModelAdmin):
    form = InvoicePropertyRuleForm
    list_display = ["vendor", "kind", "value", "property"]
    list_filter = ["vendor", "kind", "property"]
    search_fields = ["value", "vendor__name", "property__name"]


@admin.register(Subcategory)
class SubcategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "key", "category", "is_active"]
    list_filter = ["category", "is_active"]
    search_fields = ["name", "key"]

    def get_readonly_fields(self, request, obj=None):
        return ["key"] if obj else []


@admin.register(VendorBillingSchedule)
class VendorBillingScheduleAdmin(ConfigurationAdmin):
    change_form_template = "admin/bills/vendorbillingschedule/change_form.html"
    form = VendorBillingScheduleForm
    list_display = [
        "vendor",
        "start_month",
        "end_month",
        "frequency_months",
        "first_expected_month",
        "grace_days",
        "is_active",
    ]
    list_filter = ["vendor", "is_active", "frequency_months"]

    def get_urls(self):
        return [
            path(
                "<path:object_id>/change-frequency/",
                self.admin_site.admin_view(
                    lambda request, object_id: self.dated_change(request, object_id, frequency=True)
                ),
                name="bills_vendorbillingschedule_change_frequency",
            )
        ] + super().get_urls()


@admin.register(ServiceVendorAssignment)
class ServiceVendorAssignmentAdmin(ConfigurationAdmin):
    change_form_template = "admin/bills/servicevendorassignment/change_form.html"
    form = ServiceVendorAssignmentForm
    list_display = ["property_service", "vendor", "start_month", "end_month", "is_active"]
    list_filter = ["vendor", "property_service__property", "is_active"]

    def get_urls(self):
        return [
            path(
                "<path:object_id>/change-vendor/",
                self.admin_site.admin_view(self.dated_change),
                name="bills_servicevendorassignment_change_vendor",
            )
        ] + super().get_urls()


@admin.register(ServiceInvoiceException)
class ServiceInvoiceExceptionAdmin(ConfigurationAdmin):
    form = ServiceInvoiceExceptionForm
    list_display = ["vendor", "property_service", "month", "reason"]
    list_filter = ["vendor", "property_service__property"]
