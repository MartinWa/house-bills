# Configuration and the data model

Billing configuration lives in SQLite, separately from installation paths, secrets and
proxy settings. Startup migrations create the schema and preserve these records across
application restarts and image updates. Upload, document review, manual entry and
approved invoice screens are available; see [review and correction](review.md).

## Configure houses and vendors

Sign in with a staff account and open **Administration**. Superusers can maintain all
configuration; other staff need the relevant Django model permissions. Changes require
POST and CSRF protection.

1. Add each house under **Properties**, with a display name and unique storage slug.
   Slugs use lowercase letters, digits and single hyphens. `shared` and `inbox` are
   reserved for document storage.
2. Add a vendor, with a display name and unique safe slug. On the vendor form add its
   organization/VAT numbers under **Vendor identification**, select invoice formats
   and add **Property mappings**. Identification numbers identify the billing vendor and
   cannot belong to two vendors.
3. Select the vendor's **Invoice formats**. One vendor may use both `layout_01` and
   `layout_02`; vendors remain stable when a layout version changes. The installed
   registry supplies coexisting versions and implementation revisions; see
   [deterministic parsing](parsing.md). Configuration enables installed extraction
   rules; an unfamiliar structure requires review and a parser update.
4. Map installation or contract identifiers to each house. These keys are scoped by
   vendor and identifier kind. Installation and vendor identification numbers ignore
   spaces and hyphens; vendor numbers also normalize case. Contract keys preserve
   punctuation and case while normalizing whitespace. Delivery addresses normalize
   whitespace and case.

**Property mappings** also provides a separate mapping editor. One customer account
may cover both houses. Customer accounts remain source hints and cannot be configured
as property keys. A delivery-address mapping is used only when no mapped installation
or contract key exists. Conflicting specific keys require review. An explicitly reviewed
section assignment may correct a house for one invoice without changing saved mappings.

To remove a specific mapping on the vendor form, tick that row's **Delete?** checkbox
and save. This removes the mapping and preserves the property. Property selectors
are plain dropdowns; add, edit, view or delete properties from **Properties**.

When a vendor always bills one property, select its optional **Default property** in
the vendor form's **Property mappings** section, above **Specific mappings**.
Sections with no matching property mapping use this default, so you
can leave property mappings empty for that vendor. Installation/contract mappings take
precedence, followed by delivery-address mappings, then the vendor default. Conflicting
mappings still require review. Leave the default blank when each section needs a
specific assignment. Changing this setting does not alter previously approved invoices.

## Records and precision

| Record | Stored values |
| --- | --- |
| Property / Vendor | Display names and safe unique storage slugs; vendors also have an optional default property. |
| VendorIssuerIdentifier / VendorLayoutFamily | Private vendor identification numbers and compatible installed invoice formats. |
| PropertyIdentifier | A vendor-scoped installation, contract or delivery-address key mapped to one property. |
| Document | Unique SHA-256, original filename, relative private path, separate processing/storage state and messages, staging/publication journal paths, extraction counter and a private legacy fee archive. |
| ExtractionAttempt | Append-only candidate JSON, page evidence, layout family/version, parser revision, error/state and attempt number. Invalid and failed results remain private proposals. |
| Invoice | Document, vendor, reference/kind, dates, SEK, net/VAT/rounding/total/amount due, header evidence, supporting identifiers, source pages and approval metadata. |
| BillingSection | Invoice, section key, assigned property, service, source identifiers, explicit signed net/VAT/gross, overall billing period and period/tax/total evidence. |
| Usage | Section, source order, utility/unit, signed quantity, its own period, measurement basis and page evidence. |
| ReviewRevision | Append-only approved value snapshot, property assignments, vendor/kind, reviewer, time, source extraction and correction note. |

A shared invoice has one document and several independently assigned billing sections.
Usage inherits invoice, property and service through its section. Individual fees have
no normalized rows; their source text remains in the original PDF and extraction evidence.

