#!/bin/sh
# Restore a clean state: delete every demo sandbox (names start with osc-demo-) and temp files.
#   scripts/reset.sh          sandboxes + temp files
#   scripts/reset.sh --hard   also removes the provider (re-run `make setup` afterwards)
set -u
cd "$(dirname "$0")/.."
PROVIDER="${PROVIDER_NAME:-openrouter-agent}"

names="$(openshell sandbox list --names 2>/dev/null | grep '^osc-demo-' || true)"
if [ -n "$names" ]; then
  echo "Deleting sandboxes:"; echo "$names" | sed 's/^/  /'
  # shellcheck disable=SC2086
  openshell sandbox delete $names >/dev/null 2>&1 || true
  i=0
  while [ $i -lt 30 ] && openshell sandbox list --names 2>/dev/null | grep -q '^osc-demo-'; do i=$((i+1)); sleep 1; done
else
  echo "No demo sandboxes."
fi
if openshell sandbox list --names 2>/dev/null | grep -q '^osc-demo-'; then
  echo "WARNING: some demo sandboxes are still being cleaned up; re-run in a few seconds." >&2
else
  echo "Clean: no osc-demo-* sandboxes."
fi
rm -rf .cache/policies .cache/server.log
if [ "${1:-}" = "--hard" ]; then
  openshell provider delete "$PROVIDER" >/dev/null 2>&1 && echo "Provider $PROVIDER removed."
fi
exit 0
