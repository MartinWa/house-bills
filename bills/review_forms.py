"""Editable review proposals; approved rows are written only by the review service."""

from datetime import date, timedelta

from django import forms
from django.core.exceptions import ValidationError

from .models import Invoice, Property, Service, Usage, Vendor
from .parsing.compatibility import cost_totals_candidate
from .parsing.normalization import scaled_integer
from .parsing.types import Candidate, Evidence, Identifier, Section
from .parsing.types import Usage as CandidateUsage
from .presentation import scaled_value
from .validators import normalize_identifier

SECTION_MONEY = ("net_minor", "vat_minor", "gross_minor")
HEADER_MONEY = (
    "net_minor",
    "vat_minor",
    "rounding_minor",
    "invoice_total_minor",
    "amount_due_minor",
)
INVOICE_SCOPE = "invoice"
IDENTIFIER_KINDS = [
    ("customer_account", "Customer account"),
    ("installation", "Installation"),
    ("contract", "Contract"),
    ("delivery_address", "Delivery address"),
    ("organization", "Organization number"),
    ("vat", "VAT identifier"),
]


class ScaledNumberField(forms.Field):
    """Accept Swedish or dot decimals without rounding or floating point."""

    def __init__(self, *, scale=100, precision=2, **kwargs):
        self.scale, self.precision = scale, precision
        super().__init__(
            widget=forms.TextInput(attrs={"inputmode": "decimal", "maxlength": 32}), **kwargs
        )

    def to_python(self, value):
        if value in self.empty_values:
            return None
        try:
            if len(str(value)) > 32:
                raise ValueError
            number = scaled_integer(str(value), self.scale)
        except ValueError:
            raise ValidationError(
                f"Enter a number with at most {self.precision} decimal places."
            ) from None
        if not -(2**63) <= number < 2**63:
            raise ValidationError("This number is too large to store.")
        return number

    def prepare_value(self, value):
        return scaled_value(value, self.scale, self.precision) if type(value) is int else value


def date_field(label, *, required=True):
    return forms.DateField(
        label=label,
        required=required,
        input_formats=["%Y-%m-%d"],
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
    )


