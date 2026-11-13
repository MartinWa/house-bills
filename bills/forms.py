"""Normalize configuration keys before admin formsets check for duplicates."""

from django import forms
from django.core.exceptions import ValidationError

from .classification_forms import catalogue_field
from .configuration import (
    proposed_configuration,
    staged_configuration,
    validate_configuration,
)
from .models import (
    Category,
    Document,
    InvoicePropertyRule,
    Property,
    PropertyService,
    ServiceCostSchedule,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Vendor,
    VendorBillingSchedule,
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
        label="Category", choices=[("", "All categories"), *Category.choices], required=False
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
            ("unscheduled", "Needs billing schedule"),
            ("incomplete", "Incomplete allocations"),
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


class ConfigurationForm(forms.ModelForm):
    defer_graph = False

    def _post_clean(self):
        with staged_configuration():
            super()._post_clean()
        if not self.defer_graph and not self.errors:
            try:
                validate_configuration(proposed_configuration([self.instance]))
            except ValidationError as exc:
                self.add_error(None, exc)


class ClassificationFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = getattr(self.instance, "subcategory_id", None)
        self.fields["subcategory"] = catalogue_field(current)


class PropertyServiceForm(ClassificationFormMixin, ConfigurationForm):
    start_month = InvoiceMonthField(label="First tracking month")
    end_month = InvoiceMonthField(label="Last tracking month", required=False)

    class Meta:
        model = PropertyService
        fields = [
            "property",
            "category",
            "subcategory",
            "service_group",
            "mode",
            "is_active",
            "start_month",
            "end_month",
        ]


class InvoiceNotSentForm(forms.Form):
    reason = forms.CharField(max_length=300, widget=forms.Textarea(attrs={"rows": 3}))
    reviewed_invoice_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    reviewed_revision = forms.IntegerField(required=False, widget=forms.HiddenInput)

    def __init__(self, *args, review_required=False, **kwargs):
        super().__init__(*args, **kwargs)
        if review_required:
            self.fields["reviewed_allocation"] = forms.BooleanField(
                label="I reviewed the invoice and confirmed this service was not billed",
            )


class ServiceInvoiceExceptionForm(ConfigurationForm):
    month = InvoiceMonthField(label="Invoice month")

    class Meta:
        model = ServiceInvoiceException
        fields = ["property_service", "vendor", "month", "reason"]


class ServiceVendorAssignmentForm(ConfigurationForm):
    start_month = InvoiceMonthField(label="First assignment month")
    end_month = InvoiceMonthField(label="Last assignment month", required=False)

    class Meta:
        model = ServiceVendorAssignment
        fields = ["property_service", "vendor", "is_active", "start_month", "end_month"]


class VendorBillingScheduleForm(ConfigurationForm):
    start_month = InvoiceMonthField(label="Effective first month")
    end_month = InvoiceMonthField(label="Effective last month", required=False)
    first_expected_month = InvoiceMonthField(label="First expected invoice month")

    class Meta:
        model = VendorBillingSchedule
        fields = [
            "vendor",
            "start_month",
            "end_month",
            "frequency_months",
            "first_expected_month",
            "grace_days",
            "is_active",
        ]


class ServiceCostScheduleForm(ConfigurationForm):
    start_month = InvoiceMonthField(label="Effective first month")
    end_month = InvoiceMonthField(label="Effective last month", required=False)
    first_expected_month = InvoiceMonthField(
        label="First bill containing this cost", required=False
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "service_vendor_assignment" in self.fields:
            self.fields[
                "service_vendor_assignment"
            ].queryset = ServiceVendorAssignment.objects.filter(
                property_service__category="waste"
            ).select_related(
                "vendor", "property_service__property", "property_service__subcategory"
            )
        self.fields["frequency_months"].help_text = (
            "Select which shared vendor bills should include this cost. "
            "As needed leaves it optional."
        )
        self.fields[
            "first_expected_month"
        ].help_text = "Required for periodic costs; must be a vendor invoice month."
        self.fields[
            "season_start_month"
        ].help_text = "Optional billing season, 1–12. Set both ends; a season can wrap December."

    class Meta:
        model = ServiceCostSchedule
        fields = [
            "service_vendor_assignment",
            "start_month",
            "end_month",
            "frequency_months",
            "first_expected_month",
            "season_start_month",
            "season_end_month",
            "is_active",
        ]


class ChangeCostPatternForm(forms.Form):
    month = InvoiceMonthField(label="Change from month")
    frequency_months = forms.TypedChoiceField(
        choices=ServiceCostSchedule.Frequency.choices,
        coerce=int,
        label="Cost frequency",
    )
    first_expected_month = InvoiceMonthField(
        label="First bill containing this cost", required=False
    )
    season_start_month = forms.IntegerField(
        label="First billing season month", required=False, min_value=1, max_value=12
    )
    season_end_month = forms.IntegerField(
        label="Last billing season month", required=False, min_value=1, max_value=12
    )


class ChangeVendorForm(forms.Form):
    month = InvoiceMonthField(label="Change from month")
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.all(), label="Replacement vendor")
    transition_month = forms.BooleanField(
        required=False,
        label="Both vendors may bill in the transition month",
    )


class ChangeBillingFrequencyForm(forms.Form):
    month = InvoiceMonthField(label="Effective first month")
    frequency_months = forms.TypedChoiceField(
        choices=VendorBillingSchedule.Frequency.choices,
        coerce=int,
        label="Frequency",
    )
    first_expected_month = InvoiceMonthField(label="First expected invoice month")
    grace_days = forms.IntegerField(min_value=0, max_value=60)


class ConfigurationInlineFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        group = getattr(self, "configuration_group", [self])
        if self is not group[0]:
            return
        for formset in group:
            for form in formset.forms:
                if not hasattr(form, "cleaned_data"):
                    form.full_clean()
        if any(
            form.errors
            for formset in group
            for form in formset.forms
            if not (formset.can_delete and formset._should_delete_form(form))
        ):
            return
        replacements, deletions = [self.instance], []
        for formset in group:
            for form in formset.forms:
                if not form.cleaned_data:
                    continue
                if formset.can_delete and formset._should_delete_form(form):
                    deletions.append(form.instance)
                else:
                    replacements.append(form.instance)
        try:
            validate_configuration(proposed_configuration(replacements, deletions))
        except ValidationError as exc:
            # The parent may already have field errors; formsets require an error list.
            raise ValidationError(exc.messages) from exc


ServiceVendorAssignmentFormSet = ConfigurationInlineFormSet


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


class InvoicePropertyRuleForm(IdentifierForm):
    class Meta:
        model = InvoicePropertyRule
        fields = ["vendor", "property", "kind", "value"]


class VendorIssuerIdentifierForm(IdentifierForm):
    class Meta:
        model = VendorIssuerIdentifier
        fields = ["vendor", "kind", "value"]
