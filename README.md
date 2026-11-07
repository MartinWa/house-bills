# House Bills

Utility invoice extraction and review, starting with Swedish PDF invoices.

The current application provides authenticated login/logout, staff configuration for
houses, vendors and identifier mappings, and a validated invoice data model. Private
extraction attempts and approved review revisions are stored separately. Two extraction
prototypes use the normalized candidate format. Upload, review screens, original
storage and Grafana reporting are subsequent milestones.

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
tax allocation, evidence and the boundaries of the parser prototypes.
See [configuration and the data model](docs/data-model.md) for house mappings,
invoice allocations, validation and revision history.
