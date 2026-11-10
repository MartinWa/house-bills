# Parser candidate contract

The framework-independent types in `bills/parsing/types.py` describe extraction
candidates. These prototypes were validated locally against a multi-service invoice,
a shared electricity invoice and a water correction. That establishes the representative
results; private corpus replay additionally checks current and historical formats
against retained originals and approved reference values. Production routing and
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

`layout_01/v1` extracts contract-based water, waste, network and supply specifications, including
sections continuing onto another page and dated water reversals. It accepts the two
validated header arrangements. `layout_02/v1` extracts electricity supply specifications
with a separate installation on each billing page. Neither module contains vendor names
or private identifiers. Layout 01 v1 records implementation revision `8`; layout 02 v1
records revision `3`.
`layout_02/v2`, revision `2`, retains a separate replacement signature with synthetic
coverage. The registry selects by structure and keeps v1 available for older invoices.
`layout_01/v2`, revision `3`, additionally handles historical product-number sections. The
[parser guide](parsing.md) lists the other installed structures and revisions.

## Structure and precision

| Candidate value | Convention |
| --- | --- |
| Header | Invoice reference, invoice/due dates, SEK, net, VAT, rounding, invoice total and amount due, with field evidence. |
| Identifiers | Customer account is supporting evidence only. Each section retains its contract, product-number or installation keys; network sections also retain installation keys. Proposed property mapping is separate routing metadata. |
| Section | One service and contract/product/installation allocation, with optional waste cost `subcategory`, explicit signed net, VAT and gross totals, an overall billing period and period/tax/total evidence. Shared invoices retain multiple sections in one candidate. |
| Usage | Signed integer thousandths, unit, utility, period and basis: `measured`, `estimated`, `reversal`, `billed` or `billed_weight`. |
| Evidence | Original extracted text, page and bounding box. Treat the complete serialized result as private. |
| Review | `requires_review=True` for every result, including financially reconciled results. `issues` contains consistency failures. |

Money uses signed integer minor units: 100 units equal one SEK. Quantities use signed
integers at scale 1,000 for kWh, m3 and kg. Parsing uses Decimal, rejects overprecision
and never silently rounds a money/usage input. Rate and meter-reading text can retain
additional precision in evidence. Missing information is unknown, rather than zero.

Periods are start-inclusive/end-exclusive. Layout 01 uses reading boundaries directly;
layout 02's printed inclusive final day becomes the following date. Invoice dates,
reading dates and service periods remain distinct. Layout 01 retains the printed overall
service billing period. Layout 02 derives the covering period from its printed consumption
periods, falling back to other dated billing rows when consumption is invalid or absent.
Usage retains its own intervals, including dated reversals within a longer settlement.
The allocation period does not imply that all fees cover the same days or that its cost
can be prorated evenly. Calendar-month proration remains a later reporting decision.

Water price rows yield billed usage with explicit reversals and estimated/measured
evidence. Waste usage comes from billed kg rows; container litres, collection counts and
individual weight events do not become additional usage. Network usage comes from its
dated billed readings, retaining consecutive intervals through the end of the billed
period. Their summed consumption is checked against repeated energy price quantities.
Missing, invalid or ambiguous reading periods produce consumption warnings even when
the costs reconcile. Supply price rows must agree for each period; their repeated
quantities produce one usage record. Annual
estimates, historical charts and cumulative readings are excluded. Network and supply
remain separate sources; [approved reporting](reporting.md) selects network readings
first and uses supply as an explicit fallback.

Each billed waste usage row retains `subcategory`: `restavfall` or `matavfall`,
inferred from that row's explicit label. Container headers and individual weight
events do not override the billed row's type or add to its quantity. Other utilities
store an empty usage subcategory; electricity's Elnät/Elhandel distinction comes from
its service. Shared waste fees remain in the printed contract total.

Waste cost sections can use `waste_collection` or `tank_emptying`, separately from
consumption subcategories. Layout 01 labels waste collection from explicit billed rows;
Layout 03 identifies them from explicit billed collection
or tank-emptying items. A mixed total retains its amounts and an unclassified cost
subcategory, with a warning to split the allocations during review. Earlier candidate
and approval JSON remains immutable and is adapted in memory from explicit evidence.

## Financial reconciliation

Printed service net and VAT are authoritative. No per-fee VAT or gross is calculated,
and no fee description or amount is required. Price rows are read only where they supply
billed consumption and periods or corroborate network readings. The original PDF and
immutable extracted page evidence retain the source detail.

Net plus VAT must equal each service's cost including VAT. Service net and VAT must sum
to the header values; service costs plus explicit invoice rounding must equal the invoice
total. The combined printed waste subtotal is checked across its contract sections.
Invalid usage keeps independently extracted cost totals available for review.

The representative documents have no carried balance, so invoice total and amount due
coincide. An explicit invoice-total field is retained separately when present; a
difference needs review. Unsupported layouts require manual review. Missing or invalid
headers retain nulls, issues and independent candidate values. Section failures retain
previously extracted fields; ingestion preserves the original and readable page
evidence. Vendor recognition, structural matching, duplicate detection and immutable
attempt persistence are integrated. [Review forms](review.md) support manual correction
and explicit approval while preserving extraction evidence and earlier approved revisions.
