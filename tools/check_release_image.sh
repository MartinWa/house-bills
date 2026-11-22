#!/usr/bin/env bash
# Check one release platform image and write its SBOM.
set -euo pipefail
: "${IMAGE:?IMAGE must name the release image by digest}"
: "${VERSION:?VERSION must be the release version}"
: "${SBOM:?SBOM must be the SBOM output path}"
: "${PLATFORM:?PLATFORM must be a linux platform such as linux/arm64}"
# The image ships no parser code, so the probe needs the reviewed example parsers.
parsers="$(cd "$(dirname "$0")/.." && pwd)/docs/examples/parsers"
docker pull "$IMAGE"
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true --tmpfs /data:mode=1777 --tmpfs /tmp \
  --mount type=bind,source="$parsers",target=/parsers,readonly \
  --env HOUSE_BILLS_PARSER_PLUGINS=/parsers \
  --env EXPECTED_VERSION="$VERSION" \
  --env HOUSE_BILLS_SECRET_KEY=synthetic-release-check-key-never-for-runtime-000000000000000000 \
  --env HOUSE_BILLS_ALLOWED_HOSTS=ci.invalid \
  "$IMAGE" python -m tools.check_production
bash "$(dirname "$0")/audit_image.sh" "$IMAGE" "$SBOM" "$PLATFORM"
