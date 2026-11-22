#!/usr/bin/env bash
# =============================================================================
#  uninstall-hub.sh: remove the Agent Bullpen hub from a Debian or Ubuntu
#  machine set up with install-hub.sh. Run it as root:
#
#      sudo bash /opt/bullpen/src/scripts/linux/uninstall-hub.sh
#
#  For a hub in a Proxmox container made by the one-line installer, it is
#  simpler to delete the whole container (docs/04-proxmox-hub.md, "Uninstall").
#
#  Options:
#    --dry-run       only show what would be removed; change nothing
#    --yes           don't ask before removing the hub (the data stays unless
#                    --delete-data is given too)
#    --delete-data   also delete the hub's data and settings, without asking
#    --keep-data     keep the data and settings, without asking
#
#  It shows what it will remove and asks first. Then it removes:
#    1. the bullpen service, and /opt/bullpen (the hub's code and packages)
#    2. the Cloudflare Tunnel connector, if the hub installer set it up
#    3. the "update" command and the login message, in a hub container
#    4. only if you say so: the data (/var/lib/bullpen), the settings
#       (/etc/bullpen, with the password and tokens) and the service user
#       'bullpen', which owns them
#  It leaves alone: Python and the other system packages, the cloudflared
#  package, and the runners on your machines (each removes itself with
#  "runner.py uninstall"). Safe to run again: it removes only what is left.
# =============================================================================
set -euo pipefail

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
interactive() { [[ -t 0 ]]; }
ask_yes() {  # ask_yes "Question" default(y/n); with no keyboard, the default
  local ans="" def=${2:-n}
  if interactive; then
    read -r -p "  $1 ($( [[ $def == y ]] && echo Y/n || echo y/N )): " ans || true
  fi
  case "${ans:-$def}" in [yY]*) return 0 ;; *) return 1 ;; esac
}

DRY_RUN=0
YES=0
DATA=ask
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)     DRY_RUN=1 ;;
    --yes|-y)      YES=1 ;;
    --delete-data) DATA=delete ;;
    --keep-data)   DATA=keep ;;
    -h|--help)     awk 'NR > 2 { if (/^# =====/) exit; sub(/^#  ?/, ""); sub(/^#$/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)             die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || die "Run this as root (for example: sudo bash $0)."
# Tests only: work in a fake root, with stub commands first on PATH.
ROOT="${BULLPEN_TEST_ROOT:-}"
if [[ -n "$ROOT" && "$(command -v systemctl)" != "${BULLPEN_TEST_STUBS:-/nonexistent}/"* ]]; then
  die "BULLPEN_TEST_ROOT is for tests only, with the test stubs first on PATH."
fi

APP_DIR=/opt/bullpen
ETC_DIR=/etc/bullpen
ENV_FILE=$ETC_DIR/hub.env
UNIT=/etc/systemd/system/bullpen.service
TUNNEL_UNIT=/etc/systemd/system/cloudflared.service
TUNNEL_ENV=/etc/cloudflared/bullpen-tunnel.env
UPDATE_CMD=/usr/bin/update
LOGIN_MESSAGE=/etc/profile.d/00_bullpen-details.sh

# The data folder is wherever hub.env says. It is deleted only if its name says it is the hub's.
DATA_DIR=$(sed -n "s/^HUB_DATA_DIR='\(.*\)'$/\1/p" "$ROOT$ENV_FILE" 2>/dev/null | tail -n1 || true)
DATA_DIR=${DATA_DIR:-/var/lib/bullpen}
data_dir_ok() { [[ $DATA_DIR == /?*/?* && $(basename "$DATA_DIR") == *bullpen* && $DATA_DIR != *..* ]]; }
ours() { [[ -f "$ROOT$1" ]] && grep -q "$2" "$ROOT$1"; }  # ours FILE TEXT: a file the installer wrote

HAS_SERVICE=0; [[ -f "$ROOT$UNIT" ]] && HAS_SERVICE=1
HAS_APP=0;     [[ -d "$ROOT$APP_DIR" ]] && HAS_APP=1
HAS_TUNNEL=0;  ours "$TUNNEL_UNIT" 'set up by Agent Bullpen' && HAS_TUNNEL=1
HAS_UPDATE=0;  ours "$UPDATE_CMD" 'Agent Bullpen installer' && HAS_UPDATE=1
HAS_LOGIN=0;   ours "$LOGIN_MESSAGE" 'Agent Bullpen installer' && HAS_LOGIN=1
HAS_DATA=0;    [[ -d "$ROOT$DATA_DIR" ]] && HAS_DATA=1
HAS_ETC=0;     [[ -d "$ROOT$ETC_DIR" ]] && HAS_ETC=1
HAS_USER=0;    id bullpen >/dev/null 2>&1 && HAS_USER=1

bold "Agent Bullpen: removing the hub from this machine"
if (( HAS_SERVICE + HAS_APP + HAS_TUNNEL + HAS_UPDATE + HAS_LOGIN + HAS_DATA + HAS_ETC + HAS_USER == 0 )); then
  ok "nothing of the hub is left on this machine"
  exit 0
