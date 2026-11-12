"""Document workflows preserve existing domain locks, revisions and original I/O."""

from dataclasses import asdict

from django.contrib.admin.models import ADDITION, CHANGE
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import FileResponse, HttpResponse
from ninja import File, Router, Status
from ninja.files import UploadedFile

from bills.deleting import delete_document, deletion_token
from bills.invoice_coverage import mark_invoice_not_sent
from bills.manual_entries import create_manual_entry
from bills.models import (
    Document,
    Invoice,
    ReviewRevision,
    ServiceInvoiceException,
    Subcategory,
    Vendor,
)
from bills.parsing.compatibility import cost_totals_candidate
from bills.presentation import proposal_differences
from bills.processing import parse_document
from bills.review_edits import (
    HEADER_MONEY,
    SECTION_MONEY,
    ReviewEdits,
    build_review,
    evidence_page,
    source_rows,
)
from bills.reviewing import ReviewContext, current_review_context
from bills.services import approve_document_review, discard_manual_entry, invoice_for_review
from bills.storage import ingest_pdf, open_original, retry_document

from . import schemas as s
from .audit import log_change
from .auth import PREFIX, require_model
from .collections import (
    filter_collection,
    integer,
    iso_date,
    page_response,
    pagination,
    query_values,
)
from .configuration import dry_run, get_record, remove_resource, representation, resource_path
from .contract import documented_query
from .errors import ApiError, invalid_parameter

router = Router(tags=["Documents and invoices"])


def document_representation(document):
    path = resource_path("documents", document.pk)
    links = {"self": path, "review": f"{path}/review", "reviews": f"{path}/reviews"}
    if document.source == Document.Source.UPLOAD:
        links.update(
            source=f"{path}/source",
            reparse=f"{path}/reparse",
            retry_storage=f"{path}/retry-storage",
        )
    elif document.can_discard_manual_entry:
        links["cancel"] = f"{path}/cancel"
    invoices = list(document.invoices.order_by("pk").values_list("id", flat=True))
    for key in document.invoices.filter(status=Invoice.Status.APPROVED).values_list(
        "pk", flat=True
    ):
        links[f"invoice_{key}"] = resource_path("invoices", key)
    return {
        "id": document.pk,
        "source": document.source,
        "original_filename": document.original_filename,
        "sha256": document.sha256 or None,
        "state": document.state,
        "storage_state": document.storage_state,
        "storage_pending": document.storage_needs_attention,
        "extraction_revision": document.extraction_revision,
        "created_at": document.created_at,
        "invoice_ids": invoices,
        "deletion_token": deletion_token(document),
        "links": links,
    }


@router.get("/documents", response=s.Page[s.DocumentOut])
@documented_query(
    "state",
    "storage_state",
    "source",
    "created_from",
    "created_to",
    "q",
    "sort",
    "limit",
    "offset",
    sortable=["created_at", "id"],
)
def documents(request):
    queryset, (limit, offset) = filter_collection(
        Document.objects.all(),
        request,
        {
            "state": "state",
            "storage_state": "storage_state",
            "source": "source",
            "created_from": "created_at__gte",
            "created_to": "created_at__lte",
            "q": "original_filename__icontains",
        },
        ("-created_at", "id"),
        {"created_at": "created_at", "id": "id"},
        choices={
            "state": Document.State.values,
            "storage_state": Document.StorageState.values,
            "source": Document.Source.values,
        },
    )
    return page_response(
        request,
        [document_representation(obj) for obj in queryset[offset : offset + limit]],
        queryset.count(),
        limit,
        offset,
    )


@router.post("/documents", response={200: s.DocumentResult, 201: s.DocumentResult})
def upload(request, response: HttpResponse, pdf: File[UploadedFile]):
    query_values(request, [])
    if set(request.POST) or set(request.FILES) != {"pdf"}:
        raise ApiError(422, "validation_error", "Submit exactly one pdf file.")
    document, duplicate = ingest_pdf(pdf, filename=pdf.name)
    if not duplicate:
        response["Location"] = resource_path("documents", document.pk)
    return Status(
        200 if duplicate else 201,
        {
            "document": document_representation(document),
            "duplicate": duplicate,
        },
    )