Money uses signed integer minor units: `100` is one SEK. Quantities use signed integer
thousandths: `1000` is one kWh, m³ or kg. Floats, booleans and strings are rejected by the
model fields; number normalization must happen before persistence. SQLite constraints
reject non-integer stored values. Unknown amounts stay null, and approval requires
complete reconciled monetary values and an overall period for every allocation. Draft
periods may be unknown; a database constraint requires both dates or neither and rejects
empty/reversed periods. Negative credit totals and dated usage reversals retain their
signs. Billing and usage periods are start-inclusive/end-exclusive; usage may have
separate intervals within a longer service settlement. No monthly cost proration is implied.

Invoice identity is vendor + reference + kind. A partial unique index permits conflicting
documents to remain in review while allowing only one approved invoice for that identity.
The document hash and nonempty original path are unique. Ingestion reuses exact
duplicates; recognized vendor/reference/kind conflicts show both source documents for
review without changing approved allocations.

## Persistence services

`record_extraction()` appends a private proposal with page evidence and parser metadata.
Every successful extraction remains in review; errors can retain partial candidates.
Production parsing also records proposed vendor/property assignments and full
readable page evidence. Unknown, ambiguous and unreadable sources remain visible
review items even when their extraction attempt failed.
Re-parsing never updates approved allocations or usage.

`resolve_section_property()` checks saved mappings for each billing section and returns
its house, the vendor's default property when nothing matches, an unknown assignment
when no default is configured, or a validation error for conflicting keys.

`approve_review()` is the explicit review persistence boundary.
It requires an active authenticated reviewer and the expected review revision. It
validates the proposed headers and allocations, replaces normalized rows atomically,
records a snapshot and increments the revision. A stale request, conflicting approved
identity, invalid unit, unknown property or financial mismatch rolls back the entire
change, preserving previous approvals. A different amount due requires explicit
confirmation and is retained separately from invoice total. Manual proposals can be
approved without a supported parser.

The web workflow uses `approve_document_review()` to check the observed extraction
revision and invoice identity before calling `approve_review()` in the same short
transaction. A first manual invoice is created and approved atomically. Competing
submissions cannot create another bill for the same original. Signed form context
binds the document, target invoice, observed revisions and immutable source evidence.
Opening a review page or adding a form row does not write billing records.

Current parsers and the review workflow yield one invoice per document. Re-parsing
can propose a different reference/vendor/type for that invoice, while its approved
identity and values remain unchanged until accepted. Unchanged row evidence is
retained; manual/changed values carry an explicit manual-review marker, the selected
source page and an unlocated zero bounding box. Extraction attempts and earlier approved
snapshots retain original evidence.

Approval checks each allocation's net plus VAT against its gross total, then allocation
totals and rounding against the header. Printed VAT is retained without allocation to
individual fees. Usage units must match their utility and section service.
Repeated electricity usage for the same section/period/basis is prohibited; overlapping
periods with the same property, source service and basis require review. Network and
supply usage remain distinct. Usage reversals and replacements retain their separate
bases; canonical reporting selection is a later milestone.

Model saves perform validation and reject direct edits to approved allocations and
audit history. Row constraints protect low-level writes, but cross-row reconciliation
and revision history depend on the review service. Application code must use this
service for approved changes rather than bulk updates. PDF processing and filesystem
operations stay outside its short IMMEDIATE database transaction.
After approval commits, original publication runs under the shared storage lock;
failed moves preserve approved values and a recoverable publication journal.
See [uploads and private originals](ingestion.md).

Migration `0007_cost_totals` archives existing fee rows in the document's private,
non-editable `legacy_charge_details` field before dropping the old table. It retains every
fee field and derives the allocation's covering period from those rows, falling back to
usage periods. Approved invoice identities, totals, usage, extraction attempts and review
snapshots remain unchanged. Older snapshots are adapted in memory when displayed or
reviewed; their JSON is never rewritten. New approvals record service totals and usage.
Reversing the migration restores archived rows and represents newer allocations with
one service-total row for compatibility with the old schema.

Public tests use invented data. Candidate text, identifiers, source paths and approval
snapshots are private database content. Authenticated screens expose reviewed values
and private source PDFs; raw candidate JSON and approval snapshots have no export route.
