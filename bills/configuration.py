"""Shared configuration validation, including complete proposed inline edits."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from django.core.exceptions import ValidationError

_staged = ContextVar("staged_configuration", default=False)


def configuration_is_staged():
    return _staged.get()


@contextmanager
def staged_configuration():
    token = _staged.set(True)
    try:
        yield
    finally:
        _staged.reset(token)


def month_number(value):
    return value.year * 12 + value.month - 1


def add_months(value, count):
    year, month = divmod(month_number(value) + count, 12)
    return date(year, month + 1, 1)


def contains(row, month):
    return row.start_month <= month and (row.end_month is None or month <= row.end_month)


def overlaps(first, second):
    return (first.end_month is None or second.start_month <= first.end_month) and (
        second.end_month is None or first.start_month <= second.end_month
    )


def expected(schedule, month):
    return (
        schedule.is_active
        and schedule.frequency_months > 0
        and contains(schedule, month)
        and month >= schedule.first_expected_month
        and (month_number(month) - month_number(schedule.first_expected_month))
        % schedule.frequency_months
        == 0
    )


def validate_months(row):
    errors = {}
    for field in ("start_month", "end_month", "first_expected_month", "month"):
        value = getattr(row, field, None)
        if value and value.day != 1:
            errors[field] = "Use the first day of the month."
    start, end = getattr(row, "start_month", None), getattr(row, "end_month", None)
    if start and end and end < start:
        errors["end_month"] = "The last month must not precede the first."
    anchor = getattr(row, "first_expected_month", None)
    if anchor and start and anchor < start:
        errors["first_expected_month"] = (
            "The first expected month must not precede the effective first month."
        )
    if errors:
        raise ValidationError(errors)


def validate_classification(category, key):
    from .models import Category, Subcategory

    if category not in Category.values:
        raise ValidationError({"category": "Choose a valid category."})
    if key and not Subcategory.objects.filter(pk=key, category=category).exists():
        raise ValidationError({"subcategory": "The subcategory must belong to this category."})


def proposed_configuration(replacements=(), deletions=()):
    from .models import (
        PropertyService,
        ServiceInvoiceException,
        ServiceVendorAssignment,
        VendorBillingSchedule,
    )

    models = (
        PropertyService,
        ServiceVendorAssignment,
        VendorBillingSchedule,
        ServiceInvoiceException,
    )
    graph = {model: list(model.objects.all()) for model in models}
    for row in [*deletions, *replacements]:
        if type(row) in graph and row.pk:
            graph[type(row)] = [old for old in graph[type(row)] if old.pk != row.pk]
    for row in replacements:
        if type(row) in graph:
            graph[type(row)].append(row)
    return graph


def validate_configuration(graph):
    from .models import (
        PropertyService,
        ServiceInvoiceException,
        ServiceVendorAssignment,
        VendorBillingSchedule,
    )

    services = graph[PropertyService]
    assignments = graph[ServiceVendorAssignment]
    schedules = graph[VendorBillingSchedule]
    exceptions = graph[ServiceInvoiceException]
    with staged_configuration():
        for rows in graph.values():
            for row in rows:
                exclude = [
                    field.name
                    for field in row._meta.fields
                    if field.is_relation
                    and getattr(row, field.attname) is None
                    and field.is_cached(row)
                    and getattr(row, field.name) is not None
                ]
                row.full_clean(exclude=exclude, validate_unique=False, validate_constraints=False)

    for rows, key, label in (
        (services, lambda r: (r.property_id, r.category, r.subcategory_id), "property services"),
        (assignments, lambda r: (r.property_service_id, r.vendor_id), "service vendors"),
        (schedules, lambda r: r.vendor_id, "vendor billing schedules"),
    ):
        for index, row in enumerate(rows):
            validate_months(row)
            if not row.is_active:
                continue
            for other in rows[:index]:
                if other.is_active and key(row) == key(other) and overlaps(row, other):
                    raise ValidationError(f"Enabled {label} must not overlap.")

    def service_key(row):
        return row.pk if row.pk is not None else id(row)

    def scope_key(row):
        if row.property_service_id is not None:
            return row.property_service_id
        if (
            row._meta.get_field("property_service").is_cached(row)
            and row.property_service is not None
        ):
            return service_key(row.property_service)
        return None

    by_service = {service_key(row): row for row in services}
    for assignment in assignments:
        service = by_service.get(scope_key(assignment))
        if service is None:
            service = assignment.property_service
        if assignment.start_month < service.start_month or (
            service.end_month
            and (assignment.end_month is None or assignment.end_month > service.end_month)
        ):
            raise ValidationError("Service vendor dates must be within the service tracking dates.")
    for exception in exceptions:
        validate_months(exception)
        if not exception.reason.strip():
            raise ValidationError("Explain why no invoice was issued.")
        applicable = [
            row
            for row in schedules
            if row.vendor_id == exception.vendor_id and expected(row, exception.month)
        ]
        scope = scope_key(exception)
        relevant = [
            row
            for row in services
            if row.is_active
            and row.mode == PropertyService.Mode.REGULAR
            and contains(row, exception.month)
            and (scope is None or service_key(row) == scope)
        ]
        if not applicable or not any(
            assignment.is_active
            and assignment.vendor_id == exception.vendor_id
            and contains(assignment, exception.month)
            and any(service_key(service) == scope_key(assignment) for service in relevant)
            for assignment in assignments
        ):
            raise ValidationError(
                "Choose a scheduled invoice month with an assigned regular service."
            )
        if exception.reviewed_invoice_id:
            invoice = exception.reviewed_invoice
            if (
                not scope
                or invoice.vendor_id != exception.vendor_id
                or invoice.kind != "invoice"
                or invoice.status != "approved"
                or not invoice.invoice_date
                or invoice.invoice_date.replace(day=1) != exception.month
                or exception.reviewed_revision is None
                or exception.reviewed_revision > invoice.review_revision
            ):
                raise ValidationError(
                    "The reviewed invoice must match this vendor, month and service scope."
                )


def validate_configuration_write(instance):
    if not _staged.get():
        validate_configuration(proposed_configuration([instance]))


def validate_configuration_delete(instance):
    if not _staged.get():
        validate_configuration(proposed_configuration(deletions=[instance]))
