# Private deployment

The application provides one Waitress process with two threads, Django authentication,
staff billing configuration, private PDF uploads and a validated invoice data model
in host-local SQLite. Production parsing, invoice review forms, reporting, operational
backup/restore and release publishing remain later milestones. Use synthetic data
for this stage.

## Runtime and storage

The Dockerfile shares Python 3.14.10, Django 5.2 LTS and checksum-pinned SQLite 3.51.3
between development and production. `tools/check_runtime.py` verifies the actual linked
library and imports the PDF extraction runtime. Django also rejects SQLite releases
without the WAL-reset fix; fixed upstream/backport series are 3.51.3+, 3.50.7 within
3.50.x and 3.44.6 within 3.44.x.

Every application connection enables WAL and `synchronous=FULL`, with a 10-second busy
timeout. Explicit transactions use IMMEDIATE; request-wide transactions are disabled.
Keep future parsing and file processing outside short database writes. Store the whole
database directory on the server's local filesystem, including its WAL and shared-memory
files. Do not put SQLite on a network share or the Windows development bind mount.

The Docker build context permits only source, synthetic tests, build tools and locked
dependency inputs. Planning, test-data, private state, `.env` files and databases are
excluded. Production copies application code, collected static files and smoke-check
tools; no tests or development dependencies are shipped. Static assets are readable by
the configured non-root runtime. Original storage has no public URL route.

## Configure and initialize

Build the local production image:

```sh
docker build --tag house-bills:local .
```

Copy `.env.example` to an untracked `.env` (`Copy-Item .env.example .env` in PowerShell)
and replace its placeholders. Generate a random secret, for example:

```sh
docker run --rm house-bills:local python -c "import secrets; print(secrets.token_urlsafe(64))"
```

| Setting | Purpose |
| --- | --- |
| `HOUSE_BILLS_SECRET_KEY` | Private random secret of at least 50 characters. Preserve across restarts. |
| `HOUSE_BILLS_ALLOWED_HOSTS` | Comma-separated exact hostnames, without schemes or wildcards. |
| `HOUSE_BILLS_CSRF_TRUSTED_ORIGINS` | Comma-separated exact trusted HTTPS origins. |
| `HOUSE_BILLS_TIME_ZONE` | Defaults to `Europe/Stockholm`. |
| `HOUSE_BILLS_MIGRATE_ON_START` | Apply migrations before serving; defaults to `true`. |
| `HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES` | Original PDF limit; defaults to 20971520 (20 MiB), with 64 KiB extra request allowance. |
| `HOUSE_BILLS_TRUST_PROXY` | Trust only `X-Forwarded-Proto`; defaults to `false`. |
| `APP_UID`, `APP_GID` | Non-root numeric container process IDs. |
| `APP_DATA_PATH` | Absolute private host-local data directory. |
| `PROXY_NETWORK` | Existing private Docker network used by the HTTPS reverse proxy. |

Create the data directory and grant the configured UID/GID access before startup.
Compose mounts it at `/data`, keeps the image filesystem read-only, provides `/tmp`
scratch storage, drops capabilities and bounds log retention. The server refuses root
execution, DEBUG settings and missing production configuration.

Point the existing private HTTPS reverse proxy at the service's container port 8000.
When the proxy terminates HTTPS, set `HOUSE_BILLS_TRUST_PROXY=true` and configure it to
discard visitor-supplied `X-Forwarded-Proto` and replace it with the original scheme.
Trust applies to peers that can reach the application port, so restrict this network
to trusted proxy peers. Waitress removes untrusted forwarding headers and accepts only
the scheme header when proxy trust is enabled. Secure cookies and HTTPS redirects stay
enabled. Production Compose publishes no host ports and bundles no proxy.

```sh
docker compose config --quiet
docker compose build app
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py createsuperuser
docker compose run --rm app python manage.py check --deploy --database default
docker compose up -d app
docker compose logs --tail=100 app
```

`check --deploy` retains the reference's visible W005/W021 warnings: this private service
does not enable HSTS for all sibling domains or request public preload. The production
probe fails on any other deployment-check issue.

## Startup migrations

Startup applies migrations under `/data/.migrate.lock`, with a five-minute lock timeout,
and verifies the schema before accepting requests. Migration errors stop startup.
With `HOUSE_BILLS_MIGRATE_ON_START=false`, missing databases or pending migrations stop
startup without initializing the schema. Run and verify the separate migration first:

```sh
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py migrate --check
docker compose up -d app
```

Image replacement must retain `/data` and the private environment. Coordinated backup
and clean restoration are a later milestone required before normal real-data use.
Houses, vendors, invoice formats and identifier mappings are database
records, configured through the authenticated admin; they persist in `/data` across
restarts and image updates. See [configuration and the data model](data-model.md).
Originals live in `/data/invoices`, with authenticated downloads and resumable staging
and publication. See [uploads and private originals](ingestion.md) for recovery and
the shared-lock contract required by the future backup integration.

## Disposable production probe

This invokes the same startup checks as baseline CI. The temporary data disappears when
the container exits; no installation configuration or real invoice data is used.

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /data:mode=1777 --tmpfs /tmp --env HOUSE_BILLS_SECRET_KEY=synthetic-container-check-key-never-for-runtime-000000000000000000 --env HOUSE_BILLS_ALLOWED_HOSTS=ci.invalid house-bills:local python -m tools.check_production
```
