"Dated changes preserve service identity, assignment history and schedule history."

from django.core.exceptions import ValidationError
from django.db import transaction

from .configuration import (
    add_months,
    contains,
    proposed_configuration,
    staged_configuration,
    validate_configuration,
)
from .models import ServiceCostSchedule, ServiceVendorAssignment, VendorBillingSchedule


def _save_replacement(old, replacement, related_changes=()):
    validate_configuration(proposed_configuration([old, replacement, *related_changes]))
    # Assigning replacement.predecessor overwrites old's reverse relation cache.
    # Read the saved successor so a historical insertion retains the entire chain.
    successor = type(old).objects.filter(predecessor_id=old.pk).first()
    with staged_configuration():
        if successor is not None:
            # Release the unique predecessor link until the inserted period exists.
            successor.predecessor = None
            successor.save(update_fields=["predecessor"])
        for row in related_changes:
            row.save()
        old.save()
        replacement.save()
        if successor is not None:
            successor.predecessor = replacement
            successor.save(update_fields=["predecessor"])
    return replacement


@transaction.atomic
def change_vendor(assignment_id, *, month, vendor, transition_month=False):
    old = ServiceVendorAssignment.objects.get(pk=assignment_id)
    if not old.is_active or not contains(old, month) or vendor.pk == old.vendor_id:
        raise ValidationError(
            "Choose a different vendor and a month within the enabled assignment."
        )
    replacement = ServiceVendorAssignment(
        predecessor=old,
        property_service=old.property_service,
        vendor=vendor,
        start_month=month,
        end_month=old.end_month,
    )
    old.end_month = month if transition_month else add_months(month, -1)
    patterns = list(old.cost_schedules.all())
    changed = []
    for pattern in patterns:
        if pattern.start_month > old.end_month:
            raise ValidationError(
                "Reconfigure future cost patterns before ending this vendor assignment."
            )
        if pattern.end_month is None or pattern.end_month > old.end_month:
            pattern.end_month = old.end_month
            changed.append(pattern)
    return _save_replacement(old, replacement, changed)


@transaction.atomic
def change_billing_frequency(
    schedule_id, *, month, frequency_months, first_expected_month, grace_days
):
    old = VendorBillingSchedule.objects.get(pk=schedule_id)
    if not old.is_active or month <= old.start_month or not contains(old, month):
        raise ValidationError("Choose a later effective month within the enabled schedule.")
    replacement = VendorBillingSchedule(
        predecessor=old,
        vendor=old.vendor,
        start_month=month,
        end_month=old.end_month,
        frequency_months=frequency_months,
        first_expected_month=first_expected_month,
        grace_days=grace_days,
    )
    old.end_month = add_months(month, -1)
    return _save_replacement(old, replacement)


@transaction.atomic
def change_cost_pattern(
    schedule_id,
    *,
    month,
    frequency_months,
    first_expected_month=None,
    season_start_month=None,
    season_end_month=None,
):
    old = ServiceCostSchedule.objects.get(pk=schedule_id)
    if not old.is_active or month <= old.start_month or not contains(old, month):
        raise ValidationError("Choose a later effective month within the enabled cost pattern.")
    replacement = ServiceCostSchedule(
        predecessor=old,
        service_vendor_assignment=old.service_vendor_assignment,
        start_month=month,
        end_month=old.end_month,
        frequency_months=frequency_months,
        first_expected_month=first_expected_month,
        season_start_month=season_start_month,
        season_end_month=season_end_month,
    )
    old.end_month = add_months(month, -1)
    return _save_replacement(old, replacement)
