---
name: parser-maintenance
description: Load this skill when adding or fixing an invoice PDF parser, layout, or version in bills/parsing/, updating the parser registry, or writing parser regression tests. Covers the structural matching contract, revision rules, synthetic test PDFs, and the required verification and re-parsing steps.
---

# Parser maintenance (house-bills)

Follow the repository guidelines in `AGENTS.md` and the full contracts in
[docs/parsing.md](../../docs/parsing.md) and
[docs/parser-results.md](../../docs/parser-results.md). Load those docs before
changing extraction code; the summary below is a routing aid, not a replacement.

## Hard rules

- Money uses signed integer minor units; quantities use signed integer
  thousandths.
- Extraction must never approve records: parsers produce private review
  proposals only. Parsing creates no approved allocations or usage.
- Preserve immutable extraction evidence: original bytes, checksums, extracted
  text and coordinates, and review history.
- Keep PDF processing and filesystem operations outside database write
  transactions.
- Registered layouts must keep working: an earlier version stays installed
  when adding a replacement. Increment the implementation `REVISION` for bug
  fixes instead of mutating past behaviour.
- Never commit real invoices or real extracted text, values, or screenshots;
  synthetic fixtures only.

## Parser module contract

Built-in modules under `bills/parsing/`:

- Declare `FAMILY`, `VERSION` and `REVISION`.
- Implement structural `matches(pages)` — selection is by structure
  (product-number, meter-table or price-table signatures), never by dates or
  filenames.
- Return a `Candidate` from `parse(pages, *, candidate=None)`; an optional
  `read_header(pages)` extracts format-specific header fields so they survive a
  later section-parser failure.
- Use the existing candidate, section, identifier, usage and evidence types
  from the result contract; do not invent parallel types.
- Register the version in `bills/parsing/registry.py`; keep earlier versions
  registered alongside their replacements.
- Loading modules from a mounted directory is not implemented; reparse through
  the service (see below).

## Workflow for a change

1. Write the parser change (or a new layout/version) per the contract above.
2. Add wholly synthetic regressions through `tests/synthetic.py` or
   `tests/current_formats.py`. Cover cosmetic variation, older layouts,
   replacement, unsupported sources, partial failures, units, usage,
   property conflicts and preserved approvals as the change requires.
3. Verify inside the containers, from the repository root:
   ```sh
   docker compose -f compose.development.yaml run --rm app ruff check bills house_bills tests tools docs/api/example.py manage.py
   docker compose -f compose.development.yaml run --rm app ruff format --check bills house_bills tests tools docs/api/example.py manage.py
   docker compose -f compose.development.yaml run --rm app pytest -q
   ```
4. After installing a parser update, rebuild the image and explicitly re-parse
   the selected retained documents: `python manage.py parse_documents <id>...`
   inside the app container. Never re-parse broadly.
5. For interactive changes and releases, run the Chromium checks
   (`docker compose -f compose.development.yaml run --rm browser python -m tools.run_browser`)
   and the disposable production probe in `docs/deployment.md`.
