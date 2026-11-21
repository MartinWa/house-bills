"""Mounted parser plugins: loading, validation, isolation and provenance."""

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError

from bills import storage
from bills.models import (
    BillingSection,
    Invoice,
    Property,
    PropertyIdentifier,
    Vendor,
    VendorIssuerIdentifier,
    VendorLayoutFamily,
)
from bills.parsing import api, registry
from bills.parsing.api import Candidate, Section
from bills.parsing.plugins import fingerprint, plugin_set_digest
from bills.parsing.types import Evidence, Identifier
from tests.synthetic import shared_supply, write_pdf

pytestmark = pytest.mark.django_db(transaction=True)
ISSUER = "SE000000999903"

PLUGIN = '''
"""Synthetic single-file parser plugin using only the public API."""

import re
from dataclasses import dataclass

from bills.parsing import api

API_VERSION = 1
FAMILY = "{family}"
VERSION = "{version}"
REVISION = "{revision}"


@dataclass(frozen=True)
class Marker:
    text: str


MARKER = Marker("synthetic")


def matches(pages):
    return any(
        "Synthetic plugin invoice" in api.text(line) for page in pages for line in page
    )


def read_header(pages):
    return api.header(pages, FAMILY, VERSION, REVISION)


def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    for line in (line for page in pages for line in page):
        match = re.search(
            rf"Vatten avtal 777 ({{api.DATE}}) - ({{api.DATE}})\\s+({{api.NUMBER}})\\s+m3",
            api.text(line),
        )
        if not match:
            continue
        start, end = api.source_date(match[1]), api.source_date(match[2])
        candidate.sections.append(
            api.Section(
                "section_01",
                "water",
                [api.Identifier("contract", "777", line)],
                usages=[
                    api.Usage(
                        "water", "m3", api.quantity(match[3]), start, end, "billed", line
                    )
                ],
                start=start,
                end=end,
                period_evidence=line,
                net_minor=candidate.net_minor,
                vat_minor=candidate.vat_minor,
                gross_minor=candidate.net_minor + candidate.vat_minor,
                total_evidence=line,
            )
        )
    api.reconcile(candidate)
    return candidate
{extra}
'''


def plugin_source(family="plugin_synthetic", version="v1", revision="1", extra=""):
    return PLUGIN.format(family=family, version=version, revision=revision, extra=extra)


def pages():
    return [
        [
            "Synthetic plugin invoice",
            "Faktura-/OCR-nummer: 123456",
            "Fakturadatum: 2024-01-01",
            "Förfallodatum: 2024-02-01",
            "Summa exkl moms Moms Öresutjämning Att betala",
            "1 000,00 kr 250,00 kr 0,00 kr 1 250,00 kr",
            "Vatten avtal 777 2024-01-01 - 2024-02-01 10,000 m3",
        ]
    ]


def install(settings, tmp_path, source, name="synthetic_plugin.py"):
    directory = tmp_path / "parsers"
    directory.mkdir(exist_ok=True)
    (directory / name).write_text(source, encoding="utf-8")
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(directory)
    registry.reset()
    return directory


