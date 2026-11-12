# HTTP API and automation

The billing API is mounted at `/api/v1/`. It uses the same validated models,
approval services, original storage and accounting projections as the HTML application.
Its complete contract is the generated [OpenAPI schema](api/openapi.json).

## Credentials and discovery

A staff operator issues a named token in **Administration → API tokens**, choosing
**Automation** or **Reporting only**, with an optional expiry. The creation page
shows the random secret once. Copy it to private client configuration; subsequent
admin reads contain no secret. Delete a token to revoke it. Staff need the token
model's add/view/delete permissions and can manage their own tokens; only a superuser
can issue or manage another account's tokens. Tokens are immutable.

Keep the same token in client configuration across container restarts and image
upgrades. Its hash remains in the application database on the persistent `/data`
mount. Follow the [upgrade procedure](deployment.md#manual-upgrades) while retaining
that database. Expired or revoked tokens and inactive accounts no longer grant access.

Send `Authorization: Bearer <secret>` on every business request. Session cookies
cannot authenticate the API. Profiles restrict an account's existing access:
configuration additionally requires staff status and the corresponding model
permissions. Deleting configuration requires the permissions for its registered
deletion cascade. Confirmations require `add_serviceinvoiceexception`; annotation
reads, corrections and deletion require staff and the corresponding permissions.

Reporting tokens can GET `/me`, `/reports/selectors`, `/reports/summary`,
`/reports/costs`, `/reports/consumption` and `/reports/consumption-issues`.
They cannot access configuration, pending proposals, originals, coverage or writes,
even when owned by a superuser. HTML workflow access remains available to signed-in
active accounts, and automation tokens inherit that same shared-installation access.

`GET /api/v1/openapi.json` accepts either token profile or a staff session.
`/api/v1/docs` is a staff-session-only interactive viewer using collected local
static assets. Calls made in that viewer still need a bearer token. `/me` returns
the account ID, token name, profile and expiry. Automation cannot issue its own
credential; initial token issuance needs a human operator.

## Requests, pagination and errors

Use `application/json` for JSON bodies and `multipart/form-data` with one `pdf`
file for uploads. Bodyless operations require no content type. JSON is bounded to
512 KiB before decoding; PDF limits follow the installation's separate upload
settings. Every API response has `Cache-Control: no-store`; originals additionally
retain `private`. Every 401 includes `WWW-Authenticate: Bearer`.

Money uses signed integer minor units (100 per SEK), consumption signed integer
thousandths (1,000 per unit). Numeric strings, floats and booleans cannot supply
these values or observed revisions. JSON date inputs are ISO `YYYY-MM-DD` strings.
Unknown values remain null rather than becoming zero.

Record collections return `items`, the complete filtered `count`, and `next`/`previous`
continuation paths. Follow these links until `next` is null. `limit` defaults to 50
and is at most 200; `offset` is nonnegative. `sort` accepts documented comma-separated
keys with an optional `-` prefix. Every list ends its ordering with a unique key.
Continuation links preserve filters and sort. The schema documents each collection's
filter and sort allowlist. Unknown parameters, repeated scalar parameters and invalid
filter/sort values fail with 422 `invalid_parameter`.

Errors have the shape:

```json
{"error":{"code":"validation_error","message":"Resource validation failed.","details":[{"path":"start_month","code":"invalid_value","message":"Use the first day of the month."}]}}
```

Malformed requests/tokens use 400, credentials 401, permissions 403, missing resources
404, unsupported methods 405 with `Allow`, conflicts/stale observations 409,
oversized bodies 413, unsupported media types 415, validation 422 and unavailable
storage/database access 503. Database contention includes `Retry-After: 1`.
Unexpected programming failures remain distinct from transient unavailability.

## Configuration

Create properties and vendors, discover `/metadata`, `/categories`, `/subcategories`
and `/invoice-formats`, then create owned configuration through its parent's collection:

| Collection | Canonical detail |
| --- | --- |
| `/properties` | `/properties/{id}` |
| `/vendors` | `/vendors/{id}` |
| `/vendors/{id}/identifiers` | `/vendor-identifiers/{id}` |
| `/vendors/{id}/property-rules` | `/property-rules/{id}` |
| `/properties/{id}/services` | `/services/{id}` |
| `/services/{id}/vendor-assignments` | `/vendor-assignments/{id}` |
| `/vendors/{id}/billing-schedules` | `/billing-schedules/{id}` |
| `/subcategories` | `/subcategories/{key}` |

Collections support GET/POST; details support GET/PATCH/DELETE. Owner IDs come from
the path and cannot be supplied or changed in bodies. Reads include related names.
Canonical and related paths appear in `links`; parents advertise child collection
counts and first-page paths. Full child lists are always paginated. The read-only
`/vendors/{id}/vendor-assignments` view exposes the same assignment identities
across properties/services.

Select a family with `PUT /vendors/{id}/invoice-formats/{format_key}`: the first call
returns 201 and a repeated selection 200. DELETE unselects it and returns 204 even
when already absent. Catalogue versions describe installed parsers; membership
selects their family.

PATCH merges only supplied fields into the latest committed record. Omitted fields
survive, null clears nullable fields, and undeclared/owner/read-only fields fail.
The complete resulting model and configuration graph are validated. The last
committed write to a supplied field wins. Configuration creates return 201 with
`Location`; ordinary deletes return 204. Configuration changes and admin audit
entries commit together.

Configuration writes accept `?dry_run=true`: they perform the same permissions,
model/graph and cascade/protection checks, then roll back without audit entries.
Accepted previews return 200 with `dry_run: true`; their generated IDs/links are
provisional and cannot be reused in subsequent requests. To build a setup sequence,
create accepted resources normally and use their returned IDs for later calls.
A service with no vendor is a valid intermediate state.

All configuration month markers are first-of-month ISO dates. Start and end months
are inclusive: January through March uses `2027-01-01` / `2027-03-01`. Null end
means open-ended. Billed invoice/consumption periods instead exclude their end date.

For a dated change, POST to the predecessor's `/replacement` operation with
`start_month` and required `expected_end_month` (including explicit null if observed
open-ended). Assignment replacements also take `vendor_id` and optional
`transition_month`; schedule replacements take `frequency_months`,
`first_expected_month` and `grace_days`. Both add and change permissions are required.
The old end and new period commit atomically; assignment transitions may share the
transition month. A changed observed end returns 409 `stale_configuration`.
Retries cannot create another overlapping replacement. Ordinary period POST never
closes a predecessor. Canonical reads retain predecessor/replacement links through
the existing admin audit trail. Inspect current state after an ambiguous timeout.

## Documents and reviews

POST `/documents` with one PDF. A new document returns 201; exact duplicate bytes
return 200 with `duplicate: true` and the existing document. POST `/manual-invoices`
with `{}` for a paper/ledger entry, optionally adding a printable `idempotency_key`
of 1–200 characters. Repeating a key returns 200 and the same placeholder. Without
a key, each request creates a new placeholder; invoice identity validation still
prevents duplicate approved bills. An empty manual entry can be cancelled with
POST `/documents/{id}/cancel`.

GET `/documents/{id}/review` returns source values, editable `edits`, warnings,
differences, observed context and `review_token`. Approved corrections are the
default source; `?proposal=latest` explicitly selects the latest extraction.
The edit template can contain unknown/null proposal values; fill them to satisfy
the `ReviewEditsIn` schema before submitting. POST `/documents/{id}/reviews` with
`{"review_token":"...","edits":{...}}` to approve or correct.

Edits include the complete header, allocations, usage and identifier lists,
confirmation booleans and a note of at most 2,000 characters. Limits are 20
allocations, 60 usages and 60 identifiers. Omitted rows are removed. Keep each
retained usage/identifier's `source_index` from the observed template; it identifies
evidence in the immutable source regardless of submitted ordering. Indexes refer
to the complete observed source, including rows beyond the submission limits;
trim unwanted rows while retaining each kept row's original index. New rows use
null indexes. Section keys are distinct. Only source page numbers are editable
evidence; layout provenance, source text/boxes, actors and approval state are server-owned.
Warnings and amount-due differences require explicit confirmation. Both HTML and
API edits use the shared candidate/evidence builder and financial validation.

Successful approval/correction returns 201 and a canonical saved-review `Location`
at `/invoices/{id}/reviews/{revision}`. GET `/invoices` and `/invoices/{id}` reads
current approvals; GET the invoice's `/reviews` collection or a revision detail for
retained immutable history. Saved reviews are read-only. Results distinguish a
committed review from pending original publication with `storage_state` and
`storage_pending`.

POST `/documents/{id}/reparse` with the observed strict integer
`extraction_revision`; approval remains unchanged. POST its `/retry-storage` for
existing recovery. GET its `/source` downloads checksum-verified private original
bytes. Manual entries have no source PDF.

GET a document's `deletion_token` and echo it in `Deletion-Token` on DELETE
`/documents/{id}`. Invalid/misbound tokens are 400; changed revisions are 409.
Committed deletion returns 200 with `document_id`, `deleted: true` and
`cleanup_pending`. Pending journaled file cleanup remains a successful deletion
and is completed by storage recovery. Removed documents return 404; do not repeat
the mutation to trigger cleanup.

POST `/invoice-exceptions` confirms/reconfirms a missing vendor bill or service
allocation with vendor, optional service, month and required reason. After-review
confirmation includes the observed invoice ID/revision. Ordinary retries reuse the
existing confirmation. GET the collection/detail includes historical scopes;
PATCH changes only `reason`, preserving review bindings; DELETE removes a mistaken
confirmation and restores follow-up status from current configuration and receipts.
Confirmation, reason edits and deletion also accept `?dry_run=true` without persisting
changes or audit entries.

The [sequential example](api/example.py) demonstrates configuration, upload,
approval, correction and reports against disposable storage. It needs a human-issued
automation token and a deliberately chosen original; it does not auto-approve unknown
or incomplete values.

## Approved reporting and Grafana

`/reports/selectors` returns IDs/display names and classification choices without
private identification or matching rules. `/reports/summary` returns complete cost
and selected-consumption totals plus source/exclusion counts. Costs and consumption
use independent projections; electricity costs include every approved allocation,
while consumption selects network/supply before vendor/subcategory/date filtering.
Hiding network rows never promotes excluded supply to fallback.

Costs support `mode=rows`, `totals` and `invoice_month`. Consumption supports `rows`,
`totals` and `billed_period`; the latter retains complete endpoints, basis and source.
`/reports/consumption-issues` returns paginated excluded approved intervals.
`/reports/invoice-coverage` is automation-only and always includes the effective
UTC `as_of` date.

Report filters are repeated `property_id`/`vendor_id`, `category`, `subcategory`,
inclusive `date_from`/`date_to` and documented `mode`. An omitted property selection
means all properties. Cost dates are invoice dates; consumption includes whole
overlapping billed intervals without proration. Omitted bounds resolve from applicable
approved rows, with effective bounds returned; empty data infers no dates.

Aggregates return `mode`, `filters`, `effective_date_bounds`, `group_count`, `totals`
and complete `groups`, with no pagination links. Aggregation runs in SQL over every
matching source row in one read-only snapshot. Limits are 100 distinct selected IDs,
600 calendar months and 10,000 groups. Excessive filters fail with 422
`filter_too_broad`; they never return a partial total. Raw modes retain record pagination.
Currencies and units remain separate, and signed reversals remain signed.

Billing dates also have integer UTC-midnight Unix-second markers:
`invoice_time`, `invoice_month_time`, `period_start_time`, `period_end_time`.
Configure Infinity's backend parser against `groups`, its time columns as
**Time (UNIX s)**, and the dashboard timezone as UTC. Supply ISO date filters.
Scale money by 100 and usage by 1,000 only for presentation.

The [synthetic dashboard](api/grafana-dashboard.json) sums cost groups per property,
invoice month and currency, and signed water consumption per property, period end
and unit before plotting. Property IDs distinguish identical display names, and
points are sorted by UTC timestamp. It has property selection and date filters.
It is a transport verification example, not a deployment-specific dashboard.
For real use replace its URL and property options using `/reports/selectors`;
create an Infinity datasource
with `auth_method: bearerToken`, an allowlisted application host and the reporting
secret in `secureJsonData.bearerToken`. Grafana needs HTTP access only.

For regular Grafana image updates, retain its database and configured encryption key
(`GF_SECURITY_SECRET_KEY` when using the default encryption provider). With the default
SQLite database, mount persistent storage at `/var/lib/grafana`; see
[Grafana's Docker storage guide](https://grafana.com/docs/grafana/latest/setup-grafana/installation/docker/).
Save the reporting token in the datasource once. It remains usable after either
container is replaced when both applications retain their state.

Build the development and browser images, then run the disposable integration:

```sh
uv run --no-project --python 3.14 tools/check_api_grafana.py
```

The driver keeps disposable API tokens in memory and passes only their SHA-256 digests
to the API container. The sequential client receives its token through standard input;
Grafana stores its reporting token in encrypted `secureJsonData`.

It starts [isolated Compose services](../compose.api-check.yaml), seeds 1,101 invented
approved invoices, checks Infinity backend totals for single/multiple/all-property
selections, UTC month/period endpoints, live approvals/corrections, the sequential
client example and visible bounds failures, then removes only
that check project's containers and volume. It uses Grafana 13.2.4 and Infinity 4.1.6.
Installation Grafana provisioning is separate deployment work.
