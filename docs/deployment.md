# Private deployment

The application provides one Waitress process with two threads, Django authentication,
staff billing configuration, private PDF uploads, deterministic parsing, manual review
and correction, approved invoice screens and authenticated reports in host-local
SQLite. Coordinated backups and clean restoration are implemented and checked on
disposable local instances; verify them on the intended host before normal real-data use.
See [status and limits](../README.md#limits-and-documentation) for unverified areas.

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

Copy `.env.example` to an untracked `.env` (`Copy-Item .env.example .env` in PowerShell)
and replace its placeholders. Choose a tested image through `HOUSE_BILLS_IMAGE`.
For local evaluation, build from the working copy with the local-build override, which
always builds the production target and tags `house-bills:local`:

```sh
docker compose -f compose.yaml -f compose.local-build.yaml build app
```

Generate a random secret with that image and keep it across restarts:

```sh
docker run --rm house-bills:local python -c "import secrets; print(secrets.token_urlsafe(64))"
```

| Setting | Purpose |
| --- | --- |
| `HOUSE_BILLS_IMAGE` | Required local image tag or published GHCR version/digest. Defaults to `house-bills:local` in the example. |
| `HOUSE_BILLS_SECRET_KEY` | Private random secret of at least 50 characters. Preserve across restarts. |
| `HOUSE_BILLS_SECRET_KEY_FILE` | Read the secret from a Docker or Podman secret file instead of `HOUSE_BILLS_SECRET_KEY`. |
| `HOUSE_BILLS_ALLOWED_HOSTS` | Comma-separated exact hostnames, without schemes or wildcards. |
| `HOUSE_BILLS_CSRF_TRUSTED_ORIGINS` | Comma-separated exact trusted HTTPS origins. |
| `HOUSE_BILLS_TIME_ZONE` | Defaults to `Europe/Stockholm`. |
| `HOUSE_BILLS_UPDATE_CHECK` | Check public GitHub release metadata; defaults to `true`. Set `false` to disable outbound checks. |
| `HOUSE_BILLS_MIGRATE_ON_START` | Apply migrations before serving; defaults to `true`. |
| `HOUSE_BILLS_MAX_PDF_UPLOAD_BYTES` | Original PDF limit; defaults to 20971520 (20 MiB), with 64 KiB extra request allowance. |
| `HOUSE_BILLS_TRUST_PROXY` | Trust the proxy's `X-Forwarded-Proto` and `X-Forwarded-For`; defaults to `false`. |
| `HOUSE_BILLS_PARSER_PLUGINS` | Absolute path to the operator-managed parser directory mounted read-only (recommended `/parsers`). Empty by default; without it nothing is parsed. |
| `HOUSE_BILLS_PARSER_PLUGINS_FILE` | Read the parser directory from a Docker or Podman secret file instead of the value above. |
| `APP_UID`, `APP_GID` | Non-root numeric container process IDs. |
| `APP_DATA_PATH` | Absolute private host-local data directory. |
| `APP_PARSER_PATH` | Existing operator-owned host directory bound read-only at `/parsers` by `compose.parser-plugins.yaml`. |
| `HOUSE_BILLS_NETWORK` | Private network name the base file creates; defaults to `house-bills`. |
| `HOUSE_BILLS_HOST_PORT` | Loopback port for `compose.host-port.yaml`; defaults to 8000. |
| `PROXY_NETWORK` | Existing private Docker network joined by `compose.proxy-network.yaml`. |

After a release is published, select its tested `ghcr.io/martinwa/house-bills:X.Y.Z`
tag or immutable digest and run `docker compose pull app`. Each release publishes
`linux/amd64` and `linux/arm64` under one image index; a host pulls the platform it
runs. Keep the selected version/digest with the recovery configuration; avoid
silently following `latest` for installation upgrades. The local Docker build sets
`HOUSE_BILLS_VERSION=development` unless a version is explicitly supplied with
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

The base file publishes no host ports, bundles no proxy and creates one private bridge
network named by `HOUSE_BILLS_NETWORK` (default `house-bills`). Choose the pattern that
matches the HTTPS reverse proxy, and add the same `-f` override to every command below:

| Pattern | How to run it |
| --- | --- |
| Proxy container on the same network | Attach it with `docker network connect house-bills <proxy-container>` (or the `HOUSE_BILLS_NETWORK` you set) and forward to `app:8000`. |
| Proxy that already owns a private network | Add `-f compose.proxy-network.yaml` and set `PROXY_NETWORK` to that network. |
| Proxy running on the host | Add `-f compose.host-port.yaml`; the app is published on `127.0.0.1:${HOUSE_BILLS_HOST_PORT:-8000}`. |

An installation created from an earlier `compose.yaml` joined an external proxy network by
default; add `-f compose.proxy-network.yaml` with `PROXY_NETWORK` set to that network to
keep the same attachment. The data directory is unchanged either way.

When the proxy terminates HTTPS, set `HOUSE_BILLS_TRUST_PROXY=true` and configure it to
discard visitor-supplied `X-Forwarded-Proto` and replace it with the original scheme,
and to set `X-Forwarded-For` to the visitor's address. Trust applies to peers that can
reach the application port, so restrict this network to trusted proxy peers, and never
publish the application port directly. Waitress removes untrusted forwarding headers and
keeps only the scheme and client-address headers when proxy trust is enabled. Because
that trust is a wildcard peer, a visitor who reaches the port directly can spoof
`X-Forwarded-For`: sign-in rate limiting still holds per username, but the address in the
pairing can be evaded. Secure cookies and HTTPS redirects stay enabled.

```sh
docker compose config --quiet
docker compose run --rm app python manage.py migrate --noinput
docker compose run --rm app python manage.py createsuperuser
docker compose run --rm app python manage.py check --deploy --database default
docker compose up -d app
docker compose logs --tail=100 app
```

For a local build, keep one override set and use
`docker compose -f compose.yaml -f compose.local-build.yaml up -d app`, which builds
the production target because the override pins `pull_policy: build`.

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
API token hashes also persist in that database, so clients can keep the same token
across image updates. A Grafana client must retain its own database and encryption key
across Grafana updates; see [Grafana setup](api.md#approved-reporting-and-grafana).
Originals live in `/data/invoices`, with authenticated downloads and resumable staging
and publication. See [uploads and private originals](ingestion.md) for recovery and
the shared-lock contract used by operational backups.

## Parser directory

The image contains no parser code. Every layout, including the examples in
`docs/examples/parsers/`, is installed as single-file Python parsers from an
operator-managed directory, so a new vendor format or a fix never needs an image rebuild.
This is an operator deployment action with the trust model in
[SECURITY.md](../SECURITY.md): parser code runs with application privileges. There is no
upload, source editor or automatic update path. Without a parser directory the
application still runs and stores every upload as an unsupported review proposal.

Add the override and point it at an existing host directory outside `APP_DATA_PATH`,
after copying the reviewed files you want to install:

```sh
# Private .env
APP_PARSER_PATH=/opt/house-bills/parsers
HOUSE_BILLS_PARSER_PLUGINS=/parsers

docker compose -f compose.yaml -f compose.parser-plugins.yaml up -d app
docker compose -f compose.yaml -f compose.parser-plugins.yaml run --rm app \
  python manage.py parsers --check
```

```sh
cp docs/examples/parsers/*.py "$APP_PARSER_PATH"/
```

`compose.parser-plugins.yaml` binds `APP_PARSER_PATH` read-only at `/parsers` with
`bind.create_host_path: false`; it keeps the base capability drops, `no-new-privileges`,
private exposure and read-only image filesystem. The host directory must already exist,
owned by the installation operator, readable but not writable by the application UID,
and have no writable alias elsewhere in the container. A missing bind source stops
container creation; that is a Compose error, distinct from a configured directory that is
missing inside a running container.

Use `HOUSE_BILLS_PARSER_PLUGINS_FILE` instead when the path comes from a secret file. A
relative production path, or a path equal to or beneath `DATA_DIR` or `MEDIA_ROOT`
(including symlink aliases), rejects configuration. An unset or empty directory is a clean
state that parses nothing. A configured directory that is missing, unreadable or not a
directory does not stop startup: no parser is registered and `parsers --check` fails with
a load error. Rejected files are reported with bounded filenames and error codes, without
paths or exception text.

`parsers` lists each installed parser with its file, family/version/revision and
fingerprint, prints the host implementation digest that retained corrections require, and
reports vendor formats that have no installed parser.
`--check` exits non-zero on directory load errors, rejected files, such missing
references or an unreadable host implementation digest. Unset and empty directories pass
when no configured vendor references an unavailable family.

For diagnosis after a deploy or a parser change, staff open **Installed parsers** in the
User menu: this worker's loaded profiles, rejected files and vendor formats with no
installed parser. Every signed-in user sees the same load health on document and review
pages, so a reviewer can tell an unsupported layout from a rejected parser directory. The
page is diagnosis and cleanup help; `parsers --check` remains the deployment gate.

This release records the plugin filename in each new attempt's routing metadata. That
edits `bills/parsing/registry.py`, which is part of the host implementation digest, so the
digest moves once. Every attempt retained before the upgrade fails the host check:
retained waste-cost and electricity-demand corrections are refused until each approved
document is re-parsed and its proposal re-reviewed, as the [correction
policy](review.md#re-parsing-and-stale-forms) describes. The browser shows **The host
implementation changed** on those documents; it is a re-parse instruction, not an alarm.

A vendor selection whose parser is no longer installed stays listed with no versions and
can still be unselected, so cleanup after removing code needs no admin workaround:
selecting a format requires installed code, listing and unselecting do not.

Configure a vendor's **Invoice formats** after installing the directory: the choices come
from the installed set, so an empty registry offers none.

The loader reads top-level regular `*.py` files only and accepts at most 64 files of
1 MiB each. Names starting with `_` are support modules shared through sibling imports; a
file without that prefix must declare layout metadata. Bytecode caches are neither read nor
written, so the read-only image filesystem is unaffected. A family routes only after it is
added to that vendor's invoice formats; existing vendor records are not changed by
loading code. Adding, changing or removing a parser requires restarting the container and
explicitly re-parsing the affected documents with `manage.py parse_documents` (or the
document page). Approved values, review history and published originals stay unchanged
until a reviewed correction accepts different values.

Parser code is deliberately outside the backup and restore set: backups archive the
database and `MEDIA_ROOT`. Restoring an installation requires redeploying the same
reviewed directory; otherwise its extractions lose reproducibility. Review the source and
dependencies, test against wholly synthetic documents in a disposable container, and keep
the reviewed version and digest for rollback. See [deterministic parsing](parsing.md) for
the API, contract and a worked example.

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
docker compose up -d app
docker compose logs --tail=100 app
```

For a local build, add the same Compose overrides and run
`docker compose -f compose.yaml -f compose.local-build.yaml build app` before stopping
the app instead of pulling.
Confirm configuration, approved totals, signed usage, correction history, Reports and
authenticated original downloads. Run the clean-restoration procedure independently;
image updates do not replace installation state or accepted corrections. If an upgrade
fails, retain the failed instance for inspection and restore the verified pre-upgrade
backup into a separate directory using its previous image. Do not assume that an older
image can read a migrated database. Reversing migrations can silently discard configuration;
use backup restoration for rollback.

Releases never deploy or update an installation automatically.

## Disposable production probe

This invokes the same production checks as CI. The temporary data disappears when
the container exits; no installation configuration or real invoice data is used.

```sh
docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /data:mode=1777 --tmpfs /tmp --mount type=bind,source="$(pwd)/docs/examples/parsers",target=/parsers,readonly --env HOUSE_BILLS_PARSER_PLUGINS=/parsers --env HOUSE_BILLS_SECRET_KEY=synthetic-container-check-key-never-for-runtime-000000000000000000 --env HOUSE_BILLS_ALLOWED_HOSTS=ci.invalid house-bills:local python -m tools.check_production
```

The probe requires an installed parser directory: a parsed synthetic upload is part of
what it verifies, and the image ships none.

Mount a secret file and set `HOUSE_BILLS_SECRET_KEY_FILE` instead of
`HOUSE_BILLS_SECRET_KEY` to probe that path. The recovery configuration inside the probe
records the effective settings either way.

The probe verifies parsed/manual upload and review, correction history, reporting, duplicates,
private source access, migration guards and static/HTTPS behavior. It also checks parser
directory path policy, load status, fingerprints, the host implementation digest the
corrections require and the read-only mount, that
`parsers --check` rejects a configured vendor format with no installed parser even for an
empty or missing directory, and that a single-file parser loads when mounted. The strict
`docker run` check and the Chromium job mount the same reviewed directory read-only,
because neither build context contains parser code. CI also copies
`docs/examples/parsers/` into an operator-owned directory and runs the probe through
`compose.parser-plugins.yaml`, so the override, effective non-root UID, read-only mount,
denied writes and storage-path rejection are exercised together. It creates
a checksum-verified backup, restores all database rows and originals into a new directory,
then starts that instance with migrations disabled and checks authenticated HTTP reports
and original downloads. This establishes local image behavior, not NAS deployment.
