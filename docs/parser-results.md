# Parser candidate contract

The framework-independent types in `bills/parsing/types.py` describe extraction
candidates. These prototypes were validated locally against a multi-service invoice,
a shared electricity invoice and a water correction. That establishes the representative
results; full current-format coverage remains later work. Production routing and
proposal persistence are described in [deterministic parsing](parsing.md).

`reader.read_pdf()` returns text lines with one-based page numbers and PDF-point
bounding boxes `(x0, top, x1, bottom)`. A four-point vertical tolerance keeps totals
together when labels and amounts have slightly different baselines. Reading does not
recognize vendors or assign properties. Call an installed parser explicitly:

```python
from bills.parsing import layout_01
from bills.parsing.reader import read_pdf

candidate = layout_01.parse(read_pdf("private/example.pdf"))
private_result = candidate.to_dict()
```

`layout_01/v1` extracts contract-based water, waste and network specifications, including
sections continuing onto another page and dated water reversals. It accepts the two
validated header arrangements. `layout_02/v1` extracts electricity supply specifications
with a separate installation on each billing page. Neither module contains vendor names
or private identifiers. Both currently record implementation revision `2`.
`layout_02/v2`, revision `1`, retains a separate replacement signature with synthetic
coverage. The registry selects by structure and keeps v1 available for older invoices.

## Structure and precision

| Candidate value | Convention |
| --- | --- |
| Header | Invoice reference, invoice/due dates, SEK, net, VAT, rounding, invoice total and amount due, with field evidence. |
| Identifiers | Customer account is supporting evidence only. Each section retains its contract or installation keys; network sections retain both. Proposed property mapping is separate routing metadata. |
| Section | One service and contract/installation allocation, with explicit net, VAT and gross totals. Shared invoices retain multiple sections in one candidate. |
| Charge | One signed net amount and its own service period. Derived VAT and gross amounts retain `vat_basis="allocated_from_section"`. |
| Usage | Signed integer thousandths, unit, utility, period and basis: `measured`, `estimated`, `reversal`, `billed` or `billed_weight`. |
| Evidence | Original extracted text, page and bounding box. Treat the complete serialized result as private. |
| Review | `requires_review=True` for every result, including financially reconciled results. `issues` contains consistency failures. |

Money uses signed integer minor units: 100 units equal one SEK. Quantities use signed
integers at scale 1,000 for kWh, m3 and kg. Parsing uses Decimal, rejects overprecision
and never silently rounds a money/usage input. Rate and meter-reading text can retain
additional precision in evidence. Missing information is unknown, rather than zero.

Periods are start-inclusive/end-exclusive. Layout 01 uses reading boundaries directly;
layout 02's printed inclusive final day becomes the following date. Invoice dates,
reading dates and service periods remain distinct. Each charge retains its source period,
so fixed monthly fees and a longer water settlement need not share a reporting period.
Calendar-month proration remains a later reporting decision.

Water price rows yield billed usage with explicit reversals and estimated/measured
evidence. Waste usage comes from billed kg rows; container litres, collection counts and
individual weight events do not become additional usage. Network usage comes from its
billed reading and is checked against repeated energy price quantities. Supply price rows
must agree for each period; their repeated quantities produce one usage record. Annual
estimates, historical charts and cumulative readings are excluded. Network and supply
remain separate sources; canonical reporting selection follows later.

## Financial reconciliation

The printed section VAT is authoritative. Rounded net line amounts may not reproduce
that tax exactly. The prototype accepts a difference from 25% of section net up to
`ceil(number_of_charge_lines / 2)` öre, preserving the printed evidence. This bound is
specific to these tested 25% layouts and does not authorize another tax rate.

Line VAT starts with Decimal half-up rounding. Any section difference is allocated by
the largest remaining fractional amounts, using stable source order for ties. Negative
charge and tax signs are preserved. Derived VAT is identified as an allocation rather
than printed per-line evidence. A discrepancy outside the bound leaves line VAT/gross
unknown and adds a review issue.

Net lines, derived line VAT/gross, section totals and header totals must reconcile
exactly. Service allocations plus explicit invoice rounding equal the invoice total.
The combined printed waste subtotal is checked across its contract sections.

The representative documents have no carried balance, so invoice total and amount due
coincide. An explicit invoice-total field is retained separately when present; a
difference needs review. Unsupported charges also require review. Missing or invalid
headers retain nulls, issues and independent candidate values. Section failures retain
previously extracted fields; ingestion preserves the original and readable page
evidence. Vendor recognition, structural matching, duplicate detection and immutable
attempt persistence are integrated. Explicit review forms remain a later milestone.
