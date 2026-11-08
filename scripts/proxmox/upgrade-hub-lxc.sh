#!/usr/bin/env bash
# =============================================================================
#  upgrade-hub-lxc.sh: upgrades the hub in an existing container with the code
#  in this checkout (for development, or without GitHub). Run on the Proxmox
#  host as root:
#
#      bash scripts/proxmox/upgrade-hub-lxc.sh <container-id>
#
#  Your settings (/etc/bullpen/hub.env) and data are kept. It also works for
#  containers made by older versions of create-hub-lxc.sh, and adds the
#  "update" command to them.
#
#  To update to the newest release instead, type "update" in the container's
#  console (pct enter <container-id>).
# =============================================================================
set -euo pipefail
CTID=${1:-}
[[ -n "$CTID" ]] || { echo "Usage: $0 <container-id>" >&2; exit 1; }
[[ $EUID -eq 0 ]] || { echo "Run as root on the Proxmox host." >&2; exit 1; }
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -C "$REPO_ROOT" --exclude=.git --exclude=__pycache__ --exclude=hub/data --exclude=.venv --exclude=venv \
  -czf "$TMP/bullpen.tar.gz" .
pct push "$CTID" "$TMP/bullpen.tar.gz" /root/.bullpen-src.tar.gz
pct exec "$CTID" -- bash -c 'rm -rf /root/.bullpen-src && mkdir -p /root/.bullpen-src && tar -xzf /root/.bullpen-src.tar.gz -C /root/.bullpen-src'
pct exec "$CTID" -- env mode=default BULLPEN_LOCAL_SRC=/root/.bullpen-src.tar.gz bash /root/.bullpen-src/ct/bullpen.sh
pct exec "$CTID" -- rm -rf /root/.bullpen-src /root/.bullpen-src.tar.gz
