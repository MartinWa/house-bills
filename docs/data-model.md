# Configuration and the data model

Billing configuration lives in SQLite, separately from installation paths, secrets and
proxy settings. Startup migrations create the schema and preserve these records across
application restarts and image updates. Upload, document review, manual entry and
approved invoice screens are available; see [review and correction](review.md).

## Configure houses and vendors

Sign in with a staff account and open **Administration**. Superusers can maintain all
configuration; other staff need the relevant Django model permissions. Changes require
POST and CSRF protection.

The billing lists are grouped in both the Administration dashboard and sidebar:
**Property configuration** (Properties, Property services, Service vendors),
**Vendor configuration** (Vendors, Vendor billing schedules, Invoice property rules),
**Classification** (Subcategories), and **Invoice follow-up** (Months with no invoice).

1. Add each house under **Properties**, with a display name and unique storage slug.
   Slugs use lowercase letters, digits and single hyphens. `shared` and `inbox` are
   reserved for document storage.
2. Add a vendor, with a display name and unique safe slug. On the vendor form add its
   organization/VAT numbers under **Vendor identification**, select invoice formats
   and add **Invoice property rules**. Identification numbers identify the billing vendor and
   cannot belong to two vendors.
3. Select the vendor's **Invoice formats**. One vendor may use both `layout_01` and
   `layout_02`; vendors remain stable when a layout version changes. The installed
   registry supplies coexisting versions and implementation revisions; see
   [deterministic parsing](parsing.md). Configuration enables installed extraction
   rules; an unfamiliar structure requires review and a parser update.
4. Map installation, contract or product-number identifiers to each house. These keys
   are scoped by vendor and identifier kind. Installation and vendor identification
   numbers ignore spaces and hyphens; vendor numbers also normalize case. Contract
   and product keys preserve punctuation and case while normalizing whitespace.
   Delivery addresses normalize whitespace and case.

**Invoice property rules** also provides a separate mapping editor. One customer account
may cover both houses. Customer accounts remain source hints and cannot be configured
as property keys. A delivery-address mapping is used only when no mapped installation,
contract or product key exists. Conflicting specific keys require review. An explicitly
reviewed section assignment may correct a house for one invoice without changing saved mappings.

To remove a specific mapping on the vendor form, tick that row's **Delete?** checkbox
and save. This removes the mapping and preserves the property. Property selectors
are plain dropdowns; add, edit, view or delete properties from **Properties**.

When a vendor always bills one property, select its optional **Default property** in
the vendor form's **Invoice property rules** section, above **Specific rules**.
Sections with no matching property mapping use this default, so you
can leave invoice property rules empty for that vendor. Installation, contract and product
mappings take precedence, followed by delivery-address mappings, then the vendor default. Conflicting
mappings still require review. Leave the default blank when each section needs a
specific assignment. Changing this setting does not alter previously approved invoices.

## Property services and vendor history

Use **Properties** as the entry point for the services a property needs. Each
**Property service** has a category, optional subcategory, regular/occasional mode,
enabled flag and inclusive first/last tracking months. Existing first expected
months migrate as tracking bounds; they do not establish an earlier physical
service start date. Separate dated service records retain gaps and disabled periods.

**Service vendors** attach vendors to those services for inclusive assignment
months. The first assignment defaults to the service's tracking dates, with
overrides available. Several vendors may bill in a transition month; enabled
assignments for the same vendor and service cannot overlap. Use **Change vendor**
from the assignment editor to end the old assignment and create its replacement.
The service keeps its identity, tracking dates and classification.

Maintain **Vendor billing schedules** on **Vendors**, or in the filtered schedule
editor. A vendor has one shared schedule history: effective first/last months,
frequency, first expected invoice month, grace days and enabled flag. Frequencies
are monthly, every two months, quarterly, every six months, yearly and as needed.
The effective first month and first expected invoice month are independent. A
quarterly period effective in July with its first invoice in August expects August,
November, February and May. An anchor may fall after the effective last month for
a short period that produces no invoice. Enabled schedule periods cannot overlap.

