# Approved reporting analytics

Analytical GET responses under `/api/v1/reports` retain `contract_version: 1`
and add `analytics_version: 1`. Both Reporting-only and Automation tokens can
read them. They use current approved facts in one query-only SQLite snapshot,
with `Cache-Control: no-store`. `generated_at` is the read time. Independently
requested panels can observe different committed revisions.

The application owns selection, exact aggregation, coverage and qualification.
API consumers own their dashboard configuration, presentation and provisioning.
Invoice details, originals and review queues stay in the application.

## Calendar groups and optional work

| Endpoint | New calendar dimensions or modes | Date meaning |
| --- | --- | --- |
| `costs` | `group_by=invoice_year` | Inclusive invoice dates; the key is an integer year. |
| `consumption` | `group_by=period_start_month,period_start_year` | Whole intervals overlapping the inclusive range, assigned to their start month/year. |
| `billed-power` | `mode=totals`, `billed_period`, `period_start_month`, `period_start_year` | Whole overlapping billed periods; statistics rather than additive kW. |

Month keys are ISO first-of-month dates. Year markers are UTC January 1. The
corresponding `invoice_year_time`, `period_start_month_time` and
`period_start_year_time` fields contain Unix seconds at UTC midnight. Stored
intervals have an exclusive `period_end`; `last_billed_day` is the inclusive end.
A December–February water interval belongs wholly to December. No quantities
are distributed between months or prorated to the selected range.

Cost and consumption aggregates accept `representation=compact`,
`include_totals=false` and `include_quality=true`. Costs and summary also accept
`include_waste=false`. Totals and waste supplements default to included; quality
defaults to omitted. Turning off waste also avoids attaching weights to raw cost
rows. Pagination and sorting remain limited to raw rows. Compact representation
and the totals/quality options require an aggregate mode.

Compact groups contain requested dimensions, matching labels, exact metrics and
source counts. Unused detail fields and disabled supplements are omitted. An
unknown selected metric remains explicitly null. Empty `groups`, recorded zero
and null amounts have distinct meanings. Currency is mandatory for costs;
utility and unit are mandatory for consumption. Legacy requests retain their
default schemas and grouping.

Instead of explicit dates, costs, consumption and power accept `year=2024&years=3`.
This resolves to January 1, 2022 through December 31, 2024, independently of
today or the Grafana time picker. `years` defaults to 3 and supports 1–3; it
requires `year`. Focus years support 3–9998. Year windows cannot be combined with
either explicit date bound. All IDs, classifications, scalar repetitions and
boolean literals are validated strictly; booleans must be `true` or `false`.

Reports retain the 100-ID, 600-calendar-month and 10,000-group guards. Analytical
source materialization also has a 100,000-fact guard. Excessive requests return
422 `filter_too_broad`, never partial aggregates. Analytical coverage endpoints
reject December 9999 because its exclusive end cannot be represented.

Source counts are computed from facts within each group: `invoice_count` is
distinct invoices including credits, `allocation_count` is distinct allocations,
and `usage_count` counts usage rows. These counts are not interchangeable or
additive across overlapping subgroups. Money remains signed integer minor units;
quantities remain signed integer thousandths. Scale only for display.

## Availability and coverage

`availability?property_id=1` accepts property/vendor IDs, category and subcategory,
and describes all available approved history without date filters. It returns
`metrics` per house/utility/unit, `available_cost_years` and
`available_quantity_years`. Each metric includes configured-service status,
`has_costs`, `has_quantities`, cost years by invoice date, quantity years by period
start, actual history bounds and allocation/usage/exclusion counts. Electricity
has separate kWh and kW metrics. Waste costs remain available when no weights exist.

`quantity_status` is `recorded`, `excluded` or `not_recorded`. A configured service
does not establish an observation. Recorded quantities can be zero. For a filtered
aggregate, `include_quality=true` adds `quality.quantities` with a separate
`selected_status`: `recorded`, `excluded`, `empty_range` or `not_recorded`.
Historical observations outside the range establish `empty_range`, not a zero.

Quantity coverage contains actual contributing bounds, inclusive last billed day,
distinct interval count, usage count, duration extrema, basis/source counts,
estimate/reversal counts and unmatched-credit count. `covered_days`, inclusive
`gaps`, `surviving_basis` and `status` describe physical continuity after exact
reversal cancellation. Original signed rows still contribute to quantity sums
and raw basis counts. Status is `complete`, `partial`, `empty` or `unknown`;
without a requested coverage window it is not inferred from outer endpoints.
`exclusion_count` counts excluded electricity usage rows, not distinct intervals.

Costs/summary quality also includes actual contributing invoice-date bounds,
`unknown_cost_count` and a separate `invoice_coverage` aggregate. This uses dated
vendor bill expectations and current receipt/confirmation facts, with `as_of`,
expectation count, status counts and missing bill count. Complete means every
selected expectation is received or explicitly not expected; absent schedules
or assignments leave coverage unknown. Receipts must fall within the exact inclusive
invoice-date bounds; bills before the start or after a comparison cutoff cannot
complete the selected expectations. It does not reconstruct historical
approval or document availability and does not establish quantity continuity.

Compact consumption groups with quality also carry their own `coverage`,
`period_label` and `basis_summary` for tooltips. These describe the contributing
whole intervals in that bucket, including long intervals and signed corrections.

## Complete billed-power statistics

