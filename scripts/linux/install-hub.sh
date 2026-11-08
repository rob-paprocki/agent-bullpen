#!/usr/bin/env bash
# =============================================================================
#  install-hub.sh: install (or upgrade) the Agent Bullpen on Debian or Ubuntu.
#
#  On Proxmox VE, the one-line installer (ct/bullpen.sh) runs this for you
#  inside a new container. You can also run it by hand on any Debian 12 or
#  newer, or Ubuntu 24.04 or newer, machine or VM, from a copy of this
#  repository:
#
#      sudo bash scripts/linux/install-hub.sh
#
#  What it does, step by step:
#    1. installs Python (3.11 or newer is needed) and a few basics with apt
#    2. creates a service user 'bullpen' and copies the hub to /opt/bullpen
#    3. creates a private Python environment with the hub's packages
#    4. writes settings to /etc/bullpen/hub.env (password, tokens, addresses)
#    5. installs a systemd service so the hub starts on boot
#    6. optionally installs cloudflared and connects your Cloudflare Tunnel
#
#  Re-running it upgrades the code. Your settings (/etc/bullpen/hub.env) and
#  data (/var/lib/bullpen) are kept; settings that newer versions added are
#  filled in with their defaults. If the upgraded hub does not start, the
#  previous version is put back.
#
#  Unattended use: set BULLPEN_NONINTERACTIVE=1 and any of these (blank = default).
#  They only apply to a new install; an upgrade keeps hub.env as it is.
#    HUB_UI_PASSWORD     dashboard password (blank: generate one)
#    HUB_PORT            port the hub listens on (8080)
#    HUB_RUNNER_URL      address your machines use to reach the hub
#                        (blank: http://<this machine's IP>:<port>)
#    HUB_PUBLIC_URL      the https address you open the dashboard at, if any
#    CF_TUNNEL_TOKEN     Cloudflare Tunnel token (blank: no tunnel)
#    HUB_ANTHROPIC_ADMIN_KEYS, HUB_LITELLM_UI_URL   optional Usage page settings
#  An env file with these lines can also be given as the first argument; it is
#  deleted after reading. Keep this interface stable: the Proxmox installer of
#  a newer branch may drive an older release's copy of this script.
# =============================================================================
set -euo pipefail
umask 022  # the service user must be able to read the files copied below

# ----------------------------------------------------------------- helpers ---
bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
interactive() { [[ "${BULLPEN_NONINTERACTIVE:-0}" != 1 && -t 0 ]]; }
say() { if interactive; then echo "$@"; fi; }  # explanations for someone answering questions

ask() {  # ask VAR "Question" "default"
  local __var=$1 __q=$2 __def=${3:-} __ans=""
  if [[ -n "${!__var:-}" ]]; then return; fi
  if interactive; then
    read -r -p "  ${__q}${__def:+ [$__def]}: " __ans || true
  fi
  printf -v "$__var" '%s' "${__ans:-$__def}"
}

ask_secret() {  # ask_secret VAR "Question"  (blank answer = generate one)
  local __var=$1 __q=$2 __ans=""
  if [[ -n "${!__var:-}" ]]; then return; fi
  if interactive; then
    read -r -s -p "  ${__q} (leave blank to generate one): " __ans || true
    echo
  fi
  printf -v "$__var" '%s' "${__ans:-$(gen_secret)}"
}

ask_yes() {  # ask_yes "Question" default(y/n)
  local q=$1 def=${2:-y} ans=""
  if interactive; then
    read -r -p "  $q ($( [[ $def == y ]] && echo Y/n || echo y/N )): " ans || true
  fi
  ans=${ans:-$def}
  [[ ${ans,,} == y* ]]
}

gen_secret() { python3 -c 'import secrets; print(secrets.token_urlsafe(24))'; }

# Write KEY='value' in a form both bash and systemd read literally.
envline() {
  [[ "$2" == *"'"* || "$2" == *$'\n'* ]] && die "The value for $1 can't contain a single quote or a line break."
  printf "%s='%s'\n" "$1" "$2"
}

first_ip() { hostname -I 2>/dev/null | awk '{print $1}'; }

# Adds a setting to an existing hub.env when an older version didn't write it.
add_missing_setting() {  # add_missing_setting KEY VALUE "comment"
  grep -q "^$1=" "$ROOT$ENV_FILE" && return 0
  { echo "$3"; envline "$1" "$2"; } >>"$ROOT$ENV_FILE"
  ok "added the new setting $1 to $ENV_FILE"
}