@router.get("/documents/{key}", response=s.DocumentOut)
def document(request, key: int):
    query_values(request, [])
    return document_representation(get_record(Document, key))


@router.delete("/documents/{key}", response=s.DeletionOut)
def remove_document(request, key: int):
    query_values(request, [])
    get_record(Document, key)
    token = request.headers.get("Deletion-Token", "")
    try:
        cleaned = delete_document(key, token=token)
    except ValidationError as exc:
        from bills.deleting import StaleDeletionError

        if isinstance(exc, StaleDeletionError):
            raise
        raise ApiError(400, "invalid_token", "Invalid deletion token.") from None
    return {"document_id": key, "deleted": True, "cleanup_pending": not cleaned}


@router.post("/manual-invoices", response={200: s.DocumentResult, 201: s.DocumentResult})
def manual_invoice(request, response: HttpResponse, payload: s.ManualIn):
    query_values(request, [])
    if payload.idempotency_key is not None and not payload.idempotency_key.isprintable():
        raise ApiError(
            422, "validation_error", "The idempotency key must contain printable characters."
        )
    document, duplicate = create_manual_entry(idempotency_key=payload.idempotency_key)
    if not duplicate:
        response["Location"] = resource_path("documents", document.pk)
    return Status(
        200 if duplicate else 201,
        {
            "document": document_representation(document),
            "duplicate": duplicate,
        },
    )


@router.post("/documents/{key}/cancel", response=s.DeletionOut)
def cancel(request, key: int):
    query_values(request, [])
    get_record(Document, key)
    discard_manual_entry(key)
    return {"document_id": key, "deleted": True, "cleanup_pending": False}


def editable_values(origin):
    values = cost_totals_candidate(origin["values"])
    raw_usages, raw_identifiers = source_rows(values)
    header = {
        field: values.get(field)
        for field in ("reference", "invoice_date", "due_date", "currency", *HEADER_MONEY)
    }
    header.update(
        vendor_id=origin["vendor_id"],
        kind=origin["kind"],
        source_page=evidence_page(next(iter(values.get("header_evidence", {}).values()), {})),
    )
    sections = [
        {
            **{field: row.get(field) for field in ("section_id", "category", *SECTION_MONEY)},
            "subcategory": row.get("subcategory") or None,
            "property_id": origin["properties"].get(row["section_id"]),
            "period_start": row.get("start"),
            "period_end": row.get("end"),
            "source_page": evidence_page(row.get("total_evidence")),
        }
        for row in values.get("sections", [])
    ]
    usages = [
        {
            **{
                field: row.get(field)
                for field in ("section_id", "utility", "unit", "quantity_milli", "basis")
            },
            "subcategory": row.get("subcategory") or None,
            "period_start": row.get("start"),
            "period_end": row.get("end"),
            "source_page": evidence_page(row.get("evidence")),
            "source_index": index,
        }
        for index, row in enumerate(raw_usages)
    ]
    identifiers = [
        {
            **{field: row.get(field) for field in ("scope", "kind", "value")},
            "source_page": evidence_page(row.get("evidence")),
            "source_index": index,
        }
        for index, row in enumerate(raw_identifiers)
    ]
    return {
        "header": header,
        "sections": sections,
        "usages": usages,
        "identifiers": identifiers,
        "amount_due_confirmed": False,
        "warnings_confirmed": False,
        "note": "",
    }


