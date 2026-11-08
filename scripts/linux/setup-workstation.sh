#!/usr/bin/env bash
# =============================================================================
#  setup-workstation.sh: make a Linux or macOS computer a machine the hub can
#  send jobs to. Run it as your normal user (not root):
#
#      bash scripts/linux/setup-workstation.sh
#
#  The easy way is the one-line command from "Add a machine" on the hub's
#  dashboard: it downloads this script from your hub and runs it with the
#  hub's address and a one-time code filled in.
#
#  Options (all optional; whatever isn't given is asked):
#    --hub URL          the hub's address, e.g. http://192.168.100.20:8080
#    --code CODE        a one-time code from "Add a machine" (or set BULLPEN_ENROLL_CODE)
#    --token TOKEN      a runner token instead of a code (or set BULLPEN_TOKEN)
#    --name NAME        this machine's name on the dashboard (or set BULLPEN_NAME)
#    --runner-from DIR  where runner.py is (default: the runner/ folder of this repository)
#
#  It walks you through these steps, asking before each one:
#    1. basics: git, curl and python3 (apt on Debian, Ubuntu and Kali; dnf,
#       pacman or zypper elsewhere, best effort; Homebrew or Apple's Command
#       Line Tools on macOS)
#    2. the runner files, and connecting this machine to the hub
#    3. Claude Code (Anthropic's official installer)
#    4. Gemini CLI (optional; needs Node.js 20 or newer and a Gemini API key)
#    5. the agents this machine offers: any number of Claude accounts and
#       Gemini agents, each with its own name
#    6. signing in to each Claude account
#    7. starting the runner now and at every boot (a systemd user service on
#       Linux, a launchd agent on macOS)
#    8. shared notes, /remember and MCP servers from the hub
#  Safe to re-run: finished steps are detected and skipped. It sends nothing
#  anywhere except to your hub and the download sites of what it installs.
# =============================================================================
set -euo pipefail

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
# Written for the bash 3.2 that macOS ships, so no ${var,,} and no associative arrays.
ask_yes() {
  local a="" def=${2:-y}
  read -r -p "  $1 ($( [[ $def == y ]] && echo Y/n || echo y/N )): " a || true
  case "${a:-$def}" in [yY]*) return 0 ;; *) return 1 ;; esac
}
pause() { read -r -p "  Press Enter to continue… " _ || true; }

# ------------------------------------------------------------------ options
HUB_URL=${BULLPEN_HUB:-}
CODE=${BULLPEN_ENROLL_CODE:-}
TOKEN=${BULLPEN_TOKEN:-}
NAME=${BULLPEN_NAME:-}
RUNNER_FROM=""
need() { [[ $# -ge 2 && -n "$2" ]] || die "$1 needs a value (see --help)."; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --hub)          need "$@"; HUB_URL=$2; shift 2 ;;
    --hub=*)        HUB_URL=${1#*=}; shift ;;
    --code)         need "$@"; CODE=$2; shift 2 ;;
    --code=*)       CODE=${1#*=}; shift ;;
    --token)        need "$@"; TOKEN=$2; shift 2 ;;
    --token=*)      TOKEN=${1#*=}; shift ;;
    --name)         need "$@"; NAME=$2; shift 2 ;;
    --name=*)       NAME=${1#*=}; shift ;;
    --runner-from)  need "$@"; RUNNER_FROM=$2; shift 2 ;;
    --runner-from=*) RUNNER_FROM=${1#*=}; shift ;;
    -h|--help)      awk 'NR > 2 { if (/^# =====/) exit; sub(/^#  ?/, ""); sub(/^#$/, ""); print }' "${BASH_SOURCE[0]}"; exit 0 ;;
    *)              die "Unknown option: $1 (see --help)" ;;
  esac
done
# Keep the code and token out of child processes' command lines and environment.
unset BULLPEN_ENROLL_CODE BULLPEN_TOKEN

# The logins belong in a normal user's home folder, and Claude Code limits what it does as root.
# BULLPEN_ALLOW_ROOT=1 overrides this (for containers and automated tests).
[[ $EUID -ne 0 || ${BULLPEN_ALLOW_ROOT:-} == 1 ]] || die "Run this as your normal user, not root. It uses sudo only to install packages."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER=${USER:-$(id -un)}
BIN="$HOME/.local/bin"
NPM_PREFIX="$HOME/.npm-global"
BULLPEN_DIR="${BULLPEN_HOME:-$HOME/.bullpen}"
CONFIG="$BULLPEN_DIR/runner.json"
RUNNER="$BULLPEN_DIR/runner/runner.py"
mkdir -p "$BIN" "$BULLPEN_DIR"
export PATH="$BIN:$NPM_PREFIX/bin:$PATH"

