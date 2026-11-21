# House Bills

A small, self-hosted application for reviewing Swedish utility PDF invoices and
tracking household costs and billed consumption. Upload an invoice, check the
proposed values against the original, then approve it for reporting.

House Bills is a personal, early-stage project. It uses deterministic local
parsers and explicit approval. Original PDFs, configuration and billing records
stay in private installation storage. The application has no telemetry, cloud
extraction service or runtime CDN dependencies.

## What it does

- Configure properties, vendors, issuer identification and invoice property rules.
- Configure property services and vendor history, with shared dated vendor billing schedules.
- Track missing combined bills separately from received invoices needing review or
  incomplete service allocations.
- Maintain shared cost and consumption subcategories under Electricity, Water, Waste and Other.
- Separate printed electricity demand charges (Effektavgift) from Elnät costs and track
  billed kW with period averages, highest and lowest billed months and invoice details.
- Mark confirmed months with no invoice as not sent, retaining a reason and later expectations.
- Preserve original bytes and reuse exact duplicate uploads, including invoices
  copied into different property folders.
- Extract invoice headers, dated property/service cost totals and signed billed
  consumption. One invoice can cover several services or properties.
- Review missing or uncertain fields, enter unsupported invoices manually,
  approve records and retain correction and extraction history.
- Add invoices without an original PDF, such as historical paper or ledger
  entries, and include them in reports with PDF and manual source counts.
- Re-parse selected originals and review proposed changes while retaining
  approved corrections until changes are accepted.
- Report approved costs and consumption with independent totals, network-first
  electricity selection and visible exclusions for ambiguous coverage.
- Create verified database/original backups and restore into a new instance.
- Automate configuration, ingestion, review and approved reports through a versioned HTTP API.
- Show the application version and GitHub/release links in the footer, with an
  update indicator when a newer stable release is available.

## Vendors and formats

Vendor names and issuer identifiers are configured by the operator. Adding a vendor
also requires selecting a compatible installed invoice format. The
[parser guide](docs/parsing.md) lists every supported structure, version and revision.
Support does not cover every vendor, historical version or future layout change.

An unknown, unreadable or invalid invoice retains its original and opens for
[manual review](docs/review.md). Configure its vendor, enter the header, service
costs and any consumption, then approve it. OCR is not included.

## Evaluate locally

Use Linux containers with a running Docker daemon, including when editing on
Windows. From a checkout:

```sh
docker compose -f compose.development.yaml up --build -d app
docker compose -f compose.development.yaml exec app python manage.py createsuperuser
```

Open <http://localhost:8001/>. The source is mounted from the checkout; the
database, secret and caches live in a separate Linux volume. Start with invented
invoices. See [initial setup](docs/setup.md) for the first approved bill and
[development](docs/development.md) for tests and browser checks.

Production uses a non-root, read-only image, host-local SQLite and private HTTPS
through an existing reverse proxy. Released images cover `linux/amd64` and
`linux/arm64`. A local production build is available for evaluation. Optional Compose
overrides publish a loopback port for a proxy on the host, join an existing proxy
network, or build from the working copy. See [installation and upgrades](docs/deployment.md)
and verify [backup and clean restoration](docs/backup-and-recovery.md) before normal
real-data use.

## Limits and documentation

This is a shared private installation: signed-in users can access its billing
records. It has no tenant or per-property isolation. Parsing is synchronous;
NAS timing has not been measured. Reports use invoice dates for costs and whole
billed consumption periods, without calendar-month proration. Selected demand and waste
fraction charges are separated; remaining fee details stay in the original PDF.
Folder import, historical OCR, Grafana access transport
and loading parser plugins from a mounted directory are not implemented.

Release publishing is configured, but the first published image and NAS verification
are pending; local production builds and the automated Grafana check support evaluation
with synthetic data.

- [HTTP API and automation](docs/api.md), [OpenAPI schema](docs/api/openapi.json)
- [Configuration and data model](docs/data-model.md)
- [Uploads, originals and recovery](docs/ingestion.md)
- [Review, approval and correction](docs/review.md)
- [Approved reporting](docs/reporting.md) and [SQL examples](docs/reporting-examples.sql)
- [Parser result contract](docs/parser-results.md) and [parser maintenance](docs/parsing.md)
- [Security expectations](SECURITY.md), [contributing](CONTRIBUTING.md) and the
  [contributor license agreement](CLA.md)

House Bills is source-available under [PolyForm Noncommercial 1.0.0](LICENSE).
Noncommercial use needs nothing further; commercial use requires a separate paid
license from the copyright holder, arranged through the repository's Discussions.
See [NOTICE](NOTICE) for attribution and the boundary for independently authored
parser plugins and separately licensed dependencies.

Contributions are accepted under [CLA.md](CLA.md), which keeps the contributor's
copyright and grants the project the rights that dual licensing needs.
