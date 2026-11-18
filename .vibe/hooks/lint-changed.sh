#!/usr/bin/env bash
# Vibe post_agent hook: check Python files changed in the working tree with the
# project's containerized ruff. Infrastructure failures pass silently
# (fail-open); only genuine lint findings are reported back to the agent as a
# retry message. Ruff exit code 1 means findings; anything else (0 = clean,
# 2 = usage error, 125+ = docker failure) is treated as "cannot check".
set -u
cd "$(dirname "$0")/../.." || exit 0

mapfile -t files < <(
  { git diff --name-only --diff-filter=ACM HEAD; git ls-files --others --exclude-standard; } 2>/dev/null |
    sort -u | grep '\.py$'
)
[ "${#files[@]}" -gt 0 ] || exit 0

# Docker unavailable: pass silently rather than blocking the turn.
docker info >/dev/null 2>&1 || exit 0

findings=""
deny=0

run_ruff() {
  docker compose -f compose.development.yaml run --rm --no-deps app "$@" 2>/dev/null
}

out="$(run_ruff ruff check --no-cache "${files[@]}")"; rc=$?
if [ "$rc" -eq 1 ]; then
  deny=1
  findings="${findings}${out}

"
fi

out="$(run_ruff ruff format --check "${files[@]}")"; rc=$?
if [ "$rc" -eq 1 ]; then
  deny=1
  findings="${findings}${out}

"
fi

[ "$deny" -eq 1 ] || exit 0

printf '%s' "Ruff reported problems in the changed Python files (${files[*]}). Fix every finding, then finish:

$findings" | python -c 'import json, sys
print(json.dumps({"decision": "deny", "reason": sys.stdin.read().strip()[:6000]}))'
