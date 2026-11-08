#!/usr/bin/env bash
# =============================================================================
#  setup-api-gateway.sh: the pay-per-token (API key) side, on any Linux machine
#  with Docker and a few GB of free RAM.
#
#      bash scripts/linux/setup-api-gateway.sh
#
#  Installs with Docker:
#    1. LiteLLM + Postgres: one gateway for any number of Anthropic Console
#       organizations (and an optional Gemini API key), with a spend dashboard
#                                                   -> http://<this machine>:4000/ui
#    2. LibreChat (optional): a chat app that talks to LiteLLM
#                                                   -> http://<this machine>:3080
#  Each organization gets a short name, such as work or personal. Its models
#  are then called work/<model-id>, and its key pays for them.
#
#  Everything lives in ~/bullpen-stack. Safe to re-run: existing settings are
#  kept, and it offers to add organizations. Guide: docs/07-api-gateway.md
# =============================================================================
set -euo pipefail

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
info() { printf '  \033[36m•\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }
ask()  { local __v=$1 __q=$2 __d=${3:-} __a=""; read -r -p "  ${__q}${__d:+ [$__d]}: " __a || true; printf -v "$__v" '%s' "${__a:-$__d}"; }
ask_secret() { local __v=$1 __q=$2 __a=""; read -r -s -p "  ${__q}: " __a || true; echo; printf -v "$__v" '%s' "$__a"; }
ask_yes() { local a=""; read -r -p "  $1 ($( [[ ${2:-y} == y ]] && echo Y/n || echo y/N )): " a || true; a=${a:-${2:-y}}; [[ ${a,,} == y* ]]; }
hex() { python3 -c "import secrets; print(secrets.token_hex($1))"; }
set_env() {  # set_env FILE KEY VALUE  (replace the KEY= line or append it)
  # The value travels in the environment, not on the command line, so it never shows up in `ps`.
  local file=$1 key=$2
  BULLPEN_VALUE="$3" python3 - "$file" "$key" <<'PY'
import os, re, sys
path, key = sys.argv[1:3]
val = os.environ["BULLPEN_VALUE"]
lines = open(path, encoding="utf-8").read().splitlines()
out, done = [], False
for ln in lines:
    if re.match(rf"^\s*{re.escape(key)}=", ln):
        if not done:
            out.append(f"{key}={val}"); done = True
        continue
    out.append(ln)
if not done:
    out.append(f"{key}={val}")
open(path, "w", encoding="utf-8").write("\n".join(out) + "\n")
PY
}
env_value() { grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true; }  # env_value FILE KEY
files_hash() { cat "$@" 2>/dev/null | sha256sum || true; }  # files that don't exist yet count as empty

[[ $EUID -ne 0 ]] || die "Run this as your normal user; it uses sudo where needed."
command -v python3 >/dev/null || die "python3 is needed. Install it, then re-run."
command -v curl >/dev/null || die "curl is needed. Install it, then re-run."
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
GEN="$REPO_ROOT/stack/gateway-config.py"
STACK="$HOME/bullpen-stack"
mkdir -p "$STACK"
IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)

bold "Agent Bullpen: API gateway setup (LiteLLM, optional LibreChat)"
echo "  Requests through this gateway are billed per token, to the Console organization whose"
echo "  key they use. Some Claude plans include a monthly API credit for a Console organization"
echo "  you link to the plan; requests with that organization's key use the credit first."

# ------------------------------------------------------------------ docker
bold "1/3  Docker"
if ! command -v docker >/dev/null; then
  command -v apt-get >/dev/null || die "Docker isn't installed. Install Docker Engine and its Compose plugin (https://docs.docker.com/engine/install/), then re-run."
  info "installing docker.io from your distribution"
  sudo apt-get update -qq
  sudo apt-get install -y -qq docker.io
fi
sudo systemctl enable --now docker >/dev/null 2>&1 || true
if sudo docker compose version >/dev/null 2>&1; then
  COMPOSE=(sudo docker compose)
elif command -v docker-compose >/dev/null; then
  COMPOSE=(sudo docker-compose)
else
  if command -v apt-get >/dev/null; then
    info "installing the compose plugin"
    sudo apt-get install -y -qq docker-compose >/dev/null 2>&1 || sudo apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || true
  fi
  if sudo docker compose version >/dev/null 2>&1; then COMPOSE=(sudo docker compose)
  elif command -v docker-compose >/dev/null; then COMPOSE=(sudo docker-compose)
  else die "Couldn't find or install Docker Compose. Install it, then re-run."; fi
fi
ok "$(sudo docker --version)"

# ----------------------------------------------------------------- litellm
bold "2/3  LiteLLM gateway"
L="$STACK/litellm"
mkdir -p "$L"
cp "$REPO_ROOT/stack/litellm/docker-compose.yml" "$L/"
umask 077
if [[ -f "$L/.env" ]]; then
  ok "keeping existing settings in $L/.env"