@router.get("/documents/{key}/review", response=s.ReviewSourceOut)
@documented_query("proposal")
def review_source(request, key: int):
    params = query_values(request, ["proposal"])
    if "proposal" in params and params["proposal"] != "latest":
        invalid_parameter("proposal")
    document = get_record(Document, key)
    context = current_review_context(document, proposal=params.get("proposal") == "latest")
    origin = context.source()
    return {
        "review_token": context.token(),
        "context": asdict(context),
        "values": origin["values"],
        "edits": editable_values(origin),
        "warnings": origin["warnings"],
        "differences": proposal_differences(invoice_for_review(key), origin["extraction"]),
        "links": {
            "self": f"{PREFIX}/documents/{key}/review",
            "owner": resource_path("documents", key),
            "reviews": f"{PREFIX}/documents/{key}/reviews",
        },
    }


def saved_review_representation(review):
    document = review.invoice.document
    return {
        "invoice_id": review.invoice_id,
        "document_id": document.pk,
        "revision": review.revision,
        "actor_id": review.actor_id,
        "actor_name": review.actor.get_username(),
        "created_at": review.created_at,
        "note": review.note,
        "amount_due_confirmed": review.amount_due_confirmed,
        "extraction_id": review.extraction_id,
        "snapshot": review.snapshot,
        "storage_state": document.storage_state,
        "storage_pending": document.storage_needs_attention,
        "links": {
            "self": f"{PREFIX}/invoices/{review.invoice_id}/reviews/{review.revision}",
            "owner": resource_path("invoices", review.invoice_id),
            "document": resource_path("documents", document.pk),
            "collection": f"{PREFIX}/invoices/{review.invoice_id}/reviews",
        },
    }


@router.post("/documents/{key}/reviews", response={201: s.SavedReviewOut})
def approve(request, response: HttpResponse, key: int, payload: s.ReviewIn):
    query_values(request, [])
    get_record(Document, key)
    try:
        context = ReviewContext.from_token(payload.review_token, key)
        origin = context.source()
    except ValidationError:
        raise ApiError(400, "invalid_token", "Invalid or unavailable review context.") from None
    edits = ReviewEdits(**payload.edits.model_dump())
    candidate, properties = build_review(
        edits, values=origin["values"], warnings=origin["warnings"]
    )
    invoice = approve_document_review(
        key,
        expected_invoice_id=context.invoice_id,
        expected_revision=context.review_revision,
        expected_extraction_revision=context.extraction_revision,
        candidate=candidate,
        vendor=get_record(Vendor, edits.header["vendor_id"]),
        section_properties=properties,
        actor=request.user,
        extraction=origin["extraction"],
        kind=edits.header["kind"],
        note=edits.note,
        amount_due_confirmed=edits.amount_due_confirmed,
    )
    result = saved_review_representation(
        invoice.reviews.select_related("invoice__document", "actor").get(
            revision=invoice.review_revision
        )
    )
    response["Location"] = result["links"]["self"]
    return Status(201, result)


@router.post("/documents/{key}/reparse", response=s.DocumentOut)
def reparse(request, key: int, payload: s.ReparseIn):
    query_values(request, [])
    source = get_record(Document, key)
    if source.source != Document.Source.UPLOAD:
        raise ApiError(409, "no_original", "Manual entries have no PDF to reparse.")
    attempt = parse_document(key, force=True, expected_revision=payload.extraction_revision)
    if attempt is None:
        raise ApiError(503, "storage_unavailable", "Retry original storage before reparsing.")
    return document_representation(get_record(Document, key))


@router.post("/documents/{key}/retry-storage", response=s.DocumentOut)
def retry_storage(request, key: int):
    query_values(request, [])
    get_record(Document, key)
    retry_document(key)
    return document_representation(get_record(Document, key))


@router.get("/documents/{key}/source", response=None)
def original(request, key: int):
    query_values(request, [])
    document = get_record(Document, key)
    if document.source == Document.Source.MANUAL:
        raise ApiError(404, "not_found", "This document has no original PDF.")
    try:
        handle, filename = open_original(key)
    except ValidationError:
        raise ApiError(503, "storage_unavailable", "Original storage is unavailable.") from None
    response = FileResponse(
        handle, as_attachment=True, filename=filename, content_type="application/pdf"
    )
    response["Cache-Control"] = "private, no-store"
    return response


