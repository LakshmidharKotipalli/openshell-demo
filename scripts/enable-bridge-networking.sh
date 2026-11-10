#!/bin/sh
# One-time host setup: make the OpenShell gateway use Docker *bridge* networking
# so sandbox supervisors can reach it on Docker Desktop (macOS).
#
# A ~/.config/openshell/gateway.toml REPLACES the Homebrew default config, so we
# start from the Homebrew config and change only supervisor_network_mode.
# Existing user config is backed up first. Re-runnable.
set -eu

BREW_PREFIX="$(brew --prefix)"
BREW_CONF="$BREW_PREFIX/var/openshell/gateway.toml"
BREW_ERR_LOG="$BREW_PREFIX/var/log/openshell/openshell-gateway.err.log"
CONF_DIR="$HOME/.config/openshell"
CONF="$CONF_DIR/gateway.toml"

[ -f "$BREW_CONF" ] || { echo "Homebrew gateway config not found: $BREW_CONF"; exit 1; }
mkdir -p "$CONF_DIR"

if [ -f "$CONF" ]; then
  BACKUP="$CONF.bak.$(date +%s)"
  mv "$CONF" "$BACKUP"
  echo "Backed up existing config to $BACKUP"
fi

cp "$BREW_CONF" "$CONF"

if grep -q '^[[:space:]]*supervisor_network_mode' "$CONF"; then
  sed -i '' 's|^[[:space:]]*supervisor_network_mode.*|supervisor_network_mode = "bridge"|' "$CONF"
elif grep -q '^\[openshell.drivers.docker\]' "$CONF"; then
  sed -i '' 's|^\[openshell.drivers.docker\]|[openshell.drivers.docker]\
supervisor_network_mode = "bridge"|' "$CONF"
else
  printf '\n[openshell.drivers.docker]\nsupervisor_network_mode = "bridge"\n' >> "$CONF"
fi

echo "---- $CONF (no secrets expected) ----"
cat "$CONF"
echo "-------------------------------------"

brew services restart openshell

i=0
while [ $i -lt 30 ]; do
  if openshell status >/dev/null 2>&1; then
    openshell status
    exit 0
  fi
  i=$((i + 1))
  sleep 1
done

echo "Gateway did not come up within 30s. Last gateway error log lines:"
tail -n 25 "$BREW_ERR_LOG" 2>/dev/null || echo "(no err log at $BREW_ERR_LOG)"
exit 1
