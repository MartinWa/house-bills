# Approved reporting

**Reports** shows current approved service costs and billed consumption, with property
and date filters. Approval and correction appear on the next request. Proposals,
failed extraction attempts and earlier approval revisions do not contribute to totals.
Reports require login and are never cached. Invoices transcribed without an original
PDF contribute normally. A compact summary counts distinct approved invoices and
splits them into PDF and manual sources. Counts cover invoices represented in the
filtered costs, selected consumption or consumption issues; shared allocations do
not count an invoice more than once.

Report amounts and consumption display whole numbers, rounded after summing exact
stored units, with halves rounded away from zero. Invoice and review pages retain
the original precision, and the SQL views remain exact.

Costs use invoice dates. Consumption filters include the complete billed intervals
that overlap the chosen dates; selecting one day inside a monthly interval includes
that whole interval. No quantity or cost is prorated. Electricity source selection
uses all approved records before applying the report filters, including network bills
with a different invoice date.

## Missing invoices

**Missing invoices** compares received invoices with configured
[property services and vendor history](data-model.md#property-services-and-vendor-history).
It defaults to the current year and shows missing expectations or invoices needing
review. Filters select whole invoice months by property, vendor, service and status.
The expandable service table also shows as-needed services, vendor history and
ended/disabled configurations.

Each row checks one property service and its assigned vendor in an expected month.
One shared invoice may satisfy several rows, while several allocations from one
invoice count as a single receipt within each row. Statuses are:

- **Received**: a matching approved invoice exists.
- **Needs review**: a matching unapproved invoice or latest routed extraction exists.
  It counts as received without contributing costs or consumption to Reports.
- **Expected**: no matching invoice exists, but the month or grace period is still open.
- **Missing**: no matching invoice exists after the month and grace period have ended.
- **Not sent**: the vendor is confirmed not to have sent an invoice for this service
  and month. A saved reason explains the exception. It is excluded from the attention
  queue; a matching received invoice takes precedence if one arrives later.
- **Needs vendor**: the property needs a recurring service in this month, but no enabled
  vendor assignment covers it. It remains in the default attention queue.

Matches use invoice dates, rather than upload dates, payment due dates or consumption
periods. A credit invoice cannot replace a regular expected invoice. An approved
revision takes precedence over a newer extraction, including when the new proposal
changes its property or vendor. Unknown dates/vendors, unassigned properties and
unclassified services remain in Documents for review and cannot fill expectations.
Use the review links to resolve pending receipts. As-needed schedules produce no
missing invoices. Costs for absent invoices are never estimated.
Choose **Not sent this month** on a missing row to record a confirmed exception.
The invoice must still be missing when the action is saved; a repeated submission
retains the original reason. Exceptions do not change future billing expectations.

## SQLite contract, version 3

Migration `0008_reporting_views` creates five read-only SQL views in the application
database. They use native SQLite functions and work through an ordinary SQLite
connection, without Django functions or an export refresh. The migration adds indexes
for property/service and utility/period queries; applying or reversing it does not
change invoices, consumption, originals or private history.

Migration `0009_usage_subcategories` adds `category` and `subcategory` to the cost
and usage projections. Electricity costs use category `electricity`, with subcategories
`electricity_network` (Elnät) and `electricity_supply` (Elhandel). Migration
`0013_vendor_billing_schedules` extends waste cost subcategories with `waste_collection`
and `tank_emptying`; unclassified costs remain empty. Waste usage uses `restavfall`,
`matavfall` or an empty, unspecified subcategory. Other categories
retain their existing service keys and have no subcategory.

The Reports screen groups waste consumption by subcategory. Electricity consumption
keeps one combined total using the existing source preference; its source breakdown
shows Elnät and Elhandel. Group raw waste queries by `subcategory` when comparing types.

| View | One row represents |
| --- | --- |
| `reporting_invoices` | One currently approved invoice header. |
| `reporting_costs` | One approved property/service allocation, including its billing period. |
| `reporting_usage` | One signed billed consumption interval, retaining every source and basis. |
| `reporting_electricity_selection` | One raw electricity interval with its selection and reason. |
| `reporting_electricity_usage` | One electricity interval selected for combined consumption reporting. |
| `reporting_invoice_coverage` | One recurring property-service expectation and assigned vendor in an invoice month, or an unassigned expectation, with receipt counts and status. |

The views contain invoice/document IDs, references, vendor IDs/names/slugs, invoice
kind/date, review revision and approval time. Cost and usage rows also identify the
property by ID/name/slug and the allocation by `allocation_id`/`section_key`. They
exclude issuer/installation identifiers, source paths, extracted text, review
snapshots and user account details. The application database still contains private
configuration and login tables; these views do not create an external access boundary.

Migration `0014_invoice_coverage_view` adds coverage without changing the approved
reporting views' source rules. Migration `0015_property_services` anchors expectations
to property services and joins dated vendor assignments. Vendor changes preserve
earlier expectations; a transition month can have both vendors. Missing assignments
produce `unassigned` rows with null vendor/assignment IDs. Disabling a property service
suppresses all its expectations. Coverage includes expected months through the current
UTC month, `expected_time` at UTC midnight, `missing_after`, approved invoice and
pending review document counts, `property_service_id`, `assignment_id`, representative
invoice/review document IDs and status.
Migration `0016_service_invoice_exceptions` adds `exception_id` and the configured
`exception_reason`; a confirmed no-invoice month uses status `not_expected` (shown as
**Not sent**). Approved or pending receipts take precedence over this status.
Current status uses the UTC date. It is live and requires no export or refresh job.
Migration `0017_legacy_invoice_coverage` classifies pending waste receipts from older
snapshots using explicit retained period, fee or usage evidence. Mixed or unidentified
costs remain unclassified; an existing subcategory, including an explicit empty value,
takes precedence. The native view derives these labels without rewriting extraction
attempts, approval snapshots or approved allocations.
The [SQL examples](reporting-examples.sql) include a missing-invoice table suitable
for Grafana and a count of missing property/service expectations.

Invoice IDs persist through correction. Allocation and usage IDs change when the
review service replaces the current normalized rows. Query the current views afresh;
do not append their rows to a historical total using those changing IDs.

### Money and dates

- Money remains exact signed integer minor units: `net_minor`, `vat_minor` and
  `gross_minor` on costs, with `currency`. Sum integers first and divide by `100.0`
  only for presentation. Costs never join consumption, so multiple intervals cannot
  multiply an allocation's amount.
- `reporting_invoices` retains `rounding_minor`, `invoice_total_minor` and
  `amount_due_minor` once per invoice. Rounding is not assigned to a property/service;
  shared invoices still have one header. Amount due may include another balance.
- `invoice_date` determines cost reporting. Cost rows retain their allocation's
  `period_start` and `period_end` independently of consumption periods.
- `invoice_time`, `period_start_time`, `period_end_time` and `approved_time` are
  integer Unix seconds. Date-only timestamps represent UTC midnight on that date;
  they are billing-date markers. `approved_time` represents the actual UTC approval.
- Periods are start-inclusive and end-exclusive. Screens display the inclusive last
  billed day. Consumption time series should retain both endpoints; a start marker
  does not turn a billed interval into an instantaneous measurement.

### Consumption and electricity selection

`reporting_usage` provides `utility`, `unit`, signed integer `quantity_milli`,
`source_service`, `basis`, its independent period and `source_page`. Sum integer
quantities before dividing by `1000.0`; keep electricity/kWh, water/m3 and waste/kg
separate. Basis retains `measured`, `estimated`, `billed`, `billed_weight` and `reversal`.
An allocation without consumption contributes costs without inventing zero usage.

Use raw usage for water and waste. Use `reporting_electricity_usage` for combined
electricity consumption; summing raw supply and network readings would count the
same consumption twice. Both electricity services still contribute to costs.

| `source_selection` | Meaning |
| --- | --- |
| `network` | Selected network consumption. |
| `supply_fallback` | Selected supply consumption whose whole period is disjoint from network intervals. |
| `network_preferred` | Supply interval fully covered by network intervals; excluded from combined consumption. |
| `needs_review` | Ambiguous source overlap or partial network coverage; excluded and shown on Reports. |

Network coverage may be one interval or several consecutive readings. Gaps are checked;
matching the outer endpoints alone does not establish coverage. If network readings
cover only part of a supply interval, Reports retains the usable network readings and
shows the excluded supply interval for review. It does not infer the uncovered quantity.

Overlapping, uncancelled positive readings for the same property and source service
require review, including across invoices, vendors or different measurement bases.
Affected signed rows are excluded together; conflicting network rows also prevent an
overlapping supply fallback. Correct the affected invoices or remove duplicate
consumption through their review forms. Other costs and unambiguous intervals remain
available.

Exact signed corrections are retained. For overlap detection only, an equal negative
quantity cancels one positive with the same property, vendor, source service and exact
period, pairing older positive invoice dates/IDs first. Each negative can cancel at most
one positive. The original, negative and replacement rows all remain in the selected
series, so their integer sum is the corrected quantity. An unmatched negative remains
a signed credit; a partial reversal does not silently resolve overlapping positive
periods. No invoice history or source evidence is rewritten by reporting.

## Queries and read consistency

[reporting-examples.sql](reporting-examples.sql) contains executable examples for
invoice-month costs, combined electricity costs, water/waste intervals, canonical
electricity, excluded intervals and invoice reconciliation. The reconciliation query
aggregates allocations before joining headers, avoiding repeated invoice totals and
rounding. Add property/date conditions to the final queries, after source selection.
Check `needs_review` rows alongside consumption totals to see exclusions.

The Reports page opens the existing application database with SQLite `mode=ro` and
`query_only=ON`, then uses a DEFERRED read transaction for one consistent snapshot.
Normal IMMEDIATE approval writes can proceed in WAL mode while the report keeps its
previous complete snapshot. The next request sees the committed revision. The
application creates no separate reporting database and needs no refresh status.

Manual invoices without originals are included in costs and in the source counts;
the application determines them from `bills_document.source` while rendering, not from
a reporting-view column. Synthetic checks cover exact totals, multiple readings, shared
invoices, source fallbacks, gaps/overlaps, signed reversals and replacements, duplicate
files, corrections/rollback, reversible migration, manual entries and read consistency
during writes.
The Chromium suite checks the authenticated report workflow and mobile layout;
the production probe verifies the same reports through Waitress.

Grafana access transport is not implemented. The actual plugin, timestamp interpretation,
container mounts and process permissions remain to be tested before choosing direct access or
the separate reporting SQLite fallback. No Grafana connection is configured by this
application stage.
