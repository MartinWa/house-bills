# Uploads and private originals

Sign in, open **Documents** and choose **Upload invoice**. Upload one `.pdf` file, up to 20 MiB by
default. Intake checks the PDF header and actual received byte count, hashes the
original bytes with SHA-256 and preserves them without preprocessing. A repeated
upload opens the existing document, even if its filename or source house folder
differs. Filenames never assign a property.

**Delete document** is available for every document, including approved invoices.
Its delete page identifies the source and invoices before confirmation. Deletion removes
the document's invoice data, review and extraction history, and all original/staged PDF
copies. Uploading the same bytes afterward creates a new entry. File cleanup is journaled
under private storage's `.deletions/` directory and resumes before new uploads, storage
recovery and backups, so interrupted cleanup cannot restore a deleted staging receipt.

**Documents** defaults to **Needs attention**, showing unfinished reviews and storage
problems. Select **All** or **Done** to include completed PDFs and manual entries.
The status filter applies when changed. **Upload invoice** opens the file picker and
uploads the selected PDF; upload errors remain visible on Documents. Without JavaScript,
the page shows a file field and an upload button.
The document page provides
status messages, identity conflicts when recognized, review/re-parsing actions and
**Retry storage** for interrupted storage operations. Under **Original PDF**, **View** and
**Download** require an active signed-in session and check the stored
checksum. Viewing uses inline disposition; downloading uses attachment disposition.
Both use private/no-store headers. No static or media URL serves the originals.
An approved PDF left in the inbox stays in **Needs attention**, with **Retry storage**
available until its original is published.

The document page shows the **Approved invoice** once reviewed, with saved invoice
fields, property/service costs and signed usage. Earlier extracted values remain in
collapsed **Extraction history**; their warnings do not require another approval.
An unreviewed extraction appears as **Extracted proposal**, with missing fields marked
unknown and issues needing attention. Choose **Review invoice** to fill in or correct
the values. For approved invoices, review starts from the saved values. A newer
extraction with pending differences offers **Load latest extraction** inside review.
See [review and correction](review.md).

Invoices without a obtainable original start under **Invoices → Add manual invoice**
instead of intake. They have no bytes, hash, staging receipt or publication path and
store storage state `none`; approval and reporting are unchanged. Source view/download,
re-parsing, storage retry and recovery are disabled for them. See
[manual invoices](review.md#manual-invoices-without-an-original).

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
and returns `(document, duplicate)`. Manual documents never enter this service;
`recover_documents`, `publish_original` and `retry_document` skip them. Exact duplicates retain the existing attempt.
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

`backup_bills` holds the startup-migration lock and `bills.storage.originals_lock()`
for the whole SQLite backup-API snapshot and originals copy, including staging receipts,
partial uploads and journaled files. Supported billing/storage writers wait for this lock;
account changes can continue against the live database after the consistent snapshot.
Do not copy a live database/WAL as separate ordinary files.

`verify_backup` checks the package manifest, every file's size/checksum, SQLite integrity,
foreign keys and recoverable document references. Manual documents are checked for an
empty checksum and absent paths instead of recoverable bytes. `restore_bills` requires a new directory
outside live storage and retains supplied recovery configuration for operator review.
See [operational backup and recovery](backup-and-recovery.md) for private mounts,
commands and the clean-instance verification procedure. Synthetic tests and the production
HTTP probe verify restoration locally; verification on the intended NAS remains pending.
