from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from filelock import Timeout

from .forms import UploadForm
from .models import Document
from .presentation import extraction_summary
from .services import invoice_identity_conflicts
from .storage import ingest_pdf, open_original, retry_document


@never_cache
def home(request):
    return render(request, "bills/home.html")


@never_cache
@require_http_methods(["GET", "POST"])
def upload(request):
    form = UploadForm(request.POST or None, request.FILES or None)
    status = 200
    if request.method == "POST" and form.is_valid():
        file = form.cleaned_data["pdf"]
        try:
            document, duplicate = ingest_pdf(file, filename=file.name)
            messages.info(request, "This PDF is already stored." if duplicate else "PDF uploaded.")
            return redirect("bills:document", pk=document.pk)
        except ValidationError as exc:
            form.add_error("pdf", exc)
            status = 400
        except OSError, DatabaseError, Timeout:
            form.add_error(
                None, "Storage is unavailable. Try again; completed uploads can be recovered."
            )
            status = 503
    return render(
        request,
        "bills/upload.html",
        {"form": form, "limit": settings.MAX_PDF_UPLOAD_BYTES},
        status=status,
    )


@never_cache
@require_GET
def documents(request):
    page = Paginator(Document.objects.order_by("-created_at", "-pk"), 50).get_page(
        request.GET.get("page")
    )
    return render(request, "bills/documents.html", {"page": page})


@never_cache
@require_GET
def document(request, pk):
    source = get_object_or_404(Document, pk=pk)
    conflicts = {
        other.document_id
        for invoice in source.invoices.all()
        for other in invoice_identity_conflicts(invoice)
    }
    return render(
        request,
        "bills/document.html",
        {
            "document": source,
            "proposal": extraction_summary(source.extractions.order_by("-revision").first()),
            "conflicts": Document.objects.filter(pk__in=conflicts).order_by("pk"),
        },
    )


@never_cache
@require_POST
def retry_storage(request, pk):
    get_object_or_404(Document, pk=pk)
    try:
        retry_document(pk)
    except OSError, DatabaseError, Timeout, ValidationError:
        messages.error(request, "Storage is unavailable. Check private storage and retry.")
    return redirect("bills:document", pk=pk)


@never_cache
@require_GET
def source(request, pk):
    get_object_or_404(Document, pk=pk)
    try:
        handle, filename = open_original(pk)
    except OSError, ValidationError:
        raise Http404("Original unavailable; retry document storage.") from None
    except Timeout:
        return render(request, "bills/storage_busy.html", status=503)
    response = FileResponse(
        handle, as_attachment=True, filename=filename, content_type="application/pdf"
    )
    response["Cache-Control"] = "private, no-store"
    return response
