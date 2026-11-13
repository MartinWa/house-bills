# Deterministic parsing and review proposals

Every newly stored PDF is parsed synchronously by the shared ingestion service.
Reading, vendor recognition, layout matching, section extraction, property mapping
and reconciliation are separate operations. PDF work runs outside database write
transactions and creates private proposals. Approval remains explicit through
[review and correction forms](review.md).

Open **Documents** and select an upload to see its **Extracted proposal**: vendor,
reference, dates, amounts, property/service allocations, signed billed usage and
review issues. This read-only summary also shows existing attempts. Usage periods
show their final billed day; stored periods remain end-exclusive.
Choose **Review invoice** on document detail to correct missing or uncertain fields, assign
properties and approve validated records. Unsupported sources have the same manual
entry path. The form requires acknowledgement of extraction warnings and validates
the replacement values before approval.

## Configuration and versions

Configure vendor identification, compatible invoice formats and invoice property rules
in Administration; see [configuration](data-model.md). Recognition uses labelled
organization/VAT cover fields and legal identity in a bounded F-skatt footer, including
wrapped cover footers above a payment slip. Recipient addresses, filenames, customer accounts and arbitrary numbers in
service specifications do not identify the vendor. Conflicting vendors need review.

The registry in `bills/parsing/registry.py` retains coexisting versions and their
implementation revisions. A recognized vendor's compatible families limit structural
matches. With an unknown vendor, an unambiguous installed structure can still yield
fields without vendor or property assignments. Unknown or multiple matching versions
remain review proposals. Dates and filenames never select the newest parser.

| Family/version | Billing structure | Revision |
| --- | --- | --- |
| `layout_01/v1` | Contract-based water, waste, network and supply; page continuations, explicit garden/fraction cost blocks and dated water reversals. | `9` |
| `layout_01/v2` | Historical product-number water, network and supply, including printed `kbm` water units and explicit waste cost labels. | `4` |
| `layout_02/v1` | Electricity supply; installation details above each specification. | `3` |
| `layout_02/v2` | Replacement supply structure; service-specific title and inline installation key. | `2` |
| `layout_03/v1` | Quarterly waste or dated tank emptying; VAT basis and separate payment summary. | `2` |
| `layout_04/v1` | Electricity supply; cover totals and one installation specification. | `1` |
| `layout_05/v1` | Electricity supply; explicit consumption-period table beside a historical chart. | `2` |
| `layout_05/v2` | Electricity supply; printed billed consumption in a meter-reading table beside a chart. | `1` |
| `layout_06/v1` | Network and energy tax; repeated billed kWh quantities and inclusive VAT prices. | `4` |
| `layout_06/v2` | Network and energy tax; exclusive VAT prices or dated price changes with separate tax rows. | `3` |
| `layout_07/v1` | Electricity supply; area-labelled consumption and inclusive VAT totals. | `1` |
| `layout_08/v1` | Compact electricity supply summary with printed consumption, total, VAT and rounding. | `3` |

The v1 families have representative source validation and wholly synthetic regressions.
The historical v2 structures and `layout_08` also have private corpus validation;
`layout_02/v2` demonstrates coexistence using synthetic examples. This does not
cover every historical invoice or future layout change. Spacing, case, title punctuation
and period dash changes preserve a version when its structural rules still match.
Normalization retains the original extracted text and coordinates as evidence.

The historical v2 structures are selected by their product-number, meter-table or
price-table signatures; they coexist with v1 and do not route by invoice date. Legacy
network readings retain column order and exact grouped quantities. Network price rows
are checked against readings within their own printed periods. When a contract has no
overall period label, dated fee rows supply its cost period; billed waste weights keep
their separate collection intervals. Undated water reversals remain review issues.
Waste extraction keeps combined charges combined, independently of Matavfall and
Restavfall weight lines. Supported contract/product blocks classify explicit garden
costs and unambiguous named fraction cost blocks using their own reported totals.
Annual collections, container volumes and collection counts are not kg usage.
A cost section needs no weight; reported zero kg is retained. Activating Plast,
Kartong or Glas only adds review choices. Their future layouts need explicit parser
support; activation does not divide existing combined costs or generate quantities.
Network and tax rate changes can use different dates; quantities reconcile between
boundaries shared by both tables. A printed price-effective date may precede its billed
period, but dates after the period and invalid dates require review. Dated price tables
select v2 even when the other table retains a plain inclusive VAT heading.

The compact supply parser handles missing word spaces only within its own structure.
It derives net cost by subtracting printed VAT and rounding from the specification total,
without calculating tax from fee rates. It retains one billed consumption and excludes
annual estimates and chart values. Unknown or conflicting footer identities remain
unresolved; legal footer matching requires its F-skatt anchor and nearby identifiers.
The cover payment amount remains separate from the specification total; a disagreement
requires review, and a missing specification total leaves costs unknown.
The specification total may be inline or in a nearby fragment to the right of its
label, including larger figures whose top coordinate differs from the label.

