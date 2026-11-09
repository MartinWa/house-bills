import sqlite3

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import DatabaseError
from django.db.models import Prefetch, Q
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from filelock import Timeout

from .deleting import StaleDeletionError, deletion_token
from .deleting import delete_document as remove_document
from .forms import (
    DeleteDocumentForm,
    DocumentFilterForm,
    InvoiceFilterForm,
    ReparseForm,
    ReportFilterForm,
    UploadForm,
)
from .invoice_folders import invoice_folders
from .models import BillingSection, Document, Invoice
from .presentation import extraction_summary, proposal_differences, review_summary, warning_details
from .processing import parse_document
from .releases import update_available
from .reporting import read_report
from .review_forms import ReviewForms
from .reviewing import ReviewContext, current_review_context
from .services import (
    StaleReviewError,
    approve_document_review,
    discard_manual_entry,
    invoice_for_review,
    invoice_identity_conflicts,
)
from .storage import ingest_pdf, open_original, retry_document


@never_cache
@require_GET
def release_status(request):
    return JsonResponse({"update_available": update_available()})


@never_cache
def home(request):
    return redirect("bills:invoices")


@never_cache
@require_http_methods(["GET", "POST"])
def upload(request):
    if request.method == "GET":
        return redirect(f"{reverse('bills:documents')}#upload")
    form = UploadForm(request.POST, request.FILES)
    status = 400
    if form.is_valid():
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
    return _documents_response(request, upload_form=form, status=status)


@never_cache
@require_POST
def manual_invoice(request):
    """Create a reviewable invoice with no original PDF, for older paper or ledger data."""
    document = Document.objects.create(
        source=Document.Source.MANUAL,
        state=Document.State.REVIEW,
        processing_message="Manual entry; no original PDF.",
    )
    messages.info(request, "Enter the invoice values from the paper or ledger.")
    return redirect("bills:review", pk=document.pk)


@never_cache
@require_POST
def cancel_manual_entry(request, pk):
    try:
        discard_manual_entry(pk)
    except Document.DoesNotExist:
        raise Http404("Manual entry not found.") from None
    except ValidationError as exc:
        messages.error(request, exc.messages[0])
        return redirect("bills:document", pk=pk)
    except OSError, DatabaseError, Timeout:
        messages.error(request, "Deletion is unavailable. The entry was retained; try again.")
        return redirect("bills:document", pk=pk)
    messages.success(request, "Unfinished manual entry deleted.")
    return redirect("bills:documents")


