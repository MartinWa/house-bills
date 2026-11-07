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
Choose **Review and approve invoice** to correct missing or uncertain fields, assign
properties and approve validated records. Unsupported sources have the same manual
entry path. The form requires acknowledgement of extraction warnings and validates
the replacement values before approval.

## Configuration and versions

Configure vendor identification, compatible invoice formats and property mappings
in Administration; see [configuration](data-model.md). Recognition uses labelled
organization/VAT cover fields and legal identity in a bounded bottom-page F-skatt
footer. Recipient addresses, filenames, customer accounts and arbitrary numbers in
service specifications do not identify the vendor. Conflicting vendors need review.

The registry in `bills/parsing/registry.py` retains coexisting versions and their
implementation revisions. A recognized vendor's compatible families limit structural
matches. With an unknown vendor, an unambiguous installed structure can still yield
fields without vendor or property assignments. Unknown or multiple matching versions
remain review proposals. Dates and filenames never select the newest parser.

| Family/version | Billing structure | Revision |
| --- | --- | --- |
| `layout_01/v1` | Contract-based water, waste subcategories and network; page continuations and water reversals. | `5` |
| `layout_02/v1` | Electricity supply; installation details above each specification. | `3` |
| `layout_02/v2` | Replacement supply structure; service-specific title and inline installation key. | `2` |

The v1 formats have representative source validation. The v2 replacement has
synthetic regression coverage and does not establish additional real-format coverage.
Full current-format coverage remains a later milestone. Spacing, case, title
punctuation and period dash changes preserve v1 when its structure still matches.
Normalization retains the original extracted text and coordinates as evidence.

Each section resolves its own installation/contract keys, then an explicitly labelled
delivery address, then the vendor's optional default property. Conflicts remain
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
review. OCR and encoding repair remain later work. Parsing runs synchronously within
the bounded upload; NAS timing and background execution decisions remain later work.

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
properties, services, allocation totals/periods and consumption with the latest extraction. **Correct
approved values** starts with the retained approval; **Review extracted changes**
starts with the separate proposal. Only saving a valid correction accepts its values.
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
