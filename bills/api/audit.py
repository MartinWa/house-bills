"""API changes share the admin audit trail and the mutation transaction."""

from django.contrib.admin.models import LogEntry


def log_change(request, obj, flag, message):
    LogEntry.objects.log_actions(
        user_id=request.user.pk,
        queryset=[obj],
        action_flag=flag,
        change_message=message,
        single_object=True,
    )
