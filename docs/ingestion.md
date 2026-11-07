# Uploads and private originals

Sign in and choose **Upload invoice**. Upload one `.pdf` file, up to 20 MiB by
default. Intake checks the PDF header and actual received byte count, hashes the
original bytes with SHA-256 and preserves them without preprocessing. A repeated
upload opens the existing document, even if its filename or source house folder
differs. Filenames never assign a property.

**Documents** shows processing and storage states. The document page provides
status messages, identity conflicts when recognized, review/re-parsing actions and
**Retry storage** for interrupted storage operations. **View original PDF** and
**Download original PDF** require an active signed-in session and check the stored
checksum. Viewing uses inline disposition; downloading uses attachment disposition.
Both use private/no-store headers. No static or media URL serves the originals.

The document page also shows the latest **Extracted proposal**, with invoice fields,
property/service costs, signed usage and issues needing attention. Missing fields
are marked unknown. Choose **Review and approve invoice** to fill in or correct the
values. Approved invoices provide **Correct approved values** and **Review extracted
changes**. See [review and correction](review.md).

After storing the original, intake runs versioned deterministic parsing and retains
private review proposals. Recognized identities can create review records; intake
creates no approved costs or usage. Unknown, damaged, textless or password-protected
PDFs retain their original bytes and a visible review reason. They can be entered
manually through the review form. Explicit approval validates the complete proposal
and publishes its original. See [parsing and selected re-parsing](parsing.md).

## Limits and paths

`HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES` sets the byte limit from 1 through 104857600;
the default is 20971520. Django's streaming upload handler enforces the actual file
limit before CSRF parses the request, while preserving normal CSRF checks. Waitress
also caps the entire request at the file limit plus 64 KiB for multipart fields.
Configure the HTTPS proxy's request limit consistently. Rejected input creates no
document or published original.

Originals live under `MEDIA_ROOT`, normally `/data/invoices` in production and
`/state/development/invoices` in development. Newly created directories and files
have modes 0700 and 0600 respectively. The database stores relative paths.

```text
inbox/{full-sha256}.pdf
{property-or-shared}/{vendor-slug}/{year}/
    {invoice-date}_{sanitized-reference-or-id}_{hash-prefix}.pdf
```

All invoices in a document must be approved before publication. Properties come from
their approved billing sections. A document covering both houses has one original
under `shared`. For several invoices in one document, the earliest invoice date and
reference determine the filename and all invoices must have the same vendor.
An occupied or reserved destination adds a numeric suffix; differing bytes are never
overwritten. Explicit approved corrections can move the same original to its new
reviewed path. The authenticated download URL stays stable.
The current parsers and review screens produce one invoice per document; re-parsing
an approved invoice proposes changes to that invoice and preserves its approved identity.

## Recovery and backup coordination

`bills.storage.ingest_pdf(stream, filename=...)` is the shared intake service for web
uploads and the later folder importer. It stores the original, runs initial parsing
and returns `(document, duplicate)`. Exact duplicates retain the existing attempt.
Storage recovery resumes initial parsing when no attempt was committed; selected
re-parsing creates a new proposal and preserves approved values.
`register_invoice_identity()` and the optional vendor argument to `record_extraction()`
register recognized identities without approved allocations. Different files with
the same vendor/reference/kind remain proposals and show source links for review.
The existing unique approved-identity constraint prevents double approval.

Complete uploads first enter private `.staging` files. A flushed, atomically written
JSON receipt preserves the hash and original filename even if the first database
write fails. Document staging and publication paths form a durable journal. Intake
and publication flush bytes and directories before committing the final database
path. Interrupted moves can be resumed whether the bytes are still at the old path
or already at the destination. Storage failures preserve the journal and source.
An exact duplicate can restore a missing original without changing approved values.
A checksum mismatch preserves the damaged file and requires operator inspection.

Use **Retry storage** for an existing document. To recover all complete receipts and
journal entries, including uploads interrupted before their database row existed:

```sh
docker compose run --rm app python manage.py recover_documents
```

For development replace `docker compose` with
`docker compose -f compose.development.yaml`. Recovery is repeatable and returns a
nonzero exit status for remaining storage failures or invalid receipts. Invalid
receipts and incomplete staging files are retained for inspection; files without a
complete receipt are never treated as accepted uploads. Re-upload an interrupted
input stream or an original whose bytes are no longer present.

Intake, publication, source opening and the extraction/approval persistence services
coordinate through the process-shared `MEDIA_ROOT/.originals.lock`. Application code
must use these services. Acquire the storage lock before starting database writes;
do not wrap file operations in request-wide or caller-owned database transactions.
An approval commits before its publication callback. A failed callback preserves
approved rows and recovery publishes the fully approved inbox original later.

A backup integration must hold `bills.storage.originals_lock()` for the whole SQLite
backup-API snapshot and originals copy, including staging receipts and journaled
files. Do not copy a live database/WAL as separate ordinary files. The synthetic
tests demonstrate a consistent checksum-verified snapshot and verify that another
process waits for this lock. The operational backup/restore command and clean NAS
restoration remain step 8.
