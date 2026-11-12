"""API assembly, schema access and public exception translation."""

import sqlite3
from functools import wraps

from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import (
    ObjectDoesNotExist,
    PermissionDenied,
    RequestDataTooBig,
    ValidationError,
)
from django.db import DatabaseError, IntegrityError, OperationalError
from django.db.models.deletion import ProtectedError
from django.http import Http404
from django.http.multipartparser import MultiPartParserError
from filelock import Timeout
from ninja.errors import AuthenticationError, HttpError
from ninja.errors import ValidationError as NinjaValidationError

from bills.api_reporting import ReportTooBroad
from bills.deleting import StaleDeletionError
from bills.models import (
    Category,
    InvoicePropertyRule,
    Subcategory,
    Usage,
    VendorBillingSchedule,
    VendorIssuerIdentifier,
)
from bills.review_edits import REVIEW_LIMITS
from bills.review_forms import IDENTIFIER_KINDS
from bills.services import StaleReviewError

from . import schemas as s
from .auth import BearerAuth, verify_credential
from .collections import query_values
from .configuration import router as configuration_router
from .contract import ContractAPI
from .errors import ApiError, database_error_response, error_response
from .reports import router as reports_router
from .snapshots import consistent_reads
from .workflow import router as workflow_router


def docs_access(view):
    @wraps(view)
    def protected(request, *args, **kwargs):
        if request.path.endswith("/openapi.json"):
            if request.user.is_authenticated and request.user.is_active and request.user.is_staff:
                return view(request, *args, **kwargs)
            try:
                verify_credential(request, check_profile=False)
            except ApiError as exc:
                return error_response(exc)
            return view(request, *args, **kwargs)
        return staff_member_required(view)(request, *args, **kwargs)

    return protected


api = ContractAPI(
    title="House Bills API",
    version="1.0.0",
    auth=BearerAuth(),
    urls_namespace="api-v1",
    docs_decorator=docs_access,
)
api.add_decorator(consistent_reads)


@api.exception_handler(ApiError)
def api_error(request, exc):
    return error_response(exc)


@api.exception_handler(NinjaValidationError)
def input_error(request, exc):
    details = [
        {
            "path": "/".join(str(part) for part in error["loc"]),
            "code": error["type"],
            "message": "Value does not match the published schema.",
        }
        for error in exc.errors
    ]
    return error_response(ApiError(422, "validation_error", "Request validation failed.", details))


@api.exception_handler(ValidationError)
def domain_error(request, exc):
    if isinstance(exc, (StaleReviewError, StaleDeletionError)):
        return error_response(
            ApiError(409, "stale_state", "The observed state has changed; reload before retrying.")
        )
    if isinstance(exc, ReportTooBroad):
        return error_response(
            ApiError(
                422,
                "filter_too_broad",
                "The report filters are too broad.",
                [{"path": "filters", "code": "limit_exceeded", "message": f"Limit: {exc.limit}."}],
            )
        )
    errors = getattr(
        exc, "error_dict", {"__all__": exc.error_list if hasattr(exc, "error_list") else []}
    )
    conflict = any(
        error.code in ("unique", "unique_together") for items in errors.values() for error in items
    )
    # Graph conflicts have no field/code in the existing domain validators.
    conflict = conflict or any(
        "overlap" in message.lower() or "already uses" in message.lower()
        for message in exc.messages
    )
    details = []
    for name, items in errors.items():
        for error in items:
            details.append(
                {
                    "path": name,
                    "code": error.code or "invalid_value",
                    "message": error.messages[0],
                }
            )
    return error_response(
        ApiError(
            409 if conflict else 422,
            "configuration_conflict" if conflict else "validation_error",
            "Resource validation failed.",
            details,
        )
    )


@api.exception_handler(HttpError)
def http_error(request, exc):
    codes = {400: "malformed_request", 401: "unauthenticated", 403: "forbidden", 404: "not_found"}
    return error_response(
        ApiError(
            exc.status_code,
            codes.get(exc.status_code, "request_error"),
            "Request could not be completed.",
        )
    )


@api.exception_handler(AuthenticationError)
def authentication_error(request, exc):
    return error_response(
        ApiError(401, "unauthenticated", "A valid bearer credential is required.")
    )


@api.exception_handler(ObjectDoesNotExist)
@api.exception_handler(Http404)
def not_found(request, exc):
    return error_response(ApiError(404, "not_found", "Resource not found."))


@api.exception_handler(PermissionDenied)
def forbidden(request, exc):
    return error_response(ApiError(403, "forbidden", "Insufficient access."))


@api.exception_handler(ProtectedError)
def protected_error(request, exc):
    return error_response(
        ApiError(409, "protected_reference", "Referenced records prevent deletion.")
    )


@api.exception_handler(IntegrityError)
def integrity_error(request, exc):
    return error_response(
        ApiError(
            409, "resource_conflict", "A conflicting resource or configuration already exists."
        )
    )


@api.exception_handler(RequestDataTooBig)
def oversized(request, exc):
    return error_response(ApiError(413, "body_too_large", "The request body is too large."))


@api.exception_handler(MultiPartParserError)
def malformed_multipart(request, exc):
    return error_response(ApiError(400, "malformed_request", "Invalid multipart request."))


@api.exception_handler(OSError)
@api.exception_handler(Timeout)
def storage_error(request, exc):
    return error_response(ApiError(503, "storage_unavailable", "Original storage is unavailable."))


@api.exception_handler(DatabaseError)
@api.exception_handler(sqlite3.DatabaseError)
def database_error(request, exc):
    if not isinstance(exc, (OperationalError, sqlite3.OperationalError)):
        raise exc
    return database_error_response(exc)


@api.get("/me", response=s.MeOut, tags=["Authentication"])
def me(request):
    query_values(request, [])
    return {
        "account_id": request.user.pk,
        "token_name": request.auth.name,
        "profile": request.auth.profile,
        "expires_at": request.auth.expires_at,
    }


def choices(values):
    return [{"key": key, "name": name} for key, name in values]


@api.get("/metadata", response=s.MetadataOut, tags=["Configuration"])
def metadata(request):
    query_values(request, [])
    return {
        "categories": choices(Category.choices),
        "subcategories": list(
            Subcategory.objects.order_by("key").values("key", "category", "name", "is_active")
        ),
        "vendor_identifier_kinds": choices(VendorIssuerIdentifier.Kind.choices),
        "property_rule_kinds": choices(InvoicePropertyRule.Kind.choices),
        "identifier_kinds": choices(IDENTIFIER_KINDS),
        "units": ["kWh", "m3", "kg"],
        "bases": choices(Usage.Basis.choices),
        "frequencies": choices(VendorBillingSchedule.Frequency.choices),
        "review_limits": REVIEW_LIMITS,
        "billed_period_end": "exclusive",
        "configuration_end_month": "inclusive",
    }


api.add_router("", configuration_router)
api.add_router("", workflow_router)
api.add_router("/reports", reports_router)