Use **Change billing frequency** from a schedule editor to end its current period
and create its dated replacement. Earlier months continue to use their original
frequency, anchor and grace policy. Property services and service vendors keep
their identities. A replacement vendor uses its own schedule and invoice anchor.

Regular services participate in allocation checks on scheduled combined bills.
Occasional services, including tank emptying, remain available without becoming
required on each bill. A vendor billing entirely as needed creates no recurring
invoice expectation. An active regular service without a vendor appears as
**Needs vendor**; an assignment without a schedule appears as **Needs billing schedule**.

One scheduled vendor/month produces one combined-bill expectation across its
properties and services. Receipt and allocation completeness remain separate. A
water-only bill from a vendor configured for water and waste collection needs
review for the missing waste allocation. Tank emptying cannot fill the collection
requirement. See [missing invoices](reporting.md#missing-invoices).

Choose **Not sent this month** once for a missing combined bill, with a required
reason. Existing service-specific annotations retain their original vendor,
service, month and reason through later configuration changes. New annotations
or changes to their scope must match the current schedule and service configuration.
A later receipt takes precedence. After reviewing an approved bill with an absent
service, use **Confirm service not sent after review**
on that allocation and explicitly confirm the review. This binds the annotation
to the approved invoice revision. A subsequent correction requires a new review.
The stale confirmation remains as history and cannot excuse a missing bill.
A later explicit allocation takes precedence. Manage annotations through
**Months with no invoice**, also available on each service form.

Configuration forms, direct model saves and configuration actions share validation.
Administration checks the parent and all proposed inline rows before committing
atomically. Month boundaries, date order/containment, enabled overlaps,
classification compatibility, normalized identifiers and exception scope are
validated. Database constraints and triggers also protect row invariants,
classification relationships and enabled overlaps. End periods to preserve earlier
activity; disabling a service suppresses its expectations.

## Categories and subcategories

The fixed categories are **Electricity**, **Water**, **Waste** and **Other**.
Maintain their shared **Subcategories** catalogue in Administration. Initial keys
are `electricity_network`, `electricity_supply`, `matavfall`, `restavfall`,
`tank_emptying` and `waste_collection`. Add entries such as Plastic or Cardboard
without changing the schema. Category selectors filter available subcategories.
Empty subcategories preserve combined or unspecified classification.

Each subcategory has a stable key, editable name and enabled flag. Renaming updates
display labels. Disable obsolete entries to keep existing references. Saved keys
are immutable; referenced entries cannot be deleted or moved to another category.
New entries supply classification choices; they do not add PDF extraction rules.
Parsers continue to classify only explicit source evidence.

Cost allocations and consumption rows choose subcategories independently within
the allocation's category. One Waste / Waste collection cost may contain both
Food waste (Matavfall) and Residual waste (Restavfall) quantities. Keep the printed
cost together when the source supplies no reliable monetary split. Explicit
separate monetary allocations retain their classifications. Electricity network
and supply remain distinct consumption sources with the existing selection that
avoids counting energy twice. Unspecified consumption sources require review before
selection, even when their cost is classified as network or supply.

## Records and precision

| Record | Stored values |
| --- | --- |
| Property / Vendor | Display names and safe unique storage slugs; vendors also have an optional default property. |
| VendorIssuerIdentifier / VendorLayoutFamily | Private vendor identification numbers and compatible installed invoice formats. |
| InvoicePropertyRule | A vendor-scoped installation, contract, product-number or delivery-address key mapped to one property. |
| Subcategory | Stable key, fixed category, editable name and enabled flag. |
| PropertyService | Property, category, optional catalogue subcategory, regular/occasional mode, enabled flag and tracking months. A private immutable legacy billing configuration supports migration recovery. |
| VendorBillingSchedule | Vendor, effective months, first expected invoice month, frequency, grace days and enabled flag. |
| ServiceVendorAssignment | A vendor attached to a property service for inclusive assignment months, with an enabled flag. |
| ServiceInvoiceException | Vendor/month/reason, optional property-service scope and optional reviewed invoice/revision binding for an absent allocation. |
| Document | Source (`upload` or `manual`), unique SHA-256 for uploads, original filename, relative private path, separate processing/storage state and messages, staging/publication journal paths, extraction counter and a private legacy fee archive. |
| ExtractionAttempt | Append-only candidate JSON, page evidence, layout family/version, parser revision, error/state and attempt number. Invalid and failed results remain private proposals. |
| Invoice | Document, vendor, reference/kind, dates, SEK, net/VAT/rounding/total/amount due, header evidence, supporting identifiers, source pages and approval metadata. |
| BillingSection | Invoice, section key, assigned property, category and optional catalogue subcategory, source identifiers, explicit signed net/VAT/gross, overall billing period and period/tax/total evidence. |
| Usage | Section, source order, utility/unit and optional catalogue subcategory, signed quantity, its own period, measurement basis and page evidence. |
| ReviewRevision | Append-only approved value snapshot, property assignments, vendor/kind, reviewer, time, source extraction and correction note. |

A shared invoice has one document and several independently assigned billing sections.
Usage inherits invoice and property through its section, and must use its category. Individual fees have
no normalized rows; their source text remains in the original PDF and extraction evidence.

Migrations `0019`–`0022` introduce the shared classification catalogue, rename
invoice property rules and move schedule ownership to vendors. Their preflight
rejects conflicting frequencies, phases, grace policies, enabled overlaps and
invalid exception scopes before restructuring. Run
`python manage.py audit_model_restructure` in the Linux container for a read-only
configuration report before upgrading. Reconcile reported conflicts explicitly.

Invoice/document/assignment identities, money, signed quantities, units, periods,
approval metadata, original storage and immutable extraction/review JSON remain
intact. Compatibility adapters read older classification keys in memory. Earlier
migrations remain unchanged. Migrated service billing settings are archived
privately for recovery. An unchanged-data downgrade restores their values.
A downgrade stops if service modes, vendor schedules or the subcategory catalogue
changed, or the old schema cannot represent new classifications, independent
electricity consumption sources or reviewed/vendor-wide annotations. Catalogue
guards include edited names, enabled flags, categories and added or deleted entries,
even when those entries have no allocations. Restore the verified pre-upgrade backup
with the previous image in that case.

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

A manual document represents a transcribed invoice whose original is unavailable. It
has `source = manual`, blank SHA-256 and filename, no staging/publication path and
storage state `none`; database constraints enforce that combination and forbid later
changes to `source` or `sha256`. Approved manual invoices use the same review,
identity, allocation and reporting paths as uploaded ones. They provide no source
PDF, so source view/download, re-parsing and storage recovery are unavailable; their
display name is `Manual entry #<id>`. The nonempty-hash unique index applies only to
uploaded documents. Backups keep their database row and review snapshot but require
no recoverable original. Bulk ledger backfills use the same manual-entry service;
see [review](review.md#manual-invoices-without-an-original).

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
confirmation and is retained separately from invoice total. Manual proposals, and
manual documents without an extraction attempt, can be approved without a supported
parser.

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
individual fees. Usage must match the allocation category. Electricity uses kWh, Water uses m³ and Waste uses kg; Other can retain any of these supported source units.
Repeated electricity usage for the same section/period/basis is prohibited; overlapping
periods with the same property, source service and basis require review. Network and
supply usage remain distinct. Usage reversals and replacements retain their separate
bases; [approved reporting](reporting.md) selects canonical electricity separately
and exposes ambiguous overlaps without changing approved values.

Model saves perform validation and reject direct edits to approved allocations and
audit history. Row constraints protect low-level writes, but cross-row reconciliation
and revision history depend on the review service. Application code must use this
service for approved changes rather than bulk updates. PDF processing and filesystem
operations stay outside its short IMMEDIATE database transaction.
After approval commits, original publication runs under the shared storage lock;
failed moves preserve approved values and a recoverable publication journal.
See [uploads and private originals](ingestion.md).

Explicit whole-document deletion removes its invoices, allocations, usage, review
revisions and extraction attempts together. The delete form binds the observed invoice
and extraction revisions, and file cleanup resumes from a private deletion receipt.
The removed document's checksum no longer prevents a fresh upload of the same PDF.

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
