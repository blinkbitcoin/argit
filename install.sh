#!/usr/bin/env bash
set -euo pipefail
TAG="${ARGIT_TAG:-v1}"
REPO="git+https://github.com/blinkbitcoin/argit@${TAG}"
if command -v uv >/dev/null 2>&1; then
  uv tool install "${REPO}"
elif command -v pipx >/dev/null 2>&1; then
  pipx install "${REPO}"
else
  echo "Neither uv nor pipx found. Install uv: curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "Then re-run this installer."
  exit 1
fi
argit --version
