"""Bound multipart intake before CSRF accesses request.POST."""

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.core.files.uploadhandler import FileUploadHandler, StopUpload
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
        if request.resolver_match.view_name != "bills:upload" or request.method != "POST":
            return None
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        length = request.META.get("CONTENT_LENGTH")
        try:
            oversized = length and int(length) > settings.MAX_UPLOAD_REQUEST_BYTES
        except ValueError:
            return HttpResponse("Invalid request length.", status=400)
        if oversized:
            return HttpResponse("The upload exceeds the request size limit.", status=413)
        request.upload_handlers.insert(0, BoundedPdfUploadHandler(request))
        return None

    def process_response(self, request, response):
        if getattr(request, "upload_error", ""):
            return HttpResponse(request.upload_error, status=413)
        return response
