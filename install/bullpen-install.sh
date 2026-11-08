#!/usr/bin/env bash
# =============================================================================
#  install/bullpen-install.sh: installs the Agent Bullpen inside the new
#  container. ct/bullpen.sh on the Proxmox VE host sends it here; it is not
#  meant to be run by hand (on other machines, use scripts/linux/install-hub.sh).
#
#  The settings arrive as exported environment variables (see run_install in
#  misc/build.func). This script may come from a newer branch than the release
#  it installs, so it only relies on install-hub.sh's documented variables.
#
#  Copyright (c) 2026 Agent Bullpen contributors. MIT licence.
#  https://github.com/rob-paprocki/agent-bullpen
# =============================================================================
# shellcheck disable=SC1091
source /dev/stdin <<<"$FUNCTIONS_FILE_PATH"
color
catch_errors
setting_up_container
network_check
update_os

msg_info "Installing dependencies"
$STD apt-get install -y curl ca-certificates tar python3 python3-venv
msg_ok "Installed dependencies"

bullpen_mktemp BULLPEN_SRC
bullpen_fetch_source "$BULLPEN_SRC"

msg_info "Installing ${APPLICATION} ${BULLPEN_SOURCE_VERSION}"
export BULLPEN_NONINTERACTIVE=1 BULLPEN_SOURCE_REF BULLPEN_SOURCE_KIND BULLPEN_TRACK
export HUB_UI_PASSWORD HUB_PORT HUB_PUBLIC_URL HUB_RUNNER_URL CF_TUNNEL_TOKEN
$STD bash "${BULLPEN_SRC}/scripts/linux/install-hub.sh" </dev/null
msg_ok "Installed ${APPLICATION} ${BULLPEN_SOURCE_VERSION}"
if [[ -n "${CF_TUNNEL_TOKEN:-}" ]]; then
  msg_ok "Connected the Cloudflare Tunnel"
fi

motd_ssh
customize
cleanup_lxc
