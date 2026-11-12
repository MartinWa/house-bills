"""Explicit public schemas, independent of Django field serialization."""

from datetime import date, datetime
from typing import Annotated, Literal

from ninja import Schema
from pydantic import (
    AfterValidator,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    create_model,
)

from bills.review_edits import REVIEW_LIMITS

Integer = Annotated[StrictInt, Field(ge=-(2**63), le=2**63 - 1)]
Id = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]


def input_date(value):
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("Use an ISO date.")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use an ISO date.")
    return parsed


def frequency(value):
    if value not in (0, 1, 2, 3, 6, 12):
        raise ValueError("Choose an installed billing frequency.")
    return value


ISODate = Annotated[date, BeforeValidator(input_date)]
Month = Annotated[ISODate, Field(description="First day of a month; end months are inclusive.")]
Frequency = Annotated[
    StrictInt, AfterValidator(frequency), Field(json_schema_extra={"enum": [0, 1, 2, 3, 6, 12]})
]
CategoryKey = Literal["electricity", "water", "waste", "other"]
Name = Annotated[str, Field(min_length=1, max_length=120)]
Slug = Annotated[str, Field(min_length=1, max_length=80)]
Key = Annotated[str, Field(min_length=1, max_length=64)]


class Input(Schema):
    model_config = ConfigDict(extra="forbid")


class ErrorDetail(Schema):
    path: str
    code: str
    message: str


class ErrorBody(Schema):
    code: str
    message: str
    details: list[ErrorDetail]


class ErrorOut(Schema):
    error: ErrorBody


class CollectionLink(Schema):
    count: int
    href: str


class Resource(Schema):
    links: dict[str, str | CollectionLink | None]
    dry_run: bool = False


class Page[T](Schema):
    items: list[T]
    count: int
    next: str | None
    previous: str | None


class PropertyIn(Input):
    name: Name
    slug: Slug


class PropertyOut(PropertyIn, Resource):
    id: int | None


class VendorIn(PropertyIn):
    default_property_id: Id | None = None


class VendorOut(VendorIn, Resource):
    id: int | None
    default_property_name: str | None


class IdentifierIn(Input):
    kind: Literal["organization", "vat"]
    value: Annotated[str, Field(min_length=1, max_length=160)]


class IdentifierOut(IdentifierIn, Resource):
    id: int | None
    vendor_id: int
    vendor_name: str


class PropertyRuleIn(Input):
    property_id: Id
    kind: Literal["installation", "contract", "product", "delivery_address"]
    value: Annotated[str, Field(min_length=1, max_length=200)]


class PropertyRuleOut(PropertyRuleIn, Resource):
    id: int | None
    vendor_id: int
    vendor_name: str
    property_name: str


class SubcategoryIn(Input):
    key: Key
    category: CategoryKey
    name: Name
    enabled: StrictBool = True


class SubcategoryOut(SubcategoryIn, Resource):
    category_name: str


class ServiceIn(Input):
    category: CategoryKey
    subcategory: Key | None = None
    mode: Literal["regular", "occasional"] = "regular"
    enabled: StrictBool = True
    start_month: Month
    end_month: Month | None = None


class ServiceOut(ServiceIn, Resource):
    id: int | None
    property_id: int
    property_name: str
    category_name: str
    subcategory_name: str | None


class AssignmentIn(Input):
    vendor_id: Id
    enabled: StrictBool = True
    start_month: Month
    end_month: Month | None = None


class AssignmentOut(AssignmentIn, Resource):
    id: int | None
    service_id: int
    property_id: int
    property_name: str
    vendor_name: str
    category: str
    category_name: str
    subcategory: str | None
    subcategory_name: str | None


class ScheduleIn(Input):
    start_month: Month
    end_month: Month | None = None
    first_expected_month: Month
    frequency_months: Frequency = 1
    grace_days: Annotated[StrictInt, Field(ge=0, le=60)] = 0
    enabled: StrictBool = True


class ScheduleOut(ScheduleIn, Resource):
    id: int | None
    vendor_id: int
    vendor_name: str


class AssignmentReplacement(Input):
    vendor_id: Id
    start_month: Month
    expected_end_month: Month | None
    transition_month: StrictBool = False


