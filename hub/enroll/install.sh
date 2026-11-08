#!/usr/bin/env bash
# =============================================================================
#  Agent Bullpen: add this computer to your hub (Linux or macOS)
#
#  Your hub's "Add a machine" page made this script for one machine. You can
#  read it before running it; reading it doesn't use up the code:
#
#      curl -fsSL '__HUB_URL__/enroll/__CODE__/install.sh' | less
#
#  What it does, as your normal user (it refuses to run as root):
#    1. Downloads the runner and the machine setup script (__SETUP__) from
#       your hub at __HUB_URL__ into ~/.bullpen/setup/, and checks each file
#       against the sha256 sums listed below.
#    2. Runs that setup script. It asks before each step and:
#         - installs what is missing: git, curl and python3 (with sudo, or with
#           Homebrew on macOS), Claude Code (Anthropic's installer) and, if you
#           want it, Gemini CLI;
#         - connects this computer to the hub with the one-time code below. The
#           hub gives the computer its own runner token, saved in
#           ~/.bullpen/runner.json;
#         - asks which agents to offer, and signs you in to each Claude account
#           (in your browser);
#         - starts the runner now and at every boot (a systemd user service on
#           Linux, a launchd agent on macOS).
#
#  It talks only to your hub and to the official download sites of the tools
#  it installs. It sends no usage data anywhere.
#
#  The code works once, and only until it expires (__MINUTES__ minutes after
#  this script was made). Running the script again is safe: finished steps are
#  skipped. Once the code is used, run the saved setup script instead:
#
#      bash ~/.bullpen/setup/__SETUP__
#
#  Options, added after "bash -s --" in the command:
#      --name NAME    the name this machine gets on the dashboard
#                     (default: the name chosen on the dashboard, else the host name)
#
#  Made by Agent Bullpen __VERSION__.
# =============================================================================
set -euo pipefail

HUB='__HUB_URL__'
CODE='__CODE__'
SETUP='__SETUP__'
# File name and sha256 of everything this script downloads from the hub.
FILES='
__FILES__
'

say()  { printf '  \033[36m-\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mAgent Bullpen:\033[0m %s\n\n' "$*" >&2; exit 1; }

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# Everything runs from main, so a download cut off halfway can't run half a script.
main() {
  # Root is refused because the logins belong in a normal user's home folder and Claude Code limits
  # what it does as root. BULLPEN_ALLOW_ROOT=1 overrides that (for containers and automated tests).
  [[ $(id -u) -ne 0 || ${BULLPEN_ALLOW_ROOT:-} == 1 ]] || die "Run this as your normal user, not root. It asks for sudo when it needs it."
  case "$(uname -s)" in
    Linux|Darwin) ;;
    *) die "This script is for Linux and macOS. On Windows, use the PowerShell command from the hub." ;;
  esac
  command -v curl >/dev/null 2>&1 || die "curl is needed (it is how this script was meant to be started)."

  local dir="$HOME/.bullpen/setup"
  mkdir -p "$dir/runner"
  printf '\n\033[1mAgent Bullpen: adding %s to the hub at %s\033[0m\n' "$(uname -n)" "$HUB"

  grep -q "^runner\.py " <<<"$FILES" || die "The hub has no runner files to hand out. Check its installation (docs/04)."
  grep -q "^$SETUP " <<<"$FILES" || die "The hub has no $SETUP to hand out (its installation is older or incomplete). Update the hub, or set this machine up by hand: see docs/05-linux-and-macos-machines.md."

  say "downloading the runner and $SETUP from the hub"
  local name sum dest tmp
  while read -r name sum; do
    [[ -n "$name" ]] || continue
    if [[ "$name" == *.py ]]; then dest="$dir/runner/$name"; else dest="$dir/$name"; fi
    tmp="$dest.download"
    # The address (with the code in it) goes to curl on stdin, not on its command line, where
    # other users of this computer could read it in the process list.
    printf 'url = "%s"\n' "$HUB/enroll/$CODE/files/$name" | curl -fsSL --retry 2 -o "$tmp" --config - \
      || die "Couldn't download $name from the hub. Is the code still valid? (It works for an hour, once.)"
    [[ "$(sha256_of "$tmp")" == "$sum" ]] || { rm -f "$tmp"; die "$name didn't match its checksum. Nothing was installed; try again."; }
    mv -f "$tmp" "$dest"
  done <<<"$FILES"
  chmod +x "$dir/$SETUP"
  ok "saved in $dir"

  # The setup asks questions, so give it the keyboard: when this script is piped into bash,
  # its own input is the download. Without a terminal it takes the defaults.
  local input=/dev/null
  if { : </dev/tty; } 2>/dev/null; then input=/dev/tty; fi
  # The code goes in the environment, not the command line, so other users can't read it
  # from the process list.
  BULLPEN_ENROLL_CODE="$CODE" bash "$dir/$SETUP" --hub "$HUB" --runner-from "$dir/runner" "$@" <"$input"
}

main "$@"
