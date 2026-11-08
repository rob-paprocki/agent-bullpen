#!/usr/bin/env bash
# =============================================================================
#  create-hub-lxc.sh: runs the one-line installer (ct/bullpen.sh) with the
#  code in this checkout instead of a downloaded release, which is handy for
#  development or without GitHub.
#
#      bash scripts/proxmox/create-hub-lxc.sh
#
#  Most people should use the one-liner from docs/04-proxmox-hub.md instead:
#      bash -c "$(curl -fsSL https://raw.githubusercontent.com/rob-paprocki/agent-bullpen/main/ct/bullpen.sh)"
#  Every var_* setting and mode= work here too.
# =============================================================================
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$REPO_ROOT/ct/bullpen.sh" ]] || { echo "Run this from inside the agent-bullpen repository." >&2; exit 1; }
export BULLPEN_LOCAL_SRC="${BULLPEN_LOCAL_SRC:-$REPO_ROOT}"
exec bash "$REPO_ROOT/ct/bullpen.sh" "$@"