@pytest.fixture(autouse=True)
def originals(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "originals"


def upload(tmp_path, source_pages, filename="plugin.pdf", issuer=ISSUER):
    source_pages = [list(page) for page in source_pages]
    if issuer:
        source_pages[0].append(f"VAT-nr: {issuer}")
    path = write_pdf(tmp_path / filename, source_pages)
    with path.open("rb") as handle:
        document, duplicate = storage.ingest_pdf(handle, filename=filename)
    return document, document.extractions.latest("revision"), duplicate


def configure_vendor(family="plugin_synthetic"):
    vendor = Vendor.objects.create(name="Synthetic Plugin Utility", slug="plugin-utility")
    VendorIssuerIdentifier.objects.create(vendor=vendor, kind="vat", value=ISSUER)
    VendorLayoutFamily.objects.create(vendor=vendor, family=family)
    house = Property.objects.create(name="Plugin House", slug="plugin-house")
    PropertyIdentifier.objects.create(vendor=vendor, property=house, kind="contract", value="777")
    return vendor, house


def test_mounted_plugin_loads_matches_extracts_and_records_provenance(settings, tmp_path):
    directory = install(settings, tmp_path, plugin_source())
    _, house = configure_vendor()
    assert "plugin_synthetic" in registry.layouts()
    parser = registry.installed("plugin_synthetic", "v1")
    source = (directory / "synthetic_plugin.py").read_bytes()
    assert parser.PARSER_SOURCE == "plugin"
    assert parser.FINGERPRINT == fingerprint(source, "plugin_synthetic", "v1", "1")
    document, attempt, duplicate = upload(tmp_path, pages())
    assert not duplicate and document.state == "review"
    assert (attempt.layout_family, attempt.layout_version, attempt.parser_revision) == (
        "plugin_synthetic",
        "v1",
        "1",
    )
    assert attempt.candidate["issues"] == []
    assert attempt.candidate["routing"]["selected_parser"] == {
        "family": "plugin_synthetic",
        "version": "v1",
        "revision": "1",
        "source": "plugin",
        "fingerprint": parser.FINGERPRINT,
    }
    assert attempt.candidate["routing"]["plugin_set_digest"]
    assert attempt.candidate["routing"]["section_properties"] == {"section_01": house.pk}
    assert (
        not BillingSection.objects.exists()
        and not Invoice.objects.filter(status="approved").exists()
    )


def test_vendor_only_routes_a_plugin_family_after_it_is_selected(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    vendor, house = configure_vendor()
    vendor.layout_families.all().delete()
    document, attempt, _ = upload(tmp_path, pages())
    assert attempt.error and "unsupported" in attempt.error
    assert "plugin_synthetic" in registry.layouts()
    VendorLayoutFamily.objects.create(vendor=vendor, family="plugin_synthetic")
    selected = pages()
    selected[0][1] = "Faktura-/OCR-nummer: 123457"
    document, attempt, _ = upload(tmp_path, selected, filename="selected.pdf")
    assert attempt.layout_family == "plugin_synthetic" and attempt.candidate["issues"] == []


def test_module_level_dataclass_loads_through_registered_namespace(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    assert registry.installed("plugin_synthetic", "v1") is not None
    assert "house_bills_plugins.synthetic_plugin" in sys.modules


@pytest.mark.parametrize(
    "source,code",
    [
        (plugin_source().replace('FAMILY = "plugin_synthetic"', "FAMILY = ''"), "invalid_family"),
        (plugin_source().replace("API_VERSION = 1", "API_VERSION = 2"), "api_version_mismatch"),
        (plugin_source().replace("API_VERSION = 1", "API_VERSION = True"), "invalid_api_version"),
        (
            plugin_source().replace("def parse(pages, *, candidate=None):", "def parse(pages):"),
            "invalid_parse",
        ),
        (
            plugin_source(extra="def configure_sections(candidate):\n    return {}\n"),
            "invalid_configure_sections",
        ),
        (
            plugin_source().replace('FAMILY = "plugin_synthetic"', 'FAMILY = "layout_01"'),
            "family_version_collision",
        ),
        (plugin_source() + "\ndef broken(:\n", "import_failed"),
    ],
)
def test_invalid_plugins_are_rejected_with_builtins_available(settings, tmp_path, source, code):
    install(settings, tmp_path, source)
    discovery = registry.status()
    assert code in {error.code for error in discovery.errors}
    assert discovery.parsers == []
    assert "layout_01" in registry.layouts()
    with pytest.raises(CommandError):
        call_command("parsers", check=True, stdout=open(os.devnull, "w"))


@pytest.mark.parametrize(
    "extra,error",
    [
        ("def matches(pages):\n    return 1\n", "unsupported"),
        ("def read_header(pages):\n    return None\n", "extraction failed"),
        (
            "def parse(pages, *, candidate=None):\n"
            "    candidate = candidate or read_header(pages)\n"
            "    candidate.sections.append('not a section')\n"
            "    return candidate\n",
            "extraction failed",
        ),
    ],
)
def test_invalid_runtime_results_keep_safe_review_attempts(settings, tmp_path, extra, error):
    install(settings, tmp_path, plugin_source(extra=extra))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert document.state == "review" and error in attempt.error
    assert attempt.candidate["reference"] == "123456"
    assert not attempt.candidate["sections"] or (
        attempt.candidate["sections"][0]["service"] == "water"
    )


def test_import_failure_does_not_echo_exception_text(settings, tmp_path):
    install(
        settings,
        tmp_path,
        plugin_source(extra="raise RuntimeError('synthetic-secret-import')\n"),
    )
    output = io.StringIO()
    call_command("parsers", stdout=output)
    assert "import_failed" in output.getvalue()
    assert "synthetic-secret-import" not in output.getvalue()


def test_invalid_plugin_filename_is_reported_without_execution(settings, tmp_path):
    install(settings, tmp_path, plugin_source(), name="not a module.py")
    codes = {error.code for error in registry.status().errors}
    assert "invalid_filename" in codes
    assert registry.installed("plugin_synthetic", "v1") is None


def test_preexisting_bytecode_never_replaces_source_snapshot(settings, tmp_path):
    import py_compile

    directory = tmp_path / "parsers"
    directory.mkdir()
    stale = directory / "synthetic_plugin.py"
    stale.write_text(plugin_source(family="plugin_stale"), encoding="utf-8")
    stale_time = stale.stat().st_mtime
    cache = Path(py_compile.compile(str(stale), doraise=True))
    assert cache.exists()
    current = plugin_source()
    stale.write_text(current, encoding="utf-8")
    os.utime(stale, (stale_time, stale_time))
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(directory)
    registry.reset()
    assert registry.installed("plugin_synthetic", "v1") is not None
    assert registry.installed("plugin_stale", "v1") is None
    assert registry.installed("plugin_synthetic", "v1").FINGERPRINT == fingerprint(
        current.encode(), "plugin_synthetic", "v1", "1"
    )


def test_raising_matcher_is_isolated_and_other_parsers_still_route(settings, tmp_path):
    source = plugin_source(
        extra="def matches(pages):\n    raise RuntimeError('synthetic-secret')\n"
    )
    install(settings, tmp_path, source)
    vendor, _ = configure_vendor()
    VendorLayoutFamily.objects.create(vendor=vendor, family="layout_02")
    document, attempt, _ = upload(tmp_path, shared_supply())
    assert attempt.layout_family == "layout_02" and document.state == "review"
    failures = attempt.candidate["routing"]["failures"]
    assert any(
        failure["stage"] == "matches" and failure["parser"]["family"] == "plugin_synthetic"
        for failure in failures
    )
    assert "synthetic-secret" not in str(attempt.candidate)


def test_parse_returning_another_object_keeps_a_safe_review_proposal(settings, tmp_path):
    source = plugin_source(
        extra=(
            "\ndef parse(pages, *, candidate=None):\n"
            "    return api.Candidate("
            "'', '', '', '', None, None, 'SEK', None, None, None, None, None, {})\n"
        )
    )
    install(settings, tmp_path, source)
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert document.state == "review" and "extraction failed" in attempt.error
    assert attempt.candidate["reference"] == "123456"
    assert document.extractions.get().page_evidence


def test_hook_failure_leaves_the_combined_costs_and_reports_the_stage(settings, tmp_path):
    extra = """

def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    raise RuntimeError("synthetic-secret-hook")
"""
    source = plugin_source(extra=extra)
    install(settings, tmp_path, source)
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert document.state == "review"
    assert any("Configured cost split failed" in issue for issue in attempt.candidate["issues"])
    failures = attempt.candidate["routing"]["failures"]
    assert any(failure["stage"] == "configure_sections" for failure in failures)
    assert (
        "synthetic-secret" not in str(attempt.candidate) and "synthetic-secret" not in attempt.error
    )
    assert attempt.candidate["sections"][0]["net_minor"] == 100000


def test_hook_cannot_change_headers_and_is_restored(settings, tmp_path):
    extra = """

def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    candidate.reference = "tampered"
    return section_properties, []
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert attempt.candidate["reference"] == "123456"
    assert any("Configured cost split failed" in issue for issue in attempt.candidate["issues"])


def test_missing_and_empty_plugin_directories_keep_builtins_operational(settings, tmp_path):
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(tmp_path / "absent")
    registry.reset()
    assert "layout_01" in registry.layouts()
    assert "directory_missing" in {error.code for error in registry.status().errors}
    with pytest.raises(CommandError):
        call_command("parsers", check=True, stdout=open(os.devnull, "w"))
    empty = tmp_path / "empty-parsers"
    empty.mkdir()
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(empty)
    registry.reset()
    assert "layout_01" in registry.layouts()
    call_command("parsers", check=True, stdout=open(os.devnull, "w"))
    settings.HOUSE_BILLS_PARSER_PLUGINS = ""
    registry.reset()
    call_command("parsers", check=True, stdout=open(os.devnull, "w"))


def test_unavailable_vendor_family_fails_only_the_check(settings, tmp_path):
    settings.HOUSE_BILLS_PARSER_PLUGINS = ""
    registry.reset()
    vendor = Vendor.objects.create(name="Synthetic Missing", slug="synthetic-missing")
    VendorLayoutFamily.objects.bulk_create(
        [VendorLayoutFamily(vendor=vendor, family="plugin_unavailable")]
    )
    with pytest.raises(CommandError, match="no installed parser"):
        call_command("parsers", check=True, stdout=open(os.devnull, "w"))
    call_command("parsers", stdout=open(os.devnull, "w"))


def test_plugin_directory_inside_application_storage_is_rejected(tmp_path, monkeypatch):
    from house_bills.settings import base

    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    with pytest.raises(ImproperlyConfigured):
        base._parser_directory(str(tmp_path))
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(plugins, target_is_directory=True)
    with pytest.raises(ImproperlyConfigured):
        base._parser_directory(str(alias))
    monkeypatch.setattr(base, "DATA_DIR", tmp_path / "elsewhere")
    assert base._parser_directory(str(plugins)) == str(plugins)


def test_production_rejects_relative_plugin_paths(tmp_path):
    environment = {
        **os.environ,
        "PYTHONPATH": "/app",
        "HOUSE_BILLS_SECRET_KEY": "synthetic" * 8,
        "HOUSE_BILLS_ALLOWED_HOSTS": "bills.example.internal",
        "HOUSE_BILLS_DATA_DIR": str(tmp_path / "data"),
        "HOUSE_BILLS_PARSER_PLUGINS": "relative/parsers",
    }
    result = subprocess.run(
        [sys.executable, "-c", "import house_bills.settings.production"],
        capture_output=True,
        text=True,
        env=environment,
        cwd="/app",
    )
    assert result.returncode != 0
    assert "HOUSE_BILLS_PARSER_PLUGINS" in result.stderr
    secret = tmp_path / "parser-path"
    secret.write_text("relative-from-file\n", encoding="utf-8")
    environment.pop("HOUSE_BILLS_PARSER_PLUGINS")
    environment["HOUSE_BILLS_PARSER_PLUGINS_FILE"] = str(secret)
    result = subprocess.run(
        [sys.executable, "-c", "import house_bills.settings.production"],
        capture_output=True,
        text=True,
        env=environment,
        cwd="/app",
    )
    assert result.returncode != 0
    assert "HOUSE_BILLS_PARSER_PLUGINS" in result.stderr


def test_symlinked_plugin_file_is_rejected(settings, tmp_path):
    directory = tmp_path / "parsers"
    directory.mkdir()
    target = tmp_path / "outside.py"
    target.write_text(plugin_source(), encoding="utf-8")
    (directory / "linked.py").symlink_to(target)
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(directory)
    registry.reset()
    assert "symlink" in {error.code for error in registry.status().errors}
    assert registry.installed("plugin_synthetic", "v1") is None


def test_source_size_limit_and_non_regular_entries_are_rejected(settings, tmp_path):
    directory = tmp_path / "parsers"
    directory.mkdir()
    (directory / "huge.py").write_text("x = 1\n" + "#" * (1024 * 1024), encoding="utf-8")
    os.mkfifo(directory / "pipe.py")
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(directory)
    registry.reset()
    codes = {error.code for error in registry.status().errors}
    assert {"too_large", "not_regular"} <= codes
    assert "layout_01" in registry.layouts()


def test_configure_scope_violation_is_restored_on_a_builtin_parser(settings, tmp_path):
    source = plugin_source(
        extra="""

def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    candidate.sections[0].net_minor += 1
    return section_properties, []
"""
    )
    install(settings, tmp_path, source)
    parser = registry.installed("plugin_synthetic", "v1")
    candidate = Candidate(
        "plugin_synthetic",
        "v1",
        "1",
        "",
        None,
        None,
        "SEK",
        None,
        None,
        None,
        None,
        None,
        {},
        sections=[
            Section(
                "section_01",
                "waste",
                [Identifier("contract", "777", Evidence(1, "x", (0, 0, 1, 1)))],
            )
        ],
    )
    from bills.parsing.configuration import ContractError

    with pytest.raises(ContractError):
        api.configure(
            candidate,
            [],
            {"section_01": None},
            family=parser.FAMILY,
            version=parser.VERSION,
            operation="waste_cost_split",
            section_ids=frozenset({"section_01"}),
            configuration={},
        )
    assert candidate.sections[0].net_minor is None


def test_plugin_set_digest_and_fingerprint_change_with_source(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    first = registry.status().digest
    first_fingerprint = registry.installed("plugin_synthetic", "v1").FINGERPRINT
    install(settings, tmp_path, plugin_source(revision="2"))
    assert registry.status().digest != first
    assert registry.installed("plugin_synthetic", "v1").FINGERPRINT != first_fingerprint
    assert plugin_set_digest(()) != first


def test_api_surface_names_resolve_without_django():
    for name in api.__all__:
        assert getattr(api, name) is not None
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import bills.parsing.api; print('django' in sys.modules)",
        ],
        capture_output=True,
        text=True,
        cwd="/app",
    )
    assert result.returncode == 0 and result.stdout.strip() == "False"


SHARED_PARSER_MODULES = {
    "common",
    "electricity_demand",
    "layout_helpers",
    "normalization",
    "types",
    "waste_costs",
}
STDLIB_PARSER_IMPORTS = {"datetime", "re"}
PARSER_MODULE_EXCEPTIONS = {
    "layout_01_v2": {"layout_01"},
    "layout_02_v2": {"layout_02"},
    "layout_05_v2": {"layout_05"},
    "layout_06_v2": {"layout_06"},
}


def test_builtin_parser_imports_follow_an_explicit_whitelist():
    import ast

    directory = Path(__file__).resolve().parent.parent / "bills" / "parsing"
    for path in sorted(directory.glob("layout_*.py")):
        stem = path.stem
        allowed = SHARED_PARSER_MODULES | PARSER_MODULE_EXCEPTIONS.get(stem, set())
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert {alias.name for alias in node.names} <= {"re"}, path.name
            elif isinstance(node, ast.ImportFrom):
                modules = (
                    {alias.name for alias in node.names}
                    if node.module is None and node.level == 1
                    else {node.module}
                )
                for module in modules:
                    if node.level == 0:
                        assert module in {"bills.categories", *STDLIB_PARSER_IMPORTS}, (
                            path.name,
                            module,
                        )
                    else:
                        assert module in allowed, (path.name, module)


def test_registry_reset_forgets_executed_plugin_modules(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    registry.parsers()
    assert "house_bills_plugins.synthetic_plugin" in sys.modules
    registry.reset()
    assert "house_bills_plugins.synthetic_plugin" not in sys.modules
    assert "house_bills_plugins" not in sys.modules


@pytest.mark.django_db(transaction=True)
def test_plugin_attempt_without_a_fingerprint_requires_a_new_extraction(
    settings, tmp_path, django_user_model
):
    from bills.services import approve_review, record_extraction
    from bills.waste_costs import plan_retained_cost_split, reviewed_candidate

    install(settings, tmp_path, plugin_source())
    vendor, house = configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    candidate = reviewed_candidate(attempt.candidate)
    legacy = record_extraction(document.pk, candidate, page_evidence=attempt.page_evidence)
    invoice = approve_review(
        document.invoices.get().pk,
        expected_revision=0,
        candidate=candidate,
        vendor=vendor,
        section_properties={},
        actor=django_user_model.objects.create_user("plugin-reviewer"),
        extraction=legacy,
    )
    invoice.refresh_from_db()
    plan, warnings = plan_retained_cost_split(invoice, house.pk)
    assert plan is None and "recorded plugin source is unavailable" in warnings[0]
    assert invoice.review_revision == 1


def _candidate(*sections, identifiers=None):
    return Candidate(
        "plugin_synthetic",
        "v1",
        "1",
        "",
        None,
        None,
        "SEK",
        None,
        None,
        None,
        None,
        None,
        {},
        identifiers=list(identifiers or []),
        sections=list(sections),
    )


def _section(section_id, service, net=0, *, subcategory=""):
    return Section(
        section_id,
        service,
        [Identifier("contract", "777", Evidence(1, "Synthetic", (0, 0, 1, 1)))],
        net_minor=net,
        vat_minor=0,
        gross_minor=net,
        subcategory=subcategory,
    )


def _configure(parser, candidate, properties, *, selected, operation="waste_cost_split"):
    return api.configure(
        candidate,
        [],
        properties,
        family=parser.FAMILY,
        version=parser.VERSION,
        operation=operation,
        section_ids=frozenset(selected),
        configuration={},
    )


def test_hook_cannot_reassign_an_existing_property(settings, tmp_path):
    from bills.parsing.configuration import ContractError

    extra = """
def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    changed = dict(section_properties)
    changed["section_02"] = 99
    return changed, []
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    parser = registry.installed("plugin_synthetic", "v1")
    candidate = _candidate(_section("section_01", "waste"), _section("section_02", "water"))
    properties = {"section_01": 7, "section_02": 8}
    with pytest.raises(ContractError, match="property assignment"):
        _configure(parser, candidate, properties, selected=properties)
    assert candidate.sections[1].service == "water"


def test_operation_scope_rejects_changes_to_other_services(settings, tmp_path):
    from bills.parsing.configuration import ContractError

    extra = """
def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    for section in candidate.sections:
        if section.service == "water":
            section.net_minor -= 100
            section.gross_minor -= 100
        if section.service == "waste":
            section.net_minor += 100
            section.gross_minor += 100
    return section_properties, []
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    parser = registry.installed("plugin_synthetic", "v1")
    candidate = _candidate(
        _section("section_01", "waste", 2000), _section("section_02", "water", 1000)
    )
    properties = {"section_01": 7, "section_02": 7}
    with pytest.raises(ContractError, match="unrelated section"):
        _configure(parser, candidate, properties, selected=properties)
    assert candidate.sections[0].net_minor == 2000
    assert candidate.sections[1].net_minor == 1000


def test_malformed_hook_output_raises_contract_error_and_restores(settings, tmp_path):
    from bills.parsing.configuration import ContractError

    extra = """
def configure_sections(
    candidate, pages, section_properties, *, operation, section_ids, configuration
):
    candidate.sections = ["bad"]
    return section_properties, []
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    parser = registry.installed("plugin_synthetic", "v1")
    candidate = _candidate(_section("section_01", "waste"))
    properties = {"section_01": 7}
    with pytest.raises(ContractError):
        _configure(parser, candidate, properties, selected=properties)
    assert isinstance(candidate.sections, list)
    assert isinstance(candidate.sections[0], Section)


def test_validation_rejects_malformed_services_and_invoice_identifiers():
    from bills.parsing.configuration import ContractError
    from bills.parsing.validation import safe_candidate, validate_candidate

    malformed = _section("section_01", [])
    candidate = _candidate(malformed)
    with pytest.raises(ContractError, match="invalid service"):
        validate_candidate(candidate)
    with pytest.raises(ContractError, match="invalid section"):
        validate_candidate(_candidate("not a section"))
    fallback = _candidate()
    recovered = safe_candidate(candidate, fallback)
    assert recovered.sections == []

    identifiers = _candidate(identifiers=[1])
    with pytest.raises(ContractError, match="identifier"):
        validate_candidate(identifiers)
    assert safe_candidate(identifiers, fallback).identifiers == []


def test_revision_length_matches_discovery_and_model_limits():
    from bills.parsing.configuration import ContractError
    from bills.parsing.validation import validate_candidate

    candidate = _candidate(identifiers=[])
    candidate.parser_revision = "r" * 80
    validate_candidate(candidate)
    candidate.parser_revision = "r" * 81
    with pytest.raises(ContractError, match="parser_revision"):
        validate_candidate(candidate)


def test_parser_cannot_erase_the_reader_snapshot(settings, tmp_path):
    extra = """
def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    pages.clear()
    raise RuntimeError("synthetic-secret-pages")
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert document.state == "review"
    assert attempt.page_evidence
    assert attempt.candidate["reference"] == "123456"
    assert "synthetic-secret-pages" not in attempt.error


def test_rejected_parser_identity_is_not_retained_in_recovery(settings, tmp_path):
    extra = """
def parse(pages, *, candidate=None):
    candidate = candidate or read_header(pages)
    candidate.layout_family = "plugin_other"
    candidate.layout_version = "v9"
    candidate.parser_revision = "tampered"
    raise RuntimeError("synthetic")
"""
    install(settings, tmp_path, plugin_source(extra=extra))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert document.state == "review" and "extraction failed" in attempt.error
    assert (attempt.layout_family, attempt.layout_version, attempt.parser_revision) == (
        "plugin_synthetic",
        "v1",
        "1",
    )
    assert attempt.candidate["routing"]["selected_parser"]["family"] == "plugin_synthetic"


def test_relative_plugin_paths_still_respect_storage_containment(tmp_path, monkeypatch):
    from house_bills.settings import base

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(base, "DATA_DIR", tmp_path)
    monkeypatch.setattr(base, "MEDIA_ROOT", tmp_path / "invoices")
    with pytest.raises(ImproperlyConfigured):
        base._parser_directory("plugins")
    monkeypatch.setattr(base, "DATA_DIR", tmp_path / "elsewhere")
    assert base._parser_directory("plugins") == "plugins"
