"""The disposable Grafana server receives token digests, without credential files."""

import hashlib
import json
import secrets

import pytest
from django.test import Client

from bills.models import ApiToken, Invoice
from tools import check_api_grafana
from tools.api_check_server import seed


def test_probe_keeps_credentials_out_of_docker_commands_and_environment(monkeypatch):
    commands = []
    credentials_seen = {}

    def run(command, **options):
        commands.append((command, options))

    def check(credentials, environment):
        credentials_seen.update(credentials)
        check_api_grafana.check_sequential_example(credentials["automation"], environment)

    monkeypatch.setattr(check_api_grafana.subprocess, "run", run)
    monkeypatch.setattr(check_api_grafana, "check", check)
    check_api_grafana.main()

    assert len(commands) == 3
    for command, options in commands:
        metadata = json.dumps([command, options["env"]])
        assert all(credential not in metadata for credential in credentials_seen.values())
    assert commands[1][1]["input"] == credentials_seen["automation"] + "\n"


@pytest.mark.django_db(transaction=True)
def test_probe_credentials_use_supplied_digests_without_token_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HOUSE_BILLS_DATA_DIR", str(tmp_path))
    credentials = {profile: secrets.token_urlsafe(32) for profile in ApiToken.Profile.values}
    digests = {
        profile: hashlib.sha256(credential.encode("ascii")).hexdigest()
        for profile, credential in credentials.items()
    }
    for profile, digest in digests.items():
        monkeypatch.setenv(f"HOUSE_BILLS_CHECK_{profile.upper()}_TOKEN_DIGEST", digest)

    seed()

    assert list(tmp_path.iterdir()) == []
    assert dict(ApiToken.objects.values_list("profile", "digest")) == digests
    assert Invoice.objects.filter(status="approved").count() == 1101
    for profile, credential in credentials.items():
        client = Client(HTTP_AUTHORIZATION=f"Bearer {credential}")
        response = client.get("/api/v1/me")
        assert response.status_code == 200
        assert response.json()["profile"] == profile
        assert client.get("/api/v1/reports/selectors").status_code == 200
        assert client.get("/api/v1/properties").status_code == (
            200 if profile == "automation" else 403
        )
