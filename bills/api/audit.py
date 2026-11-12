"""API changes share the admin audit trail and the mutation transaction."""

from django.contrib.admin.models import LogEntry


def log_change(request, obj, flag, message):
    actor = {"api_token": {"id": request.auth.pk, "name": request.auth.name}}
    # Sentences must stay strings: Django interprets JSON fields as field edits
    # and ignores custom metadata when rendering the admin history.
    message = (
        [*message, actor]
        if isinstance(message, list)
        else f"{message} (token {request.auth.name!r}, id {request.auth.pk})"
    )
    LogEntry.objects.log_actions(
        user_id=request.user.pk,
        queryset=[obj],
        action_flag=flag,
        change_message=message,
        single_object=True,
    )
