"""Shared header parsing, credential verification and profile restrictions."""

import hashlib
import re

from django.utils import timezone
from ninja.security import HttpBearer

from bills.models import ApiToken

from .errors import ApiError

PREFIX = "/api/v1"
REPORTING_PATHS = frozenset(
    {
        "/me",
        "/reports/selectors",
        "/reports/summary",
        "/reports/costs",
        "/reports/consumption",
        "/reports/consumption-issues",
    }
)


def verify_credential(request, *, check_profile=True):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    match = re.fullmatch(r"(?i:Bearer) ([A-Za-z0-9_-]{43})", header)
    if match is None:
        raise ApiError(401, "unauthenticated", "A valid bearer credential is required.")
    digest = hashlib.sha256(match[1].encode("ascii")).hexdigest()
    token = ApiToken.objects.select_related("owner").filter(digest=digest).first()
    if (
        token is None
        or not token.owner.is_active
        or (token.expires_at is not None and token.expires_at <= timezone.now())
    ):
        raise ApiError(401, "unauthenticated", "A valid bearer credential is required.")
    path = request.path.removeprefix(PREFIX)
    if (
        check_profile
        and token.profile == ApiToken.Profile.REPORTING
        and (request.method != "GET" or path not in REPORTING_PATHS)
    ):
        raise ApiError(403, "forbidden", "This credential permits approved reporting only.")
    request.user, request.auth = token.owner, token
    return token


class BearerAuth(HttpBearer):
    def __call__(self, request):
        # Middleware has already parsed and verified the header before reading bodies.
        return self.authenticate(request, None)

    def authenticate(self, request, token):
        credential = getattr(request, "auth", None)
        return credential if isinstance(credential, ApiToken) else None


def require_model(request, model, action):
    if not request.user.is_staff or not request.user.has_perm(
        f"{model._meta.app_label}.{action}_{model._meta.model_name}"
    ):
        raise ApiError(403, "forbidden", "Insufficient configuration permissions.")
