#!/usr/bin/env python3
"""Write the LiteLLM and LibreChat settings for the API gateway from a list of organizations.

scripts/linux/setup-api-gateway.sh runs this; you don't need to. Standard library only.

The list of Anthropic Console organizations is the gateway's .env file, one line each:

    ANTHROPIC_KEY_WORK=sk-ant-api03-...
    ANTHROPIC_KEY_PERSONAL=sk-ant-api03-...

The part after ANTHROPIC_KEY_ names the organization: WORK becomes the model prefix "work/".
Optional lines:

    DEFAULT_ORG=work        which organization pays for plain model names (default: the first)
    GEMINI_API_KEY=...      adds Gemini models as gemini/<model-id>

LiteLLM routes by pattern, so no model ids are written down here and none go out of date:

    work/<model-id>         Anthropic, with the WORK key
    claude-<...>            Anthropic, with the DEFAULT_ORG key (for Claude Code through the gateway)
    gemini/<model-id>       Google, with GEMINI_API_KEY

Commands:

    gateway-config.py orgs ENV_FILE                  print the organization names, one per line
    gateway-config.py check-name NAME                print NAME's .env variable, or why NAME can't be used
    gateway-config.py litellm ENV_FILE OUT_FILE      write LiteLLM's config.yaml
    gateway-config.py librechat OUT_FILE VERSION     write librechat.yaml; reads the gateway's
                                                     model list (GET /v1/models, JSON) on stdin

A file this script wrote starts with MARKER. It is only replaced while that line is there,
so delete the line to keep your own edits.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

MARKER = "# Written by setup-api-gateway.sh (Agent Bullpen). Running it again rewrites this file;"
MARKER_2 = "# delete these two lines to keep your own edits."
KEY_RE = re.compile(r"^ANTHROPIC_KEY_([A-Z][A-Z0-9_]{0,31})$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def name_problem(name: str) -> str:
    """Why an organization name can't be used, or "" if it can."""
    if not NAME_RE.match(name):
        return "use 1-32 lowercase letters, digits and hyphens, starting with a letter"
    if name.startswith("claude") or name == "gemini":
        return "names starting with 'claude', and 'gemini', are taken by the plain model routes"
    return ""