# ---------------------------------------------------------- what this is
OS_KIND=""
PM=""
case "$(uname -s)" in
  Darwin)
    OS_KIND=macos
    # Homebrew lives here even when your shell profile doesn't add it to PATH.
    for d in /opt/homebrew/bin /usr/local/bin; do [[ -d $d ]] && PATH="$PATH:$d"; done
    if command -v brew >/dev/null 2>&1; then PM=brew; fi ;;
  Linux)
    OS_KIND=linux
    for pm in apt-get dnf pacman zypper; do
      if command -v "$pm" >/dev/null 2>&1; then PM=${pm%-get}; break; fi
    done ;;
  *) die "This script is for Linux and macOS. On Windows, use scripts/windows/Setup-Workstation.ps1." ;;
esac
# shellcheck disable=SC1091  # read at run time, on this machine
DISTRO=$(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-}" || true)
[[ $OS_KIND == macos ]] && DISTRO="macOS $(sw_vers -productVersion 2>/dev/null || true)"

pkg_install() {  # pkg_install <package>... (Debian-style names; mapped where they differ)
  local pkgs=() p
  for p in "$@"; do
    case "$PM:$p" in
      pacman:python3) pkgs+=(python) ;;
      brew:python3)   pkgs+=(python) ;;
      *)              pkgs+=("$p") ;;
    esac
  done
  case "$PM" in
    apt)    sudo apt-get update -qq && sudo apt-get install -y -qq "${pkgs[@]}" ;;
    dnf)    sudo dnf install -y -q "${pkgs[@]}" ;;
    pacman) sudo pacman -S --needed --noconfirm "${pkgs[@]}" ;;
    zypper) sudo zypper --non-interactive install "${pkgs[@]}" ;;
    brew)   brew install "${pkgs[@]}" ;;
    *)      return 1 ;;
  esac
}

# Make sure ~/.local/bin and the npm folder are on PATH in new terminals (bash and zsh).
add_path_line() {
  # shellcheck disable=SC2016  # written as-is, so it expands in each new terminal
  local line='export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$PATH"' rc
  [[ $OS_KIND == macos && ! -f "$HOME/.zshrc" ]] && touch "$HOME/.zshrc"  # zsh is the macOS default
  for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
    [[ -f "$rc" ]] || continue
    grep -qF '.npm-global/bin' "$rc" || { printf '\n# added by bullpen setup\n%s\n' "$line" >> "$rc"; info "updated PATH in $rc"; }
  done
}

python_ok() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; }
node_major() { node -v 2>/dev/null | sed -E 's/^v([0-9]+).*/\1/' || true; }

bold "Agent Bullpen: machine setup"
echo "  Computer: $(uname -n)   User: $USER   System: ${DISTRO:-$(uname -sr)}"
if [[ $OS_KIND == linux && $PM != apt ]]; then
  warn "This script is written first for Debian, Ubuntu and Kali. Here it uses ${PM:-no package manager it knows},"
  warn "on a best-effort basis: if a step fails, install that piece yourself and run the script again."
fi
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  warn "ANTHROPIC_API_KEY is set in your shell. Claude Code would bill that API key instead of your"
  warn "subscription. Remove it from your shell profile unless that's what you want."
fi

# ------------------------------------------------------------------ 1. basics
bold "1/8  Basic tools: git, curl and python3"
if [[ $OS_KIND == macos ]]; then
  # Apple's Command Line Tools include git and python3; Homebrew is used if you have it.
  if ! xcode-select -p >/dev/null 2>&1 && [[ -z $PM ]]; then
    info "macOS needs Apple's Command Line Tools for git and python3 (or Homebrew, from brew.sh)."
    if ask_yes "Open the Command Line Tools installer now?" y; then xcode-select --install 2>/dev/null || true; fi
    die "When the Command Line Tools have finished installing, run this script again."
  fi
  if ! python_ok && [[ $PM == brew ]]; then info "installing Python with Homebrew"; pkg_install python3; fi
