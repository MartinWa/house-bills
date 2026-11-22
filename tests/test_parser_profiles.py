"""Parser profile and parser-directory health as shown in the browser."""

import io
import os
from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from bills import parser_health, parser_profiles
from bills.models import Document, Property, Vendor, VendorLayoutFamily
from bills.parsing import plugins, registry
from bills.parsing.plugins import MAX_PLUGIN_FILES
from bills.services import record_extraction, validated_provenance
from bills.waste_costs import COST_OPERATION, PROFILE_REFUSALS, retained_source
from tests.test_parsers_plugins import (
    configure_vendor,
    install,
    install_examples,
    pages,
    plugin_source,
    upload,
)
from tests.test_review import manual_fields, posted_fields

pytestmark = pytest.mark.django_db(transaction=True)
SECRET = "/private/synthetic-secret-source"


@pytest.fixture(autouse=True)
def originals(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "originals"


@pytest.fixture
def reviewer(client, django_user_model):
    user = django_user_model.objects.create_user("synthetic-parser-reviewer", is_staff=True)
    client.force_login(user)
    return user


def synthetic_attempt(identity, *, routing=None, revision=None):
    """A minimal attempt-shaped object; the profile helpers only read attributes."""
    if routing is None:
        routing = {
            "selected_parser": identity,
            "host_implementation": registry.host_implementation_digest(),
            "layout_matches": [identity],
            "plugin_errors": [],
        }
    return SimpleNamespace(
        candidate={"routing": routing},
        layout_family=identity["family"],
        layout_version=identity["version"],
        parser_revision=identity["revision"] if revision is None else revision,
        page_evidence=[{"page": 1, "text": "synthetic retained evidence", "bbox": [0, 0, 1, 1]}],
    )


def installed_identity():
    parser = registry.installed("plugin_synthetic", "v1")
    assert parser is not None
    return registry.parser_identity(parser)


def test_profile_codes_follow_the_recorded_and_installed_identity(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    identity = installed_identity()
    assert parser_profiles.profile_state(synthetic_attempt(identity)) == (parser_profiles.MATCHES,)
    assert parser_profiles.profile_state(synthetic_attempt(identity, revision="999"))[0] == (
        parser_profiles.REVISION_CHANGED
    )
    changed_source = {**identity, "fingerprint": "0" * 64}
    assert parser_profiles.profile_state(synthetic_attempt(changed_source))[0] == (
        parser_profiles.SOURCE_CHANGED
    )
    changed_code = {**identity, "code_fingerprint": "0" * 64}
    assert parser_profiles.profile_state(synthetic_attempt(changed_code))[0] == (
        parser_profiles.CODE_CHANGED
    )
    missing_format = {**identity, "family": "plugin_missing"}
    assert parser_profiles.profile_state(synthetic_attempt(missing_format))[0] == (
        parser_profiles.NOT_INSTALLED
    )
    other_host = {
        "selected_parser": identity,
        "host_implementation": "0" * 64,
        "layout_matches": [identity],
    }
    assert parser_profiles.profile_state(synthetic_attempt(identity, routing=other_host))[0] == (
        parser_profiles.HOST_CHANGED
    )
    legacy = {"layout_matches": [{"family": "layout_01", "version": "v1", "revision": "12"}]}
    assert parser_profiles.profile_state(synthetic_attempt(identity, routing=legacy))[0] == (
        parser_profiles.NOT_RECORDED
    )
    unrouted = {
        "selected_parser": None,
        "host_implementation": registry.host_implementation_digest(),
        "plugin_errors": [{"filename": "broken.py", "code": "import_failed"}],
    }
    assert parser_profiles.profile_state(synthetic_attempt(identity, routing=unrouted))[0] == (
        parser_profiles.NOT_ROUTED
    )
    assert parser_profiles.profile_state(None) == (parser_profiles.NOT_RECORDED,)


def test_every_failing_profile_code_makes_retained_corrections_refuse(settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    identity = installed_identity()
    other_host = {
        "selected_parser": identity,
        "host_implementation": "0" * 64,
        "layout_matches": [identity],
    }
    cases = {
        parser_profiles.NOT_INSTALLED: synthetic_attempt({**identity, "family": "plugin_missing"}),
        parser_profiles.REVISION_CHANGED: synthetic_attempt(identity, revision="999"),
        parser_profiles.SOURCE_CHANGED: synthetic_attempt({**identity, "fingerprint": "0" * 64}),
        parser_profiles.CODE_CHANGED: synthetic_attempt({**identity, "code_fingerprint": "0" * 64}),
        parser_profiles.HOST_CHANGED: synthetic_attempt(identity, routing=other_host),
        parser_profiles.NOT_RECORDED: synthetic_attempt(
            identity,
            routing={"layout_matches": [{"family": identity["family"]}]},
        ),
        parser_profiles.NOT_ROUTED: synthetic_attempt(
            identity,
            routing={"selected_parser": None, "host_implementation": "0" * 64},
        ),
    }
    assert set(PROFILE_REFUSALS) == set(cases)
    invoice = SimpleNamespace()
    for expected, attempt in cases.items():
        review = SimpleNamespace(extraction=attempt)
        assert parser_profiles.profile_state(attempt)[0] == expected
        source, warnings = retained_source(invoice, review, COST_OPERATION)
        assert source is None
        assert warnings == [PROFILE_REFUSALS[expected]]


def test_correction_provenance_still_rejects_a_recorded_filename():
    provenance = {
        "extraction_id": 1,
        "parser": {
            "family": "plugin_synthetic",
            "version": "v1",
            "revision": "1",
            "source": "synthetic_plugin.py",
            "fingerprint": "a" * 64,
            "code_fingerprint": "b" * 64,
        },
        "host_implementation": "c" * 64,
        "api_version": 1,
        "app_version": "test",
        "operation": COST_OPERATION,
        "section_ids": ["section_01"],
        "limitation": None,
    }
    assert validated_provenance(provenance) == provenance
    with_filename = {**provenance, "parser": {**provenance["parser"], "filename": "extra.py"}}
    with pytest.raises(ValidationError):
        validated_provenance(with_filename)


def test_document_page_shows_the_profile_that_produced_the_latest_attempt(
    client, reviewer, settings, tmp_path
):
    install(settings, tmp_path, plugin_source())
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    response = client.get(f"/documents/{document.pk}/")
    body = response.content.decode()
    recorded = attempt.candidate["routing"]["selected_parser"]
    assert "Extraction source" in body
    assert "Matches the installed parser." in body
    assert "plugin_synthetic / v1 / revision 1" in body
    assert "synthetic_plugin.py" in body
    assert recorded["fingerprint"][:8] in body
    assert recorded["code_fingerprint"][:8] in body


def test_document_page_explains_a_changed_revision(client, reviewer, settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    configure_vendor()
    document, _, _ = upload(tmp_path, pages())
    install(settings, tmp_path, plugin_source(revision="2"))
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "A different parser revision is installed. Re-parse to refresh this proposal." in body


def test_document_page_explains_changed_source_and_host_implementation(
    client, reviewer, settings, tmp_path, monkeypatch
):
    install(settings, tmp_path, plugin_source())
    configure_vendor()
    document, _, _ = upload(tmp_path, pages())
    install(settings, tmp_path, plugin_source(extra="# cosmetic change moves the fingerprint"))
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "The parser source changed. Re-parse to refresh this proposal." in body
    install(settings, tmp_path, plugin_source())
    monkeypatch.setattr(registry, "host_implementation_digest", lambda: "0" * 64)
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "The host implementation changed. Re-parse to refresh this proposal." in body


def test_document_page_explains_a_missing_format(client, reviewer, settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    configure_vendor()
    document, _, _ = upload(tmp_path, pages())
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(tmp_path / "absent")
    registry.reset()
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "This invoice format is not installed. Re-parse to refresh this proposal." in body


def test_document_page_shows_recorded_plugin_errors_for_an_unrouted_attempt(
    client, reviewer, settings, tmp_path
):
    directory = install_examples(settings, tmp_path)
    (directory / "broken.py").write_text("raise RuntimeError('synthetic')\n", encoding="utf-8")
    registry.reset()
    document, attempt, _ = upload(
        tmp_path, [["Wholly synthetic unsupported water credit"]], issuer=None
    )
    assert attempt.candidate["routing"]["selected_parser"] is None
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "No installed parser matched at extraction time." in body
    assert "broken.py: import_failed" in body


def test_document_page_marks_attempts_recorded_before_parser_identity(
    client, reviewer, settings, tmp_path
):
    settings.HOUSE_BILLS_PARSER_PLUGINS = ""
    registry.reset()
    document = Document.objects.create(sha256="1" * 64, original_filename="legacy.pdf")
    record_extraction(
        document.pk,
        layout_family="layout_01",
        layout_version="v1",
        parser_revision="12",
        routing={
            "layout_matches": [{"family": "layout_01", "version": "v1", "revision": "12"}],
            "section_properties": {},
        },
    )
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "layout_01 / v1 / revision 12" in body
    assert "Parser metadata was not recorded; re-parse to record it." in body
    assert "fingerprint" not in body.lower()
    without_routing = Document.objects.create(sha256="3" * 64, original_filename="empty.pdf")
    record_extraction(without_routing.pk, error="Invoice layout is unsupported.")
    body = client.get(f"/documents/{without_routing.pk}/").content.decode()
    assert "Parser metadata was not recorded; re-parse to record it." in body
    assert "layout_01 / v1" not in body


def test_document_page_omits_a_missing_plugin_filename(client, reviewer, settings):
    parser = registry.installed("layout_01", "v1")
    identity = registry.parser_identity(parser)
    recorded = {key: identity[key] for key in identity if key != "filename"}
    document = Document.objects.create(sha256="2" * 64, original_filename="recorded.pdf")
    record_extraction(
        document.pk,
        layout_family="layout_01",
        layout_version="v1",
        parser_revision=identity["revision"],
        routing={
            "selected_parser": recorded,
            "host_implementation": registry.host_implementation_digest(),
            "layout_matches": [recorded],
            "plugin_errors": [],
        },
    )
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "Matches the installed parser." in body
    assert f"layout_01 / v1 / revision {identity['revision']}" in body
    assert "layout_01.py" not in body


def test_document_page_shows_the_profile_for_a_failed_attempt(client, reviewer, settings, tmp_path):
    failing = "def parse(pages, *, candidate=None):\n    raise RuntimeError('synthetic')\n"
    install(settings, tmp_path, plugin_source(extra=failing))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    assert attempt.error
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert "Extraction source" in body
    assert "Matches the installed parser." in body


def test_review_shows_the_profile_and_is_not_blocked_by_a_mismatch(
    client, reviewer, settings, tmp_path
):
    install(settings, tmp_path, plugin_source())
    configure_vendor()
    document, _, _ = upload(tmp_path, pages())
    install(settings, tmp_path, plugin_source(revision="2"))
    response = client.get(f"/documents/{document.pk}/review/")
    body = response.content.decode()
    assert "A different parser revision is installed. Re-parse to refresh this proposal." in body
    assert "Re-parsing may change this proposal; this review is not blocked." in body
    saved = client.post(f"/documents/{document.pk}/review/", posted_fields(response))
    assert saved.status_code == 302
    assert document.invoices.get().status == "approved"


def test_manual_entry_and_review_without_extraction_omit_the_profile(client, reviewer):
    vendor = Vendor.objects.create(name="Synthetic Manual Vendor", slug="synthetic-manual")
    house = Property.objects.create(name="Manual House", slug="manual-house")
    created = client.post("/invoices/manual/")
    assert created.status_code == 302
    document = Document.objects.get(pk=int(created.url.split("/")[2]))
    path = f"/documents/{document.pk}/review/"
    response = client.get(path)
    assert "Extraction source" not in response.content.decode()
    approved = client.post(path, manual_fields(posted_fields(response), vendor, house))
    assert approved.status_code == 302, approved.content.decode()
    assert document.invoices.get().reviews.get().extraction is None
    body = client.get(path).content.decode()
    assert "Extraction source" not in body


def test_directory_state_applies_errors_emptiness_and_configuration_in_order(
    settings, tmp_path, monkeypatch
):
    settings.HOUSE_BILLS_PARSER_PLUGINS = ""
    registry.reset()
    assert parser_health.directory_state(registry.status())["state"] == (parser_health.UNCONFIGURED)
    empty = tmp_path / "empty-parsers"
    empty.mkdir()
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(empty)
    registry.reset()
    assert parser_health.directory_state(registry.status()) == {
        "state": parser_health.EMPTY,
        "files": 0,
        "errors": [],
    }
    (empty / "broken.py").write_text("raise RuntimeError('synthetic')\n", encoding="utf-8")
    registry.reset()
    assert parser_health.directory_state(registry.status()) == {
        "state": parser_health.ERRORS,
        "files": 1,
        "errors": [{"filename": "broken.py", "code": "import_failed"}],
    }
    monkeypatch.setattr(
        plugins, "discover", lambda directory: (_ for _ in ()).throw(RuntimeError("synthetic"))
    )
    registry.reset()
    assert parser_health.directory_state(registry.status()) == {
        "state": parser_health.ERRORS,
        "files": 0,
        "errors": [{"filename": "", "code": "discovery_failed"}],
    }


def test_directory_level_codes_are_reported(settings, tmp_path):
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(tmp_path / "missing-parsers")
    registry.reset()
    assert [error.code for error in registry.status().errors] == ["directory_missing"]
    path = tmp_path / "not-a-directory"
    path.write_text("synthetic", encoding="utf-8")
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(path)
    registry.reset()
    assert [error.code for error in registry.status().errors] == ["directory_not_a_directory"]
    many = tmp_path / "many-parsers"
    many.mkdir()
    for index in range(MAX_PLUGIN_FILES + 1):
        (many / f"parser_{index}.py").write_text("", encoding="utf-8")
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(many)
    registry.reset()
    assert [error.code for error in registry.status().errors] == ["too_many_files"]
    if os.geteuid() != 0:
        unreadable = tmp_path / "unreadable-parsers"
        unreadable.mkdir()
        unreadable.chmod(0o000)
        settings.HOUSE_BILLS_PARSER_PLUGINS = str(unreadable)
        registry.reset()
        assert [error.code for error in registry.status().errors] == ["directory_unreadable"]


@pytest.mark.parametrize(
    "hook",
    [
        "def matches(pages):\n    raise RuntimeError({secret!r})\n",
        "def read_header(pages):\n    raise RuntimeError({secret!r})\n",
        "def parse(pages, *, candidate=None):\n    raise RuntimeError({secret!r})\n",
        "def configure_sections(candidate, pages, section_properties, *, operation, "
        "section_ids, configuration):\n    raise RuntimeError({secret!r})\n",
    ],
)
def test_parser_entry_point_exceptions_never_reach_output(
    hook, caplog, client, reviewer, settings, tmp_path
):
    install(settings, tmp_path, plugin_source(extra=hook.format(secret=SECRET)))
    configure_vendor()
    document, attempt, _ = upload(tmp_path, pages())
    body = client.get(f"/documents/{document.pk}/").content.decode()
    assert SECRET not in body
    output = io.StringIO()
    call_command("parsers", stdout=output)
    assert SECRET not in output.getvalue()
    assert SECRET not in caplog.text
    assert attempt.candidate["routing"]["failures"]
    for failure in attempt.candidate["routing"]["failures"]:
        assert set(failure) == {"parser", "stage", "code"}


def test_parsers_page_lists_installed_profiles_for_staff(client, reviewer, settings, tmp_path):
    install(settings, tmp_path, plugin_source())
    response = client.get("/parsers/")
    body = response.content.decode()
    assert response.status_code == 200
    assert "plugin_synthetic" in body and "synthetic_plugin.py" in body
    assert "No load errors." in body
    assert SECRET not in body
    assert "/app/docs/examples/parsers" not in body
    assert str(tmp_path) not in body
    assert registry.host_implementation_digest()[:8] in body


def test_parsers_page_links_vendor_formats_without_an_installed_parser(
    client, reviewer, settings, tmp_path
):
    settings.HOUSE_BILLS_PARSER_PLUGINS = ""
    registry.reset()
    vendor = Vendor.objects.create(name="Synthetic Missing Vendor", slug="synthetic-missing")
    VendorLayoutFamily.objects.bulk_create(
        [VendorLayoutFamily(vendor=vendor, family="plugin_unavailable")]
    )
    body = client.get("/parsers/").content.decode()
    assert "plugin_unavailable" in body
    assert f"/admin/bills/vendor/{vendor.pk}/change/" in body
    assert "No parsers are installed." in body
    assert "No parser directory is configured." in body


def test_parsers_page_renders_a_directory_level_error_row(client, reviewer, settings, tmp_path):
    settings.HOUSE_BILLS_PARSER_PLUGINS = str(tmp_path / "missing-parsers")
    registry.reset()
    response = client.get("/parsers/")
    assert response.status_code == 200
    assert "directory: directory_missing" in response.content.decode()


def test_parsers_page_requires_a_staff_session(client, django_user_model):
    response = client.get("/parsers/")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/admin/login/")
    user = django_user_model.objects.create_user("synthetic-plain-reviewer")
    client.force_login(user)
    response = client.get("/parsers/")
    assert response.status_code == 302
    assert response.headers["Location"].startswith("/admin/login/")


def test_user_menu_links_installed_parsers_only_for_staff(
    client, reviewer, settings, django_user_model
):
    assert 'href="/parsers/"' in client.get("/invoices/").content.decode()
    client.force_login(django_user_model.objects.create_user("synthetic-plain-user"))
    assert 'href="/parsers/"' not in client.get("/invoices/").content.decode()