def invoice_representation(invoice):
    review = invoice.reviews.get(revision=invoice.review_revision)
    return {
        "id": invoice.pk,
        "document_id": invoice.document_id,
        "vendor_id": invoice.vendor_id,
        "vendor_name": invoice.vendor.name,
        "review_revision": invoice.review_revision,
        "approved_at": invoice.approved_at,
        "values": review.snapshot["values"],
        "links": {
            "self": resource_path("invoices", invoice.pk),
            "document": resource_path("documents", invoice.document_id),
            "vendor": resource_path("vendors", invoice.vendor_id),
            "reviews": {
                "count": invoice.reviews.count(),
                "href": f"{PREFIX}/invoices/{invoice.pk}/reviews",
            },
        },
    }


@router.get("/invoices", response=s.Page[s.InvoiceOut])
@documented_query(
    "property_id",
    "vendor_id",
    "category",
    "subcategory",
    "invoice_date_from",
    "invoice_date_to",
    "period_from",
    "period_to",
    "q",
    "sort",
    "limit",
    "offset",
    repeated=["property_id", "vendor_id"],
    sortable=["invoice_date", "id"],
)
def invoices(request):
    filters = {
        "property_id": "sections__property_id__in",
        "vendor_id": "vendor_id__in",
        "category": "sections__category",
        "subcategory": "sections__subcategory_id",
        "invoice_date_from": "invoice_date__gte",
        "invoice_date_to": "invoice_date__lte",
        "period_from": "sections__end__gt",
        "period_to": "sections__start__lte",
        "q": "reference__icontains",
    }
    params = query_values(
        request, [*filters, "limit", "offset", "sort"], repeated=["property_id", "vendor_id"]
    )
    queryset = Invoice.objects.filter(status=Invoice.Status.APPROVED)
    conditions = {}
    for name, lookup in filters.items():
        if name not in params:
            continue
        value = params[name]
        if name.endswith("_id"):
            if len(value) > 100:
                invalid_parameter(name, "At most 100 IDs are accepted.")
            value = [integer(item, name) for item in value]
        elif name.endswith(("_from", "_to")):
            value = iso_date(value, name)
        elif name == "category" and value not in ("electricity", "water", "waste", "other"):
            invalid_parameter(name)
        elif name == "subcategory" and not Subcategory.objects.filter(pk=value).exists():
            invalid_parameter(name)
        conditions[lookup] = value
    queryset = queryset.filter(**conditions).distinct()
    sort = params.get("sort", "-invoice_date,id").split(",")
    if any(key.removeprefix("-") not in ("invoice_date", "id") for key in sort):
        invalid_parameter("sort")
    if not any(key.removeprefix("-") == "id" for key in sort):
        sort.append("id")
    queryset = queryset.order_by(*sort)
    limit, offset = pagination(request, params)
    return page_response(
        request,
        [invoice_representation(obj) for obj in queryset[offset : offset + limit]],
        queryset.count(),
        limit,
        offset,
    )


@router.get("/invoices/{key}", response=s.InvoiceOut)
def invoice(request, key: int):
    query_values(request, [])
    obj = get_record(Invoice, key)
    if obj.status != Invoice.Status.APPROVED:
        raise ApiError(404, "not_found", "Approved invoice not found.")
    return invoice_representation(obj)


@router.get("/invoices/{key}/reviews", response=s.Page[s.SavedReviewOut])
@documented_query("limit", "offset")
def saved_reviews(request, key: int):
    params = query_values(request, ["limit", "offset"])
    get_record(Invoice, key)
    limit, offset = pagination(request, params)
    queryset = (
        ReviewRevision.objects.filter(invoice_id=key)
        .select_related("invoice__document", "actor")
        .order_by("-revision")
    )
    return page_response(
        request,
        [saved_review_representation(obj) for obj in queryset[offset : offset + limit]],
        queryset.count(),
        limit,
        offset,
    )


