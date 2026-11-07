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

Parser modules are executable Python with application privileges, including
access to files, configuration and the database. Review third-party code before
building it into an installation. A parser plugin would have the same trust
boundary; Python imports are not a sandbox. Mounted plugin loading is not
currently implemented. Independently authored plugins retain their own licenses;
this does not reduce the privileges their code could exercise.

## Reporting vulnerabilities

Use the repository's [Security tab](https://github.com/MartinWa/house-bills/security)
for private vulnerability reporting when available. If unavailable,
[open an issue](https://github.com/MartinWa/house-bills/issues/new) requesting a private
contact channel without exploit details, credentials or invoice data. Use wholly
synthetic examples once a private channel has been agreed.

This personal project has no guaranteed response time, security support period
or service-level agreement. Evaluate updates before installing them; the maintainer
may only be able to address the current version.
