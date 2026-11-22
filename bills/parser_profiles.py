"""Bounded description of the parser profile recorded on an extraction attempt.

These helpers deliberately live outside `bills.parsing`: every source file in that
package feeds the host implementation digest, so a display helper placed there would
invalidate retained corrections. The codes below are shared with the correction
service, but each caller owns its own wording.
"""

from .parsing import registry
from .parsing.plugins import safe_filename

NOT_RECORDED = "not_recorded"
NOT_ROUTED = "not_routed"
NOT_INSTALLED = "not_installed"
REVISION_CHANGED = "revision_changed"
SOURCE_CHANGED = "source_changed"
CODE_CHANGED = "code_changed"
HOST_CHANGED = "host_changed"
MATCHES = "matches"

MAX_FAMILY = 40
MAX_VERSION = 40
MAX_REVISION = 80
MAX_FILENAME = 80
MAX_CODE = 40
MAX_FINGERPRINT = 64
MAX_ERRORS = 64
SHORT_FINGERPRINT = 8


def bounded(value, limit):
    """A single bounded, printable display value."""
    if not isinstance(value, str):
        return ""
    printable = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(printable.split())[:limit]


def recorded_routing(attempt):
    """The routing metadata and selected parser recorded on an attempt."""
    candidate = attempt.candidate if attempt is not None else None
    routing = candidate.get("routing") if isinstance(candidate, dict) else None
    if not isinstance(routing, dict):
        return {}, {}
    selected = routing.get("selected_parser")
    return routing, selected if isinstance(selected, dict) else {}


def profile_state(attempt):
    """Ordered installed-state codes; the first code is the reason to show.

    An attempt that predates parser identity has routing without a `selected_parser`
    key, and a current attempt without a selected parser never routed. Otherwise the
    checks match retained-correction verification exactly: installed format, revision,
    source fingerprint, shared code fingerprint and host implementation.
    """
    routing, selected = recorded_routing(attempt)
    if "selected_parser" not in routing:
        return (NOT_RECORDED,)
    if not selected:
        return (NOT_ROUTED,)
    parser = registry.installed(attempt.layout_family, attempt.layout_version)
    if parser is None:
        return (NOT_INSTALLED,)
    if parser.REVISION != attempt.parser_revision:
        return (REVISION_CHANGED,)
    fingerprint = selected.get("fingerprint")
    if not isinstance(fingerprint, str) or fingerprint != registry.parser_fingerprint(parser):
        return (SOURCE_CHANGED,)
    code = registry.parser_code_fingerprint(parser)
    if not code or selected.get("code_fingerprint") != code:
        return (CODE_CHANGED,)
    host = registry.host_implementation_digest()
    if not host or routing.get("host_implementation") != host:
        return (HOST_CHANGED,)
    return (MATCHES,)


def short_fingerprint(value):
    """A short fingerprint for display with the full value available as a title."""
    text = bounded(value, MAX_FINGERPRINT)
    if not text:
        return None
    return {"short": text[:SHORT_FINGERPRINT], "full": text}


def _plugin_errors(value):
    if not isinstance(value, list):
        return []
    rows = []
    for entry in value[:MAX_ERRORS]:
        if not isinstance(entry, dict):
            continue
        filename = entry.get("filename")
        rows.append(
            {
                "filename": safe_filename(filename) if filename else "",
                "code": bounded(entry.get("code"), MAX_CODE),
            }
        )
    return rows


def profile_summary(attempt):
    """Bounded display values for the profile recorded on an attempt, or None."""
    if attempt is None:
        return None
    routing, selected = recorded_routing(attempt)
    filename = selected.get("filename")
    return {
        "state": profile_state(attempt)[0],
        "family": bounded(attempt.layout_family, MAX_FAMILY),
        "version": bounded(attempt.layout_version, MAX_VERSION),
        "revision": bounded(attempt.parser_revision, MAX_REVISION),
        "filename": safe_filename(filename) if filename else "",
        "fingerprint": short_fingerprint(selected.get("fingerprint")),
        "code_fingerprint": short_fingerprint(selected.get("code_fingerprint")),
        "plugin_errors": _plugin_errors(routing.get("plugin_errors")),
    }


def installed_profile(parser):
    """Bounded display values for one installed parser module."""
    identity = registry.parser_identity(parser)
    filename = identity.get("filename")
    return {
        "family": bounded(identity.get("family"), MAX_FAMILY),
        "version": bounded(identity.get("version"), MAX_VERSION),
        "revision": bounded(identity.get("revision"), MAX_REVISION),
        "filename": safe_filename(filename) if filename else "",
        "fingerprint": short_fingerprint(identity.get("fingerprint")),
        "code_fingerprint": short_fingerprint(identity.get("code_fingerprint")),
        "dependencies": len(getattr(parser, "PLUGIN_DEPENDENCIES", ())),
    }