class ScheduleReplacement(Input):
    start_month: Month
    first_expected_month: Month
    frequency_months: Frequency
    grace_days: Annotated[StrictInt, Field(ge=0, le=60)]
    expected_end_month: Month | None


class CategoryOut(Resource):
    key: str
    name: str


class FormatOut(Resource):
    format_key: str
    name: str
    versions: list[str]
    vendor_id: int | None = None


class MeOut(Schema):
    account_id: int
    token_name: str
    profile: Literal["automation", "reporting"]
    expires_at: datetime | None


def partial_schema(schema, *, exclude=()):
    # The original type still rejects supplied null for required/nonnullable fields.
    # Defaults are not validated, so omitted fields remain absent from exclude_unset.
    return create_model(
        schema.__name__.replace("In", "Patch"),
        __base__=Input,
        **{
            name: (field.rebuild_annotation(), None)
            for name, field in schema.model_fields.items()
            if name not in exclude
        },
    )


PropertyPatch = partial_schema(PropertyIn)
VendorPatch = partial_schema(VendorIn)
IdentifierPatch = partial_schema(IdentifierIn)
PropertyRulePatch = partial_schema(PropertyRuleIn)
SubcategoryPatch = partial_schema(SubcategoryIn, exclude=("key",))
ServicePatch = partial_schema(ServiceIn)
AssignmentPatch = partial_schema(AssignmentIn)
SchedulePatch = partial_schema(ScheduleIn)


SourcePage = Annotated[StrictInt, Field(ge=1, le=2**63 - 1)]
SourceIndex = Annotated[StrictInt, Field(ge=0, le=59)]


class ReviewHeader(Input):
    vendor_id: Id
    reference: Name
    kind: Literal["invoice", "credit"]
    invoice_date: ISODate
    due_date: ISODate | None = None
    currency: Literal["SEK"] = "SEK"
    net_minor: Integer
    vat_minor: Integer
    rounding_minor: Integer
    invoice_total_minor: Integer
    amount_due_minor: Integer
    source_page: SourcePage = 1


class ReviewSection(Input):
    section_id: Name
    property_id: Id
    category: CategoryKey
    subcategory: Key | None = None
    net_minor: Integer
    vat_minor: Integer
    gross_minor: Integer
    period_start: ISODate
    period_end: ISODate
    source_page: SourcePage = 1


class ReviewUsage(Input):
    section_id: Name
    utility: CategoryKey
    subcategory: Key | None = None
    unit: Literal["kWh", "m3", "kg"]
    quantity_milli: Integer
    period_start: ISODate
    period_end: ISODate
    basis: Literal["measured", "estimated", "reversal", "billed", "billed_weight"]
    source_page: SourcePage = 1
    source_index: SourceIndex | None = None


class ReviewIdentifier(Input):
    scope: Name
    kind: Literal[
        "customer_account",
        "installation",
        "contract",
        "product",
        "delivery_address",
        "organization",
        "vat",
    ]
    value: Annotated[str, Field(min_length=1, max_length=200)]
    source_page: SourcePage = 1
    source_index: SourceIndex | None = None


class ReviewEditsIn(Input):
    header: ReviewHeader
    sections: Annotated[
        list[ReviewSection], Field(min_length=1, max_length=REVIEW_LIMITS["sections"])
    ]
    usages: Annotated[list[ReviewUsage], Field(max_length=REVIEW_LIMITS["usages"])] = []
    identifiers: Annotated[
        list[ReviewIdentifier], Field(max_length=REVIEW_LIMITS["identifiers"])
    ] = []
    amount_due_confirmed: StrictBool = False
    warnings_confirmed: StrictBool = False
    note: Annotated[str, Field(max_length=2000)] = ""


class ReviewIn(Input):
    review_token: Annotated[str, Field(min_length=1, max_length=4096)]
    edits: ReviewEditsIn


class ReviewContextOut(Schema):
    document_id: int
    invoice_id: int | None
    review_revision: int
    extraction_revision: int
    review_id: int | None
    extraction_id: int | None


class ReviewSourceOut(Resource):
    review_token: str
    context: ReviewContextOut
    values: dict
    edits: dict
    warnings: list[str]
    differences: list[dict]


