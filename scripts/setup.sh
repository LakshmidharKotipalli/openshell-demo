#!/bin/sh
# One-time (re-runnable) setup: venv + deps, provider profile, provider holding the API key.
# The key is read from OPENROUTER_API_KEY (host env or .env). It is handed to the gateway via
# `--from-existing` (read from the environment, never put on a command line) and is never printed.
set -eu
cd "$(dirname "$0")/.."

PROVIDER="${PROVIDER_NAME:-openrouter-agent}"

say() { printf '==> %s\n' "$*"; }

if [ -f .env ]; then set -a; . ./.env; set +a; fi
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
  echo "OPENROUTER_API_KEY is not set. Copy .env.example to .env, paste your key, and re-run." >&2
  exit 1
fi

say "Python venv + dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt

say "OpenShell gateway reachable?"
openshell status >/dev/null 2>&1 || { echo "Gateway not reachable. Run: brew services restart openshell" >&2; exit 1; }

say "Provider profile (OpenRouter, python3.12 only) + provider '$PROVIDER'"
# Re-runnable: remove the old provider and profile (update needs a resource_version), then recreate.
openshell provider delete "$PROVIDER" >/dev/null 2>&1 || true
openshell profile delete openrouter-agent >/dev/null 2>&1 || true
openshell profile lint -f providers/openrouter-agent.yaml
openshell profile import -f providers/openrouter-agent.yaml >/dev/null
echo "profile imported"
# The key goes gateway-ward via --from-existing (read from the environment); sandboxes only see a placeholder.
openshell provider create --name "$PROVIDER" --type openrouter-agent --from-existing >/dev/null
echo "provider created (key not shown)"

say "Warm the sandbox image cache (first pull can take a minute)"
WARM="osc-demo-warmup"
openshell sandbox delete "$WARM" >/dev/null 2>&1 || true
openshell sandbox create --name "$WARM" --from "${AGENT_IMAGE:-python:3.12-slim}" --policy policies/strict.yaml \
  --provider "$PROVIDER" --label app=osc-demo --detach --no-tty -- sleep 60 >/dev/null
openshell sandbox delete "$WARM" >/dev/null 2>&1 || true

say "Done. Next: make preflight && make run"
