#!/bin/sh
# Pre-demo checklist. Prints PASS/FAIL per check, exits non-zero if anything failed.
#   scripts/preflight.sh          full (includes a ~15s real sandbox smoke test)
#   scripts/preflight.sh --quick  skip the sandbox smoke test
cd "$(dirname "$0")/.."
PROVIDER="${PROVIDER_NAME:-openrouter-agent}"
WANT="$(cat .openshell-version 2>/dev/null || echo v0.1.7)"
FAILS=0
pass() { printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; FAILS=$((FAILS+1)); }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$*"; }

echo "OpenShell Sandboxed Agent Console — preflight"

# 1. CLI + pinned version
if command -v openshell >/dev/null 2>&1; then
  HAVE="$(openshell --version 2>/dev/null | awk '{print $2}')"
  if [ "v$HAVE" = "$WANT" ]; then pass "openshell CLI $HAVE (pinned $WANT)"; else fail "openshell CLI is $HAVE, pinned version is $WANT"; fi
else fail "openshell CLI not found (make install)"; fi

# 2. gateway status
if OUT="$(openshell status 2>&1)" && echo "$OUT" | grep -q 'Connected'; then
  pass "gateway connected ($(echo "$OUT" | awk '/Server:/{print $2}'), v$(echo "$OUT" | awk '/Version:/{print $2}'))"
else fail "gateway not reachable → brew services restart openshell"; fi

# 3. gateway list
if openshell gateway list 2>/dev/null | grep -q '^\*'; then pass "an active gateway is selected"; else fail "no active gateway (openshell gateway list)"; fi

# 4. Docker + bridge networking
if openshell doctor check 2>&1 | grep -q 'All checks passed'; then pass "Docker daemon reachable (openshell doctor check)"; else fail "Docker not reachable — start Docker Desktop"; fi
if grep -qs 'supervisor_network_mode *= *"bridge"' "$HOME/.config/openshell/gateway.toml"; then pass "gateway uses Docker bridge networking"; else warn "bridge networking not set in ~/.config/openshell/gateway.toml (needed on Docker Desktop: scripts/enable-bridge-networking.sh)"; fi

# 5. provider + profile
if openshell provider list --names 2>/dev/null | grep -qx "$PROVIDER"; then pass "provider '$PROVIDER' exists"; else fail "provider '$PROVIDER' missing → make setup"; fi
if openshell profile list 2>/dev/null | grep -q '^openrouter-agent'; then pass "provider profile imported"; else fail "provider profile missing → make setup"; fi

# 6. policy files + provider profile lint
if [ -x .venv/bin/python ]; then
  if .venv/bin/python - <<'EOF'
import sys
sys.path.insert(0, ".")
from app import parsing
bad = 0
for f in ("policies/strict.yaml", "policies/research.yaml"):
    probs = parsing.lint_policy(parsing.load_policy_file(f))
    if probs:
        bad += 1
        print(f, probs)
a = parsing.load_policy_file("policies/strict.yaml"); b = parsing.load_policy_file("policies/research.yaml")
for k in ("filesystem_policy", "landlock", "process"):
    if a.get(k) != b.get(k):
        bad += 1
        print("presets differ in startup-only section:", k)
sys.exit(1 if bad else 0)
EOF
  then pass "policies/strict.yaml & research.yaml lint clean; startup sections identical"; else fail "policy files invalid (see above)"; fi
else fail "python venv missing → make setup"; fi
if openshell profile describe openrouter-agent 2>/dev/null | grep -q 'python3.12'; then pass "provider profile allows python3.12 to reach openrouter.ai"; else fail "provider profile missing the python3.12 binary → make setup"; fi

# 7. sandbox smoke test: image present, supervisor, placeholder credential, strict denial
if [ "${1:-}" != "--quick" ]; then
  SB="osc-demo-preflight"
  openshell sandbox delete "$SB" >/dev/null 2>&1
  T0=$(date +%s)
  if openshell sandbox create --name "$SB" --from "${AGENT_IMAGE:-python:3.12-slim}" --policy policies/strict.yaml \
       --provider "$PROVIDER" --label app=osc-demo --detach --no-tty -- sleep 120 >/dev/null 2>&1; then
    pass "sandbox image present & sandbox created in $(( $(date +%s) - T0 ))s (Landlock/seccomp qualified)"
    openshell sandbox upload "$SB" agent/agent.py --no-git-ignore >/dev/null 2>&1
    OUT="$(openshell sandbox exec -n "$SB" --no-tty -- python3 agent.py --selftest --task preflight --url https://example.com/ 2>&1)"
    if echo "$OUT" | grep -q '"type": "policy_denied"'; then pass "strict policy denies an unlisted host (example.com)"; else fail "expected a policy denial under strict policy"; fi
    ENVV="$(openshell sandbox exec -n "$SB" --no-tty -- sh -c 'echo "$OPENROUTER_API_KEY"' 2>/dev/null | head -c 18)"
    case "$ENVV" in openshell:resolve:*) pass "API key inside sandbox is a placeholder, not the real key";; *) fail "unexpected API key value in sandbox env";; esac
  else fail "could not create a sandbox (Docker/bridge networking/image?). Try: openshell sandbox create --from ${AGENT_IMAGE:-python:3.12-slim} -- true"; fi
  openshell sandbox delete "$SB" >/dev/null 2>&1
else warn "sandbox smoke test skipped (--quick)"; fi

# 8. app prerequisites
if (command -v lsof >/dev/null 2>&1 && lsof -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1); then warn "port 8000 is already in use (app already running?)"; else pass "port 8000 is free"; fi
if [ -f recordings/success.json ]; then pass "replay recording present (REPLAY=1 fallback)"; else warn "no recordings/success.json (make record)"; fi
if [ -n "${OPENROUTER_API_KEY:-}" ]; then warn "OPENROUTER_API_KEY is exported in this shell (harmless; the app never needs it)"; fi

echo
if [ "$FAILS" -eq 0 ]; then printf '\033[32mALL CHECKS PASSED\033[0m\n'; exit 0; else printf '\033[31m%s CHECK(S) FAILED\033[0m\n' "$FAILS"; exit 1; fi