@never_cache
@require_http_methods(["GET", "POST"])
def delete_document(request, pk):
    source = get_object_or_404(Document, pk=pk)
    form = DeleteDocumentForm(
        request.POST if request.method == "POST" else None,
        initial={"token": deletion_token(source)},
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                cleaned_up = remove_document(pk, token=form.cleaned_data["token"])
            except Document.DoesNotExist:
                raise Http404("Document not found.") from None
            except StaleDeletionError as exc:
                form.add_error(None, exc)
                status = 409
            except ValidationError as exc:
                form.add_error(None, exc)
                status = 400
            except OSError, DatabaseError, Timeout:
                form.add_error(
                    None, "Deletion is unavailable. The document was retained; try again."
                )
                status = 503
            else:
                if cleaned_up:
                    messages.success(request, "Document deleted.")
                else:
                    messages.warning(
                        request,
                        "Document deleted. PDF cleanup is pending; uploading again will retry it.",
                    )
                return redirect("bills:documents")
        else:
            status = 400
    return render(
        request,
        "bills/delete_document.html",
        {
            "document": source,
            "form": form,
            "invoices": source.invoices.select_related("vendor").order_by("pk"),
            "stale": status == 409,
        },
        status=status,
    )


@never_cache
@require_GET
def documents(request):
    return _documents_response(request)


def _documents_response(request, *, upload_form=None, status=200):
    query = request.GET.copy()
    query.setdefault("processing", DocumentFilterForm.ATTENTION)
    form = DocumentFilterForm(query)
    records = Document.objects.order_by("-created_at", "-pk")
    if form.is_valid():
        processing = form.cleaned_data["processing"]
        if processing == DocumentFilterForm.ATTENTION:
            records = records.filter(
                ~Q(state=Document.State.READY)
                | Q(state=Document.State.READY, storage_state=Document.StorageState.INBOX)
                | Q(
                    storage_state__in=[
                        Document.StorageState.PENDING,
                        Document.StorageState.PUBLISHING,
                        Document.StorageState.FAILED,
                    ]
                )
                | ~Q(storage_error="")
                | ~Q(staging_path="")
                | ~Q(publication_path="")
            )
        elif processing:
            records = records.filter(state=processing)
    else:
        records = records.none()
        status = max(status, 400)
    page = Paginator(records, 50).get_page(request.GET.get("page"))
    for key in ("page", "upload", "storage"):
        query.pop(key, None)
    return render(
        request,
        "bills/documents.html",
        {
            "page": page,
            "form": form,
            "attention_only": form.is_valid()
            and form.cleaned_data["processing"] == DocumentFilterForm.ATTENTION,
            "filter_query": query.urlencode(),
            "upload_form": upload_form if upload_form is not None else UploadForm(),
            "limit": settings.MAX_PDF_UPLOAD_BYTES,
        },
        status=status,
    )


@never_cache
@require_GET
def document(request, pk):
    source = get_object_or_404(Document, pk=pk)
    attempt = source.extractions.order_by("-revision").first()
    conflicts = {
        other.document_id
        for invoice in source.invoices.all()
        for other in invoice_identity_conflicts(invoice)
    }
    if attempt:
        routing = attempt.candidate.get("routing", {})
        if routing.get("vendor_id") and attempt.candidate.get("reference"):
            conflicts.update(
                Invoice.objects.filter(
                    vendor_id=routing["vendor_id"],
                    reference=attempt.candidate["reference"],
                    kind=routing.get("kind", "invoice"),
                )
                .exclude(document_id=source.pk)
                .values_list("document_id", flat=True)
            )
    invoice = invoice_for_review(pk)
    review = (
        invoice.reviews.get(revision=invoice.review_revision)
        if invoice and invoice.status == Invoice.Status.APPROVED
        else None
    )
    return render(
        request,
        "bills/document.html",
        {
            "document": source,
            "can_discard_manual_entry": source.can_discard_manual_entry,
            "approved": review_summary(review),
            "proposal": extraction_summary(attempt),
            "proposal_reviewed": bool(
                attempt and review and attempt.created_at <= review.created_at
            ),
            "invoice": invoice,
            "differences": proposal_differences(invoice, attempt, pending_only=True),
            "reparse_form": ReparseForm(
                initial={"extraction_revision": source.extraction_revision}
            ),
            "conflicts": Document.objects.filter(pk__in=conflicts).order_by("pk"),
        },
    )


@never_cache
@require_GET
def invoices(request):
    invoice_view = "folders" if request.GET.get("view") == "folders" else "list"
    form = InvoiceFilterForm(request.GET)
    records = (
        Invoice.objects.filter(status=Invoice.Status.APPROVED)
        .select_related("vendor", "document")
        .prefetch_related(
            Prefetch(
                "sections",
                queryset=BillingSection.objects.select_related("property").order_by(
                    "property__name", "property_id", "service", "start", "pk"
                ),
            )
        )
        .order_by("-invoice_date", "-pk")
    )
    if form.is_valid():
        allocations = {}
        if form.cleaned_data["property"]:
            allocations["sections__property"] = form.cleaned_data["property"]
        if form.cleaned_data["service"]:
            allocations["sections__service"] = form.cleaned_data["service"]
        if allocations:
            records = records.filter(**allocations).distinct()
        if form.cleaned_data["date_from"]:
            records = records.filter(invoice_date__gte=form.cleaned_data["date_from"])
        if form.cleaned_data["date_to"]:
            records = records.filter(invoice_date__lte=form.cleaned_data["date_to"])
    else:
        records = records.none()
    query = request.GET.copy()
    query.pop("page", None)
    query["view"] = "list"
    list_query = query.urlencode()
    query["view"] = "folders"
    folders_query = query.urlencode()
    query["view"] = invoice_view
    context = {
        "form": form,
        "invoice_view": invoice_view,
        "list_query": list_query,
        "folders_query": folders_query,
        "filter_query": query.urlencode(),
        "clear_view_url": f"{reverse('bills:invoices')}?view={invoice_view}",
    }
    if invoice_view == "folders":
        context.update(invoice_folders(records))
    else:
        context["page"] = Paginator(records, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "bills/invoices.html",
        context,
        status=400 if form.errors else 200,
    )


@never_cache
@require_GET
def reports(request):
    form = ReportFilterForm(request.GET)
    report = {
        "costs": [],
        "consumption": [],
        "breakdown": [],
        "issues": [],
        "total_invoices": 0,
        "pdf_invoices": 0,
        "manual_invoices": 0,
        "manual_document_ids": [],
    }
    unavailable, status = False, 200
    if form.is_valid():
        property = form.cleaned_data["property"]
        try:
            report = read_report(
                property_id=property.pk if property else None,
                date_from=form.cleaned_data["date_from"],
                date_to=form.cleaned_data["date_to"],
            )
        except sqlite3.DatabaseError:
            unavailable, status = True, 503
    else:
        status = 400
    return render(
        request,
        "bills/reports.html",
        {"form": form, "unavailable": unavailable, **report},
        status=status,
    )


@never_cache
@require_GET
def invoice(request, pk):
    record = get_object_or_404(
        Invoice.objects.select_related("vendor", "document", "approved_by").prefetch_related(
            "sections__property", "sections__usages"
        ),
        pk=pk,
    )
    if record.status != Invoice.Status.APPROVED:
        return redirect("bills:document", pk=record.document_id)
    return render(
        request,
        "bills/invoice.html",
        {
            "invoice": record,
            "history": record.reviews.select_related("actor").order_by("-revision"),
        },
    )


def _add_form_error(form, error):
    if hasattr(error, "message_dict"):
        for field, errors in error.message_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    else:
        form.add_error(None, error)


def _added_row(data, group):
    limits = {"sections": 20, "usages": 60, "identifiers": 60}
    if group not in limits:
        raise ValidationError("Choose a valid row to add.")
    try:
        index = int(data[f"{group}-TOTAL_FORMS"])
        if not 0 <= index < limits[group]:
            raise ValueError
    except KeyError, ValueError:
        raise ValidationError("The row limit has been reached; check the existing rows.") from None
    data = data.copy()
    data[f"{group}-TOTAL_FORMS"] = index + 1
    data[f"{group}-{index}-page"] = 1
    if group == "sections":
        data[f"{group}-{index}-section_id"] = f"manual-added-{index}"
    elif group == "identifiers":
        data[f"{group}-{index}-scope"] = "invoice"
    else:
        data[f"{group}-{index}-section"] = data.get("sections-0-section_id", "")
    return data


@never_cache
@require_http_methods(["GET", "POST"])
def review(request, pk):
    source = get_object_or_404(Document, pk=pk)
    context = current_review_context(source, proposal=request.GET.get("proposal") == "latest")
    data, error, status = None, None, 200
    if request.method == "POST":
        data = request.POST
        try:
            context = ReviewContext.from_token(data.get("review_token", ""), pk)
            context.source()
            if data.get("add_row"):
                data = _added_row(data, data["add_row"])
        except ValidationError as exc:
            error, status = exc, 400
            context = current_review_context(source)
    origin = context.source()
    forms = ReviewForms(
        **{key: origin[key] for key in ("values", "properties", "vendor_id", "kind", "warnings")},
        token=context.token(),
        data=data,
    )
    if error:
        _add_form_error(forms.header, error)
    elif request.method == "POST" and not data.get("add_row"):
        if forms.is_valid():
            candidate, properties = forms.candidate()
            try:
                approved = approve_document_review(
                    pk,
                    expected_invoice_id=context.invoice_id,
                    expected_revision=context.review_revision,
                    expected_extraction_revision=context.extraction_revision,
                    candidate=candidate,
                    vendor=forms.header.cleaned_data["vendor"],
                    section_properties=properties,
                    actor=request.user,
                    extraction=origin["extraction"],
                    kind=forms.header.cleaned_data["kind"],
                    note=forms.header.cleaned_data["note"],
                    amount_due_confirmed=forms.header.cleaned_data["amount_due_confirmed"],
                )
                messages.success(
                    request,
                    "Invoice approved." if context.review_revision == 0 else "Correction saved.",
                )
                return redirect("bills:invoice", pk=approved.pk)
            except Document.DoesNotExist:
                raise Http404("Document not found.") from None
            except StaleReviewError as exc:
                _add_form_error(forms.header, exc)
                status = 409
            except ValidationError as exc:
                _add_form_error(forms.header, exc)
                status = 400
            except OSError, DatabaseError, Timeout:
                forms.header.add_error(
                    None,
                    "Storage or database is unavailable. Your entries are retained; retry saving.",
                )
                status = 503
        else:
            status = 400
    current_invoice = invoice_for_review(pk)
    attempt = origin["extraction"] if context.review_id is None else None
    latest_attempt = source.extractions.order_by("-revision").first()
    pending_differences = proposal_differences(current_invoice, latest_attempt, pending_only=True)
    return render(
        request,
        "bills/review.html",
        {
            "document": source,
            "can_discard_manual_entry": source.can_discard_manual_entry,
            "invoice": current_invoice,
            "review": forms,
            "warnings": origin["warnings"],
            "warning_details": warning_details(
                origin["warnings"], origin["values"].get("sections", []), origin["properties"]
            ),
            "from_proposal": context.review_id is None,
            "differences": (
                proposal_differences(current_invoice, attempt)
                if context.review_id is None
                else pending_differences
            ),
            "has_latest_changes": bool(pending_differences),
            "show_errors": request.method == "POST" and (status != 200 or not data.get("add_row")),
            "stale": status == 409,
        },
        status=status,
    )


@never_cache
@require_POST
def reparse(request, pk):
    source = get_object_or_404(Document, pk=pk)
    if source.source == Document.Source.MANUAL:
        messages.error(request, "Manual entries have no original PDF to re-parse.")
        return redirect("bills:document", pk=pk)
    form = ReparseForm(request.POST)
    if not form.is_valid():
        messages.error(request, "The re-parsing form is invalid; reload the document.")
        return redirect("bills:document", pk=pk)
    try:
        attempt = parse_document(
            pk, force=True, expected_revision=form.cleaned_data["extraction_revision"]
        )
        if attempt is None:
            messages.error(request, "The original is unavailable. Retry storage before re-parsing.")
            return redirect("bills:document", pk=pk)
    except StaleReviewError as exc:
        messages.error(request, exc.messages[0])
        return redirect("bills:document", pk=pk)
    except OSError, DatabaseError, Timeout, ValidationError:
        messages.error(request, "Re-parsing is unavailable. Check storage and retry.")
        return redirect("bills:document", pk=pk)
    messages.info(request, "New extraction is ready for review. Approved values are retained.")
    return redirect("bills:document", pk=pk)


@never_cache
@require_POST
def retry_storage(request, pk):
    get_object_or_404(Document, pk=pk)
    try:
        retry_document(pk)
    except Document.DoesNotExist:
        raise Http404("Document not found.") from None
    except OSError, DatabaseError, Timeout, ValidationError:
        messages.error(request, "Storage is unavailable. Check private storage and retry.")
    return redirect("bills:document", pk=pk)


@never_cache
@require_GET
def source(request, pk, download=True):
    document = get_object_or_404(Document, pk=pk)
    if not document.original_path:
        raise Http404("This document has no original PDF.")
    try:
        handle, filename = open_original(pk)
    except OSError, ValidationError, Document.DoesNotExist:
        raise Http404("Original unavailable; retry document storage.") from None
    except Timeout:
        return render(request, "bills/storage_busy.html", status=503)
    response = FileResponse(
        handle, as_attachment=download, filename=filename, content_type="application/pdf"
    )
    response["Cache-Control"] = "private, no-store"
    return response
