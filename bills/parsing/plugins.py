"""Bounded loader for operator-installed single-file parser plugins.

Plugins execute with application privileges. The loader validates paths, source
size, metadata and entry-point signatures; it is not a sandbox and cannot contain
arbitrary code. See SECURITY.md for the operator trust model.
"""

import hashlib
import inspect
import os
import re
import stat
import sys
import types
from dataclasses import dataclass, field
from importlib.machinery import ModuleSpec

from .api import API_VERSION

PLUGIN_NAMESPACE = "house_bills_plugins"
MAX_PLUGIN_FILES = 64
MAX_PLUGIN_SOURCE_BYTES = 1024 * 1024
FAMILY_PATTERN = re.compile(r"[a-z][a-z0-9_]*")
IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")
STEM_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
MAX_FAMILY = 40
MAX_VERSION = 40
MAX_REVISION = 80
FAMILY_PREFIX = "plugin_"


class PluginRejected(Exception):
    """A plugin file failed validation before or after execution."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PluginError:
    filename: str
    code: str


@dataclass
class Discovery:
    """Outcome of one plugin-directory discovery."""

    directory: str = ""
    files: int = 0
    parsers: list = field(default_factory=list)
    errors: list[PluginError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    digest: str = ""

    @property
    def loaded(self):
        return bool(self.parsers)


def safe_filename(name):
    """Bound a reported filename and keep markup, separators and controls out."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name))[:80]
    return cleaned or "unnamed"


def fingerprint(source, family, version, revision):
    """Hash the exact executed source snapshot with its declared identity."""
    header = f"house-bills-plugin-v1\n{family}\n{version}\n{revision}\n".encode()
    return hashlib.sha256(header + source).hexdigest()


def plugin_set_digest(entries):
    """Canonical digest over the accepted plugin set, independent of import caches."""
    payload = "".join(
        f"{name}\0{family}\0{version}\0{revision}\0{digest}\n"
        for name, family, version, revision, digest in sorted(entries)
    )
    return hashlib.sha256(b"house-bills-plugin-set-v1\n" + payload.encode()).hexdigest()


