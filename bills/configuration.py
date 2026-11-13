"""Shared configuration validation, including complete proposed inline edits."""

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date

from django.core.exceptions import ValidationError

_staged = ContextVar("staged_configuration", default=False)
EXCEPTION_CONTEXT_FIELDS = (
    "property_service_id",
    "vendor_id",
    "month",
    "reviewed_invoice_id",
    "reviewed_revision",
)


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
        and in_billing_season(schedule, month)
    )


def in_billing_season(schedule, month):
    start = getattr(schedule, "season_start_month", None)
    end = getattr(schedule, "season_end_month", None)
    if start is None:
        return True
    return (
        start <= month.month <= end if start <= end else month.month >= start or month.month <= end
    )


def cost_pattern(assignment, month, patterns):
    if assignment is None:
        return None

    def matches(row):
        if assignment.id is not None:
            return row.service_vendor_assignment_id == assignment.id
        return (
            row._meta.get_field("service_vendor_assignment").is_cached(row)
            and row.service_vendor_assignment is assignment
        )

    return next(
        (row for row in patterns if row.is_active and matches(row) and contains(row, month)),
        None,
    )


def cost_is_expected(service, assignment, month, patterns):
    pattern = cost_pattern(assignment, month, patterns)
    return expected(pattern, month) if pattern else service.mode == "regular"


def tracks_vendor_receipt(service, assignment, month, patterns):
    pattern = cost_pattern(assignment, month, patterns)
    return service.mode == "regular" or bool(pattern and pattern.frequency_months > 0)


def cost_pattern_label(pattern):
    from .models import VendorBillingSchedule

    label = VendorBillingSchedule.Frequency(pattern.frequency_months).label
    if pattern.frequency_months:
        label += f", first cost {pattern.first_expected_month:%Y-%m}"
    if pattern.season_start_month is not None:
        label += f", billing season {pattern.season_start_month}–{pattern.season_end_month}"
    return label


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


def validate_selected_classification(category, key, *, current=None):
    """Disabled historical choices remain valid; new choices must be enabled."""
    from .models import Subcategory

    validate_classification(category, key)
    if key and key != current and not Subcategory.objects.filter(pk=key, is_active=True).exists():
        raise ValidationError(
            {"subcategory": "Enable this subcategory before selecting it for a new record."}
        )


def proposed_configuration(replacements=(), deletions=()):
    from .models import (
        PropertyService,
        ServiceCostSchedule,
        ServiceInvoiceException,
        ServiceVendorAssignment,
        VendorBillingSchedule,
    )

    models = (
        PropertyService,
        ServiceVendorAssignment,
        VendorBillingSchedule,
        ServiceCostSchedule,
        ServiceInvoiceException,
    )
    graph = {model: list(model.objects.all()) for model in models}
    for row in [*deletions, *replacements]:
        if type(row) in graph and row.pk:
            graph[type(row)] = [old for old in graph[type(row)] if old.pk != row.pk]
    for row in replacements:
        if type(row) in graph:
            graph[type(row)].append(row)
    removed_services = {row.pk for row in deletions if isinstance(row, PropertyService)}
    removed_assignments = {row.pk for row in deletions if isinstance(row, ServiceVendorAssignment)}
    removed_assignments.update(
        row.pk
        for row in graph[ServiceVendorAssignment]
        if row.property_service_id in removed_services
    )
    graph[ServiceVendorAssignment] = [
        row
        for row in graph[ServiceVendorAssignment]
        if row.property_service_id not in removed_services
    ]
    graph[ServiceCostSchedule] = [
        row
        for row in graph[ServiceCostSchedule]
        if row.service_vendor_assignment_id not in removed_assignments
    ]
    return graph


def validate_configuration(graph):
    from .models import (
        PropertyService,
        ServiceCostSchedule,
        ServiceInvoiceException,
        ServiceVendorAssignment,
        VendorBillingSchedule,
    )

    services = graph[PropertyService]
    assignments = graph[ServiceVendorAssignment]
    schedules = graph[VendorBillingSchedule]
    exceptions = graph[ServiceInvoiceException]
    patterns = graph[ServiceCostSchedule]
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
        (
            patterns,
            lambda r: r.service_vendor_assignment_id or id(r.service_vendor_assignment),
            "service cost patterns",
        ),
    ):
        for index, row in enumerate(rows):
            validate_months(row)
            if not row.is_active:
                continue
            for other in rows[:index]:
                if other.is_active and key(row) == key(other) and overlaps(row, other):
                    raise ValidationError(
                        f"Enabled {label} must not overlap.", code="configuration_conflict"
                    )

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
    by_assignment = {service_key(row): row for row in assignments}
    for pattern in patterns:
        assignment_id = pattern.service_vendor_assignment_id
        if assignment_id is None and pattern._meta.get_field("service_vendor_assignment").is_cached(
            pattern
        ):
            assignment_id = service_key(pattern.service_vendor_assignment)
        assignment = by_assignment.get(assignment_id) or pattern.service_vendor_assignment
        service = by_service.get(scope_key(assignment)) or assignment.property_service
        if service.category != "waste":
            raise ValidationError("Cost patterns are currently available for waste services only.")
        if pattern.start_month < assignment.start_month or (
            assignment.end_month
            and (pattern.end_month is None or pattern.end_month > assignment.end_month)
        ):
            raise ValidationError("Cost pattern dates must be within the vendor assignment dates.")
        if not (
            pattern.is_active
            and assignment.is_active
            and service.is_active
            and pattern.frequency_months
        ):
            continue
        applicable = [
            row
            for row in schedules
            if row.is_active and row.vendor_id == assignment.vendor_id and overlaps(row, pattern)
        ]
        if not applicable:
            raise ValidationError(
                "Configure a shared vendor billing schedule before a periodic cost pattern."
            )
        for schedule in applicable:
            if (
                schedule.frequency_months == 0
                or pattern.frequency_months % schedule.frequency_months != 0
                or (
                    month_number(pattern.first_expected_month)
                    - month_number(schedule.first_expected_month)
                )
                % schedule.frequency_months
            ):
                raise ValidationError(
                    "The cost pattern must align with the shared vendor invoice months."
                )
    saved_contexts = {
        row[0]: row[1:]
        for row in ServiceInvoiceException.objects.values_list("pk", *EXCEPTION_CONTEXT_FIELDS)
    }
    for exception in exceptions:
        validate_months(exception)
        if not exception.reason.strip():
            raise ValidationError("Explain why no invoice was issued.")
        context = tuple(getattr(exception, field) for field in EXCEPTION_CONTEXT_FIELDS)
        # Retain saved scopes as history when schedules, services or invoices change.
        # New or retargeted annotations must still match the current configuration.
        if exception.pk is not None and saved_contexts.get(exception.pk) == context:
            continue
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
            and contains(row, exception.month)
            and (scope is None or service_key(row) == scope)
        ]
        if not applicable or not any(
            assignment.is_active
            and assignment.vendor_id == exception.vendor_id
            and contains(assignment, exception.month)
            and any(
                service_key(service) == scope_key(assignment)
                and (
                    cost_is_expected(service, assignment, exception.month, patterns)
                    if scope is not None
                    else tracks_vendor_receipt(service, assignment, exception.month, patterns)
                )
                for service in relevant
            )
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