The added single-installation formats flag multiple identifiers/specifications for review.
Quarterly waste retains its service total and billing period without converting collections,
container litres or annual price quantities into weight consumption. Supply parsers
distinguish billed consumption from dated meter readings and charts; the chart format
requires its explicit consumption table. Monthly network combines network and energy-tax
costs while retaining repeated billed kWh once. Signed corrections and invalid quantity,
money and period fields remain covered by synthetic checks.

`layout_06` locally translates six checked Swedish character codes from its source font.
This translation is bounded to that parser and preserves untouched evidence. Unknown
codes in consumption require review; general encoding repair and OCR are not included.

Each section resolves its own installation, contract or product-number keys, then an
explicitly labelled delivery address, then the vendor's optional default property. Conflicts remain
unresolved. Network sections use local installation keys; document-wide fallback
requires exactly one distinct installation. Shared customer accounts never assign
all sections to one house. Proposed property IDs are snapshots; approval validates
current mappings or explicit reviewed assignments again.

## Proposals and failures

`ExtractionAttempt` stores layout/version/revision, candidate fields, all readable
page lines and private `routing` metadata: vendor, issuer evidence, layout matches,
invoice kind and proposed section properties. Missing values are null. Independent
headers and previously extracted sections survive invalid fields or parser errors.
Parsers retain service cost totals and overall billing periods without extracting
individual fee amounts or allocating their VAT. Numeric precision, units, periods and
allocation/header reconciliation require validation. Repeated
supply price quantities yield one usage per period; network usage comes from its
dated billed readings. Multiple readings retain consecutive periods, and their total
must agree with the billed kWh. Incomplete or ambiguous readings are identified as
consumption warnings. Overlapping usage for the same property/source needs review.
See [the result contract](parser-results.md) for quantity and tax conventions.

Every attempt leaves the document in **Needs review**. Unknown or unreadable sources,
ambiguous routing and unexpected parser errors also record failed attempts with safe
reasons. Invalid extracted fields retain candidate issues. Original bytes and checksums
are preserved. Parsing creates no approved allocations or usage; the approval service
rejects unresolved issues and invalid allocations. Recognized different-file invoice
identities become review records and expose source conflicts.

Damaged, textless, password-protected PDFs and PDFs exceeding 100 pages require
review. General OCR and encoding repair remain later work. Parsing runs synchronously
within the bounded upload; NAS timing has not been measured.

## Retry and selected re-parsing

Exact duplicates retain their existing attempt, including failures and approved edits.
A per-document process lock serializes initial parsing. Storage recovery and re-upload
resume initial processing when an interruption left no attempt. Database errors retain
the original for retry. Backups use the shared storage lock in [ingestion](ingestion.md).

After changing configuration or installing a parser update, explicitly re-parse
selected retained document IDs:

```sh
docker compose -f compose.development.yaml exec -T app python manage.py parse_documents 12 19
```

For production use the production Compose service. Each document gets a new immutable
proposal, preserving approved allocations, review history and published originals.
The command requires existing IDs and recoverable original storage. The document page
also offers **Re-parse document**. Repeated submissions of that same re-parsing form
cannot append another attempt after its observed extraction revision changes.

For an approved invoice, **Proposed differences** compares headers, identifiers,
properties, services, allocation totals/periods and consumption with a newer extraction.
**Review invoice** starts with the retained approval; **Load latest extraction** inside
review starts with the separate proposal when it has changes awaiting review.
Only saving a valid correction accepts its values.
Both review and extraction revisions are checked when saving, so an older form cannot
replace newer work. Re-parsing a changed invoice identity preserves one current invoice
for the original and leaves approved values intact.

## Maintenance

Keep earlier versions installed when adding a replacement. Increment the implementation
revision for bug fixes and add wholly synthetic regressions. Tests cover cosmetic,
replacement, unsupported and older layouts, partial failures, shared sections, units,
usage, property conflicts, recovery and preserved approvals. The production probe
uploads a synthetic bill through Waitress without development test dependencies.
Real PDFs, expected values and extraction evidence remain private.

Built-in modules declare `FAMILY`, `VERSION` and `REVISION`, implement structural
`matches(pages)` and return a `Candidate` from `parse(pages, *, candidate=None)`.
An optional `read_header(pages)` extracts format-specific header fields before section
parsing so those fields survive a later parser failure. Use the existing candidate,
section, identifier, usage and evidence types; see [the result contract](parser-results.md).
Register built-in versions in `bills/parsing/registry.py`, rebuild the image and explicitly
re-parse selected documents. Follow [contributing](../CONTRIBUTING.md) for fixtures/checks.

Loading modules from a mounted directory is not implemented. Such modules would execute
with application privileges; independently authored plugins may retain their own licenses,
as described in [NOTICE](../NOTICE) and [security expectations](../SECURITY.md).
