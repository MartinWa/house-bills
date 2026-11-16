# Repository Guidelines

## Project Structure & Module Organization

House Bills reviews Swedish utility invoices with Django.

- `bills/`: models, services, HTTP API (`api/`), deterministic PDF parsers (`parsing/`), and migrations.
- `house_bills/`: settings, routing, and runtime configuration.
- `bills/templates/` and `bills/static/bills/`: HTML templates, CSS, JavaScript, and icons.
- `tests/`: application/parser tests; `tests/api/` contains API checks and `tests/browser/` contains Chromium scenarios.
- `tools/`: maintenance utilities; `docs/`: contributor guides and contracts.

## Build, Test, and Development Commands

Use Linux containers, including on Windows; run from the repository root:

```sh
# Build/start development; open http://localhost:8001/
docker compose -f compose.development.yaml up --build -d app
# Create a login account
docker compose -f compose.development.yaml exec app python manage.py createsuperuser
# Lint and verify formatting
docker compose -f compose.development.yaml run --rm app ruff check bills house_bills tests tools docs/api/example.py manage.py
docker compose -f compose.development.yaml run --rm app ruff format --check bills house_bills tests tools docs/api/example.py manage.py
# Run Python tests
docker compose -f compose.development.yaml run --rm app pytest -q
# Build/run isolated Chromium checks
docker compose -f compose.development.yaml build browser
docker compose -f compose.development.yaml run --rm browser python -m tools.run_browser
```

Before submitting code, also run Django, migration drift, and API schema checks from `docs/development.md`.

## Coding Style & Naming Conventions

Use English identifiers, comments, and documentation. Python uses four-space indentation, `snake_case` functions/modules, and `PascalCase` classes. Ruff targets Python 3.14, enforces import ordering, and formats to 100 columns. Follow existing template and JavaScript conventions. Register layouts in `bills/parsing/registry.py`; extraction must never approve records.

## Testing Guidelines

Use pytest and pytest-django; name files `test_*.py` and functions `test_*`. Add synthetic regressions for parser and operational changes. Generate invented PDFs through `tests/synthetic.py` or `tests/current_formats.py`. No numeric coverage threshold is configured. Run Chromium checks for interactive changes and release work, followed by the production probe described in `docs/deployment.md`.

## Commit & Pull Request Guidelines

History uses concise imperative subjects such as `Track waste fraction costs separately`; squash merges append PR numbers. Use branches; discuss substantial scope changes in an issue first. Follow `.github/pull_request_template.md`: explain changes, report verification and limitations, and update migrations, fixtures, documentation, and dependency locks. Link related issues; use synthetic data for screenshots. Required CI checks must pass before merging.

## Privacy & Configuration

Never commit real invoices, extracted text, databases, backups, credentials, or private configuration. Redacted invoices are unsuitable fixtures. Keep private evidence in ignored directories. Use `.env.example` for configuration examples and follow `SECURITY.md` for vulnerability reporting.
