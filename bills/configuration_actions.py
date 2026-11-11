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
from .models import ServiceVendorAssignment, VendorBillingSchedule


@transaction.atomic
def change_vendor(assignment_id, *, month, vendor, transition_month=False):
    old = ServiceVendorAssignment.objects.get(pk=assignment_id)
    if not old.is_active or not contains(old, month) or vendor.pk == old.vendor_id:
        raise ValidationError(
            "Choose a different vendor and a month within the enabled assignment."
        )
    replacement = ServiceVendorAssignment(
        property_service=old.property_service,
        vendor=vendor,
        start_month=month,
        end_month=old.end_month,
    )
    old.end_month = month if transition_month else add_months(month, -1)
    validate_configuration(proposed_configuration([old, replacement]))
    with staged_configuration():
        old.save()
        replacement.save()
    return replacement


@transaction.atomic
def change_billing_frequency(
    schedule_id, *, month, frequency_months, first_expected_month, grace_days
):
    old = VendorBillingSchedule.objects.get(pk=schedule_id)
    if not old.is_active or month <= old.start_month or not contains(old, month):
        raise ValidationError("Choose a later effective month within the enabled schedule.")
    replacement = VendorBillingSchedule(
        vendor=old.vendor,
        start_month=month,
        end_month=old.end_month,
        frequency_months=frequency_months,
        first_expected_month=first_expected_month,
        grace_days=grace_days,
    )
    old.end_month = add_months(month, -1)
    validate_configuration(proposed_configuration([old, replacement]))
    with staged_configuration():
        old.save()
        replacement.save()
    return replacement
