# Operational backup and recovery

This is the canonical procedure for database/original backup and clean restoration.
Packages contain configuration, accounts, password hashes, sessions, invoices,
extraction/review history, originals, pending uploads and operator-selected recovery
files. Keep them private. They are disaster-recovery packages, not portable exports.

## Mount recovery storage

Use the intended instance and settings. Keep backups outside the live database
and originals directories. The supplied Compose file mounts no recovery storage.
Create an ignored `private/recovery.compose.yaml` with installation-specific paths:

```yaml
services:
  app:
    volumes:
      - ${APP_BACKUP_PATH:?Set private backup storage}:/backups
      - ${APP_RECOVERY_CONFIG_PATH:?Set private recovery configuration}:/recovery:ro
      - ${APP_RESTORE_PARENT:?Set a separate restore parent}:/restores
```

Give the configured non-root UID/GID access. Place environment and private
Compose/proxy files needed for recovery under the read-only mount. Protect and
keep them stable during backup. The command copies only files explicitly supplied
with `--config`; it does not export environment variables or guess configuration.

## Create and verify

```sh
docker compose -f compose.yaml -f private/recovery.compose.yaml run --rm app python manage.py backup_bills /backups/example-001 --config /recovery/operator.env --config /recovery/installation.compose.yaml
docker compose -f compose.yaml -f private/recovery.compose.yaml run --rm app python manage.py verify_backup /backups/example-001
```

Existing destinations are rejected. Backup takes the startup-migration and
originals locks, creates a SQLite backup-API snapshot, then copies files without
holding a database write transaction. Account changes can continue; the snapshot
represents one consistent state. Supported billing/storage writers wait until
copying finishes. Do not run manual schema migrations concurrently.

Packages use `database.sqlite3`, `invoices/`, `configuration/` and a versioned
`manifest.json`. They include staging receipts, partial uploads and files needing
operator inspection; transient originals/parse locks are recreated on restore.
Verification checks every file's SHA-256 and size, rejects unlisted files and
symlinks, checks SQLite integrity and foreign keys, and verifies checksum-correct
recoverable bytes for each uploaded document and valid complete intake receipt.
Manual invoices have no original by design: their database rows are verified to have an
empty checksum and no staging/publication paths, and no source file is expected.
Approved values and review snapshots remain recoverable from the database snapshot. Missing
or corrupt originals prevent publication of a complete backup.

The snapshot uses rollback journaling and is self-contained. Never copy a live
SQLite database, WAL and shared-memory files separately as a backup. Completed
packages publish from flushed temporary directories; incomplete packages are not
restorable. Checksums detect corruption; trust and protect the backup source.

For a disposable development instance:

```sh
docker compose -f compose.development.yaml run --rm app python manage.py backup_bills /state/backups/example-001 --config /state/development/.development-secret
```

That includes only its generated secret and stays on the development volume.
Supply additional recovery files as needed and keep an independent copy outside
live storage. Scheduling, retention, off-host copies and encryption are operator
responsibilities. Protect Windows copies with private ACLs; POSIX modes do not
establish Windows permissions.

## Restore into a clean instance

```sh
docker compose -f compose.yaml -f private/recovery.compose.yaml run --rm app python manage.py restore_bills /backups/example-001 /restores/example-001
```

The destination must not exist and must be outside the running instance and
completed backup. Restore verifies the package, copies `db.sqlite3` and
`invoices/`, rechecks copied files and the database, and publishes a new private
directory. It never merges with or replaces the running installation.

Recovered configuration goes under `recovered-configuration/` with numbered
filenames. Review it before use: retain the secret and relevant settings, but
select the new instance's data path, image, host and proxy routing. Configuration
is not executed or automatically applied. Use the source instance's tested image
for the first restore check; upgrade separately afterward.

Point a separate private Compose installation at the restored directory and
reviewed environment. Use distinct proxy routing and
`HOUSE_BILLS_MIGRATE_ON_START=false` for its first startup:

```sh
docker compose run --rm app python manage.py migrate --check
docker compose run --rm app python manage.py check --deploy --database default
docker compose up -d --no-build app
```

Sign in and check configuration, totals, signed consumption, correction history,
Reports and exact-byte authenticated downloads. Confirm anonymous source blocking.
For interrupted uploads/publication, run `python manage.py recover_documents` in
the restored instance. Complete receipts resume intake/publication; incomplete or
malformed files remain for inspection. See [ingestion recovery](ingestion.md#recovery-and-backup-coordination).

Verify this process on the intended host before normal real-data use. Retain the
original installation and backup until the separate restored instance is checked.