fi
if (( HAS_UPDATE )); then
  info "This looks like the hub's Proxmox container. Deleting the container on the Proxmox host"
  info "removes everything at once (docs/04-proxmox-hub.md, \"Uninstall\")."
fi

echo
(( HAS_SERVICE + HAS_APP + HAS_TUNNEL + HAS_UPDATE + HAS_LOGIN )) && echo "  This removes:"
(( HAS_SERVICE )) && echo "    - the bullpen service ($UNIT), stopped first"
(( HAS_APP ))     && echo "    - $APP_DIR: the hub's code and its Python packages"
(( HAS_TUNNEL ))  && echo "    - the Cloudflare Tunnel connector: cloudflared.service and $TUNNEL_ENV"
(( HAS_UPDATE ))  && echo "    - the update command, $UPDATE_CMD"
(( HAS_LOGIN ))   && echo "    - the login message, $LOGIN_MESSAGE"
if (( HAS_DATA + HAS_ETC + HAS_USER )); then
  echo "  Only if you say so (asked separately):"
  (( HAS_DATA )) && echo "    - $DATA_DIR: the hub's data (jobs, notes, machines)"
  (( HAS_ETC ))  && echo "    - $ETC_DIR: the settings, with the dashboard password and tokens"
  (( HAS_USER )) && echo "    - the service user 'bullpen', which owns them"
fi
echo "  This leaves alone: Python and the other system packages, the cloudflared package, and the"
echo "  runners on your machines (on each one: runner.py uninstall)."

if (( DRY_RUN )); then
  echo
  echo "  Dry run: nothing was changed."
  exit 0
fi

echo
if (( ! YES )) && ! ask_yes "Remove the hub?" n; then
  echo "  Nothing was changed."
  interactive || echo "  (There is no keyboard to answer with: add --yes to remove it.)"
  exit 1
fi

DELETE_DATA=0
if (( HAS_DATA + HAS_ETC + HAS_USER )); then
  case "$DATA" in
    delete) DELETE_DATA=1 ;;
    keep)   ;;
    ask)  # with no keyboard to answer, they stay
      if interactive; then
        echo "  The data and settings can't be brought back once deleted. Copy $DATA_DIR and $ENV_FILE"
        echo "  somewhere safe first if you may want them again."
        if ask_yes "Also delete the hub's data, settings and service user?" n; then DELETE_DATA=1; fi
      fi ;;
  esac
fi
if (( DELETE_DATA && HAS_DATA )) && ! data_dir_ok; then
  die "The data folder in $ENV_FILE is $DATA_DIR, which doesn't look like the hub's own folder. Nothing was changed. Delete it yourself if you're sure, then run this again."
fi

echo
if (( HAS_SERVICE )); then
  systemctl disable --now bullpen >/dev/null 2>&1 || warn "systemctl couldn't stop the bullpen service; removing it anyway"
  rm -f "$ROOT$UNIT"
  ok "stopped and removed the bullpen service"
fi
if (( HAS_TUNNEL )); then
  systemctl disable --now cloudflared >/dev/null 2>&1 || true
  rm -f "$ROOT$TUNNEL_UNIT" "$ROOT$TUNNEL_ENV"
  ok "stopped and removed the Cloudflare Tunnel connector"
fi
if (( HAS_SERVICE + HAS_TUNNEL )); then
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl reset-failed bullpen cloudflared >/dev/null 2>&1 || true
fi
if (( HAS_APP )); then
  rm -rf "$ROOT$APP_DIR"
  ok "removed $APP_DIR"
fi
if (( HAS_UPDATE )); then rm -f "$ROOT$UPDATE_CMD"; ok "removed the update command"; fi
if (( HAS_LOGIN )); then rm -f "$ROOT$LOGIN_MESSAGE"; ok "removed the login message"; fi

if (( DELETE_DATA )); then
  if (( HAS_DATA )); then rm -rf "$ROOT$DATA_DIR"; ok "deleted $DATA_DIR"; fi
  if (( HAS_ETC )); then rm -rf "$ROOT$ETC_DIR"; ok "deleted $ETC_DIR"; fi
  if (( HAS_USER )); then userdel bullpen; ok "removed the service user 'bullpen'"; fi
elif (( HAS_DATA + HAS_ETC + HAS_USER )); then
  info "kept $DATA_DIR, $ETC_DIR and the service user 'bullpen'. Installing the hub again picks"
  info "them up. To delete them later: sudo rm -rf $DATA_DIR $ETC_DIR, then sudo userdel bullpen"
fi

bold "The hub is removed."
echo "  • On each machine, remove the runner too: python3 ~/.bullpen/runner/runner.py uninstall"
echo "    (on Windows, see docs/06-windows-machines.md, \"Removing a PC\")."
if (( HAS_TUNNEL )); then
  echo "  • Delete the tunnel and its Access application in the Cloudflare dashboard as well."
  echo "    The cloudflared package stays installed: apt-get remove cloudflared removes it."
fi
