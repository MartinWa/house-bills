# Private deployment

The application provides one Waitress process with two threads, Django authentication,
staff billing configuration, private PDF uploads, deterministic parsing, manual review
and correction, approved invoice screens and authenticated reports in host-local
SQLite. Coordinated backups and clean restoration are implemented and checked on
disposable local instances. Verify them on the intended host before normal real-data use.
Grafana access transport and NAS operation remain unverified. The first published image
is pending; local production builds support evaluation with synthetic invoices.

## Runtime and storage

The Dockerfile shares Python 3.14.10, Django 5.2 LTS and checksum-pinned SQLite 3.51.3
between development and production. `tools/check_runtime.py` verifies the actual linked
library and imports the PDF extraction runtime. Django also rejects SQLite releases
without the WAL-reset fix; fixed upstream/backport series are 3.51.3+, 3.50.7 within
3.50.x and 3.44.6 within 3.44.x.

Every Django connection enables WAL and `synchronous=FULL`, with a 10-second busy
timeout. Explicit transactions use IMMEDIATE; request-wide transactions are disabled.
Keep PDF reading and file processing outside short database writes. Store the whole
database directory on the server's local filesystem, including its WAL and shared-memory
files. Do not put SQLite on a network share or the Windows development bind mount.

The Docker build context permits only source, synthetic tests, build tools and locked
dependency inputs. Planning, test-data, private state, `.env` files and databases are
excluded. Production copies application code, collected static files and smoke-check
tools; no tests or development dependencies are shipped. Static assets are readable by
the configured non-root runtime. Original storage has no public URL route.

## Configure and initialize

Choose a tested image through `HOUSE_BILLS_IMAGE`. For local evaluation, build:

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
| `HOUSE_BILLS_IMAGE` | Required local image tag or published GHCR version/digest. Defaults to `house-bills:local` in the example. |
| `HOUSE_BILLS_SECRET_KEY` | Private random secret of at least 50 characters. Preserve across restarts. |
| `HOUSE_BILLS_ALLOWED_HOSTS` | Comma-separated exact hostnames, without schemes or wildcards. |
| `HOUSE_BILLS_CSRF_TRUSTED_ORIGINS` | Comma-separated exact trusted HTTPS origins. |
| `HOUSE_BILLS_TIME_ZONE` | Defaults to `Europe/Stockholm`. |
| `HOUSE_BILLS_UPDATE_CHECK` | Check public GitHub release metadata; defaults to `true`. Set `false` to disable outbound checks. |
| `HOUSE_BILLS_MIGRATE_ON_START` | Apply migrations before serving; defaults to `true`. |
| `HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES` | Original PDF limit; defaults to 20971520 (20 MiB), with 64 KiB extra request allowance. |
| `HOUSE_BILLS_TRUST_PROXY` | Trust only `X-Forwarded-Proto`; defaults to `false`. |
| `APP_UID`, `APP_GID` | Non-root numeric container process IDs. |
| `APP_DATA_PATH` | Absolute private host-local data directory. |
| `PROXY_NETWORK` | Existing private Docker network used by the HTTPS reverse proxy. |

After a release is published, select its tested `ghcr.io/martinwa/house-bills:X.Y.Z`
tag or immutable digest and run `docker compose pull app`. The release workflow currently
targets Linux/amd64. Keep the selected version/digest with the recovery configuration;
avoid silently following `latest` for installation upgrades. The local Docker build
sets `HOUSE_BILLS_VERSION=development` unless a version is explicitly supplied with
`--build-arg HOUSE_BILLS_VERSION=X.Y.Z`.

The footer shows the application version and links to releases and GitHub. Local
development detects the checkout's Git version at startup when `HOUSE_BILLS_VERSION`
is unset. The footer shows the base release version and keeps the exact identifier,
including commit distance/hash and `-dirty`, in its tooltip. Without a release tag or
Git metadata, it shows **Development**. Restart the server after checkout changes to
refresh the identifier. The static package version is local tooling metadata.

On signed-in page loads, JavaScript requests release status from the application.
For stable release versions, the server checks GitHub's public latest-release API
without credentials or billing data, caching valid metadata for six hours and failures
for one hour. A small dot appears beside the version when a newer stable release
exists. The check has a three-second network timeout and runs separately from page
rendering. Development versions skip checks. Set `HOUSE_BILLS_UPDATE_CHECK=false`
to disable them; version and GitHub links still appear. Install updates manually
using the [upgrade procedure](#manual-upgrades).

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
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py createsuperuser
docker compose run --rm app python manage.py check --deploy --database default
docker compose up -d --no-build app
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

Image replacement must retain `/data` and the private environment. Follow
[backup and recovery](backup-and-recovery.md) to create a verified package and start a
separate restored instance with the same tested image before normal real-data use.
Houses, vendors, invoice formats and identifier mappings are database
records, configured through the authenticated admin; they persist in `/data` across
restarts and image updates. See [configuration and the data model](data-model.md).
Originals live in `/data/invoices`, with authenticated downloads and resumable staging
and publication. See [uploads and private originals](ingestion.md) for recovery and
the shared-lock contract used by operational backups.

Migration `0007_cost_totals` replaces individual fee records with dated service totals.
Existing fee rows are archived privately before removing their table. Invoice identities,
approved totals, usage and immutable extraction/review history are preserved. Older
proposals can still be reviewed without re-parsing; see [the data model](data-model.md).

## Manual upgrades

Create and verify a coordinated backup including the private environment and
Compose/proxy configuration. Record the current image digest. Select the new tested
release in `HOUSE_BILLS_IMAGE`, pull it, then stop the serving instance before applying
schema migrations:

```sh
docker compose pull app
docker compose stop app
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py migrate --check
docker compose run --rm app python manage.py check --deploy --database default
docker compose up -d --no-build app
docker compose logs --tail=100 app
```

For a local image, build it explicitly before stopping the app instead of pulling.
Confirm configuration, approved totals, signed usage, correction history, Reports and
authenticated original downloads. Run the clean-restoration procedure independently;
image updates do not replace installation state or accepted corrections. If an upgrade
fails, retain the failed instance for inspection and restore the verified pre-upgrade
backup into a separate directory using its previous image. Do not assume that an older
image can read a migrated database.

Releases never deploy or update an installation automatically. NAS verification remains
a documented follow-up, including proxy routing, permissions, upload timing, review and
clean restoration. Grafana's read-only integration must be verified separately.

## Disposable production probe

This invokes the same production checks as CI. The temporary data disappears when
the container exits; no installation configuration or real invoice data is used.

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /data:mode=1777 --tmpfs /tmp --env HOUSE_BILLS_SECRET_KEY=synthetic-container-check-key-never-for-runtime-000000000000000000 --env HOUSE_BILLS_ALLOWED_HOSTS=ci.invalid house-bills:local python -m tools.check_production
```

The probe verifies parsed/manual upload and review, correction history, reporting,
duplicates, private source access, migration guards and static/HTTPS behavior. It creates
a checksum-verified backup, restores all database rows and originals into a new directory,
then starts that instance with migrations disabled and checks authenticated HTTP reports
and original downloads. This establishes local image behavior, not NAS deployment.
