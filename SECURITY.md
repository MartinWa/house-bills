# Security

House Bills is intended for a small, trusted, private self-hosted installation.
Keep it behind private HTTPS access and an existing managed reverse proxy, with
no published application port or router forwarding. Run a configured non-root
user on host-local storage and follow the [deployment guide](docs/deployment.md).
SQLite WAL storage must not be an SMB/NFS share mounted from another host.

Authentication protects application pages and original PDF viewing/downloads.
Every signed-in user can access billing records; there is no tenant or
per-property isolation. Staff configuration additionally uses Django model
permissions. Restrict accounts, application access and proxy peers to trusted users.

Originals, extracted evidence, identifiers, accounts and approval history stay in
local private storage. Static serving does not expose originals. The application
does not send telemetry or make outbound extraction/API calls, and serves its
own frontend assets. Dependency installation, image pulls, CI and operator-run
audits require external access. Operator-added software can change that boundary.
Opening PDFs also relies on the browser's PDF viewer.

Treat databases, originals, environment files, staging receipts and backups as
sensitive. Backup packages include accounts, password hashes, sessions and
operator-selected recovery configuration. Protect permissions, copies and
encryption according to your installation. Accept backups only from trusted
storage: checksums detect corruption and do not authenticate the publisher.
Verify a clean restore before real-data use and before relying on a new backup
procedure. See [backup and recovery](docs/backup-and-recovery.md).

The image ships no parser code. Every parser is installed by the operator, so all parser
code is executable Python outside this repository's review and runs with application
privileges, including access to files, configuration and the database. A parser can read
invoices and secrets, change records, delete writable files and use the process's network
access, including outbound connections. Python imports, private module names, API checks
and source fingerprints are not a sandbox and do not
restrict those privileges. Review code and dependencies before mounting it,
test against wholly synthetic documents in a disposable container with no real data or
production secrets, and retain the reviewed source, version and digest for rollback.
Fingerprints identify executed source; they provide no signature, author authentication
or safety assurance. The examples in `docs/examples/parsers/` are reviewed in this
repository but are no safer once copied: audit local changes too.

Parser installation is an operator deployment action: there is no
web, admin or API code upload, source editor, caller-selected import path, runtime
package installation or automatic update. Vendor format configuration may select
only families already installed in the registry; invoice names, contents and database
records never determine source paths. Mount parser code read-only from an operator-owned
host directory outside `APP_DATA_PATH`, with no writable alias elsewhere in the
container and protected parent directories. `compose.parser-plugins.yaml` supplies the
`/parsers` mount and the `HOUSE_BILLS_PARSER_PLUGINS` setting; leaving it unset parses
nothing and stores uploads for manual review. Parser code is deliberately not part of
backups or restore: redeploy the same reviewed directory with the image.

Enforceable controls are bounded: at most 64 top-level files and 1 MiB per source, only
regular files with Python-identifier names, no symlinks, directory-relative no-follow
checks on Linux, validated metadata and entry-point signatures, and rejection of
duplicate `(family, version)` identities. Configured paths equal to or inside
`DATA_DIR` or `MEDIA_ROOT`, including symlink aliases, are rejected; relative production
paths are rejected. Discovery and entry-point exception guards keep ordinary failures
from breaking startup or other documents, and host diagnostics use error codes and safe
parser identity/stage details instead of exception text, paths or source snippets. These
guards cannot contain arbitrary side effects, process termination, hangs or memory
exhaustion.

Keep the existing non-root user, capability drops, `no-new-privileges`, read-only image
filesystem and private HTTPS deployment. Set operator memory, CPU and PID limits; upload
byte and 100-page PDF limits still apply. These reduce host resource exposure but do not
stop a runaway parser from making the application unavailable, and HTTP/proxy or
thread-future timeouts cannot terminate Python execution. The private Compose bridge does
not block internet egress: restrict destinations at the host/network boundary when
needed. `HOUSE_BILLS_UPDATE_CHECK=false` disables only the host release check. Run
`python manage.py parsers --check` after deployment and treat its load errors and
unavailable vendor formats as deployment failures. Independently authored parsers retain
their own licenses; this does not reduce the privileges their code could exercise. Code
copied or derived from this project, including the shipped examples, remains subject to
`LICENSE`.

## Reporting vulnerabilities

Use the repository's [Security tab](https://github.com/MartinWa/house-bills/security)
for private vulnerability reporting when available. If unavailable,
[open an issue](https://github.com/MartinWa/house-bills/issues/new) requesting a private
contact channel without exploit details, credentials or invoice data. Use wholly
synthetic examples once a private channel has been agreed.

This personal project has no guaranteed response time, security support period
or service-level agreement. Evaluate updates before installing them; the maintainer
may only be able to address the current version.
