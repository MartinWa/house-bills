"""Lazily built registry of built-in parsers and operator-installed plugins."""

import hashlib
import sys
import threading
from contextvars import ContextVar

from . import (
    layout_01,
    layout_01_v2,
    layout_02,
    layout_02_v2,
    layout_03,
    layout_04,
    layout_05,
    layout_05_v2,
    layout_06,
    layout_06_v2,
    layout_07,
    layout_08,
    plugins,
)
from .api import API_VERSION

BUILTIN_PARSERS = (
    layout_01,
    layout_01_v2,
    layout_02,
    layout_02_v2,
    layout_03,
    layout_04,
    layout_05,
    layout_05_v2,
    layout_06,
    layout_06_v2,
    layout_07,
    layout_08,
)

_cache = None
_build_lock = threading.Lock()
_failures = ContextVar("house_bills_parser_failures", default=None)


def parser_source(parser):
    return getattr(parser, "PARSER_SOURCE", "builtin")


def parser_fingerprint(parser):
    return getattr(parser, "FINGERPRINT", None)


def parser_identity(parser):
    """Host-generated, bounded identity; no path or exception detail."""
    return {
        "family": parser.FAMILY,
        "version": parser.VERSION,
        "revision": parser.REVISION,
        "source": parser_source(parser),
        "fingerprint": parser_fingerprint(parser),
    }


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
            discovery = plugins.discover(directory, builtins=BUILTIN_PARSERS)
        except Exception:
            discovery = plugins.Discovery(directory=str(directory))
            discovery.errors.append(plugins.PluginError("", "discovery_failed"))
            discovery.digest = plugins.plugin_set_digest(())
        _cache = (tuple(BUILTIN_PARSERS), tuple(discovery.parsers), discovery)
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
    return _build()[2]


def parsers():
    """Installed parsers: built-ins first, then sorted accepted plugins."""
    builtins, installed, _ = _build()
    return [*builtins, *installed]


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
    """Digest over the loaded plugin set, the API version and the host release."""
    _, _, discovery = _build()
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
