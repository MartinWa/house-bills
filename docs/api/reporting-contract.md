# Coverage, human review and invoice costs

These additive GET endpoints live under `/api/v1`. Both Automation and Reporting-only
credentials can read them. They use the existing datasource and token, require no
consumer dependency upgrade or data migration, and retain contract version 1.
Grafana adoption and deployment belong to the independent consumer project.

| Need | Request |
| --- | --- |
| Missing count and oldest scheduled month | `/reports/missing-invoices?mode=summary&property_id=1&date_from=2026-01-01&date_to=2026-03-01` |
| Independent review statistics | `/reports/review-queue?mode=summary&property_id=1` |
| Combined attention table | `/reports/attention?property_id=1&coverage_date_from=2026-01-01&coverage_date_to=2026-03-01&limit=200&sort=-attention_date` |
| List of invoices and their complete costs | `/reports/invoice-costs?property_id=1&currency=SEK&date_from=2026-01-01&date_to=2026-03-01&limit=200&sort=-invoice_date` |

## Shared contract

Every response has `contract_version: 1`, UTC `generated_at`, resolved `filters`
and `effective_date_bounds`. Each response reads its facts, configuration, labels,
counts and amounts in one read-only transaction, with `Cache-Control: no-store`.
Coverage, review and attention also return `as_of`, defaulting to the server's UTC
calendar date. This sets age and overdue calculations on current facts; it does
not reconstruct past approvals or document availability.

Property and vendor IDs are repeated positive integer parameters. Omission means
all properties/vendors; empty, malformed and nonpositive values fail. Unknown
positive IDs produce empty results and never widen selection. Internally, `None`
means unrestricted and an empty selection yields no rows or statistics. A token
has shared-installation access; property selection is not authorization.

Row responses have `items`, complete filtered `count`, `next`, `previous` and
`has_more = (next != null)`. Default `limit` is 50, maximum 200; offset is nonnegative.
Mode defaults to `rows`; `summary` is complete and unpaginated on the three
coverage/review projections. Invoice costs only support rows and have no `mode` parameter.
Sorts use comma-separated allowlisted keys with optional `-` prefixes, followed
by a unique key unless already selected. Nulls follow known values in both directions.
Continuations preserve repeated IDs, mode and sort, and pin resolved `as_of` and
applicable date defaults. Each subsequent page reads a new snapshot, so concurrent
changes can affect it. Follow `next` to get every matching invoice.

Unknown parameters, repeated scalars, unsupported/empty/duplicate sort keys,
invalid dates or IDs, reversed supplied dates, and pagination/sorting on summary
return 422 `invalid_parameter`. Limits are 100 distinct selected property IDs,
100 vendor IDs where accepted, 600 months for bounded date windows, and 10,000
materialized coverage groups, review documents, final attention items or invoice
cost groups. Exceeding a guard returns 422 `filter_too_broad` before pagination,
with no partial totals. Review dates may be unbounded.

Money is signed integer minor units, with currency. Scale by 100 only for SEK
display. A contributing null makes an aggregate amount null. Zero is a recorded
value; empty results are no data. Proposal gross is net plus VAT, excluding rounding.

## Invoice expectations

`/reports/missing-invoices` accepts property/vendor IDs, `category`, `subcategory`,
`date_from`, `date_to`, `as_of`, `status`, `mode` and row pagination/sort. Supplied
date ordering is validated before both dates are normalized to their month's first
day. January 20 through March 10 selects all three scheduled invoice months.
Omitted bounds resolve independently to January of the `as_of` year and its month,
even if no invoice has arrived. December 9999 is outside the supported month range.

An expectation is one vendor/month bill, with key `vendor:<id>:month:<YYYY-MM>`.
An unassigned diagnostic uses `service:<property-service-id>:month:<YYYY-MM>`.
Shared properties and services count once. Whole-bill receipt eligibility precedes
projection of the selected service/property details. Receipt matching uses vendor
and invoice date, excludes credits, and retains dated schedules, frequency anchors,
optional/seasonal services, transitions and revision-bound no-invoice confirmations.

