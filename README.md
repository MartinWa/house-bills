# House Bills

Utility invoice extraction and review, starting with Swedish PDF invoices.

The first implementation milestone provides two extraction prototypes and a normalized
candidate format. The Django application, upload/review screens, storage, deployment and
reporting are subsequent milestones.

Use Python 3.14.10 and [uv](https://docs.astral.sh/uv/):

```powershell
uv sync --locked
uv run pytest -q
uv run ruff check bills tests
uv run ruff format --check bills tests
```

Tests generate wholly synthetic PDFs in temporary directories. No real invoices are
needed to run them. Real documents, extraction evidence, checked expectations and
planning notes belong in the ignored `test-data/`, `private/` and `planning/` directories.

See [the parser result contract](docs/parser-results.md) for units, service periods,
tax allocation, evidence and the boundaries of this milestone.
