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

The registry in `bills/parsing/registry.py` lists the installed parsers and their
implementation revisions. It is built lazily from the configured parser directory, so the
image itself ships no parser code; see [installing parsers](#installed-parsers). A recognized
vendor's compatible families limit structural matches. With an unknown vendor, an
unambiguous installed structure can still yield fields without vendor or property
assignments. Unknown or multiple matching versions remain review proposals. Dates and
filenames never select the newest parser. A family routes only after it is added to
a vendor's invoice formats; a recognized vendor with no formats yields **Invoice layout
is unsupported**. Matchers must be strict because exactly one match is required.

The repository ships reviewed example parsers in `docs/examples/parsers/` and installs none
of them. Copy the directory you need and install it read-only as described below. The
table lists what those examples cover; a revised example keeps its family/version and
increments its revision.

| Family/version | Billing structure | Revision |
| --- | --- | --- |
| `layout_01/v1` | Contract-based water, waste, network and supply; page continuations, explicit garden/fraction and demand costs, billed kW and dated water reversals. | `12` |
| `layout_01/v2` | Historical product-number water, network and supply, including printed `kbm` water units, explicit waste and demand costs and billed kW. | `7` |
| `layout_02/v1` | Electricity supply; installation details above each specification. | `3` |
| `layout_02/v2` | Replacement supply structure; service-specific title and inline installation key. | `2` |
| `layout_03/v1` | Quarterly waste or dated tank emptying; VAT basis and separate payment summary. | `3` |
| `layout_04/v1` | Electricity supply; cover totals and one installation specification. | `1` |
| `layout_05/v1` | Electricity supply; explicit consumption-period table beside a historical chart. | `2` |
| `layout_05/v2` | Electricity supply; printed billed consumption in a meter-reading table beside a chart. | `1` |
| `layout_06/v1` | Network and energy tax; repeated billed kWh quantities and inclusive VAT prices. | `4` |
| `layout_06/v2` | Network and energy tax; exclusive VAT prices or dated price changes with separate tax rows. | `3` |
| `layout_07/v1` | Electricity supply; area-labelled consumption and inclusive VAT totals. | `1` |
| `layout_08/v1` | Compact electricity supply summary with printed consumption, total, VAT and rounding. | `3` |

Support does not cover every historical invoice or future layout change. The examples are
the maintained reference implementations; a vendor format you need beyond them is your own
plugin. Spacing, case,
title punctuation and period dash changes preserve a version when its structural rules
still match. Normalization retains the original extracted text and coordinates as
evidence.

The v2 structures are selected by their product-number, meter-table or price-table
signatures; they coexist with v1 and do not route by invoice date. Legacy
network readings retain column order and exact grouped quantities. Network price rows
are checked against readings within their own printed periods. When a contract has no
overall period label, dated fee rows supply its cost period; billed waste weights keep
their separate collection intervals. Undated water reversals remain review issues.
Waste extraction keeps combined charges combined unless the mapped property has
enabled fraction cost services. For those services, Layout 01 splits explicit billed
fraction amounts into separate allocations, retaining shared fees under general
collection. VAT shares reconcile to the printed contract VAT. Missing charge amounts
keep the combined cost and require review. Supported contract/product blocks also
classify explicit garden costs and unambiguous named fraction blocks using their totals.
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
page lines and private `routing` metadata: vendor, issuer evidence, layout matches with
source and code fingerprints, the selected parser, `API_VERSION`, host `APP_VERSION`, the
loaded installed-set digest, the host implementation digest, per-stage parser failures,
parser-directory load errors, invoice kind and proposed section properties. Missing values
are null. Independent
headers and previously extracted sections survive invalid fields or parser errors.
Parsers retain service cost totals and overall billing periods. Layout 01 separates
printed Effektavgift and configured waste fraction charges, balancing their VAT to
the original contract total. Remaining fee amounts are optional. Numeric precision, units, periods and
allocation/header reconciliation require validation. Repeated
supply price quantities yield one usage per period; network usage comes from its
dated billed readings. Multiple readings retain consecutive periods, and their total
must agree with the billed kWh. Incomplete or ambiguous readings are identified as
consumption warnings. Overlapping usage for the same property/source needs review.
See [the result contract](parser-results.md) for quantity and tax conventions.

Every attempt leaves the document in **Needs review**. Unknown or unreadable sources,
ambiguous routing and unexpected parser errors also record failed attempts with safe
reasons. The ambiguity message names the matching family/version entries. Malformed
parser results are rejected against a bounded candidate contract: independently valid
header fields and sections are retained, malformed portions are discarded with an issue,
and serialization is checked before the database write. Invalid extracted fields retain
candidate issues. Original bytes and checksums
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

Modules declare `FAMILY`, `VERSION` and `REVISION`, implement structural
`matches(pages)` and return a `Candidate` from `parse(pages, *, candidate=None)`.
An optional `read_header(pages)` extracts format-specific header fields before section
parsing so those fields survive a later parser failure. Use the existing candidate,
section, identifier, usage and evidence types; see [the result contract](parser-results.md).
To author a format, copy the closest example from `docs/examples/parsers/` into your own
parser directory, install it and explicitly re-parse selected documents. Follow
[contributing](../CONTRIBUTING.md) for fixtures/checks; a change to a shipped example is a
change to which the tests hold it.

## Installed parsers

All parser code is installed by the operator. The image contains none, so a new layout or
a private vendor format never requires an image rebuild or a source change to this
repository. A parser is a top-level `.py` file in an operator-managed directory mounted
read-only as `/parsers`. Set `HOUSE_BILLS_PARSER_PLUGINS=/parsers` (or
`HOUSE_BILLS_PARSER_PLUGINS_FILE` for a secret file) and add
`compose.parser-plugins.yaml`. The setting is empty by default, and an installation with
no parser directory stores uploads as unsupported review proposals. The directory is not
part of backups: redeploy it together with the image. Adding, changing or removing a
parser requires a container restart and an explicit re-parse of the affected documents:

```sh
cp docs/examples/parsers/*.py /srv/house-bills/parsers/   # reviewed examples only
python manage.py parsers --check
python manage.py parse_documents 12 19
```

Install the parser directory before configuring a vendor's invoice formats: the format
choices come from the installed set, so an empty registry offers no choice and a
configured family with no installed parser fails `parsers --check`. A selection whose
parser is no longer installed stays listed with no versions and can still be unselected,
so removing code does not require the admin.

The loader reads only top-level files whose names are Python identifiers. It accepts
regular files only (no symlinks, subpackages, FIFOs or devices), at most 64 files and
1 MiB per source. It adds no `sys.path` entry: shared code comes from `bills.parsing.api`
or from a sibling file in the same directory (see below). A bare `import helper` binds an
installed module or fails. It reads each source once, compiles that
snapshot and executes it in a private `house_bills_plugins.<stem>` module. Import must be
cheap and side-effect free: do not query the database, touch storage or write files,
because discovery also runs for management commands such as `migrate`.

Vocabulary is closed: services must be `electricity`,
`electricity_network`, `electricity_supply`, `water`, `waste` or `other`, and usage units
`kWh`, `kW`, `m3` or `kg`. District heating, gas and new units are later work; an
unsupported value fails validation rather than being silently accepted.

### Shared code

Every `.py` file is loaded through the same bounded reader, so files may share code with
relative imports. A file whose name starts with `_` is a support module: it needs no
layout metadata, cannot be selected as a parser, and still contributes to the installed
set digest.

```python
# _supply.py
from bills.parsing import api


def extract_supply(candidate, pages): ...

# layout_02.py
from bills.parsing import api

from ._supply import extract_supply
```

Use a sibling import only inside the installed directory. Importing `bills.parsing`
internals directly bypasses the stable API: it works today and breaks silently when the
host changes. The shipped examples demonstrate both patterns.

### Stable API

Parsers import only `bills.parsing.api`, which re-exports the candidate types
(`Candidate`, `Section`, `Usage`, `Identifier`, `Evidence`), normalization helpers
(`NUMBER`, `DATE`, `PERIOD`, `money`, `quantity`, `source_date`, `source_period`, `unit`,
`anchor`, `starts`, `text`, `normalized_text`), extraction helpers (`header`,
`reconcile`, `find`, `AMOUNT`, `QUANTITY`, `capture`, `empty_header`, `installation`,
`identity_fields`, `cost_period`, `due_amount`, `price_quantities`, `tax_basis`,
`last_money`), the classification lookups `waste_subcategory` and
`waste_cost_subcategory`, and the host-invoked split operations `split_waste_costs` and
`split_electricity_demand`. `API_VERSION = 1` keeps
those names, signatures and value conventions stable; breaking changes require a new
version. Everything else under `bills.parsing` is internal.

A module declares the version it targets and the loader rejects a mismatch before the
module reaches the registry:

```python
API_VERSION = 1
FAMILY = "private_energy"
VERSION = "v1"
REVISION = "1"
```

`FAMILY` uses `[a-z][a-z0-9_]*`, and `VERSION` and `REVISION` use
`[A-Za-z0-9][A-Za-z0-9._+-]*` within the model length limits. A `(FAMILY, VERSION)` pair
may not collide with another installed parser. Entry points are validated by
signature:

- `matches(pages)` returns a real `bool`. Raise or return a non-boolean and the parser is
excluded and its failure recorded; other parsers keep working.
- `read_header(pages)` returns a `Candidate`; `parse(pages, *, candidate=None)` mutates
  and returns the supplied candidate. The host checks both the return value, its identity
  with the supplied object and the candidate's parser metadata.
- `configure_sections` is optional; see [the configuration hook](#configuration-hook).

A candidate may be partial: null fields and financial inconsistencies are review issues,
not contract failures. Malformed objects, duplicate section IDs, too many sections/usages
and unsupported units are contract failures; the host then retains a safe review proposal
built from independently valid header fields and sections.

### Worked example

This wholly synthetic plugin uses only the public API and the standard library. It
matches one labelled structure, keeps a partial candidate when the amount is missing and
exposes an optional scoped configuration hook:

```python
"""Synthetic example: one water allocation per labelled invoice."""

import re

from bills.parsing import api

API_VERSION = 1
FAMILY = "example_water"
VERSION = "v1"
REVISION = "1"
LINE = re.compile(rf"Water charge\s+({api.DATE})\s+-\s+({api.DATE})\s+({api.NUMBER})\s+m3")


def matches(pages):
    return any("Example Utility invoice" in api.text(line) for page in pages for line in page)


def read_header(pages):
    return api.header(pages, FAMILY, VERSION, REVISION)


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    for line in (row for page in pages for row in page):
        match = LINE.search(api.text(line))
        if not match:
            continue
        start, end = api.source_date(match[1]), api.source_date(match[2])
        candidate.sections.append(
            api.Section(
                "section_01",
                "water",
                [],
                usages=[
                    api.Usage("water", "m3", api.quantity(match[3]), start, end, "billed", line)
                ],
                start=start,
                end=end,
                net_minor=candidate.net_minor,
                vat_minor=candidate.vat_minor,
                gross_minor=(candidate.net_minor or 0) + (candidate.vat_minor or 0),
            )
        )
    return api.reconcile(candidate)


def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    # Operations in API version 1 are waste_cost_split and electricity_demand_split.
    return dict(section_properties), []
```

The shipped examples in `docs/examples/parsers/` follow the same shape at real size: each
declares its metadata, imports `bills.parsing.api`, and the v2 files reuse their v1
sibling with `from . import layout_01`. Read one before writing your own.

Test it against wholly synthetic PDFs with plain pages, without models or a database:

```python
from bills.parsing.plugins import discover
from bills.parsing.reader import read_pdf

pages = read_pdf("synthetic-example.pdf")
parser = discover("/parsers").parsers[0]
assert parser.matches(pages)
candidate = parser.parse(pages)
assert candidate.requires_review
print(candidate.to_dict())
```

Then place the file in the mounted directory and verify the load:

```sh
# Host directory is owned by the operator, readable but not writable by APP_UID.
docker compose -f compose.yaml -f compose.parser-plugins.yaml run --rm app \
  python manage.py parsers --check
```

`parsers` lists every installed parser with its file, family/version/revision and
fingerprint, prints the host implementation digest, plus load errors. `--check` exits
non-zero for directory load errors, rejected files, a configured vendor format with no
installed parser or an unreadable host implementation digest, because retained corrections
cannot verify their host code without it. An unset or empty directory
passes when no vendor references an unavailable family, and stores uploads as manual
review work. A missing or unreadable configured directory leaves startup running with no
parsers and reports the load error; it does not raise a settings exception. Invalid path
policy is different: a relative production path or a path equal to or beneath `DATA_DIR` or
`MEDIA_ROOT` (including symlink aliases) rejects configuration.

### Fingerprints and failure isolation

Each accepted file has a SHA-256 fingerprint over the exact compiled source snapshot plus
its declared family/version/revision. Existing bytecode caches are neither read nor
written, so the fingerprint describes what executed. Support modules are digested too, so
a shared helper cannot change without moving the installed-set digest. Each module also
gets a code fingerprint over its own snapshot and the transitive sibling imports it
declares, so a parser that reuses shared code cannot keep its identity when that code
changes. Relative imports may appear in any filename order: the loader retries files that
wait on a sibling and reports only the files that never resolve.

The extraction attempt's `routing` payload records each matched parser's identity, source
and code fingerprints, the selected parser, host `APP_VERSION`, `API_VERSION`, a digest
over the installed directory and a digest over the host parsing implementation, plus the
parser-directory load errors that explain a missing parser. The installed-directory digest
covers every readable file through its identity or source snapshot and adds the directory
and load error codes, so two different directories never share it. A retained
correction requires the recorded source fingerprint, code fingerprint and host
implementation digest, so changed parser code, shared code or hook implementation needs a
re-parse and review first. Re-parsing after such a change legitimately produces new
fingerprints on a
new attempt; earlier attempts stay immutable.

Entry-point exceptions and malformed results never break ingestion for other documents.
Diagnostics record a parser identity and stage, not exception text, paths, source snippets
or local values. Detailed failures are investigated locally with synthetic documents.
A failing matcher is excluded; a failing selected parser keeps a safe partial proposal;
a failing configuration hook restores the pre-hook candidate and property mapping.
Process exits, hangs and resource exhaustion are outside these guards.

### Configuration hook

`configure_sections(candidate, pages, section_properties, *, operation, section_ids,
configuration)` is an optional protocol, not an exported function. The host resolves the
exact recorded family/version through `api.configure(...)` and validates the effects in
both ingestion and retained corrections. API version 1 operations are `waste_cost_split`
and `electricity_demand_split`; each invocation requests exactly one. `section_ids` is a
frozen set of host-selected original IDs. `configuration` maps those IDs to plain records
with `enabled_waste_fractions` (a frozen set of catalogue keys) and is empty for demand
splitting. Hooks receive no Django objects and perform no database or storage access.
They may change only the requested cost/usage split in selected sections of that
operation's own service: waste splitting touches only waste allocations and demand
splitting only network allocations. Every other selected allocation, all unselected
sections, existing property assignments, headers, totals and unrelated usage must stay
intact. A new section must keep the printed identifiers of one selected source
allocation, inherit that source's property and stay within the source's own cost totals
and existing usage; identifier keys are the printed contract, product or installation
numbers. A complete fraction split may replace its parent allocation with the fraction
sections when it conserves the parent's costs and usage. Hooks return
`(section_properties, warnings)`, and the returned property mapping covers exactly the
resulting section IDs.
Unsupported operations return the values unchanged with an explicit warning. See
[review and correction](review.md#re-parsing-and-stale-forms) for how retained
corrections use the approval's exact recorded parser.

Parsers execute with application privileges. Read
[security expectations](../SECURITY.md) before mounting code; [NOTICE](../NOTICE)
states the licensing position for independently authored parsers.
