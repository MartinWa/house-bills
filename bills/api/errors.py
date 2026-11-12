"""One public error envelope; never serialize exception arguments or request input."""

import sqlite3

from django.http import JsonResponse


class ApiError(Exception):
    def __init__(self, status, code, message, details=(), *, headers=None):
        self.status, self.code, self.message = status, code, message
        self.details, self.headers = list(details), headers or {}


def error_response(error):
    response = JsonResponse(
        {"error": {"code": error.code, "message": error.message, "details": error.details}},
        status=error.status,
    )
    response["Cache-Control"] = "no-store"
    if error.status == 401:
        response["WWW-Authenticate"] = "Bearer"
    for key, value in error.headers.items():
        response[key] = value
    return response


def invalid_parameter(name, message="Invalid parameter value."):
    raise ApiError(
        422,
        "invalid_parameter",
        message,
        [{"path": name, "code": "invalid_parameter", "message": message}],
    )


def database_error_response(exc):
    original = exc.__cause__ or exc
    code = getattr(original, "sqlite_errorcode", None)
    available_later = {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_PROTOCOL,
        sqlite3.SQLITE_INTERRUPT,
    }
    # SQLITE_ERROR includes invalid SQL and missing columns/views. Those require a
    # code or migration fix; retrying them cannot restore database availability.
    if code is not None and code & 0xFF not in available_later:
        raise exc
    busy = (code is not None and code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)) or (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )
    return error_response(
        ApiError(
            503,
            "database_unavailable",
            "Database access is unavailable.",
            headers={"Retry-After": "1"} if busy else {},
        )
    )