| Status | Meaning |
| --- | --- |
| `expected` | Bill has not arrived and its grace deadline has not been reached. |
| `missing` | No qualifying receipt on or after the grace deadline. |
| `needs_review` | Candidate documents exist without an approved receipt. |
| `received` | Approved receipt with complete or explicitly excused required allocations. |
| `incomplete` | Approved receipt exists but required allocations need a decision. |
| `not_expected` | Applicable no-invoice confirmation excuses the expectation. |
| `unassigned` | Tracked service needs a vendor. |
| `unscheduled` | Assigned vendor needs an applicable schedule. |

`status=all` is the default. A specific status or `attention` is supported;
attention selects missing, needs_review, incomplete, unassigned and unscheduled.
Optional service details also support `not_due`. Receipt status is separate:
an incomplete bill can still have `receipt_status=received`.

Summary and row statistics describe the same selected population before pagination:
`expectation_count`, `expected_invoice_count` (expected/missing/needs_review/received/incomplete),
`received_invoice_count` (including incomplete), `missing_invoice_count`,
`review_invoice_count` (bills, not documents), complete zero-filled `counts_by_status`,
and nullable `oldest_missing_month`. Missing is never expected minus received.
Counts for shared bills are not additive across separate property/service groups.

Rows include identity, vendor label, `expected_month`, UTC Unix-second
`expected_month_time`, status and label, receipt status, `missing_after`,
`days_overdue`, labelled selected properties/services, and separate
`approved_invoice_ids`, `approved_document_ids`, `pending_document_ids` and
`document_ids`. Singular invoice/document IDs are set only when unambiguous.
Service metadata contains IDs, category/subcategory keys and names, required/optional
classification, status and applicable confirmation reason. Configuration rules and
private document contents are excluded.

Service `confirmation_reason` is populated only when a confirmation excuses that
service (`status=not_expected`). Applicable scoped confirmations take precedence
over bill-wide confirmations; retained annotations that no longer apply are omitted.

The deadline is the following month's first day plus grace days. On the deadline
`days_overdue` is zero; it is `(as_of - missing_after).days` for missing bills,
and null for every other status. Actual `period_start`/`period_end` are nullable
and require one unambiguous source interval. Frequency never invents a billed period.
Sort keys: expected_month, vendor_id, vendor_name, status, missing_after,
days_overdue, expectation_key. Default: `-expected_month,vendor_name,expectation_key`.

## Review documents

`/reports/review-queue` accepts property IDs, `reason_key`, inclusive attention
dates `date_from`/`date_to`, `as_of`, mode and row pagination/sort. Omitted dates
are unbounded. Unknown invoice dates do not discard failed parses or manual entries.
One row represents one document in content-review or failed-parsing state,
including unfinished manual entries and approved invoices returned to review.
Pending/processing documents await the worker. Storage-only maintenance on a
completed document is outside this queue. Identical re-parsing still needs the
application's explicit completion decision.
Documents with several retained invoice records still produce one row using the
review workflow's current invoice.

Rows project the latest compatible extraction candidate, with nullable invoice ID,
vendor/name, invoice date/kind, net/VAT/gross and currency. `value_source=proposal`
means a candidate exists; `empty` means none. Missing fields never fall back to an
older approved amount. Known proposal and current approved property associations
are labelled `proposed`, `approved` or `both`; selected houses match either.
`routing_status` describes proposal properties as resolved, partial or unresolved.
Completely unassigned documents appear globally and do not inherit a guessed
property from their vendor.

Primary reason precedence and application-owned wording:

| Key | Reason |
| --- | --- |
| `manual_entry` | Complete the manual invoice entry. |
| `parse_failed` | Parsing failed; review the document. |
| `proposal_changes` | The latest proposal differs from the approved invoice. |
| `reparse_confirmation` | Confirm completion of the re-parsed invoice. |
| `vendor_unresolved` | Assign the invoice vendor. |
| `property_unresolved` | Assign the invoice properties. |
| `validation_required` | Validate the proposed invoice values. |
| `awaiting_approval` | Review and approve the invoice. |

