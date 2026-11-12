"""Bound bodies before decoding and preserve HTML login/CSRF behavior."""

import io
import sqlite3

from django.contrib.auth.middleware import LoginRequiredMiddleware
from django.core.exceptions import RequestDataTooBig
from django.db import OperationalError
from django.utils.deprecation import MiddlewareMixin

from .auth import PREFIX, verify_credential
from .errors import ApiError, database_error_response, error_response

JSON_LIMIT = 512 * 1024


class ApiLoginRequiredMiddleware(LoginRequiredMiddleware):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if request.path.startswith(f"{PREFIX}/"):
            return None
        return super().process_view(request, view_func, view_args, view_kwargs)


class BoundedStream:
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def _read(self, method, size):
        size = min(size, self.remaining + 1) if size >= 0 else self.remaining + 1
        data = getattr(self.stream, method)(size)
        self.remaining -= len(data)
        if self.remaining < 0:
            raise RequestDataTooBig
        return data

    def read(self, size=-1):
        return self._read("read", size)

    def readline(self, size=-1):
        return self._read("readline", size)

    def close(self):
        self.stream.close()


class ApiContractMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        if not request.path.startswith(f"{PREFIX}/"):
            return None
        if request.path in (f"{PREFIX}/docs", f"{PREFIX}/openapi.json"):
            return None
        try:
            verify_credential(request)
            length = request.META.get("CONTENT_LENGTH", "")
            try:
                declared = int(length) if length else 0
                if declared < 0:
                    raise ValueError
            except ValueError:
                raise ApiError(400, "malformed_request", "Invalid request length.") from None
            if request.method in ("POST", "PUT", "PATCH"):
                upload = request.path == f"{PREFIX}/documents" and request.method == "POST"
                media = request.content_type
                if upload:
                    if media != "multipart/form-data" or not request.content_params.get("boundary"):
                        raise ApiError(415, "unsupported_media_type", "Use multipart/form-data.")
                else:
                    if declared > JSON_LIMIT:
                        raise RequestDataTooBig
                    body = request.read(JSON_LIMIT + 1)
                    if len(body) > JSON_LIMIT:
                        raise RequestDataTooBig
                    if body and media != "application/json":
                        raise ApiError(415, "unsupported_media_type", "Use application/json.")
                    request._body, request._stream = body, io.BytesIO(body)
        except ApiError as exc:
            return error_response(exc)
        except RequestDataTooBig:
            return error_response(ApiError(413, "body_too_large", "The request body is too large."))
        except (OperationalError, sqlite3.OperationalError) as exc:
            return database_error_response(exc)
        return None

    def process_response(self, request, response):
        if not request.path.startswith(f"{PREFIX}/"):
            return response
        if response.status_code in (400, 401, 403, 404, 405, 413, 415, 500) and (
            not response.get("Content-Type", "").startswith("application/json")
        ):
            status = response.status_code
            code = {
                400: "malformed_request",
                401: "unauthenticated",
                403: "forbidden",
                404: "not_found",
                405: "method_not_allowed",
                413: "body_too_large",
                415: "unsupported_media_type",
                500: "internal_error",
            }[status]
            headers = {"Allow": response["Allow"]} if response.has_header("Allow") else {}
            response = error_response(
                ApiError(status, code, "Request could not be completed.", headers=headers)
            )
        if response.status_code == 401:
            response["WWW-Authenticate"] = "Bearer"
        if "no-store" not in response.get("Cache-Control", ""):
            response["Cache-Control"] = "no-store"
        return response