def read_env(path: Path) -> list[tuple[str, str]]:
    """KEY=VALUE lines of a .env file, in order (quotes around a value are removed)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out.append((key.strip(), value))
    return out


def env_var(org: str) -> str:
    """The .env name for an organization: work-eu -> ANTHROPIC_KEY_WORK_EU."""
    return "ANTHROPIC_KEY_" + org.upper().replace("-", "_")


def orgs(env: list[tuple[str, str]]) -> list[str]:
    """Organization names with a key, in file order: ANTHROPIC_KEY_WORK_EU -> work-eu."""
    names = []
    for key, value in env:
        m = KEY_RE.match(key)
        if m and value:
            name = m.group(1).lower().replace("_", "-")
            problem = name_problem(name)
            if problem:
                print(f"skipping {key}: {problem}", file=sys.stderr)
            elif name not in names:
                names.append(name)
    return names


def q(text: str) -> str:
    """A YAML double-quoted string (JSON's escaping is valid YAML)."""
    return json.dumps(text)


def litellm_config(env: list[tuple[str, str]]) -> str:
    names = orgs(env)
    values = dict(env)
    default = values.get("DEFAULT_ORG", "").strip().lower()
    if default not in names:
        default = names[0] if names else ""
    lines = [
        MARKER, MARKER_2,
        "#",
        "# LiteLLM model routes, one per Anthropic Console organization in .env. Ask for",
        "# <organization>/<model-id>, for example work/<model-id>, and that organization's key pays.",
        "# Model ids: https://platform.claude.com/docs/en/about-claude/models",
        "",
        "model_list:",
    ]
    if not names and not values.get("GEMINI_API_KEY"):
        lines[-1] = "model_list: []   # no keys in .env yet"
    for name in names:
        lines += [
            f"  # ---- Anthropic organization '{name}'",
            f"  - model_name: {q(name + '/*')}",
            f"    litellm_params: {{ model: \"anthropic/*\", api_key: {q('os.environ/' + env_var(name))} }}",
        ]
    if default:
        lines += [
            f"  # ---- Plain Anthropic names (claude-...), paid by '{default}'. They exist for tools that",
            "  #      only know Anthropic's own model ids, such as Claude Code with ANTHROPIC_BASE_URL.",
            "  - model_name: \"claude-*\"",
            f"    litellm_params: {{ model: \"anthropic/claude-*\", api_key: {q('os.environ/' + env_var(default))} }}",
        ]
    if values.get("GEMINI_API_KEY"):
        lines += [
            "  # ---- Gemini through an AI Studio API key: gemini/<model-id>",
            "  - model_name: \"gemini/*\"",
            "    litellm_params: { model: \"gemini/*\", api_key: \"os.environ/GEMINI_API_KEY\" }",
        ]
    lines += [
        "",
        "litellm_settings:",
        "  drop_params: true   # ignore settings a provider doesn't support instead of failing",
        "",
        "general_settings:",
        "  master_key: os.environ/LITELLM_MASTER_KEY",
        "",
    ]
    return "\n".join(lines)


def librechat_config(models: list[str], version: str) -> str:
    lines = [
        MARKER, MARKER_2,
        "#",
        "# LibreChat settings. Docs: https://www.librechat.ai/docs/configuration/librechat_yaml",
        f"version: {version}",
        "cache: true",
        "",
        "endpoints:",
        "  custom:",
        "    # Everything goes through your LiteLLM gateway, so spend is tracked per key. The model",
        "    # name says which organization pays: work/<model-id> is paid by 'work'.",
        "    - name: \"Gateway\"",
        "      apiKey: \"${LITELLM_API_KEY}\"",
        "      baseURL: \"http://host.docker.internal:4000/v1\"",
        "      models:",
        "        # LibreChat asks the gateway for its models when it starts (fetch); this list,",
        "        # what the gateway offered when the setup ran, is only used if that fails.",
        "        default:",
        *[f"          - {q(m)}" for m in models],
        "        fetch: true",
        "      titleConvo: true",
        "      titleModel: \"current_model\"",
        "      modelDisplayLabel: \"Gateway\"",
        "",
    ]
    return "\n".join(lines)


def gateway_models(data: dict) -> list[str]:
    """Model ids from the gateway's GET /v1/models answer, without wildcard routes."""
    ids = [str(m.get("id", "")) for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({i for i in ids if i and "*" not in i})


def write(path: Path, text: str) -> bool:
    """Write the file unless it exists without our marker (then it's the user's own)."""
    if path.exists():
        old = path.read_text(encoding="utf-8", errors="replace")
        if not old.startswith(MARKER):
            print(f"{path} was edited by hand (its first line isn't ours), so it was left alone.", file=sys.stderr)
            return False
        if old == text:
            return True
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "orgs":
        for name in orgs(read_env(Path(argv[1]))):
            print(name)
        return 0
    if len(argv) == 2 and argv[0] == "check-name":
        problem = name_problem(argv[1])
        print(problem or env_var(argv[1]))
        return 1 if problem else 0
    if len(argv) == 3 and argv[0] == "litellm":
        return 0 if write(Path(argv[2]), litellm_config(read_env(Path(argv[1])))) else 3
    if len(argv) == 3 and argv[0] == "librechat":
        if not re.match(r"^[0-9][0-9A-Za-z.\-]*$", argv[2]):
            print(f"'{argv[2]}' isn't a LibreChat config version", file=sys.stderr)
            return 2
        try:
            models = gateway_models(json.load(sys.stdin))
        except (ValueError, AttributeError) as err:
            print(f"couldn't read the gateway's model list: {err}", file=sys.stderr)
            return 2
        if not models:
            print("the gateway offers no models yet", file=sys.stderr)
            return 2
        return 0 if write(Path(argv[1]), librechat_config(models, argv[2])) else 3
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
