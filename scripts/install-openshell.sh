#!/bin/sh
# Install the PINNED OpenShell release with the official installer (Homebrew formula on macOS),
# then switch the gateway to Docker bridge networking (needed on Docker Desktop).
set -eu
cd "$(dirname "$0")/.."
VERSION="$(cat .openshell-version)"
mkdir -p .cache/openshell-install
curl -fsSL https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh -o .cache/openshell-install/install.sh
OPENSHELL_VERSION="$VERSION" sh .cache/openshell-install/install.sh
sh scripts/enable-bridge-networking.sh
openshell --version