Power groups retain house and kW boundaries, and expose `highest_milli`,
`lowest_milli`, rounded `average_milli`, surviving `value_count`, raw `usage_count`,
distinct invoice/allocation counts, `cancelled_positive_count`,
`unmatched_credit_count` and `excluded_count`. Highest values include their billed
period start and inclusive last billed day. Empty groups are no data.

The API uses the application's native arithmetic mean and exact credit pairing.
For values 5, -5, 3 and 0 kW in one period, statistics retain 3 and 0: highest 3,
lowest 0, average 1.5 kW. An unmatched negative is counted and excluded from
statistics; explicit zero survives. The original and credit remain raw facts.
Aggregates evaluate every matching row and are independent of the raw 200-row cap.

## Equivalent-period comparisons

`comparisons?property_id=1&metric=costs&year=2024` returns qualified comparisons,
annuals and monthly cells for the focus year and its two predecessors.
`metric=consumption` keeps separate house/utility/unit/fraction groups; selected
electricity sources combine as kWh. `comparison_year` defaults to the focus year's
predecessor and must differ from it. It supports 1–9998. `history=true` includes
every available annual year, subject to the history breadth guard, while retaining
the focus year and its two predecessors even when they have no observations. The
monthly grid always retains those three recent years. Explicit dates
can replace a focus year; their end year determines the default focus.

The current cutoff is the earlier of the requested end and the latest contributing
invoice date (costs) or surviving inclusive billed end (quantities). Both window
bounds are shifted by the year difference for the baseline; February 29 becomes
February 28 where necessary. Responses expose both starts, both cutoffs, values,
coverage objects, `comparable`, `status`, stable `reasons` and readable
`reason_labels`.

Absolute changes require both observations, known amounts and complete coverage.
Quantity comparisons additionally require matching surviving bases, no unmatched
credits or excluded consumption, and no whole interval crossing a comparison
boundary. Matching estimated bases can be compared, with that basis retained.
Unavailable comparisons have null deltas. A comparable zero/negative baseline can
have an absolute change but no percentage; `percentage_reasons` explains why.

Annual totals use invoice year or whole-interval start year, with actual
contributing bounds and coverage status. They can be partial. Monthly cells label
`state` as `recorded`, `unknown` or `absent`. Cost
`cumulative_gross_minor` accumulates signed adjustments only while every preceding
month has a known observation. An absent month or unknown amount makes that and
all subsequent cumulative cells null. Earlier history and missing months are never
filled with invented zeros.

## Period-aligned effective unit costs

`unit-costs?property_id=1&category=electricity&date_from=2024-01-01&date_to=2024-12-31`
uses `alignment_policy: whole_periods_no_proration_v1`. It independently aggregates
approved gross cost allocations and selected usage, then establishes a relationship
between their whole billed periods. Invoice dates do not determine this numerator.
One allocation with several readings contributes its cost once.

Omitted date bounds resolve from known billed cost periods and selected quantity
and exclusion periods. Invoice dates do not determine these bounds. Cost-only
services and periods remain visible when quantities are missing.

Groups retain house, utility/unit, currency and applicable waste fraction. They
include aligned exclusive bounds/inclusive last day, `gross_minor`,
`quantity_milli`, decimal-string `rate_minor_per_unit`, included service keys/names,
source counts, quantity coverage and explicit numerator/denominator descriptions.
Divide the rate by 100 to display SEK per kWh, m³ or kg. Electricity's effective
average includes selected gross network, supply, fixed, demand and other charges;
it is an average billed cost, not a marginal tariff.

A rate requires complete continuous period alignment for every included service,
no ambiguous overlaps, known cost periods and amounts, a positive quantity,
non-estimated surviving quantities, no unmatched quantity credits/exclusions and
whole intervals within the requested boundaries. Water and separately billed waste
fractions additionally require matching vendor period coverage. Vendor names alone
do not establish alignment. Exact duplicate interval bounds can contain multiple
readings or cost components without multiplying allocations.

Known cost periods are selected by overlap. A filtered allocation without an
approved period cannot safely be ruled outside the range using its invoice date;
it is conservatively included and makes the rate unavailable. Reasons identify
unknown periods/costs, gaps, overlaps, estimated quantities, missing observations,
nonpositive quantities or unestablished relationships. Unavailable groups retain
the independent known facts and a null rate.

Combined collection costs remain combined; they are never allocated to weights.
Only approved separately billed catalogue fractions can establish a cost/kg
relationship. A weight without a matching cost has `no_costs`; collection or tank
emptying without weight has `no_quantities`. Both remain useful cost/quantity
results in the Utility analysis dashboard.

## Consumer verification

[OpenAPI](openapi.json) publishes the query parameters and analytical schemas.
[Synthetic reporting examples](reporting-examples.json) have an
`analytics_examples` section alongside the existing paginated projections. Regenerate
with `python -m tools.generate_reporting_examples` in the Linux development image;
verify with `--check`. Validate the documented dashboard with the
[Grafana verification](../api.md#grafana-verification) procedure. Keep consumer-specific
configuration, generators and test scenarios with the consumer.

Within a snapshot the API reuses canonical electricity selection. House constraints
can enter its source CTE because all reversal and overlap partitions are
house-local. Date, vendor and classification filters apply after source selection.
Filtering therefore cannot promote an excluded supply interval to fallback.
Bounded reports restrict the cached output; historical quality/availability retains
all relevant dates. Supplying both bounds skips inference queries.
