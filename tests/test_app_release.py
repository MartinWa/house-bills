import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from http.client import IncompleteRead
from threading import Event
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest
from django.core.cache import cache
from django.urls import reverse

from bills import releases
from house_bills.version import checkout_version


@pytest.fixture(autouse=True)
def release_cache(settings):
    settings.APP_VERSION = "0.1.9"
    settings.UPDATE_CHECK_ENABLED = True
    cache.delete(releases.CACHE_KEY)
    yield
    cache.delete(releases.CACHE_KEY)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user("synthetic-user", password="synthetic-password")


def github_response(monkeypatch, payload):
    response = Mock()
    response.read.return_value = (
        json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
    )
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    fetch = Mock(return_value=response)
    monkeypatch.setattr(releases, "urlopen", fetch)
    return fetch


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("0.1.10", True), ("v0.2.0", True), ("0.1.9", False), ("0.1.8", False)],
)
def test_newer_stable_release_uses_numeric_comparison_and_cached_metadata(
    monkeypatch, tag, expected
):
    fetch = github_response(monkeypatch, {"tag_name": tag, "draft": False, "prerelease": False})
    save = Mock(wraps=cache.set)
    monkeypatch.setattr(cache, "set", save)
    assert releases.update_available() is expected
    assert releases.update_available() is expected
    fetch.assert_called_once()
    request = fetch.call_args.args[0]
    assert request.full_url == "https://api.github.com/repos/MartinWa/house-bills/releases/latest"
    assert request.data is None
    assert not request.has_header("Authorization")
    assert fetch.call_args.kwargs["timeout"] == 3
    assert save.call_args.args[2] == releases.CHECK_INTERVAL


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"x" * (releases.MAX_RESPONSE_BYTES + 1),
        [],
        {"tag_name": "0.2.0", "draft": True, "prerelease": False},
        {"tag_name": "0.2.0", "draft": False, "prerelease": True},
        {"tag_name": "0.2.0-rc.1", "draft": False, "prerelease": False},
        {"tag_name": "01.2.0", "draft": False, "prerelease": False},
        {"tag_name": 123, "draft": False, "prerelease": False},
        {},
    ],
)
def test_unusable_responses_are_cached_as_unknown(monkeypatch, payload):
    fetch = github_response(monkeypatch, payload)
    assert releases.update_available() is None
    assert releases.update_available() is None
    fetch.assert_called_once()


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        HTTPError(releases.LATEST_RELEASE_API, 403, "Rate limited", {}, None),
        IncompleteRead(b"{", 10),
    ],
)
def test_failed_checks_are_unknown_and_retry_after_failure_interval(monkeypatch, error):
    fetch = Mock(side_effect=error)
    monkeypatch.setattr(releases, "urlopen", fetch)
    save = Mock(wraps=cache.set)
    monkeypatch.setattr(cache, "set", save)
    assert releases.update_available() is None
    assert releases.update_available() is None
    fetch.assert_called_once()
    assert save.call_args.args[2] == releases.FAILURE_INTERVAL
    cache.delete(releases.CACHE_KEY)
    github_response(monkeypatch, {"tag_name": "0.2.0", "draft": False, "prerelease": False})
    assert releases.update_available() is True


def test_concurrent_checks_do_not_duplicate_network_requests_or_wait(monkeypatch):
    entered, finish = Event(), Event()

    def fetch(request, **kwargs):
        entered.set()
        assert finish.wait(5)
        raise TimeoutError

    network = Mock(side_effect=fetch)
    monkeypatch.setattr(releases, "urlopen", network)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(releases.update_available)
        try:
            assert entered.wait(5)
            assert releases.update_available() is None
            network.assert_called_once()
        finally:
            finish.set()
        assert first.result(timeout=5) is None


@pytest.mark.django_db
def test_footer_does_not_contact_github_and_status_requires_active_login(client, user, monkeypatch):
    fetch = github_response(
        monkeypatch, {"tag_name": "0.1.10", "draft": False, "prerelease": False}
    )
    status_url = reverse("bills:release-status")
    assert client.get(status_url).status_code == 302
    login_page = client.get(reverse("login")).content.decode()
    assert "GitHub" in login_page and "0.1.9" in login_page
    assert "data-release-status-url" not in login_page
    client.force_login(user)
    page = client.get(reverse("bills:invoices")).content.decode()
    assert releases.REPOSITORY_URL in page
    assert releases.RELEASES_URL in page
    assert 'data-release-status-url="/release-status/"' in page
    assert 'data-update-indicator hidden title="Update available"' in page
    fetch.assert_not_called()
    response = client.get(status_url)
    assert response.json() == {"update_available": True}
    assert "no-store" in response.headers["Cache-Control"]
    assert client.post(status_url).status_code == 405
    user.is_active = False
    user.save(update_fields=["is_active"])
    assert client.get(status_url).status_code == 302


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("version", "enabled", "label"),
    [
        ("development", True, ""),
        ("dev-abcdef0", True, ""),
        ("0.1.8-dirty", True, "0.1.8"),
        ("v0.1.8-2-gabcdef0", True, "0.1.8"),
        ("0.1.9", False, "0.1.9"),
    ],
)
def test_development_and_disabled_checks_keep_version_links_without_network(
    client, user, settings, monkeypatch, version, enabled, label
):
    settings.APP_VERSION = version
    settings.UPDATE_CHECK_ENABLED = enabled
    fetch = Mock()
    monkeypatch.setattr(releases, "urlopen", fetch)
    client.force_login(user)
    response = client.get(reverse("bills:invoices"))
    page = response.content.decode()
    assert releases.RELEASES_URL in page
    assert "data-release-status-url" not in page
    assert response.context["app_version_label"] == label
    if not label:
        assert "Development" in page
    if version != "development":
        assert f'title="{version}"' in page
    assert client.get(reverse("bills:release-status")).json() == {"update_available": None}
    fetch.assert_not_called()


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("0.1.8\n", "0.1.8"),
        ("0.1.8-dirty\n", "0.1.8-dirty"),
        ("v0.1.8-2-gabcdef0-dirty\n", "v0.1.8-2-gabcdef0-dirty"),
        ("abcdef0\n", "dev-abcdef0"),
        ("abcdef0-dirty\n", "dev-abcdef0-dirty"),
        ("0.1.8-rc.1\n", "development"),
        ("unexpected\n", "development"),
    ],
)
def test_checkout_version_preserves_source_identity_without_claiming_a_release(
    monkeypatch, tmp_path, description, expected
):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=description))
    monkeypatch.setattr("house_bills.version.subprocess.run", run)
    assert checkout_version(tmp_path) == expected
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert run.call_args.kwargs["timeout"] == 2
    assert run.call_args.args[0][:2] == ["git", "describe"]


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("git", 2)])
def test_checkout_without_accessible_git_metadata_uses_development(monkeypatch, tmp_path, error):
    monkeypatch.setattr("house_bills.version.subprocess.run", Mock(side_effect=error))
    assert checkout_version(tmp_path) == "development"
