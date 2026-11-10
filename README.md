# House Bills

A small, self-hosted application for reviewing Swedish utility PDF invoices and
tracking household costs and billed consumption. Upload an invoice, check the
proposed values against the original, then approve it for reporting.

House Bills is a personal, early-stage project. It uses deterministic local
parsers and explicit approval. Original PDFs, configuration and billing records
stay in private installation storage. The application has no telemetry, cloud
extraction service or runtime CDN dependencies.

## What it does

- Configure properties, vendors, issuer identification and property mappings.
- Configure each property's services, billing frequency and vendor history, and identify
  missing invoices separately from received invoices awaiting review.
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
- Show the application version and GitHub/release links in the footer, with an
  update indicator when a newer stable release is available.

## Vendors and formats

Vendor names and issuer identifiers are configured by the operator. Automatic
extraction supports these structures; adding a vendor also requires selecting
a compatible invoice format.

| Format | Supported structure |
| --- | --- |
| `layout_01/v1` | Contract-based network, water, waste and supply; section continuations, waste weights and dated water reversals. |
| `layout_01/v2` | Historical product-number network, water and supply specifications. |
| `layout_02/v1` | Electricity supply with installation details above each specification, including shared invoices. |
| `layout_03/v1` | Quarterly waste or dated tank emptying with a VAT basis and payment summary; container sizes remain source detail. |
| `layout_04/v1` | Electricity supply with cover totals and a separate single-installation specification. |
| `layout_05/v1` | Electricity supply with a consumption table beside a historical chart. |
| `layout_05/v2` | Electricity supply with printed billed consumption in a meter-reading table beside a chart. |
| `layout_06/v1` | Monthly network and energy-tax totals with billed electricity quantities. |
| `layout_06/v2` | Network and energy-tax totals with exclusive VAT prices or dated price changes. |
| `layout_07/v1` | Electricity supply with area-labelled consumption and inclusive VAT totals. |
| `layout_08/v1` | Compact electricity supply with printed consumption, VAT, total and rounding. |

These families have representative source validation and synthetic regressions.
`layout_02/v2` additionally demonstrates a replacement using synthetic examples.
Support does not cover every vendor, historical version or future layout change.
The [parser guide](docs/parsing.md) describes recognition, versions and failures.

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
through an existing reverse proxy. See [installation and upgrades](docs/deployment.md)
and verify [backup and clean restoration](docs/backup-and-recovery.md) before normal
real-data use. Release publishing is configured; the first published image and NAS
verification are pending. A local production build is available for evaluation.

## Limits and documentation

This is a shared private installation: signed-in users can access its billing
records. It has no tenant or per-property isolation. Parsing is synchronous;
NAS timing has not been measured. Reports use invoice dates for costs and whole
billed consumption periods, without calendar-month proration. Individual fee rows
stay in the original PDF. Folder import, historical OCR, Grafana access transport
and loading parser plugins from a mounted directory are not implemented.

- [Configuration and data model](docs/data-model.md)
- [Uploads, originals and recovery](docs/ingestion.md)
- [Review, approval and correction](docs/review.md)
- [Approved reporting](docs/reporting.md) and [SQL examples](docs/reporting-examples.sql)
- [Parser result contract](docs/parser-results.md) and [parser maintenance](docs/parsing.md)
- [Security expectations](SECURITY.md) and [contributing](CONTRIBUTING.md)

House Bills is source-available under [PolyForm Noncommercial 1.0.0](LICENSE).
See [NOTICE](NOTICE) for attribution and the boundary for independently authored
parser plugins and separately licensed dependencies.
