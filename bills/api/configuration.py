"""Owner-scoped configuration routes using validated models and dated services."""

import inspect
from dataclasses import dataclass
from functools import wraps

from django.contrib import admin
from django.contrib.admin.models import ADDITION, CHANGE, DELETION
from django.contrib.admin.utils import get_deleted_objects
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.http import HttpResponse
from ninja import Router, Status

from bills.configuration_actions import change_billing_frequency, change_vendor
from bills.models import (
    Category,
    InvoicePropertyRule,
    Property,
    PropertyService,
    ServiceInvoiceException,
    ServiceVendorAssignment,
    Subcategory,
    Vendor,
    VendorBillingSchedule,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing.registry import LAYOUTS, layout_family_choices

from . import schemas as s
from .audit import log_change
from .auth import PREFIX, require_model
from .collections import boolean, filter_collection, page_response, pagination, query_values
from .contract import documented_query, query_parameters
from .errors import ApiError, invalid_parameter

router = Router(tags=["Configuration"])


def resource_path(segment, key):
    return f"{PREFIX}/{segment}/{key}" if key is not None else None


def configuration_queryset(model):
    relations = {
        Vendor: ("default_property",),
        VendorIssuerIdentifier: ("vendor",),
        InvoicePropertyRule: ("vendor", "property"),
        PropertyService: ("property", "subcategory"),
        ServiceVendorAssignment: (
            "vendor",
            "property_service__property",
            "property_service__subcategory",
            "replacement_period",
        ),
        VendorBillingSchedule: ("vendor", "replacement_period"),
        ServiceInvoiceException: ("vendor",),
    }
    counts = {
        Property: ("services",),
        Vendor: (
            "issuer_identifiers",
            "layout_families",
            "property_identifiers",
            "billing_schedules",
            "service_assignments",
        ),
        PropertyService: ("vendor_assignments",),
    }
    queryset = model.objects.all()
    if model in relations:
        queryset = queryset.select_related(*relations[model])
    for name in counts.get(model, ()):
        relation = model._meta.get_field(name)
        related_counts = (
            relation.related_model.objects.filter(**{relation.field.name: OuterRef("pk")})
            .order_by()
            .values(relation.field.name)
            .annotate(total=Count("pk"))
            .values("total")
        )
        queryset = queryset.annotate(**{f"{name}_count": Coalesce(Subquery(related_counts), 0)})
    return queryset


def relation_count(obj, name):
    if not obj.pk:
        return 0
    count = getattr(obj, f"{name}_count", None)
    return count if count is not None else getattr(obj, name).count()


def representation(obj):
    fields = {}
    links = {}
    if isinstance(obj, Property):
        segment = "properties"
        fields = {"name": obj.name, "slug": obj.slug}
        links["services"] = {
            "count": relation_count(obj, "services"),
            "href": f"{PREFIX}/properties/{obj.pk}/services",
        }
    elif isinstance(obj, Vendor):
        segment = "vendors"
        fields = {
            "name": obj.name,
            "slug": obj.slug,
            "default_property_id": obj.default_property_id,
            "default_property_name": obj.default_property.name if obj.default_property_id else None,
        }
        if obj.default_property_id:
            links["default_property"] = resource_path("properties", obj.default_property_id)
        for name, relation in {
            "identifiers": "issuer_identifiers",
            "invoice-formats": "layout_families",
            "property-rules": "property_identifiers",
            "billing-schedules": "billing_schedules",
            "vendor-assignments": "service_assignments",
        }.items():
            links[name.replace("-", "_")] = {
                "count": relation_count(obj, relation),
                "href": f"{PREFIX}/vendors/{obj.pk}/{name}",
            }
    elif isinstance(obj, VendorIssuerIdentifier):
        segment = "vendor-identifiers"
        fields = {
            "kind": obj.kind,
            "value": obj.value,
            "vendor_id": obj.vendor_id,
            "vendor_name": obj.vendor.name,
        }
        links.update(
            owner=resource_path("vendors", obj.vendor_id),
            collection=f"{PREFIX}/vendors/{obj.vendor_id}/identifiers",
        )
    elif isinstance(obj, InvoicePropertyRule):
        segment = "property-rules"
        fields = {
            "kind": obj.kind,
            "value": obj.value,
            "vendor_id": obj.vendor_id,
            "vendor_name": obj.vendor.name,
            "property_id": obj.property_id,
            "property_name": obj.property.name,
        }
        links.update(
            owner=resource_path("vendors", obj.vendor_id),
            collection=f"{PREFIX}/vendors/{obj.vendor_id}/property-rules",
            property=resource_path("properties", obj.property_id),
        )
    elif isinstance(obj, Subcategory):
        segment = "subcategories"
        fields = {
            "key": obj.key,
            "name": obj.name,
            "category": obj.category,
            "category_name": Category(obj.category).label,
            "enabled": obj.is_active,
        }
        links["category"] = resource_path("categories", obj.category)
    elif isinstance(obj, PropertyService):
        segment = "services"
        fields = {
            "property_id": obj.property_id,
            "property_name": obj.property.name,
            "category": obj.category,
            "category_name": Category(obj.category).label,
            "subcategory": obj.subcategory_id,
            "subcategory_name": obj.subcategory.name if obj.subcategory_id else None,
            "mode": obj.mode,
        }
        links.update(
            owner=resource_path("properties", obj.property_id),
            collection=f"{PREFIX}/properties/{obj.property_id}/services",
            category=resource_path("categories", obj.category),
            subcategory=resource_path("subcategories", obj.subcategory_id),
        )
        links["vendor_assignments"] = {
            "count": relation_count(obj, "vendor_assignments"),
            "href": f"{PREFIX}/services/{obj.pk}/vendor-assignments",
        }
    elif isinstance(obj, ServiceVendorAssignment):
        segment = "vendor-assignments"
        service = obj.property_service
        fields = {
            "vendor_id": obj.vendor_id,
            "vendor_name": obj.vendor.name,
            "service_id": obj.property_service_id,
            "property_id": service.property_id,
            "property_name": service.property.name,
            "category": service.category,
            "category_name": Category(service.category).label,
            "subcategory": service.subcategory_id,
            "subcategory_name": service.subcategory.name if service.subcategory_id else None,
        }
        links.update(
            owner=resource_path("services", obj.property_service_id),
            collection=f"{PREFIX}/services/{obj.property_service_id}/vendor-assignments",
            vendor=resource_path("vendors", obj.vendor_id),
            property=resource_path("properties", service.property_id),
            category=resource_path("categories", service.category),
            subcategory=resource_path("subcategories", service.subcategory_id),
            replacement=f"{resource_path(segment, obj.pk)}/replacement"
            if obj.pk and obj.is_active
            else None,
        )
    elif isinstance(obj, VendorBillingSchedule):
        segment = "billing-schedules"
        fields = {
            "vendor_id": obj.vendor_id,
            "vendor_name": obj.vendor.name,
            "first_expected_month": obj.first_expected_month,
            "frequency_months": obj.frequency_months,
            "grace_days": obj.grace_days,
        }
        links.update(
            owner=resource_path("vendors", obj.vendor_id),
            collection=f"{PREFIX}/vendors/{obj.vendor_id}/billing-schedules",
            replacement=f"{resource_path(segment, obj.pk)}/replacement"
            if obj.pk and obj.is_active
            else None,
        )
    elif isinstance(obj, ServiceInvoiceException):
        segment = "invoice-exceptions"
        fields = {
            "vendor_id": obj.vendor_id,
            "vendor_name": obj.vendor.name,
            "service_id": obj.property_service_id,
            "month": obj.month,
            "reason": obj.reason,
            "invoice_id": obj.reviewed_invoice_id,
            "review_revision": obj.reviewed_revision,
        }
        links.update(
            vendor=resource_path("vendors", obj.vendor_id),
            owner=resource_path("services", obj.property_service_id)
            or resource_path("vendors", obj.vendor_id),
            invoice=resource_path("invoices", obj.reviewed_invoice_id),
            collection=f"{PREFIX}/invoice-exceptions",
        )
    else:
        raise TypeError("Unregistered configuration resource")
    if isinstance(obj, (PropertyService, ServiceVendorAssignment, VendorBillingSchedule)):
        fields.update(start_month=obj.start_month, end_month=obj.end_month, enabled=obj.is_active)
    if isinstance(obj, (ServiceVendorAssignment, VendorBillingSchedule)) and obj.pk:
        if obj.predecessor_id:
            links["predecessor"] = resource_path(segment, obj.predecessor_id)
        replacement = getattr(obj, "replacement_period", None)
        if replacement is not None:
            links["replace_period"] = links.get("replacement")
            links["replacement"] = resource_path(segment, replacement.pk)
    if not isinstance(obj, Subcategory):
        fields["id"] = obj.pk
    links["self"] = resource_path(segment, obj.pk)
    return {**fields, "links": links, "dry_run": False}


def dry_run(request):
    values = query_values(request, ["dry_run"])
    return boolean(values.get("dry_run", "false"), "dry_run")


def get_record(model, key):
    try:
        return configuration_queryset(model).get(pk=key)
    except model.DoesNotExist:
        raise ApiError(404, "not_found", "Resource not found.") from None


def deletion_allowed(request, obj):
    require_model(request, type(obj), "delete")
    _, _, missing, protected = get_deleted_objects([obj], request, admin.site)
    if missing:
        raise ApiError(403, "forbidden", "Deletion requires permissions for related configuration.")
    if protected:
        raise ApiError(409, "protected_reference", "Referenced records prevent deletion.")


def write_resource(request, response, model, payload, *, key=None, owner=None):
    preview = dry_run(request)
    require_model(request, model, "add" if key is None else "change")
    with transaction.atomic():
        obj = get_record(model, key) if key is not None else model(**(owner or {}))
        for name, value in payload.model_dump(exclude_unset=key is not None).items():
            name = {"enabled": "is_active", "subcategory": "subcategory_id"}.get(name, name)
            setattr(obj, name, value)
        obj.save()
        result = representation(obj)
        if preview:
            result["dry_run"] = True
            transaction.set_rollback(True)
        else:
            log_change(
                request, obj, ADDITION if key is None else CHANGE, "API configuration change"
            )
    if key is None and not preview:
        response["Location"] = result["links"]["self"]
        return Status(201, result)
    return Status(200, result)


def remove_resource(request, model, key):
    preview = dry_run(request)
    with transaction.atomic():
        obj = get_record(model, key)
        deletion_allowed(request, obj)
        result = representation(obj)
        if not preview:
            log_change(request, obj, DELETION, "API configuration deletion")
        obj.delete()
        if preview:
            transaction.set_rollback(True)
            result["dry_run"] = True
            return Status(200, result)
    return Status(204, None)


@dataclass(frozen=True)
class Collection:
    model: type
    path: str
    detail: str
    input: type
    patch: type
    output: type
    filters: dict
    order: tuple
    sortable: dict
    owner: tuple | None = None


COLLECTIONS = (
    Collection(
        Property,
        "/properties",
        "/properties/{key}",
        s.PropertyIn,
        s.PropertyPatch,
        s.PropertyOut,
        {"slug": "slug", "name": "name__icontains"},
        ("name", "id"),
        {"name": "name", "slug": "slug", "id": "id"},
    ),
    Collection(
        Vendor,
        "/vendors",
        "/vendors/{key}",
        s.VendorIn,
        s.VendorPatch,
        s.VendorOut,
        {"slug": "slug", "name": "name__icontains", "default_property_id": "default_property_id"},
        ("name", "id"),
        {"name": "name", "slug": "slug", "id": "id"},
    ),
    Collection(
        VendorIssuerIdentifier,
        "/vendors/{owner_id}/identifiers",
        "/vendor-identifiers/{key}",
        s.IdentifierIn,
        s.IdentifierPatch,
        s.IdentifierOut,
        {"kind": "kind", "value": "value"},
        ("kind", "id"),
        {"kind": "kind", "value": "value", "id": "id"},
        (Vendor, "vendor_id"),
    ),
    Collection(
        InvoicePropertyRule,
        "/vendors/{owner_id}/property-rules",
        "/property-rules/{key}",
        s.PropertyRuleIn,
        s.PropertyRulePatch,
        s.PropertyRuleOut,
        {"property_id": "property_id", "kind": "kind"},
        ("id",),
        {"id": "id"},
        (Vendor, "vendor_id"),
    ),
    Collection(
        Subcategory,
        "/subcategories",
        "/subcategories/{key}",
        s.SubcategoryIn,
        s.SubcategoryPatch,
        s.SubcategoryOut,
        {"category": "category", "enabled": "is_active", "q": "name__icontains"},
        ("key",),
        {"key": "key", "name": "name"},
    ),
    Collection(
        PropertyService,
        "/properties/{owner_id}/services",
        "/services/{key}",
        s.ServiceIn,
        s.ServicePatch,
        s.ServiceOut,
        {
            "category": "category",
            "subcategory": "subcategory_id",
            "mode": "mode",
            "enabled": "is_active",
        },
        ("category", "subcategory", "id"),
        {"category": "category", "subcategory": "subcategory_id", "id": "id"},
        (Property, "property_id"),
    ),
    Collection(
        ServiceVendorAssignment,
        "/services/{owner_id}/vendor-assignments",
        "/vendor-assignments/{key}",
        s.AssignmentIn,
        s.AssignmentPatch,
        s.AssignmentOut,
        {"vendor_id": "vendor_id", "active_month": "", "enabled": "is_active"},
        ("start_month", "id"),
        {"start_month": "start_month", "end_month": "end_month", "id": "id"},
        (PropertyService, "property_service_id"),
    ),
    Collection(
        VendorBillingSchedule,
        "/vendors/{owner_id}/billing-schedules",
        "/billing-schedules/{key}",
        s.ScheduleIn,
        s.SchedulePatch,
        s.ScheduleOut,
        {"active_month": "", "enabled": "is_active"},
        ("start_month", "id"),
        {"start_month": "start_month", "id": "id"},
        (Vendor, "vendor_id"),
    ),
)


def register_collection(spec):
    # This table only wires identical HTTP operations; all validation stays in models.
    def list_records(request, owner_id: int | None = None):
        require_model(request, spec.model, "view")
        queryset = configuration_queryset(spec.model)
        if spec.owner:
            get_record(spec.owner[0], owner_id)
            queryset = queryset.filter(**{spec.owner[1]: owner_id})
        choices = {"category": Category.values, "mode": PropertyService.Mode.values}
        if "kind" in spec.filters:
            choices["kind"] = spec.model.Kind.values
        if "subcategory" in spec.filters and "subcategory" in request.GET:
            choices["subcategory"] = list(Subcategory.objects.values_list("key", flat=True))
        queryset, (limit, offset) = filter_collection(
            queryset,
            request,
            spec.filters,
            spec.order,
            spec.sortable,
            choices=choices,
        )
        return page_response(
            request,
            [representation(obj) for obj in queryset[offset : offset + limit]],
            queryset.count(),
            limit,
            offset,
        )

    def create_record(request, response: HttpResponse, payload, owner_id: int | None = None):
        owner = None
        if spec.owner:
            get_record(spec.owner[0], owner_id)
            owner = {spec.owner[1]: owner_id}
        return write_resource(request, response, spec.model, payload, owner=owner)

    def read_record(request, key):
        query_values(request, [])
        require_model(request, spec.model, "view")
        return representation(get_record(spec.model, key))

    def patch_record(request, response: HttpResponse, key, payload):
        return write_resource(request, response, spec.model, payload, key=key)

    def delete_record(request, key):
        return remove_resource(request, spec.model, key)

    key_type = str if spec.model is Subcategory else int
    create_record.__annotations__["payload"] = spec.input
    for fn in (list_records, create_record):
        signature = inspect.signature(fn)
        fn.__signature__ = signature.replace(
            parameters=[
                param.replace(default=inspect.Parameter.empty, annotation=int)
                if param.name == "owner_id"
                else param
                for param in signature.parameters.values()
                if param.name != "owner_id" or spec.owner
            ]
        )
    patch_record.__annotations__.update(key=key_type, payload=spec.patch)
    read_record.__annotations__["key"] = key_type
    delete_record.__annotations__["key"] = key_type
    name = spec.model._meta.model_name
    list_records.api_parameters = query_parameters(
        [*spec.filters, "sort", "limit", "offset"],
        sortable=spec.sortable,
    )
    for fn in (create_record, patch_record, delete_record):
        fn.api_parameters = query_parameters(["dry_run"])
    for method, path, fn, result in (
        ("get", spec.path, list_records, s.Page[spec.output]),
        ("post", spec.path, create_record, {200: spec.output, 201: spec.output}),
        ("get", spec.detail, read_record, spec.output),
        ("patch", spec.detail, patch_record, {200: spec.output}),
        ("delete", spec.detail, delete_record, {200: spec.output, 204: None}),
    ):
        aliases = {}
        if "{owner_id}" in path:
            owner_name = "service_id" if spec.owner[0] is PropertyService else spec.owner[1]
            aliases[owner_name] = "owner_id"
        if "{key}" in path:
            key_name = {
                Property: "property_id",
                Vendor: "vendor_id",
                VendorIssuerIdentifier: "identifier_id",
                InvoicePropertyRule: "rule_id",
                Subcategory: "subcategory_key",
                PropertyService: "service_id",
                ServiceVendorAssignment: "assignment_id",
                VendorBillingSchedule: "schedule_id",
            }[spec.model]
            aliases[key_name] = "key"
        for public, internal in aliases.items():
            path = path.replace("{" + internal + "}", "{" + public + "}")
        if aliases:
            fn = named_path_parameters(fn, aliases)
        getattr(router, method)(
            path,
            response=result,
            operation_id=f"{method}_{name}_{fn.__name__}",
            summary=f"{fn.__name__.replace('_', ' ').capitalize()}: {name}",
        )(fn)


def named_path_parameters(view, aliases):
    # Keep generic owner_id/key handlers while publishing resource-specific names
    # in Ninja's inspected signature, request binding and generated OpenAPI schema.
    @wraps(view)
    def named(request, *args, **kwargs):
        return view(
            request, *args, **{aliases.get(key, key): value for key, value in kwargs.items()}
        )

    public_names = {internal: public for public, internal in aliases.items()}
    named.__signature__ = inspect.signature(view).replace(
        parameters=[
            param.replace(name=public_names.get(param.name, param.name))
            for param in inspect.signature(view).parameters.values()
        ]
    )
    named.__annotations__ = {
        public_names.get(name, name): value for name, value in view.__annotations__.items()
    }
    return named


for collection in COLLECTIONS:
    register_collection(collection)


@router.get("/vendors/{vendor_id}/vendor-assignments", response=s.Page[s.AssignmentOut])
@documented_query(
    "service_id",
    "property_id",
    "active_month",
    "enabled",
    "sort",
    "limit",
    "offset",
    sortable=["start_month", "end_month", "id"],
)
def vendor_assignments(request, vendor_id: int):
    require_model(request, ServiceVendorAssignment, "view")
    get_record(Vendor, vendor_id)
    queryset, (limit, offset) = filter_collection(
        configuration_queryset(ServiceVendorAssignment).filter(vendor_id=vendor_id),
        request,
        {
            "service_id": "property_service_id",
            "property_id": "property_service__property_id",
            "active_month": "",
            "enabled": "is_active",
        },
        ("start_month", "id"),
        {"start_month": "start_month", "end_month": "end_month", "id": "id"},
    )
    return page_response(
        request,
        [representation(obj) for obj in queryset[offset : offset + limit]],
        queryset.count(),
        limit,
        offset,
    )


def replace_period(request, response, model, key, payload):
    preview = dry_run(request)
    require_model(request, model, "change")
    require_model(request, model, "add")
    for name in ("start_month", "expected_end_month", "first_expected_month"):
        value = getattr(payload, name, None)
        if value is not None and value.day != 1:
            raise ApiError(
                422,
                "validation_error",
                "Configuration dates must be first-of-month.",
                [
                    {
                        "path": name,
                        "code": "invalid_month",
                        "message": "Use the first day of a month.",
                    }
                ],
            )
    with transaction.atomic():
        old = get_record(model, key)
        if old.end_month != payload.expected_end_month:
            raise ApiError(409, "stale_configuration", "The predecessor end month has changed.")
        try:
            if model is ServiceVendorAssignment:
                replacement = change_vendor(
                    key,
                    month=payload.start_month,
                    vendor=get_record(Vendor, payload.vendor_id),
                    transition_month=payload.transition_month,
                )
            else:
                replacement = change_billing_frequency(
                    key,
                    month=payload.start_month,
                    frequency_months=payload.frequency_months,
                    first_expected_month=payload.first_expected_month,
                    grace_days=payload.grace_days,
                )
        except ValidationError:
            raise ApiError(
                409,
                "configuration_conflict",
                "This period cannot be replaced with the requested configuration.",
            ) from None
        if preview:
            result = representation(replacement)
            result["links"]["predecessor"] = representation(old)["links"]["self"]
            transaction.set_rollback(True)
            result["dry_run"] = True
        else:
            old.refresh_from_db()
            # Relationships drive current lineage; these IDs retain the historical
            # replacement event even after a later split or configuration deletion.
            relation = {
                "api_replacement": {"predecessor_id": old.pk, "replacement_id": replacement.pk}
            }
            log_change(request, old, CHANGE, [{"changed": {"fields": ["end_month"]}}, relation])
            log_change(request, replacement, ADDITION, [{"added": {}}, relation])
            successor = getattr(replacement, "replacement_period", None)
            if successor is not None:
                log_change(
                    request,
                    successor,
                    CHANGE,
                    f"API replacement predecessor changed from {old.pk} to {replacement.pk}",
                )
            result = representation(replacement)
    if preview:
        return Status(200, result)
    response["Location"] = result["links"]["self"]
    return Status(201, result)


@router.post(
    "/vendor-assignments/{assignment_id}/replacement",
    response={200: s.AssignmentOut, 201: s.AssignmentOut},
)
@documented_query("dry_run")
def replace_assignment(
    request, response: HttpResponse, assignment_id: int, payload: s.AssignmentReplacement
):
    return replace_period(request, response, ServiceVendorAssignment, assignment_id, payload)


@router.post(
    "/billing-schedules/{schedule_id}/replacement",
    response={200: s.ScheduleOut, 201: s.ScheduleOut},
)
@documented_query("dry_run")
def replace_schedule(
    request, response: HttpResponse, schedule_id: int, payload: s.ScheduleReplacement
):
    return replace_period(request, response, VendorBillingSchedule, schedule_id, payload)


def format_representation(key, vendor_id=None):
    choices = dict(layout_family_choices())
    if key not in choices:
        raise ApiError(404, "not_found", "Invoice format not found.")
    path = (
        f"{PREFIX}/invoice-formats/{key}"
        if vendor_id is None
        else f"{PREFIX}/vendors/{vendor_id}/invoice-formats/{key}"
    )
    links = {"self": path}
    if vendor_id is not None:
        links.update(
            owner=resource_path("vendors", vendor_id),
            collection=f"{PREFIX}/vendors/{vendor_id}/invoice-formats",
            format=f"{PREFIX}/invoice-formats/{key}",
        )
    return {
        "format_key": key,
        "name": choices[key],
        "vendor_id": vendor_id,
        "versions": sorted(LAYOUTS[key]),
        "links": links,
    }


def catalogue_page(request, rows, *, sort_keys, default):
    values = query_values(request, ["limit", "offset", "sort"])
    limit, offset = pagination(request, values)
    order = values.get("sort", default).split(",")
    for key in reversed([*order, default]):
        if key.lstrip("-") not in sort_keys:
            invalid_parameter("sort")
        rows.sort(key=lambda row: row[key.lstrip("-")], reverse=key.startswith("-"))
    return page_response(request, rows[offset : offset + limit], len(rows), limit, offset)


@router.get("/categories", response=s.Page[s.CategoryOut])
@documented_query("limit", "offset", "sort", sortable=["key", "name"])
def categories(request):
    return catalogue_page(
        request,
        [category_representation(key) for key in Category.values],
        sort_keys={"key", "name"},
        default="key",
    )


def category_representation(key):
    if key not in Category.values:
        raise ApiError(404, "not_found", "Category not found.")
    return {
        "key": key,
        "name": Category(key).label,
        "links": {"self": resource_path("categories", key)},
    }


@router.get("/categories/{key}", response=s.CategoryOut)
def category(request, key: str):
    query_values(request, [])
    return category_representation(key)


@router.get("/invoice-formats", response=s.Page[s.FormatOut])
@documented_query("limit", "offset", "sort", sortable=["format_key"])
def formats(request):
    return catalogue_page(
        request,
        [format_representation(key) for key in LAYOUTS],
        sort_keys={"format_key"},
        default="format_key",
    )


@router.get("/invoice-formats/{format_key}", response=s.FormatOut)
def invoice_format(request, format_key: str):
    query_values(request, [])
    return format_representation(format_key)


@router.get("/vendors/{vendor_id}/invoice-formats", response=s.Page[s.FormatOut])
@documented_query("limit", "offset", "sort", sortable=["format_key"])
def vendor_formats(request, vendor_id: int):
    require_model(request, VendorLayoutFamily, "view")
    vendor = get_record(Vendor, vendor_id)
    return catalogue_page(
        request,
        [
            format_representation(key, vendor_id)
            for key in vendor.layout_families.values_list("family", flat=True)
        ],
        sort_keys={"format_key"},
        default="format_key",
    )


@router.get("/vendors/{vendor_id}/invoice-formats/{format_key}", response=s.FormatOut)
def vendor_format(request, vendor_id: int, format_key: str):
    query_values(request, [])
    require_model(request, VendorLayoutFamily, "view")
    get_record(Vendor, vendor_id)
    if not VendorLayoutFamily.objects.filter(vendor_id=vendor_id, family=format_key).exists():
        raise ApiError(404, "not_found", "Format selection not found.")
    return format_representation(format_key, vendor_id)


@router.put(
    "/vendors/{vendor_id}/invoice-formats/{format_key}",
    response={200: s.FormatOut, 201: s.FormatOut},
)
@documented_query("dry_run")
def select_format(request, response: HttpResponse, vendor_id: int, format_key: str):
    preview = dry_run(request)
    require_model(request, VendorLayoutFamily, "add")
    result = format_representation(format_key, vendor_id)
    with transaction.atomic():
        get_record(Vendor, vendor_id)
        obj, created = VendorLayoutFamily.objects.get_or_create(
            vendor_id=vendor_id, family=format_key
        )
        if preview:
            transaction.set_rollback(True)
            result["dry_run"] = True
        elif created:
            log_change(request, obj, ADDITION, "API invoice format selected")
    if created and not preview:
        response["Location"] = result["links"]["self"]
        return Status(201, result)
    return Status(200, result)


@router.delete(
    "/vendors/{vendor_id}/invoice-formats/{format_key}", response={200: s.FormatOut, 204: None}
)
@documented_query("dry_run")
def unselect_format(request, vendor_id: int, format_key: str):
    preview = dry_run(request)
    require_model(request, VendorLayoutFamily, "delete")
    result = format_representation(format_key, vendor_id)
    with transaction.atomic():
        get_record(Vendor, vendor_id)
        obj = VendorLayoutFamily.objects.filter(vendor_id=vendor_id, family=format_key).first()
        if obj:
            deletion_allowed(request, obj)
            if not preview:
                log_change(request, obj, DELETION, "API invoice format unselected")
            obj.delete()
        if preview:
            transaction.set_rollback(True)
            result["dry_run"] = True
            return Status(200, result)
    return Status(204, None)