else
  ask UI_USER "Username for the LiteLLM dashboard" "admin"
  ask_secret UI_PASS "Password for the LiteLLM dashboard (blank = generate)"
  UI_PASS=${UI_PASS:-$(hex 12)}
  cat > "$L/.env" <<EOF
# LiteLLM settings. After editing, re-run setup-api-gateway.sh (it rewrites config.yaml)
# or:  cd $L && sudo docker compose up -d --force-recreate litellm
LITELLM_MASTER_KEY=sk-$(hex 24)
# Encrypts keys stored in the database. Never change it after first start.
LITELLM_SALT_KEY=sk-$(hex 24)
POSTGRES_PASSWORD=$(hex 16)
UI_USERNAME=$UI_USER
UI_PASSWORD=$UI_PASS
# One line per Anthropic Console organization: ANTHROPIC_KEY_<NAME>=<API key>.
# DEFAULT_ORG pays for plain model names (claude-...), used by Claude Code through the gateway.
DEFAULT_ORG=
GEMINI_API_KEY=
EOF
  chmod 600 "$L/.env"
  ok "settings saved to $L/.env"
fi
chmod 600 "$L/.env"

mapfile -t ORGS < <(python3 "$GEN" orgs "$L/.env")
if (( ${#ORGS[@]} )); then
  ok "organizations: ${ORGS[*]}"
fi
echo "  Each Anthropic Console organization gets a short name and its own API key"
echo "  (platform.claude.com > API keys). Its models are then called <name>/<model-id>."
while ask_yes "Add $( (( ${#ORGS[@]} )) && echo another || echo an ) organization?" "$( (( ${#ORGS[@]} )) && echo n || echo y )"; do
  ask ORG "  Short name, e.g. work or personal" ""
  ORG=${ORG,,}
  if ! VAR=$(python3 "$GEN" check-name "$ORG"); then
    warn "'$ORG' can't be used: $VAR"
    continue
  fi
  if [[ -n "$(env_value "$L/.env" "$VAR")" ]] && ! ask_yes "  '$ORG' already has a key. Replace it?" n; then
    continue
  fi
  ask_secret KEY "  API key for '$ORG'"
  if [[ -z "$KEY" ]]; then warn "no key given; '$ORG' skipped"; continue; fi
  set_env "$L/.env" "$VAR" "$KEY"
  KEY=""
  mapfile -t ORGS < <(python3 "$GEN" orgs "$L/.env")
  ok "added '$ORG'"
done
if (( ${#ORGS[@]} > 1 )); then
  DEFAULT_ORG=$(env_value "$L/.env" DEFAULT_ORG)
  [[ " ${ORGS[*]} " == *" $DEFAULT_ORG "* ]] || DEFAULT_ORG=${ORGS[0]}
  ask NEW_DEFAULT "Which organization pays for plain model names, used by Claude Code through the gateway (${ORGS[*]})" "$DEFAULT_ORG"
  if [[ " ${ORGS[*]} " == *" $NEW_DEFAULT "* ]]; then
    set_env "$L/.env" DEFAULT_ORG "$NEW_DEFAULT"
  else
    warn "'$NEW_DEFAULT' isn't one of them; keeping '$DEFAULT_ORG'"
    set_env "$L/.env" DEFAULT_ORG "$DEFAULT_ORG"
  fi
fi
if [[ -z "$(env_value "$L/.env" GEMINI_API_KEY)" ]] && ask_yes "Add a Gemini API key from aistudio.google.com (optional)?" n; then
  ask_secret GKEY "Gemini API key"
  [[ -z "$GKEY" ]] || set_env "$L/.env" GEMINI_API_KEY "$GKEY"
  GKEY=""
fi
umask 022

FIRST_RUN=1; [[ -f "$L/config.yaml" ]] && FIRST_RUN=0
BEFORE=$(files_hash "$L/config.yaml" "$L/.env")
if python3 "$GEN" litellm "$L/.env" "$L/config.yaml"; then
  ok "model routes written to $L/config.yaml"
else
  warn "config.yaml was left as it is. To let this script manage it, delete the file and re-run."
fi
AFTER=$(files_hash "$L/config.yaml" "$L/.env")
# Compose reads POSTGRES_PASSWORD for variable substitution from .env in the same folder.
(cd "$L" && "${COMPOSE[@]}" pull -q && "${COMPOSE[@]}" up -d)
if (( ! FIRST_RUN )) && [[ "$BEFORE" != "$AFTER" ]]; then
  # LiteLLM reads config.yaml and its keys only when it starts.
  (cd "$L" && "${COMPOSE[@]}" up -d --force-recreate litellm)
fi
printf '  waiting for LiteLLM to start'
for _ in $(seq 1 60); do
  curl -fsS http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1 && break
  printf '.'; sleep 3
done
echo
UP=0
if curl -fsS http://127.0.0.1:4000/health/liveliness >/dev/null 2>&1; then
  UP=1
  ok "LiteLLM is up: http://${IP:-localhost}:4000/ui  (log in with the username/password in $L/.env)"
else
  warn "LiteLLM didn't answer yet. Check:  cd $L && ${COMPOSE[*]} logs -f litellm"
fi
# Debian 12 has only the older docker-compose; the guides' commands say "docker compose".
info "the compose command on this machine is: ${COMPOSE[*]}  (use it where the guides say 'sudo docker compose')"
MASTER=$(env_value "$L/.env" LITELLM_MASTER_KEY)

# --------------------------------------------------------------- librechat
bold "3/3  LibreChat chat app (optional)"
if ask_yes "Install or update LibreChat (about 1.5–2 GB of RAM, uses the gateway above)?" y; then
  if (( ! UP )); then
    warn "LibreChat needs the gateway running, to create its key and read the model list."
    warn "Skipped for now. Once LiteLLM answers, run this script again."
  else
    LC="$STACK/LibreChat"
    if [[ ! -d "$LC/.git" ]]; then
      git clone -q https://github.com/danny-avila/LibreChat.git "$LC"
    fi
    (cd "$LC" && git fetch -q --tags)
    TAG=$(cd "$LC" && git tag --sort=-v:refname | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    if [[ -n "$TAG" ]]; then (cd "$LC" && git -c advice.detachedHead=false checkout -q "$TAG"); ok "LibreChat $TAG"; fi

    if [[ ! -f "$LC/.env" ]]; then
      # A dedicated LiteLLM key for LibreChat, so its spend shows separately in the dashboard.
      VKEY=$(curl -fsS http://127.0.0.1:4000/key/generate -H "Authorization: Bearer $MASTER" \
        -H 'Content-Type: application/json' -d '{"key_alias":"librechat"}' \
        | python3 -c 'import sys, json; print(json.load(sys.stdin)["key"])' 2>/dev/null || true)
      [[ -n "$VKEY" ]] || die "Couldn't create a LiteLLM key for LibreChat. Check the LiteLLM log, then re-run."
      cp "$LC/.env.example" "$LC/.env"
      chmod 600 "$LC/.env"
      set_env "$LC/.env" LITELLM_API_KEY "$VKEY"
      set_env "$LC/.env" CREDS_KEY "$(hex 32)"
      set_env "$LC/.env" CREDS_IV "$(hex 16)"
      set_env "$LC/.env" JWT_SECRET "$(hex 32)"
      set_env "$LC/.env" JWT_REFRESH_SECRET "$(hex 32)"
      set_env "$LC/.env" MEILI_MASTER_KEY "$(hex 16)"
      set_env "$LC/.env" ALLOW_REGISTRATION true
      ask PUBLIC_URL "Address you'll open LibreChat at (e.g. https://chat.example.com)" "http://${IP:-localhost}:3080"
      set_env "$LC/.env" DOMAIN_CLIENT "$PUBLIC_URL"
      set_env "$LC/.env" DOMAIN_SERVER "$PUBLIC_URL"
      ok "LibreChat settings written"
    fi
    CFG_VERSION=$(awk '/^version:/ {print $2; exit}' "$LC/librechat.example.yaml" 2>/dev/null)
    LC_HAD_YAML=0; [[ -f "$LC/librechat.yaml" ]] && LC_HAD_YAML=1
    LC_BEFORE=$(files_hash "$LC/librechat.yaml")
    if curl -fsS http://127.0.0.1:4000/v1/models -H "Authorization: Bearer $MASTER" \
        | python3 "$GEN" librechat "$LC/librechat.yaml" "${CFG_VERSION:-1.3.18}"; then
      ok "LibreChat model list written to $LC/librechat.yaml"
    else
      warn "librechat.yaml was not written (see the message above)."
    fi
    cp "$REPO_ROOT/stack/librechat/docker-compose.override.yml" "$LC/"
    (cd "$LC" && "${COMPOSE[@]}" up -d)
    if (( LC_HAD_YAML )) && [[ "$LC_BEFORE" != "$(files_hash "$LC/librechat.yaml")" ]]; then
      (cd "$LC" && "${COMPOSE[@]}" restart api)  # LibreChat reads librechat.yaml only when it starts
    fi
    ok "LibreChat is starting: http://${IP:-localhost}:3080"
    cat <<EOF

  First visit: register an account; the first account becomes the admin.
  Then close registration so nobody else can sign up:
      cd $LC && sed -i 's/^ALLOW_REGISTRATION=true/ALLOW_REGISTRATION=false/' .env && sudo docker compose up -d
EOF
  fi
fi

bold "Done."
cat <<EOF
  LiteLLM dashboard:  http://${IP:-localhost}:4000/ui
  OpenAI-compatible:  http://${IP:-localhost}:4000/v1        (key: create one in the dashboard)
  Anthropic-style:    http://${IP:-localhost}:4000            (for Claude Code: ANTHROPIC_BASE_URL)
  Model names:        <organization>/<model-id>, for example ${ORGS[0]:-work}/<model-id>

  To reach these from outside your network, see docs/03-cloudflare.md (for example
  llm.<your-domain> -> http://${IP:-<this-machine>}:4000 behind Cloudflare Access).
EOF