# The parts of a source tree the hub and the updater use.
copy_tree() {  # copy_tree SRC DEST
  local item
  rm -rf "$2"
  mkdir -p "$2"
  for item in VERSION LICENSE README.md hub runner agent-kit scripts ct misc install; do
    if [[ -e "$1/$item" ]]; then cp -r "$1/$item" "$2/"; fi
  done
  rm -rf "$2/hub/data"
  find "$2" -name __pycache__ -type d -prune -exec rm -rf {} +
}

healthy() {  # healthy PORT: waits up to 30 seconds for the hub to answer
  local _
  for _ in $(seq 1 30); do
    curl -fsS "http://127.0.0.1:$1/healthz" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# ------------------------------------------------------------------ checks ---
[[ $EUID -eq 0 ]] || die "Run this as root (for example: sudo bash $0)."
command -v apt-get >/dev/null || die "This script expects a Debian or Ubuntu system (apt-get)."

# Tests only: install into a fake root, with stub commands first on PATH.
ROOT="${BULLPEN_TEST_ROOT:-}"
if [[ -n "$ROOT" && "$(command -v apt-get)" != "${BULLPEN_TEST_STUBS:-/nonexistent}/"* ]]; then
  die "BULLPEN_TEST_ROOT is for tests only, with the test stubs first on PATH."
fi

# Optional answers file. It is deleted after reading.
if [[ $# -ge 1 && -f "$1" ]]; then
  # shellcheck disable=SC1090
  source "$1"
  rm -f "$1"
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
[[ -f "$SRC_DIR/hub/app.py" ]] || die "Can't find hub/app.py next to this script (looked in $SRC_DIR)."
if [[ -f "$SRC_DIR/VERSION" ]]; then
  SRC_VERSION=$(tr -d '[:space:]' <"$SRC_DIR/VERSION")
else
  SRC_VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC_DIR/hub/app.py" | head -n1)
fi

APP_DIR=/opt/bullpen
DATA_DIR=/var/lib/bullpen
ETC_DIR=/etc/bullpen
ENV_FILE=$ETC_DIR/hub.env
TUNNEL_ENV=/etc/cloudflared/bullpen-tunnel.env
SERVICE=bullpen
UPGRADE=0
[[ -f "$ROOT$ENV_FILE" ]] && UPGRADE=1
# Re-running the installed copy (for example to connect a tunnel later) keeps
# the record of which release this is.
if [[ -z "${BULLPEN_SOURCE_KIND:-}" && -f "$ROOT$APP_DIR/release.env" &&
  "$(cd "$SRC_DIR" && pwd -P)" == "$(cd "$ROOT$APP_DIR/src" 2>/dev/null && pwd -P)" ]]; then
  # shellcheck disable=SC1090,SC1091
  source "$ROOT$APP_DIR/release.env"
fi

bold ""
bold "Agent Bullpen installer ($([[ $UPGRADE == 1 ]] && echo upgrade || echo new install), version ${SRC_VERSION:-unknown})"
echo "  Source: $SRC_DIR"
echo

# --------------------------------------------------------- 1. system packages
bold "1/6  Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null
PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' ||
  die "The hub needs Python 3.11 or newer, and this system has $PY_VERSION. Use Debian 12 or newer, or Ubuntu 24.04 or newer."
ok "python3 $PY_VERSION ready"

# ------------------------------------------------------------ 2. user + files
bold "2/6  Creating the service user and copying the hub"
if ! id bullpen >/dev/null 2>&1; then
  useradd --system --home "$DATA_DIR" --shell /usr/sbin/nologin bullpen
  ok "created user 'bullpen'"
else
  ok "user 'bullpen' already exists"
fi
install -d -o bullpen -g bullpen -m 0750 "$ROOT$DATA_DIR"
install -d -m 0755 "$ROOT$APP_DIR"
install -d -m 0750 -g bullpen "$ROOT$ETC_DIR"
# A full copy of this version's files: the "update" command, and re-running
# this script later (for example to connect a tunnel), use it.
copy_tree "$SRC_DIR" "$ROOT$APP_DIR/src.new"
NEW="$ROOT$APP_DIR/app.new"
rm -rf "$NEW"
cp -r "$ROOT$APP_DIR/src.new/hub" "$NEW"
# The runner files the hub hands out for one-click runner updates, the agent
# add-ons, and the machine setup scripts the hub serves to new machines.
if [[ -d "$SRC_DIR/runner" ]]; then
  mkdir -p "$NEW/runner_dist"
  cp "$SRC_DIR/runner/"*.py "$NEW/runner_dist/"
fi
if [[ -d "$SRC_DIR/agent-kit" ]]; then cp -r "$SRC_DIR/agent-kit" "$NEW/agent_kit"; fi
for os_dir in linux macos windows; do
  if [[ -d "$SRC_DIR/scripts/$os_dir" ]]; then
    mkdir -p "$NEW/setup_dist"
    cp -r "$SRC_DIR/scripts/$os_dir" "$NEW/setup_dist/"
  fi
done
if [[ -n "${SRC_VERSION:-}" ]]; then echo "$SRC_VERSION" >"$NEW/VERSION"; fi
ok "hub code ready"

# --------------------------------------------------------- 3. python packages
# Installed before the new code goes live, so a failed download leaves the
# running version as it was.
bold "3/6  Installing Python packages in a private environment"
VENV="$ROOT$APP_DIR/venv"
# A venv made by another Python version (after an OS upgrade) can't be reused.
if [[ -x "$VENV/bin/python" ]] &&
  [[ "$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" != "$PY_VERSION" ]]; then
  warn "the Python environment was made by another Python version; making a new one"
  rm -rf "$VENV"
fi
[[ -x "$VENV/bin/python" ]] || python3 -m venv "$VENV"
# Keep a copy of the packages the running version uses: if the new version doesn't start, both
# its code and its packages are put back (a release may need newer packages the old code can't use).
rm -rf "$VENV.old"
if [[ $UPGRADE == 1 && -x "$VENV/bin/python" ]]; then cp -a "$VENV" "$VENV.old"; fi
if ! { "$VENV/bin/pip" install -q --upgrade pip && "$VENV/bin/pip" install -q -r "$NEW/requirements.txt"; }; then
  if [[ -d "$VENV.old" ]]; then rm -rf "$VENV"; mv "$VENV.old" "$VENV"; fi
  die "Installing the Python packages failed; nothing was changed. Check the internet connection and try again."
fi
ok "packages installed in $APP_DIR/venv"
rm -rf "$ROOT$APP_DIR/app.old" "$ROOT$APP_DIR/src.old"
if [[ -d "$ROOT$APP_DIR/app" ]]; then mv "$ROOT$APP_DIR/app" "$ROOT$APP_DIR/app.old"; fi
mv "$NEW" "$ROOT$APP_DIR/app"
if [[ -d "$ROOT$APP_DIR/src" ]]; then mv "$ROOT$APP_DIR/src" "$ROOT$APP_DIR/src.old"; fi
mv "$ROOT$APP_DIR/src.new" "$ROOT$APP_DIR/src"
ok "hub code copied to $APP_DIR/app"

# ---------------------------------------------------------------- 4. settings
bold "4/6  Settings"
if [[ $UPGRADE == 1 ]]; then
  ok "keeping existing settings in $ENV_FILE (delete it and re-run to start over)"
  set -a
  # shellcheck disable=SC1090
  source "$ROOT$ENV_FILE"
  set +a
  HUB_PORT=${HUB_PORT:-8080}
  add_missing_setting HUB_PUBLIC_URL "${HUB_PUBLIC_URL:-}" \
    "# The address OAuth sign-ins for MCP servers return to (blank: the address the dashboard is open at)."
  add_missing_setting HUB_RUNNER_URL "${HUB_RUNNER_URL:-http://$(first_ip):${HUB_PORT}}" \
    "# The address your machines use to reach the hub; shown in Add a machine."
else
  say "  The hub needs a password for the web page, and the address your machines"
  say "  use to reach it."
  say
  ask HUB_PORT "Port for the hub (1024-65535)" "8080"
  ask_secret HUB_UI_PASSWORD "Password for the dashboard"
  say
  say "  The address your machines use to reach the hub. Pick one they can all"
  say "  reach, usually this machine's LAN address."
  ask HUB_RUNNER_URL "Address for machines" "http://$(first_ip):${HUB_PORT}"
  say
  say "  Optional: the https address you'll open the dashboard at through a tunnel"
  say "  or reverse proxy, e.g. https://hub.example.com. MCP servers that use an"
  say "  OAuth sign-in send your browser back to it. Blank = whatever address you"
  say "  open the dashboard at."
  ask HUB_PUBLIC_URL "Public address (blank to skip, you can add it later)" ""
  say
  say "  Optional: Anthropic Admin API keys let the Usage page show API spend per"
  say "  Console organization. Format:  Work=sk-ant-admin01-...;Personal=sk-ant-admin01-..."
  ask HUB_ANTHROPIC_ADMIN_KEYS "Admin keys (blank to skip, you can add them later)" ""
  ask HUB_LITELLM_UI_URL "LiteLLM dashboard address if you set one up (blank to skip)" ""
  # The hub runs as an ordinary service user, which can't listen below 1024 (use a reverse proxy).
  if ! [[ $HUB_PORT =~ ^[0-9]+$ ]] || (( HUB_PORT < 1024 || HUB_PORT > 65535 )); then
    die "The hub's port must be from 1024 to 65535 (got '$HUB_PORT'). For 80 or 443, put a reverse proxy in front."
  fi
  HUB_RUNNER_TOKEN=${HUB_RUNNER_TOKEN:-$(gen_secret)}
  HUB_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  umask 027
  {
    echo "# Agent Bullpen settings. After editing:  systemctl restart $SERVICE"
    envline HUB_PORT "$HUB_PORT"
    envline HUB_DATA_DIR "$DATA_DIR"
    envline HUB_UI_PASSWORD "$HUB_UI_PASSWORD"
    echo "# The address your machines use to reach the hub; shown in Add a machine."
    envline HUB_RUNNER_URL "$HUB_RUNNER_URL"
    echo "# Shared token for runners set up by hand. Machines added with Add a machine get their own."
    envline HUB_RUNNER_TOKEN "$HUB_RUNNER_TOKEN"
    envline HUB_SECRET "$HUB_SECRET"
    echo "# Set to 1 once you only reach the hub over https: marks the login cookie Secure."
    envline HUB_COOKIE_SECURE 0
    envline HUB_ANTHROPIC_ADMIN_KEYS "${HUB_ANTHROPIC_ADMIN_KEYS:-}"
    envline HUB_LITELLM_UI_URL "${HUB_LITELLM_UI_URL:-}"
    echo "# Extra links for the Usage page:  Name=https://...;Other=https://..."
    envline HUB_LINKS ""
    echo "# The address OAuth sign-ins for MCP servers return to (blank: the address the dashboard is open at)."
    envline HUB_PUBLIC_URL "${HUB_PUBLIC_URL:-}"
  } >"$ROOT$ENV_FILE"
  umask 022
  chown root:bullpen "$ROOT$ENV_FILE"
  chmod 0640 "$ROOT$ENV_FILE"
  ok "settings saved to $ENV_FILE"
fi

# ----------------------------------------------------------------- 5. service
bold "5/6  Installing the systemd service"
cat >"$ROOT/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=Agent Bullpen (dashboard for your AI agents)
After=network-online.target
Wants=network-online.target

[Service]
User=bullpen
Group=bullpen
EnvironmentFile=$ENV_FILE
WorkingDirectory=$APP_DIR/app
ExecStart=$APP_DIR/venv/bin/uvicorn app:app --host 0.0.0.0 --port \${HUB_PORT} --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=always
RestartSec=3
# Light sandboxing: the hub only needs to write its database.
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now $SERVICE >/dev/null
systemctl restart $SERVICE
if healthy "$HUB_PORT"; then
  ok "hub is running on port $HUB_PORT"
elif [[ $UPGRADE == 1 && -d "$ROOT$APP_DIR/app.old" ]]; then
  warn "the new version didn't start; putting the previous version back"
  rm -rf "$ROOT$APP_DIR/app.failed"
  mv "$ROOT$APP_DIR/app" "$ROOT$APP_DIR/app.failed"
  mv "$ROOT$APP_DIR/app.old" "$ROOT$APP_DIR/app"
  if [[ -d "$ROOT$APP_DIR/src.old" ]]; then rm -rf "$ROOT$APP_DIR/src"; mv "$ROOT$APP_DIR/src.old" "$ROOT$APP_DIR/src"; fi
  if [[ -d "$VENV.old" ]]; then rm -rf "$VENV"; mv "$VENV.old" "$VENV"; fi
  systemctl restart $SERVICE
  if healthy "$HUB_PORT"; then
    die "The upgraded hub did not start, so the previous version is running again. See: journalctl -u $SERVICE -n 50"
  fi
  die "The upgraded hub did not start, and the previous version didn't start again either. See: journalctl -u $SERVICE -n 50"
elif interactive; then
  warn "the hub didn't answer yet. Check:  journalctl -u $SERVICE -n 50"
else
  journalctl -u $SERVICE -n 30 --no-pager 2>/dev/null || true
  die "The hub did not start. See: journalctl -u $SERVICE -n 50"
fi
rm -rf "$ROOT$APP_DIR/src.old" "$VENV.old"
# What is installed, for the "update" command.
{
  echo "# Written by install-hub.sh. The update command reads it."
  envline BULLPEN_REPO "${BULLPEN_REPO:-rob-paprocki/agent-bullpen}"
  envline BULLPEN_VERSION "${SRC_VERSION:-}"
  envline BULLPEN_SOURCE_REF "${BULLPEN_SOURCE_REF:-}"
  envline BULLPEN_SOURCE_KIND "${BULLPEN_SOURCE_KIND:-local}"
  echo "# Blank: updates follow new releases. A branch name: updates follow that branch."
  envline BULLPEN_TRACK "${BULLPEN_TRACK:-}"
  envline BULLPEN_INSTALLED_AT "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >"$ROOT$APP_DIR/release.env"

# ------------------------------------------------------------- 6. cloudflared
bold "6/6  Cloudflare Tunnel (optional)"
if command -v cloudflared >/dev/null && systemctl is-active --quiet cloudflared 2>/dev/null; then
  ok "cloudflared is already installed and running"
elif [[ -n "${CF_TUNNEL_TOKEN:-}" ]] || { interactive && ask_yes "Install cloudflared and connect a tunnel now?" n; }; then
  if ! command -v cloudflared >/dev/null; then
    info "adding Cloudflare's apt repository"
    install -d -m 0755 "$ROOT/usr/share/keyrings"
    curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o "$ROOT/usr/share/keyrings/cloudflare-main.gpg"
    echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
      >"$ROOT/etc/apt/sources.list.d/cloudflared.list"
    apt-get update -qq
    apt-get install -y -qq cloudflared >/dev/null
    ok "cloudflared $(cloudflared --version 2>/dev/null | awk '{print $3}') installed"
  fi
  if [[ -z "${CF_TUNNEL_TOKEN:-}" ]]; then
    echo "  In the Cloudflare dashboard: Networking > Tunnels > Create a tunnel > (name it) >"
    echo "  Debian. The install command ends with a long token (starts with 'ey...')."
    ask CF_TUNNEL_TOKEN "Paste just the token" ""
  fi
  if [[ -n "${CF_TUNNEL_TOKEN:-}" ]]; then
    # The token goes in a root-only file, not on cloudflared's command line,
    # so it never shows up in ps or in the service definition.
    install -d -m 0755 "$ROOT/etc/cloudflared"
    (umask 077 && envline TUNNEL_TOKEN "$CF_TUNNEL_TOKEN" >"$ROOT$TUNNEL_ENV")
    cat >"$ROOT/etc/systemd/system/cloudflared.service" <<EOF
[Unit]
Description=Cloudflare Tunnel connector (set up by Agent Bullpen)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
EnvironmentFile=$TUNNEL_ENV
ExecStart=/usr/bin/cloudflared --no-autoupdate tunnel run
Restart=on-failure
RestartSec=5s
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now cloudflared >/dev/null 2>&1 || warn "cloudflared didn't start; see: journalctl -u cloudflared -n 50"
    systemctl is-active --quiet cloudflared && ok "tunnel connector is running"
  else
    warn "no token given; skipped. Re-run this script later to connect the tunnel."
  fi
else
  info "skipped"
fi

# ------------------------------------------------------------------ summary
IP=$(first_ip)
DASHBOARD="http://${IP:-<this-machine>}:${HUB_PORT}"
echo
bold "Done. Agent Bullpen ${SRC_VERSION:-} is installed."
if interactive; then
  cat <<EOF

  Dashboard on your network:  $DASHBOARD
  Dashboard password:         (the one you chose; stored in $ENV_FILE)

  Add machines: open the dashboard, then Add a machine.

  Shared runner token, for machines you set up by hand with an older guide:

      ${HUB_RUNNER_TOKEN:-}

  Next steps
    • To reach the hub from outside your network, see docs/03-cloudflare.md.
    • Update later: sudo bash $APP_DIR/src/ct/bullpen.sh
      (inside a Proxmox container made by the one-line installer: type update)

  Useful commands
    systemctl status $SERVICE        journalctl -u $SERVICE -f
    nano $ENV_FILE  then  systemctl restart $SERVICE

EOF
else
  echo "  Dashboard: $DASHBOARD   Settings: $ENV_FILE"
fi
