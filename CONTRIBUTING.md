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

Keep deployment hostnames, private repository references and consumer configuration
out of public source, documentation, PR descriptions, comments and CI artifacts.
Use generic examples here. Consumer-specific generators and validation scenarios
belong with the consumer.

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
fields and evidence on failure; parsers never approve records. Parsers are installed
plugin files, never compiled into the image; new formats and fixes belong in
`docs/examples/parsers/` as reviewed examples (see
[parser maintenance](docs/parsing.md#maintenance)).

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

Contributions use the project [license](LICENSE); independently authored parsers
may use their own, but derived code remains subject to it. Preserve [NOTICE](NOTICE) and
dependency notices. Report vulnerabilities through [SECURITY.md](SECURITY.md).

## Contributor agreement

Before a first pull request can be merged, sign the [Contributor License
Agreement](CLA.md). An app comments on the pull request with a signing link; one
signature covers your future contributions too. You keep the copyright in your
contribution.

Signing matters because the project is dual licensed: it is published under the
PolyForm Noncommercial License 1.0.0 and also offered commercially. The agreement is
what lets a contribution be part of both. If you contribute in the course of
employment, read section 6 first: your employer may own the work, and an authorised
person has to sign for the entity.

The project does not use a Developer Certificate of Origin (`Signed-off-by`), because
a DCO certifies that a contribution may be distributed under the project's current
licence and does not permit relicensing it.

### For maintainers

Signatures are collected by [CLA Assistant](https://cla-assistant.io), a free GitHub
app, not by a workflow in this repository:

1. Create a public gist containing `CLA.md` and copy its URL.
2. Sign in at <https://cla-assistant.io> with GitHub, add this repository, and paste
the gist URL.
3. Make **CLA Assistant** a required status check in the `Protect main` ruleset.

Signatures are stored in that gist, so contributor accounts never enter this
repository. The `contributor-assistant/github-action` action is not used: it was
archived upstream in 2026 and has no maintained release.
