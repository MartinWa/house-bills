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
    errors: list = field(default_factory=list)
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
    return family, version, revision


def _execute(directory, stem, source):
    """Compile the snapshot and execute it under a private module namespace."""
    module_name = f"{PLUGIN_NAMESPACE}.{stem}"
    path = os.path.join(directory, f"{stem}.py")
    module = types.ModuleType(module_name)
    module.__file__ = path
    module.__package__ = PLUGIN_NAMESPACE
    previous = sys.modules.get(module_name)
    sys.modules[module_name] = module
    try:
        exec(compile(source, path, "exec"), module.__dict__)
        if stem.startswith("_"):
            # A support module is shared code, not a parser: no layout metadata.
            module.PLUGIN_IDENTITY = ("", "", "")
        else:
            module.PLUGIN_IDENTITY = _metadata(module)
        module.FINGERPRINT = fingerprint(source, *module.PLUGIN_IDENTITY)
    except Exception:
        if previous is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous
        raise
    setattr(sys.modules[PLUGIN_NAMESPACE], stem, module)
    return module


def _register_namespace(directory):
    """Expose a private package so plugin files can share code with relative imports.

    Each discovery starts from an empty namespace, so a module removed from the
    directory can never be resolved or digested from an earlier load.
    """
    for name in [name for name in sys.modules if name.startswith(f"{PLUGIN_NAMESPACE}.")]:
        sys.modules.pop(name, None)
    namespace = types.ModuleType(PLUGIN_NAMESPACE)
    namespace.__directory__ = directory
    namespace.__path__ = []
    sys.modules[PLUGIN_NAMESPACE] = namespace
    return namespace


def _load(directory, sources):
    """Execute every snapshot, retrying files that wait on a sibling import.

    A plugin imports shared code as a sibling (``from . import layout_01``) and never
    through ``sys.path``, so the loop runs until no further file can start. Files that
    still wait on an unresolved or rejected sibling end as ``import_failed``.
    """
    errors, pending = [], dict(sources)
    while pending:
        progress = False
        for stem in sorted(pending):
            try:
                _execute(directory, stem, pending[stem])
            except ModuleNotFoundError as exc:
                if exc.name and exc.name.startswith(f"{PLUGIN_NAMESPACE}."):
                    continue
                errors.append((stem, "import_failed"))
            except PluginRejected as exc:
                errors.append((stem, exc.code))
            except Exception:
                errors.append((stem, "import_failed"))
            del pending[stem]
            progress = True
        if not progress:
            errors.extend((stem, "import_failed") for stem in sorted(pending))
            break
    return errors


def _namespace_entries():
    """Digest entries for every executed module, parsers and shared helpers."""
    prefix = f"{PLUGIN_NAMESPACE}."
    entries = []
    for fullname, module in sorted(sys.modules.items()):
        if not fullname.startswith(prefix) or "." in fullname[len(prefix) :]:
            continue
        identity = getattr(module, "PLUGIN_IDENTITY", None)
        digest = getattr(module, "FINGERPRINT", None)
        if identity is None or digest is None:
            continue
        entries.append((fullname[len(prefix) :], *identity, digest))
    return entries


def discover(directory):
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
    sources = [name for name in names if name.endswith(".py")]
    if len(sources) > MAX_PLUGIN_FILES:
        status.errors.append(PluginError("", "too_many_files"))
        status.digest = plugin_set_digest(())
        return status
    status.files = len(sources)
    _register_namespace(resolved)
    snapshots = {}
    for name in sources:
        filename = safe_filename(name)
        if not STEM_PATTERN.fullmatch(name[:-3]):
            status.errors.append(PluginError(filename, "invalid_filename"))
            continue
        try:
            snapshots[name[:-3]] = _read_source(resolved, name)
        except PluginRejected as exc:
            status.errors.append(PluginError(filename, exc.code))
        except OSError:
            status.errors.append(PluginError(filename, "unreadable"))
    status.errors.extend(PluginError(name, code) for name, code in _load(resolved, snapshots))
    seen = set()
    for stem in sorted(snapshots):
        module = sys.modules.get(f"{PLUGIN_NAMESPACE}.{stem}")
        if module is None or stem.startswith("_"):
            continue
        family, version, revision = module.PLUGIN_IDENTITY
        if (family, version) in seen:
            sys.modules.pop(f"{PLUGIN_NAMESPACE}.{stem}", None)
            status.errors.append(
                PluginError(safe_filename(f"{stem}.py"), "duplicate_family_version")
            )
            continue
        seen.add((family, version))
        module.PLUGIN_FILENAME = safe_filename(f"{stem}.py")
        status.parsers.append(module)
    status.digest = plugin_set_digest(_namespace_entries())
    return status