def _read_source(directory, name):
    """Read one bounded source snapshot without following links or blocking."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise PluginRejected("symlink")
            if not stat.S_ISREG(info.st_mode):
                raise PluginRejected("not_regular")
            descriptor = os.open(name, flags, dir_fd=directory_fd)
            try:
                return _bounded_read(descriptor)
            finally:
                os.close(descriptor)
        finally:
            os.close(directory_fd)
    path = os.path.join(directory, name)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode):
        raise PluginRejected("symlink")
    if not stat.S_ISREG(info.st_mode):
        raise PluginRejected("not_regular")
    with open(path, "rb") as handle:
        data = handle.read(MAX_PLUGIN_SOURCE_BYTES + 1)
    if len(data) > MAX_PLUGIN_SOURCE_BYTES:
        raise PluginRejected("too_large")
    return data


def _bounded_read(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise PluginRejected("not_regular")
    if info.st_size > MAX_PLUGIN_SOURCE_BYTES:
        raise PluginRejected("too_large")
    data = os.read(descriptor, MAX_PLUGIN_SOURCE_BYTES + 1)
    if len(data) > MAX_PLUGIN_SOURCE_BYTES:
        raise PluginRejected("too_large")
    return data


def _accepts(function, *args, **kwargs):
    try:
        inspect.signature(function).bind(*args, **kwargs)
    except TypeError, ValueError:
        return False
    return True


def _metadata(module):
    family, version, revision = (
        getattr(module, "FAMILY", None),
        getattr(module, "VERSION", None),
        getattr(module, "REVISION", None),
    )
    for value, pattern, limit, code in (
        (family, FAMILY_PATTERN, MAX_FAMILY, "invalid_family"),
        (version, IDENTIFIER_PATTERN, MAX_VERSION, "invalid_version"),
        (revision, IDENTIFIER_PATTERN, MAX_REVISION, "invalid_revision"),
    ):
        if not isinstance(value, str) or not value or len(value) > limit:
            raise PluginRejected(code)
        if not pattern.fullmatch(value):
            raise PluginRejected(code)
    declared = getattr(module, "API_VERSION", None)
    if isinstance(declared, bool) or not isinstance(declared, int):
        raise PluginRejected("invalid_api_version")
    if declared != API_VERSION:
        raise PluginRejected("api_version_mismatch")
    matches = getattr(module, "matches", None)
    if not callable(matches) or not _accepts(matches, []):
        raise PluginRejected("invalid_matches")
    parse = getattr(module, "parse", None)
    if not callable(parse) or not _accepts(parse, [], candidate=None):
        raise PluginRejected("invalid_parse")
    read_header = getattr(module, "read_header", None)
    if read_header is not None and (not callable(read_header) or not _accepts(read_header, [])):
        raise PluginRejected("invalid_read_header")
    configure = getattr(module, "configure_sections", None)
    if configure is not None and (
        not callable(configure)
        or not _accepts(
            configure,
            [],
            [],
            {},
            operation="waste_cost_split",
            section_ids=frozenset(),
            configuration={},
        )
    ):
        raise PluginRejected("invalid_configure_sections")
    return family, version, revision, matches, parse, read_header


def _register_namespace():
    if PLUGIN_NAMESPACE in sys.modules:
        return
    namespace = types.ModuleType(PLUGIN_NAMESPACE)
    namespace.__path__ = []
    sys.modules[PLUGIN_NAMESPACE] = namespace


def _execute(name, source, path):
    """Compile the snapshot and execute it under a private module namespace."""
    code = compile(source, path, "exec")
    module_name = f"{PLUGIN_NAMESPACE}.{name}"
    module = types.ModuleType(module_name)
    module.__file__ = path
    module.__package__ = PLUGIN_NAMESPACE
    module.__spec__ = ModuleSpec(module_name, loader=None)
    module.__spec__.origin = path
    _register_namespace()
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    return module


def discover(directory, builtins=()):
    """Load top-level plugin files; report rejections without raising them."""
    status = Discovery(directory=str(directory) if directory else "")
    if not directory:
        status.digest = plugin_set_digest(())
        return status
    try:
        resolved = os.path.realpath(directory)
    except OSError:
        resolved = str(directory)
    status.directory = resolved
    try:
        names = sorted(os.listdir(resolved))
    except FileNotFoundError:
        status.errors.append(PluginError("", "directory_missing"))
        status.digest = plugin_set_digest(())
        return status
    except NotADirectoryError:
        status.errors.append(PluginError("", "directory_not_a_directory"))
        status.digest = plugin_set_digest(())
        return status
    except OSError:
        status.errors.append(PluginError("", "directory_unreadable"))
        status.digest = plugin_set_digest(())
        return status
    candidates = [name for name in names if name.endswith(".py") and not name.startswith("_")]
    if len(candidates) > MAX_PLUGIN_FILES:
        status.errors.append(PluginError("", "too_many_files"))
        status.digest = plugin_set_digest(())
        return status
    builtin_keys = {
        (getattr(parser, "FAMILY", ""), getattr(parser, "VERSION", "")) for parser in builtins
    }
    seen = set()
    entries = []
    for name in candidates:
        filename = safe_filename(name)
        status.files += 1
        if not STEM_PATTERN.fullmatch(name[:-3]):
            status.errors.append(PluginError(filename, "invalid_filename"))
            continue
        try:
            source = _read_source(resolved, name)
        except PluginRejected as exc:
            status.errors.append(PluginError(filename, exc.code))
            continue
        except OSError:
            status.errors.append(PluginError(filename, "unreadable"))
            continue
        path = os.path.join(resolved, name)
        try:
            module = _execute(name[:-3], source, path)
        except Exception:
            status.errors.append(PluginError(filename, "import_failed"))
            continue
        try:
            family, version, revision, *_ = _metadata(module)
        except PluginRejected as exc:
            sys.modules.pop(f"{PLUGIN_NAMESPACE}.{name[:-3]}", None)
            status.errors.append(PluginError(filename, exc.code))
            continue
        key = (family, version)
        if key in builtin_keys:
            sys.modules.pop(f"{PLUGIN_NAMESPACE}.{name[:-3]}", None)
            status.errors.append(PluginError(filename, "family_version_collision"))
            continue
        if key in seen:
            sys.modules.pop(f"{PLUGIN_NAMESPACE}.{name[:-3]}", None)
            status.errors.append(PluginError(filename, "duplicate_family_version"))
            continue
        seen.add(key)
        if not family.startswith(FAMILY_PREFIX):
            status.warnings.append(
                f"{filename}: FAMILY does not use the documented {FAMILY_PREFIX} prefix."
            )
        module.PARSER_SOURCE = "plugin"
        module.PLUGIN_FILENAME = filename
        module.FINGERPRINT = fingerprint(source, family, version, revision)
        status.parsers.append(module)
        entries.append((name[:-3], family, version, revision, module.FINGERPRINT))
    status.digest = plugin_set_digest(entries)
    return status
