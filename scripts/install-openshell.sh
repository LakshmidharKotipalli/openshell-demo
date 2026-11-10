#!/bin/sh
# Install the PINNED OpenShell release with the official installer (Homebrew formula on macOS),
# then switch the gateway to Docker bridge networking (needed on Docker Desktop).
# Re-runnable: each step is skipped when it is already in place, so a working gateway is not restarted.
set -eu
cd "$(dirname "$0")/.."
VERSION="$(cat .openshell-version)"

HAVE=""
if command -v openshell >/dev/null 2>&1; then
  HAVE="v$(openshell --version 2>/dev/null | awk '{print $2}')"
fi
if [ "$HAVE" = "$VERSION" ]; then
  echo "OpenShell $VERSION already installed; skipping the installer."
else
  # The installer script comes from the same pinned tag as the release, not from the moving main branch.
  URL="https://raw.githubusercontent.com/NVIDIA/OpenShell/$VERSION/install.sh"
  mkdir -p .cache/openshell-install
  curl -fsSL "$URL" -o .cache/openshell-install/install.sh \
    || { echo "Could not download the installer for $VERSION from $URL" >&2; exit 1; }
  OPENSHELL_VERSION="$VERSION" sh .cache/openshell-install/install.sh
fi

if grep -qs 'supervisor_network_mode *= *"bridge"' "$HOME/.config/openshell/gateway.toml"; then
  echo "Gateway already uses Docker bridge networking; leaving its config alone."
else
  sh scripts/enable-bridge-networking.sh
fi
openshell --version
