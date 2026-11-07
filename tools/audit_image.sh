#!/usr/bin/env bash
# Audit the already tested local image; optionally emit its SPDX SBOM.
set -euo pipefail
image=$1
sbom=${2:-}
audit_dir=$(mktemp -d)
trap 'rm -rf -- "$audit_dir"' EXIT
curl --fail --silent --show-error --location --retry 3 \
  https://github.com/aquasecurity/trivy/releases/download/v0.76.1/trivy_0.76.1_Linux-64bit.tar.gz \
  --output "$audit_dir/trivy.tar.gz"
printf '%s  %s\n' \
  'c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0' \
  "$audit_dir/trivy.tar.gz" | sha256sum --check
tar -xzf "$audit_dir/trivy.tar.gz" -C "$audit_dir" trivy
"$audit_dir/trivy" image --scanners vuln --severity HIGH,CRITICAL \
  --ignore-unfixed --exit-code 1 --timeout 10m "$image"
if [ -n "$sbom" ]; then
  "$audit_dir/trivy" image --scanners license --format spdx-json --output "$sbom" "$image"
fi
