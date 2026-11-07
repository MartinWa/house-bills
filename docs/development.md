# Development and checks

Run application commands in the Linux development image, including on Windows. Both
image targets link Python 3.14.10 to source-built, checksum-pinned SQLite 3.51.3 and use
the same locked application dependencies. The Windows interpreter is not the supported
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
then vendors with their identification numbers, invoice formats and property
mappings. **Administration** and **Sign out** are in the header's **User** menu.
See [configuration and the data model](data-model.md). The domain app is
`bills`. Sign-in opens **Invoices**, with property, service and invoice-date filters.
Use **Upload invoice** on **Documents** to store a PDF privately. Documents can be
filtered by processing and storage state. Filters apply when changed; without
JavaScript, use **Apply filters**. Choose **Review and approve invoice** to check a proposal
or enter an unsupported invoice manually. **Invoices** lists approved records and
provides correction forms. See [review and correction](review.md),
[deterministic parsing](parsing.md) and
[uploads and private originals](ingestion.md).
**Reports** shows approved service costs and billed consumption, with property/date
filters and electricity source selection. See [approved reporting](reporting.md).
For a vendor that always bills one house, set its optional **Default property**;
specific property mappings take precedence.

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

Build the development target before running the same checks as baseline CI:

```sh
docker compose -f compose.development.yaml build app
docker compose -f compose.development.yaml run --rm app ruff check bills house_bills tests tools manage.py
docker compose -f compose.development.yaml run --rm app ruff format --check bills house_bills tests tools manage.py
docker compose -f compose.development.yaml run --rm app python manage.py check --database default
docker compose -f compose.development.yaml run --rm app python manage.py makemigrations --check --dry-run
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
entry. The repeatable [Chromium browser check](../tests/browser/README.md) uses a fresh
isolated Compose project and covers configuration, upload, duplicates, review,
correction, re-parsing, source privacy and mobile layout.

Cost-total regressions cover ignored fee amounts, independent costs when usage is invalid,
legacy proposals, preserved immutable history and rejection of older review tokens.
A disposable migration test verifies archival of old fee rows, unchanged approvals/usage
and reversal back to the old schema.

`.github/workflows/ci.yml` implements **Checks (Linux)** and **Container (Linux)**.
The first verifies dependency exports, Ruff, Django, migration drift, clean development
startup and tests. The second validates workflow syntax and builds and probes production
with no network, a read-only root filesystem and disposable writable `/data` and `/tmp`.
The probe starts Waitress, checks migrations, non-root execution, HTTPS/proxy behavior,
static assets, parsed upload/duplicate/private-source HTTP flows, failed extraction,
manual entry, approval, correction, stale forms and explicit re-parsing acceptance.
It also checks absence of development dependencies/private state. Weekly and manual
CI rebuilds bypass the image cache. Browser CI integration, dependency/image audits, release
publishing and repository protections remain later milestones.

## Dependency updates

Use uv 0.13.7 for lockfile/export tooling. It can run on the editing host without running
the Django application there. Edit the exact direct dependency pins in `pyproject.toml`,
then regenerate the lock and hash-pinned container exports:

```sh
uv lock
uv export --locked --no-dev --no-emit-project --format requirements-txt --output-file requirements.txt
uv export --locked --only-group dev --no-emit-project --format requirements-txt --output-file requirements-dev.txt
```

Rebuild both targets and run the relevant checks. Keep Python's patch pin aligned in
`.python-version`, `Dockerfile` and `tools/check_runtime.py`. Development dependencies
are installed without resolution into a virtualenv that shares the image's application
dependencies. Production excludes the test toolchain and removes pip.
