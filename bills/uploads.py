"""Bound multipart intake before CSRF accesses request.POST."""

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
from django.db import OperationalError
from django.http import HttpResponse
from django.utils.deprecation import MiddlewareMixin


class BoundedPdfUploadHandler(FileUploadHandler):
    def __init__(self, request):
        super().__init__(request)
        self.bytes_received = 0
        self.files_received = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.files_received += 1
        if self.files_received > 1:
            self.request.upload_error = "Upload one PDF at a time."
            raise StopUpload(connection_reset=True)

    def receive_data_chunk(self, raw_data, start):
        self.bytes_received += len(raw_data)
        if self.bytes_received > settings.MAX_PDF_UPLOAD_BYTES:
            self.request.upload_error = "The PDF exceeds the upload size limit."
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None


class BoundedUploadMiddleware(MiddlewareMixin):
    def process_view(self, request, view_func, view_args, view_kwargs):
        api_upload = request.path == "/api/v1/documents" and request.method == "POST"
        if (
            request.resolver_match.view_name != "bills:upload" and not api_upload
        ) or request.method != "POST":
            return None
        if api_upload:
            from .api.auth import verify_credential
            from .api.errors import ApiError, database_error_response, error_response

            try:
                verify_credential(request)
            except ApiError as exc:
                return error_response(exc)
            except OperationalError as exc:
                return database_error_response(exc)
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        length = request.META.get("CONTENT_LENGTH")
        try:
            oversized = length and int(length) > settings.MAX_UPLOAD_REQUEST_BYTES
        except ValueError:
            return HttpResponse("Invalid request length.", status=400)
        if oversized:
            if api_upload:
                return error_response(
                    ApiError(413, "body_too_large", "The upload exceeds the request size limit.")
                )
            return HttpResponse("The upload exceeds the request size limit.", status=413)
        if api_upload:
            from .api.middleware import BoundedStream

            request._stream = BoundedStream(request._stream, settings.MAX_UPLOAD_REQUEST_BYTES)
        request.upload_handlers.insert(0, BoundedPdfUploadHandler(request))
        return None

    def process_response(self, request, response):
        if getattr(request, "upload_error", ""):
            if request.path.startswith("/api/v1/"):
                from .api.errors import ApiError, error_response

                return error_response(
                    ApiError(413, "body_too_large", "Upload one PDF within the size limit.")
                )
            return HttpResponse(request.upload_error, status=413)
        return response
