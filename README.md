# House Bills

Utility invoice extraction and review, starting with Swedish PDF invoices.

The current application provides authenticated login/logout, staff configuration for
houses, vendors and identifier mappings, and a validated invoice data model. Private
extraction attempts and approved review revisions are stored separately. Authenticated
PDF uploads preserve private originals, reuse exact duplicates and expose recoverable
storage status. Versioned deterministic parsers recognize configured vendors, match
invoice structures and propose service cost totals, billing periods, usage and property
assignments with private
evidence. Review forms support manual entry, approval, corrections and acceptance of
re-parsed proposals, with stale-save protection and retained approval history.
Invoice screens show approved allocations and consumption with private PDF viewing
and download. Authenticated reports show approved costs and consumption with network
electricity preference, explicit supply fallbacks and visible overlap exclusions.
Grafana integration is deferred to step 7b.

Individual fee rows stay in the original PDF and extraction evidence. Parsing and
review use invoice totals, dated property/service allocations and billed consumption.

Development, tests and production use Linux containers with Python 3.14.10, Django 5.2
LTS and checksum-pinned SQLite 3.51.3. A running Docker daemon is required. Start local
development from the repository root:

```sh
docker compose -f compose.development.yaml up --build -d app
docker compose -f compose.development.yaml exec app python manage.py createsuperuser
```

Open <http://localhost:8001/>. Source is mounted from the checkout; the virtualenv,
database and caches live in a separate Linux volume. No production `.env` is needed.
See [development and checks](docs/development.md) for tests and dependency updates, and
[private deployment](docs/deployment.md) for production configuration and startup.

Tests generate wholly synthetic PDFs in temporary directories. No real invoices are
needed to run them. Real documents, extraction evidence, checked expectations and
planning notes belong in the ignored `test-data/`, `private/` and `planning/` directories.

See [the parser result contract](docs/parser-results.md) for units, service periods,
printed VAT and evidence. See [deterministic parsing](docs/parsing.md) for installed
versions, review-only outcomes, retries and selected-document re-parsing.
See [configuration and the data model](docs/data-model.md) for house mappings,
invoice allocations, validation and revision history.
See [uploads and private originals](docs/ingestion.md) for limits, duplicate handling,
approved paths and storage recovery.
See [review and correction](docs/review.md) for supported and manual invoice workflows.
See [approved reporting](docs/reporting.md) for date filters, electricity source
selection, exact SQLite views and executable example queries.
