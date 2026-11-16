# Repository Guidelines

## Project Structure

House Bills reviews Swedish utility invoices with Django.

- `bills/`: models, services, API (`api/`), PDF parsers (`parsing/`), migrations.
- `house_bills/`: settings, routing, and runtime configuration.
- `bills/templates/` and `bills/static/bills/`: HTML templates, CSS, JavaScript, and icons.
- `tests/`: application/parser tests, API checks (`api/`), Chromium scenarios (`browser/`).
- `tools/`: maintenance utilities; `docs/`: guides and contracts.

## Development Commands

Use Linux containers, including on Windows; run from the repository root:

```sh
# Build/start development; open http://localhost:8001/
docker compose -f compose.development.yaml up --build -d app
# Create login account
docker compose -f compose.development.yaml exec app python manage.py createsuperuser
# Lint and formatting
docker compose -f compose.development.yaml run --rm app ruff check bills house_bills tests tools docs/api/example.py manage.py
docker compose -f compose.development.yaml run --rm app ruff format --check bills house_bills tests tools docs/api/example.py manage.py
# Python tests
docker compose -f compose.development.yaml run --rm app pytest -q
# Build/run isolated Chromium checks
docker compose -f compose.development.yaml build browser
docker compose -f compose.development.yaml run --rm browser python -m tools.run_browser
```

For code changes, run Django, migration drift, and API schema [checks](docs/development.md#checks). For documentation-only changes, verify accuracy, paths/commands, and whitespace; report skipped application checks.

## Style & Domain Rules

Use English identifiers, comments, and documentation. Follow existing conventions and Ruff's Python 3.14 target, import ordering, and 100-column formatting.

Follow the [data model](docs/data-model.md): money uses signed integer minor units; quantities use signed integer thousandths. Route approved changes through review services; preserve immutable extraction evidence and review history. Keep PDF processing and filesystem operations outside database write transactions.

Follow [parser maintenance](docs/parsing.md) and the [candidate contract](docs/parser-results.md). Register layouts in `bills/parsing/registry.py`; extraction must never approve records.

## Tests

Add synthetic regressions for parser and operational changes. Generate PDFs through `tests/synthetic.py` or `tests/current_formats.py`. Run Chromium for interactive changes and releases, then the [disposable local production container probe](docs/deployment.md#disposable-production-probe).

For PR reviews, tests do not need to be rerun locally because the CI pipeline already covers them. Review CI results for the current PR commit and report failing, pending, or missing checks.

## Commits & Pull Requests

Use branches and imperative commits; discuss substantial scope changes in an issue first. Follow `.github/pull_request_template.md`: explain changes, report verification and limitations, and update migrations, fixtures, documentation, and dependency locks where needed. Required CI checks must pass before merging.

## Privacy & Configuration

Use wholly synthetic fixtures, screenshots, and public examples; redaction is insufficient. Never commit or publish real invoices, extracted text, databases, backups, credentials, deployment hostnames, private repository references, or private configuration in source, documentation, issues, PRs, comments, or CI artifacts. Keep private evidence in ignored directories. Use `.env.example` for examples; report vulnerabilities through `SECURITY.md`.
