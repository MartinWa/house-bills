# Invoice review and correction

Sign in and upload a PDF. **Documents** keeps its original, processing status and latest
extraction. **Review invoice** on document detail opens editable values for both supported and
unsupported PDFs. All invoices require explicit approval.
Allocation warnings show the allocation number, property, service and source-page links.
Select the allocation link to jump to its review fields; select a PDF page link to
open the corresponding page of the original.
Warnings about electricity consumption refer to billed kWh and reading periods under
**Consumption**; the allocation totals can still be complete and correct. Consecutive
network readings appear as separate consumption entries, with their total checked
against the printed energy quantities.

## Check and approve

1. Choose **View** or **Download** under **Original PDF** and check the invoice fields:
   vendor, reference, invoice/credit type, dates, SEK, net, VAT, rounding, invoice total
   and amount due. Due date is optional. A different amount due needs explicit confirmation.
2. Assign every allocation to its property and service. Keep different services and
   property sections separate, including a shared invoice covering both houses.
3. Check each allocation's net, VAT, cost including VAT and overall billing period.
   Allocation totals and invoice rounding must reconcile with the header. Individual
   fees stay in the original PDF; entering them is unnecessary. Use the printed VAT.
4. Check billed consumption, unit, period and measurement basis. Record electricity
   once per period/source rather than once per price line. Network and supply are
   separate sources. Water uses m³, waste kg and electricity kWh. Preserve negative
   corrections and reversals; a reversal cannot have a positive quantity.
   For waste, check **Waste subcategory**: Restavfall or Matavfall. Leave it unspecified
   when the source does not identify the type. Electricity uses the allocation's
   network (Elnät) or supply (Elhandel) service as its subcategory.
5. Keep customer accounts with the invoice and installation/contract identifiers with
   their allocation. Source page fields identify the relevant page. A reviewed property
   assignment or identifier edit applies to this invoice; maintain future mappings in
   **Administration**.
6. Resolve and acknowledge extraction warnings, add an optional review note and choose
   **Approve invoice**. Successful approval opens the invoice and publishes its one
   original to approved storage. A shared invoice still has one physical original.

Numbers accept a comma or decimal point and spaces for grouping. Money has at most two
decimal places; consumption has at most three. Inputs are stored exactly without silent
rounding. All displayed/editable billed periods include their first and last days;
storage converts the final day to an end-exclusive date.

**Add allocation**, **Add consumption** and **Add identifier** add rows.
Select the allocation for related rows. **Remove this row** explicitly excludes it;
remove or reassign related rows when removing an allocation. Row controls work with
JavaScript and have a server-rendered fallback. The review is bounded to 20 allocations,
60 consumption entries and 60 identifiers, within the request-size limits.

Unsupported, damaged or unreadable sources can be entered manually using the same fields.
Fill in the headers and at least one reconciled allocation with its billing period.
Add consumption only
when supplied by the source. The original and failed extraction remain preserved after
manual approval. Review forms do not save partial drafts. Validation errors retain the
submitted fields and approved values; closing or cancelling discards unsaved entries.

## Manual invoices without an original

**Invoices** has an **Add manual invoice** form for a bill that exists only on paper or
in another archive. It creates a review document with no source PDF, then opens the same
review form. Enter vendor, reference and dates, then at least one property/service
allocation whose net + VAT and rounding reconcile with the invoice total, plus its
billing period. Consumption is optional. Approval follows all normal checks and
records the reviewer and note.

Manual invoices show a **Manual entry** badge instead of a source link. They have no
**View/Download original**, **Re-parse**, or **Retry storage** action, and storage
recovery ignores them. They are otherwise normal approved invoices: corrections,
identity conflicts and reports behave identically. Their checksum is empty, so two
manual entries never collide as duplicate uploads. Prefer a real PDF when one becomes
available; the existing vendor/reference/kind identity check then surfaces the
conflict instead of silently counting the bill twice.

## Approved invoices and corrections

**Invoices** lists approved records and filters by property, service and an inclusive
invoice-date range. Filters apply when changed. Each row shows the invoice total and
groups allocations by property, showing each property name once.
Detail pages show service costs, consumption, billing periods, reviewer and approval
history. **Open document** leads to the source PDF and review actions.
The document page also shows the approved fields, costs and usage, with earlier
parser output kept in collapsed **Extraction history**. Warnings from an extraction
reviewed before approval remain as history; a new extraction still needs review.

Choose **Open document**, then **Review invoice**, to start from the latest approved
values. Edit the relevant fields,
add a note and choose **Save correction**. A successful correction records a new immutable
revision and retains all earlier snapshots. Changing the reference, date or property
can move the same original to its new approved path; the source links remain stable.

Different files with the same vendor/reference/type show source conflicts. Only one
invoice with that identity can be approved. Open both sources and resolve the identity
before approving the other file. Exact duplicate uploads reopen the existing document
and preserve its approvals.

## Re-parsing and stale forms

After changing configuration or installing a parser update, choose **Re-parse document**
on a selected document. Parsing appends a proposal and retains approved values. For an
approved invoice, **Proposed differences** shows changed headers, identifiers,
allocation totals/periods and consumption.
Vendor and property labels include their unique slugs to distinguish records with
the same name.

**Review invoice** keeps the current approval as the starting point. When a newer
extraction differs, review shows the proposed differences and **Load latest extraction**
loads the proposal separately. **Return to approved values** restores the saved starting
point. Edit/check the values and choose **Save correction** to accept them. Saving the
approved values also completes review while retaining your corrections. An extraction
that matches the approval, or predates the latest review, offers no loading option.
Unsupported or invalid re-parsing outcomes
still require full manual review; they cannot approve themselves.

Saves check the invoice review revision and document extraction revision inside the
approval transaction. A repeated submission or a form opened before another save or
re-parsing receives a conflict response and keeps the submitted fields. **Reload the
latest review** opens the current values; apply the intended changes there. Competing
first manual submissions also approve once. Re-parsing has its own observed revision
check so retries of the same form do not append duplicate attempts.

Storage/database failures retain entered fields for retry. Original publication runs
after approval commits; interrupted publication preserves approved records and offers
storage recovery on the document page. See [uploads and private originals](ingestion.md).

The application tests and [Chromium browser check](../tests/browser/README.md) exercise
these workflows with wholly synthetic PDFs. [Reports](reporting.md) reflects accepted
corrections and links ambiguous electricity intervals back to invoice review.
A bulk backfill of ledger rows uses the same manual-entry service; the
`import_ledger_invoices` command and its input file stay with the private ledger data,
outside this repository. Grafana integration and complete real-format coverage remain
subsequent steps.