@router.get("/invoices/{key}/reviews/{revision}", response=s.SavedReviewOut)
def saved_review(request, key: int, revision: int):
    query_values(request, [])
    obj = (
        ReviewRevision.objects.select_related("invoice__document", "actor")
        .filter(invoice_id=key, revision=revision)
        .first()
    )
    if obj is None:
        raise ApiError(404, "not_found", "Saved review not found.")
    return saved_review_representation(obj)


@router.get("/invoice-exceptions", response=s.Page[s.ExceptionOut])
@documented_query(
    "invoice_id",
    "service_id",
    "month_from",
    "month_to",
    "reason",
    "sort",
    "limit",
    "offset",
    sortable=["month", "id"],
)
def invoice_exceptions(request):
    require_model(request, ServiceInvoiceException, "view")
    queryset, (limit, offset) = filter_collection(
        ServiceInvoiceException.objects.all(),
        request,
        {
            "invoice_id": "reviewed_invoice_id",
            "service_id": "property_service_id",
            "month_from": "month__gte",
            "month_to": "month__lte",
            "reason": "reason__icontains",
        },
        ("-month", "id"),
        {"month": "month", "id": "id"},
    )
    return page_response(
        request,
        [representation(obj) for obj in queryset[offset : offset + limit]],
        queryset.count(),
        limit,
        offset,
    )


@router.post("/invoice-exceptions", response={200: s.ExceptionOut, 201: s.ExceptionOut})
@documented_query("dry_run")
def confirm_exception(request, response: HttpResponse, payload: s.ExceptionIn):
    preview = dry_run(request)
    require_model(request, ServiceInvoiceException, "add")
    if payload.month.day != 1:
        raise ApiError(422, "validation_error", "Use the first day of the month.")
    with transaction.atomic():
        try:
            obj, created = mark_invoice_not_sent(
                payload.service_id,
                payload.vendor_id,
                payload.month,
                reason=payload.reason,
                reviewed_invoice_id=payload.invoice_id,
                reviewed_revision=payload.review_revision,
            )
        except ValidationError:
            raise ApiError(
                409, "stale_confirmation", "The invoice coverage or observed review has changed."
            ) from None
        result = representation(obj)
        if preview:
            result["dry_run"] = True
            transaction.set_rollback(True)
        else:
            log_change(
                request, obj, ADDITION if created else CHANGE, "API invoice exception confirmation"
            )
    if created and not preview:
        response["Location"] = result["links"]["self"]
    return Status(201 if created and not preview else 200, result)


@router.get("/invoice-exceptions/{key}", response=s.ExceptionOut)
def invoice_exception(request, key: int):
    query_values(request, [])
    require_model(request, ServiceInvoiceException, "view")
    return representation(get_record(ServiceInvoiceException, key))


@router.patch("/invoice-exceptions/{key}", response=s.ExceptionOut)
@documented_query("dry_run")
def patch_exception(request, key: int, payload: s.ExceptionPatch):
    preview = dry_run(request)
    require_model(request, ServiceInvoiceException, "change")
    with transaction.atomic():
        obj = get_record(ServiceInvoiceException, key)
        if "reason" in payload.model_fields_set:
            obj.reason = payload.reason
        obj.save()
        result = representation(obj)
        if preview:
            result["dry_run"] = True
            transaction.set_rollback(True)
        else:
            log_change(request, obj, CHANGE, "API invoice exception reason corrected")
        return result


@router.delete("/invoice-exceptions/{key}", response={200: s.ExceptionOut, 204: None})
@documented_query("dry_run")
def delete_exception(request, key: int):
    return remove_resource(request, ServiceInvoiceException, key)