`reason_details` adds stable keys and application wording, without extraction/error
text. `reason_key` filters the primary reason. `attention_since` is document creation
for initial/manual review or latest extraction time for an approved re-parse.
`age_days` is the nonnegative UTC calendar-date difference from `as_of`.
Summary has `review_document_count`, zero-filled primary `counts_by_reason`, and
nullable `oldest_attention_since`. Reason counts sum to distinct documents.
Sort keys: attention_since, age_days, invoice_date, vendor_name, reason_key,
document_id. Default: `-attention_since,document_id`.

## Combined attention

`/reports/attention` accepts property IDs, `as_of`, `coverage_date_from`/
`coverage_date_to`, `review_date_from`/`review_date_to`, mode and row pagination/sort.
It returns separate effective coverage month bounds and review attention-date bounds.
Review defaults to unbounded so an older backlog stays visible.

The server emits each review document once and attaches all its related selected
coverage expectations. If a visible queue document represents an attention
expectation, its separate coverage item is suppressed. Multiple candidates stay
multiple human tasks; the bill count stays one. Remaining expectations become
coverage-only items, including incomplete approved bills and configuration diagnostics.
Deduplication precedes count, global sort and pagination in the same snapshot.

Rows include `attention_key` (`document:<id>` or `coverage:<expectation_key>`), kind,
attention date, labelled properties/services, reason, related expectation keys,
nullable expected month, all related expected months, nullable actual billed periods,
invoice/document IDs and all related document IDs. Document dates are review attention
dates; coverage-only dates are expected months. A singular expected month is null
when several months are related. Summary/source statistics are `attention_count`,
`review_document_count`, `coverage_attention_count`, `missing_invoice_count` and
`oldest_missing_month`. They use different units and must not be added to derive the union count.

`review_path` is only present for an unambiguous document; coverage-only missing or
configuration items use `coverage_path`. Show Open review only for a returned path.
HTML links such as `/documents/3/review/` still require the operator's normal login.
Sort keys: attention_date, kind, reason_key, attention_key. Default:
`-attention_date,attention_key`.

## Invoice list with costs

`/reports/invoice-costs` accepts property/vendor IDs, category, subcategory, currency,
inclusive invoice-date bounds and pagination/sort. Omitted dates resolve from
applicable approved cost rows. It returns one complete invoice/currency row after
aggregating all matching approved allocations, then paginating invoice groups.
The count is invoices, not allocations. An invoice with more than 200 allocations
still has its complete cost in a single row.

Rows contain invoice/document identity, vendor ID/name, invoice date/time, kind,
review revision, document source, labelled selected properties, currency,
net/VAT/gross minor units and allocation count. These are the selected allocated
share, such as one house's part of a shared bill, rather than the invoice header
total. Allocation details are not returned. Use invoice date, vendor and gross
cost to display a simple invoice list. Manual source permits omitting a PDF link.
Credits remain signed; contributing nulls propagate independently through each
amount. Sort keys: invoice_date, invoice_id, vendor_name, currency, invoice_kind,
review_revision, net_minor, vat_minor, gross_minor. Default: `-invoice_date,invoice_id`.

## Consumer handoff and verification

[Synthetic examples and Infinity backend queries](reporting-examples.json) use
schema-validated invented records. Queries visibly reject unsupported contract
versions, preserve unknown cost wording, and report returned versus matching counts
and whether another page is available. The empty display says No matching records.
Invoice amounts are scaled for display only. Coverage shows expected month separately
from actual billed intervals; review links remain conditional on returned paths.

Regenerate with `python -m tools.generate_reporting_examples`, verify with `--check`,
and run `python -m tools.check_api_grafana` after building the repository's development
and browser images. Generation and application checks use the supported Linux image;
the HTTP-only integration driver can run on the editing host. No companion dashboard
is required. Existing reporting modes and the Automation-only invoice-coverage
adapter keep their contracts. Consumer edits, provisioning and deployment are separate.