else
  need_pkgs=()
  for p in git curl python3; do command -v "$p" >/dev/null 2>&1 || need_pkgs+=("$p"); done
  if [[ ${#need_pkgs[@]} -gt 0 ]]; then
    info "installing: ${need_pkgs[*]}"
    pkg_install "${need_pkgs[@]}" || die "Couldn't install ${need_pkgs[*]}. Install them with your package manager, then run this again."
  fi
fi
command -v git >/dev/null 2>&1 || die "git is missing. Install it, then run this again."
python_ok || die "The runner needs Python 3.9 or newer; this computer has $(python3 --version 2>&1 || echo none). Install a newer one, then run this again."
ok "git, curl and $(python3 --version 2>&1) are present"
add_path_line

# ------------------------------------------------- 2. runner + the hub
bold "2/8  The runner, and connecting to the hub"
runner_version() { sed -n 's/^VERSION = "\(.*\)"/\1/p' "$1/runner.py" 2>/dev/null | head -n1; }
version_newer() { # version_newer A B: is A newer than B?
  python3 -c 'import sys
t = lambda v: tuple(int(x) for x in (v.split(".") + ["0", "0"])[:3] if x.isdigit())
sys.exit(0 if t(sys.argv[1]) > t(sys.argv[2]) else 1)' "$1" "$2"
}
RUNNER_SRC=""
for d in "$RUNNER_FROM" "$SCRIPT_DIR/../../runner" "$SCRIPT_DIR/runner" "$BULLPEN_DIR/runner"; do
  if [[ -n "$d" && -f "$d/runner.py" ]]; then RUNNER_SRC="$(cd "$d" && pwd)"; break; fi
done
[[ -n "$RUNNER_SRC" ]] || die "Can't find runner.py. Run this from the agent-bullpen repository, or use the command from Add a machine."
mkdir -p "$BULLPEN_DIR/runner"
if [[ "$RUNNER_SRC" == "$(cd "$BULLPEN_DIR/runner" && pwd)" ]]; then
  ok "runner is in $BULLPEN_DIR/runner"
elif [[ -z "$RUNNER_FROM" && -f "$BULLPEN_DIR/runner/runner.py" ]] &&
  ! version_newer "$(runner_version "$RUNNER_SRC")" "$(runner_version "$BULLPEN_DIR/runner")"; then
  # Re-running the saved setup (to add agents) must not put back the older runner it was saved
  # with: "Update now" may have installed a newer one since.
  ok "keeping the installed runner $(runner_version "$BULLPEN_DIR/runner") in $BULLPEN_DIR/runner"
else
  cp "$RUNNER_SRC/"*.py "$BULLPEN_DIR/runner/"
  ok "runner $(runner_version "$RUNNER_SRC") copied to $BULLPEN_DIR/runner"
fi
PY=$(python3 -c 'import sys; print(sys.executable)')
runner() { "$PY" "$RUNNER" --config "$CONFIG" "$@"; }

HUB_DONE=0
if [[ -n "$CODE" || -n "$TOKEN" ]]; then
  if [[ -z "$HUB_URL" ]]; then read -r -p "  Hub address (e.g. http://192.168.100.20:8080): " HUB_URL || true; fi
  [[ -n "$HUB_URL" ]] || die "The hub's address is needed (--hub)."
  name_args=()
  [[ -n "$NAME" ]] && name_args=(--name "$NAME")
  # The code or token reaches the runner through its environment, not its command line.
  if [[ -n "$CODE" ]]; then
    info "trading the one-time code for this machine's own runner token"
    BULLPEN_ENROLL_CODE="$CODE" runner enroll --hub "$HUB_URL" ${name_args[@]+"${name_args[@]}"} \
      || die "The hub didn't connect this machine (see the message above). Nothing else was changed."
  else
    BULLPEN_TOKEN="$TOKEN" runner enroll --hub "$HUB_URL" ${name_args[@]+"${name_args[@]}"} \
      || die "The hub didn't accept the token (see the message above)."
  fi
  HUB_DONE=1
elif [[ -f "$CONFIG" ]] && grep -q '"token": *"[^"]' "$CONFIG"; then
  current_name=$("$PY" -c 'import json, sys; print(json.load(open(sys.argv[1])).get("name", ""))' "$CONFIG" 2>/dev/null || true)
  if [[ -n "$NAME" && "$NAME" != "$current_name" ]]; then
    # The hub knows this machine by its name (its own token is tied to it), so renaming is done
    # with a new code made for the new name.
    die "This machine is on the hub as '$current_name'. To rename it, click Add a machine on the dashboard, type
  '$NAME' as the name, and run the command it shows here. Then Forget '$current_name' on the dashboard."
  fi
  ok "already connected to a hub (step 5 lets you keep or change that)"
else
  info "you'll be asked for the hub's address and a one-time code in step 5"
  info "(on the dashboard: Add a machine; or use the hub's shared runner token)"
fi

# ---------------------------------------------------------------- 3. Claude
bold "3/8  Claude Code"
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code already installed: $(claude --version 2>/dev/null | head -1)"
else
  info "installing with Anthropic's official installer (https://claude.ai/install.sh)"
  curl -fsSL https://claude.ai/install.sh | bash
  hash -r
  command -v claude >/dev/null 2>&1 || die "Claude Code didn't end up on PATH. Open a new terminal and run this again."
  ok "installed $(claude --version 2>/dev/null | head -1)"
fi

# ---------------------------------------------------------------- 4. Gemini
bold "4/8  Gemini CLI (optional)"
echo "  Gemini CLI needs a Gemini API key from https://aistudio.google.com/apikey (billed per use);"
echo "  Google AI Pro and other consumer sign-ins no longer work in it. See docs/01."

# Gemini CLI needs Node.js 20 or newer. Debian 12 and Ubuntu 24.04 ship Node 18, so on those
# this offers NodeSource's packages (or explains nvm) instead of giving up.
ensure_node() {
  local major candidate
  major=$(node_major)
  if [[ -n $major && $major -ge 20 ]]; then return 0; fi
  if [[ $OS_KIND == macos ]]; then
    if [[ $PM == brew ]]; then info "installing Node.js with Homebrew"; brew install node; hash -r
    else
      warn "Node.js 20 or newer is needed. Install it from https://nodejs.org (the LTS .pkg) or with"
      warn "Homebrew (brew install node), then run this again."
      return 1
    fi
  elif [[ $PM == apt ]]; then
    candidate=$(apt-cache policy nodejs 2>/dev/null | awk '/Candidate:/ {print $2}' | sed -E 's/^([0-9]+:)?([0-9]+).*/\2/')
    if [[ $candidate =~ ^[0-9]+$ && $candidate -ge 20 ]]; then
      info "installing Node.js $candidate and npm from your distribution"
      pkg_install nodejs npm
    else
      echo
      echo "  Your distribution offers Node.js ${candidate:-(none)}, and Gemini CLI needs 20 or newer."
      echo "  (Debian 12 and Ubuntu 24.04 ship 18.) Two ways to get a newer one:"
      echo "    a) NodeSource's packages: adds deb.nodesource.com to apt, so updates come with"
      echo "       your normal system updates. This script can do it for you now."
      echo "    b) nvm, in your home folder, no sudo:  https://github.com/nvm-sh/nvm#installing-and-updating"
      echo "       then:  nvm install --lts   and run this script again."
      if ask_yes "Use NodeSource now (current LTS release)?" y; then
        local tmp; tmp=$(mktemp)
        curl -fsSL https://deb.nodesource.com/setup_lts.x -o "$tmp"
        sudo -E bash "$tmp"
        rm -f "$tmp"
        sudo apt-get install -y -qq nodejs
      else
        return 1
      fi
    fi
  else
    info "installing Node.js and npm with $PM"
    pkg_install nodejs npm || true
  fi
  hash -r
  major=$(node_major)
  if [[ -n $major && $major -ge 20 ]]; then ok "Node.js $(node -v)"; return 0; fi
  warn "Node.js is still older than 20 ($(node -v 2>/dev/null || echo missing)). Install a newer one (nodejs.org or nvm) and run this again."
  return 1
}

if command -v gemini >/dev/null 2>&1; then
  ok "Gemini CLI already installed: $(gemini --version 2>/dev/null | head -1)"
elif ask_yes "Install Gemini CLI?" n; then
  if ensure_node; then
    if [[ -n "${NVM_DIR:-}" || "$(command -v node)" == "$HOME/.nvm/"* ]]; then
      # nvm refuses to work with a global npm prefix setting, so install into the folder directly.
      npm install -g --prefix "$NPM_PREFIX" @google/gemini-cli >/dev/null
    else
      npm config set prefix "$NPM_PREFIX"
      npm install -g @google/gemini-cli >/dev/null
    fi
    hash -r
    if command -v gemini >/dev/null 2>&1; then ok "installed Gemini CLI $(gemini --version 2>/dev/null | head -1)"
    else warn "Gemini CLI installed but isn't on PATH yet; open a new terminal later"; fi
  else
    warn "skipped Gemini CLI for now; run this script again once Node.js 20+ is installed"
  fi
fi

# ---------------------------------------------------------------- 5. agents
bold "5/8  The agents this machine offers"
echo "  The runner asks which agents to offer. Add one Claude agent for each Claude account you want"
echo "  to use here: the first is called 'claude' (its login lives in ~/.claude); give the others a"
echo "  name such as 'work', and each gets its own login folder (~/.claude-work). Gemini works the same."
echo "  Add your project folders now or later with:"
echo "    python3 ~/.bullpen/runner/runner.py add-project NAME /path/to/folder"
init_args=()
[[ $HUB_DONE == 1 ]] && init_args=(--skip-hub)
BULLPEN_HUB="$HUB_URL" BULLPEN_NAME="$NAME" runner init ${init_args[@]+"${init_args[@]}"}

# ---------------------------------------------------------------- 6. logins
bold "6/8  Sign in to each Claude account"
echo "  Each login opens your browser. Make sure the browser is signed in to the RIGHT Claude"
echo "  account first (tip: one browser profile per account, and copy the login link into that"
echo "  profile). Choose the option to sign in with your Claude subscription."

# A command per extra account, so 'claude-work' in a terminal uses that account's login.
make_wrapper() {  # make_wrapper <agent> <settings folder>
  local name=$1 dir=$2 file="$BIN/$1"
  if [[ -e "$file" ]] && ! grep -qE 'bullpen setup|Claude Code signed in to account' "$file"; then
    warn "$file exists and isn't ours, so it was left alone"
    return
  fi
  cat > "$file" <<EOF
#!/usr/bin/env sh
# Claude Code with the '$name' login (made by the bullpen setup). Its login and settings live in $dir.
export CLAUDE_CONFIG_DIR="$dir"
unset CLAUDECODE
exec claude "\$@"
EOF
  chmod +x "$file"
}

claude_as() {  # claude_as <folder> <default|own> <args>...: run claude with that account's settings
  local dir=$1 kind=$2; shift 2
  if [[ $kind == own ]]; then
    env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDECODE CLAUDE_CONFIG_DIR="$dir" claude "$@"
  else
    env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDECODE -u CLAUDE_CONFIG_DIR claude "$@"
  fi
}

signed_in() {  # signed_in <folder> <default|own>
  # 'claude auth status' also sees logins kept in the macOS Keychain; the file check covers
  # older Claude Code versions on Linux.
  if claude_as "$1" "$2" auth status --json 2>/dev/null | grep -q '"loggedIn": *true'; then return 0; fi
  [[ -f "$1/.credentials.json" ]]
}

wrappers=""
while IFS=$'\t' read -r agent dir kind how <&3; do
  [[ -n "$agent" ]] || continue
  if [[ $how != login ]]; then ok "$agent signs in with an API key or token; nothing to do"; continue; fi
  cmd=claude
  if [[ $kind == own ]]; then
    make_wrapper "$agent" "$dir"
    cmd="$agent"
    wrappers="$wrappers $agent"
  fi
  if signed_in "$dir" "$kind"; then ok "$agent is signed in ($dir)"; continue; fi
  if ask_yes "Sign in '$agent' now?" y; then
    mkdir -p "$dir"
    if claude auth --help >/dev/null 2>&1; then
      claude_as "$dir" "$kind" auth login || true
    else  # an older Claude Code: log in from inside a session
      echo "  Starting Claude Code. Choose the option to log in with your Claude subscription, then type /exit."
      pause
      claude_as "$dir" "$kind" || true
    fi
    if signed_in "$dir" "$kind"; then ok "$agent signed in"; else warn "no login found for $agent; later run: $cmd auth login"; fi
  fi
done 3< <(runner claude-accounts)
[[ -z "$wrappers" ]] || ok "a command for each extra account is in $BIN:$wrappers"

# ---------------------------------------------------------------- 7. runner service
bold "7/8  Start the runner"
runner list || true
while IFS= read -r agent <&3; do
  [[ -n "$agent" ]] || continue
  if ask_yes "Test '$agent' now (uses a tiny bit of its quota)?" y; then runner test "$agent" || true; fi
done 3< <("$PY" -c 'import json,sys; print("\n".join(json.load(open(sys.argv[1]))["agents"]))' "$CONFIG")

SERVICE_PATH="$BIN:$NPM_PREFIX/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# The service doesn't read your shell's settings: give it the Node.js this setup found (for example
# one from nvm, which lives in a version-specific folder; after switching versions, run this again).
if command -v node >/dev/null 2>&1; then SERVICE_PATH="$(dirname "$(command -v node)"):$SERVICE_PATH"; fi
SERVICE=""   # set to launchd or systemd once the runner is set up to start by itself
if [[ $OS_KIND == macos ]]; then
  if ask_yes "Start the runner now and whenever you log in (launchd agent)?" y; then
    label=bullpen.runner
    plist="$HOME/Library/LaunchAgents/$label.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <!-- Agent Bullpen runner: takes jobs from the hub. Made by the bullpen setup. -->
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$RUNNER</string><string>--config</string><string>$CONFIG</string><string>run</string></array>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$SERVICE_PATH</string></dict>
  <key>WorkingDirectory</key><string>$BULLPEN_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$BULLPEN_DIR/launchd.log</string>
  <key>StandardErrorPath</key><string>$BULLPEN_DIR/launchd.log</string>
</dict>
</plist>
EOF
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    if launchctl bootstrap "gui/$(id -u)" "$plist"; then SERVICE=launchd; else warn "launchctl couldn't load $plist"; fi
    sleep 2
    if launchctl print "gui/$(id -u)/$label" 2>/dev/null | grep -q 'state = running'; then ok "runner is running"
    else warn "runner didn't start; see ~/.bullpen/runner.log and ~/.bullpen/launchd.log"; fi
    info "it runs while you're logged in to this Mac (see docs/05 to log in automatically)"
    # macOS 15 and later ask before a background program may reach devices on the local network.
    info "macOS may ask whether Python can find devices on your local network: click Allow, or the runner"
    info "can't reach a hub on your network. If you're connected over SSH, allow it at the Mac in"
    info "System Settings > Privacy & Security > Local Network."
  fi
elif ! systemctl --user show-environment >/dev/null 2>&1; then
  warn "This system has no systemd user session, so the runner can't be set up as a service here."
  warn "Start it yourself (for example from your desktop's autostart):  $PY $RUNNER run"
elif ask_yes "Start the runner now and on every boot (systemd user service)?" y; then
  mkdir -p "$HOME/.config/systemd/user"
  cat > "$HOME/.config/systemd/user/bullpen-runner.service" <<EOF
[Unit]
Description=Agent Bullpen runner (takes jobs from the hub)
After=network-online.target

[Service]
Environment="PATH=$SERVICE_PATH"
ExecStart="$PY" "$RUNNER" --config "$CONFIG" run
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable bullpen-runner.service >/dev/null 2>&1 || true
  systemctl --user restart bullpen-runner.service && SERVICE=systemd
  # 'linger' keeps user services running after you log out and starts them at boot. You may turn it
  # on for yourself; sudo is only the fallback (when polkit isn't there to allow it).
  if [[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == yes ]]; then
    ok "linger is already on"
  elif loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"; then
    ok "linger is on: the runner starts at boot and keeps running after you log out"
  else
    warn "couldn't turn on linger; the runner will only run while you're logged in"
  fi
  sleep 2
  if systemctl --user is-active --quiet bullpen-runner; then ok "runner is running"
  else warn "runner didn't start; see: journalctl --user -u bullpen-runner -n 50"; fi
fi

# ------------------------------------------------------------ 8. shared context
bold "8/8  Shared notes, /remember and central MCP servers"
echo "  The hub adds your pinned notes, the /remember skill, the 'second opinion' add-on and the"
echo "  MCP servers from its MCP page to every Claude account, Gemini CLI and Antigravity here."
echo "  It keeps doing this by itself whenever something changes on the hub."
if runner sync; then
  ok "shared context synced"
else
  warn "sync didn't finish; it retries automatically once the runner can reach the hub"
fi

bold "All done."
if [[ $SERVICE == systemd ]]; then
  logs="journalctl --user -u bullpen-runner -f     (or ~/.bullpen/runner.log)"
  online="This machine should now show as online on the hub's Overview page."
elif [[ $SERVICE == launchd ]]; then
  logs="tail -f ~/.bullpen/runner.log"
  online="This machine should now show as online on the hub's Overview page."
else
  logs="tail -f ~/.bullpen/runner.log"
  online="Start the runner (python3 ~/.bullpen/runner/runner.py run) and this machine shows as online."
fi
cat <<EOF
  • $online
  • Logs:      $logs
  • Projects:  python3 ~/.bullpen/runner/runner.py add-project NAME /path
  • Add or remove agents later:  python3 ~/.bullpen/runner/runner.py init
    then run this script again to sign in to new Claude accounts.
  Open a new terminal so the PATH changes take effect.
EOF
