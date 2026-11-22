"""Lazily built registry of the operator-installed parser plugins."""

import hashlib
import sys
import threading
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from . import plugins
from .api import API_VERSION

_cache = None
_build_lock = threading.Lock()
_failures = ContextVar("house_bills_parser_failures", default=None)


def parser_fingerprint(parser):
    return getattr(parser, "FINGERPRINT", None)


def parser_code_fingerprint(parser):
    """Source fingerprint of a parser together with its shared siblings."""
    return getattr(parser, "CODE_FINGERPRINT", None)


def parser_identity(parser):
    """Host-generated, bounded identity; no path or exception detail."""
    return {
        "family": parser.FAMILY,
        "version": parser.VERSION,
        "revision": parser.REVISION,
        "fingerprint": parser_fingerprint(parser),
        "code_fingerprint": parser_code_fingerprint(parser),
    }


@lru_cache(maxsize=1)
def host_implementation_digest():
    """Digest of the host parser implementation that runs extraction and hooks.

    Retained corrections replay stored evidence through this code, so a correction
    requires the same host implementation that produced the proposal. An unreadable
    source set yields an empty digest and refuses corrections instead of guessing.
    """
    package = Path(__file__).resolve().parent
    sources = [(f"parsing/{path.name}", path) for path in package.glob("*.py")]
    sources.append(("categories.py", package.parent / "categories.py"))
    digest = hashlib.sha256(b"house-bills-host-implementation-v1\n")
    try:
        for label, path in sorted(sources):
            digest.update(f"{label}\0".encode())
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def _configured_directory():
    try:
        from django.conf import settings

        value = getattr(settings, "HOUSE_BILLS_PARSER_PLUGINS", "")
    except Exception:
        return ""
    return value or ""


def _build():
    global _cache
    if _cache is not None:
        return _cache
    with _build_lock:
        if _cache is not None:
            return _cache
        directory = _configured_directory()
        try:
            discovery = plugins.discover(directory)
        except Exception:
            discovery = plugins.Discovery(directory=str(directory))
            discovery.errors.append(plugins.PluginError("", "discovery_failed"))
            discovery.digest = plugins.plugin_set_digest(())
        _cache = (tuple(discovery.parsers), discovery)
    return _cache


def reset():
    """Invalidate the cached registry and forget executed plugin modules."""
    global _cache
    _cache = None
    _failures.set(None)
    for name in list(sys.modules):
        if name == plugins.PLUGIN_NAMESPACE or name.startswith(f"{plugins.PLUGIN_NAMESPACE}."):
            sys.modules.pop(name, None)


def status():
    """Discovery outcome, including rejected files and load errors."""
    return _build()[1]


def parsers():
    """Installed parsers in discovery order."""
    return list(_build()[0])


def layouts():
    """Return ``{family: {version: revision}}`` for installed parsers."""
    result = {}
    for parser in parsers():
        result.setdefault(parser.FAMILY, {})[parser.VERSION] = parser.REVISION
    return result


def layout_family_choices():
    """Callable choices source; migrations serialize this reference."""
    return [(family, family) for family in layouts()]


def installed(family, version):
    """Resolve the exact installed family/version, never a newest-version fallback."""
    for parser in parsers():
        if parser.FAMILY == family and parser.VERSION == version:
            return parser
    return None


def parser_set_digest(app_version):
    """Digest over the loaded plugin set, the API version and the installed release."""
    discovery = _build()[1]
    payload = f"{discovery.digest}\0{API_VERSION}\0{app_version}".encode()
    return hashlib.sha256(b"house-bills-routing-v1\n" + payload).hexdigest()


def begin_document():
    """Start collecting per-document parser failures in this context."""
    _failures.set([])


def record_failure(parser, stage, code):
    failures = _failures.get()
    if failures is None:
        return
    failures.append({"parser": parser_identity(parser), "stage": stage, "code": code})


def take_failures():
    failures = _failures.get()
    _failures.set(None)
    return list(failures or ())


def match_layouts(pages, families):
    """Structural matches inside the selected families; guards each matcher."""
    selected = set(families)
    matches = []
    for parser in parsers():
        if parser.FAMILY not in selected:
            continue
        try:
            matched = parser.matches(pages)
        except Exception:
            record_failure(parser, "matches", "exception")
            continue
        if matched is True:
            matches.append(parser)
        elif matched is not False:
            record_failure(parser, "matches", "invalid_result")
    return matches
