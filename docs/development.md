# Development and checks

Run application commands in the Linux development image, including on Windows. All
three image targets link Python 3.14.10 to source-built, checksum-pinned SQLite 3.51.3
and use the same locked application dependencies. The Windows interpreter is not the supported
database runtime.

## Local application

From the repository root, with Docker running:

```sh
docker compose -f compose.development.yaml up --build -d app
docker compose -f compose.development.yaml exec app python manage.py createsuperuser
docker compose -f compose.development.yaml logs --tail=100 app
```

Open <http://localhost:8001/> and sign in. Set `HOUSE_BILLS_DEV_PORT` in the shell to
change the loopback host port; the container listens on port 8000. The server applies
migrations before starting Django's development reloader. Login and logout use Django's
built-in authentication, with POST and CSRF protection for logout. Only staff accounts
can use the admin, subject to model permissions. Configure houses under **Properties**,
then vendors with their identification numbers, invoice formats and invoice property
rules. **Administration** and **Sign out** are in the header's **User** menu.
See [configuration and the data model](data-model.md). The domain app is
`bills`. Sign-in opens **Invoices**, with property, service and invoice-date filters.
Use **Upload invoice** on **Documents** to choose and upload a PDF directly. Documents
defaults to **Needs attention**; use the status filter to select **All** or **Done**.
Storage problems remain visible in the attention queue. Filters apply when changed;
without JavaScript, use **Apply filters** and the visible PDF upload field.
Choose **Review invoice** on document detail to check a proposal or enter an unsupported
invoice manually. **Invoices** lists approved records; **Open document** leads to review
and correction. See [review and correction](review.md),
[deterministic parsing](parsing.md) and
[uploads and private originals](ingestion.md).
**Reports** shows approved service costs and billed consumption, with property/date
filters and electricity source selection. See [approved reporting](reporting.md).
**Missing invoices** checks combined vendor bills using dated shared billing
schedules and their required property services. Configure services from
**Properties**, assignments under **Service vendors**, and schedules from
**Vendors**. Use **Not sent this month** once on a missing combined bill. Required
allocations remain inspectable, including an after-review confirmation for a
service that was not billed. Maintain the shared catalogue under **Subcategories**.
See [configuration](data-model.md#property-services-and-vendor-history).
For a vendor that always bills one house, its optional **Default property** applies
after specific invoice property rules.

The source checkout is mounted at `/app`. The `development-state` volume holds the
Linux virtualenv, `/state/development/db.sqlite3`, the development secret, temporary
files, test databases and tool caches. Image startup seeds or refreshes the volume's
virtualenv under a lock when the image fingerprint changes. Do not run host-side `uv
sync` against this volume. Tests use `/state/test-state/test.sqlite3`, independently
of development data, and exercise file-backed WAL instead of SQLite's in-memory mode.

Stop the containers while retaining development state:

```sh
docker compose -f compose.development.yaml down
```

An isolated Compose project (`-p house-bills-check`) creates a separate volume for a
clean-start probe. Avoid deleting the normal volume when it contains work to retain.

## Checks

Build the development target before running the same application checks as CI:

```sh
docker compose -f compose.development.yaml build app
docker compose -f compose.development.yaml run --rm app ruff check bills house_bills tests tools docs/api/example.py manage.py
docker compose -f compose.development.yaml run --rm app ruff format --check bills house_bills tests tools docs/api/example.py manage.py
docker compose -f compose.development.yaml run --rm app python manage.py check --database default
docker compose -f compose.development.yaml run --rm app python manage.py makemigrations --check --dry-run
docker compose -f compose.development.yaml run --rm app python manage.py export_api_schema --check
docker compose -f compose.development.yaml run --rm app pytest -q
docker compose -f compose.development.yaml exec -T app python -m tools.check_http
docker compose -f compose.development.yaml exec -T app python manage.py migrate --check
```

The HTTP checks require the development service to be running. Tests cover synthetic
parser results, authenticated access, CSRF, safe login redirects, the linked SQLite
policy, short IMMEDIATE transactions, concurrent readers/writers, production settings
and missing/pending/locked startup migrations. Domain tests cover integer money and
usage constraints, shared invoices, dated corrections, identity conflicts, preserved
extraction evidence, revision checks, rollback and authenticated configuration forms.
Ingestion tests cover actual upload byte limits, CSRF, exact duplicates, invoice
identity conflicts, shared original publication, collision handling, interruption
recovery, concurrent intake and a coordinated database/original snapshot. Parsing
tests cover issuer recognition, version coexistence, unsupported/ambiguous structures,
partial fields, invalid units/totals, property routing, initial processing concurrency
and re-parsing that preserves approved corrections.
Review tests exercise supported and manual approval through HTTP, signed money and
consumption, explicit row removal, validation feedback, identity conflicts, immutable
evidence, stale extraction/review forms and competing saves, including first manual
entry. The repeatable [Chromium browser check](../tests/browser/README.md) creates a
temporary application instance and covers configuration, all current formats, upload,
duplicates, review, correction, reporting, re-parsing, source privacy and mobile layout.

Cost-total regressions cover ignored fee amounts, independent costs when usage is invalid,
legacy proposals, preserved immutable history and rejection of older review tokens.
A disposable migration test verifies archival of old fee rows, unchanged approvals/usage
and reversal back to the old schema.

Backup tests exercise SQLite snapshots, preserved configuration/history, staged intake,
storage-lock coordination, corruption detection and restoration into a separate directory.

Run Chromium using its separate image and state volume:

```sh
docker compose -f compose.development.yaml build browser
docker compose -f compose.development.yaml run --rm browser python -m tools.run_browser
```

The browser target pins Playwright through `package-lock.json`, obtains Node 24 from a
digest-pinned image and installs Chromium. The normal application and production
targets exclude Node and browser dependencies. Browser setup never uses the normal
development database. CI runs the built browser image without a source mount or
external network access and collects synthetic traces, screenshots and server logs.

`.github/workflows/ci.yml` provides these checks:

- **Checks (Linux)**: locked exports, Ruff, Django, migration drift, clean development
  startup and synthetic application/parser tests.
- **Browser (Chromium)**: locked npm dependency audit and the isolated browser scenario.
- **Container (Linux)**: actionlint, locked Python dependency audit, production build,
  HTTP/runtime checks and a Trivy audit for fixable high/critical vulnerabilities.

The production probe uses a read-only root filesystem, no network and disposable writable
`/data` and `/tmp`. It exercises Waitress, migration guards, non-root execution,
HTTPS/proxy behavior, static assets, upload, manual review, approval, corrections,
duplicates, reporting and private sources. It also backs up the populated instance and
starts a separate restored instance with migrations disabled. Tests, browser dependencies
and private state must be absent from production. Weekly and manual rebuilds bypass
the image cache.

Separate **Dependency review**, **zizmor** and **Scorecard** workflows audit dependency
changes, workflow security and repository posture. Actions are pinned by full commit SHA.
Dependabot proposes updates; its patch auto-merge workflow relies on enabled auto-merge and
required-check protections. Enable those repository settings only after the corresponding
GitHub checks exist and pass. Local checks do not establish that GitHub has executed the
workflows.

Repository settings currently in use:

- Squash-only merges, automatic branch deletion, auto-merge and no wiki; Dependabot alerts,
  Dependabot security updates, private vulnerability reporting, immutable releases,
  secret-scanning push protection, SHA-pinned Actions and default code scanning are enabled.
- The `Protect main` ruleset blocks deletion and force-pushes and requires a pull request
  (squash-only, resolved conversations, up-to-date branch) with successful **Checks (Linux)**,
  **Browser (Chromium)**, **Container (Linux)** and **Dependency review** status checks.
- The `Protect release tags` ruleset blocks deleting or moving `X.Y.Z` tags.

Direct pushes to `main` are therefore rejected; push a branch and open a pull request.

## Releases

The release workflow accepts stable numeric `X.Y.Z` tags on main commits with a successful
CI push run containing all three application, browser and container jobs. It tests the
versioned Linux/amd64 image, audits it, emits an SPDX SBOM and publishes the tested image
with provenance/SBOM attestations to GHCR. Existing version tags are not overwritten;
`latest` advances only for the current stable release. A Git tag supplies the image version
and application footer through the `HOUSE_BILLS_VERSION` build argument; no version bump
in `pyproject.toml` or `uv.lock` is needed. See [application version and update checks](deployment.md#configure-and-initialize).

On a clean, committed `main` checkout, create a local annotated tag in PowerShell:

```powershell
./tools/tag_release.ps1 -Version 0.0.1 -WhatIf
./tools/tag_release.ps1 -Version 0.0.1
```

Use the chosen version; `0.0.1` is illustrative. After the first stable tag, omitting
`-Version` increments its patch component. The helper pushes only when explicitly
given `-Push`; publishing a tag triggers image/release publication. Obtain authorization
before pushing. CI and releases never deploy or upgrade a NAS instance.

Use the [production probe](deployment.md#disposable-production-probe) for a local build.
In Linux with Docker, `bash tools/audit_image.sh house-bills:local` audits that image;
an optional second argument selects the SPDX output path. The script verifies its
pinned Trivy download before use.

## Dependency updates

Use uv 0.13.7 for lockfile/export tooling. It can run on the editing host without running
the Django application there. Edit the exact direct dependency pins in `pyproject.toml`,
then regenerate the lock:

```sh
uv lock
```

Rebuild the affected targets and run the relevant checks. Docker generates hash-pinned
requirement exports directly from `uv.lock`; exports are build artifacts, so Dependabot
updates need only change `pyproject.toml` and `uv.lock`. The Dockerfile owns Python's exact
patch pin, while `requires-python` controls the supported series for uv on the editing host.
The build records that interpreter version and the locked pdfplumber version for
`tools/check_runtime.py` to verify in every image target. Development dependencies
are installed without resolution into a virtualenv that shares the image's application
dependencies. Production excludes the test toolchain and removes pip.

For browser updates, change the exact Playwright pin in `package.json`, regenerate
`package-lock.json` with npm, rebuild the browser target and run its audit/scenario.
Keep the Node digest and major version in the Dockerfile aligned. Browser tooling does
not add an application frontend build or runtime JavaScript dependency.