class InvoiceReviewForm(forms.Form):
    review_token = forms.CharField(widget=forms.HiddenInput)
    vendor = forms.ModelChoiceField(queryset=Vendor.objects.all())
    reference = forms.CharField(label="Invoice reference", max_length=120)
    kind = forms.ChoiceField(label="Invoice type", choices=Invoice.Kind.choices)
    invoice_date = date_field("Invoice date")
    due_date = date_field("Due date", required=False)
    currency = forms.ChoiceField(choices=[("SEK", "SEK")])
    net_minor = ScaledNumberField(label="Net (SEK)")
    vat_minor = ScaledNumberField(label="VAT (SEK)")
    rounding_minor = ScaledNumberField(label="Rounding (SEK)")
    invoice_total_minor = ScaledNumberField(label="Invoice total (SEK)")
    amount_due_minor = ScaledNumberField(label="Amount due (SEK)")
    page = forms.IntegerField(label="Header source page", min_value=1, initial=1)
    amount_due_confirmed = forms.BooleanField(
        label="I confirm any difference between invoice total and amount due.", required=False
    )
    warnings_confirmed = forms.BooleanField(
        label="I checked the source and resolved the extraction warnings above."
    )
    note = forms.CharField(
        label="Review or correction note", required=False, max_length=2000, widget=forms.Textarea
    )

    def __init__(self, *args, warnings=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not warnings:
            del self.fields["warnings_confirmed"]
        self.fields["note"].widget.attrs["rows"] = 3

    def clean(self):
        data = super().clean()
        fields = ("net_minor", "vat_minor", "rounding_minor", "invoice_total_minor")
        if all(field in data for field in fields) and (
            data["net_minor"] + data["vat_minor"] + data["rounding_minor"]
            != data["invoice_total_minor"]
        ):
            self.add_error("invoice_total_minor", "Net, VAT and rounding must equal invoice total.")
        if (
            "invoice_total_minor" in data
            and "amount_due_minor" in data
            and data["invoice_total_minor"] != data["amount_due_minor"]
            and not data.get("amount_due_confirmed")
        ):
            self.add_error("amount_due_confirmed", "Confirm the difference in amount due.")
        return data


class TotalsForm(forms.Form):
    net_minor = ScaledNumberField(label="Net (SEK)")
    vat_minor = ScaledNumberField(label="VAT (SEK)")
    gross_minor = ScaledNumberField(label="Cost including VAT (SEK)")

    def clean(self):
        data = super().clean()
        if all(field in data for field in SECTION_MONEY) and (
            data["net_minor"] + data["vat_minor"] != data["gross_minor"]
        ):
            self.add_error("gross_minor", "Net plus VAT must equal cost including VAT.")
        return data


class SectionChoiceMixin:
    def __init__(self, *args, section_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["section"].choices = [("", "Choose an allocation"), *section_choices]


class PeriodMixin:
    def clean(self):
        data = super().clean()
        if "start" in data and "end" in data:
            if data["start"] > data["end"]:
                self.add_error("end", "Last billed day must be on or after the first day.")
            elif data["end"] == date.max:
                self.add_error("end", "Choose a last billed day before 9999-12-31.")
        return data


class SectionForm(PeriodMixin, TotalsForm):
    field_order = ["section_id", "property", "service", *SECTION_MONEY, "start", "end", "page"]
    section_id = forms.CharField(max_length=120, widget=forms.HiddenInput)
    property = forms.ModelChoiceField(queryset=Property.objects.all())
    service = forms.ChoiceField(choices=Service.choices)
    start = date_field("First billed day")
    end = date_field("Last billed day")
    page = forms.IntegerField(label="Source page", min_value=1, initial=1)

    def clean_section_id(self):
        value = self.cleaned_data["section_id"]
        if value == INVOICE_SCOPE:
            raise ValidationError("Choose a distinct allocation.")
        return value


class UsageForm(SectionChoiceMixin, PeriodMixin, forms.Form):
    section = forms.ChoiceField(label="Allocation", choices=[])
    utility = forms.ChoiceField(choices=Usage.Utility.choices)
    unit = forms.ChoiceField(choices=Usage._meta.get_field("unit").choices)
    quantity_milli = ScaledNumberField(label="Quantity", scale=1000, precision=3)
    start = date_field("First billed day")
    end = date_field("Last billed day")
    basis = forms.ChoiceField(label="Measurement basis", choices=Usage.Basis.choices)
    page = forms.IntegerField(label="Source page", min_value=1, initial=1)

    def clean(self):
        data = super().clean()
        units = {"electricity": "kWh", "water": "m3", "waste": "kg"}
        if "utility" in data and "unit" in data and data["unit"] != units[data["utility"]]:
            self.add_error("unit", "Choose the unit that matches this utility.")
        if data.get("basis") == "reversal" and data.get("quantity_milli", 0) > 0:
            self.add_error("quantity_milli", "A reversal must have a zero or negative quantity.")
        return data


class ReviewIdentifierForm(forms.Form):
    scope = forms.ChoiceField(label="Applies to", choices=[], initial=INVOICE_SCOPE)
    kind = forms.ChoiceField(label="Identifier type", choices=IDENTIFIER_KINDS)
    value = forms.CharField(max_length=200)
    page = forms.IntegerField(label="Source page", min_value=1, initial=1)

    def __init__(self, *args, section_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["scope"].choices = [(INVOICE_SCOPE, "Invoice"), *section_choices]

    def clean_value(self):
        return normalize_identifier(self.cleaned_data.get("kind", ""), self.cleaned_data["value"])


class ReviewFormSet(forms.BaseFormSet):
    def _construct_form(self, i, **kwargs):
        # Rows exist only when extracted or explicitly added; every live row is required.
        return super()._construct_form(i, empty_permitted=False, **kwargs)

    def clean(self):
        expected = len(self.initial or [])
        if self.management_form.cleaned_data.get("INITIAL_FORMS") != expected or (
            self.management_form.cleaned_data.get("TOTAL_FORMS", 0) < expected
        ):
            raise ValidationError(
                "Form rows changed unexpectedly; reload the review before saving."
            )

    @property
    def active_forms(self):
        return [form for form in self.forms if not form.cleaned_data.get("DELETE")]


def formset(form, limit):
    return forms.formset_factory(
        form,
        formset=ReviewFormSet,
        extra=0,
        can_delete=True,
        max_num=limit,
        absolute_max=limit,
        validate_max=True,
    )


SectionFormSet = formset(SectionForm, 20)
UsageFormSet = formset(UsageForm, 60)
IdentifierFormSet = formset(ReviewIdentifierForm, 60)


def evidence_page(evidence):
    return evidence.get("page", 1) if isinstance(evidence, dict) else 1


def last_billed_day(value):
    try:
        return date.fromisoformat(value) - timedelta(days=1)
    except ValueError, TypeError:
        return value


def reviewed_evidence(original, *, page, unchanged, label):
    if unchanged and original and evidence_page(original) == page:
        return Evidence(original["page"], original["text"], tuple(original["bbox"]))
    return Evidence(page, f"Manually reviewed: {label}", (0, 0, 0, 0))


class ReviewForms:
    """Invoice totals, dated allocations and consumption; no itemized fee formset."""

    def __init__(self, *, values, properties, vendor_id, kind, token, warnings, data=None):
        values = cost_totals_candidate(values)
        self.values = values
        self.originals = {"usages": [], "identifiers": []}
        sections, usages, identifiers = [], [], []
        for identifier in values.get("identifiers", []):
            self._identifier(identifier, INVOICE_SCOPE, identifiers)
        for section in values.get("sections", []):
            key = section["section_id"]
            sections.append(
                {
                    **{field: section.get(field) for field in SECTION_MONEY},
                    "section_id": key,
                    "property": properties.get(key),
                    "service": section.get("service"),
                    "start": section.get("start"),
                    "end": last_billed_day(section.get("end")),
                    "page": evidence_page(section.get("total_evidence")),
                }
            )
            for identifier in section.get("identifiers", []):
                self._identifier(identifier, key, identifiers)
            for group, destination in (("usages", usages),):
                for row in section.get(group, []):
                    self.originals[group].append({**row, "section": key})
                    destination.append(
                        {
                            **row,
                            "section": key,
                            "end": last_billed_day(row.get("end")),
                            "page": evidence_page(row.get("evidence")),
                        }
                    )
        if not sections:
            sections = [{"section_id": "manual-1", "page": 1}]

        self.header = InvoiceReviewForm(
            data,
            warnings=bool(warnings),
            initial={
                **values,
                "review_token": token,
                "vendor": vendor_id,
                "kind": kind,
                "page": evidence_page(next(iter(values.get("header_evidence", {}).values()), {})),
            },
        )
        self.sections = SectionFormSet(data, initial=sections, prefix="sections")
        choices = []
        # Choices include added rows on a bound form, but never trusts their assignments.
        for index, section in enumerate(self.sections):
            key = section["section_id"].value()
            if isinstance(key, str) and key and len(key) <= 120 and key != INVOICE_SCOPE:
                choices.append((key, f"Allocation {index + 1}"))
        kwargs = {"data": data, "form_kwargs": {"section_choices": choices}}
        self.usages = UsageFormSet(**kwargs, initial=usages, prefix="usages")
        self.identifiers = IdentifierFormSet(**kwargs, initial=identifiers, prefix="identifiers")
        self.groups = [
            ("sections", "Property and service allocations", self.sections),
            ("usages", "Consumption", self.usages),
            ("identifiers", "Identifiers", self.identifiers),
        ]

    def _identifier(self, row, scope, destination):
        self.originals["identifiers"].append({**row, "scope": scope})
        destination.append({**row, "scope": scope, "page": evidence_page(row.get("evidence"))})

    def is_valid(self):
        results = [self.header.is_valid(), *[group.is_valid() for _, _, group in self.groups]]
        if not all(results):
            return False
        sections = {}
        for form in self.sections.active_forms:
            key = form.cleaned_data["section_id"]
            if key in sections:
                form.add_error(None, "Each allocation must be distinct.")
            sections[key] = form
        if not sections:
            self.header.add_error(None, "Add at least one property and service allocation.")
            return False

        for field in ("net_minor", "vat_minor"):
            if sum(form.cleaned_data.get(field, 0) for form in sections.values()) != (
                self.header.cleaned_data.get(field)
            ):
                self.header.add_error(field, "Allocation amounts must add up to the invoice total.")
        gross = sum(form.cleaned_data.get("gross_minor", 0) for form in sections.values())
        if gross + self.header.cleaned_data.get("rounding_minor", 0) != (
            self.header.cleaned_data.get("invoice_total_minor")
        ):
            self.header.add_error(
                "invoice_total_minor", "Allocation costs plus rounding must equal invoice total."
            )

        electricity = []
        utilities = {
            Service.ELECTRICITY_SUPPLY: "electricity",
            Service.ELECTRICITY_NETWORK: "electricity",
            Service.WATER: "water",
            Service.WASTE: "waste",
        }
        for form in self.usages.active_forms:
            data = form.cleaned_data.copy()
            section = sections.get(data["section"])
            if section is None:
                form.add_error("section", "This allocation was removed.")
                continue
            service = section.cleaned_data["service"]
            if data["utility"] != utilities.get(service):
                form.add_error("utility", "Consumption must match the allocation's service.")
            if data["utility"] == "electricity":
                source = (section.cleaned_data["property"].pk, service, data["basis"])
                for other, other_source in electricity:
                    if (
                        source == other_source
                        and data["start"] <= other["end"]
                        and (other["start"] <= data["end"])
                    ):
                        form.add_error("start", "Overlapping electricity consumption needs review.")
                electricity.append((data, source))
        for form in self.identifiers.active_forms:
            scope = form.cleaned_data["scope"]
            if scope != INVOICE_SCOPE and scope not in sections:
                form.add_error("scope", "This allocation was removed.")
        return not self.header.errors and all(
            not form.errors for _, _, group in self.groups for form in group.active_forms
        )

    def candidate(self):
        """Convert a fully checked form into a fresh proposal, retaining unchanged evidence."""
        header = self.header.cleaned_data
        values = self.values
        evidence = dict(values.get("header_evidence", {}))
        aliases = {
            "net_minor": "net",
            "vat_minor": "vat",
            "rounding_minor": "rounding",
            "amount_due_minor": "amount_due",
        }
        for field in ("reference", "invoice_date", "due_date", "currency", *HEADER_MONEY):
            value = header[field]
            comparison = value.isoformat() if isinstance(value, date) else value
            key = aliases.get(field, field)
            evidence[key] = reviewed_evidence(
                values.get("header_evidence", {}).get(key),
                page=header["page"],
                unchanged=comparison == values.get(field),
                label=field,
            )
        candidate = Candidate(
            layout_family=values.get("layout_family") or "manual",
            layout_version=values.get("layout_version") or "",
            parser_revision=values.get("parser_revision") or "",
            reference=header["reference"],
            invoice_date=header["invoice_date"],
            due_date=header["due_date"],
            currency=header["currency"],
            **{field: header[field] for field in HEADER_MONEY},
            header_evidence=evidence,
        )
        originals = {section["section_id"]: section for section in values.get("sections", [])}
        sections, properties = {}, {}
        for form in self.sections.active_forms:
            data = form.cleaned_data
            key = data["section_id"]
            original = originals.get(key, {})
            section = Section(
                key,
                data["service"],
                [],
                **{f: data[f] for f in SECTION_MONEY},
                start=data["start"],
                end=data["end"] + timedelta(days=1),
            )
            section.period_evidence = reviewed_evidence(
                original.get("period_evidence"),
                page=data["page"],
                unchanged=(section.start.isoformat(), section.end.isoformat())
                == (original.get("start"), original.get("end")),
                label="billing period",
            )
            for field in ("tax_evidence", "total_evidence"):
                unchanged = all(data[f] == original.get(f) for f in SECTION_MONEY)
                setattr(
                    section,
                    field,
                    reviewed_evidence(
                        original.get(field), page=data["page"], unchanged=unchanged, label=field
                    ),
                )
            candidate.sections.append(section)
            sections[key], properties[key] = section, data["property"]
        for group_name, cls, fields in (
            ("usages", CandidateUsage, ("utility", "unit", "quantity_milli", "basis")),
        ):
            for index, form in enumerate(getattr(self, group_name)):
                data = form.cleaned_data
                if data.get("DELETE"):
                    continue
                raw = self.originals[group_name]
                original = raw[index] if index < len(raw) else {}
                row = {field: data[field] for field in fields}
                row.update(start=data["start"], end=data["end"] + timedelta(days=1))
                unchanged = data["section"] == original.get("section") and all(
                    (value.isoformat() if isinstance(value, date) else value) == original.get(field)
                    for field, value in row.items()
                )
                row["evidence"] = reviewed_evidence(
                    original.get("evidence"),
                    page=data["page"],
                    unchanged=unchanged,
                    label=group_name,
                )
                getattr(sections[data["section"]], group_name).append(cls(**row))
        for index, form in enumerate(self.identifiers):
            data = form.cleaned_data
            if data.get("DELETE"):
                continue
            raw = self.originals["identifiers"]
            original = raw[index] if index < len(raw) else {}
            unchanged = all(data[field] == original.get(field) for field in ("kind", "scope")) and (
                data["value"] == normalize_identifier(data["kind"], original.get("value", ""))
            )
            identifier = Identifier(
                data["kind"],
                data["value"],
                reviewed_evidence(
                    original.get("evidence"),
                    page=data["page"],
                    unchanged=unchanged,
                    label="identifier",
                ),
            )
            destination = (
                candidate.identifiers
                if data["scope"] == INVOICE_SCOPE
                else sections[data["scope"]].identifiers
            )
            destination.append(identifier)
        return candidate, properties
