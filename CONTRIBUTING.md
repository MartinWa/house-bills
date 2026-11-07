# Contributing

House Bills is a personal project with limited maintenance capacity. Small,
focused fixes and parser contributions are welcome; discuss substantial scope
changes in an issue first. There is no guaranteed support or acceptance schedule.
Code, identifiers, comments and documentation are in English.

## Privacy and fixtures

Use wholly invented PDFs, values, names, issuer identifiers, installation keys
and addresses. Redacting a real invoice does not make it a public fixture. Do not
commit originals, extracted text, screenshots of real records, databases,
environment files or backups. Keep corpus evidence in ignored private directories.
Public issues and PRs must use synthetic examples.

Tests generate PDFs using `tests/synthetic.py` and `tests/current_formats.py`;
no real invoices are needed.

## Parser contributions

Follow [parser maintenance](docs/parsing.md) and the
[normalized result contract](docs/parser-results.md). Include:

- A generic family ID, layout version and implementation revision.
- Structural `matches(pages)` rules and `parse(pages, *, candidate=None)` producing
  existing candidate types. A format-specific `read_header(pages)` is optional.
- Synthetic expected headers, property identifiers, reconciled financial totals,
  overall billing periods and signed consumption.
- Regressions for misleading price/chart quantities, invalid fields and an
  unsupported structure. Keep earlier versions working.

Separate vendor recognition, extraction and property assignment. Match source
structures rather than vendor names, filenames or invoice dates. Preserve useful
fields and evidence on failure; parsers never approve records. Register built-in
modules in `bills/parsing/registry.py`. They currently require an image rebuild;
there is no mounted plugin loader.

## Checks

Run the [development checks](docs/development.md#checks) in Linux containers.
Parser and operational changes need relevant synthetic tests. Run the
[Chromium suite](tests/browser/README.md) for interactive changes and release work,
then the [production probe](docs/deployment.md#disposable-production-probe).
CI also checks locked exports, migration drift, workflows and dependencies.

Keep `uv.lock` and hash-pinned requirement exports aligned with exact direct pins.
`package-lock.json` pins the browser-only Playwright toolchain; update it together
with `package.json` and rebuild the browser target. Runtime images must exclude
test/browser dependencies. Documentation describes implemented behavior; keep
private plans and verification logs out of public guides.

Repository contributions use [PolyForm Noncommercial 1.0.0](LICENSE).
Independently authored parser plugins may use their authors' chosen licenses;
code copied or derived from this project remains subject to its license.
Preserve [NOTICE](NOTICE) and dependency notices. Report vulnerabilities through
[SECURITY.md](SECURITY.md).
