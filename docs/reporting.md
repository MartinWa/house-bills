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

**Missing invoices** checks one combined bill per vendor and scheduled invoice
month, bounded by tracked regular services and dated service vendor assignments.
Several properties or services on a bill do not multiply the invoice count.
Property and category filters select relevant services while keeping each vendor
bill counted once. Effective schedule dates and the first expected invoice month
are separate; dated frequency changes retain earlier expectations.

Approved invoices and pending documents match by vendor and invoice date,
independently of their allocation and consumption periods. Credits cannot satisfy
a regular expectation. Pending documents remain **Needs review**, including
unresolved allocations. Approved receipt is visible separately from
**Incomplete allocations**. Expand a bill's required services to inspect each
property/category/subcategory match. Occasional tank emptying is not required on
every bill and cannot satisfy a regular waste-collection check.

Missing bills become **Missing** after the scheduled month plus its stored grace
days. **Not sent this month** records a vendor-wide reason once. Existing scoped
annotations remain visible on their services. A received bill takes precedence
over a no-invoice annotation. An absent allocation still needs review; after
review of the approved invoice, an explicit service confirmation can excuse it
for that revision. Corrections invalidate that review binding while retaining its
history; a stale confirmation cannot excuse a missing bill. A later matching
allocation takes precedence.

Active regular services without vendors show **Needs vendor**. Assigned vendors
without applicable schedules show **Needs billing schedule**. Configuration links
lead to the appropriate editors. The service detail table also retains occasional,
ended and disabled activity and vendor history.

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

`reporting_invoice_coverage` exposes one combined vendor/month expectation with
schedule policy, required/received service counts, approved/pending receipt counts,
representative invoice/document IDs and status. `reporting_service_coverage`
provides the property, category, subcategory and assignment detail behind each
bill, including retained scoped annotations. Unassigned rows retain their service
ID; unscheduled assignments remain visible. Native views include months through
the current UTC month and use UTC status dates. The application can also inspect
explicit future month ranges without changing configured records.

Migrations `0019`–`0022` rebuild these projections for shared schedules and the
classification catalogue. Legacy pending waste sections still derive labels only
from explicit retained evidence. Existing values, including an explicit empty
subcategory, take precedence. Immutable proposals and approval snapshots are
interpreted in memory without rewriting their JSON. The
[SQL examples](reporting-examples.sql) include combined-bill counts and service
details for Grafana-style tables.

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
| `needs_review` | Unspecified electricity source, ambiguous source overlap or partial network coverage; excluded and shown on Reports. |

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
