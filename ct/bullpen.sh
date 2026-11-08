#!/usr/bin/env bash
# =============================================================================
#  Agent Bullpen: creates the hub's container on a Proxmox VE host, or, run inside
#  that container (the "update" command does this), updates the hub.
#
#      bash -c "$(curl -fsSL https://raw.githubusercontent.com/rob-paprocki/agent-bullpen/main/ct/bullpen.sh)"
#
#  Unattended: put mode=default (and any var_* settings) before the command.
#  docs/04-proxmox-hub.md explains every setting.
#
#  Copyright (c) 2026 Agent Bullpen contributors. MIT licence.
#  https://github.com/rob-paprocki/agent-bullpen
#  The menus and messages follow community-scripts/ProxmoxVE (MIT licence).
#  Agent Bullpen is not affiliated with community-scripts or Proxmox Server Solutions.
# =============================================================================
BULLPEN_REPO="${BULLPEN_REPO:-rob-paprocki/agent-bullpen}"
BULLPEN_REF="${BULLPEN_REF:-}" # blank: the newest release (or main when there is none)

# Use the installer files next to this script when there are any (a checkout,
# an unpacked release, or the copy inside the container); otherwise GitHub's.
if [[ -n "${BULLPEN_LOCAL_SRC:-}" && -f "${BULLPEN_LOCAL_SRC}/misc/build.func" ]]; then
  BULLPEN_ENGINE_ROOT=$(cd "$BULLPEN_LOCAL_SRC" && pwd)
elif [[ -f "${BASH_SOURCE[0]:-}" && -f "$(dirname "${BASH_SOURCE[0]}")/../misc/build.func" ]]; then
  BULLPEN_ENGINE_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi
if [[ -n "${BULLPEN_ENGINE_ROOT:-}" ]]; then
  # shellcheck source=misc/build.func
  source "${BULLPEN_ENGINE_ROOT}/misc/build.func"
else
  _bullpen_engine=$(curl -fsSL --connect-timeout 10 --max-time 60 \
    "https://raw.githubusercontent.com/${BULLPEN_REPO}/${BULLPEN_REF:-main}/misc/build.func") || {
    echo "Could not download the installer from GitHub (${BULLPEN_REPO}). Check this machine's internet access." >&2
    exit 115
  }
  # shellcheck disable=SC1091
  source /dev/stdin <<<"$_bullpen_engine"
fi

APP="Agent Bullpen"
NSAPP="bullpen" # the hostname, install/bullpen-install.sh and misc/headers/bullpen
var_tags="${var_tags:-bullpen}"
var_cpu="${var_cpu:-1}"
var_ram="${var_ram:-1024}"
var_disk="${var_disk:-6}"
var_os="${var_os:-debian}"
var_version="${var_version:-13}"
var_unprivileged="${var_unprivileged:-1}"
# The hub's own settings. Advanced asks for each; set them for unattended runs.
var_hub_password="${var_hub_password:-}"       # blank: generate one and show it at the end
var_hub_port="${var_hub_port:-8080}"
var_hub_runner_url="${var_hub_runner_url:-}"   # blank: http://<container IP>:<port>
var_hub_public_url="${var_hub_public_url:-}"   # e.g. https://hub.example.com
var_cf_tunnel_token="${var_cf_tunnel_token:-}" # blank: no Cloudflare Tunnel
var_ref="${var_ref:-${BULLPEN_REF}}"           # blank: the newest release

header_info "$APP"
variables
color
catch_errors

function update_script() {
  header_info
  check_hub_installed
  if check_for_hub_release; then
    fetch_hub_source
    upgrade_hub
  fi
  exit
}

start
build_container
description

msg_ok "Completed successfully!\n"
echo -e "${CREATING}${GN}${APP} setup has been successfully initialized!${CL}"
echo -e "${INFO}${YW} Access it using the following URL:${CL}"
echo -e "${TAB}${GATEWAY}${BGN}http://${IP}:${HUB_PORT}${CL}"
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo -e "${VERIFYPW}${YW} Dashboard password: ${BGN}${HUB_UI_PASSWORD}${CL}"
fi
echo -e "${INFO}${YW} Add machines: open the dashboard, then Add a machine${CL}"