class DocumentOut(Resource):
    id: int
    source: Literal["upload", "manual"]
    original_filename: str
    sha256: str | None
    state: str
    storage_state: str
    storage_pending: bool
    extraction_revision: int
    created_at: datetime
    invoice_ids: list[int]
    deletion_token: str


class DocumentResult(Schema):
    document: DocumentOut
    duplicate: bool = False


class ManualIn(Input):
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class ReparseIn(Input):
    extraction_revision: Annotated[StrictInt, Field(ge=0, le=2**63 - 1)]


class DeletionOut(Schema):
    document_id: int
    deleted: bool
    cleanup_pending: bool


class InvoiceOut(Resource):
    id: int
    document_id: int
    vendor_id: int
    vendor_name: str
    review_revision: int
    approved_at: datetime
    values: dict


class SavedReviewOut(Resource):
    invoice_id: int
    document_id: int
    revision: int
    actor_id: int
    actor_name: str
    created_at: datetime
    note: str
    amount_due_confirmed: bool
    extraction_id: int | None
    snapshot: dict
    storage_state: str
    storage_pending: bool


class ExceptionIn(Input):
    vendor_id: Id
    service_id: Id | None = None
    month: Month
    reason: Annotated[str, Field(min_length=1, max_length=300)]
    invoice_id: Id | None = None
    review_revision: Annotated[StrictInt, Field(ge=1, le=2**63 - 1)] | None = None


class ExceptionPatch(Input):
    reason: Annotated[str, Field(min_length=1, max_length=300)] = None


class ExceptionOut(Resource):
    id: int
    vendor_id: int
    vendor_name: str
    service_id: int | None
    month: date
    reason: str
    invoice_id: int | None
    review_revision: int | None


class MetadataOut(Schema):
    categories: list[dict]
    subcategories: list[dict]
    vendor_identifier_kinds: list[dict]
    property_rule_kinds: list[dict]
    identifier_kinds: list[dict]
    units: list[str]
    bases: list[dict]
    frequencies: list[dict]
    review_limits: dict[str, int]
    billed_period_end: str
    configuration_end_month: str


class ReportFilters(Schema):
    property_id: list[int] = []
    vendor_id: list[int] = []
    category: str | None = None
    subcategory: str | None = None
    date_from: date | None = None
    date_to: date | None = None


class DateBounds(Schema):
    date_from: date | None
    date_to: date | None


class ReportRow(Schema):
    property_id: int
    property_name: str
    vendor_id: int | None = None
    vendor_name: str | None = None
    invoice_id: int | None = None
    invoice_date: date | None = None
    invoice_time: int | None = None
    invoice_month: date | None = None
    invoice_month_time: int | None = None
    category: str | None = None
    subcategory: str | None = None
    currency: str | None = None
    net_minor: int | None = None
    vat_minor: int | None = None
    gross_minor: int | None = None
    utility: str | None = None
    unit: str | None = None
    quantity_milli: int | None = None
    period_start: date | None = None
    period_end: date | None = None
    period_start_time: int | None = None
    period_end_time: int | None = None
    basis: str | None = None
    source_service: str | None = None
    source_selection: str | None = None
    selection_reason: str | None = None
    source: str | None = None


class AggregateOut(Schema):
    mode: str
    filters: ReportFilters
    effective_date_bounds: DateBounds
    group_count: int
    totals: list[ReportRow]
    groups: list[ReportRow]


class ReportPage(Page[ReportRow]):
    mode: str
    filters: ReportFilters
    effective_date_bounds: DateBounds


class SummaryOut(Schema):
    filters: ReportFilters
    effective_date_bounds: DateBounds
    costs: list[ReportRow]
    consumption: list[ReportRow]
    total_invoices: int
    pdf_invoices: int
    manual_invoices: int
    exclusion_count: int


class SelectorsOut(Schema):
    properties: list[dict]
    vendors: list[dict]
    categories: list[dict]
    subcategories: list[dict]


class CoverageOut(Schema):
    as_of: date
    coverage_rows: list[dict]
    property_services: list[dict]
    counts: dict[str, int]
