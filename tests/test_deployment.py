"""Deployment packaging: Compose overrides, release platforms and the contributor agreement."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def read(name):
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_production_compose_builds_only_through_the_local_override():
    base = read("compose.yaml")
    local_build = read("compose.local-build.yaml")

    assert "build:" not in base
    image = 'image: "${HOUSE_BILLS_IMAGE:?Set a tested local or released image tag or digest}"'
    assert image in base
    assert "image: house-bills:local" in local_build
    assert "pull_policy: build" in local_build
    assert "target: production" in local_build
    assert "HOUSE_BILLS_IMAGE" not in local_build


def test_production_compose_networks_are_explicit_and_overridable():
    base = read("compose.yaml")
    proxy_network = read("compose.proxy-network.yaml")

    assert 'name: "${HOUSE_BILLS_NETWORK:-house-bills}"' in base
    assert "external: true" not in base
    assert "external: true" in proxy_network
    assert 'name: "${PROXY_NETWORK:?Set the existing private proxy network}"' in proxy_network


def test_host_port_override_publishes_loopback_only():
    host_port = read("compose.host-port.yaml")

    assert '"127.0.0.1:${HOUSE_BILLS_HOST_PORT:-8000}:8000"' in host_port


def test_release_checks_and_attests_each_platform():
    text = read(".github/workflows/release.yml")

    assert "platforms: linux/amd64" in text
    assert "platforms: linux/arm64" in text
    assert text.count("push-by-digest=true") == 2
    assert text.count("bash tools/check_release_image.sh") == 2
    assert "sbom-amd64.spdx.json" in text
    assert "sbom-arm64.spdx.json" in text
    assert '--tag "$IMAGE:$VERSION" "$IMAGE@$AMD64" "$IMAGE@$ARM64"' in text
    # Provenance covers the index; each platform manifest carries its own SBOM.
    assert "subject-digest: ${{ steps.push.outputs.digest }}" in text


def test_release_helpers_accept_a_platform():
    audit = read("tools/audit_image.sh")
    check = read("tools/check_release_image.sh")

    assert "platform=${3:-}" in audit
    assert 'platform_args=(--platform "$platform")' in audit
    for name in ("IMAGE", "VERSION", "SBOM", "PLATFORM"):
        assert f"${{{name}:?" in check
    # The probe requires an installed parser directory and the image ships none.
    assert "docs/examples/parsers" in check
    assert "target=/parsers,readonly" in check
    assert "HOUSE_BILLS_PARSER_PLUGINS=/parsers" in check


def test_emulated_release_check_gets_a_larger_timeout_budget():
    workflow = read(".github/workflows/release.yml")
    check = read("tools/check_release_image.sh")
    http = read("tools/check_http.py")

    # QEMU runs startup, migrations and requests an order of magnitude slower.
    assert 'TIMEOUT_SCALE: "20"' in workflow
    assert "scale=${TIMEOUT_SCALE:-1}" in check
    assert '--env TIMEOUT_SCALE="$scale"' in check
    assert 'os.environ.get("TIMEOUT_SCALE", "1")' in http
    for probe in ("tools/check_production.py", "tools/check_recovery.py"):
        assert "budget(" in read(probe)


def test_published_package_must_be_publicly_pullable():
    text = read(".github/workflows/release.yml")

    assert "ghcr.io/token?scope=repository" in text
    assert "-f visibility=public" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert "secrets." not in text


def test_contributor_agreement_allows_commercial_relicensing():
    text = read("CLA.md")

    assert "You retain ownership of the copyright" in text
    assert "sublicense and distribute" in text
    assert "right to relicense" in text
    assert "## 6. Corporate contributions" in text
    assert "Harmony contributor agreement model" in text
    assert "## 13. Signature records" in text
    assert "the owner of the `MartinWa/house-bills` GitHub repository" in text
    assert "the laws of Sweden" in text
    assert re.search(r"\[[A-Z][A-Z _]*\]", text) is None


def test_contributor_agreement_is_enforced_without_an_archived_action():
    contributing = read("CONTRIBUTING.md")
    workflows = [
        path.read_text(encoding="utf-8") for path in (REPO_ROOT / ".github/workflows").glob("*.yml")
    ]

    assert "Contributor License" in contributing
    assert "Agreement](CLA.md)" in contributing
    assert "CLA Assistant" in contributing
    assert "required status check" in contributing
    assert not any("contributor-assistant/github-action" in text for text in workflows)
    assert not (REPO_ROOT / "signatures").exists()


def test_example_environment_documents_every_deployment_setting():
    example = read(".env.example")

    for name in (
        "HOUSE_BILLS_SECRET_KEY_FILE",
        "HOUSE_BILLS_NETWORK",
        "HOUSE_BILLS_HOST_PORT",
        "PROXY_NETWORK",
    ):
        assert f"{name}=" in example
    assert "HOUSE_BILLS_ENV_FILE" not in example


def test_parser_plugin_override_is_optional_read_only_and_never_creates_paths():
    override = read("compose.parser-plugins.yaml")
    base = read("compose.yaml")
    example = read(".env.example")

    assert "HOUSE_BILLS_PARSER_PLUGINS: /parsers" in override
    assert (
        'source: "${APP_PARSER_PATH:?Set an existing operator-owned parser code directory}"'
        in override
    )
    assert "read_only: true" in override and "create_host_path: false" in override
    assert "cap_drop" not in override and "no-new-privileges" not in override
    assert 'user: "${APP_UID:?Set a non-root APP_UID}:${APP_GID:?Set APP_GID}"' in base
    assert "read_only: true" in base and "no-new-privileges:true" in base
    assert "HOUSE_BILLS_PARSER_PLUGINS=/parsers" in example
