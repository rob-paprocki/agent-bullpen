#!/usr/bin/env python3
"""Agent Bullpen runner: the small program that runs on every machine.

It checks in with the hub, waits for jobs, runs the *official* agent CLIs
(Claude Code, Gemini CLI, ...) in your project folders, streams their
progress back, and reports the answer. It only uses Python's standard
library (3.9 or newer), so it runs on Linux, macOS and Windows with nothing
else installed.

    python runner.py enroll --hub URL --code CODE [--name NAME]
                                     connect this machine with a code from "Add a machine"
    python runner.py init            interactive setup (writes ~/.bullpen/runner.json)
    python runner.py run             connect to the hub and work (the default)
    python runner.py test <agent>    check that one agent answers on this machine
    python runner.py list            show the agents and projects this machine offers
    python runner.py limits          show the Claude plan limits this machine last saw
    python runner.py add-project NAME PATH
    python runner.py remove-project NAME

Nothing here touches your logins. Each CLI signs in through its own official
flow and keeps its own credentials; the runner just starts the CLI the same
way you would in a terminal.
"""

from __future__ import annotations

import argparse
import atexit
import base64
import datetime
import getpass
import hashlib
import json
import logging
import logging.handlers
import math
import ntpath
import os
import platform
import posixpath
import queue
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

VERSION = "1.1.0"
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
# The machine kind the hub's MCP list uses ("platforms" on the MCP page).
PLATFORM = "windows" if IS_WINDOWS else "macos" if IS_MACOS else "linux"
HOME = Path.home()
BASE_DIR = Path(os.environ.get("BULLPEN_HOME", HOME / ".bullpen"))
DEFAULT_CONFIG = BASE_DIR / "runner.json"
CONTEXT_DIR = BASE_DIR / "context"  # pinned shared notes, synced from the hub
HERE = Path(__file__).resolve().parent
APPLE_PYTHON = "/usr/bin/python3"
# Xcode wherever it is (Xcode-beta.app too), or the Command Line Tools.
APPLE_DEVELOPER_PYTHON_RE = re.compile(r"/Xcode[^/]*\.app/Contents/Developer/|^/Library/Developer/CommandLineTools/")

log = logging.getLogger("runner")


def stable_python() -> str:
    """The Python to write into the `bullpen` command and to restart with: this one, except on a Mac where
    it is Apple's Python inside Xcode or the Command Line Tools (what /usr/bin/python3 runs, and what
    sys.executable then names). That copy moves when Xcode is moved or switched, so it is /usr/bin/python3,
    which follows xcode-select, as the setup puts in the launchd agent."""
    exe = sys.executable
    if (sys.platform == "darwin" and APPLE_DEVELOPER_PYTHON_RE.search(exe or "")
            and os.access(APPLE_PYTHON, os.X_OK)):
        return APPLE_PYTHON
    return exe

# ---------------------------------------------------------------------------
# Agent presets: how to drive each official CLI headlessly.
#
# "autonomy" maps the levels you pick in the dashboard to CLI flags:
#   readonly  look but don't touch (answers, critiques, reviews)
#   edits     may edit files in the project, no other risky commands
#   ask       may edit files, and asks you on the dashboard before commands (Claude Code only)
#   full      may also run commands; the CLI's own safety classifier still applies
#
# If a CLI changes its flags, override them per agent in runner.json
# ("args", "autonomy", "resume", "model") instead of editing this file.
#
# Other keys a preset (or a runner.json entry) may use, for CLIs that work differently:
#   prompt_via     "stdin" (the default: the prompt is written to the CLI's input) or "arg":
#                  the prompt replaces "{prompt}" in the arguments, or is added as the last
#                  argument when none has "{prompt}". On Windows an argument is limited to about
#                  32,000 characters, and a .cmd/.bat launcher can't take one safely.
#   autonomy_env   extra environment variables per level, e.g. {"full": {"SOME_MODE": "auto"}}
#   new_session    arguments for a job that doesn't continue an earlier one; "{new_session_id}"
#                  becomes a fresh id, which is also reported as the session id for follow-ups
#   Templates in any argument list: {model}, {session_id}, {workdir} (the project folder).
#   Every job also gets PWD set to its project folder.
#   job_mcp        how the agent is handed the hub's job tools (/mcp/job) when the hub sends a job
#                  token: "mcp-config" (a per-job MCP config file, passed with mcp_config_args, where
#                  "{job_mcp_config}" becomes its path), or "gemini-settings" (a per-job copy of the
#                  machine's Gemini CLI system-defaults file with the job tools added, passed in
#                  GEMINI_CLI_SYSTEM_DEFAULTS_PATH). Absent: the agent gets no job tools.
#                  The MCP servers a task picked go in the same mcp-config file (see prepare_job_mcp).
#   strict_mcp_args   arguments that limit the agent to the servers in that file, for a task narrowed to
#                  the MCP servers it lists ("--strict-mcp-config" for Claude Code). Absent: such a task
#                  fails on this agent.
#   allow_tools_flag  the flag that pre-approves tools by name ("--allowedTools" for Claude Code); the
#                  job tools' names are added after it in the level's flags, or with it when it's absent
#   terminal       the command that continues a session in a terminal, with "{session_id}"
#                  (the dashboard's "continue in a terminal"); "" for none
#   usage_check    how "Check limits" asks the CLI for its plan usage: {"args": [...], "prompt":
#                  "/usage"}; the CLI is started with these arguments (not "args") and the prompt
#                  on its input, in the chat folder. Absent: the agent can't check plan limits.
#   live           arguments that keep the CLI's input open for more messages (a live session the
#                  dashboard can type into): every message is one JSON line on its input. Absent:
#                  the agent answers once per job.
#   remote_control arguments (after the command) that start the CLI's Remote Control server in a
#                  project, with "{rc_name}" and "{rc_spawn}". Absent: no Remote Control.
#   cli_version    arguments (after the command) that print the CLI's version ("--version"), checked
#                  before auth_status and auth_logout run, so an older CLI never gets them.
#   auth_status    arguments that print the CLI's own sign-in status as JSON ("auth status --json"),
#                  shown on the machine page (see SignInStore). Absent: no sign-in status.
#   auth_logout    arguments that sign the CLI out ("auth logout"), for "Sign out" on the machine page
#                  when runner.json allows it (allow_remote_sign_out). Absent: no sign-out.
#
# The "ask" level needs the hub's job tools (see prepare_job_tools): Claude Code asks through their
# permission prompt tool, which the runner never pre-approves.
# ---------------------------------------------------------------------------
PRESETS: dict[str, dict[str, Any]] = {
    "claude": {
        "command": "claude",
        "args": ["-p", "--output-format", "stream-json", "--verbose"],
        # --allowedTools lets headless jobs use the hub's shared memory without a prompt:
        # read-only jobs may only look things up; jobs that may change things may also save notes.
        "autonomy": {
            "readonly": ["--permission-mode", "dontAsk", "--allowedTools", "mcp__bullpen__recall", "mcp__bullpen__list_notes"],
            "edits": ["--permission-mode", "acceptEdits", "--allowedTools", "mcp__bullpen"],
            # Edit files, and ask the owner on the dashboard before commands (through the job tools).
            "ask": ["--permission-mode", "acceptEdits", "--allowedTools", "mcp__bullpen",
                    "--permission-prompt-tool", "mcp__bullpenjob__approve"],
            "full": ["--permission-mode", "auto", "--allowedTools", "mcp__bullpen"],
        },
        "resume": ["--resume", "{session_id}"],
        "model": ["--model", "{model}"],
        "live": ["--input-format", "stream-json"],
        "remote_control": ["remote-control", "--name", "{rc_name}", "--spawn", "{rc_spawn}"],
        "cli_version": ["--version"],
        "auth_status": ["auth", "status", "--json"],
        "auth_logout": ["auth", "logout"],
        "models": ["opus", "sonnet", "haiku", "fable"],  # suggestions; any model id works
        "parser": "claude-stream",
        # Claude Code refuses to start "inside" another Claude Code session;
        # these variables leak in if the runner itself was started from one.
        "unset_env": ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"],
        "job_mcp": "mcp-config",
        "mcp_config_args": ["--mcp-config", "{job_mcp_config}"],
        "strict_mcp_args": ["--strict-mcp-config"],
        "allow_tools_flag": "--allowedTools",
        # /usage in print mode answers locally (no model call); --max-turns 1 bounds the cost if an
        # older Claude Code sends it to the model as an ordinary prompt.
        "usage_check": {"args": ["-p", "--output-format", "stream-json", "--verbose", "--max-turns", "1",
                                 "--permission-mode", "dontAsk"], "prompt": "/usage"},
    },
    "gemini": {
        "command": "gemini",
        "args": ["--output-format", "stream-json", "--skip-trust"],
        "autonomy": {
            "readonly": ["--approval-mode", "default"],
            "edits": ["--approval-mode", "auto_edit"],
            "full": ["--approval-mode", "yolo"],
        },
        "resume": ["--resume", "{session_id}"],
        "model": ["--model", "{model}"],
        # Gemini CLI's own aliases; the exact model ids your API key can use are added
        # automatically from Google's model list (see gemini_models()).
        "models": ["auto", "pro", "flash", "flash-lite"],
        "parser": "gemini-stream",
        # Subtask tools through Gemini CLI's lowest settings layer (no approvals: it has no prompt tool).
        "job_mcp": "gemini-settings",
    },
    # A pretend agent for testing the plumbing before any real login exists.
    "selftest": {
        "command": sys.executable,
        "args": [str(HERE / "selftest_agent.py")],
        "autonomy": {
            "readonly": ["--permission-mode", "dontAsk"],
            "edits": ["--permission-mode", "acceptEdits"],
            "ask": ["--permission-mode", "acceptEdits", "--permission-prompt-tool", "mcp__bullpenjob__approve"],
            "full": ["--permission-mode", "auto"],
        },
        "resume": ["--resume", "{session_id}"],
        "model": ["--model", "{model}"],
        "live": ["--input-format", "stream-json"],
        # The fake needs its script first (the rest is Claude's).
        "remote_control": [str(HERE / "selftest_agent.py"), "remote-control", "--name", "{rc_name}",
                           "--spawn", "{rc_spawn}"],
        "cli_version": [str(HERE / "selftest_agent.py"), "--version"],
        "auth_status": [str(HERE / "selftest_agent.py"), "auth", "status", "--json"],
        "auth_logout": [str(HERE / "selftest_agent.py"), "auth", "logout"],
        "models": ["fake-small", "fake-large"],
        "parser": "claude-stream",
        "job_mcp": "mcp-config",
        "mcp_config_args": ["--mcp-config", "{job_mcp_config}"],
        "strict_mcp_args": ["--strict-mcp-config"],
        "allow_tools_flag": "--allowedTools",
        "terminal": "selftest --resume {session_id}",  # a pretend command, for the tests
        # Its transcripts go to CLAUDE_CONFIG_DIR only when its own env sets one (runner.json), never to one the
        # runner happens to have: that would be a real Claude Code folder (see session_config_dir).
        "unset_env": ["CLAUDE_CONFIG_DIR"],
        # The fake needs its script first (the CLI flags are Claude's).
        "usage_check": {"args": [str(HERE / "selftest_agent.py"), "-p", "--output-format", "stream-json",
                                 "--verbose", "--max-turns", "1", "--permission-mode", "dontAsk"],
                        "prompt": "/usage"},
    },
    # Any other CLI that reads a prompt (on stdin, or as an argument with prompt_via "arg")
    # and prints an answer.
    "text": {
        "command": "",
        "args": [],
        "autonomy": {"readonly": [], "edits": [], "full": []},
        "resume": [],
        "model": [],
        "models": [],
        "parser": "text",
    },
}

CHAT_README = """# Chat workspace

This folder is where the Agent Bullpen hub sends general (non-project) questions.
Answer the question directly. Don't create or edit files here unless the
request explicitly asks for a file.
"""


# =============================================================== configuration
def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.setdefault("max_parallel", 1)
    cfg.setdefault("projects", {})
    cfg.setdefault("agents", {})
    # Folder where projects created or cloned from the dashboard go.
    cfg.setdefault("projects_root", str(HOME / "projects"))
    # Let the dashboard add/clone/remove projects and update this runner.
    cfg.setdefault("allow_remote_projects", True)
    cfg.setdefault("allow_remote_update", True)
    return cfg


def save_config(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS and path.parent == BASE_DIR:
        os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    # Private from the first byte: it holds the hub token and maybe API keys.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    if not IS_WINDOWS:
        os.chmod(tmp, 0o600)  # in case the file already existed with wider permissions
    os.replace(tmp, path)


def expand_path(value: str) -> str:
    return os.path.expanduser(os.path.expandvars(value))


def agent_spec(cfg: dict, name: str) -> dict:
    """Merge an agent's runner.json entry over its preset."""
    entry = cfg["agents"][name]
    preset = PRESETS[entry.get("preset", "text")]
    spec = {**preset, **{k: v for k, v in entry.items() if k != "env"}}
    # The entry's levels replace the preset's one by one; a level set to null is removed, so a job
    # asking for it fails instead of running (see Job._task).
    levels = {**preset.get("autonomy", {}), **(entry.get("autonomy") or {})}
    spec["autonomy"] = {level: flags for level, flags in levels.items() if flags is not None}
    # Own levels may not mean what their names say, so the hub isn't told they do ("levels").
    spec["autonomy_overridden"] = "autonomy" in entry or "autonomy_env" in entry
    # Neither do the levels when other flags change permissions: a later --permission-mode wins.
    spec["permission_flags"] = permission_flags(spec, preset)
    spec["env"] = dict(entry.get("env", {}))
    spec["name"] = name
    spec.setdefault("label", name)
    return spec


# Flags that change what an agent may do, outside its levels: the CLI keeps the last --permission-mode
# (checked with Claude Code 2.1.319), and the others add rules, tools, folders or settings that hold
# permissions. Compared without dashes and case, so --allowedTools and --allowed-tools are one flag.
# Gemini CLI's short -y is --yolo.
PERMISSION_FLAGS = {
    "claude": ("--permission-mode", "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
               "--allowed-tools", "--disallowed-tools", "--permission-prompt-tool", "--permission-prompts",
               "--settings", "--setting-sources", "--add-dir", "--tools", "--agent", "--agents", "--plugin-dir",
               "--plugin-url"),
    "gemini": ("--approval-mode", "--yolo", "--allowed-tools", "--policy", "--admin-policy",
               "--include-directories"),
}
PERMISSION_SHORT_FLAGS = {"gemini": "y"}
PERMISSION_FLAG_LISTS = ("args", "live", "model", "resume", "new_session", "extra_args", "mcp_config_args",
                         "strict_mcp_args")


def flag_spelling(flag: str) -> str:
    """A long flag as the CLI's two spellings of it share it: "--allowedTools" and "--allowed-tools"
    both give "--allowed-tools". Other case changes are kept ("--allowedtools" stays as it is), since
    the CLI wouldn't take them for that flag."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", lambda m: "-" + m.group(1).lower(), flag) if flag.startswith("--") \
        else flag


def permission_flags(spec: dict, preset: dict) -> list[str]:
    """The flags in an agent's command line, outside its levels, that change its permissions (see
    PERMISSION_FLAGS), as written; [] for presets the runner doesn't know the flags of."""
    kind = "claude" if spec.get("preset") in ("claude", "selftest") else spec.get("preset")
    known = {"--" + f[2:].replace("-", "").lower() for f in PERMISSION_FLAGS.get(str(kind), ())}
    short = PERMISSION_SHORT_FLAGS.get(str(kind), "")
    found: list[str] = []
    words = [w for key in PERMISSION_FLAG_LISTS if isinstance(spec.get(key), list) for w in spec[key]]
    flag = spec.get("allow_tools_flag")
    if isinstance(flag, str) and flag and flag_spelling(flag) != flag_spelling(str(preset.get("allow_tools_flag") or "")):
        words.append(flag)  # another flag in place of the one that pre-approves the job tools
    for word in (str(w) for w in words):
        name = word.split("=", 1)[0]
        hit = (name.startswith("--") and "--" + name[2:].replace("-", "").lower() in known) or \
            (short and re.fullmatch(r"-[A-Za-z]+", name) is not None and any(c in short for c in name[1:]))
        if hit and name not in found:
            found.append(name)
    return found


def build_env(spec: dict) -> dict[str, str]:
    env = dict(os.environ)
    for key in spec.get("unset_env", []):
        env.pop(key, None)
    for key, value in spec.get("env", {}).items():
        value = str(value)
        if value.startswith("~") or key.endswith(("_DIR", "_HOME", "PATH")):
            value = expand_path(value)
        env[key] = value
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["BULLPEN_AGENT"] = spec.get("name", "")
    return env


def fill_args(args: list, values: dict[str, str]) -> list[str]:
    """An argument list with its templates filled in: `values` maps "{model}", "{session_id}", ... to text."""
    out = []
    for a in args:
        a = str(a)
        for key, value in values.items():
            a = a.replace(key, value)
        out.append(a)
    return out


def resolve_command(spec: dict) -> str | None:
    command = spec.get("command") or ""
    if not command:
        return None
    expanded = expand_path(command)
    if os.path.isabs(expanded):
        return expanded if os.path.exists(expanded) else None
    return shutil.which(expanded)


# ------------------------------------------------------------ secrets in logs
# Command lines go into runner.log (and git commands into the job log). Keys and tokens in
# them are replaced by *** first: values after flags that look secret, well-known key formats,
# passwords inside URLs, and the exact secrets this machine knows about (its hub token, API keys
# in agents' env).
SECRET_NAME_RE = re.compile(r"(token|key|secret|passw|auth|bearer|credential|cookie)", re.I)
SECRET_VALUE_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),                          # Anthropic, OpenAI and similar API keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),                         # Google API keys
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                   # Slack tokens
    # GitLab tokens. Prefixes from GitLab's "Token prefixes" table
    # (https://docs.gitlab.com/security/tokens/, last checked 2026-11-08): glpat personal/project/group access,
    # gldt deploy, glrt/glrtr runner (glrtr: made from a registration token), glptt trigger, glft feed, glimt incoming email, glagent Kubernetes agent,
    # gloas OAuth application secret, glcbt CI job, glsoat SCIM, glffct feature flags client, glwt workspace.
    re.compile(r"\bgl(?:pat|dt|rtr?|ptt|ft|imt|agent|oas|cbt|soat|ffct|wt)-[A-Za-z0-9_-]{20,}"),
    # Agent Bullpen machine, job and per-machine MCP tokens: "bp_", "bpj_" or "bpm_" and
    # secrets.token_urlsafe(32), which is always 43 URL-safe characters. Only that exact shape, so names
    # like bp_admin_routes are left alone.
    re.compile(r"\bbp[jm]?_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    # JWTs. Tried once per run of key characters (the run must end in ".xxxxxxxx."), so a long run can't
    # make it backtrack; a word joined to the token by a hyphen is masked with it.
    re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{8,}\.)[A-Za-z0-9_-]*?\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
]
AUTH_WORD_RE = re.compile(r"(?i)\b(bearer|basic|token)(\s+|=)[A-Za-z0-9._~+/=-]{8,}")
# Passwords inside URLs. This pattern and the next start only where a run of name characters starts,
# and check the end of the run once before looking inside it, so they take time in proportion to the
# text (lines can be long, and patterns that backtrack took hours on 20,000 characters).
URL_USERINFO_RE = re.compile(r"(?i)(?<![a-z0-9+.-])(?=[a-z0-9+.-]*://[^/@\s]+@)([a-z0-9+.-]*?\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")
# "X-Api-Key: value", "api_key=value" (also in URLs), "password: value", ...
NAMED_SECRET_RE = re.compile(r"(?i)(?<![\w-])(?=[\w-]+\s*[:=]\s*[^\s&,;\"']{6})([\w-]*?(?:token|key|secret|passw|credential)[\w-]*)(\s*[:=]\s*)([^\s&,;\"']{6,})")


KNOWN_SECRET_MIN = 6  # known_secrets leaves out shorter values: masking them would hide ordinary words


def known_secrets(cfg: dict, spec: dict | None = None, extra: list[str] | tuple = ()) -> list[str]:
    """Secret values this runner knows: its hub token, any key-looking env values, and `extra`
    (a job's token)."""
    found = [cfg.get("token") or "", cfg.get("cf_access_client_secret") or "", *extra]
    for key, value in ((spec or {}).get("env") or {}).items():
        if SECRET_NAME_RE.search(key):
            found.append(str(value))
    return [s for s in found if len(s) >= KNOWN_SECRET_MIN]


JOB_TOKEN_RE = re.compile(r"\bbpj_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")  # the shape the hub makes


def mask_job_token(text: str, token: str) -> str:
    """A job log line without the job's token (or anything that looks like a job token)."""
    if token:
        text = text.replace(token, "***")
    return JOB_TOKEN_RE.sub("***", text)


NO_URL_CREDENTIALS = "Put credentials in a git account on the machine, not in the URL."
# A clone URL's scheme and authority. Every slash after "scheme:" is skipped, because git and curl read
# "https:///u:pw@host/x" as user u at host: the authority is what follows the slashes.
URL_AUTHORITY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*):/*([^/?#]*)")


def url_has_userinfo(url: str) -> bool:
    """True when a clone URL carries a user name or password: https://user@host, https://user:pw@host,
    ssh://user:pw@host. A bare ssh user (ssh://git@host/...) and git@host:path carry no password. The '@' is
    looked for in everything up to the first '/' after the slashes, so a password holding a raw ? or # counts."""
    m = URL_AUTHORITY_RE.match(url or "")
    if not m:
        return False
    authority = url[m.start(2):].partition("/")[0]
    if "@" not in authority:
        return False
    userinfo = authority.rsplit("@", 1)[0]
    return m.group(1).lower() != "ssh" or ":" in userinfo


def redact(text: str, secrets_: list[str] | tuple = ()) -> str:
    for secret in sorted(secrets_, key=len, reverse=True):
        text = text.replace(secret, "***")
    text = URL_USERINFO_RE.sub(r"\1***@", text)
    text = AUTH_WORD_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    text = NAMED_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + "***", text)
    for rx in SECRET_VALUE_RES:
        text = rx.sub("***", text)
    return text


def redact_argv(argv: list[str], secrets_: list[str] | tuple = ()) -> str:
    """A command line, safe to log."""
    out: list[str] = []
    hide_next = False
    for arg in argv:
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        m = re.match(r"^(--?[A-Za-z][A-Za-z0-9_-]*)=", arg)
        if m and SECRET_NAME_RE.search(m.group(1)):
            out.append(m.group(1) + "=***")
            continue
        if re.match(r"^--?[A-Za-z][A-Za-z0-9_-]*$", arg) and SECRET_NAME_RE.search(arg):
            hide_next = True  # e.g. --api-key VALUE
        out.append(redact(arg, secrets_))
    return " ".join(out)


GEMINI_DIR = HOME / ".gemini"
GEMINI_ENV = GEMINI_DIR / ".env"


def gemini_key_saved() -> bool:
    return len(gemini_key()) > 8


def gemini_key(env: dict | None = None) -> str:
    """The Gemini API key an agent would use: its own env, else ~/.gemini/.env."""
    env = env or {}
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        if env.get(name):
            return env[name]
    try:
        for line in GEMINI_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


GEMINI_MODELS_CACHE = BASE_DIR / "gemini-models.json"
GEMINI_MODELS_MAX_AGE = 12 * 3600
_gemini_lock = threading.Lock()


def _model_sort_key(name: str) -> tuple:
    """Newest version first (3.8 before 3.5 before 3 before 2.5), stable releases before previews."""
    m = re.match(r"gemini-(\d+(?:\.\d+)*)", name)
    parts = [int(p) for p in m.group(1).split(".")] if m else [0]
    parts = (parts + [0, 0, 0])[:3]
    return (tuple(-p for p in parts), "preview" in name, name)


def fetch_gemini_models(key: str, timeout: float = 20) -> list[str]:
    """Ask Google which Gemini models this API key can use for chat (generateContent)."""
    names, token = [], ""
    for _ in range(10):  # pages
        base = os.environ.get("BULLPEN_GEMINI_MODELS_URL", "https://generativelanguage.googleapis.com/v1beta/models")
        url = base + "?pageSize=1000" + (f"&pageToken={token}" if token else "")
        req = urllib.request.Request(url, headers={"x-goog-api-key": key, "User-Agent": f"bullpen-runner/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        for m in data.get("models", []):
            name = (m.get("name") or "").removeprefix("models/")
            if not name.startswith("gemini") or "generateContent" not in (m.get("supportedGenerationMethods") or []):
                continue
            if re.search(r"(tts|image|embedding|native-audio|live|robotics|computer-use)", name):
                continue  # special-purpose models an agent CLI can't use
            names.append(name)
        token = data.get("nextPageToken") or ""
        if not token:
            break
    return sorted(set(names), key=_model_sort_key)


def gemini_models(key: str, refresh: bool = False) -> list[str]:
    """Cached list of the key's Gemini models (refreshed in the background every 12 hours)."""
    cached: dict = {}
    try:
        cached = json.loads(GEMINI_MODELS_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    fingerprint = hashlib.sha256(key.encode()).hexdigest()[:12]
    fresh = cached.get("key") == fingerprint and time.time() - cached.get("at", 0) < GEMINI_MODELS_MAX_AGE
    if refresh and key and not fresh and _gemini_lock.acquire(blocking=False):
        try:
            models = fetch_gemini_models(key)
            cached = {"key": fingerprint, "at": time.time(), "models": models}
            GEMINI_MODELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
            GEMINI_MODELS_CACHE.write_text(json.dumps(cached), encoding="utf-8")
        except (OSError, ValueError) as err:
            log.info("couldn't list Gemini models (%s); keeping the last list", err)
        finally:
            _gemini_lock.release()
    return list(cached.get("models") or []) if cached.get("key") == fingerprint else []


def save_gemini_key(key: str) -> None:
    """Gemini CLI reads ~/.gemini/.env by itself, in the terminal and headless.
    Also make sure it is set to use the API key rather than a Google login,
    because Google AI Pro logins no longer work in Gemini CLI."""
    key = key.strip()
    if not key:
        print("  no key given; skipped")
        return
    GEMINI_DIR.mkdir(parents=True, exist_ok=True)
    lines = []
    if GEMINI_ENV.exists():
        lines = [ln for ln in GEMINI_ENV.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}")
    GEMINI_ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not IS_WINDOWS:
        os.chmod(GEMINI_ENV, 0o600)
    settings_file = GEMINI_DIR / "settings.json"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
    except ValueError:
        print(f"  note: couldn't read {settings_file}; set the auth type to 'Gemini API key' with /auth in gemini")
        return
    auth = settings.setdefault("security", {}).setdefault("auth", {})
    if auth.get("selectedType") != "gemini-api-key":
        auth["selectedType"] = "gemini-api-key"
        settings_file.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    print(f"  saved the key in {GEMINI_ENV} and set Gemini CLI to use it")


# ------------------------------------------------- what each agent can do (heartbeat)
# Words a machine can be given in runner.json "tags" (the platform tag is added at run time).
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
MAX_TAGS = 16                  # what a machine reports, its platform tag included
MAX_OWN_TAGS = MAX_TAGS - 1    # the owner's own words
PLATFORM_TAGS = ("linux", "macos", "windows")
# An agent's account label ("account" in runner.json): shown on the dashboard, sent only when set.
ACCOUNT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")
# Git accounts (runner.json "git_accounts"): named git identities on this machine, picked per task, follow-up,
# clone or pull, with a default for the machine ("default_git_account") and per project
# ("project_git_accounts"). A name only picks credentials here: it never changes which machine, agent or AI
# account runs a job. The hub learns names, hosts and kinds only, never a token, a key path, a commit name or
# an email. Names and hosts are matched whole (fullmatch), so a trailing newline doesn't pass.
GIT_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
# A host name and maybe a port: no scheme, path or user@.
GIT_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?$")
GIT_KINDS = ("github", "gitlab", "other")
GIT_ACCOUNT_FIELDS = ("host", "kind", "name", "email", "ssh_key", "token", "user")
# Control characters (C0, DEL, C1). The key path goes into a job's ssh config file in quotes, and user and token
# into a credential helper's output, one line each: such a character there could end the line and add another.
GIT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Not in user or token either, and no double quote or backslash there: the helper prints them as they are
# (printf '%s'), but neither belongs in a user name or a token.
GIT_HELPER_UNSAFE_RE = re.compile(r'["\\\x00-\x1f\x7f-\x9f]')
# A shorter token is refused: no real one is that short, and the job's log masks every exact copy of it, which
# for a value that short would hide ordinary words too.
GIT_TOKEN_MIN = 8
# An agent whose own env sets any of these keeps its own git setup: a default account is skipped for its jobs
# (an account picked for the task still applies).
GIT_ENV_VARS = ("GIT_SSH_COMMAND", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "GIT_CONFIG_COUNT", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN")
# Presets whose readonly/edits/full flags the runner knows mean what the names say.
LEVEL_PRESETS = {"claude", "gemini", "selftest"}
# How an agent can be handed the hub's job tools ("job_mcp"): a --mcp-config file (Claude Code), or
# Gemini CLI's system-defaults file (see gemini_job_defaults).
JOB_MCP_MODES = ("mcp-config", "gemini-settings")
COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def claude_auth(env: dict) -> str:
    """How a Claude Code agent signs in: 'api_key', 'token' (claude setup-token or
    CLAUDE_CODE_OAUTH_TOKEN), 'other' (not Anthropic's own service), or 'login' (a full
    claude.ai login)."""
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return "api_key"
    if any(env.get(k) for k in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
        return "other"
    base = env.get("ANTHROPIC_BASE_URL", "")
    if base and not re.match(r"^https://api\.anthropic\.com/?$", base):
        return "other"
    if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        return "token"
    return "login"


def agent_auth(spec: dict, env: dict) -> str:
    """The sign-in kind the hub shows in its messages."""
    if spec.get("parser") == "claude-stream":
        return claude_auth(env)
    return "api_key" if spec.get("preset") == "gemini" else ""


def credits_agent(cfg: dict) -> tuple[str | None, str]:
    """runner.json "credits_agent": the agent the hub may use for background work paid by platform
    credits (the API credits some Claude plans include). (name, "") when it is usable: one of this
    machine's Claude agents that signs in with an API key. (None, why) when it is set but can't be
    used, (None, "") when it isn't set."""
    name = cfg.get("credits_agent")
    if name is None or name == "":
        return None, ""
    if not isinstance(name, str) or name not in (cfg.get("agents") or {}):
        return None, f"credits_agent {name!r} isn't one of this machine's agents; it is ignored"
    try:
        spec = agent_spec(cfg, name)
    except (KeyError, TypeError, AttributeError):
        return None, f"credits_agent {name!r} has an entry the runner can't read; it is ignored"
    if spec.get("parser") != "claude-stream" or spec.get("preset") not in ("claude", "selftest"):
        return None, f"credits_agent {name!r} isn't a Claude agent; it is ignored"
    if claude_auth(build_env(spec)) != "api_key":
        return None, (f"credits_agent {name!r} doesn't sign in with an API key (ANTHROPIC_API_KEY in its env), "
                      "so it can't use platform credits; it is ignored")
    return name, ""


def usage_check_settings(spec: dict) -> dict | None:
    """The agent's "usage_check" settings when they are usable: {"args": [...], "prompt": str}."""
    check = spec.get("usage_check")
    if not isinstance(check, dict) or not isinstance(check.get("args"), list):
        return None
    prompt = check.get("prompt")
    return {"args": [str(a) for a in check["args"]],
            "prompt": prompt if isinstance(prompt, str) and prompt else "/usage"}


def can_summarize(spec: dict) -> bool:
    """Can the runner write a summary of this agent's jobs (see Job._summarize): its resume arguments continue a
    session by its id ("{session_id}": one without it, such as --continue, could continue another job's session in
    the same folder), it has a readonly level with no permission flags outside it (they would win over it, so the turn
    might not only read: see permission_flags), and the runner learns the session's id (the Claude and Gemini stream
    parsers read it from the CLI; with new_session the runner makes it)."""
    resume = spec.get("resume")
    if not isinstance(resume, list) or not any("{session_id}" in str(a) for a in resume):
        return False
    if not isinstance((spec.get("autonomy") or {}).get("readonly"), list) or spec.get("permission_flags"):
        return False
    return spec.get("parser") in ("claude-stream", "gemini-stream") or bool(spec.get("new_session"))


def agent_features(spec: dict, env: dict) -> list[str]:
    """What this agent can do that older runners couldn't, for the hub to check before it sends
    a job that needs it. job_mcp: it can be handed the job tools at /mcp/job. approvals: it has an
    'ask' level and takes the job tools, so it can ask the owner before commands. mcp_pick: it can be
    handed the MCP servers a task picked (Claude Code's way, see prepare_job_mcp). live: it can
    keep a session open for more messages. levels: its readonly/edits/full flags are the runner's
    own, so the names mean what they say. summary: it can continue a job's session for a summary
    (can_summarize). limits: its jobs report the account's plan limits (a claude.ai login or a
    setup-token token). usage_check: "Check limits" can ask it for them, and remote_control: it
    can run Remote Control (a full login only: a setup-token token can do neither). sign_out: "Sign out"
    can run its auth_logout (a full login only)."""
    features = []
    claude_like = spec.get("parser") == "claude-stream"
    auth = claude_auth(env) if claude_like else ""
    mode = spec.get("job_mcp")
    mcp_config = mode == "mcp-config" and bool(spec.get("mcp_config_args"))
    # Gemini CLI reads the job's settings file only where root owns it (see gemini_job_tools_problem).
    if mcp_config or (mode == "gemini-settings" and gemini_job_tools_problem() is None):
        features.append("job_mcp")
    # Asking the owner needs Claude Code's permission prompt tool, so only the --mcp-config way. A
    # permission flag outside the levels (a later --permission-mode, say) would win over asking.
    if mcp_config and isinstance((spec.get("autonomy") or {}).get("ask"), list) \
            and not spec.get("permission_flags"):
        features.append("approvals")
    if takes_mcp_picks(spec):
        features.append("mcp_pick")
    if spec.get("live") and claude_like and (spec.get("prompt_via") or "stdin") == "stdin":
        features.append("live")
    if spec.get("preset") in LEVEL_PRESETS and not spec.get("autonomy_overridden") \
            and not spec.get("permission_flags"):
        features.append("levels")
    if can_summarize(spec):
        features.append("summary")
    if claude_like and auth in ("login", "token"):
        features.append("limits")
    if claude_like and auth == "login" and usage_check_settings(spec):
        features.append("usage_check")
    if claude_like and auth == "login" and isinstance(spec.get("remote_control"), list) and spec["remote_control"]:
        features.append("remote_control")
    if claude_like and auth == "login" and isinstance(spec.get("auth_logout"), list) and spec["auth_logout"]:
        features.append("sign_out")
    if takes_session_moves(spec, env):
        features.append("session_move")
    return features


# ------------------------------------------------------------------ moving a session (exact moves)
# "Continue on another machine" on the dashboard can move one Claude Code session to another machine signed in to the
# same account (hub/moves.py). Claude Code keeps each session's transcript as
#   <config folder>/projects/<project folder>/<session id>.jsonl
# where the config folder is CLAUDE_CONFIG_DIR or ~/.claude, and the project folder is the working folder's path with
# every character other than a letter or digit replaced by "-" (a name over 200 characters is cut and gets a hash
# instead, which isn't documented, so such paths can't move), or CLAUDE_CODE_PROJECT_DIR_NAME when it is set with
# CLAUDE_CONFIG_DIR (https://code.claude.com/docs/en/sessions, "Where transcripts are stored", checked 2026-11-17).
# The runner reads and writes that one file, by its exact path: nothing else in the config folder (no login, no
# settings), no listing, and never through a link.
SESSION_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SESSION_MOVE_MAX_BYTES = 256 * 1024 * 1024  # the most a runner ever reads or writes, whatever the hub asks
CLAUDE_DIR_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CLAUDE_PROJECT_NAME_MAX = 200


class SessionMoveError(Exception):
    """A session that can't be read or written here. The message says why, for the job's error."""


def takes_session_moves(spec: dict, env: dict) -> bool:
    """Can this agent's sessions move between machines: a Claude Code agent (or the self-test one) that resumes a
    session by its id and keeps transcripts (CLAUDE_CODE_SKIP_PROMPT_HISTORY turns them off)?"""
    resume = spec.get("resume")
    return (spec.get("preset") in ("claude", "selftest") and spec.get("parser") == "claude-stream"
            and isinstance(resume, list) and any("{session_id}" in str(a) for a in resume)
            and not env.get("CLAUDE_CODE_SKIP_PROMPT_HISTORY"))


def session_config_dir(spec: dict, env: dict) -> Path:
    """The agent's config folder, where its sessions are: CLAUDE_CONFIG_DIR, else ~/.claude (the self-test agent's
    pretend one is in the runner's own folder, so tests never touch a real one)."""
    custom = env.get("CLAUDE_CONFIG_DIR") or ""
    if custom:
        return Path(expand_path(custom))
    if spec.get("preset") == "selftest":
        return BASE_DIR / "selftest" / "claude"
    return HOME / ".claude"


def claude_project_dirnames(workdir: str, env: dict) -> list[str]:
    """The names Claude Code may give the project folder of sessions started in workdir, most likely first: the
    pinned name (CLAUDE_CODE_PROJECT_DIR_NAME with CLAUDE_CONFIG_DIR), else the path with every character other than
    a letter or digit made "-". On Linux and macOS the path with its links resolved comes first (the folder a
    program finds itself in), then the path as written. Paths with other than plain ASCII characters, or whose name
    would be over 200 characters, give none: their names aren't documented."""
    pinned = env.get("CLAUDE_CODE_PROJECT_DIR_NAME") or ""
    if env.get("CLAUDE_CONFIG_DIR") and CLAUDE_DIR_NAME_RE.match(pinned):
        return [pinned]
    paths = [os.path.abspath(workdir)] if IS_WINDOWS else [os.path.realpath(workdir), os.path.abspath(workdir)]
    names: list[str] = []
    for path in paths:
        name = claude_project_dirname(path)
        if name and name not in names:
            names.append(name)
    return names


def claude_project_dirname(path: str) -> str | None:
    """Claude Code's project folder name for a full path ("/home/me/myapp" gives "-home-me-myapp"), or None for a
    path with other than plain ASCII characters or a name over 200 characters (not documented)."""
    if not all(ord(c) < 128 for c in path):
        return None
    name = re.sub(r"[^A-Za-z0-9]", "-", path)
    return name if len(name) <= CLAUDE_PROJECT_NAME_MAX else None


def _plain_file(path: Path) -> bool:
    """A file of its own (not a link, a folder or a device); False when there is nothing there."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _inside(path: Path, folder: Path) -> bool:
    """Is path inside folder, with every link resolved?"""
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(folder))
        return True
    except ValueError:
        return False


def find_session_transcript(config_dir: Path, workdir: str, session_id: str, env: dict) -> Path:
    """The transcript of this session, started in workdir: an exact path tried for each name the project folder may
    have (claude_project_dirnames). Raises SessionMoveError."""
    if not SESSION_UUID_RE.match(session_id or ""):
        raise SessionMoveError("this session's id isn't a Claude Code session id")
    names = claude_project_dirnames(workdir, env)
    if not names:
        raise SessionMoveError(f"the project folder {workdir} has a path Claude Code names in a way that isn't "
                               "documented (non-ASCII characters, or very long)")
    projects = config_dir / "projects"
    for name in names:
        path = projects / name / f"{session_id}.jsonl"
        if os.path.lexists(path):
            if not _plain_file(path) or not _inside(path, projects):
                raise SessionMoveError(f"{path} isn't a plain file, so it isn't read")
            return path
    raise SessionMoveError(f"no transcript of session {session_id} in {projects / names[0]}")


def read_session_transcript(path: Path, max_bytes: int) -> bytes:
    """The transcript's bytes, as they are (never masked: that would change the session). Raises SessionMoveError
    when it is larger than max_bytes."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags)
    except OSError as err:
        raise SessionMoveError(f"couldn't open the transcript ({err.strerror or err})")
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise SessionMoveError("the transcript isn't a plain file")
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise SessionMoveError(f"the session is larger than {max_bytes // (1024 * 1024)} MB, the most that can move")
    return data


def install_session_transcript(config_dir: Path, workdir: str, session_id: str, data: bytes, env: dict) -> Path:
    """Write a moved session's transcript where this machine's agent looks for it first: its project folder for
    workdir. A transcript already there under that id (the session moved here before) is set aside next to it as
    <id>.jsonl.superseded-<time>, as Claude Code does itself, not overwritten. Raises SessionMoveError."""
    if not SESSION_UUID_RE.match(session_id or ""):
        raise SessionMoveError("this session's id isn't a Claude Code session id")
    names = claude_project_dirnames(workdir, env)
    if not names:
        raise SessionMoveError(f"the project folder {workdir} has a path Claude Code names in a way that isn't "
                               "documented (non-ASCII characters, or very long)")
    projects = config_dir / "projects"
    folder = projects / names[0]
    try:
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as err:
        raise SessionMoveError(f"couldn't make the folder {folder} ({err.strerror or err})")
    if os.path.islink(folder) or not _inside(folder, config_dir):
        raise SessionMoveError(f"{folder} is a link, so nothing is written there")
    path = folder / f"{session_id}.jsonl"
    try:
        if os.path.lexists(path):
            if not _plain_file(path):
                raise SessionMoveError(f"{path} isn't a plain file, so it isn't replaced")
            os.replace(path, path.with_name(f"{path.name}.superseded-{int(time.time() * 1000)}"))
        write_private_bytes(path, data)
    except OSError as err:
        raise SessionMoveError(f"couldn't write {path} ({err.strerror or err})")
    return path


def cli_command(spec: dict, env: dict) -> str:
    """How to start this agent's CLI in a terminal, for messages such as '<command> auth login'
    and the "continue in a terminal" command."""
    preset = spec.get("preset")
    if preset == "claude":
        cfg_dir = env.get("CLAUDE_CONFIG_DIR") or ""
        # An agent whose command is a wrapper of its own on PATH (runner.json "command": "claude-work") is
        # started by that name, which sets up its login itself.
        own = re.sub(r"(?i)\.(exe|cmd|bat|ps1)$", "", str(spec.get("command") or ""))
        if own != "claude" and COMMAND_NAME_RE.fullmatch(own):
            return own
        if not cfg_dir:
            return "claude"
        name = spec.get("name") or ""
        if COMMAND_NAME_RE.match(name) and shutil.which(name):
            return name  # the command the setup script made for this login
        if IS_WINDOWS:
            return "$env:CLAUDE_CONFIG_DIR='" + cfg_dir.replace("'", "''") + "'; claude"
        return "CLAUDE_CONFIG_DIR=" + shlex.quote(cfg_dir) + " claude"
    if preset in ("gemini", "selftest"):
        return preset
    return Path(str(spec.get("command") or spec.get("name") or "")).name or "the agent's CLI"


def agent_terminal(spec: dict, env: dict) -> str:
    """The command that continues one of this agent's sessions in a terminal, with {session_id};
    "" when there is no safe one. A "terminal" key in runner.json (or the preset) wins."""
    if "terminal" in spec:
        value = spec.get("terminal")
        return value.strip() if isinstance(value, str) else ""
    preset = spec.get("preset")
    if preset == "claude":
        if claude_auth(env) != "login":
            return ""  # the key or token is in runner.json, not in the user's terminal
        return cli_command(spec, env) + " --resume {session_id}"
    if preset == "gemini":
        # A key of the agent's own is in runner.json only: a terminal would use another one.
        return "" if (spec.get("env") or {}).get("GEMINI_API_KEY") else "gemini --resume {session_id}"
    return ""


def clean_tags(raw: Any) -> tuple[list[str], list[str]]:
    """runner.json "tags" (a list, or one string with commas or spaces) as (tags, skipped):
    lowercase words matching TAG_RE, without repeats, in the order given, at most MAX_OWN_TAGS
    besides platform names (which take no place: they are left out later)."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, (list, tuple)):
        return [], []
    tags: list[str] = []
    skipped: list[str] = []
    for item in raw:
        word = item.strip().lower() if isinstance(item, str) else ""
        if isinstance(item, str) and not word:
            continue  # the space between two separators
        if word in tags:
            continue
        own = sum(1 for t in tags if t not in PLATFORM_TAGS)
        if TAG_RE.match(word) and (word in PLATFORM_TAGS or own < MAX_OWN_TAGS):
            tags.append(word)
        else:
            skipped.append(item.strip() if isinstance(item, str) else str(item))
    return tags, skipped


def heartbeat_tags(cfg: dict) -> list[str]:
    """The tags this machine reports: its own words (another platform's name is left out) and
    its platform tag, at most MAX_TAGS in all."""
    own = [t for t in clean_tags(cfg.get("tags", []))[0] if t not in PLATFORM_TAGS]
    return sorted(set(own[:MAX_OWN_TAGS] + [PLATFORM]))


def _shown(value: Any, limit: int = 40) -> str:
    """A runner.json key or name for a message: short, printable, with anything token-shaped masked."""
    if not isinstance(value, str):
        return "(not a name)"
    text = redact(value)
    if not text.isprintable():
        text = text.encode("unicode_escape").decode("ascii")
    return text if len(text) <= limit else text[:limit] + "..."


def git_kind_of(host: str) -> str:
    """The kind of a git account whose entry gives none, from its host name (a port in the host is for ssh):
    github for github.com, gitlab for gitlab.com, other for any other host, a self-hosted one included."""
    return {"github.com": "github", "gitlab.com": "gitlab"}.get(host.partition(":")[0].lower(), "other")


def check_git_account(name: str, entry: Any) -> tuple[dict | None, str]:
    """One runner.json git account as (account, ""), normalised to {"host" (lowercase), "kind", and any of
    "name", "email", "ssh_key" (expanded), "token", "user" that are set}; (None, why) when it can't be used.
    The reason never holds a value from the entry: no host, path or token."""
    if not isinstance(name, str) or not GIT_ACCOUNT_RE.fullmatch(name):
        return None, "bad name: use 1 to 32 lowercase letters, digits or dashes, starting with a letter or digit"
    if not isinstance(entry, dict):
        return None, "not an account: give it a host, and an ssh_key, a token, or a commit name and email"
    unknown = [key for key in entry if key not in GIT_ACCOUNT_FIELDS]
    if unknown:
        return None, f"unknown field '{_shown(unknown[0])}' (the fields are {', '.join(GIT_ACCOUNT_FIELDS)})"
    host = entry.get("host")
    if not isinstance(host, str) or not GIT_HOST_RE.fullmatch(host.strip()):
        return None, "bad host: give a host name such as github.com, without https://, a path or user@"
    host = host.strip().lower()
    kind = entry.get("kind")
    if kind is None:
        kind = git_kind_of(host)
    elif kind not in GIT_KINDS:
        return None, f"unknown kind: use {', '.join(GIT_KINDS[:-1])} or {GIT_KINDS[-1]}"
    account = {"host": host, "kind": kind}
    for field in ("name", "email", "ssh_key", "token", "user"):
        value = entry.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            return None, f"{field} must be text"
        value = value.strip()
        if not value:
            continue
        if field == "ssh_key":
            value = expand_path(value)
            if '"' in value or GIT_CONTROL_RE.search(value):
                return None, "ssh_key can't hold a double quote, a line break or another control character"
        elif field in ("user", "token") and GIT_HELPER_UNSAFE_RE.search(value):
            return None, f"{field} can't hold a double quote, a backslash, a line break or another control character"
        if field == "token" and len(value) < GIT_TOKEN_MIN:
            return None, f"token is too short: a git token has at least {GIT_TOKEN_MIN} characters"
        account[field] = value
    if not any(field in account for field in ("ssh_key", "token", "name", "email")):
        return None, "nothing to use: give it an ssh_key, a token, or a commit name and email"
    return account, ""


def git_accounts(cfg: dict) -> tuple[dict[str, dict], list[str]]:
    """runner.json "git_accounts" as (the valid accounts by name, problems as "<name>: <reason>"). An
    account with a problem is left out: a job can't pick it, and no default can name it. A key file that isn't
    there is only reported: the account stays, and a job that uses it is refused when it starts
    (git_account_env checks the file again then)."""
    raw = cfg.get("git_accounts")
    if raw is None:
        return {}, []
    if not isinstance(raw, dict):
        return {}, ["git_accounts: must be an object with one entry per account name"]
    accounts: dict[str, dict] = {}
    problems: list[str] = []
    for name, entry in raw.items():
        account, why = check_git_account(name, entry)
        if account is None:
            problems.append(f"{_shown(name)}: {why}")
            continue
        accounts[name] = account
        if "ssh_key" in account and not os.path.isfile(account["ssh_key"]):
            problems.append(f"{_shown(name)}: its ssh key file is missing")
    return accounts, problems


def _git_default_problem(value: Any, accounts: dict) -> str:
    """Why a default (default_git_account, or a value in project_git_accounts) can't be applied; "" if it can."""
    if isinstance(value, str) and value in accounts:
        return ""
    if isinstance(value, str) and GIT_ACCOUNT_RE.fullmatch(value):
        return f"names '{value}', which is not a valid git account"
    return "is not the name of a git account"


def git_default_problems(cfg: dict, accounts: dict) -> list[str]:
    """The defaults in runner.json that name no valid git account (each refuses the jobs it would apply to).
    A project_git_accounts entry for a project this machine doesn't have yet is no problem: it applies when that
    project is cloned ('runner.py list' says so)."""
    problems = []
    machine = cfg.get("default_git_account")
    if machine not in (None, ""):
        why = _git_default_problem(machine, accounts)
        if why:
            problems.append(f"default_git_account: {why}")
    projects = cfg.get("project_git_accounts")
    if projects is not None and not isinstance(projects, dict):
        problems.append("project_git_accounts: must be an object of project names and git account names")
    elif projects:
        for project, value in projects.items():
            why = _git_default_problem(value, accounts)
            if why:
                problems.append(f"project_git_accounts ({_shown(project)}): {why}")
    return problems


def choose_git_account(cfg: dict, spec: dict | None, project: str, choice: str | None,
                       subtask: bool = False) -> tuple[str | None, str, str]:
    """The git account a job, clone or pull uses, as (name or None, error, note):
    - a subtask: none, always, default included (it uses the machine user's own git setup);
    - choice "-": none; a name: that account, which must be valid here;
    - choice None: the project's default (project_git_accounts), else the machine's (default_git_account),
      else none. A default is skipped, with a note for the job log, when the agent's own env (spec) sets any
      of GIT_ENV_VARS: that agent keeps its own git setup, so a default it never uses can't refuse its jobs.
      Otherwise a default that names no valid account is an error: the job doesn't start. spec is None for a
      clone or pull."""
    if subtask or choice == "-":
        return None, "", ""
    accounts = git_accounts(cfg)[0]
    machine = cfg.get("name") or socket.gethostname()
    if choice is not None:
        if isinstance(choice, str) and choice in accounts:
            return choice, "", ""
        return None, f"there is no valid git account '{_shown(str(choice))}' on {machine}", ""
    projects = cfg.get("project_git_accounts")
    unknown = projects is not None and not isinstance(projects, dict)
    if unknown:
        where, default = "project_git_accounts", None
    elif projects and project in projects:
        where, default = f"project_git_accounts for '{_shown(project)}'", projects[project]
    else:
        where, default = "default_git_account", cfg.get("default_git_account")
        if default in (None, ""):
            return None, "", ""
    own = [var for var in GIT_ENV_VARS if var in ((spec or {}).get("env") or {})]
    if own:
        named = f" '{_shown(default)}'" if isinstance(default, str) and default else ""
        return None, "", (f"The default git account{named} ({where}) was skipped: this agent's own env sets "
                          f"{', '.join(own)}, so it keeps its own git setup.")
    if unknown:
        return None, (f"project_git_accounts in runner.json on {machine} isn't an object of project and git "
                      "account names, so the default git account isn't known. Fix runner.json, or pick a git "
                      "account or None for the job."), ""
    why = _git_default_problem(default, accounts)
    if why:
        return None, (f"the default git account ({where}) on {machine} {why}. Fix runner.json, or pick a git "
                      "account or None for the job."), ""
    return default, "", ""


def git_account_info(cfg: dict) -> dict:
    """What the heartbeat says about git accounts: names, hosts and kinds; the defaults as set (names only);
    and the problems, broken defaults included, and a git too old for any account (the version git_version
    keeps, from the runner's PATH). The keys are always there, so the hub can tell an older runner from one
    without accounts."""
    accounts, problems = git_accounts(cfg)
    machine = cfg.get("default_git_account")
    projects = cfg.get("project_git_accounts")

    def named(value: Any) -> bool:
        return isinstance(value, str) and GIT_ACCOUNT_RE.fullmatch(value) is not None

    version = git_version() if accounts else None
    old = ["git on this machine is older than 2.31, so git accounts can't be used"] \
        if version is not None and version < GIT_MIN_VERSION else []
    return {
        "git_accounts": [{"name": n, "host": a["host"], "kind": a["kind"]} for n, a in sorted(accounts.items())],
        "default_git_account": machine if named(machine) else None,
        "project_git_accounts": {p: v for p, v in projects.items() if named(v)} if isinstance(projects, dict) else {},
        "git_account_problems": problems + git_default_problems(cfg, accounts) + old,
    }


def git_credentials(account: dict) -> str:
    """Which credentials a git account has, in words ("ssh key", "token"), never their values."""
    return ", ".join(word for field, word in (("ssh_key", "ssh key"), ("token", "token"))
                     if account.get(field)) or "no ssh key or token"


def agent_status(cfg: dict, name: str, sign_in: dict | None = None) -> dict:
    """What the dashboard shows about an agent: is it installed, does it look logged in? sign_in: what
    its CLI last said about its sign-in (SignInStore.view), when the runner has checked; a Claude agent
    the CLI calls signed out isn't ready. Without an answer (none yet, or a CLI too old to say), the
    login file is looked for instead."""
    spec = agent_spec(cfg, name)
    info: dict[str, Any] = {"name": name, "label": spec.get("label", name), "preset": spec.get("preset", "text"),
                            "ready": True, "note": "", "models": list(spec.get("models") or []),
                            "default_model": spec.get("model_name") or ""}
    env = build_env(spec)
    account = cfg["agents"][name].get("account")
    if isinstance(account, str) and ACCOUNT_RE.match(account.strip()):
        info["account"] = account.strip()  # only when set: no label is its own account on the hub
    info.update(auth=agent_auth(spec, env), features=agent_features(spec, env),
                terminal=agent_terminal(spec, env))
    if spec.get("permission_flags"):
        info["permission_flags"] = list(spec["permission_flags"])  # why it takes no subtasks and can't ask
    if resolve_command(spec) is None:
        info.update(ready=False, note=f"'{spec.get('command')}' not found on this machine")
        return info
    state = sign_in.get("state") if isinstance(sign_in, dict) else None
    if state in SIGN_IN_STATES and sign_in_applies(spec, env):
        # The login command is shown on the machine page, to run on this machine itself.
        info["sign_in"] = {**sign_in, "command": cli_command(spec, env) + " auth login"}
    else:
        state = None
    if spec.get("preset") == "claude":
        cfg_dir = Path(env.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")
        if env.get("ANTHROPIC_API_KEY"):
            info["note"] = "uses an API key (billed per token)"
        elif env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            info["note"] = "uses a long-lived subscription token"
        elif state == "in":
            pass  # the CLI says so itself (on a Mac too, where the login is in the Keychain)
        elif state == "out":
            info.update(ready=False, note=f"signed out; run '{cli_command(spec, env)} auth login' on this machine")
        elif (cfg_dir / ".credentials.json").exists():
            pass
        elif IS_MACOS:
            # On macOS Claude Code keeps the login in the Keychain, which the runner doesn't read.
            info["note"] = "login is in the macOS Keychain (not checked here; 'runner.py test' checks it)"
        else:
            info.update(ready=False, note=f"no login found in {cfg_dir}; run its login once")
    elif spec.get("preset") == "gemini":
        key = gemini_key(env)
        if key:
            info["note"] = "uses a Gemini API key"
            info["models"] = list(dict.fromkeys(info["models"] + gemini_models(key)))
        else:
            info.update(ready=False, note=f"no Gemini API key (set one in {GEMINI_ENV})")
    return info


# ============================================================ sign-in status
# The machine page shows whether each Claude agent is signed in, from the CLI's own
# "claude auth status --json", run with the agent's command and environment. It costs no usage.
# The runner keeps only the fields in parse_auth_status (never the account's email, organization
# or folders), and never reads a login's files or the macOS Keychain. Signing in is never done
# from here: it happens on the machine itself, through Anthropic's own flow.
# Claude Code's changelog (https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md, last
# checked 2026-11-17): "claude auth status" and "claude auth logout" came in 2.1.41. Before 2.1.317,
# "auth status" could exit in the middle of a token refresh and sign the login out (fixed in 2.1.301
# and 2.1.317), so the runner asks only 2.1.317 and newer; older ones show "Unknown (CLI too old)".
AUTH_STATUS_SINCE = (2, 1, 317)
AUTH_LOGOUT_SINCE = (2, 1, 41)
SIGN_IN_EVERY = 600          # seconds between checks of a signed-in agent
SIGN_IN_RETRY = 60           # ... of one that is signed out, or couldn't be checked
SIGN_IN_TIMEOUT = 120        # generous: cutting "auth status" off during a token refresh is what harmed logins
SIGN_IN_STATES = ("in", "out", "unknown")
SIGN_IN_WORD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,39}$")
# The fields kept from "auth status --json", and the names they go by in the check-in.
SIGN_IN_FIELDS = (("authMethod", "method"), ("apiProvider", "provider"), ("subscriptionType", "plan"),
                  ("planTier", "tier"))
CLI_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_auth_status(text: str) -> dict | None:
    """What "auth status --json" printed, as {"state": "in" or "out", and any of "method", "provider",
    "plan", "tier" (short plain words) and "expires_at" (epoch seconds)}; None when it isn't that JSON
    (an older CLI, an error message). Nothing else is kept: no email, organization or folder."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")  # a line printed before or after the JSON
        try:
            data = json.loads(text[start:end + 1]) if 0 <= start < end else None
        except ValueError:
            return None
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        return None
    out: dict[str, Any] = {"state": "in" if data["loggedIn"] else "out"}
    for field, key in SIGN_IN_FIELDS:
        value = data.get(field)
        if isinstance(value, str) and SIGN_IN_WORD_RE.match(value):
            out[key] = value
    expires = data.get("loginExpiresAt")
    when = _iso_epoch(expires) if isinstance(expires, str) else None
    if when is not None:
        out["expires_at"] = when
    return out


def cli_run(argv: list[str], env: dict, timeout: float) -> tuple[int, str, str] | None:
    """Run one short CLI command (no input, no window on Windows); (exit code, stdout, stderr) or None."""
    try:
        done = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL,
                              creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired, ValueError) as err:
        log.warning("%s failed: %s", redact_argv(argv[:4]), err)
        return None
    return done.returncode, done.stdout or "", done.stderr or ""


def cli_version(spec: dict, env: dict) -> tuple[int, int, int] | None:
    """The agent's CLI version from its cli_version arguments, run with its environment (asked each time:
    a check runs every few minutes at most, and the CLI may have updated itself); None when it has none or
    doesn't say."""
    exe, args = resolve_command(spec), spec.get("cli_version")
    if exe is None or not isinstance(args, list) or not args:
        return None
    done = cli_run([exe, *[str(a) for a in args]], env, 60)
    m = CLI_VERSION_RE.search(done[1] + "\n" + done[2]) if done is not None and done[0] == 0 else None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def sign_in_applies(spec: dict, env: dict) -> bool:
    """Does the machine page show this agent's sign-in: a Claude agent with a full login (not an API key,
    a token or another service) whose CLI can say (auth_status)?"""
    return spec.get("parser") == "claude-stream" and claude_auth(env) == "login" \
        and isinstance(spec.get("auth_status"), list) and bool(spec["auth_status"])


def check_sign_in(spec: dict, env: dict) -> dict:
    """Ask the agent's CLI whether it is signed in: parse_auth_status's answer, or {"state": "unknown",
    "why": "old"} for a CLI too old to ask, "error" when it didn't answer."""
    exe = resolve_command(spec)
    if exe is None:
        return {"state": "unknown", "why": "error"}
    version = cli_version(spec, env)
    if version is None:
        return {"state": "unknown", "why": "error"}
    if version < AUTH_STATUS_SINCE:
        return {"state": "unknown", "why": "old"}
    done = cli_run([exe, *[str(a) for a in spec["auth_status"]]], env, SIGN_IN_TIMEOUT)
    # Signed out, it may exit non-zero and still say so: the answer is read whatever the exit code. Its
    # output first, so a notice on stderr can't spoil the JSON.
    parsed = (parse_auth_status(done[1]) or parse_auth_status(done[1] + "\n" + done[2])) if done is not None else None
    return parsed or {"state": "unknown", "why": "error"}


class SignInStore:
    """Each Claude agent's latest sign-in status, checked in the background: when the runner starts, then
    every SIGN_IN_EVERY seconds (SIGN_IN_RETRY while it is signed out or couldn't be checked), never while
    one of the agent's jobs runs, and right after a sign-out. In memory only."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.changed = threading.Event()  # a status changed; the runner checks in soon after
        self.entries: dict[str, dict] = {}  # agent: {"key", "at", "view"}
        self.busy: set[str] = set()
        # One per agent, held while its CLI is asked or signed out: a status check during "auth logout" could
        # save a refreshed login after the logout, or write "signed in" over the answer that follows it.
        self.agent_locks: dict[str, threading.Lock] = {}

    def agent_lock(self, agent: str) -> threading.Lock:
        with self.lock:
            return self.agent_locks.setdefault(agent, threading.Lock())

    @staticmethod
    def key(spec: dict, env: dict) -> tuple:
        """What the answer depends on: the program, its arguments and its settings folder."""
        return (resolve_command(spec), tuple(str(a) for a in spec.get("auth_status") or []),
                env.get("CLAUDE_CONFIG_DIR") or "")

    def view(self, agent: str, key: tuple) -> dict | None:
        with self.lock:
            entry = self.entries.get(agent)
            return dict(entry["view"]) if entry and entry["key"] == key else None

    def due(self, agent: str, key: tuple) -> bool:
        with self.lock:
            if agent in self.busy:
                return False
            entry = self.entries.get(agent)
            if not entry or entry["key"] != key:
                return True
            wait = SIGN_IN_EVERY if entry["view"].get("state") == "in" else SIGN_IN_RETRY
            return self.clock() - entry["at"] >= wait

    def refresh(self, agent: str, spec: dict, env: dict) -> dict:
        """Check now (in this thread, after any check or sign-out of this agent already running) and keep the
        answer."""
        with self.agent_lock(agent):
            return self.refresh_held(agent, spec, env)

    def refresh_held(self, agent: str, spec: dict, env: dict) -> dict:
        """refresh(), for a caller that holds agent_lock(agent) already."""
        key = self.key(spec, env)
        with self.lock:
            self.busy.add(agent)
        try:
            view = check_sign_in(spec, env)
            view["checked_at"] = self.clock()
        finally:
            with self.lock:
                self.busy.discard(agent)
        with self.lock:
            before = self.entries.get(agent)
            if not before or {k: v for k, v in before["view"].items() if k != "checked_at"} \
                    != {k: v for k, v in view.items() if k != "checked_at"}:
                self.changed.set()
            self.entries[agent] = {"key": key, "at": self.clock(), "view": view}
        return view

    def refresh_due(self, cfg: dict, running: set) -> None:
        """Start a background check for each agent that is due and has no job running."""
        for name in list(cfg.get("agents") or {}):
            try:
                spec = agent_spec(cfg, name)
            except (KeyError, TypeError, AttributeError):
                continue
            env = build_env(spec)
            if name in running or not sign_in_applies(spec, env) or not self.due(name, self.key(spec, env)):
                continue
            with self.lock:
                self.busy.add(name)  # so the next pass doesn't start a second one
            threading.Thread(target=self._refresh_quietly, args=(name, spec, env), daemon=True).start()

    def _refresh_quietly(self, agent: str, spec: dict, env: dict) -> None:
        lock = self.agent_lock(agent)
        if not lock.acquire(blocking=False):  # a sign-out (or another check) is running: it checks after itself
            with self.lock:
                self.busy.discard(agent)
            return
        try:
            self.refresh_held(agent, spec, env)
        except Exception:  # never let a check take the runner down
            log.exception("sign-in check for %s failed", agent)
            with self.lock:
                self.busy.discard(agent)
        finally:
            lock.release()


def sign_out(spec: dict, env: dict) -> tuple[bool, str]:
    """Run the agent's own "auth logout" (its command and environment): (worked, its output, masked)."""
    exe = resolve_command(spec)
    args = spec.get("auth_logout")
    if exe is None or not isinstance(args, list) or not args:
        return False, "This agent can't be signed out from the dashboard."
    version = cli_version(spec, env)
    if version is None:
        return False, "Couldn't tell which version of the CLI this is, so it wasn't asked to sign out."
    if version < AUTH_LOGOUT_SINCE:
        return False, ("This version of Claude Code has no 'auth logout'. Update Claude Code on this machine, "
                       "or run /logout in it there.")
    done = cli_run([exe, *[str(a) for a in args]], env, 120)
    if done is None:
        return False, "The sign-out command didn't finish (see the runner log)."
    text = RC_EMAIL_RE.sub("…@…", redact((done[1] + "\n" + done[2]).strip()))[-1500:]
    return done[0] == 0, text


# ================================================================ hub client
def cf_access_headers(cfg: dict) -> dict[str, str]:
    """The optional Cloudflare Access service token, if the hub URL sits behind Access."""
    if not cfg.get("cf_access_client_id"):
        return {}
    return {"CF-Access-Client-Id": cfg["cf_access_client_id"],
            "CF-Access-Client-Secret": cfg.get("cf_access_client_secret", "")}


class Hub:
    def __init__(self, get_cfg: Callable[[], dict]):
        self.get_cfg = get_cfg

    def call(self, path: str, payload: dict, timeout: float = 30) -> tuple[int, dict | None]:
        cfg = self.get_cfg()
        url = cfg["hub_url"].rstrip("/") + path
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {cfg['token']}",
                   "User-Agent": f"bullpen-runner/{VERSION}", **cf_access_headers(cfg)}
        return post_json(url, payload, headers, timeout)

    def call_bytes(self, path: str, data: bytes, content_type: str = "application/octet-stream",
                   headers: dict | None = None, timeout: float = 120) -> tuple[int, bytes, dict]:
        """A POST whose body, or answer, is raw bytes (a moved session's transcript): (status, body, headers). An
        error's body is FastAPI's message, as text."""
        cfg = self.get_cfg()
        url = cfg["hub_url"].rstrip("/") + path
        sent = {"Content-Type": content_type, "Authorization": f"Bearer {cfg['token']}",
                "User-Agent": f"bullpen-runner/{VERSION}", **cf_access_headers(cfg), **(headers or {})}
        req = urllib.request.Request(url, data=data, headers=sent, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as err:
            raw = err.read()[:2000].decode(errors="replace")
            try:
                detail = json.loads(raw).get("detail", raw)
            except (ValueError, AttributeError):
                detail = raw
            return err.code, str(detail)[:500].encode(), {}


def post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = 30) -> tuple[int, dict | None]:
    headers = {"Content-Type": "application/json", "User-Agent": f"bullpen-runner/{VERSION}", **(headers or {})}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as err:
        raw = err.read()[:2000].decode(errors="replace")
        try:  # FastAPI errors are {"detail": "..."}: pass the message on, not the JSON around it
            detail = json.loads(raw).get("detail", raw)
        except (ValueError, AttributeError):
            detail = raw
        return err.code, {"detail": str(detail)[:500]}


# ===================================================== plan limits and token counts
# Claude Code tells every job how much of the account's plan limits is used (a "rate_limit_event"),
# and "Check limits" on the dashboard runs its /usage command. The runner keeps the newest figures
# for each agent in limits.json and sends them with its check-ins; the hub only shows them. Nothing
# here reads a login's files or calls Anthropic itself.
LIMITS_FILE = BASE_DIR / "limits.json"
LIMIT_STATUSES = {"allowed", "allowed_warning", "rejected"}
WINDOW_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
LIMIT_FIELDS = ("used", "status", "resets_at", "threshold", "resets_text")  # each merged on its own
LIMITS_MAX_AGE = 8 * 86400   # a window not seen for this long is forgotten
MAX_WINDOWS = 16             # per agent, so a check-in stays small
DISABLED_REASON_RE = re.compile(r"^[a-z_]{1,60}$")
# What the dashboard calls each window (hub/limits.py has the same table).
WINDOW_LABELS = {"five_hour": "5-hour session", "seven_day": "Week (all models)", "seven_day_sonnet": "Week (Sonnet)",
                 "seven_day_opus": "Week (Opus)", "seven_day_overage_included": "Week (Fable)",
                 "overage": "Extra usage", "current": "Current limit"}
WINDOW_SHORT = {"five_hour": "5-hour session", "seven_day": "week", "seven_day_sonnet": "Sonnet",
                "seven_day_opus": "Opus", "seven_day_overage_included": "Fable", "current": "current limit"}
# ISO 8601 as Python 3.9's fromisoformat can't always read it ("Z", 7 fraction digits, "+0200").
ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?)?"
                    r"\s*([Zz]|[+-]\d{2}(?::?\d{2})?)?$")


def _num(v: Any) -> float | None:
    """A number from an int, float or numeric string; None for anything else (True/False too)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        try:
            n = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return n if math.isfinite(n) else None


def _iso_epoch(text: str) -> float | None:
    m = ISO_RE.match(text.strip())
    if not m:
        return None
    year, month, day, hour, minute, second, frac, zone = m.groups()
    stamp = f"{year}-{month}-{day}T{hour or '00'}:{minute or '00'}:{second or '00'}"
    if frac:
        stamp += "." + (frac + "000000")[:6]
    if zone and zone not in ("Z", "z"):
        digits = zone[1:].replace(":", "")
        stamp += zone[0] + digits[:2] + ":" + (digits[2:4] or "00")
    elif zone:
        stamp += "+00:00"
    try:
        when = datetime.datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)  # a time without a zone is read as UTC
    return when.timestamp()


def limit_epoch(v: Any) -> float | None:
    """A reset time as epoch seconds: from seconds, milliseconds, a digit string or ISO 8601."""
    if isinstance(v, str) and ISO_RE.match(v.strip()):
        return _iso_epoch(v)
    n = _num(v)
    if n is None:
        return None
    if n > 1e12:
        return n / 1000
    return n if n > 1e8 else None


def limit_fraction(v: Any) -> float | None:
    """How much of a window is used, as a fraction (1 = the whole limit). Claude Code sends a
    fraction, which may run a little past 1; a value over 2 can only be a 0-100 percentage."""
    n = _num(v)
    if n is None:
        return None
    if n > 2:
        n = n / 100
    return min(max(n, 0.0), 5.0)


def _window_id(name: Any) -> str | None:
    if not isinstance(name, str):
        return None
    wid = name.strip().lower()
    return wid if WINDOW_ID_RE.match(wid) else None


def window_label(wid: str, lower: bool = False) -> str:
    """The dashboard's name for a window: '5-hour session', 'Week (Sonnet)', ..."""
    if wid in WINDOW_LABELS:
        label = WINDOW_LABELS[wid]
    elif wid.startswith("seven_day_"):
        label = "Week (" + wid[len("seven_day_"):].replace("_", " ").title() + ")"
    else:
        label = wid.replace("_", " ")
    return label[:1].lower() + label[1:] if lower and label[:1].isupper() else label


def window_order(wid: str) -> tuple:
    rank = {"five_hour": 0, "seven_day": 1, "current": 3, "overage": 4}
    return (rank.get(wid, 2 if wid.startswith("seven_day_") else 5), wid)


def format_utc(ts: Any) -> str:
    """'2026-11-07 15:00' (UTC) for a reset time, or '' when there is none."""
    n = _num(ts)
    if n is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.gmtime(n))
    except (OverflowError, OSError, ValueError):
        return ""


def _limit_observation(d: dict, now: float, source: str) -> dict:
    status = d.get("status")
    return {"used": limit_fraction(d.get("utilization")),
            "status": status if status in LIMIT_STATUSES else None,
            "resets_at": limit_epoch(d.get("resetsAt")),
            "threshold": limit_fraction(d.get("surpassedThreshold")),
            "resets_text": None, "observed_at": now, "source": source}


def normalize_rate_limit(info: dict, now: float) -> tuple[dict, dict | None, str | None]:
    """Claude Code's rate_limit_info as (windows, overage, top-level status). Windows are keyed by
    their id ("five_hour", "seven_day", ...; "current" when Claude Code doesn't name the window)."""
    if not isinstance(info, dict):
        return {}, None, None
    windows: dict[str, dict] = {}
    unified = info.get("unifiedWindows")
    if isinstance(unified, dict):
        for name, d in unified.items():
            wid = _window_id(name)
            if wid and isinstance(d, dict) and wid not in windows:
                windows[wid] = _limit_observation(d, now, "job")
    main_id = _window_id(info.get("rateLimitType")) or "current"
    main = _limit_observation(info, now, "job")
    if main_id in windows:
        # The window's own figures win; the top-level ones fill what it leaves out.
        for field in ("used", "resets_at", "threshold", "status"):
            if windows[main_id][field] is None:
                windows[main_id][field] = main[field]
    else:
        windows[main_id] = main
    windows = {wid: obs for wid, obs in windows.items()
               if obs["used"] is not None or obs["status"] is not None or obs["resets_at"] is not None}
    overage = None
    if any(k in info for k in ("isUsingOverage", "overageStatus", "overageResetsAt", "overageDisabledReason")):
        status = info.get("overageStatus")
        reason = info.get("overageDisabledReason")
        reason = reason.strip().lower() if isinstance(reason, str) else ""
        overage = {"using": info.get("isUsingOverage") is True,
                   "status": status if status in LIMIT_STATUSES else None,
                   "resets_at": limit_epoch(info.get("overageResetsAt")),
                   "disabled_reason": reason if DISABLED_REASON_RE.match(reason) else None,
                   "observed_at": now}
    top = info.get("status")
    return windows, overage, (top if top in LIMIT_STATUSES else None)


def merge_window(current: dict | None, obs: dict, now: float) -> dict:
    """One window's figures with a newer observation merged in field by field: a newer value
    replaces an older one, and a field the observation leaves out (None) keeps its value. Each
    field keeps its own time (<field>_at), so an event that only carries a status never blanks
    the number on a bar."""
    out = dict(current or {})
    at = _num(obs.get("observed_at")) or now
    for field in LIMIT_FIELDS:
        value = obs.get(field)
        if value is None:
            continue
        if (_num(out.get(field + "_at")) or 0) <= at:
            out[field] = value
            out[field + "_at"] = at
            if field == "used":
                out["source"] = obs.get("source") or out.get("source")
    for field in LIMIT_FIELDS:
        out.setdefault(field, None)
        out.setdefault(field + "_at", None)
    stamps = [out[f + "_at"] for f in LIMIT_FIELDS if _num(out.get(f + "_at")) is not None]
    out["observed_at"] = max(stamps) if stamps else at
    if not out.get("source"):
        out["source"] = obs.get("source")
    return out


def _plain(value: Any) -> Any:
    """JSON-safe copy of what limits.json holds: dicts of numbers, strings, booleans and None."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items() if isinstance(k, str)}
    if isinstance(value, (str, bool)) or value is None:
        return value
    n = _num(value)
    return n


class LimitStore:
    """The newest plan-limit figures for each agent on this machine. path=None keeps them in
    memory only (runner.py test, the unit tests); otherwise they are kept in limits.json, so they
    survive restarts and updates."""

    def __init__(self, path: Path | None = LIMITS_FILE) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.changed = threading.Event()  # set by record(); the runner checks in soon after
        self.agents: dict[str, dict] = self._read(path) if path is not None else {}
        self._prune(time.time())

    @staticmethod
    def _read(path: Path) -> dict:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("agents"), dict):
            return {}
        agents = {}
        for agent, entry in data["agents"].items():
            if not isinstance(agent, str) or not isinstance(entry, dict):
                continue
            windows = entry.get("windows") if isinstance(entry.get("windows"), dict) else {}
            overage, status = entry.get("overage"), entry.get("status")
            agents[agent] = {
                "windows": {wid: _plain(w) for wid, w in windows.items()
                            if isinstance(wid, str) and WINDOW_ID_RE.match(wid) and isinstance(w, dict)},
                "overage": _plain(overage) if isinstance(overage, dict) else None,
                "status": _plain(status) if isinstance(status, dict) else None}
        return agents

    def record(self, agent: str, windows: dict, overage: dict | None, top_status: str | None = None,
               now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self.lock:
            before = json.dumps(self.agents.get(agent), sort_keys=True)
            entry = self.agents.setdefault(agent, {"windows": {}, "overage": None, "status": None})
            for wid, obs in (windows or {}).items():
                if isinstance(wid, str) and WINDOW_ID_RE.match(wid) and isinstance(obs, dict):
                    entry["windows"][wid] = merge_window(entry["windows"].get(wid), obs, now)
            if isinstance(overage, dict):
                entry["overage"] = {**overage, "observed_at": _num(overage.get("observed_at")) or now}
            if top_status in LIMIT_STATUSES:
                entry["status"] = {"value": top_status, "observed_at": now}
            self._prune(now)
            after = json.dumps(self.agents.get(agent), sort_keys=True)
            if after != before:
                self._save()
                self.changed.set()

    def _prune(self, now: float) -> None:
        old = now - LIMITS_MAX_AGE
        for agent in list(self.agents):
            entry = self.agents[agent]
            fresh = {wid: w for wid, w in entry["windows"].items() if (_num(w.get("observed_at")) or 0) >= old}
            keep = sorted(fresh, key=lambda wid: _num(fresh[wid].get("observed_at")) or 0, reverse=True)[:MAX_WINDOWS]
            entry["windows"] = {wid: fresh[wid] for wid in sorted(keep)}
            for key in ("overage", "status"):
                if isinstance(entry.get(key), dict) and (_num(entry[key].get("observed_at")) or 0) < old:
                    entry[key] = None
            if not entry["windows"] and entry.get("overage") is None and entry.get("status") is None:
                del self.agents[agent]

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            write_if_changed(self.path, json.dumps({"version": 1, "agents": self.agents}, indent=1, sort_keys=True)
                             + "\n", private=True)
        except OSError as err:
            log.warning("couldn't save %s: %s", self.path, err)

    def view(self, agent: str) -> dict:
        """{"windows": {...}, "overage": {...} or None, "status": {...} or None}, a copy."""
        with self.lock:
            entry = self.agents.get(agent) or {"windows": {}, "overage": None, "status": None}
            return json.loads(json.dumps(entry))


# ------------------------------------------------------------- tokens per job
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
MU_MAP = {"inputTokens": "input_tokens", "outputTokens": "output_tokens",
          "cacheReadInputTokens": "cache_read_tokens", "cacheCreationInputTokens": "cache_write_tokens"}
RUN_USAGE_MAP = {"input_tokens": "input_tokens", "output_tokens": "output_tokens",
                 "cache_read_input_tokens": "cache_read_tokens", "cache_creation_input_tokens": "cache_write_tokens"}
MAX_USAGE_MODELS = 20


def _tokens(v: Any) -> int:
    n = _num(v)
    return int(n) if n is not None and n > 0 else 0


def _round_cost(v: float | None) -> float | None:
    return round(v, 6) if v is not None else None


def usage_record(per_model: Any) -> dict | None:
    """Claude Code's per-model figures (modelUsage, model_usage_since_last_result) as one record:
    the four token counts, cost_usd (the sum of costUSD) and the same per model."""
    if not isinstance(per_model, dict):
        return None
    record: dict[str, Any] = {f: 0 for f in USAGE_FIELDS}
    cost = None
    models: dict[str, dict] = {}
    for name, m in per_model.items():
        if not isinstance(m, dict) or len(models) >= MAX_USAGE_MODELS:
            continue
        one: dict[str, Any] = {field: _tokens(m.get(key)) for key, field in MU_MAP.items()}
        one["cost_usd"] = _num(m.get("costUSD"))
        for f in USAGE_FIELDS:
            record[f] += one[f]
        if one["cost_usd"] is not None:
            cost = (cost or 0.0) + one["cost_usd"]
        models[str(name)[:100]] = one
    record["cost_usd"] = _round_cost(cost)
    record["models"] = models
    return record


def _usage_zero(record: dict | None) -> bool:
    return record is None or (all(not record.get(f) for f in USAGE_FIELDS) and not record.get("cost_usd"))


def add_usage(a: dict | None, b: dict) -> dict:
    """Two records added up, field by field and model by model."""
    if a is None:
        return json.loads(json.dumps(b))
    out: dict[str, Any] = {f: (a.get(f) or 0) + (b.get(f) or 0) for f in USAGE_FIELDS}
    costs = [c for c in (a.get("cost_usd"), b.get("cost_usd")) if c is not None]
    out["cost_usd"] = _round_cost(sum(costs)) if costs else None
    models = json.loads(json.dumps(a.get("models") or {}))
    for name, m in (b.get("models") or {}).items():
        if name in models:
            models[name] = add_usage(models[name], m)
            models[name].pop("models", None)
        elif len(models) < MAX_USAGE_MODELS:
            models[name] = dict(m)
    out["models"] = models
    return out


def _gemini_counts(d: dict) -> dict:
    """Gemini CLI's stats: its input_tokens include the cached ones; "input" is the rest."""
    cached = _tokens(d.get("cached"))
    if _num(d.get("input")) is not None:
        fresh = _tokens(d.get("input"))
    else:
        fresh = max(_tokens(d.get("input_tokens")) - cached, 0)
    return {"input_tokens": fresh, "output_tokens": _tokens(d.get("output_tokens")),
            "cache_read_tokens": cached, "cache_write_tokens": 0}


# From this Claude Code version on, modelUsage includes the totals of the session a run resumes (hub/limits.py has
# the same value and rule, and tests/test_runner_units.py checks that the two match).
CUMULATIVE_FROM = (2, 1, 277)


def cumulative_cli(version: str | None) -> bool:
    """Does this Claude Code version count resumed totals in modelUsage? Unknown counts as yes, as on the hub."""
    if not isinstance(version, str) or not version:
        return True
    try:
        parsed = tuple(int(x) for x in version.split(".")[:3])
    except ValueError:
        parsed = (0,)
    return parsed == (0,) or parsed >= CUMULATIVE_FROM


class UsageTracker:
    """Tokens and cost a job used, from the agent's result events.

    Claude Code: modelUsage and total_cost_usd cover the whole conversation (resumed totals and
    subagents included); model_usage_since_last_result is the share since the previous result.
    So "reported" is the newest conversation total and "own" the sum of the shares (None when a
    result came without one: the hub then works it out from the job this one continues).
    Cumulative figures are never added up. Gemini CLI reports per run: both are its stats."""

    def __init__(self, source: str) -> None:
        self.source = source            # "claude" or "gemini"
        self.reported: dict | None = None
        self.own: dict | None = None
        self.every_result_had_since = True
        self.turns = 0                  # results counted
        self.cli_version: str | None = None

    def add_claude(self, ev: dict) -> None:
        cumulative = usage_record(ev.get("modelUsage"))
        if cumulative is not None:
            total = _num(ev.get("total_cost_usd"))
            if total is not None:
                cumulative["cost_usd"] = _round_cost(total)
        elif isinstance(ev.get("usage"), dict):
            # Last resort (a Claude Code without modelUsage): this run's own figures.
            run = ev["usage"]
            cumulative = {field: _tokens(run.get(key)) for key, field in RUN_USAGE_MAP.items()}
            cumulative.update(cost_usd=_round_cost(_num(ev.get("total_cost_usd"))), models={}, scope="run")
        since = usage_record(ev.get("model_usage_since_last_result"))
        if cumulative is None and since is None:
            return
        if ev.get("subtype") == "error_during_execution" and _usage_zero(cumulative) and _usage_zero(since):
            return  # a crash's last result can carry all-zero figures: they mean nothing
        self.turns += 1
        if cumulative is not None:
            self.reported = cumulative
        if since is None:
            self.every_result_had_since = False
        else:
            self.own = add_usage(self.own, since)

    def add_gemini(self, stats: dict) -> None:
        if not isinstance(stats, dict):
            return
        record: dict[str, Any] = _gemini_counts(stats)
        models = {}
        for name, m in (stats.get("models") or {}).items() if isinstance(stats.get("models"), dict) else ():
            if isinstance(m, dict) and len(models) < MAX_USAGE_MODELS:
                models[str(name)[:100]] = {**_gemini_counts(m), "cost_usd": None}
        record.update(cost_usd=None, models=models)
        self.reported = self.own = record
        self.turns += 1

    def add_run(self, other: "UsageTracker") -> None:
        """Another run of the same conversation (the summary turn, which resumes the job's session), counted as if
        its results had come in this run: Claude Code's newest total for the conversation and the shares added up;
        Gemini CLI reports per run, so its figures are added up. So are Claude Code's when they cover only the run
        they came from (a version before CUMULATIVE_FROM, or one without modelUsage: scope "run", which stays)."""
        if not other.turns:
            return
        self.turns += other.turns
        if self.source == "gemini":
            if other.own is not None:
                self.reported = self.own = add_usage(self.own, other.own)
            return
        scopes = [r.get("scope") for r in (self.reported, other.reported) if r is not None]
        if other.reported is not None and self.reported is not None \
                and ("run" in scopes or not cumulative_cli(other.cli_version or self.cli_version)):
            self.reported = add_usage(self.reported, other.reported)
            if "run" in scopes:
                self.reported["scope"] = "run"
        elif other.reported is not None:
            self.reported = other.reported
        if other.own is not None:
            self.own = add_usage(self.own, other.own)
        self.every_result_had_since = self.every_result_had_since and other.every_result_had_since
        self.cli_version = other.cli_version or self.cli_version

    def total(self) -> dict | None:
        if not self.turns or self.reported is None:
            return None
        if self.source == "gemini":
            return {"reported": self.reported, "own": self.own, "source": "gemini", "turns": self.turns}
        return {"reported": self.reported, "own": self.own if self.every_result_had_since else None,
                "source": "claude", "turns": self.turns, "cli_version": self.cli_version}


# ------------------------------------------------------- "Check limits" (/usage)
HEADING_RE = re.compile(r"(?i)current\s+(session|week)(?:\s*\(([^)]{1,40})\))?")
PCT_RE = re.compile(r"(?i)(\d{1,3}(?:\.\d+)?)\s*%\s*used")
RESET_RE = re.compile(r"(?i)\bresets?\s+(?:at\s+|on\s+)?(.+?)\s*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
BAR_RE = re.compile("[█▉▊▋▌▍▎▏░▒▓]")
RATE_LIMITED_RE = re.compile(r"(?i)rate limited until (.+)")
REPORT_KINDS = {"session": "five_hour", "weekly_all": "seven_day"}
# "normal" says the window is fine now, so an older status from a job doesn't stay on its bar.
REPORT_SEVERITY = {"normal": "allowed", "warning": "allowed_warning", "error": "rejected", "critical": "rejected",
                   "exceeded": "rejected"}
USAGE_CHECK_TIMEOUT = 90  # seconds for one agent's /usage


def window_for(kind_or_heading: str, model: str | None) -> str:
    """The window id for a /usage line: 'session' -> five_hour; 'week' (all models) -> seven_day;
    a week for one model -> seven_day_sonnet, seven_day_opus, seven_day_overage_included (Fable,
    as Claude Code's own label table names that window), or seven_day_<model>."""
    kind = (kind_or_heading or "").strip().lower()
    if kind.startswith("session"):
        return "five_hour"
    if not kind.startswith("week"):
        return "current"
    label = re.sub(r"\bonly\b", " ", (model or "").lower()).strip()
    if not label or label in ("all models", "all"):
        return "seven_day"
    for word, wid in (("sonnet", "seven_day_sonnet"), ("opus", "seven_day_opus"),
                      ("fable", "seven_day_overage_included")):
        if word in label:
            return wid
    slug = re.sub(r"[^a-z0-9]+", "_", label).strip("_")[:30]
    return "seven_day_" + slug if slug else "seven_day"


def _check_observation(used: float | None, status: str | None, resets_at: float | None,
                       resets_text: str | None, now: float) -> dict:
    return {"used": used, "status": status, "resets_at": resets_at, "resets_text": resets_text,
            "threshold": None, "observed_at": now, "source": "check"}


def parse_usage_report(report: dict | None, now: float) -> dict[str, dict]:
    """The windows in Claude Code's structured usage_report (from /usage in print mode)."""
    limits = ((report or {}).get("rate_limits") or {}) if isinstance(report, dict) else {}
    entries = limits.get("limits") if isinstance(limits, dict) else None
    windows: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else ():
        if not isinstance(entry, dict):
            continue
        kind = entry.get("kind")
        if kind == "weekly_scoped":
            scope = entry.get("scope") if isinstance(entry.get("scope"), dict) else {}
            model = scope.get("model") if isinstance(scope.get("model"), dict) else {}
            name = model.get("display_name")
            wid = window_for("week", name if isinstance(name, str) else None)
        elif kind in REPORT_KINDS:
            wid = REPORT_KINDS[kind]
        else:
            continue
        percent = _num(entry.get("percent"))
        used = min(max(percent / 100, 0.0), 5.0) if percent is not None else None
        severity = entry.get("severity")
        status = REPORT_SEVERITY.get(severity.lower()) if isinstance(severity, str) else None
        obs = _check_observation(used, status, limit_epoch(entry.get("resets_at")), None, now)
        if wid not in windows and (obs["used"] is not None or obs["status"] or obs["resets_at"] is not None):
            windows[wid] = obs
    return windows


def parse_usage_text(text: str, now: float) -> dict[str, dict]:
    """The windows in /usage's text: "Current session: 31% used · resets Nov 6, 11pm (UTC)", or the
    same spread over several lines with bars. Lines before the first heading are ignored."""
    if not isinstance(text, str):
        return {}
    clean = BAR_RE.sub(" ", ANSI_RE.sub("", text))
    parts = [p.strip() for line in clean.splitlines() for p in re.split(r"\s+·\s+", line)]
    parts = [p for p in parts if p]
    windows: dict[str, dict] = {}
    i = 0
    while i < len(parts):
        heading = HEADING_RE.search(parts[i])
        if not heading:
            i += 1
            continue
        block, j = [parts[i]], i + 1
        while j < len(parts) and len(block) < 4 and not HEADING_RE.search(parts[j]):
            block.append(parts[j])
            j += 1
        used = resets_text = None
        for part in block:
            pct = PCT_RE.search(part)
            if used is None and pct:
                used = min(float(pct.group(1)) / 100, 5.0)
            reset = RESET_RE.search(part)
            if resets_text is None and reset:
                resets_text = reset.group(1).strip()[:80] or None
        wid = window_for(heading.group(1), heading.group(2))
        if wid not in windows and (used is not None or resets_text):
            windows[wid] = _check_observation(used, None, parse_reset_text(resets_text, now) if resets_text else None,
                                              resets_text, now)
        i = j
    return windows


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?"
ZONE_SUFFIX_RE = re.compile(r"\s*\(\s*([A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)*)\s*\)\s*$")
RESET_DATE_RE = re.compile(r"(?i)^([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?"
                           r"(?:,?\s+(?:at\s+)?" + _CLOCK + r")?$")
RESET_IN_RE = re.compile(r"(?i)^in\s+(?:(\d+)\s*d(?:ays?)?\s*)?(?:(\d+)\s*h(?:ours?|rs?)?\s*)?"
                         r"(?:(\d+)\s*m(?:in(?:ute)?s?)?\s*)?$")
RESET_WEEKDAY_RE = re.compile(r"(?i)^([a-z]{3})[a-z]*\.?,?\s+(?:at\s+)?" + _CLOCK + r"$")
RESET_TIME_RE = re.compile(r"(?i)^(?:at\s+)?" + _CLOCK + r"$")


def _zone(name: str) -> datetime.tzinfo | None:
    """A time zone by name; None (the machine's local time) when it isn't known here, e.g. on
    Windows without time zone data."""
    if name.upper() in ("UTC", "GMT", "Z"):
        return datetime.timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # unknown name, no tz data, or no zoneinfo module
        return None


def _clock(hour: str, minute: str | None, ampm: str | None) -> tuple[int, int] | None:
    hh, mm = int(hour), int(minute or 0)
    if ampm:
        if not 1 <= hh <= 12:
            return None
        hh = hh % 12 + (12 if ampm.lower().startswith("p") else 0)
    return (hh, mm) if 0 <= hh <= 23 and 0 <= mm <= 59 else None


def parse_reset_text(text: str, now: float) -> float | None:
    """When a /usage reset text means, as epoch seconds: "Nov 6, 11pm (Europe/Berlin)",
    "Nov 6, 2027, 11:30pm (UTC)", "in 2h 5m", "Mon 9am", "3pm". A date or time without a year or
    day is the next one after `now`. None when it can't be read."""
    if not isinstance(text, str):
        return None
    s = ANSI_RE.sub("", text).strip()
    tz = None
    zone = ZONE_SUFFIX_RE.search(s)
    if zone:
        tz = _zone(zone.group(1))
        s = s[:zone.start()]
    s = s.strip().rstrip(".").strip()
    if not s:
        return None
    base = datetime.datetime.fromtimestamp(now, tz)  # naive local time when tz is None

    def at(day: datetime.date, hm: tuple[int, int]) -> float | None:
        try:
            return datetime.datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=tz).timestamp()
        except (ValueError, OverflowError):
            return None

    m = RESET_IN_RE.match(s)
    if m and any(m.groups()):
        days, hours, minutes = (int(g or 0) for g in m.groups())
        return now + days * 86400 + hours * 3600 + minutes * 60
    m = RESET_DATE_RE.match(s)
    if m and m.group(1).lower() in MONTHS:
        hm = _clock(m.group(4), m.group(5), m.group(6)) if m.group(4) else (0, 0)
        month, day = MONTHS.index(m.group(1).lower()) + 1, int(m.group(2))
        if hm is None:
            return None
        try:
            if m.group(3):
                return at(datetime.date(int(m.group(3)), month, day), hm)
            when = at(datetime.date(base.year, month, day), hm)
            if when is not None and when < now - 86400:  # more than a day ago: next year's
                when = at(datetime.date(base.year + 1, month, day), hm)
            return when
        except ValueError:
            return None
    m = RESET_WEEKDAY_RE.match(s)
    if m and m.group(1).lower() in WEEKDAYS:
        hm = _clock(m.group(2), m.group(3), m.group(4))
        if hm is None:
            return None
        day = base.date() + datetime.timedelta(days=(WEEKDAYS.index(m.group(1).lower()) - base.weekday()) % 7)
        when = at(day, hm)
        if when is not None and when <= now:
            when = at(day + datetime.timedelta(days=7), hm)
        return when
    m = RESET_TIME_RE.match(s)
    if m:
        hm = _clock(m.group(1), m.group(2), m.group(3))
        if hm is None:
            return None
        when = at(base.date(), hm)
        if when is not None and when <= now:
            when = at(base.date() + datetime.timedelta(days=1), hm)
        return when
    return None


def _local_time(ts: Any) -> str:
    n = _num(ts)
    if n is None:
        return ""
    try:
        t = time.localtime(n)
    except (OverflowError, OSError, ValueError):
        return ""
    return f"{time.strftime('%b', t)} {t.tm_mday}, {t.tm_hour:02d}:{t.tm_min:02d}"


def usage_summary(windows: dict) -> str:
    """One line for the job log: '5-hour session 31% used, resets Nov 6, 11pm · week 47% · Fable 12%'."""
    parts = []
    for i, wid in enumerate(sorted(windows, key=window_order)):
        w = windows[wid]
        pct = f"{round(w['used'] * 100)}%" if w.get("used") is not None else "?"
        if i == 0:
            when = w.get("resets_text") or _local_time(w.get("resets_at"))
            parts.append(f"{window_label(wid, lower=True)} {pct} used" + (f", resets {when}" if when else ""))
        else:
            parts.append(f"{WINDOW_SHORT.get(wid) or window_label(wid, lower=True)} {pct}")
    return " · ".join(parts)


# ============================================================ output parsers
def summarize_tool(name: str, params: dict | None) -> str:
    params = params or {}
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if key in params and params[key]:
            value = str(params[key]).replace("\n", " ")
            return f"{name}: {value[:140]}"
    return name


# Claude's own messages at a usage limit name the window: "You've hit your 5-hour limit · resets 3pm (UTC)".
LIMIT_TEXT_RE = re.compile(r"(?i)you['’]ve hit your [^·\n]{0,40}\blimit\b|usage limit|rate limit")
LIMIT_WINDOW_TEXT_RE = re.compile(r"(?i)you['’]ve hit your ([^·\n]{0,40}?)\s*\blimit\b")
LIMIT_RESET_TEXT_RE = re.compile(r"(?i)\bresets?\s+(?:at\s+|on\s+)?([^·\n]{1,60})")
# The Anthropic API when the Console organization behind an API key has no credit left: HTTP 400
# invalid_request_error "Your credit balance is too low to access the Anthropic API. Please go to Plans &
# Billing to upgrade or purchase credits." Matched loosely, in case the wording changes a little.
CREDIT_TEXT_RE = re.compile(r"(?i)credit balance (?:is )?too low")
# What kind of error a job ended with, when the hub shows it in its own way: a Claude usage limit, a
# Gemini quota, or platform credits used up (an API-key Claude agent).
ERROR_KINDS = ("claude_limit", "quota", "credits")
# Gemini CLI at a quota limit, after its own retries (the text need not say 429).
QUOTA_RE = re.compile(r"(?i)\b429\b|RESOURCE_EXHAUSTED|quota exceeded|exceeded your current quota|rateLimitExceeded"
                      r"|request a quota increase through AI Studio")
QUOTA_ERROR_TYPES = {"TerminalQuotaError", "RetryableQuotaError"}
# The same on stderr and in warning events, where a bare 429 may be a line or column number in a stack
# trace. Gemini CLI also logs there every 429 it retries ("Attempt 1 failed with status 429. Retrying
# with backoff..."), with the error's details on indented lines below; those say nothing about how the
# run ended.
STDERR_QUOTA_RE = re.compile(r"(?i)RESOURCE_EXHAUSTED|quota exceeded|exceeded your current quota|rateLimitExceeded"
                             r"|request a quota increase through AI Studio|TerminalQuotaError|RetryableQuotaError"
                             r'|"code":\s*429\b|\b429 Too Many Requests')
GEMINI_RETRY_RE = re.compile(r"(?i)\bretrying\b")


class ClaudeStreamParser:
    """Reads `claude -p --output-format stream-json --verbose` events. `on_limits(windows, overage,
    top_status)` is called with every usage-limit event (see normalize_rate_limit)."""

    def __init__(self, on_limits: Callable[..., None] | None = None) -> None:
        self.result: str | None = None
        self.session_id: str | None = None
        self.error: str | None = None
        self.error_kind: str | None = None   # "claude_limit" at a usage limit, "credits" when credits ran out
        self.api_key = False                 # it signs in with an API key (set by the job)
        self.credit_said = ""                # what it said about the credit balance, on stderr or in a message
        self.last_text = ""
        self.on_limits = on_limits
        self.usage = UsageTracker("claude")
        self.cli_version: str | None = None
        self.started = False                 # the first init event was seen
        self.mcp_servers: dict = {}          # MCP server name -> status, from the first init event
        self.limit_hit: tuple | None = None  # (window id, resets_at) when requests were refused
        self.limit_signal = False            # an assistant message said "rate_limit"
        self.limit_lines: set = set()        # (kind, window) already logged in this job
        self.turns: list[dict] = []

    def start_turn(self) -> None:
        """A new turn in a live session: forget the last turn's answer and error."""
        self.error = self.result = self.error_kind = None
        self.limit_signal = False
        self.credit_said = ""

    def note_stderr(self, line: str) -> None:
        """For a CLI that exits without a result: what stderr said about the credit balance."""
        if self.api_key and not self.credit_said and CREDIT_TEXT_RE.search(line):
            self.credit_said = line.strip()

    def feed(self, line: str) -> list[str]:
        try:
            ev = json.loads(line)
        except ValueError:
            return [line]
        if not isinstance(ev, dict):
            return [line]
        out: list[str] = []
        kind, sub = ev.get("type"), ev.get("subtype")
        self.session_id = ev.get("session_id") or self.session_id
        nested = "  ↳ " if ev.get("parent_tool_use_id") else ""
        if kind == "system" and sub == "init":
            if not self.started:  # live sessions send one per turn; only the first counts
                self.started = True
                for server in ev.get("mcp_servers") or []:
                    if isinstance(server, dict) and isinstance(server.get("name"), str):
                        self.mcp_servers[server["name"]] = str(server.get("status") or "")
                version = ev.get("claude_code_version") or ev.get("version")
                self.cli_version = self.usage.cli_version = version[:40] if isinstance(version, str) else None
                out.append(f"▶ session started · model {ev.get('model', '?')}")
        elif kind == "system" and sub == "api_retry":
            out.append(f"⟳ API retry {ev.get('attempt')}/{ev.get('max_retries')} ({ev.get('error')})")
        elif kind == "rate_limit_event":
            info = ev.get("rate_limit_info") if isinstance(ev.get("rate_limit_info"), dict) else {}
            windows, overage, top = normalize_rate_limit(info, time.time())
            if self.on_limits is not None:
                try:
                    self.on_limits(windows, overage, top)
                except Exception:  # keeping the figures must never stop the job
                    log.exception("couldn't keep the usage limits")
            out += self._limit_lines(info, windows, top)
        elif kind == "assistant":
            if ev.get("error") == "rate_limit":
                self.limit_signal = True
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    if not nested:
                        self.last_text = block["text"]
                        if self.api_key and ev.get("error") and CREDIT_TEXT_RE.search(block["text"]):
                            self.credit_said = block["text"].strip()
                    out += [f"{nested}💬 {ln}" for ln in block["text"].strip().splitlines()[:40]]
                elif block.get("type") == "tool_use":
                    out.append(f"{nested}🔧 {summarize_tool(block.get('name', 'tool'), block.get('input'))}")
        elif kind == "user":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    text = block.get("content")
                    text = text if isinstance(text, str) else json.dumps(text)
                    out.append(f"{nested}  ✖ {text[:300]}")
        elif kind == "result":
            self.usage.add_claude(ev)
            self.result = ev.get("result")
            num_turns = ev.get("num_turns")
            secs = round((ev.get("duration_ms") or 0) / 1000)
            failed = bool(ev.get("is_error") or (sub and sub != "success"))
            self.turns.append({"turn_no": len(self.turns) + 1,
                               "result": self.result if isinstance(self.result, str) else "", "is_error": failed})
            if failed:
                said = str(ev.get("result") or ev.get("error") or "")
                if self.api_key and (CREDIT_TEXT_RE.search(said) or self.credit_said):
                    self.error_kind = "credits"
                    self.error = credits_message(said if CREDIT_TEXT_RE.search(said) else self.credit_said)
                elif (self.limit_hit or self.limit_signal or _num(ev.get("api_error_status")) == 429
                        or LIMIT_TEXT_RE.search(said)):
                    self.error_kind = "claude_limit"
                    self.error = self.limit_message(said)
                else:
                    self.error = f"{sub or 'error'}: {said or 'agent reported an error'}"
                out.append(f"✖ finished with an error ({sub})")
            else:
                out.append(f"✔ finished · {num_turns} turns · {secs}s")
        return out

    def _limit_lines(self, info: dict, windows: dict, top: str | None) -> list[str]:
        """⛔ when requests are refused, ⚠ when a window gets close; each once per window per job."""
        lines = []
        if top == "rejected":
            main = _window_id(info.get("rateLimitType")) or "current"
            resets = (windows.get(main) or {}).get("resets_at")
            self.limit_hit = (main, resets)
            if ("rejected", main) not in self.limit_lines:
                self.limit_lines.add(("rejected", main))
                when = format_utc(resets)
                lines.append(f"⛔ Usage limit reached: {window_label(main)}"
                             + (f", resets {when} UTC" if when else ""))
        for wid in sorted(windows, key=window_order):
            obs = windows[wid]
            if obs.get("status") == "allowed_warning" and ("warning", wid) not in self.limit_lines:
                self.limit_lines.add(("warning", wid))
                pct = f" ({round(obs['used'] * 100)}% used)" if obs.get("used") is not None else ""
                lines.append(f"⚠ Getting close to the {window_label(wid, lower=True)} limit{pct}")
        return lines

    def limit_message(self, said: str) -> str:
        """The job's error at a Claude usage limit, with what is known left in."""
        wid, resets = self.limit_hit or (None, None)
        label = window_label(wid) if wid else ""
        if not label:
            m = LIMIT_WINDOW_TEXT_RE.search(said)
            label = m.group(1).strip() if m else ""
        if resets is None:
            m = LIMIT_RESET_TEXT_RE.search(said)
            resets = parse_reset_text(m.group(1).strip(), time.time()) if m else None
        when = format_utc(resets)
        parts = ["Claude usage limit reached" + (f" ({label})" if label else "") + "."]
        if when:
            parts.append(f"It resets at {when} UTC.")
        parts.append("Wait for the reset: the hub doesn't move work to another account (guide 1).")
        if said.strip():
            parts.append("Claude said: " + said.strip()[:300])
        return " ".join(parts)

    def exit_error(self) -> str | None:
        """The error for a CLI that exited with an error code and no result, when it is known."""
        if self.api_key and self.credit_said:
            self.error_kind = "credits"
            return credits_message(self.credit_said)
        if self.limit_hit or self.limit_signal:
            self.error_kind = "claude_limit"
            return self.limit_message(self.last_text if self.limit_signal else "")
        return None

    def final(self, stdout_tail: str) -> str:
        return self.result if self.result is not None else self.last_text


def credits_message(said: str) -> str:
    """The job's error when the API refused an API-key Claude agent for lack of credit."""
    return ("Platform credits used up: the Console organization behind this agent's API key has no credit left, "
            "so the Anthropic API refuses requests until credits arrive with the next billing cycle, or more are "
            "bought (see the Usage page and guide 8)."
            + (" Anthropic said: " + said.strip()[:300] if said.strip() else ""))


def gemini_quota_message(said: str) -> str:
    return ("Gemini quota hit: the Google Cloud project behind this agent's API key reached a rate or quota limit "
            "(Gemini CLI had already retried). Limits apply per project, not per key. See "
            "https://aistudio.google.com/rate-limit. Requests-per-day limits reset at midnight Pacific time."
            + (" Gemini said: " + said.strip()[:300] if said.strip() else ""))


class GeminiStreamParser:
    """Reads `gemini --output-format stream-json` events."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.error: str | None = None
        self.error_kind: str | None = None   # "quota" when the error is a Gemini quota limit
        self.usage = UsageTracker("gemini")
        self.quota_said = ""   # what an error event or stderr said about a quota since the last answer
        self.segment = ""      # assistant text since the last tool call
        self.all_text = ""
        self.pending = ""

    def note_stderr(self, line: str) -> None:
        """For a CLI that exits without a result: what stderr said about a quota (not a retry it logged)."""
        if self.quota_said or line[:1].isspace() or GEMINI_RETRY_RE.search(line):
            return
        if STDERR_QUOTA_RE.search(line):
            self.quota_said = line.strip()

    def exit_error(self) -> str | None:
        """The error for a CLI that exited with an error code and no result, when it is known: a quota
        limit, when that was the last thing it said before it stopped answering."""
        if self.quota_said:
            self.error_kind = "quota"
            return gemini_quota_message(self.quota_said)
        return None

    def _flush(self, force: bool = False) -> list[str]:
        out = []
        while "\n" in self.pending:
            ln, self.pending = self.pending.split("\n", 1)
            if ln.strip():
                out.append(f"💬 {ln}")
        if force and self.pending.strip():
            out.append(f"💬 {self.pending}")
            self.pending = ""
        return out

    def feed(self, line: str) -> list[str]:
        try:
            ev = json.loads(line)
        except ValueError:
            return [line]
        if not isinstance(ev, dict):
            return [line]
        kind = ev.get("type")
        out: list[str] = []
        if kind == "init":
            self.session_id = ev.get("session_id")
            out.append(f"▶ session started · model {ev.get('model', '?')}")
        elif kind == "message" and ev.get("role") == "assistant":
            self.quota_said = ""  # a request after it worked
            text = ev.get("content") or ""
            if not ev.get("delta"):
                text = text + "\n"
            self.segment += text
            self.all_text += text
            self.pending += text
            out += self._flush()
        elif kind == "tool_use":
            self.quota_said = ""
            out += self._flush(force=True)
            self.segment = ""
            out.append(f"🔧 {summarize_tool(ev.get('tool_name', 'tool'), ev.get('parameters'))}")
        elif kind == "tool_result":
            self.quota_said = ""
            if ev.get("status") == "error":
                err = ev.get("error") or {}
                out.append(f"  ✖ {(err.get('message') if isinstance(err, dict) else err) or 'tool failed'}"[:300])
        elif kind == "error":
            err = ev.get("error")
            said = str(ev.get("message") or (err.get("message") if isinstance(err, dict) else err) or "")
            if not self.quota_said and ((isinstance(err, dict) and err.get("type") in QUOTA_ERROR_TYPES)
                                        or STDERR_QUOTA_RE.search(said)):
                self.quota_said = said or "quota limit"
            out.append(f"⚠ {ev.get('message') or ev.get('error') or line}"[:400])
        elif kind == "result":
            out += self._flush(force=True)
            if isinstance(ev.get("stats"), dict):
                self.usage.add_gemini(ev["stats"])
            if ev.get("status") == "error":
                err = ev.get("error") or {}
                said = str((err.get("message") if isinstance(err, dict) else err) or "")
                # Only how the run ended counts: an earlier 429 that Gemini CLI retried doesn't.
                if (isinstance(err, dict) and err.get("type") in QUOTA_ERROR_TYPES) or QUOTA_RE.search(said):
                    self.error_kind = "quota"
                    self.error = gemini_quota_message(said)
                else:
                    self.error = said or "Gemini reported an error"
                out.append("✖ finished with an error")
            else:
                out.append("✔ finished")
        return out

    def final(self, stdout_tail: str) -> str:
        return (self.segment.strip() or self.all_text.strip())


class TextParser:
    def __init__(self) -> None:
        self.session_id = None
        self.error = None
        self.error_kind = None
        self.lines: list[str] = []

    def feed(self, line: str) -> list[str]:
        self.lines.append(line)
        return [line]

    def final(self, stdout_tail: str) -> str:
        return "\n".join(self.lines).strip()


PARSERS = {"claude-stream": ClaudeStreamParser, "gemini-stream": GeminiStreamParser, "text": TextParser}


# ============================================================ shared context
# The hub's shared context (pinned notes, the hub's memory MCP server, the central
# MCP list and the agent add-ons) is written into each agent's OWN settings, so it
# works in interactive sessions too, not just in hub jobs. Everything we write is
# marked as ours; settings and files you made yourself are never changed.
MARKER = "managed-by: bullpen"
BLOCK_BEGIN = "<!-- bullpen:begin (managed by the Agent Bullpen runner; this block is rewritten on every sync) -->"
BLOCK_END = "<!-- bullpen:end -->"
BLOCK_RE = re.compile(r"<!-- bullpen:begin.*?<!-- bullpen:end -->", re.S)
STATE_FILE = BASE_DIR / "wired.json"
SYNC_LOCK = BASE_DIR / "sync.lock"
NODE_LAUNCHERS = {"npx", "npm", "pnpm", "pnpx", "yarn", "bunx"}
RESERVED_SERVERS = {"bullpen"}  # the hub's own memory server: always ours
# Add-on files installed by the first release (before files carried the marker), by sha256.
LEGACY_KIT_SHA256 = {
    "465b837d57beb9b4d5897efec38e0c717c0219af1af7773d32b10e73092a739a",  # second-opinion/SKILL.md
    "629c8a4bfc64d0c414a7c4c69eea530a1165a2924d939eac70b05313cd71efe8",  # second-opinion.toml
}


class FileLock:
    """An exclusive OS lock on a file, released automatically if the process dies.
    Used so two runner processes (the service and `runner.py sync`) never wire at once,
    and so only one runner runs per machine."""

    def __init__(self, path: Path):
        self.path = path
        self.fh = None

    def acquire(self, timeout: float = 60.0) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        deadline = time.monotonic() + timeout
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    self.fh.seek(0)
                    msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    self.fh.close()
                    self.fh = None
                    return False
                time.sleep(0.5)

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                self.fh.seek(0)
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.fh.close()
        self.fh = None


def write_if_changed(path: Path, text: str, private: bool = False) -> bool:
    """Write a text file only if its content changes. private=True keeps it readable by you
    only (on Windows your profile folder already is)."""
    if path.exists() and path.read_text(encoding="utf-8", errors="replace") == text:
        if private and not IS_WINDOWS:
            os.chmod(path, 0o600)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".bullpen-tmp")
    tmp.write_text(text, encoding="utf-8")
    if private and not IS_WINDOWS:
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def set_block(path: Path, lines: list[str]) -> bool:
    """Put our managed block (import lines) into a markdown file, leaving the rest alone."""
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    block = BLOCK_BEGIN + "\n" + "\n".join(lines) + "\n" + BLOCK_END
    if BLOCK_RE.search(text):
        new = BLOCK_RE.sub(lambda _m: block, text, count=1)
    else:
        new = (text.rstrip() + "\n\n" if text.strip() else "") + block + "\n"
    return write_if_changed(path, new)


def _jsonc_pass(text: str, drop_comments: bool) -> str:
    """One pass over JSON-with-comments text, string-aware: drop // and /* */ comments, or
    (second pass) drop trailing commas before } or ]."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
        elif drop_comments and text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        elif drop_comments and text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        elif not drop_comments and c == ",":
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j < n and text[j] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def load_jsonc(path: Path) -> tuple[dict, bool]:
    """Read JSON that may contain // or /* */ comments and trailing commas (Antigravity allows them).
    Returns (data, had_comments). Commas and slashes inside strings are left alone."""
    if not path.exists():
        return {}, False
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}, False
    try:
        return json.loads(text), False
    except ValueError:
        pass
    return json.loads(_jsonc_pass(_jsonc_pass(text, True), False)), True


MCP_TOKEN_FILE = BASE_DIR / "mcp-token.json"  # this machine's own MCP token (keep_machine_mcp_token), private
MCP_TOKEN_LOCK = BASE_DIR / "mcp-token.lock"  # held while one sync reads, renews and keeps that token
MCP_TOKEN_RE = re.compile(r"^bpm_[A-Za-z0-9_-]{43}$")  # the shape the hub makes


def read_machine_mcp_token(cfg: dict, path: Optional[Path] = None) -> str:
    """This machine's own MCP token, as the hub at cfg's hub_url last gave it ('' for none). The hub keeps only
    its sha256, so the runner keeps the token itself, readable only by its user, and sends it back at each sync."""
    path = path or MCP_TOKEN_FILE
    try:
        data = json.loads(read_private(path))
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, UnicodeError) as err:
        log.warning("couldn't read %s (%s); the hub makes a new MCP token for this machine", path, err)
        return ""
    if not isinstance(data, dict) or data.get("hub_url") != str(cfg.get("hub_url") or "").rstrip("/"):
        return ""  # made by another hub: never sent to this one
    token = data.get("token")
    return token if isinstance(token, str) and MCP_TOKEN_RE.match(token) else ""


def keep_machine_mcp_token(cfg: dict, context: dict, held: str, path: Optional[Path] = None) -> None:
    """After a sync: keep the MCP token the hub gave this machine (mcp_token_kind "machine"), atomically and
    private, when it is new. A hub that gives the shared token (a machine on the shared runner token, or an older
    hub) makes a kept one useless, so it is deleted. The token itself is never logged."""
    path = path or MCP_TOKEN_FILE
    if context.get("mcp_token_kind") != "machine":
        if path.exists():
            try:
                path.unlink()
            except OSError as err:
                log.warning("couldn't remove %s (%s)", path, err)
        return
    token = context.get("mcp_token")
    if not isinstance(token, str) or not MCP_TOKEN_RE.match(token) or token == held:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_private(path, json.dumps({"hub_url": str(cfg.get("hub_url") or "").rstrip("/"), "token": token}) + "\n")
    except OSError as err:
        log.warning("couldn't save this machine's MCP token in %s (%s); after a restart the hub makes a new one",
                    path, err)
        return
    log.info("the hub gave this machine a new MCP token; this sync writes it into the agents' settings")


def claude_import(path: Path) -> str:
    """An @import line Claude Code understands, on Linux and Windows."""
    try:
        ref = "~/" + path.relative_to(HOME).as_posix()
    except ValueError:
        ref = path.as_posix()
    return "@" + ref.replace(" ", "\\ ")


class Wiring:
    def __init__(self, cfg: dict, context: dict, emit: Callable[..., None]):
        self.cfg = cfg
        self.ctx = context
        self.emit = emit
        self.notes: list[str] = []
        self.machine = cfg.get("name") or socket.gethostname()
        self.platform = PLATFORM
        if PLATFORM == "macos" and "macos" not in (context.get("platforms") or []):
            self.platform = "linux"  # a hub that lists no "macos" platform sends Macs the Linux servers
        self.state: dict = {}
        gemini_home = Path(os.environ.get("GEMINI_CLI_HOME") or HOME) / ".gemini"
        self.gemini_dir = gemini_home
        self.has_gemini = gemini_home.is_dir() or any(
            agent_spec(cfg, a).get("preset") == "gemini" for a in cfg["agents"])
        self.has_antigravity = (gemini_home / "config").is_dir() or (gemini_home / "antigravity").is_dir()

    # -- helpers -----------------------------------------------------------
    def hub_server(self, who: str) -> tuple[str, dict]:
        url = self.cfg["hub_url"].rstrip("/") + "/mcp"
        who = re.sub(r"[^\x20-\x7e]", "?", who)[:100]  # HTTP header values must be plain ASCII
        headers = {"Authorization": f"Bearer {self.ctx.get('mcp_token', '')}", "X-Bullpen-Agent": who,
                   **cf_access_headers(self.cfg)}
        return url, headers

    def servers_for(self, target: str, who: str = "", with_on_demand: bool = False) -> list[dict]:
        """The central servers for one kind of agent on this platform. Servers routed through the
        hub become plain HTTP servers at the hub's /mcp/s/<name>, with the hub's token. On-demand
        servers are only for the jobs that pick them, so they're left out unless `with_on_demand`
        (an older hub sends no on_demand key: none are)."""
        out = []
        for s in self.ctx.get("servers") or []:
            if target not in s.get("targets", []) or self.platform not in s.get("platforms", []):
                continue
            if s.get("on_demand") and not with_on_demand:
                continue
            if s.get("route") == "hub":
                if not self.ctx.get("mcp_enabled"):
                    continue  # the hub can't serve it (its MCP support is off)
                url, headers = self.hub_server(who or f"{target}@{self.machine}")
                s = {**s, "transport": "http", "url": url.rstrip("/") + "/s/" + s["name"], "headers": headers}
            out.append(s)
        return out

    def claude_entries(self, names: list[str], who: str) -> dict[str, dict]:
        """The mcpServers entries Claude Code gets for `names`, in the hub's order: `bullpen` (the
        hub's own server) and the central servers, on-demand ones included. A name it can't give
        (unknown, not for Claude Code or this platform, or relayed while the hub's MCP support is
        off) is left out."""
        wanted = set(names)
        entries: dict[str, dict] = {}
        if "bullpen" in wanted and self.ctx.get("mcp_enabled"):
            url, headers = self.hub_server(who)
            entries["bullpen"] = {"type": "http", "url": url, "headers": headers}
        for srv in self.servers_for("claude", who, with_on_demand=True):
            if srv["name"] not in wanted:
                continue
            if srv["transport"] == "http":
                entries[srv["name"]] = {"type": "http", "url": srv["url"], "headers": srv["headers"]}
            else:
                cmd, args = srv["command"], list(srv["args"])
                if IS_WINDOWS and cmd.lower() in NODE_LAUNCHERS:
                    cmd, args = "cmd", ["/c", srv["command"], *args]  # Claude Code needs this for npx on Windows
                entries[srv["name"]] = {"type": "stdio", "command": cmd, "args": args, "env": srv["env"]}
        return entries

    def kit_files(self, prefix: str) -> dict[str, str]:
        return {k[len(prefix):]: v for k, v in (self.ctx.get("kit") or {}).items() if k.startswith(prefix)}

    def install_kit(self, prefix: str, dest: Path, label: str) -> None:
        for rel, text in self.kit_files(prefix).items():
            self.install_kit_file(dest / rel, text, label)

    def install_kit_file(self, target: Path, text: str, label: str) -> None:
        if target.exists():
            old = target.read_text(encoding="utf-8", errors="replace")
            legacy = hashlib.sha256(old.replace("\r\n", "\n").encode()).hexdigest() in LEGACY_KIT_SHA256
            if MARKER not in old and not legacy:
                self.notes.append(f"{label}: {target} exists and isn't ours, so it was left alone")
                return
        if write_if_changed(target, text):
            self.emit(f"{label}: wrote {target}")

    def remove_kit_file(self, dest: Path, rel: str, label: str) -> None:
        """Take out an add-on file an earlier sync wrote (and the folders it leaves empty). A file that
        isn't ours stays."""
        target = dest / rel
        if not target.is_file() or target.is_symlink() or not made_by_runner(target):
            return
        target.unlink()
        self.emit(f"{label}: removed {target}")
        parent = target.parent
        while parent != dest and _inside(parent, dest):
            try:
                parent.rmdir()  # only when it is empty: a file of your own keeps it
            except OSError:
                break
            parent = parent.parent

    def sync_servers(self, key: str, current: dict, desired: dict, label: str,
                     apply: Callable[[str, dict | None], bool]) -> None:
        """Add/update/remove our MCP entries; never touch servers the user added."""
        managed = set(self.state.get(key, []))
        for name, conf in desired.items():
            # Ours if we recorded it, if it's the hub's reserved name, or if it's exactly what we'd write
            # (so a sync that was interrupted before saving its state heals itself).
            if name in current and (name in RESERVED_SERVERS or current.get(name) == conf):
                managed.add(name)
            if name in current and name not in managed:
                self.notes.append(f"{label}: you already have an MCP server called '{name}'; left it alone")
                continue
            if current.get(name) == conf:
                managed.add(name)
                continue
            if apply(name, conf):
                managed.add(name)
                self.emit(f"{label}: {'updated' if name in current else 'added'} MCP server '{name}'")
        for name in sorted(managed - set(desired)):
            if name in current and not apply(name, None):
                continue
            managed.discard(name)
            self.emit(f"{label}: removed MCP server '{name}'")
        self.state[key] = sorted(managed)
        self.save_state()

    def save_state(self) -> None:
        try:
            write_if_changed(STATE_FILE, json.dumps(self.state, indent=2) + "\n")
        except OSError as err:
            self.notes.append(f"couldn't save {STATE_FILE}: {err}")

    # -- Claude Code (each account's config dir) ------------------------------
    def claude(self) -> None:
        seen = set()
        for agent in self.cfg["agents"]:
            spec = agent_spec(self.cfg, agent)
            if spec.get("preset") != "claude":
                continue
            env = build_env(spec)
            custom = env.get("CLAUDE_CONFIG_DIR")
            cfg_dir = Path(custom) if custom else HOME / ".claude"
            if str(cfg_dir) in seen:
                continue
            seen.add(str(cfg_dir))
            label = f"Claude ({agent})"
            if not cfg_dir.is_dir():
                self.notes.append(f"{label}: {cfg_dir} doesn't exist yet (sign in once), skipped")
                continue
            # 1. Pinned notes: import the synced files from this account's own CLAUDE.md.
            imports = [claude_import(CONTEXT_DIR / "shared.md")]
            for extra in (f"agent-{agent}.md",):
                if (CONTEXT_DIR / extra).exists():
                    imports.append(claude_import(CONTEXT_DIR / extra))
            if set_block(cfg_dir / "CLAUDE.md", imports):
                self.emit(f"{label}: updated {cfg_dir / 'CLAUDE.md'}")
            # 2. Skills.
            self.install_kit("skills/", cfg_dir / "skills", label)
            self.install_kit("claude/skills/", cfg_dir / "skills", label)
            # 3. MCP servers, via Claude Code's own CLI so its settings file stays consistent.
            exe = resolve_command(spec)
            if exe is None:
                self.notes.append(f"{label}: Claude Code not found, MCP servers not configured")
                continue
            claude_json = (cfg_dir / ".claude.json") if custom else (HOME / ".claude.json")
            try:
                current = (json.loads(claude_json.read_text(encoding="utf-8")) if claude_json.exists()
                           else {}).get("mcpServers") or {}
            except ValueError:
                self.notes.append(f"{label}: couldn't read {claude_json}; MCP servers not changed")
                continue
            who = f"{agent}@{self.machine}"
            desired = self.claude_entries(["bullpen"] + [s["name"] for s in self.servers_for("claude", who)], who)
            run_env = dict(env)
            run_env.pop("CLAUDECODE", None)

            def apply(name: str, conf: dict | None) -> bool:
                if name in current:
                    self.run_cli([exe, "mcp", "remove", "-s", "user", name], run_env)
                if conf is None:
                    return True
                ok = self.run_cli([exe, "mcp", "add-json", "-s", "user", name, json.dumps(conf)], run_env)
                if not ok:
                    self.notes.append(f"{label}: couldn't add MCP server '{name}' (see the runner log)")
                return ok

            self.sync_servers(f"claude:{cfg_dir}", current, desired, label, apply)

    def run_cli(self, argv: list[str], env: dict) -> bool:
        try:
            res = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=90, stdin=subprocess.DEVNULL,
                                 creationflags=0x08000000 if IS_WINDOWS else 0)
        except (OSError, subprocess.TimeoutExpired) as err:
            log.warning("%s failed: %s", redact_argv(argv[:4]), err)
            return False
        if res.returncode != 0:
            log.warning("%s exited %s: %s", redact_argv(argv[:4]), res.returncode,
                        redact((res.stderr or res.stdout)[-400:], known_secrets(self.cfg)))
        return res.returncode == 0

    # -- Gemini CLI and Antigravity (both under ~/.gemini) -----------------------
    def gemini(self) -> None:
        g = self.gemini_dir
        label = "Gemini CLI"
        # 1. Pinned notes. Gemini only imports files inside ~/.gemini, so keep a copy there.
        shared = (CONTEXT_DIR / "shared.md").read_text(encoding="utf-8") if (CONTEXT_DIR / "shared.md").exists() else ""
        extra = CONTEXT_DIR / "agent-gemini.md"
        if extra.exists():
            shared += "\n" + extra.read_text(encoding="utf-8")
        if write_if_changed(g / "bullpen-shared.md", shared):
            self.emit(f"{label}: updated {g / 'bullpen-shared.md'}")
        lines = ["@./bullpen-shared.md"]
        if self.has_antigravity:
            lines.append("@[Shared memory from the Agent Bullpen](~/.gemini/bullpen-shared.md)")
        if set_block(g / "GEMINI.md", lines):
            self.emit(f"{label}: updated {g / 'GEMINI.md'}")
        # 2. Commands and skills.
        self.install_kit("gemini/commands/", g / "commands", label)
        self.gemini_skills(label)
        # 3. MCP servers in settings.json.
        settings_file = g / "settings.json"
        try:
            settings, had_comments = load_jsonc(settings_file)
        except ValueError:
            self.notes.append(f"{label}: couldn't read {settings_file}; MCP servers not changed")
            return
        current = settings.get("mcpServers") or {}
        desired: dict[str, dict] = {}
        if self.ctx.get("mcp_enabled"):
            url, headers = self.hub_server(f"gemini@{self.machine}")
            desired["bullpen"] = {"url": url, "type": "http", "headers": headers, "trust": True,
                                "timeout": 600000, "description": "Shared memory (Agent Bullpen)"}
        for srv in self.servers_for("gemini", f"gemini@{self.machine}"):
            desired[srv["name"]] = ({"url": srv["url"], "type": "http", "headers": srv["headers"]}
                                    if srv["transport"] == "http" else
                                    {"command": srv["command"], "args": srv["args"], "env": srv["env"]})
        changed = []

        def apply(name: str, conf: dict | None) -> bool:
            if conf is None:
                current.pop(name, None)
            else:
                current[name] = conf
            changed.append(name)
            return True

        self.sync_servers("gemini", dict(current), desired, label, apply)
        if changed:
            settings["mcpServers"] = current
            if had_comments:
                shutil.copy2(settings_file, settings_file.with_name("settings.json.bak-bullpen"))
            write_if_changed(settings_file, json.dumps(settings, indent=2) + "\n", private=True)

    def gemini_reads_shared_skills(self) -> bool:
        """Whether Gemini CLI on this machine reads the runner's ~/.agents/skills. Gemini CLI 0.28 and later
        reads <its home>/.agents/skills as another name for <its home>/.gemini/skills, and its home is
        GEMINI_CLI_HOME when that is set."""
        return self.has_gemini and (
            os.path.normcase(os.path.abspath(self.gemini_dir.parent)) == os.path.normcase(os.path.abspath(HOME)))

    def gemini_skills(self, label: str) -> None:
        """The add-on skills in ~/.gemini/skills, unless Gemini CLI finds them in ~/.agents/skills already.
        Gemini CLI warns of a "skill conflict" when a skill is in both. So a skill that is in
        ~/.agents/skills (ours or yours) is left out here, and a copy an earlier sync wrote here is taken
        out. Gemini CLI older than 0.28 loses only that copy: /remember and /recall are also its commands,
        in ~/.gemini/commands.
        The Antigravity CLI reads ~/.gemini/skills, ~/.gemini/antigravity-cli/skills and
        ~/.gemini/config/skills, but not ~/.agents/skills. When it has its own folder and the Antigravity
        skills folder (config/skills) isn't written, it gets the skill in its own folder."""
        dest = self.gemini_dir / "skills"
        agy_cli = self.gemini_dir / "antigravity-cli" / "skills"
        shared = HOME / ".agents" / "skills"
        reads_shared = self.gemini_reads_shared_skills()
        agy_cli_needs_it = agy_cli.parent.is_dir() and not self.has_antigravity
        for rel, text in self.kit_files("skills/").items():
            in_shared = reads_shared and (shared / rel.split("/")[0] / "SKILL.md").is_file()
            if in_shared:
                self.remove_kit_file(dest, rel, label)
            else:
                self.install_kit_file(dest / rel, text, label)
            if in_shared and agy_cli_needs_it:
                self.install_kit_file(agy_cli / rel, text, "Antigravity CLI")
            else:
                self.remove_kit_file(agy_cli, rel, "Antigravity CLI")

    def antigravity(self) -> None:
        g = self.gemini_dir
        label = "Antigravity"
        self.install_kit("skills/", g / "config" / "skills", label)
        mcp_file = g / "config" / "mcp_config.json"
        try:
            data, had_comments = load_jsonc(mcp_file)
        except ValueError:
            self.notes.append(f"{label}: couldn't read {mcp_file}; MCP servers not changed")
            return
        current = data.get("mcpServers") or {}
        desired: dict[str, dict] = {}
        if self.ctx.get("mcp_enabled"):
            url, headers = self.hub_server(f"antigravity@{self.machine}")
            desired["bullpen"] = {"serverUrl": url, "headers": headers, "timeoutSeconds": 600}
        for srv in self.servers_for("antigravity", f"antigravity@{self.machine}"):
            desired[srv["name"]] = ({"serverUrl": srv["url"], "headers": srv["headers"]}
                                    if srv["transport"] == "http" else
                                    {"command": srv["command"], "args": srv["args"], "env": srv["env"]})
        changed = []

        def apply(name: str, conf: dict | None) -> bool:
            if conf is None:
                current.pop(name, None)
            else:
                current[name] = conf
            changed.append(name)
            return True

        self.sync_servers("antigravity", dict(current), desired, label, apply)
        if changed:
            data["mcpServers"] = current
            if had_comments:
                shutil.copy2(mcp_file, mcp_file.with_name("mcp_config.json.bak-bullpen"))
            write_if_changed(mcp_file, json.dumps(data, indent=2) + "\n", private=True)

    # -- the shared skills folder (~/.agents/skills) ------------------------------
    def shared_skills(self) -> None:
        """The add-ons for every agent, in the user-level skills folder that many agent CLIs read
        (Codex, OpenCode, Copilot CLI, Cursor, Qwen Code, Kimi, Vibe, Amp and others). Gemini CLI reads
        this folder too (see gemini_skills), so a skill you made yourself in ~/.gemini/skills keeps that
        name: ours isn't put here, where it would replace yours and make Gemini CLI warn."""
        label, dest = "Shared skills folder", HOME / ".agents" / "skills"
        gemini_own = self.gemini_dir / "skills"
        reads_shared = self.gemini_reads_shared_skills()
        for rel, text in self.kit_files("skills/").items():
            yours = gemini_own / rel.split("/")[0] / "SKILL.md"
            if reads_shared and yours.is_file() and not made_by_runner(yours):
                self.remove_kit_file(dest, rel, label)
                if rel.endswith("/SKILL.md"):
                    self.notes.append(f"{label}: Gemini CLI has a skill of yours at {yours}, so the runner's "
                                      f"wasn't put in {dest}")
                continue
            self.install_kit_file(dest / rel, text, label)

    # -- the `bullpen` command (fallback for skills, handy in a terminal) ----------
    def command(self) -> None:
        bin_dir = HOME / ".local" / "bin"
        python = stable_python()
        if IS_WINDOWS and python.lower().endswith("pythonw.exe"):
            python = python[:-len("pythonw.exe")] + "python.exe"
        runner = HERE / "runner.py"
        if IS_WINDOWS:
            path = bin_dir / "bullpen.cmd"
            # Plain \n: text mode writes \r\n on Windows by itself.
            text = f'@echo off\nrem {MARKER}\n"{python}" "{runner}" --config "{self.cfg_path}" %*\n'
        else:
            path = bin_dir / "bullpen"
            text = f'#!/bin/sh\n# {MARKER}\nexec "{python}" "{runner}" --config "{self.cfg_path}" "$@"\n'
        if path.exists() and MARKER not in path.read_text(encoding="utf-8", errors="replace"):
            self.notes.append(f"{path} exists and isn't ours, so the bullpen command wasn't installed")
            return
        if write_if_changed(path, text):
            if not IS_WINDOWS:
                os.chmod(path, 0o755)
            self.emit(f"installed the bullpen command at {path}")

    def run(self, cfg_path: Path) -> list[str]:
        self.cfg_path = cfg_path
        if not self.cfg.get("manage_agent_settings", True):
            return ["managing agent settings is turned off for this machine (manage_agent_settings)"]
        lock = FileLock(SYNC_LOCK)
        if not lock.acquire(timeout=120):
            return ["another sync was still running after 2 minutes; this one was skipped"]
        try:
            try:
                self.state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
            except ValueError:
                self.state = {}
            return self._run_steps()
        finally:
            lock.release()

    def _run_steps(self) -> list[str]:
        # The shared skills folder before Gemini CLI: gemini_skills() looks at what is there.
        steps = [("Claude", self.claude), ("Shared skills folder", self.shared_skills),
                 ("bullpen command", self.command)]
        if self.has_gemini:
            steps.append(("Gemini", self.gemini))
        if self.has_antigravity:
            steps.append(("Antigravity", self.antigravity))
        for name, step in steps:
            try:
                step()
            except Exception as err:  # one tool's odd settings mustn't stop the others
                log.exception("sync step %s failed", name)
                self.notes.append(f"{name}: sync failed ({err})")
        self.save_state()
        return self.notes


def wire_agents(cfg: dict, context: dict, emit: Callable[..., None], cfg_path: Path = DEFAULT_CONFIG) -> list[str]:
    """Point each agent's own memory file, skills and MCP settings at the shared context."""
    return Wiring(cfg, context, emit).run(cfg_path)


def claude_server_entries(cfg: dict, context: dict, names: list[str], who: str) -> dict[str, dict]:
    """The mcpServers entries Claude Code gets for `names`, from a context the hub sent: the same
    entries Wiring.claude writes, on-demand servers included (see Wiring.claude_entries)."""
    return Wiring(cfg, context, lambda *a: None).claude_entries(names, who)


# ================================================================== running
def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=0x08000000)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def run_capture(argv: list[str], cwd: str, env: dict, stdin_text: str, timeout: float,
                cancel: threading.Event) -> tuple[int | None, str, list[str], str]:
    """Run a CLI once in its own process group with `stdin_text` on its input, stopping it on
    cancel or after `timeout` seconds. (exit code, stdout, stderr lines, "" or why it was stopped)."""
    kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} \
        if IS_WINDOWS else {"start_new_session": True}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **kw)
    stopped: list[str] = []
    finished = threading.Event()

    def watchdog() -> None:
        deadline = time.monotonic() + timeout
        while not finished.wait(0.25):
            if cancel.is_set() or time.monotonic() > deadline:
                stopped.append("cancelled" if cancel.is_set() else "timeout")
                kill_tree(proc)
                return

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        out, err = proc.communicate(stdin_text, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out, err = proc.communicate()
        stopped.append("timeout")
    finally:
        finished.set()
    return proc.returncode, out or "", (err or "").splitlines(), (stopped[0] if stopped else "")


def git(path: str, *args: str, env: dict[str, str] | None = None) -> str | None:
    try:
        res = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60, env=env,
                             creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return res.stdout if res.returncode == 0 else None


def capture_diff(path: str, start_sha: str | None, limit: int = 200_000,
                 mask: Callable[[str], str] | None = None) -> str:
    """Everything that changed in the project since the job started, as one diff; `mask` (a job's secrets out)
    applies before it is cut to `limit`, so a secret at the cut can't stay half there."""
    if git(path, "rev-parse", "--is-inside-work-tree") is None:
        return "(this project is not a git repository, so no diff is available)"
    base = start_sha or "HEAD"
    parts = [git(path, "diff", base) or ""]
    untracked = (git(path, "ls-files", "--others", "--exclude-standard") or "").splitlines()
    for rel in untracked[:40]:
        file = Path(path) / rel
        try:
            if file.stat().st_size > 50_000:
                parts.append(f"\n=== new file {rel} (too large to include) ===\n")
                continue
            text = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"\n=== new file {rel} ===\n{text}")
    if len(untracked) > 40:
        parts.append(f"\n... and {len(untracked) - 40} more new files\n")
    diff = "".join(parts).strip()
    if mask is not None:
        diff = mask(diff)
    if len(diff) > limit:
        diff = diff[:limit] + f"\n\n[... diff truncated, {len(diff) - limit} more characters ...]"
    return diff or "(no file changes)"


# ------------------------------------------------------------- git accounts in a job
# A job, clone or pull with a git account gets that account alone for its host:
# - ssh to that host gets the account's key only, or, for an account without a key, no way to sign in at all,
#   so it fails rather than use the machine user's own key (a per-job ssh config; the machine user's own
#   ~/.ssh/config and the system's /etc/ssh/ssh_config still apply to every other host). git runs the ssh on
#   its PATH with it, as git does when nothing names another: an ssh command the machine's git names
#   (core.sshCommand, GIT_SSH) isn't used, and the log says so (git_ssh_program_note);
# - the credential helpers set before for that host (https:// and http://) are cleared, then the account's token
#   helper added for https://; extra HTTP headers set for the host (an Authorization header in a repository's
#   config, say) are cleared too. GIT_CONFIG_PARAMETERS (git -c settings) is left out of the environment, with
#   a note in the log: git reads it after these entries, so its credential helpers would come back;
# - git gets an askpass program that answers nothing (GIT_ASKPASS, with GIT_TERMINAL_PROMPT=0), and
#   SSH_ASKPASS and SSH_ASKPASS_REQUIRE are left out, so an account without a token fails over https://
#   instead of asking the machine's own program or a terminal for a password;
# - refused before anything is written: a netrc file curl would read for the host (git's https:// goes through
#   curl, which signs in with it before any helper is asked: git_netrc_problem); and, for a job, a remote URL
#   or a url.<base>.insteadOf target with credentials for the host (check_git_credential_urls);
# - gh or glab get a config folder of their own and the account's token. Without a token they get a stand-in
#   that the host refuses, and the log says they are signed out: with no token at all, gh would take the
#   machine user's own login from the system keychain (macOS, Windows, a Linux desktop), which no config folder
#   hides.
# The account's host is a host name and maybe a port, which is the port for ssh: https:// goes to the host name
# (on port 443). Other hosts are not touched. A program that reads the machine user's keychain on purpose (gh
# auth token --secure-storage, say) still can: the job runs as that user. The token travels in the environment
# only (BULLPEN_GIT_TOKEN, which the helper reads), never in a command line or a file, and the job's log is
# masked with it.
GIT_MIN_VERSION = (2, 31)  # GIT_CONFIG_COUNT and its entries
# printf '%s', not echo: some shells' echo (dash, macOS's /bin/sh) reads a backslash in a value as an escape.
GIT_CREDENTIAL_HELPER = ("!f() { printf '%s\\n' \"username=${BULLPEN_GIT_USER}\" \"password=${BULLPEN_GIT_TOKEN}\"; "
                         "}; f")
# Left out of the environment of a job, clone or pull with a git account (see above).
GIT_ACCOUNT_UNSET = ("GIT_CONFIG_PARAMETERS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE")
# The ssh settings that sign in, all off for the host of an account without a key (with IdentitiesOnly yes, no
# identity file and no ssh agent).
SSH_SIGN_IN_OPTIONS = ("PubkeyAuthentication", "PasswordAuthentication", "KbdInteractiveAuthentication",
                       "GSSAPIAuthentication", "HostbasedAuthentication")
# The system-wide ssh settings, which ssh -F doesn't read: included for every host but the account's.
SYSTEM_SSH_CONFIG = Path("/etc/ssh/ssh_config")
# The variables gh and glab take a token from, by the hosts they apply to, first one first
# (https://cli.github.com/manual/gh_help_environment and https://docs.gitlab.com/cli/configuration/, last
# checked 2026-11-08). One already in the environment would stand in for an account that has no token.
GH_TOKEN_VARS = ("GH_TOKEN", "GITHUB_TOKEN")                                   # github.com, *.ghe.com
GH_ENTERPRISE_TOKEN_VARS = ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")  # GitHub Enterprise Server
GLAB_TOKEN_VARS = ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")
# gh's or glab's token for the host of a github or gitlab account without one: not a secret, not shaped like a
# token, and refused by the host (HTTP 401), so they fail there instead of signing in as the machine user.
GH_NO_TOKEN = "no-token-in-this-git-account"
_GIT_VERSION: dict[str, tuple[int, int]] = {}  # by the git program's full path: what its --version said


class GitAccountProblem(Exception):
    """A git account can't be given to a job, clone or pull; the message says why, without a value from it."""


def git_version(search_path: str | None = None) -> tuple[int, int] | None:
    """(major, minor) of the git found first on `search_path` (a PATH value: the one a job's environment has, as
    the agent finds git there; None: the runner's own PATH); None when there is none there. Kept per program
    once it answers."""
    exe = shutil.which("git", path=search_path)
    if exe is None:
        return None
    exe = os.path.abspath(exe)
    if exe in _GIT_VERSION:
        return _GIT_VERSION[exe]
    try:
        done = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.match(r"git version (\d+)\.(\d+)", (done.stdout or "").strip())
    if done.returncode != 0 or not m:
        return None
    _GIT_VERSION[exe] = (int(m.group(1)), int(m.group(2)))
    return _GIT_VERSION[exe]


def _ssh_path(path: Any, what: str, shell: bool = False) -> str:
    """A full path as an ssh config file (in double quotes) or GIT_SSH_COMMAND (in double quotes, read by a
    shell) holds it: forward slashes on Windows, where ssh and git's shell read a backslash as an escape.
    GitAccountProblem when a character in it would end the quotes or the line."""
    text = os.path.abspath(str(path))
    if IS_WINDOWS:
        text = text.replace("\\", "/")
    if re.search(r'["$`\\]' if shell else r'"', text) or GIT_CONTROL_RE.search(text):
        raise GitAccountProblem(f"{what} has a character that can't go into an ssh setting")
    return text


def git_netrc_problem(env: dict[str, str], hostname: str, name: str) -> str:
    """Why git account `name` can't be used for `hostname` (no port) with the netrc file curl would read for a job,
    clone or pull with environment `env`: $HOME/.netrc (HOME from `env`, else the runner's home), and on Windows
    also _netrc, with HOME falling back to USERPROFILE. git's https:// goes through curl, which signs in with a
    'machine <host>' or 'default' entry there before git asks any credential helper, so the machine user's login
    would be used instead of the account's. A plain scan of the words (keywords and host compared in lower case),
    not a netrc parser, so a file one would refuse can't let the job through. "" when there is no such entry.
    Never holds anything from the file."""
    home = env.get("HOME") or (env.get("USERPROFILE") if IS_WINDOWS else "") or str(HOME)
    files = [(".netrc", "~/.netrc")] + ([("_netrc", "%USERPROFILE%\\_netrc")] if IS_WINDOWS else [])
    for file_name, shown in files:
        path = Path(home) / file_name
        if not path.is_file():
            continue
        try:
            words = path.read_text(encoding="utf-8", errors="replace").split()
        except OSError:
            return (f"the runner can't read {shown}, so it can't check that curl wouldn't sign in to {hostname} "
                    f"with it instead of git account '{name}'")
        words = [w.strip('"').lower() for w in words]
        for i, word in enumerate(words):
            if word == "machine" and words[i + 1:i + 2] == [hostname.lower()]:
                return (f"{shown} has a login for {hostname}, which git would use instead of git account '{name}'. "
                        f"Remove that entry from {shown}, or pick None for the job.")
            if word == "default":
                return (f"{shown} has a default login, which git would use for {hostname} instead of git account "
                        f"'{name}'. Remove that entry from {shown}, or pick None for the job.")
    return ""


def git_account_env(account: dict, folder: Path, base_env: dict[str, str],
                    name: str = "") -> tuple[dict[str, str], list[str]]:
    """What a job, clone or pull with a git account (as git_accounts gives it) gets: (the environment
    additions, the secrets to mask in what it prints). Its files go in `folder`, the job's private folder: an
    ssh config and an askpass program for every account (see above), and gh's or glab's folder. `base_env` is
    the environment the additions go into: its GIT_CONFIG_COUNT entries are kept and appended to, its PATH
    says which git is checked, and its HOME where curl's netrc file is. The caller also removes
    GIT_ACCOUNT_UNSET from the environment. `name` is the account's name, for
    messages. GitAccountProblem when the account can't be applied (git older than 2.31, a key file that isn't
    there, a netrc entry curl would sign in with: git_netrc_problem); nothing is written then."""
    version = git_version(base_env.get("PATH", os.defpath))
    if version is None or version < GIT_MIN_VERSION:
        have = f"{version[0]}.{version[1]}" if version else "no git"
        raise GitAccountProblem(f"git accounts need git 2.31 or later; this machine has {have}")
    key = account.get("ssh_key")
    if key and not os.path.isfile(key):
        raise GitAccountProblem(f"the ssh key file of git account '{name}' doesn't exist")
    count = base_env.get("GIT_CONFIG_COUNT") or ""
    if count and not re.fullmatch(r"[0-9]{1,9}", count):
        raise GitAccountProblem("GIT_CONFIG_COUNT in this job's environment isn't a whole number, so git would "
                                "refuse every command")
    host = account["host"]
    hostname, _, port = host.partition(":")
    netrc = git_netrc_problem(base_env, hostname, name)
    if netrc:
        raise GitAccountProblem(netrc)
    ssh_lines = [f"Host {hostname}", f"  HostName {hostname}", *([f"  Port {port}"] if port else [])]
    if key:
        # ssh reads %x in an IdentityFile as a token: %% is a percent sign.
        key_text = _ssh_path(key, f"the ssh key path of git account '{name}'").replace("%", "%%")
        ssh_lines += [f'  IdentityFile "{key_text}"', "  IdentitiesOnly yes"]
    else:
        # No key: no way to sign in to this host over ssh, so an ssh remote fails rather than use the machine
        # user's own key, ssh agent or password.
        ssh_lines += ["  IdentitiesOnly yes", "  IdentityFile none", "  IdentityAgent none",
                      *(f"  {option} no" for option in SSH_SIGN_IN_OPTIONS)]
    # The machine user's own settings, then the system's, for every other host. Not for this one: an IdentityFile
    # there (a "Host *" block often has one) would add a key ssh tries when the account's is refused. Named from
    # ~, as ssh finds it (from its own HOME), and written even when there is no such file, which ssh skips: Git
    # for Windows' ssh reads a full path such as C:/Users/... as one under ~/.ssh.
    ssh_lines += [f"Host * !{hostname}", '  Include "~/.ssh/config"']
    if not IS_WINDOWS and SYSTEM_SSH_CONFIG.is_file():
        ssh_lines.append(f'  Include "{_ssh_path(SYSTEM_SSH_CONFIG, "the system ssh config")}"')
    ssh_config = folder / "ssh_config"
    ssh_command = f'ssh -F "{_ssh_path(ssh_config, "the job folder", shell=True)}"'

    env: dict[str, str] = {}
    secrets: list[str] = []
    for field, variables in (("name", ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME")),
                             ("email", ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"))):
        if account.get(field):
            env.update(dict.fromkeys(variables, account[field]))
    # The empty value clears the helpers set before it for this host (the machine's, a repository's own). The host
    # name only: the account's port is for ssh, and this entry also matches https://<host>:443.
    helper = f"credential.https://{hostname}.helper"
    entries = [(helper, "")]
    token = account.get("token")
    if token:
        user = account.get("user") or "x-access-token"
        entries.append((helper, GIT_CREDENTIAL_HELPER))
        env.update(BULLPEN_GIT_USER=user, BULLPEN_GIT_TOKEN=token)
        # git sends the two as HTTP Basic auth, which its trace shows when told not to hide it.
        secrets += [token, base64.b64encode(f"{user}:{token}".encode()).decode()]
    # http:// gets no helper at all, and neither scheme an extra header set before (an Authorization header in a
    # repository's config, say): for http.<url>.extraHeader, an empty value clears the list.
    entries += [(f"credential.http://{hostname}.helper", ""), (f"http.https://{hostname}/.extraHeader", ""),
                (f"http.http://{hostname}/.extraHeader", "")]
    first = int(count or 0)
    for i, (key_name, value) in enumerate(entries, first):
        env[f"GIT_CONFIG_KEY_{i}"] = key_name
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    env["GIT_CONFIG_COUNT"] = str(first + len(entries))

    write_private(ssh_config, "\n".join(ssh_lines) + "\n")
    env["GIT_SSH_COMMAND"] = ssh_command
    # An askpass program that answers nothing, before core.askPass and SSH_ASKPASS (and with no terminal to ask):
    # a password git can't get from the account's helper is an empty one, so the host refuses it.
    askpass = folder / ("askpass.cmd" if IS_WINDOWS else "askpass")
    write_private(askpass, "@exit /b 0\r\n" if IS_WINDOWS else "#!/bin/sh\nexit 0\n")
    if not IS_WINDOWS:
        os.chmod(askpass, 0o700)
    env.update(GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0")
    kind = account.get("kind")
    if kind not in ("github", "gitlab"):
        return env, secrets
    tool_dir = folder / ("gh" if kind == "github" else "glab")
    tool_dir.mkdir(mode=0o700, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(tool_dir, 0o700)
    # Without a token, the stand-in: gh and glab skip an empty variable, and gh would then read the keychain.
    if kind == "github":
        env["GH_CONFIG_DIR"] = str(tool_dir)
        token_vars = GH_TOKEN_VARS if hostname == "github.com" or hostname.endswith(".ghe.com") \
            else GH_ENTERPRISE_TOKEN_VARS
        env[token_vars[0]] = token or GH_NO_TOKEN
        if hostname != "github.com":
            env["GH_HOST"] = hostname
    else:
        # glab reads the first config.yml it finds, this folder's first: without one it would read the
        # machine user's own.
        write_private(tool_dir / "config.yml", "")
        env["GLAB_CONFIG_DIR"] = str(tool_dir)
        token_vars = GLAB_TOKEN_VARS
        env.update(GITLAB_TOKEN=token or GH_NO_TOKEN, GITLAB_HOST=hostname)
    if not token:
        env.update({var: "" for var in token_vars[1:] if base_env.get(var)})
    return env, secrets


def git_account_notes(account: dict, name: str, base_env: dict[str, str], tools: bool = True) -> list[str]:
    """What the log of a job (`tools`), clone or pull with git account `name` says about it, besides its name:
    a GIT_CONFIG_PARAMETERS in `base_env` that was left out, and (for a job) gh or glab signed out, for a github
    or gitlab account without a token. Never a value."""
    notes = []
    if "GIT_CONFIG_PARAMETERS" in base_env:
        notes.append("⚠ GIT_CONFIG_PARAMETERS (settings given with git -c) was left out of the environment. Git "
                     "reads it after the git account's settings, so its credential helpers would come back.")
    tool = {"github": "gh", "gitlab": "glab"}.get(account.get("kind") or "")
    if tools and tool and not account.get("token"):
        notes.append(f"{tool} is signed out for this job: git account '{name}' has no token.")
    return notes


def url_host(url: str) -> str | None:
    """The host a clone URL points at, lowercase, and its port when the URL gives one other than the scheme's
    own (443 for https, 22 for ssh): https://host[:port]/..., ssh://[user@]host[:port]/... and user@host:path.
    None for any other form (a local path, http://, git://), which a clone or pull with a git account refuses."""
    text = (url or "").strip()
    m = re.match(r"(?i)(https|ssh)://([^/?#]+)(?:[/?#]|$)", text)
    if m:
        host = m.group(2).rsplit("@", 1)[-1].lower()
        default = ":443" if m.group(1).lower() == "https" else ":22"
        if host.endswith(default):
            host = host[:-len(default)]
    else:
        m = re.match(r"[^@/:\s]+@([^/:\s]+):", text)
        if not m:
            return None
        host = m.group(1).lower()
    return host if GIT_HOST_RE.fullmatch(host) else None


def git_account_url_problem(url: str, name: str, account: dict) -> str:
    """Why a clone or pull from `url` (the URL git uses, after its url.<base>.insteadOf settings) can't use git
    account `name`; "" when it can. Never holds the URL. The account's port is its ssh port: an https:// URL
    must be on its host name (port 443), an ssh one on its host and port (22 when it gives none)."""
    if url_has_userinfo(url):
        return (f"the URL holds credentials, which git would use instead of git account '{name}'. "
                + NO_URL_CREDENTIALS)
    there = url_host(url)
    if there is None:
        return (f"the runner can't tell which host this URL is on, so it can't use git account '{name}' (for "
                f"{account['host']}) with it")
    want = git_account_url_host(url, account)
    if there != want:
        return f"this URL is on {there}, but git account '{name}' is for {want}"
    if "ssh_key" not in account and re.match(r"(?i)https://", url.strip()) is None:
        return f"git account '{name}' has no ssh key: use the https URL"
    return ""


def git_account_url_host(url: str, account: dict) -> str:
    """The host (as url_host gives it) a URL must be on to be the host of `account`: its host name for https://
    (port 443), its host and port for ssh (none for port 22)."""
    hostname, _, port = account["host"].partition(":")
    https = re.match(r"(?i)https://", url.strip()) is not None
    return hostname if https or port in ("", "22") else account["host"]


def check_git_credential_urls(cwd: str, env: dict[str, str], name: str, account: dict) -> None:
    """Before a job with git account `name` starts: GitAccountProblem when a URL git would use in `cwd` with the
    job's `env` holds credentials for the account's host (url_host, compared as git_account_url_problem does).
    git uses those before any credential helper, so they would stand in for the account. Looked at: the targets
    of url.<base>.insteadOf and pushInsteadOf (the machine's settings, and the repository's when `cwd` is in
    one), and each remote's URLs as git uses them, after those settings, to fetch and to push. A folder outside
    a repository has no remotes. The message never holds a URL."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=30, stdin=subprocess.DEVNULL,
                              creationflags=0x08000000 if IS_WINDOWS else 0)

    def credentials_for(url: str) -> str:
        """The account's host when `url` holds credentials and is on it, else "". http:// counts like https:// (git
        would send them there just the same), and a URL with credentials but no host at all counts too: as a
        url.<base>.insteadOf target (https://user:pw@ for https://) it stands in for the start of URLs on every host,
        the account's included."""
        text = url.strip()
        if not url_has_userinfo(text):
            return ""
        m = URL_AUTHORITY_RE.match(text)
        scheme = m.group(1).lower() if m else ""
        if scheme not in ("http", "https", "ssh"):
            there = url_host(text)
            return there if there and there == git_account_url_host(text, account) else ""
        authority = text[len(m.group(1)) + 1:].lstrip("/").split("/", 1)[0]
        host = authority.rpartition("@")[2].lower()
        if not host:
            return account["host"]
        hostname, _, port = host.partition(":")
        if scheme in ("http", "https"):
            return account["host"] if hostname == account["host"].partition(":")[0].lower() else ""
        return account["host"] if (hostname if port in ("", "22") else host) == git_account_url_host(text, account) \
            else ""

    why = f", which git would use instead of git account '{name}'. " + NO_URL_CREDENTIALS
    try:
        done = git("config", "-z", "--get-regexp", r"^url\..*\.(push)?insteadof$")
        if done.returncode not in (0, 1):  # 1: no such setting
            raise GitAccountProblem(f"git can't read its settings here, so the runner can't check the URLs it would "
                                    f"use with git account '{name}'")
        for item in done.stdout.split("\0"):
            key = item.split("\n", 1)[0]
            setting = "pushInsteadOf" if key.endswith(".pushinsteadof") else "insteadOf"
            there = credentials_for(key[len("url."):-len(setting) - 1]) if key.startswith("url.") else ""
            if there:
                raise GitAccountProblem(f"a url.<base>.{setting} setting here gives URLs credentials for {there}" + why)
        remotes = git("remote")
        for remote in remotes.stdout.split() if remotes.returncode == 0 else []:
            for push in ((), ("--push",)):
                urls = git("remote", "get-url", "--all", *push, remote)
                for url in urls.stdout.splitlines() if urls.returncode == 0 else []:
                    there = credentials_for(url.strip())
                    if there:
                        raise GitAccountProblem(f"remote '{_shown(remote)}' has credentials for {there} in its URL"
                                                + why)
    except (OSError, subprocess.TimeoutExpired) as err:
        raise GitAccountProblem(f"the runner couldn't ask git which URLs it would use here with git account "
                                f"'{name}' ({type(err).__name__})") from None


def git_clone_url(url: str, folder: Path) -> str | None:
    """The URL git clone fetches `url` from: after the machine's url.<base>.insteadOf settings, which can send
    it to another host or over ssh (git ls-remote --get-url, which doesn't contact it). Asked in `folder`, an
    empty folder, and no higher: git clone reads no repository's own settings, so the one the runner happens to
    run in mustn't count. Without GIT_ACCOUNT_UNSET, as the clone runs. None when git can't say."""
    env = {k: v for k, v in os.environ.items() if k not in GIT_ACCOUNT_UNSET}
    try:
        done = subprocess.run(["git", "ls-remote", "--get-url", "--", url], cwd=str(folder), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60,
                              env=dict(env, GIT_CEILING_DIRECTORIES=str(folder.parent)),
                              creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (done.stdout.strip() or None) if done.returncode == 0 else None


def git_ssh_program_note(cwd: str | Path, env: dict[str, str], alone: bool = False) -> str:
    """The note for the log of a job, clone or pull with a git account when git in `cwd` with `env` is set to
    run an ssh command of its own (core.sshCommand, or GIT_SSH: Windows' own OpenSSH for its ssh agent, plink
    for Pageant). The account's GIT_SSH_COMMAND comes before both, so git runs the ssh on its PATH instead. It
    doesn't run theirs: its options could add a key of the machine user's own. `alone`: `cwd` is a clone's
    empty folder, and no repository above it counts. "" when neither is set. Never holds their values."""
    names = []
    try:
        done = subprocess.run(["git", "config", "--get", "core.sshCommand"], cwd=str(cwd), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=30,
                              env=dict(env, GIT_CEILING_DIRECTORIES=str(Path(cwd).parent)) if alone else env,
                              creationflags=0x08000000 if IS_WINDOWS else 0)
        if done.returncode == 0 and done.stdout.strip():
            names.append("core.sshCommand")
    except (OSError, subprocess.TimeoutExpired):
        pass
    if env.get("GIT_SSH"):
        names.append("GIT_SSH")
    if not names:
        return ""
    return (f"⚠ git is set here to use an ssh command of its own ({', '.join(names)}). With a git account, git "
            "uses the ssh on its PATH with the account's settings instead, so an ssh key that only that "
            "command's ssh agent holds can't be used.")


# ------------------------------------------------------------- job tools
# A job that needs more than knowing who it is (an 'ask' job, or one that may hand out subtasks)
# comes with a job token: it lets the agent use the hub's job tools at <hub>/mcp/job, as that job
# only, while the job runs. Ordinary jobs get none, and their command line doesn't change. The
# token goes to the agent in a private per-job file (an MCP config file for Claude Code, a settings
# file for Gemini CLI), never on a command line, and is masked in the job's log.
JOB_SERVER = "bullpenjob"         # the MCP server name agents see (no underscore: Gemini splits on it)
JOBS_DIR = BASE_DIR / "jobs"      # one private folder per job that needs one (job_folder), removed afterwards
JOB_DIR_MAX_AGE = 24 * 3600       # leftovers (from a runner that was killed) are removed at start-up
JOB_TOOL_RE = re.compile(r"^[a-z][a-z_]{0,39}$")
MCP_PROTOCOL_VERSION = "2025-11-25"


def job_folder(job_id: int) -> Path:
    """A job's private folder, JOBS_DIR/<id> (0700), made when first needed: for its job tools' settings, its
    git account's ssh config and gh or glab folder. The job removes it when it ends; clean_job_dirs removes
    what a runner that was stopped left behind."""
    folder = JOBS_DIR / str(int(job_id))
    folder.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        os.chmod(JOBS_DIR, 0o700)
        os.chmod(folder, 0o700)
    return folder


def job_server_entry(cfg: dict, token: str, who: str, timeout_ms: int) -> dict:
    """The job tools as one MCP server entry (Claude Code's format)."""
    who = re.sub(r"[^\x20-\x7e]", "?", who)[:100]  # HTTP header values must be plain ASCII
    return {"type": "http", "url": cfg["hub_url"].rstrip("/") + "/mcp/job",
            "headers": {"Authorization": "Bearer " + token, "X-Bullpen-Agent": who, **cf_access_headers(cfg)},
            # Per-server limit for one call: an approval may wait for the owner's answer.
            "timeout": timeout_ms, "alwaysLoad": True}


def job_tool_list(job: dict) -> list[str]:
    """The job tools the hub says this job may use (names checked, so they are safe in arguments)."""
    tools = job.get("job_tools")
    if not isinstance(tools, list):
        return []
    return [t for t in tools if isinstance(t, str) and JOB_TOOL_RE.match(t)]


def allowed_job_tool_names(tools: list[str]) -> list[str]:
    """The job tools an agent may use without asking. Never `approve`: Claude Code finds its
    permission prompt tool among the connected tools, and the model mustn't call it itself."""
    return [f"mcp__{JOB_SERVER}__{t}" for t in tools if t != "approve"]


def add_allowed_tools(argv: list[str], flag: str, names: list[str]) -> list[str]:
    """`argv` (a level's flags) with `names` pre-approved: right after the first `flag`, in either of
    its spellings (Claude Code's --allowedTools takes several values), or as `flag` and the names at
    the end."""
    names = [n for n in names if n and n != f"mcp__{JOB_SERVER}__approve"]
    out = list(argv)
    if not names or not flag:
        return out
    same = [i for i, word in enumerate(out) if flag_spelling(str(word)) == flag_spelling(flag)]
    if same:
        at = same[0] + 1
        return out[:at] + names + out[at:]
    return out + [flag, *names]


def write_private(path: Path, text: str) -> None:
    """Write a file only its owner can read, from the first byte (like save_config). The text goes
    to a temporary file next to it, which then replaces the file in one step, so a reader (or a
    crash halfway) never finds it empty or half written."""
    _write_private(path, text)


def write_private_bytes(path: Path, data: bytes) -> None:
    """write_private for bytes, written exactly as they are (no line-ending change on Windows)."""
    _write_private(path, data)


def _write_private(path: Path, content: str | bytes) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    binary = getattr(os, "O_BINARY", 0) if isinstance(content, bytes) else 0
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | binary, 0o600)
    try:
        with (os.fdopen(fd, "wb") if isinstance(content, bytes) else os.fdopen(fd, "w", encoding="utf-8")) as fh:
            fh.write(content)
        if not IS_WINDOWS:
            os.chmod(tmp, 0o600)  # in case a leftover existed with wider permissions
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows refuses to replace a file another thread or program has open at that
                # moment (a reader of this very file); it is closed again within milliseconds.
                if not IS_WINDOWS or attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_private(path: Path) -> str:
    """Read a file write_private writes. Windows refuses to open a file for a moment while another
    thread or program replaces it or has it open (write_private retries the writer's side of this),
    so a refused read is tried again there, as often as write_private tries."""
    for _ in range(19):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:
            if not IS_WINDOWS:
                raise
            time.sleep(0.05)
    return path.read_text(encoding="utf-8")  # the 20th try; its error is raised


INSTALL_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def install_id(path: Optional[Path] = None) -> str:
    """This install's own id: a random value made once and kept in ~/.bullpen/install-id. The hub
    uses it to tell computers apart that check in under one name; a host name can't do that, because
    it changes by itself on some computers (a Mac's, with the network). '' when the file can't be
    read or written: the hub then compares host names, as before. A copy of ~/.bullpen on another
    computer carries the id along, so delete install-id there (guide 5)."""
    path = path or BASE_DIR / "install-id"
    for attempt in range(2):
        try:
            found = read_private(path).strip()
        except FileNotFoundError:
            found = ""
        except (OSError, UnicodeError) as err:
            log.warning("couldn't read %s (%s); the hub tells this computer apart by its host name", path, err)
            return ""
        if INSTALL_ID_RE.match(found):
            return found
        if attempt:
            break
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_private(path, uuid.uuid4().hex + "\n")
        except OSError as err:
            log.warning("couldn't write %s (%s); the hub tells this computer apart by its host name", path, err)
            return ""
        # Read back what is there, so two processes that made one at the same moment agree on it.
    return ""


# Gemini CLI reads its settings in layers: its own defaults, then the system-defaults file, the
# user's and the project's settings, and last the system settings an administrator may set. A job
# that may hand out subtasks gets its own copy of the first file layer with the job tools added,
# so every other layer (and the user's own MCP servers, which merge by name) stays as it was.
# Gemini CLI 0.60 and later read a system file only when root owns it and every folder above it
# (gemini_settings_problem), so this works only for a runner that runs as root.
GEMINI_DEFAULTS_VAR = "GEMINI_CLI_SYSTEM_DEFAULTS_PATH"
GEMINI_SETTINGS_VAR = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"
GEMINI_JOB_DEFAULTS = "gemini-system-defaults.json"  # the copy's name in the job's folder
GEMINI_TOOL_TIMEOUT_MS = 120_000                      # one job-tool call (task_status waits up to 60 s)
GEMINI_DEFAULTS_MAX_BYTES = 1_000_000                 # a bigger system-defaults file isn't copied


def gemini_system_defaults_path(env: dict, windows: bool = IS_WINDOWS, macos: bool = IS_MACOS) -> str:
    """The system-defaults file Gemini CLI itself would read with this environment:
    GEMINI_CLI_SYSTEM_DEFAULTS_PATH, else system-defaults.json next to the system settings file
    (GEMINI_CLI_SYSTEM_SETTINGS_PATH, or the platform's own place for it)."""
    own = env.get(GEMINI_DEFAULTS_VAR)
    if own:
        return str(own)
    settings = env.get(GEMINI_SETTINGS_VAR) or (
        "C:\\ProgramData\\gemini-cli\\settings.json" if windows
        else "/Library/Application Support/GeminiCli/settings.json" if macos
        else "/etc/gemini-cli/settings.json")
    paths = ntpath if windows else posixpath
    return paths.join(paths.dirname(str(settings)), "system-defaults.json")


class JobToolsUnavailable(Exception):
    """The agent can't be handed the job tools for this job; the message says why."""


# Why Gemini CLI won't read a job's settings file, for the job's log ("⚠ Subtask tools unavailable: ...").
GEMINI_SKIPS = ("Gemini CLI reads a system-defaults file only when root (on Windows, an administrator or SYSTEM) "
                "owns it and every folder above it")
# Gemini CLI's own words on stderr when it skips such a file.
GEMINI_SKIPPED_RE = re.compile(r"Security Warning: Skipping system defaults file")


def gemini_settings_problem(path: str, windows: bool = IS_WINDOWS, stat_fn: Callable[[str], Any] = os.stat,
                            lstat_fn: Callable[[str], Any] = os.lstat,
                            realpath: Callable[[str], str] = os.path.realpath) -> str | None:
    """Why Gemini CLI (0.60 and later) would skip a system settings or system-defaults file at `path`,
    or None when it would read it. A copy of its own check (isFileAndDirectorySecureSync in its
    packages/core/src/utils/security.ts): on Linux and macOS the file and every folder above it, also
    where a link leads, must be owned by root and not writable by group or others, and a link on the
    way must be root's. On Windows they must belong to an administrator or SYSTEM with no other
    writers, which the runner doesn't check, so it counts as skipped there."""
    if windows:
        return "the runner can't check that on Windows"
    path = posixpath.abspath(path)
    paths = [path]
    real = realpath(path)
    if real != path:
        paths.append(posixpath.abspath(real))
    for target in paths:
        folders = []
        folder = posixpath.dirname(target)
        while True:
            folders.append(folder)
            parent = posixpath.dirname(folder)
            if parent == folder:
                break
            folder = parent
        for item in folders + [target]:
            try:
                link = lstat_fn(item)
                if (link.st_mode & 0o170000) == 0o120000 and link.st_uid != 0:
                    return f"{item} is a link not owned by root"
                st = stat_fn(item)
            except FileNotFoundError:
                continue  # nothing there to read
            except OSError as err:
                return f"couldn't check {item} ({err.strerror or err})"
            if st.st_uid != 0:
                return f"{item} is not owned by root"
            if st.st_mode & 0o022:
                return f"{item} is writable by group or others"
    return None


def gemini_job_tools_problem() -> str | None:
    """Why this runner can't hand Gemini CLI the job tools, or None when it can. The job's copy of the
    settings is written by the runner under JOBS_DIR, so it is owned by the user the runner runs as."""
    if IS_WINDOWS:
        return gemini_settings_problem("", windows=True)
    if os.geteuid() != 0:
        return "the runner doesn't run as root"
    folder = JOBS_DIR
    while not folder.exists() and folder.parent != folder:
        folder = folder.parent  # the folders the runner will make are root's, 0700
    return gemini_settings_problem(str(folder))


def gemini_server_entry(cfg: dict, token: str, who: str, tools: list[str]) -> dict:
    """The job tools as one MCP server entry in Gemini CLI's format. trust: headless Gemini calls
    them without asking; includeTools: only this job's tools, and never approve (Gemini has no
    permission prompt tool, and with trust it would call it unasked)."""
    base = job_server_entry(cfg, token, who, GEMINI_TOOL_TIMEOUT_MS)
    return {"url": base["url"], "type": "http", "headers": base["headers"], "trust": True,
            "timeout": GEMINI_TOOL_TIMEOUT_MS, "includeTools": [t for t in tools if t != "approve"]}


def gemini_job_defaults(source: str, entry: dict) -> dict:
    """A job's system-defaults settings: a copy of the machine's own file at `source` ({} when
    there is none or it can't be read), with the job tools set as mcpServers.bullpenjob."""
    data: Any = {}
    try:
        path = Path(source)
        if path.is_file() and path.stat().st_size <= GEMINI_DEFAULTS_MAX_BYTES:
            data = load_jsonc(path)[0]
    except (OSError, ValueError):  # unreadable, or not JSON (UnicodeDecodeError is a ValueError)
        data = {}
    if not isinstance(data, dict):
        data = {}
    servers = data.get("mcpServers")
    data["mcpServers"] = {**(servers if isinstance(servers, dict) else {}), JOB_SERVER: entry}
    return data


def prepare_job_tools(cfg: dict, spec: dict, job: dict, machine: str) -> tuple[list[str], dict[str, str], Path | None]:
    """(arguments to add after the autonomy flags, environment additions, folder to delete
    afterwards). Empty when the hub sent no job token or this agent can't take the job tools.
    JobToolsUnavailable when Gemini CLI would skip the job's settings file (it would also skip the
    machine's own defaults then, so the variable is never pointed at one it skips)."""
    token = job.get("job_token")
    tools = job_tool_list(job)
    mode = spec.get("job_mcp")
    if not isinstance(token, str) or not token or not tools or mode not in JOB_MCP_MODES:
        return [], {}, None
    template = spec.get("mcp_config_args")
    if mode == "mcp-config" and (not isinstance(template, list) or not template):
        return [], {}, None
    if mode == "gemini-settings":
        problem = gemini_job_tools_problem()
        if problem:
            raise JobToolsUnavailable(f"{GEMINI_SKIPS}, and {problem} (guide 9)")
    folder = job_folder(int(job["id"]))
    who = job_who(spec, job, machine)
    if mode == "gemini-settings":
        # Copied from the file this agent would read without the job tools (its own variable, the
        # runner's, or Gemini's place for it), so the machine's defaults still apply.
        source = gemini_system_defaults_path(build_env(spec))
        settings = gemini_job_defaults(source, gemini_server_entry(cfg, token, who, tools))
        target = folder / GEMINI_JOB_DEFAULTS
        write_private(target, json.dumps(settings, indent=2) + "\n")
        problem = gemini_settings_problem(str(target))
        if problem:
            shutil.rmtree(folder, ignore_errors=True)
            raise JobToolsUnavailable(f"{GEMINI_SKIPS}, and {problem} (guide 9)")
        return [], {GEMINI_DEFAULTS_VAR: str(target)}, folder
    config = folder / "mcp.json"
    write_private(config, json.dumps({"mcpServers": {JOB_SERVER: job_tools_server(cfg, job, who)}}, indent=2) + "\n")
    args = [str(a).replace("{job_mcp_config}", str(config)) for a in template]
    return args, {}, folder


def job_who(spec: dict, job: dict, machine: str) -> str:
    """Who a job's MCP calls come from (their X-Bullpen-Agent header): "<agent>@<machine> job <id>"."""
    return f"{spec.get('name') or job.get('agent') or 'agent'}@{machine} job {job['id']}"


def job_tools_server(cfg: dict, job: dict, who: str) -> dict:
    """The job tools' entry in a job's --mcp-config file. One call may take as long as an approval may wait."""
    try:
        wait_s = int(job.get("approval_timeout_s") or 600)
    except (TypeError, ValueError):
        wait_s = 600
    timeout_ms = (max(wait_s, 0) + 60) * 1000 if "approve" in job_tool_list(job) else 120_000
    return job_server_entry(cfg, job["job_token"], who, timeout_ms)


# What a job's error adds when a task picks MCP servers before the runner has synced with the hub.
NO_SERVER_LIST = "this runner hasn't had the hub's list of MCP servers yet"


def job_mcp_picks(job: dict) -> tuple[list, list]:
    """The MCP servers a task picked, as the hub sent them: (options.mcp_add, options.mcp_only). An absent or
    empty list picks none (an empty one is how a follow-up turns an inherited choice off). A value that isn't
    a list counts as one name, which prepare_job_mcp refuses unless this machine has a server by that name."""
    options = job.get("options") if isinstance(job.get("options"), dict) else {}

    def names(value: Any) -> list:
        return [] if value is None else list(value) if isinstance(value, list) else [value]

    return names(options.get("mcp_add")), names(options.get("mcp_only"))


def takes_mcp_picks(spec: dict) -> bool:
    """Whether a task's picked MCP servers can be handed to this agent: Claude Code's way, in a --mcp-config
    file (mcp_config_args). Gemini CLI can't add a server for one job unless the runner runs as root."""
    template = spec.get("mcp_config_args")
    return spec.get("parser") == "claude-stream" and isinstance(template, list) and bool(template)


# Words that name a secret in an MCP server's settings, besides SECRET_NAME_RE's (which known_secrets and the
# command-line masking share, so it stays as it is).
MCP_SECRET_WORDS = ("pin", "pwd", "pass", "passphrase", "sig", "signature", "session", "sid", "otp")
MCP_FLAG_RE = re.compile(r"--?([A-Za-z0-9][A-Za-z0-9_.-]*)(=(.*))?", re.S)  # -x, --name, --name=value


def mcp_name_is_secret(name: Any) -> bool:
    """Whether the name of an MCP server's setting (an environment variable, a query value's name, a flag without
    its dashes) says its value is a secret: SECRET_NAME_RE finds a secret's word in it, or one of MCP_SECRET_WORDS
    is a whole word of it (the name split on _ - . and where a lower-case letter meets an upper-case one, compared
    in lower case), so PIN, DB_PWD and sessionId count and SPIN, OPTIONS and SIGNAL don't."""
    name = str(name)
    words = re.split(r"[_.-]|(?<=[a-z])(?=[A-Z])", name)
    return bool(SECRET_NAME_RE.search(name)) or any(word.lower() in MCP_SECRET_WORDS for word in words)


class StandAloneSecret(str):
    """A short secret that Job.mask hides only where it stands alone (no ASCII letter or digit touching it), so it
    leaves longer words and numbers be: one that is all digits, one only MCP_SECRET_WORDS calls a secret (PIN=1234),
    or a header value that is one only because it is a header value (X-Pin: 99). Job.mask hides any other value in
    mask_values wherever it is."""
    __slots__ = ()


def has_letter_or_digit(value: str) -> bool:
    """Whether a short `value` can be a secret at all: one with no letter or digit (*, */*, -, .) never is, and
    masking it would scramble markdown and the *** left by values masked before it (ruling G13c). Callers ask only
    for values shorter than KNOWN_SECRET_MIN: a longer one is a secret whatever it holds (ruling G13d)."""
    return any(ch.isalnum() for ch in value)


def mcp_entry_secrets(entries: dict, hub_url: str = "") -> list[str]:
    """The secrets in a job's MCP server entries, for masking its log, its answer and its diff (Job.mask): every
    header value but the agent's name (a "Bearer <token>" value's token also on its own), every environment value
    and a URL's query values, a URL's user and password and path segments, and a stdio server's arguments (each
    whole, and what follows "="). Most of them count from KNOWN_SECRET_MIN characters up, as in known_secrets:
    shorter ones are mostly settings and words ("1", "info", "true", "s", "-y"). Shorter ones count too for a URL's
    password and a header value (but true or false), for an environment or query value whose name says it's a
    secret (mcp_name_is_secret; digits too, but not true or false), and for the argument after a flag named like a
    secret and what follows "=" in one (--api-key ab12c, --api-key=ab12c; not true or false, and not a next argument
    that is a flag itself, as in --token -y). Such a short one is a strong secret,
    masked wherever it is, when it is a URL's password or its name is one SECRET_NAME_RE knows (Authorization,
    Proxy-Authorization, Cookie, X-Api-Key, API_KEY, --token), unless it is all digits; the rest (all digits, a name
    only MCP_SECRET_WORDS knows, any other header) come as StandAloneSecret. A value of spaces alone, or a short
    one with no letter or digit (*, */*, -, .), never counts; a longer one counts whatever else it holds. The URL of the hub's own entries (`bullpen` and the servers it relays, at hub_url's
    /mcp) has nothing secret in it, and its path ends in the server's name."""
    found: list[str] = []
    words: list[str] = []

    def secret(value: str, strong: bool) -> None:
        found.append(value if strong and not re.fullmatch(r"[0-9]+", value) else StandAloneSecret(value))

    def named(name: Any, value: str) -> None:
        if value.lower() in ("true", "false") or not mcp_name_is_secret(name):
            words.append(value)
        else:
            secret(value, bool(SECRET_NAME_RE.search(str(name))))

    hub_mcp = hub_url.rstrip("/") + "/mcp" if hub_url else ""
    for entry in entries.values():
        for key, value in (entry.get("headers") or {}).items():
            if str(key).lower() != "x-bullpen-agent":
                rest = str(value).partition(" ")[2].strip()  # a "Bearer <token>" value's token
                for part in (str(value), rest):
                    if part.lower() not in ("true", "false"):
                        # SECRET_NAME_RE knows Authorization, Proxy-Authorization and Cookie (auth, cookie).
                        secret(part, bool(SECRET_NAME_RE.search(str(key))))
        for key, value in (entry.get("env") or {}).items():
            named(key, str(value))
        url = str(entry.get("url") or "")
        if url and not (hub_mcp and (url == hub_mcp or url.startswith(hub_mcp + "/"))):
            m = URL_AUTHORITY_RE.match(url)
            # The user and password as url_has_userinfo reads them: up to the first "/" after the slashes, so a
            # password holding a raw ? or # counts too.
            userinfo = url[m.start(2):].partition("/")[0].rpartition("@")[0] if m else ""
            if userinfo:
                user, _, password = userinfo.partition(":")
                secret(password, True)
                words += [userinfo, user]
            path, _, query = url[m.end() if m else 0:].partition("#")[0].partition("?")
            for part in query.split("&"):
                name, eq, value = part.partition("=")
                if eq:
                    named(name, value)
                else:
                    words.append(name)
            words += path.split("/")
        args = [str(arg) for arg in entry.get("args") or []]
        for i, arg in enumerate(args):
            words += [arg, arg.partition("=")[2]]
            flag = MCP_FLAG_RE.fullmatch(arg)
            if flag and mcp_name_is_secret(flag.group(1)):
                # The value after "=", else the next argument unless that is a flag itself (--token -y); true and
                # false are settings, never a secret.
                after = [a for a in args[i + 1:i + 2] if not MCP_FLAG_RE.fullmatch(a)]
                for value in [flag.group(3)] if flag.group(2) else after:
                    if value.lower() not in ("true", "false"):
                        secret(value, bool(SECRET_NAME_RE.search(flag.group(1))))
    kept = [s for s in found if s] + [s for s in words if len(s) >= KNOWN_SECRET_MIN]
    return [s for s in kept if s.strip() and (len(s) >= KNOWN_SECRET_MIN or has_letter_or_digit(s))]


def prepare_job_mcp(cfg: dict, spec: dict, job: dict, machine: str,
                    context: dict | None) -> tuple[list[str], Path | None, list[str], list[str]]:
    """A job's MCP servers for Claude Code: (arguments to add after the autonomy flags, folder to delete
    afterwards, the names of the picked servers, the secrets in the file). For a task that picked servers, one
    mcp.json in the job's folder holds the job tools (when the hub sent a job token, as prepare_job_tools writes
    them), the servers it added (options.mcp_add) and, for a task narrowed to some servers (options.mcp_only),
    those plus the hub's own `bullpen`; a narrowed job also gets strict_mcp_args, so the agent uses no other
    server. The entries are built from `context` (the runner's last sync) by the code the machine-wide setup
    uses. Without picks it is prepare_job_tools' arguments and folder: the job tools only, as before (their
    environment additions are always empty for an agent that takes picks, so _task calls prepare_job_tools
    itself then). JobToolsUnavailable, before anything is written, when a picked server can't be given."""
    add, only = job_mcp_picks(job)
    if not add and not only:
        args, _env, folder = prepare_job_tools(cfg, spec, job, machine)
        return args, folder, [], []
    agent = spec.get("name") or job.get("agent") or "this agent"
    if not takes_mcp_picks(spec):
        raise JobToolsUnavailable(f"{agent} can't be given a task's MCP servers (only Claude Code agents can)")
    strict = spec.get("strict_mcp_args")
    if only and not (isinstance(strict, list) and strict):
        raise JobToolsUnavailable(f"{agent} can't be limited to the task's MCP servers: it has no strict_mcp_args "
                                  "in runner.json")
    picked: list = []
    for name in add + only:
        if name not in picked:
            picked.append(name)
    who = job_who(spec, job, machine)
    # The hub's own servers are never a pick: `bullpen` keeps the level's own tools, the job tools their own entry.
    usable = [n for n in picked if isinstance(n, str) and n not in (*RESERVED_SERVERS, JOB_SERVER)]
    entries = claude_server_entries(cfg, context, usable + (["bullpen"] if only else []), who) if context else {}
    for name in picked:
        if name not in usable or name not in entries:
            raise JobToolsUnavailable(f"MCP server '{_shown(name)}' isn't available on {machine}"
                                      + ("" if context else f" ({NO_SERVER_LIST})"))
    servers: dict[str, dict] = {}
    token = job.get("job_token")
    if isinstance(token, str) and token and job_tool_list(job) and spec.get("job_mcp") == "mcp-config":
        servers[JOB_SERVER] = job_tools_server(cfg, job, who)
    servers.update(entries)
    folder = job_folder(int(job["id"]))
    config = folder / "mcp.json"
    try:
        write_private(config, json.dumps({"mcpServers": servers}, indent=2) + "\n")
    except OSError:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    args = [str(a).replace("{job_mcp_config}", str(config)) for a in spec["mcp_config_args"]]
    if only:
        args += [str(a) for a in strict]
    return args, folder, picked, mcp_entry_secrets(entries, str(cfg.get("hub_url") or ""))


def mcp_post(url: str, headers: dict, message: dict, timeout: float) -> tuple[int, dict]:
    """One JSON-RPC message to an MCP server over HTTP. The answer may be JSON or a short
    event stream; the reply to this message is returned."""
    req = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "User-Agent": f"bullpen-runner/{VERSION}", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1_000_000).decode("utf-8", "replace")
            status = resp.status
            stream = resp.headers.get("content-type", "").startswith("text/event-stream")
    except urllib.error.HTTPError as err:
        return err.code, {}
    if not stream:
        data = json.loads(raw or "{}")
        return status, data if isinstance(data, dict) else {}
    reply: dict = {}
    for line in raw.splitlines():
        if line.startswith("data:") and line[5:].strip():
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("id") == message.get("id"):
                reply = event
    return status, reply


def check_job_tools(cfg: dict, token: str, timeout: float = 5) -> str | None:
    """One MCP tools/list POST to <hub>/mcp/job with the token. None when it answers, else the reason."""
    url = cfg["hub_url"].rstrip("/") + "/mcp/job"
    headers = {"Authorization": "Bearer " + token, **cf_access_headers(cfg)}
    try:
        status, body = mcp_post(url, headers, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                                timeout)
    except (OSError, ValueError) as err:
        return str(getattr(err, "reason", None) or err) or type(err).__name__
    if status != 200:
        return f"the hub answered {status}"
    if not isinstance((body.get("result") or {}).get("tools"), list):
        return "the hub's answer had no tool list"
    return None


def job_tools_unreachable(cfg: dict, reason: str) -> str:
    """Why an 'ask' job can't run: its agent can't use the permission prompt tool."""
    return (f"The agent can't reach the hub's job tools at {cfg['hub_url'].rstrip('/')}/mcp/job ({reason}), so it "
            "couldn't ask you before commands. Check that this machine can reach the hub address in runner.json.")


# Claude Code's words on stderr when it can't find its permission prompt tool among the MCP tools.
PROMPT_TOOL_MISSING = "passed via --permission-prompt-tool) not found"


def clean_job_dirs(max_age: float = JOB_DIR_MAX_AGE) -> None:
    """Remove job folders left behind by a runner that stopped in the middle of a job."""
    try:
        folders = list(JOBS_DIR.iterdir())
    except OSError:
        return
    for folder in folders:
        try:
            if folder.is_dir() and time.time() - folder.stat().st_mtime > max_age:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------- live sessions
# A live session keeps the agent's input open after the first prompt, so the owner can type more
# messages on the dashboard while the job runs (Claude Code with --input-format stream-json). Each
# message is one JSON line on the agent's input and one more turn; the hub hands the messages over
# in its answers to the log uploads, and gets each turn's answer back with the next upload.
LIVE_YOU_CHARS = 300          # how much of a message the job log shows
LIVE_END_GRACE = 30           # seconds to wait for the agent to exit after the time limit closed its input
LIVE_TURN_CHARS = 200_000     # an answer as the hub keeps it


def duration_words(seconds: float) -> str:
    """'15 minutes', '1 minute', '3 seconds'."""
    if seconds >= 60 and round(seconds) % 60 == 0:
        n = round(seconds / 60)
        return f"{n} minute" + ("" if n == 1 else "s")
    n = max(round(seconds), 1)
    return f"{n} second" + ("" if n == 1 else "s")


def live_idle_seconds(options: dict) -> float:
    """How long a live session waits for a message after an answer (BULLPEN_TEST_IDLE_SECONDS is
    for tests only)."""
    test = os.environ.get("BULLPEN_TEST_IDLE_SECONDS")
    if test:
        try:
            return max(float(test), 0.1)
        except ValueError:
            pass
    try:
        minutes = float(options.get("idle_min") or 15)
    except (TypeError, ValueError):
        minutes = 15.0
    return min(max(minutes, 1.0), 240.0) * 60


def user_message_line(text: str) -> bytes:
    """One stream-json user message, as Claude Code reads them on its input. Plain ASCII, so no
    platform can change it on the way (Windows would turn a text-mode \n into \r\n)."""
    return (json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}},
                       ensure_ascii=True) + "\n").encode("ascii")


class LiveSession:
    """The open input of a live session: writes the owner's messages to the agent one turn at a
    time, and closes it on End session, after the idle time, or at the time limit.

    Writes happen on a thread of their own, so a slow agent never holds up the watchdog."""

    def __init__(self, job: "Job", proc: subprocess.Popen, parser: Any, idle_s: float) -> None:
        self.job = job
        self.proc = proc
        self.parser = parser
        self.idle_s = idle_s
        self.lock = threading.RLock()
        self.queue: list[dict] = []    # messages from the hub not yet written, in id order
        self.seen: set = set()         # message ids already queued or written
        self.received_max = 0          # the highest message id written to the agent (after_message)
        self.turn_active = False       # the agent is answering
        self.open = True               # its input is still open
        self.end_requested = False
        self.last_result = time.monotonic()
        self.closed_at: float | None = None
        self.outbox: "queue.Queue[bytes | None]" = queue.Queue()
        threading.Thread(target=self._writer, name=f"live-{job.id}", daemon=True).start()

    def _writer(self) -> None:
        while True:
            item = self.outbox.get()
            try:
                if item is None:
                    self.proc.stdin.close()
                    return
                self.proc.stdin.buffer.write(item)
                self.proc.stdin.buffer.flush()
            except (OSError, ValueError):  # the agent has gone: the job ends on its own
                if item is None:
                    return

    def start(self, prompt: str) -> None:
        """The job's prompt is the first message (the job log already shows it)."""
        with self.lock:
            self.turn_active = True
            self.outbox.put(user_message_line(prompt))

    def write_user(self, text: str) -> None:
        """Write one message to the agent and show it in the job log."""
        lines = text.strip().splitlines() or [""]
        shown = lines[0][:LIVE_YOU_CHARS]
        if len(lines) > 1 or len(lines[0]) > LIVE_YOU_CHARS:
            shown += "…"
        self.job.emit(f"🧑 You: {shown}")
        self.outbox.put(user_message_line(text))

    def offer(self, messages: Any) -> None:
        """Messages from the hub's answer to a log upload. Repeats are ignored; nothing is taken
        once the session has closed (the hub marks those as not delivered)."""
        if not isinstance(messages, list):
            return
        with self.lock:
            if not self.open:
                return
            for m in messages:
                mid = m.get("id") if isinstance(m, dict) else None
                text = m.get("text") if isinstance(m, dict) else None
                if not isinstance(mid, int) or isinstance(mid, bool) or mid <= 0 or not isinstance(text, str):
                    continue
                if mid in self.seen or mid <= self.received_max:
                    continue
                self.seen.add(mid)
                self.queue.append({"id": mid, "text": text})
            self.queue.sort(key=lambda m: m["id"])

    def on_result(self) -> None:
        """The agent finished a turn."""
        with self.lock:
            self.turn_active = False
            self.last_result = time.monotonic()

    def request_end(self) -> None:
        """End session from the dashboard: messages already received are still answered first."""
        with self.lock:
            self.end_requested = True

    def tick(self) -> str | None:
        """Called by the watchdog about twice a second: between turns, write the next message, or
        close the session when it was ended or has been idle too long. The reason when it closed."""
        with self.lock:
            if not self.open or self.turn_active:
                return None
            if self.queue:
                message = self.queue.pop(0)
                self.turn_active = True
                self.received_max = max(self.received_max, message["id"])
                start_turn = getattr(self.parser, "start_turn", None)
                if start_turn is not None:
                    start_turn()
                self.write_user(message["text"])
                return None
            if self.end_requested:
                why = "Session ended from the dashboard."
            elif time.monotonic() - self.last_result > self.idle_s:
                why = f"Session ended after {duration_words(self.idle_s)} without a message."
            else:
                return None
        self.close(why)
        return why

    def close(self, why: str) -> None:
        """Close the agent's input once (it then finishes and exits); the job log says why."""
        with self.lock:
            if not self.open:
                return
            self.open = False
            self.closed_at = time.monotonic()
        if why:
            self.job.emit(f"⏹ {why}")
        self.outbox.put(None)


# A summary for the hub's shared memory, after a job whose task asked for one (options.summarize): the job's session
# is resumed once with this prompt (see Job._summarize), and the answer goes back with the result as "summary".
SUMMARY_PROMPT = ("Summarize this conversation for a shared notes file other AI agents will read. At most 8 short "
                  "lines: what was asked; what you did or found; files you changed; anything left open. Plain text, "
                  "no preamble.")
SUMMARY_TIMEOUT = 180     # seconds the summary turn may take
SUMMARY_MAX_CHARS = 2000  # a summary as the hub is sent it, masked first
# The session ids the summary turn resumes (UUIDs, in practice): never one that starts with "-", which the CLI would
# read as another flag after its resume flag.
SUMMARY_SESSION_RE = re.compile(r"[A-Za-z0-9._:][A-Za-z0-9._:-]{0,127}")


class Job:
    """One job: start the CLI, feed it the prompt, follow its output, report back."""

    def __init__(self, runner: "Runner", data: dict):
        self.runner = runner
        self.data = data
        self.id = data["id"]
        self.cancel = threading.Event()
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        # The job token, when the hub sent one: every log line and the answer are masked with it.
        token = data.get("job_token")
        self.token = token if isinstance(token, str) else ""
        # Exact secret values its log lines and answer are masked with too (longest first, see mask): a git
        # account's token, the secrets of its picked MCP servers, and the machine's other secrets, for a job with
        # either. Each wherever it is, but a short StandAloneSecret only where it stands alone. Empty for other jobs.
        self.mask_values: list[str] = []
        self.job_dir: Path | None = None  # this job's private folder (job_folder), removed when it ends
        self.live: LiveSession | None = None  # a live session's open input
        self.turns_pending: list[dict] = []   # a live session's answers not yet sent to the hub
        self.closed_sent = False              # the hub has been told the live session closed

    # -- log shipping ------------------------------------------------------
    def mask(self, text: str) -> str:
        """`text` without this job's secrets (ruling G13e). Every match of the job token, of anything shaped like one
        (JOB_TOKEN_RE, with or without a token) and of each value in mask_values, also as JSON writes it (a stdout
        line is masked while it is still raw JSON), is found in the text as it came. Overlapping and touching matches
        are merged and each merged stretch becomes one ***, so no value replaced first can hide another value, or
        the token, from the mask. A StandAloneSecret shorter than KNOWN_SECRET_MIN counts only where it stands alone
        (no ASCII letter or digit touching it), so a short PIN leaves longer words and numbers be; any other value
        counts wherever it is. A value of spaces alone, or a short one with no letter or digit (*, */*, -, .), is
        never a secret; one of KNOWN_SECRET_MIN characters or more is, whatever else it holds (rulings G13c, G13d)."""
        spans = [m.span() for m in JOB_TOKEN_RE.finditer(text)]
        needles = [(self.token, False)] if self.token else []
        for value in self.mask_values:
            if not value.strip() or (len(value) < KNOWN_SECRET_MIN and not has_letter_or_digit(value)):
                continue
            alone = isinstance(value, StandAloneSecret) and len(value) < KNOWN_SECRET_MIN
            for form in {str(value), json.dumps(value)[1:-1], json.dumps(value, ensure_ascii=False)[1:-1]}:
                needles.append((form, alone))
        for needle, alone in needles:
            if alone:
                spans += [m.span() for m in re.finditer(f"(?<![A-Za-z0-9]){re.escape(needle)}(?![A-Za-z0-9])", text)]
                continue
            at = text.find(needle)
            while at != -1:
                spans.append((at, at + len(needle)))
                at = text.find(needle, at + 1)
        if not spans:
            return text
        merged: list[list[int]] = []
        for start, stop in sorted(spans):
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], stop)
            else:
                merged.append([start, stop])
        pieces, last = [], 0
        for start, stop in merged:
            pieces += [text[last:start], "***"]
            last = stop
        return "".join(pieces) + text[last:]

    def emit(self, *lines: str) -> None:
        if self.token or self.mask_values:
            lines = tuple(self.mask(str(ln)) for ln in lines)
        with self.lock:
            self.lines.extend(lines)

    def ship_logs(self, force: bool = False) -> None:
        """Send new log lines. Also asks whether the job was cancelled, so an
        empty batch is sent every few seconds while an agent is quiet. A live session also
        sends its answers and which messages the agent has, and gets new messages back."""
        live = self.live
        with self.lock:
            batch, self.lines = self.lines, []
            turns = list(self.turns_pending)
        payload: dict[str, Any] = {"lines": batch}
        if live is not None:
            payload.update(after_message=live.received_max, turns=turns, session_open=live.open)
            if turns or (not live.open and not self.closed_sent):
                force = True
        if not batch and not force:
            return
        try:
            status, body = self.runner.hub.call(f"/api/runner/jobs/{self.id}/log", payload, timeout=20)
        except (OSError, ValueError) as err:
            log.warning("job %s: could not send logs (%s); will retry", self.id, err)
            with self.lock:
                self.lines = batch + self.lines
            return
        if status != 200:
            return
        if live is not None:
            with self.lock:
                del self.turns_pending[:len(turns)]  # a batch sent again after a lost answer adds nothing
            if not payload["session_open"]:
                self.closed_sent = True
        body = body if isinstance(body, dict) else {}
        if body.get("cancel"):
            self.cancel.set()
        if live is not None:
            live.offer(body.get("messages"))
            if body.get("end"):
                live.request_end()

    def _shipper(self, done: threading.Event) -> None:
        ticks = 0
        while not done.wait(1.0):
            ticks += 1
            live = self.live
            # Every second while a live session is open (its messages come with the answers).
            self.ship_logs(force=ticks % 3 == 0 or (live is not None and live.open))
        self.ship_logs()

    # -- the actual run ------------------------------------------------------
    def run(self) -> None:
        done = threading.Event()
        shipper = threading.Thread(target=self._shipper, args=(done,), daemon=True)
        shipper.start()
        outcome: dict[str, Any]
        try:
            outcome = self._execute()
        except Exception as err:  # never let one job take the runner down
            log.exception("job %s crashed", self.id)
            outcome = {"status": "error", "error": f"runner error: {err}"}
        if self.token or self.mask_values:
            for key in ("result", "error", "diff"):
                if isinstance(outcome.get(key), str):
                    outcome[key] = self.mask(outcome[key])
        # Check in now when the job changed what the dashboard shows (an admin action, or new plan-limit
        # figures), so it shows the change before the job reads "done" instead of up to 15 s later.
        limits = getattr(self.runner, "limits", None)
        changed = limits is not None and limits.changed.is_set()
        if (self.data.get("kind") == "admin" or changed) and hasattr(self.runner, "send_heartbeat"):
            try:
                self.runner.send_heartbeat()
            except Exception:
                log.exception("heartbeat after job %s failed", self.id)
        done.set()
        shipper.join(timeout=30)
        self._report(outcome)

    def _execute(self) -> dict:
        if self.data.get("kind") == "admin":
            return self._admin()
        try:
            return self._task()
        finally:
            if self.job_dir is not None:
                shutil.rmtree(self.job_dir, ignore_errors=True)  # the job token's config file

    def machine_name(self) -> str:
        # Runner stubs (`runner.py test`, the unit tests) have only cfg and hub.
        name = getattr(self.runner, "name", None)
        if callable(name):
            return name()
        return self.runner.cfg.get("name") or socket.gethostname()

    def _task(self) -> dict:
        cfg = self.runner.cfg
        options = self.data.get("options") or {}
        # A subtask an agent on another machine handed over runs only where the owner said yes.
        subtask = bool(options.get("delegated") or self.data.get("delegated_by"))
        if subtask and cfg.get("accept_delegated_tasks") is not True:
            return {"status": "error", "error": "This machine doesn't take subtasks from other agents "
                                                "(accept_delegated_tasks in its runner.json)."}
        agent = self.data["agent"]
        if agent not in cfg["agents"]:
            return {"status": "error", "error": f"agent '{agent}' is not configured on this machine"}
        project = self.data.get("project") or "chat"
        if project not in cfg["projects"]:
            return {"status": "error", "error": f"project '{project}' is not configured on this machine"}
        workdir = expand_path(cfg["projects"][project])
        if not os.path.isdir(workdir):
            return {"status": "error", "error": f"project folder {workdir} does not exist"}

        spec = agent_spec(cfg, agent)
        if subtask and "levels" not in agent_features(spec, build_env(spec)):
            # The hub sends subtasks only to agents that report 'levels'; this one stopped (its entry
            # changed) after the hub last heard from it.
            why = (f"permission flags outside its levels ({', '.join(spec['permission_flags'])})"
                   if spec.get("permission_flags") else "autonomy flags of its own"
                   if spec.get("autonomy_overridden") else "no levels the hub can check")
            return {"status": "error", "error": f"{agent} can't take subtasks here: its entry in runner.json has "
                                                f"{why}, so a read-only subtask might not only read. Remove them, "
                                                "or send the subtask to another agent."}
        autonomy = self.data.get("autonomy") or "readonly"
        if autonomy == "ask" and spec.get("permission_flags"):
            # The hub offers 'ask' only to agents that report 'approvals'; this one stopped (its entry
            # changed) after the hub last heard from it. Its flag would win, so nothing would ask.
            return {"status": "error", "error": f"{agent} can't ask you before commands here: its entry in runner.json "
                                                "has permission flags outside its levels "
                                                f"({', '.join(spec['permission_flags'])}), which would win over "
                                                "asking. Move them into its autonomy levels, or pick another level."}
        exe = resolve_command(spec)
        if exe is None:
            return {"status": "error", "error": f"could not find '{spec.get('command')}' on this machine"}

        model = (self.data.get("model") or spec.get("model_name") or "").strip()
        if model.startswith("-") or any(c.isspace() for c in model):
            return {"status": "error", "error": f"'{model}' isn't a valid model name"}
        resume = self.data.get("resume_session") or ""
        move = options.get("move") if isinstance(options.get("move"), dict) else {}
        if move.get("mode") == "exact":
            # "Continue on another machine": the session's transcript comes from the machine that ran it first.
            if not resume:
                return {"status": "error", "error": "The hub sent no session to continue for this move."}
            why = self._take_session(spec, build_env(spec), workdir, resume, str(move.get("copy") or ""))
            if why:
                return {"status": "error", "error": why}
        new_session_id = str(uuid.uuid4()) if spec.get("new_session") and not resume else ""
        values = {"{model}": model, "{session_id}": resume, "{workdir}": workdir, "{new_session_id}": new_session_id}

        def fill(args: list) -> list[str]:
            return fill_args(args, values)

        # A level this agent doesn't have fails the job: running it with other flags (read only, say)
        # would not be what was asked for.
        levels = spec.get("autonomy") or {}
        if not isinstance(levels.get(autonomy), list):
            return {"status": "error", "error": f"{agent} has no '{autonomy}' level in its settings."}
        level_args = fill(levels[autonomy])

        # The hub's job tools, when the hub sent a job token for this job.
        tools = job_tool_list(self.data)
        tool_args: list[str] = []
        tool_env: dict[str, str] = {}
        tool_note = ""
        if self.token:
            try:
                tool_args, tool_env, self.job_dir = prepare_job_tools(cfg, spec, self.data, self.machine_name())
            except JobToolsUnavailable as err:
                if autonomy == "ask":
                    return {"status": "error", "error": f"The agent can't be handed the job tools: {err}."}
                tool_note = f"⚠ Subtask tools unavailable: {err}"
            except OSError as err:
                if autonomy == "ask":
                    return {"status": "error", "error": f"Couldn't write the job tools' settings for the agent ({err})."}
                tool_note = f"⚠ Subtask tools unavailable: couldn't write their settings ({err})"
        if autonomy == "ask" and not tool_args:
            return {"status": "error", "error": "This job asks you before commands, but the hub sent no job tools "
                                                "(is the hub's MCP support on?)."}
        # Arguments (a --mcp-config file) or an environment variable (Gemini's defaults file).
        if (tool_args or tool_env) and any(t != "job_info" for t in tools):
            # Check before the agent starts that it will reach them: an agent that can't reach its
            # permission prompt tool stops at the first command that needs an answer.
            reason = check_job_tools(cfg, self.token)
            if reason and "approve" in tools:
                return {"status": "error", "error": job_tools_unreachable(cfg, reason)}
            if reason:
                tool_note = f"⚠ Subtask tools unavailable: {reason}"
                tool_args, tool_env = [], {}
        if tool_args:
            level_args = add_allowed_tools(level_args, str(spec.get("allow_tools_flag") or ""),
                                           allowed_job_tool_names(tools))
        has_job_tools = bool(tool_args)  # in a --mcp-config file (Claude Code's way)

        # The MCP servers the task picked (Claude Code), in one file with the job tools when they were handed
        # over above. Their tools are allowed at every level, read only included: "always, once picked", as the
        # owner decided. A subtask picks none. A server that can't be given fails the job before the agent starts.
        mcp_note = ""
        if not subtask and any(job_mcp_picks(self.data)):
            job = self.data if has_job_tools else {**self.data, "job_token": ""}
            try:
                tool_args, self.job_dir, picked, mcp_secrets = prepare_job_mcp(
                    cfg, spec, job, self.machine_name(), getattr(self.runner, "last_context", None))
            except JobToolsUnavailable as err:
                return {"status": "error", "error": str(err)}
            except OSError as err:
                return {"status": "error", "error": f"Couldn't write the MCP servers' settings for the agent ({err})."}
            level_args = add_allowed_tools(level_args, str(spec.get("allow_tools_flag") or ""),
                                           [f"mcp__{name}" for name in picked])
            mcp_note = f"MCP servers for this job: {', '.join(picked)}. Their tools are allowed even in Read only."
            # The hub's MCP token and the servers' own secrets, masked like the job token: each exact value that
            # mcp_entry_secrets counts, short ones too (known_secrets would leave out values under 6 characters;
            # Job.mask takes a short StandAloneSecret only where it stands alone, a strong one wherever it is).
            self.mask_values = known_secrets(cfg, spec) + [s for s in mcp_secrets if s]

        # A live session keeps the agent's input open for the owner's messages.
        live_note = ""
        live = bool(options.get("live") or self.data.get("live"))
        if live and "live" not in agent_features(spec, build_env(spec)):
            live = False
            live_note = "⚠ This agent can't keep a session open, so it answers once."

        # The order: command, args, live, the level's flags, the job tools, model, resume or new
        # session, extra_args (and the prompt, when it is an argument).
        argv = [exe, *fill(spec.get("args", []))]
        if live:
            argv += fill(spec["live"])
        argv += level_args
        argv += tool_args
        if model and spec.get("model"):
            argv += fill(spec["model"])
        if resume and spec.get("resume"):
            argv += fill(spec["resume"])
        elif new_session_id:
            argv += fill(spec["new_session"])
        argv += fill(spec.get("extra_args", []))

        # How the prompt reaches the CLI: on its input (most CLIs), or as an argument.
        prompt = self.data["prompt"]
        via = spec.get("prompt_via") or "stdin"
        if via not in ("stdin", "arg"):
            return {"status": "error", "error": f"prompt_via must be 'stdin' or 'arg', not '{via}'"}
        shown = list(argv)  # what the log shows: the prompt itself is left out
        if via == "arg":
            if any("{prompt}" in a for a in argv):
                shown = [a.replace("{prompt}", f"<prompt: {len(prompt)} characters>") for a in argv]
                argv = [a.replace("{prompt}", prompt) for a in argv]
            else:
                shown.append(f"<prompt: {len(prompt)} characters>")
                argv.append(prompt)
            if IS_WINDOWS and exe.lower().endswith((".cmd", ".bat")):
                # cmd.exe would read parts of the prompt (& | > and so on) as commands of its own.
                return {"status": "error", "error": f"{exe} is a .cmd/.bat launcher, which can't be given the "
                                                    "prompt as an argument safely. Point 'command' at the program "
                                                    "itself, or use prompt_via 'stdin'."}
            if IS_WINDOWS and len(subprocess.list2cmdline(argv)) > 32000:
                return {"status": "error", "error": "This prompt is too long to pass as an argument on Windows "
                                                    "(about 32,000 characters at most)."}

        env = build_env(spec)
        for key, value in ((spec.get("autonomy_env") or {}).get(autonomy) or {}).items():
            env[str(key)] = str(value)
        env.update(tool_env)
        # The git account: the one picked for the task, else the project's or the machine's default. A subtask
        # gets none. One that can't be applied fails the job here, before the agent starts.
        git_name, git_error, git_note = choose_git_account(cfg, spec, project, options.get("git_account"), subtask)
        if git_error:
            return {"status": "error", "error": git_error}
        git_notes: list[str] = []
        if git_name:
            account = git_accounts(cfg)[0][git_name]
            try:
                if self.job_dir is None:
                    self.job_dir = job_folder(int(self.id))
                git_env, git_secrets = git_account_env(account, self.job_dir, env, git_name)
            except GitAccountProblem as err:
                return {"status": "error", "error": str(err)}
            except OSError as err:
                return {"status": "error", "error": f"Couldn't write the settings of git account '{git_name}' for "
                                                    f"the job ({err})."}
            more = git_account_notes(account, git_name, env)
            for var in GIT_ACCOUNT_UNSET:
                env.pop(var, None)
            git_notes = [f"Git account: {git_name} ({account['host']})", git_ssh_program_note(workdir, env), *more]
            env.update(git_env)
            try:
                check_git_credential_urls(workdir, env, git_name, account)
            except GitAccountProblem as err:
                return {"status": "error", "error": str(err)}
            # Every exact git secret, however short (known_secrets leaves out values under 6 characters), after the
            # MCP file's secrets when the task picked servers (they include the machine's own).
            self.mask_values = (self.mask_values or known_secrets(cfg, spec)) + [s for s in git_secrets if s]
        env["PWD"] = workdir  # some CLIs read the folder from PWD rather than asking the OS

        timeout = float(options.get("timeout_min", 60)) * 60
        start_sha = None
        if options.get("capture_diff"):
            start_sha = (git(workdir, "rev-parse", "HEAD") or "").strip() or None

        self.emit(f"▶ {spec.get('label', agent)} in {project} ({workdir}) · autonomy: {autonomy}"
                  + (f" · model: {model}" if model else ""))
        for note in (*git_notes, git_note, tool_note, mcp_note, live_note):
            if note:
                self.emit(note)
        log.info("job %s: %s", self.id,
                 redact_argv(shown, known_secrets(cfg, spec, [self.token, *self.mask_values])))

        popen_kw: dict[str, Any] = {}
        if IS_WINDOWS:
            # New process group so we can stop the whole tree; no flashing console windows.
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        else:
            popen_kw["start_new_session"] = True

        parser = PARSERS[spec.get("parser", "text")]()
        if hasattr(parser, "api_key"):
            parser.api_key = claude_auth(env) == "api_key"  # "credit balance is too low" applies to these only
        limits = getattr(self.runner, "limits", None)  # runner.py test and the unit tests may have none
        if limits is not None and hasattr(parser, "on_limits"):
            parser.on_limits = lambda windows, overage, top: limits.record(agent, windows, overage, top)
        note_stderr = getattr(parser, "note_stderr", None)
        try:
            self.proc = subprocess.Popen(
                argv, cwd=workdir, env=env, stdin=subprocess.PIPE if via == "stdin" else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", bufsize=1, **popen_kw,
            )
        except OSError as err:
            return {"status": "error", "error": f"could not start {exe}: {err}"}

        def feed_stdin() -> None:
            if via != "stdin":
                return
            try:
                self.proc.stdin.write(prompt)
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

        stderr_tail: list[str] = []
        stop_reason: list[str] = []
        asks = "approve" in tools and has_job_tools  # the agent asks the owner through the job tools
        gemini_file = tool_env.get(GEMINI_DEFAULTS_VAR)  # the job's settings file, for Gemini CLI

        def tools_lost(reason: str) -> None:
            """An 'ask' job whose agent can't use the permission prompt tool stops at once: the
            agent would otherwise fail at its first command that needs an answer."""
            if not stop_reason:
                stop_reason.append("job_tools:" + reason)
                kill_tree(self.proc)

        # A job with secrets to mask masks each line as it comes, before anything cuts it short: a secret cut in
        # two (a tool's command shown up to 140 characters, say) would no longer match.
        masks = bool(self.token or self.mask_values)

        def read_stderr() -> None:
            for raw in self.proc.stderr:
                raw = raw.rstrip("\r\n")
                if masks:
                    raw = self.mask(raw)
                if raw.strip():
                    stderr_tail.append(raw)
                    del stderr_tail[:-40]
                    self.emit(f"stderr: {raw}")
                    if note_stderr is not None:
                        note_stderr(raw)
                    if gemini_file and GEMINI_SKIPPED_RE.search(raw) and gemini_file in raw:
                        self.emit("⚠ Subtask tools unavailable: Gemini CLI skipped the job's settings file "
                                  "(its warning is above)")
                    if asks and PROMPT_TOOL_MISSING in raw:
                        tools_lost("Claude Code couldn't find the approve tool")

        if live:
            idle = {"idle_min": options.get("idle_min") or self.data.get("idle_min")}
            self.live = LiveSession(self, self.proc, parser, live_idle_seconds(idle))
            self.live.start(prompt)
        else:
            threading.Thread(target=feed_stdin, daemon=True).start()
        err_thread = threading.Thread(target=read_stderr, daemon=True)
        err_thread.start()

        def watchdog() -> None:
            deadline = time.monotonic() + timeout
            session = self.live
            while self.proc.poll() is None:
                if self.cancel.is_set():
                    stop_reason.append("cancelled")
                    kill_tree(self.proc)
                    return
                if time.monotonic() > deadline:
                    if session is not None and session.open and not session.turn_active:
                        # Between turns: close the session and let the agent finish on its own.
                        session.close("Session ended at the job's time limit.")
                    if session is None or session.turn_active or session.closed_at is None \
                            or time.monotonic() > max(deadline, session.closed_at) + LIVE_END_GRACE:
                        stop_reason.append("timeout")
                        kill_tree(self.proc)
                        return
                elif session is not None:
                    session.tick()  # the next message, or the end of the session
                time.sleep(0.5)

        threading.Thread(target=watchdog, daemon=True).start()

        checked = False
        for raw in self.proc.stdout:
            raw = raw.rstrip("\r\n")
            if raw and masks:
                raw = self.mask(raw)
            if raw:
                before = len(getattr(parser, "turns", ()))
                lines = parser.feed(raw)
                if lines:
                    self.emit(*lines)
                if asks and not checked and getattr(parser, "started", False):
                    checked = True
                    status = getattr(parser, "mcp_servers", {}).get(JOB_SERVER)
                    if status in (None, "failed", "needs-auth"):
                        tools_lost("Claude Code couldn't connect to them" if status else
                                   "Claude Code started without them")
                if self.live is not None and len(getattr(parser, "turns", ())) > before:
                    turn = dict(parser.turns[-1])
                    if masks:
                        turn["result"] = self.mask(turn["result"])
                    turn["result"] = turn["result"][:LIVE_TURN_CHARS]
                    with self.lock:
                        self.turns_pending.append(turn)
                    self.live.on_result()
        code = self.proc.wait()
        if self.live is not None:
            self.live.close("")  # the agent has exited: no more messages (quietly: the outcome says why)
        err_thread.join(timeout=5)
        # Close the pipes now rather than whenever they are collected (stderr only once it is read).
        for pipe in (self.proc.stdout, None if err_thread.is_alive() else self.proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

        outcome: dict[str, Any] = {"exit_code": code, "session_id": parser.session_id or new_session_id or None,
                                   "result": parser.final("")}
        error_kind = None
        if options.get("capture_diff"):
            outcome["diff"] = capture_diff(workdir, start_sha, mask=self.mask if masks else None)

        if stop_reason and stop_reason[0] == "cancelled":
            outcome.update(status="cancelled", error="Cancelled from the dashboard.")
        elif stop_reason and stop_reason[0].startswith("job_tools:"):
            outcome.update(status="error", error=job_tools_unreachable(cfg, stop_reason[0].split(":", 1)[1]))
        elif stop_reason and stop_reason[0] == "timeout":
            outcome.update(status="error", error=f"Stopped after the {options.get('timeout_min', 60)} minute time limit.")
        elif parser.error:
            outcome.update(status="error", error=parser.error)
            error_kind = getattr(parser, "error_kind", None)
        elif code != 0:
            known = parser.exit_error() if hasattr(parser, "exit_error") else None
            tail = "\n".join(stderr_tail[-8:])
            if known:  # its own last lines too, so the real end isn't hidden if the guess is wrong
                error = known + (f"\n\nIts last lines (exit code {code}):\n{tail}" if tail else "")
            else:
                error = f"{spec.get('label', agent)} exited with code {code}.\n{tail}".strip()
            outcome.update(status="error", error=error)
            error_kind = getattr(parser, "error_kind", None) if known else None
        elif not (outcome["result"] or "").strip():
            outcome.update(status="error", error="The agent finished but returned no answer. Check the log.")
        else:
            outcome["status"] = "done"
        tracker = getattr(parser, "usage", None)
        # A summary for shared memory when the task asked for one (never a subtask's), once a live session has
        # closed. Nothing about it can change how the job ended: a summary that isn't written is one log line.
        if outcome["status"] == "done" and options.get("summarize") is True and not subtask:
            # Gemini CLI: only the id it reported in this run. It reads a hub's "latest" or a number as the folder's
            # newest session or one by index, which may be another job's.
            summary_session = (parser.session_id if spec.get("parser") == "gemini-stream"
                               else outcome["session_id"] or resume)
            try:
                summary = self._summarize(spec, exe, model, summary_session, workdir, tracker)
            except Exception as err:
                log.exception("job %s: the summary turn failed", self.id)
                self.emit(f"Summary not written: the runner hit an error ({type(err).__name__}).")
                summary = None
            if summary:
                outcome["summary"] = summary
        # Tokens and cost as the agent reported them (also for a job that was stopped: they were spent; a summary
        # turn's too), and what kind of error it was when the hub shows it in its own way (a usage or quota limit).
        usage = tracker.total() if tracker is not None else None
        if usage:
            outcome["usage"] = usage
        if error_kind in ERROR_KINDS:
            outcome["error_kind"] = error_kind
        return outcome

    def _summarize(self, spec: dict, exe: str, model: str, session: str, workdir: str,
                   tracker: UsageTracker | None) -> str | None:
        """The summary turn: the job's `session` resumed once with SUMMARY_PROMPT, the agent's readonly level and the
        job's model, in its folder, with the agent's own arguments and environment and nothing a job adds (no live
        input, job tools, MCP picks, new session or git account: as for a job with git account "-"), and no MCP servers
        at all when the agent has strict_mcp_args. Its answer, masked with this job's mask (ruling G13e; the
        machine's known secrets for a job with no values of its own) before it is cut to SUMMARY_MAX_CHARS; None when
        there is none, with one log line that says why in the runner's own words. For Gemini CLI, "latest" and a bare
        number are no session. Its tokens go into `tracker`, the job's."""
        limit = f"{SUMMARY_TIMEOUT / 60:g} minutes" if SUMMARY_TIMEOUT >= 60 else f"{SUMMARY_TIMEOUT:g} seconds"

        def not_written(reason: str) -> None:
            self.emit(f"Summary not written: {reason}")

        if not can_summarize(spec):  # its entry changed after the hub last heard from it
            if spec.get("permission_flags"):
                return not_written(f"{spec['label']} has permission flags outside its levels in runner.json "
                                   f"({', '.join(spec['permission_flags'])}), so a readonly turn might not only read.")
            return not_written(f"{spec['label']} can't resume the job's session by its id with a readonly level here.")
        if spec.get("parser") == "gemini-stream" and isinstance(session, str) \
                and (session.lower() == "latest" or session.isdigit()):
            session = ""  # to Gemini CLI, the newest session in the folder or one by its index: not an id
        if not session:
            return not_written("the agent reported no session to continue.")
        if not isinstance(session, str) or not SUMMARY_SESSION_RE.fullmatch(session):
            return not_written("the job's session id isn't a plain id (letters, digits, '.', '_', ':' and '-').")
        values = {"{model}": model, "{session_id}": session, "{workdir}": workdir, "{new_session_id}": ""}
        argv = [exe, *fill_args(spec.get("args", []), values), *fill_args(spec["autonomy"]["readonly"], values)]
        # No MCP server at all where the agent can be limited to a --mcp-config file it isn't given (Claude Code's
        # --strict-mcp-config): a summary needs no tools, and a task narrowed to some servers (mcp_only) must not
        # reach the machine's others in its summary.
        if isinstance(spec.get("strict_mcp_args"), list):
            argv += [str(a) for a in spec["strict_mcp_args"]]
        if model and spec.get("model"):
            argv += fill_args(spec["model"], values)
        argv += fill_args(spec["resume"], values) + fill_args(spec.get("extra_args", []), values)
        prompt = SUMMARY_PROMPT
        if (spec.get("prompt_via") or "stdin") == "arg":  # as the job passed its prompt
            if any("{prompt}" in a for a in argv):
                argv = [a.replace("{prompt}", prompt) for a in argv]
            else:
                argv.append(prompt)
            prompt = ""
        env = build_env(spec)
        for key, value in ((spec.get("autonomy_env") or {}).get("readonly") or {}).items():
            env[str(key)] = str(value)
        env["PWD"] = workdir
        self.emit(f"Writing a summary for shared memory (up to {limit}).")
        log.info("job %s: summary turn: %s", self.id,
                 redact_argv(argv, known_secrets(self.runner.cfg, spec, [self.token, *self.mask_values])))
        try:
            code, out, _, stopped = run_capture(argv, workdir, env, prompt, SUMMARY_TIMEOUT, self.cancel)
        except OSError as err:
            return not_written(f"couldn't start {exe} ({err}).")
        parser = PARSERS[spec.get("parser", "text")]()
        for raw in out.splitlines():
            if raw:
                parser.feed(raw)
        used = getattr(parser, "usage", None)
        if tracker is not None and used is not None:
            tracker.add_run(used)  # spent whatever came of it
        if stopped == "cancelled":
            return not_written("the job was cancelled.")
        if stopped == "timeout":
            return not_written(f"no answer within {limit}.")
        if parser.error:
            return not_written("the agent reported an error.")
        if code != 0:
            return not_written(f"the agent exited with code {code}.")
        answer = parser.final("")
        # A job with no exact values of its own (no git account, no MCP picks) masks its summary with the machine's
        # known secrets too: the summary goes to shared memory and other tasks' prompts. Put back afterwards, for the
        # job's result, error and diff (masked in run()).
        saved = self.mask_values
        self.mask_values = saved or known_secrets(self.runner.cfg, spec)
        try:
            summary = self.mask(answer.strip())[:SUMMARY_MAX_CHARS].rstrip() if isinstance(answer, str) else ""
        finally:
            self.mask_values = saved
        if not summary:
            return not_written("the agent's answer was empty.")
        return summary

    # -- admin actions sent from the dashboard ----------------------------------
    def _admin(self) -> dict:
        opts = self.data.get("options") or {}
        action = opts.get("action", "")
        params = opts.get("params") or {}
        cfg = self.runner.cfg
        if action.startswith("project."):
            if not cfg.get("allow_remote_projects"):
                return {"status": "error", "error": "This machine doesn't allow managing projects remotely."}
            return self._project(action, params)
        if action == "runner.update":
            if not cfg.get("allow_remote_update"):
                return {"status": "error", "error": "This machine doesn't allow remote updates."}
            return self._update()
        if action == "sync":
            return self.runner.sync_from_hub(self.emit)
        if action == "usage.check":
            return self._usage_check(params if isinstance(params, dict) else {})
        if action == "auth.logout":
            return self._sign_out(params if isinstance(params, dict) else {})
        if action == "session.export":
            return self._session_export(params if isinstance(params, dict) else {})
        if action in ("rc.start", "rc.stop"):
            rc = getattr(self.runner, "rc", None)
            params = params if isinstance(params, dict) else {}
            agent, project = str(params.get("agent") or ""), str(params.get("project") or "")
            if rc is None:
                return {"status": "error", "error": "This runner can't run Remote Control."}
            if action == "rc.stop":  # always allowed, whatever runner.json says now
                return rc.stop(agent, project, why="stopped from the dashboard")
            if not rc_allowed(cfg):
                return {"status": "error", "error": f"{self.machine_name()} doesn't allow Remote Control from the "
                                                    "dashboard (allow_remote_control in its runner.json)."}
            # "Start again when the runner starts": the hub sends it only to runners that report they can.
            keep = str(params.get("start_with_runner") or "").strip().lower() in ("1", "true")
            return rc.start(agent, project, self.emit, keep=keep)
        return {"status": "error", "error": f"unknown action '{action}' (is this runner up to date?)"}

    # -- "Sign out": the agent's own "auth logout", only where runner.json allows it ---------------------
    def _sign_out(self, params: dict) -> dict:
        cfg = self.runner.cfg
        machine = self.machine_name()
        agent = str(params.get("agent") or "")
        if not sign_out_allowed(cfg):
            return {"status": "error", "error": f"{machine} doesn't allow signing agents out from the dashboard "
                                                "(allow_remote_sign_out in its runner.json)."}
        if agent not in (cfg.get("agents") or {}):
            return {"status": "error", "error": f"{machine} has no agent called '{agent}'."}
        spec = agent_spec(cfg, agent)
        env = build_env(spec)
        if "sign_out" not in agent_features(spec, env):
            return {"status": "error", "error": f"{agent} on {machine} has no claude.ai login to sign out of."}
        busy_agents = getattr(self.runner, "busy_agents", None)
        if callable(busy_agents) and agent in busy_agents():
            return {"status": "error", "error": f"{agent} is running a job on {machine}. Sign it out once the job "
                                                "has finished, or cancel the job first."}
        rc = getattr(self.runner, "rc", None)
        if rc is not None and any(e.get("agent") == agent and e.get("state") in ("starting", "running")
                                  for e in rc.view()):
            return {"status": "error", "error": f"Remote Control is running for {agent} on {machine}. Stop it "
                                                "first."}
        store = getattr(self.runner, "sign_in", None)
        lock = store.agent_lock(agent) if store is not None else None
        # Never at the same time as a status check of this agent: wait for one that is running.
        if lock is not None and not lock.acquire(timeout=SIGN_IN_TIMEOUT + 30):
            return {"status": "error", "error": f"{agent}'s sign-in check on {machine} didn't finish, so it wasn't "
                                                "signed out. Try again in a few minutes."}
        try:
            self.emit(f"▶ {cli_command(spec, env)} auth logout")
            worked, text = sign_out(spec, env)
            if text:
                self.emit(text)
            now = store.refresh_held(agent, spec, env) if store is not None and sign_in_applies(spec, env) else None
        finally:
            if lock is not None:
                lock.release()
        state = {"in": "still signed in", "out": "signed out"}.get(str((now or {}).get("state")),
                                                                     "not known (the CLI didn't say)")
        if not worked:
            return {"status": "error", "error": f"Signing {agent} out on {machine} didn't work. Its sign-in is "
                                                f"{state} now." + (f"\n\n{text}" if text else "")}
        return {"status": "done", "result": f"{agent} on {machine} is {state} now. To sign in again, run "
                                            f"'{cli_command(spec, env)} auth login' on {machine} itself."}

    # -- "Continue on another machine", the exact session: send its transcript to the hub ---------------
    def _session_export(self, params: dict) -> dict:
        """Read one session's transcript from the agent's config folder (find_session_transcript: that file only)
        and send it to the hub, which hands it once to the machine the owner picked. Its content never goes to the
        job's log."""
        cfg = self.runner.cfg
        agent, project = str(params.get("agent") or ""), str(params.get("project") or "")
        session, copy_id = str(params.get("session_id") or ""), str(params.get("copy") or "")
        if agent not in cfg["agents"]:
            return {"status": "error", "error": f"agent '{agent}' is not configured on this machine"}
        if project not in cfg["projects"]:
            return {"status": "error", "error": f"project '{project}' is not configured on this machine"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", copy_id):
            return {"status": "error", "error": "the hub sent no copy to fill (is the hub up to date?)"}
        spec = agent_spec(cfg, agent)
        env = build_env(spec)
        if not takes_session_moves(spec, env):
            return {"status": "error", "error": f"{agent}'s sessions can't move (only Claude Code agents' can)"}
        try:
            limit = min(int(params.get("max_bytes") or SESSION_MOVE_MAX_BYTES), SESSION_MOVE_MAX_BYTES)
        except (TypeError, ValueError):
            limit = SESSION_MOVE_MAX_BYTES
        try:
            path = find_session_transcript(session_config_dir(spec, env), expand_path(cfg["projects"][project]),
                                           session, env)
            data = read_session_transcript(path, limit)
        except SessionMoveError as err:
            why = str(err)
            return {"status": "error", "error": f"{why[:1].upper()}{why[1:]}."}
        digest = hashlib.sha256(data).hexdigest()
        self.emit(f"Sending session {session} ({max(1, len(data) // 1024)} KB) to the hub.")
        try:
            status, body, _ = self.runner.hub.call_bytes(f"/api/runner/session-copies/{copy_id}", data,
                                                        headers={"X-Bullpen-Sha256": digest})
        except OSError as err:
            return {"status": "error", "error": f"The hub couldn't be reached to send the session ({err})."}
        if status != 200:
            why = body.decode("utf-8", "replace").strip() or f"HTTP {status}"
            return {"status": "error", "error": f"The hub didn't take the session: {why}"}
        return {"status": "done", "result": f"Sent session {session} ({max(1, len(data) // 1024)} KB) to the hub, "
                                            "for the machine you picked. It stays on this machine too."}

    def _take_session(self, spec: dict, env: dict, workdir: str, session: str, copy_id: str) -> str:
        """An exact move: take the session's transcript from the hub (once), check it, and write it where this
        machine's agent finds it (install_session_transcript). Returns an error, or ''."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", copy_id or ""):
            return "The hub sent no session copy for this move."
        if not takes_session_moves(spec, env):
            return f"{spec.get('name')}'s sessions can't move here (only Claude Code agents' can)."
        try:
            status, data, headers = self.runner.hub.call_bytes(
                f"/api/runner/session-copies/{copy_id}/take", json.dumps({"job_id": self.id}).encode(),
                content_type="application/json")
        except OSError as err:
            return f"The hub couldn't be reached for the session ({err})."
        if status != 200:
            return f"The session couldn't be taken from the hub: {data.decode('utf-8', 'replace').strip()}"
        if len(data) > SESSION_MOVE_MAX_BYTES or hashlib.sha256(data).hexdigest() != headers.get("x-bullpen-sha256"):
            return "The session arrived damaged (its checksum doesn't match). Move it again."
        try:
            install_session_transcript(session_config_dir(spec, env), workdir, session, data, env)
        except SessionMoveError as err:
            return f"The session couldn't be written here: {err}."
        self.emit(f"Copied session {session} here ({max(1, len(data) // 1024)} KB); continuing it.")
        return ""

    # -- "Check limits": ask Claude Code itself (/usage), under each agent's own login ----------------
    def _usage_check(self, params: dict) -> dict:
        cfg = self.runner.cfg
        wanted = params.get("agents") or ""
        wanted = ",".join(str(a) for a in wanted) if isinstance(wanted, list) else str(wanted)
        names = [n.strip() for n in wanted.split(",") if n.strip()]
        if not names:
            names = [a for a in cfg["agents"]
                     if "usage_check" in agent_features(agent_spec(cfg, a), build_env(agent_spec(cfg, a)))]
        if not names:
            return {"status": "error", "error": "No agent on this machine can check plan limits."}
        rows: list[str] = []
        notes: list[str] = []
        failures: list[str] = []
        for agent in dict.fromkeys(names):
            if self.cancel.is_set():
                return {"status": "cancelled", "error": "Cancelled from the dashboard."}
            got = self._usage_check_agent(agent)
            if got.get("failure"):
                failures.append(got["failure"])
                self.emit(f"✖ {got['failure']}")
                continue
            windows = got.get("windows") or {}
            if windows:
                self.emit(f"{agent}: {usage_summary(windows)}")
            for wid in sorted(windows, key=window_order):
                w = windows[wid]
                used = f"{round(w['used'] * 100)}%" if w.get("used") is not None else "?"
                when = w.get("resets_text") or (format_utc(w.get("resets_at")) + " UTC" if w.get("resets_at") else "")
                cells = [agent, window_label(wid), used, when]
                rows.append("| " + " | ".join(c.replace("|", "/").replace("\n", " ") for c in cells) + " |")
            if got.get("note"):
                notes.append(got["note"])
                self.emit(got["note"])
        if self.cancel.is_set():
            return {"status": "cancelled", "error": "Cancelled from the dashboard."}
        if not rows and not notes:
            return {"status": "error", "error": "Couldn't check plan limits:\n" + "\n".join(f"- {f}" for f in failures)}
        parts = []
        if rows:
            parts.append("| Agent | Window | Used | Resets |\n|---|---|---|---|\n" + "\n".join(rows))
        parts += notes
        if failures:
            parts.append("\n".join(f"- {f}" for f in failures))
        return {"status": "done", "result": "\n\n".join(parts)}

    def _usage_check_agent(self, agent: str) -> dict:
        """Run one agent's CLI, unmodified, with /usage in print mode, and keep what it says.
        {"windows": {...}, "note": str} when it ran, {"failure": str} when it couldn't."""
        cfg = self.runner.cfg
        if agent not in cfg["agents"]:
            return {"failure": f"{agent}: there is no agent with that name on this machine"}
        spec = agent_spec(cfg, agent)
        env = build_env(spec)
        check = usage_check_settings(spec)
        if "usage_check" not in agent_features(spec, env) or check is None:
            auth = agent_auth(spec, env)
            why = ("it signs in with an API key, which has no plan limits" if auth == "api_key" else
                   "it signs in with a long-lived token (claude setup-token), which can't read plan limits"
                   if auth == "token" else "its settings have no usage_check")
            return {"failure": f"{agent} can't check plan limits: {why}"}
        exe = resolve_command(spec)
        if exe is None:
            return {"failure": f"{agent}: could not find '{spec.get('command')}' on this machine"}
        # The chat folder, so no project's own settings or hooks take part.
        chat = expand_path(cfg["projects"].get("chat") or "")
        workdir = chat if chat and os.path.isdir(chat) else str(make_chat_workspace())
        values = {"{model}": "", "{session_id}": "", "{workdir}": workdir, "{new_session_id}": ""}
        argv = [exe]
        for arg in check["args"]:
            for key, value in values.items():
                arg = arg.replace(key, value)
            argv.append(arg)
        env["PWD"] = workdir
        secrets_ = known_secrets(cfg, spec)
        log.info("job %s: %s", self.id, redact_argv(argv, secrets_))
        self.emit(f"▶ {agent}: asking {spec.get('label', agent)} for its plan usage ({check['prompt']})")
        try:
            code, out, err_lines, stopped = run_capture(argv, workdir, env, check["prompt"], USAGE_CHECK_TIMEOUT,
                                                        self.cancel)
        except OSError as err:
            return {"failure": f"{agent}: could not start {exe}: {err}"}
        for line in err_lines[-20:]:
            if line.strip():
                self.emit(f"stderr: {redact(line, secrets_)}")
        if stopped == "cancelled":
            return {"failure": f"{agent}: cancelled"}
        if stopped == "timeout":
            return {"failure": f"{agent}: no answer within {USAGE_CHECK_TIMEOUT} seconds"}
        now = time.time()
        limits = getattr(self.runner, "limits", None)
        report_found, report, texts = False, None, []
        for raw in out.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = None
            if not isinstance(ev, dict):
                texts.append(raw)
                continue
            kind = ev.get("type")
            message = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if kind == "rate_limit_event" and limits is not None:
                windows, overage, top = normalize_rate_limit(ev.get("rate_limit_info") or {}, now)
                limits.record(agent, windows, overage, top, now)
            if not report_found:
                for holder in (message, ev):
                    if "usage_report" in holder:
                        report_found, report = True, holder.get("usage_report")
                        break
            if kind == "assistant":
                for block in message.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                        texts.append(block["text"])
            if kind == "result" and isinstance(ev.get("result"), str):
                texts.append(ev["result"])
            for key in ("content", "output"):
                if isinstance(ev.get(key), str):
                    texts.append(ev[key])
        text = "\n".join(texts)
        windows = parse_usage_report(report, now) if report_found else {}
        if not windows:
            windows = parse_usage_text(text, now)
        if windows and limits is not None:
            limits.record(agent, windows, None, None, now)
        # A login that can't read plan usage (a setup-token token) gets a report without figures.
        no_usage = report_found and isinstance(report, dict) and "rate_limits" in report \
            and report["rate_limits"] is None
        if code != 0 and not windows and not no_usage:
            tail = " ".join(ln.strip() for ln in err_lines[-3:] if ln.strip())
            return {"failure": f"{agent}: Claude Code exited with code {code}" + (f" ({redact(tail, secrets_)[:300]})"
                                                                                  if tail else "")}
        if windows:
            return {"windows": windows}
        if no_usage:
            return {"windows": {}, "note": f"{agent}'s login can't read plan usage (Claude Code returned none). "
                                           f"A full login (\"{cli_command(spec, env)} auth login\") can."}
        note = (f"Claude Code printed nothing the hub could read as plan usage for {agent}. The bars keep the "
                "figures from its last job. (Claude Code may be limiting how often usage can be checked; try "
                "again later.)")
        limited = RATE_LIMITED_RE.search(text)
        if limited:
            note += f" Claude Code says it is rate limited until {limited.group(1).strip()[:80]}."
        return {"windows": {}, "note": note}

    def _run_logged(self, argv: list[str], cwd: str | None = None, timeout_s: float = 1200,
                    env_extra: dict[str, str] | None = None, secrets: list[str] | tuple = ()) -> int:
        """Run a helper command (git), streaming its output into the job log. env_extra: a git account's
        environment (GIT_ACCOUNT_UNSET is left out then); secrets: its values to mask in the log, however short,
        besides the ones the machine knows."""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        if env_extra:
            for var in GIT_ACCOUNT_UNSET:
                env.pop(var, None)
            env.update(env_extra)
        known = known_secrets(self.runner.cfg) + [s for s in secrets if s]
        kw: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000} \
            if IS_WINDOWS else {"start_new_session": True}
        self.emit("$ " + redact_argv(argv, known))  # a clone URL may carry a token
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", **kw)
        deadline = time.monotonic() + timeout_s

        def watchdog() -> None:
            while proc.poll() is None:
                if self.cancel.is_set() or time.monotonic() > deadline:
                    kill_tree(proc)
                    return
                time.sleep(0.5)

        threading.Thread(target=watchdog, daemon=True).start()
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            if line.strip():
                self.emit(redact(line, known))
        return proc.wait()

    def _git_account_run(self, project: str, params: dict, argv: list[str], url: Callable[[Path], str | None],
                         no_url: str, repo: str | None = None) -> tuple[int, str]:
        """Run a clone or pull (`argv`) of `project` with its git account: params "git_account", else the
        project's default, else the machine's; with none, as today. (exit code, "") when it ran, (-1, why)
        when the account can't be used (the URL git would use, which `url` gives, is on another host, say;
        `no_url` starts the message for a None from it). The account's settings are in this job's folder (which
        `url` gets) while git runs, and removed afterwards. `repo`: the repository a pull runs in; None for a
        clone, which runs in none."""
        cfg = self.runner.cfg
        name, error, _ = choose_git_account(cfg, None, project, params.get("git_account"))
        if error:
            return -1, error
        if name is None:
            return self._run_logged(argv), ""
        account = git_accounts(cfg)[0][name]
        base = {k: v for k, v in os.environ.items() if k not in GIT_ACCOUNT_UNSET}  # as _run_logged runs git
        try:
            try:
                folder = job_folder(int(self.id))
                env, secrets = git_account_env(account, folder, base, name)
            except GitAccountProblem as err:
                return -1, str(err)
            except OSError as err:
                return -1, f"couldn't write the settings of git account '{name}' ({err})"
            address = url(folder)
            if address is None:
                return -1, (f"{no_url}, so the runner can't check that it is on {account['host']}, the host of git "
                            f"account '{name}'")
            problem = git_account_url_problem(address, name, account)
            if problem:
                return -1, problem
            self.emit(f"Git account: {name} ({account['host']})")
            for note in (git_ssh_program_note(repo or folder, base, alone=repo is None),
                         *git_account_notes(account, name, dict(os.environ), tools=False)):
                if note:
                    self.emit(note)
            return self._run_logged(argv, env_extra=env, secrets=secrets), ""
        finally:
            shutil.rmtree(JOBS_DIR / str(int(self.id)), ignore_errors=True)

    def _project(self, action: str, params: dict) -> dict:
        name = (params.get("name") or "").strip()
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name):
            return {"status": "error", "error": f"'{name}' isn't a valid project name"}
        root = Path(expand_path(self.runner.cfg["projects_root"]))
        projects = self.runner.cfg["projects"]

        if action == "project.remove":
            if name == "chat" or name not in projects:
                return {"status": "error", "error": f"can't remove project '{name}'"}
            self.runner.update_config(lambda c: c["projects"].pop(name, None))
            return {"status": "done", "result": f"Project **{name}** is no longer offered. "
                                                f"Its files are still in `{expand_path(projects.get(name, ''))}`."}

        if action == "project.pull":
            if name not in projects:
                return {"status": "error", "error": f"no project called '{name}'"}
            path = expand_path(projects[name])

            def pull_url(_folder: Path) -> str | None:
                # The remote git pull fetches from: the current branch's (branch.<name>.remote), else the only one,
                # else origin; after url.<base>.insteadOf, and without GIT_ACCOUNT_UNSET, as the pull runs.
                env = {k: v for k, v in os.environ.items() if k not in GIT_ACCOUNT_UNSET}
                return (git(path, "ls-remote", "--get-url", env=env) or "").strip() or None

            code, problem = self._git_account_run(name, params, ["git", "-C", path, "pull", "--ff-only"], pull_url,
                                                  f"git can't say which remote '{name}' pulls from", repo=path)
            if problem:
                return {"status": "error", "error": problem}
            if self.cancel.is_set():
                return {"status": "cancelled", "error": "Cancelled from the dashboard."}
            return ({"status": "done", "result": f"Pulled the latest changes into **{name}**."} if code == 0 else
                    {"status": "error", "error": f"git pull failed (exit {code}); see the log."})

        if name in projects:
            return {"status": "error", "error": f"a project called '{name}' already exists"}
        target = root / name
        if action == "project.create":
            if target.exists() and any(target.iterdir()):
                return {"status": "error", "error": f"{target} already exists and isn't empty"}
            target.mkdir(parents=True, exist_ok=True)
            if params.get("git", "yes") != "no":
                self._run_logged(["git", "init", "-q", str(target)])
        elif action == "project.clone":
            url = (params.get("url") or "").strip()
            if not re.match(r"^(?:(?:https|ssh)://|git@)[^/\s]\S*$", url):
                return {"status": "error", "error": "use an https://, ssh:// or git@ address"}
            if url_has_userinfo(url):
                return {"status": "error", "error": NO_URL_CREDENTIALS}
            if target.exists():
                return {"status": "error", "error": f"{target} already exists"}
            root.mkdir(parents=True, exist_ok=True)
            code, problem = self._git_account_run(name, params, ["git", "clone", "--progress", "--", url, str(target)],
                                                  lambda folder: git_clone_url(url, folder),
                                                  "git can't say where this URL leads")
            if problem:
                return {"status": "error", "error": problem}
            if self.cancel.is_set():
                return {"status": "cancelled", "error": "Cancelled from the dashboard."}
            if code != 0:
                return {"status": "error", "error": f"git clone failed (exit {code}). For a private repository, "
                                                    "this machine needs git credentials (see the troubleshooting guide)."}
        else:
            return {"status": "error", "error": f"unknown project action '{action}'"}
        self.runner.update_config(lambda c: c["projects"].__setitem__(name, str(target)))
        return {"status": "done", "result": f"Project **{name}** is ready at `{target}`."}

    def _update(self) -> dict:
        status, body = self.runner.hub.call("/api/runner/bundle", {}, timeout=60)
        if status != 200 or not body or "runner.py" not in body.get("files", {}):
            return {"status": "error", "error": f"couldn't download the runner from the hub ({status})"}
        new_version = body.get("version") or "?"
        staged: dict[str, bytes] = {}
        for fname, f in body["files"].items():
            if not re.match(r"^[A-Za-z0-9_]+\.py$", fname):
                return {"status": "error", "error": f"refusing unexpected file name {fname!r}"}
            data = base64.b64decode(f["content"])
            if hashlib.sha256(data).hexdigest() != f.get("sha256"):
                return {"status": "error", "error": f"checksum mismatch for {fname}; nothing was changed"}
            staged[fname] = data
        try:
            compile(staged["runner.py"], "runner.py", "exec")
        except SyntaxError as err:
            return {"status": "error", "error": f"the new runner.py doesn't compile ({err}); nothing was changed"}
        for fname, data in staged.items():
            dest = HERE / fname
            if fname == "runner.py" and dest.exists():
                shutil.copy2(dest, HERE / "runner.py.bak")
            tmp = dest.with_suffix(".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            self.emit(f"updated {fname}")
        self.runner.restart_pending = True
        return {"status": "done", "result": f"Updated the runner from {VERSION} to {new_version}. "
                                            "It restarts as soon as no other job is running."}

    def _report(self, outcome: dict) -> None:
        for attempt in range(30):
            try:
                status, body = self.runner.hub.call(f"/api/runner/jobs/{self.id}/finish", outcome, timeout=30)
                if status in (200, 404):
                    return
                log.warning("job %s: hub answered %s when finishing: %s", self.id, status, body)
            except (OSError, ValueError) as err:
                log.warning("job %s: could not report result (%s)", self.id, err)
            time.sleep(min(60, 2 ** min(attempt, 6)))
        log.error("job %s: gave up reporting the result", self.id)


# ============================================================ Remote Control
# "Start Remote Control" on the machine page runs `claude remote-control` in a project folder, so
# the owner can drive Claude there from claude.ai/code or the Claude app (signed in to the same
# account). Only when runner.json says "allow_remote_control": true. Each server stops after
# remote_control_max_hours, on Stop, when the setting is turned off, and with the runner.
# "Start again when the runner starts" (start_with_runner) keeps a server's choice in remote-control.json: the
# next runner (after a reboot, say) starts it again once it has checked in, inside the same time limit (its
# hours still count from the first start). Stop, the time limit, the setting turned off, the hub refusing
# this machine and a start that fails all clear the choice, so nothing is tried over and over.
RC_STATE_FILE = BASE_DIR / "remote-control.json"
RC_URL_RE = re.compile(r"https://claude\.ai/\S+")
RC_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
RC_URL_WAIT = 15            # seconds Start waits for the server to show its address
RC_STOP_GRACE = 5           # seconds between asking a server to stop and killing it (Linux, macOS)
RC_KEEP_ENDED = 600         # seconds a failed or stopped server stays on the machine page
RC_TAIL_LINES = 30          # lines of its output kept (masked) for messages
RC_SHOWN_LINES = 5          # lines of that tail a message shows
RC_ERROR_SETTLE = 1         # seconds Start waits for more lines after an "Error:" line, before it stops the server
# A line Claude Code starts with "Error:" before it shows an address means the server didn't start, even
# when the process stays up for a while before it exits.
RC_ERROR_LINE_RE = re.compile(r"^\s*Error:")
RC_RESTART_WITHIN = 600     # seconds after an update in which the new runner starts them again
RC_MAX_HOURS = 8            # default remote_control_max_hours
RC_MAX_SERVERS = 4          # default max_remote_control
RC_HEARTBEATS_REFUSED = 3   # check-ins answered 401 in a row before every server is stopped
# Why Remote Control didn't start, from its last lines (checked in this order).
# Claude Code's own messages: https://code.claude.com/docs/en/remote-control#troubleshooting
RC_FLAG_VARS_RE = re.compile(r"\b(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC|DISABLE_GROWTHBOOK|"
                             r"CLAUDE_CODE_DISABLE_FEATURE_FLAGS)\b")
RC_FAILURES = [
    # `claude remote-control` already runs in a terminal in the same folder (it then waits a minute before it exits).
    ("busy", re.compile(r"(?i)already served by a terminal")),
    ("token",re.compile(r"(?i)full-scope login token|setup-token|CLAUDE_CODE_OAUTH_TOKEN")),
    # Couldn't reach the feature-flag service or read the organization's policy (offline, a proxy).
    ("unreachable", re.compile(r"(?i)couldn't verify (remote control eligibility|your organi[sz]ation's)|"
                               r"feature.flag service was unreachable")),
    # The account-level check (out of date after a plan change; the second wording is before 2.1.239).
    ("account", re.compile(r"(?i)isn't enabled for this account|not yet enabled for your account")),
    ("org", re.compile(r"(?i)disabled by your organi[sz]ation|not enabled for your organi[sz]ation|"
                       r"turned off by your organi[sz]ation")),
    ("flags", re.compile(r"(?i)feature.flag|growthbook|nonessential traffic")),
    ("other", re.compile(r"(?i)bedrock|vertex|foundry|first.party|ANTHROPIC_BASE_URL")),
    ("trust", re.compile(r"(?i)not trusted|trust this folder|workspace trust|enable remote control|"
                         r"at a terminal and answer")),
    ("api_key", re.compile(r"(?i)requires claude\.ai subscription|api key")),
    ("login", re.compile(r"(?i)not logged in|please run /login|not signed in")),
]


def rc_max_hours(cfg: dict) -> float:
    """remote_control_max_hours from runner.json, 1-72 (8 when absent or not a number)."""
    value = _num(cfg.get("remote_control_max_hours"))
    return RC_MAX_HOURS if value is None else min(max(value, 1.0), 72.0)


def rc_max_servers(cfg: dict) -> int:
    value = _num(cfg.get("max_remote_control"))
    return RC_MAX_SERVERS if value is None else int(min(max(value, 1), 16))


def rc_allowed(cfg: dict) -> bool:
    return cfg.get("allow_remote_control") is True


def sign_out_allowed(cfg: dict) -> bool:
    """runner.json "allow_remote_sign_out": may "Sign out" on the machine page sign an agent out here?"""
    return cfg.get("allow_remote_sign_out") is True


def hours_words(hours: float) -> str:
    n = int(hours) if float(hours).is_integer() else round(hours, 1)
    return f"{n} hour" + ("" if n == 1 else "s")


def other_service(env: dict) -> str:
    """Which other service a Claude agent uses instead of Anthropic's own."""
    for key, name in (("CLAUDE_CODE_USE_BEDROCK", "Bedrock"), ("CLAUDE_CODE_USE_VERTEX", "Vertex"),
                      ("CLAUDE_CODE_USE_FOUNDRY", "Foundry")):
        if env.get(key):
            return name
    if env.get("ANTHROPIC_BASE_URL"):
        return "another API address (ANTHROPIC_BASE_URL)"
    return "Bedrock, Vertex, Foundry or another API address (ANTHROPIC_BASE_URL)"


def process_group_kw() -> dict[str, Any]:
    """Popen arguments that put a child in a process group of its own (so it can be stopped
    with everything it started), without a console window on Windows."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000}
    return {"start_new_session": True}


def end_process(proc: subprocess.Popen, grace: float = RC_STOP_GRACE) -> None:
    """Stop a child and everything it started. Linux and macOS: SIGTERM to its process group, so it
    can tidy up, and SIGKILL after `grace` seconds. Windows: `taskkill /T /F` at once. A Ctrl+Break
    can't reach a child on a console of its own (and the runner, started with pythonw, has none),
    and Claude Code would only be killed by it, not shut down cleanly; so there is nothing to wait for."""
    if proc.poll() is not None:
        return
    if not IS_WINDOWS:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (OSError, ValueError):
            pass
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=0x08000000)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ValueError):
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


class WindowsJobObject:
    """A kill-on-close Job Object: every Remote Control server is put in it, so they all end
    with the runner however the runner ends (Windows has no process groups that do this)."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.ctypes = ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel32 = kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, proc: subprocess.Popen) -> None:
        if not self.kernel32.AssignProcessToJobObject(self.handle, int(proc._handle)):  # type: ignore[attr-defined]
            raise OSError(self.ctypes.get_last_error(), "AssignProcessToJobObject failed")


def pid_command_line(pid: int) -> str:
    """The command line of a running process on this machine ("" when it isn't running)."""
    try:
        if sys.platform.startswith("linux"):
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
        res = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=10)
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class RemoteControl:
    """The Remote Control servers this runner started (`Runner.rc`)."""

    def __init__(self, runner: Any) -> None:
        self.runner = runner
        self.lock = threading.RLock()  # an RLock: the runner's signal handler may run while it is held
        self.servers: dict[tuple, dict] = {}   # (agent, project) -> what the machine page shows, and the process
        self.restart_after_update: list[dict] = []
        # (agent, project) -> when that server first started: the ones to start again when the runner starts.
        self.chosen: dict[tuple, float] = {}
        self.restarting: set = set()        # (agent, project) being started again by this runner now
        self.chosen_started = False         # start_chosen has run (once per runner)
        self.job_object: WindowsJobObject | None = None
        self.job_object_failed = False
        self.clock: Callable[[], float] = time.time  # the unit tests move it
        self.state_file = RC_STATE_FILE

    # -- start ---------------------------------------------------------------
    def _machine(self) -> str:
        name = getattr(self.runner, "name", None)
        return name() if callable(name) else (self.runner.cfg.get("name") or socket.gethostname())

    def _refusal(self, agent: str, spec: dict, env: dict) -> str:
        """Why an agent can't run Remote Control (it needs a full claude.ai login)."""
        machine = self._machine()
        auth = agent_auth(spec, env)
        if auth == "api_key":
            return (f"{agent} on {machine} signs in with an API key, and Remote Control works only with a Claude "
                    "subscription login.")
        if auth == "token":
            return (f"{agent} signs in with a long-lived token (claude setup-token). Remote Control needs a full "
                    f"login: run \"{cli_command(spec, env)} auth login\" on {machine}.")
        if auth == "other":
            return (f"{agent} uses {other_service(env)}, and Remote Control works only with Anthropic's own "
                    "service.")
        return f"{agent} on {machine} can't run Remote Control (only Claude agents with a full login can)."

    def start(self, agent: str, project: str, emit: Callable[..., None] = lambda *a: None,
              started: float | None = None, keep: bool = False) -> dict:
        """Start a server for (agent, project) and wait up to RC_URL_WAIT seconds for its address.
        The outcome for the admin job. `keep`: start it again when the runner starts (kept only if it
        runs; once a server was started, the choice is exactly that)."""
        cfg = self.runner.cfg
        machine = self._machine()
        with self.lock:
            if not rc_allowed(cfg):
                return {"status": "error", "error": f"{machine} doesn't allow Remote Control from the dashboard "
                                                    "(allow_remote_control in its runner.json)."}
            if agent not in cfg.get("agents", {}):
                return {"status": "error", "error": f"There is no agent called '{agent}' on {machine}."}
            if project not in cfg.get("projects", {}):
                return {"status": "error", "error": f"There is no project called '{project}' on {machine}."}
            workdir = expand_path(cfg["projects"][project])
            if not os.path.isdir(workdir):
                return {"status": "error", "error": f"The project folder {workdir} does not exist."}
            spec = agent_spec(cfg, agent)
            env = build_env(spec)
            if "remote_control" not in agent_features(spec, env):
                return {"status": "error", "error": self._refusal(agent, spec, env)}
            current = self.servers.get((agent, project))
            if current and current["state"] in ("starting", "running"):
                return {"status": "error", "error": f"Remote Control is already running for {agent} in {project}."}
            running = [e for e in self.servers.values() if e["state"] in ("starting", "running")]
            if len(running) >= rc_max_servers(cfg):
                return {"status": "error", "error": f"{machine} already runs {len(running)} Remote Control servers "
                                                    "(max_remote_control in its runner.json). Stop one first."}
            exe = resolve_command(spec)
            if exe is None:
                return {"status": "error", "error": f"Could not find '{spec.get('command')}' on {machine}."}
            name = f"{machine} {project}" if agent == "claude" else f"{machine} {project} ({agent})"
            inside_git = (git(workdir, "rev-parse", "--is-inside-work-tree") or "").strip() == "true"
            spawn = "worktree" if inside_git else "same-dir"
            values = {"{rc_name}": name, "{rc_spawn}": spawn, "{workdir}": workdir, "{model}": "",
                      "{session_id}": "", "{new_session_id}": ""}
            argv = [exe]
            for arg in spec["remote_control"]:
                arg = str(arg)
                for key, value in values.items():
                    arg = arg.replace(key, value)
                argv.append(arg)
            env["PWD"] = workdir
            secrets_ = known_secrets(cfg, spec)
            log.info("remote control: %s", redact_argv(argv, secrets_))
            emit(f"▶ Starting Remote Control for {agent} in {project} ({workdir}) as '{name}'")
            try:
                proc = subprocess.Popen(argv, cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                        **process_group_kw())
            except OSError as err:
                return {"status": "error", "error": f"Could not start {exe}: {err}"}
            self._contain(proc)
            now = self.clock()
            entry: dict[str, Any] = {
                "agent": agent, "project": project, "name": name, "spawn": spawn, "state": "starting",
                "since": started if started is not None else now, "begun": time.monotonic(), "url": None,
                "note": "", "error": "", "proc": proc, "tail": [], "secrets": secrets_, "workdir": workdir,
                "spec": spec, "env": env, "ended": None, "changed": threading.Event(), "error_seen": False}
            self.servers[(agent, project)] = entry
            self._save()
        threading.Thread(target=self._read, args=(entry,), name=f"rc-{agent}-{project}", daemon=True).start()

        deadline = time.monotonic() + RC_URL_WAIT
        while entry["state"] == "starting" and not entry["error_seen"] and time.monotonic() < deadline:
            entry["changed"].wait(0.25)
            entry["changed"].clear()
        if entry["error_seen"]:  # give it a moment to say the rest (or to exit)
            settle = time.monotonic() + RC_ERROR_SETTLE
            while proc.poll() is None and time.monotonic() < settle:
                entry["changed"].wait(0.1)
                entry["changed"].clear()
        refused = False
        with self.lock:
            if entry["state"] == "starting" and entry["error_seen"]:  # it said Error: and no address
                entry.update(state="failed", error=self._failure(entry, None), ended=self.clock())
                refused = True
            elif entry["state"] == "starting":  # still up, but no address yet
                entry.update(state="running", note="started, but hasn't shown its address")
            state = entry["state"]
            if state == "running" and keep:
                self.chosen[(agent, project)] = entry["since"]
            else:
                self.chosen.pop((agent, project), None)
            self._save()
        if refused:  # don't leave it waiting to exit (lock released: the reader needs it to finish)
            log.info("remote control %s: stopping (it printed an error)", entry["name"])
            end_process(proc)
        hours = rc_max_hours(cfg)
        if state == "running":
            text = (f"Remote Control is running for {agent} in {project} as '{name}'. Open claude.ai/code or the "
                    f"Claude app, signed in to that account, to start a session. It stops at "
                    f"{_local_time(entry['since'] + hours * 3600)} (after {hours_words(hours)}).")
            if keep:
                text += " If the runner restarts before then (after a reboot, say), it starts this server again."
            if entry["url"]:
                text += "\n\n" + entry["url"]
            else:
                text += "\n\nIt hasn't shown its address yet; its last lines:\n" + self._tail(entry)
            emit("✔ " + text.splitlines()[0])
            return {"status": "done", "result": text}
        if state == "stopped":
            return {"status": "cancelled", "error": "Remote Control was stopped while it started."}
        error = entry["error"] or "Remote Control stopped."
        emit("✖ " + error.splitlines()[0])
        return {"status": "error", "error": error}

    def _contain(self, proc: subprocess.Popen) -> None:
        """Windows: put the server in the runner's kill-on-close Job Object."""
        if not IS_WINDOWS or self.job_object_failed:
            return
        try:
            if self.job_object is None:
                self.job_object = WindowsJobObject()
            self.job_object.assign(proc)
        except Exception as err:  # ctypes errors, an older Windows: stop_all still stops them
            self.job_object_failed = self.job_object is None
            log.warning("remote control: couldn't tie the server to the runner (%s); it is stopped when the "
                        "runner stops normally", err)

    def _read(self, entry: dict) -> None:
        """Follow a server's output: keep its last lines (masked) and its address."""
        proc = entry["proc"]
        for raw in proc.stdout:
            raw = raw.rstrip("\r\n")
            if not raw.strip():
                continue
            clean = redact(raw, entry["secrets"])
            log.info("remote control %s: %s", entry["name"], clean[:500])
            with self.lock:
                entry["tail"] = (entry["tail"] + [clean])[-RC_TAIL_LINES:]
                found = RC_URL_RE.search(raw) if entry["url"] is None else None
                if found:
                    entry["url"] = found.group(0)[:300]
                    if entry["state"] == "starting":
                        entry.update(state="running", note="")
                error = (not found and entry["url"] is None and entry["state"] == "starting"
                         and RC_ERROR_LINE_RE.match(ANSI_RE.sub("", raw)) is not None)
                if error:
                    entry["error_seen"] = True
            if found or error:
                entry["changed"].set()
        code = proc.wait()
        try:
            proc.stdout.close()
        except OSError:
            pass
        with self.lock:
            if entry["state"] in ("starting", "running"):
                entry.update(state="failed", error=self._failure(entry, code))
            entry["ended"] = entry["ended"] or self.clock()
            self._save()
        entry["changed"].set()

    def _tail(self, entry: dict) -> str:
        lines = [RC_EMAIL_RE.sub("…@…", ln)[:300] for ln in entry["tail"][-RC_SHOWN_LINES:]]
        return "\n".join(lines) or "(nothing)"

    def _failure(self, entry: dict, code: int | None) -> str:
        """Why a server stopped, in plain words, with its last lines."""
        agent, spec, env = entry["agent"], entry["spec"], entry["env"]
        machine = self._machine()
        command = cli_command(spec, env)
        said = "\n".join(entry["tail"])
        kind = next((k for k, rx in RC_FAILURES if rx.search(said)), "")
        if kind == "busy":
            text = (f"Remote Control is already running for this folder from a terminal on {machine} "
                    f"(`{command} remote-control`). Stop that one first (Ctrl+C in its terminal), then press Start "
                    "Remote Control again.")
        elif kind == "token" and claude_auth(env) == "token":
            text = (f"{agent} signs in with a long-lived token (claude setup-token). Remote Control needs a full "
                    f"login: run \"{command} auth login\" on {machine}.")
        elif kind == "token":
            # Claude Code says this when there is no login at all, too; a token can also come from the env
            # block of its settings.json, which the runner doesn't read.
            text = (f"{agent} on {machine} has no full claude.ai login that Remote Control can use: it isn't signed "
                    "in, or it signs in with a long-lived token (claude setup-token) set in its Claude Code "
                    f"settings. Run '{command} auth login' there once.")
        elif kind == "unreachable":
            text = (f"Claude Code on {machine} couldn't reach Anthropic to check whether Remote Control is available "
                    "(it may be offline, or a proxy or firewall blocks it). Check that machine's network, then press "
                    "Start Remote Control again.")
        elif kind == "account":
            text = (f"Remote Control isn't enabled for the Claude account {agent} uses on {machine}. This usually "
                    "means its login is out of date after a plan change: run "
                    f"'{command} auth logout' then '{command} auth login' there, and update Claude Code if it is old.")
        elif kind == "org":
            text = (f"Remote Control is turned off by a policy for the Claude account {agent} uses on {machine}. On "
                    "Team and Enterprise plans an Owner turns it on in the admin settings. On a Pro or Max plan, "
                    "Claude Code may still be signed in under an earlier Team or Enterprise organization: run "
                    f"'{command} auth logout' then '{command} auth login' there. If the lines below name "
                    "disableRemoteControl, that machine's managed settings turn it off.")
        elif kind == "flags":
            found = RC_FLAG_VARS_RE.search(said)
            var = found.group(1) if found else "a setting"
            text = (f"Claude Code on {machine} can't check whether Remote Control is available: {var} turns off its "
                    f"feature flags. Remove {found.group(1) if found else 'it'} from the agent's env in runner.json, "
                    "from that user's environment and from the env block of the agent's Claude Code settings.json, "
                    "then restart the runner.")
        elif kind == "other":
            text = f"{agent} uses {other_service(env)}, and Remote Control works only with Anthropic's own service."
        elif kind == "trust":
            folder = entry["workdir"]
            cd = (f"Set-Location -LiteralPath '{folder.replace(chr(39), chr(39) * 2)}'" if IS_WINDOWS
                  else f"cd {shlex.quote(folder)}")
            text = (f"Remote Control needs a one-time answer in a terminal on {machine}. Open a terminal there and "
                    f"run:\n\n    {cd}\n    {command} remote-control\n\nAnswer y to “Enable Remote Control?” and "
                    "trust the folder if it asks. When it says it is ready, stop it with Ctrl+C, then press Start "
                    "Remote Control here again.")
        elif kind == "api_key":
            text = (f"{agent} on {machine} signs in with an API key, and Remote Control works only with a Claude "
                    "subscription login.")
        elif kind == "login":
            text = f"{agent} isn't signed in on {machine}. Run '{command} auth login' there once."
        elif code is None:  # it printed an error before its address, and was still running
            text = f"Remote Control didn't start on {machine}: Claude Code reported an error."
        else:
            seconds = round(time.monotonic() - entry["begun"])
            return (f"Remote Control stopped after {seconds} seconds (exit code {code}). Its last lines:\n"
                    + self._tail(entry))
        return text + "\n\nIts last lines:\n" + self._tail(entry)

    # -- stop ----------------------------------------------------------------
    def stop(self, agent: str, project: str, why: str = "stopped") -> dict:
        """Stop one server (always allowed, whatever runner.json says now). It isn't started again when the
        runner starts: a server stopped on purpose (or by its time limit) stays stopped."""
        with self.lock:
            entry = self.servers.get((agent, project))
            if self.chosen.pop((agent, project), None) is not None:
                self._save()
            if entry is None:
                return {"status": "error", "error": f"Remote Control isn't running for {agent} in {project} here."}
            if entry["state"] not in ("starting", "running"):
                self.servers.pop((agent, project), None)  # already ended: take it off the machine page
                self._save()
                return {"status": "done", "result": f"Remote Control for {agent} in {project} had already stopped."}
            entry.update(state="stopped", note=why, ended=self.clock())
            self._save()
        log.info("remote control %s: stopping (%s)", entry["name"], why)
        end_process(entry["proc"])
        entry["changed"].set()
        return {"status": "done", "result": f"Stopped Remote Control for {agent} in {project}."}

    def stop_all(self, remember: bool = False, why: str = "stopped", forget: bool = False) -> None:
        """Stop every server, e.g. when the runner stops. `remember`: start them again when the
        updated runner starts (within RC_RESTART_WITHIN seconds). The ones chosen to start again
        when the runner starts stay chosen, unless `forget` (Remote Control turned off, the hub
        refusing this machine)."""
        with self.lock:
            entries = [e for e in self.servers.values() if e["state"] in ("starting", "running")]
            if remember:
                self.restart_after_update = [{"agent": e["agent"], "project": e["project"], "started": e["since"]}
                                             for e in entries]
            if forget:
                self.chosen.clear()
            for entry in entries:
                entry.update(state="stopped", note=why, ended=self.clock())
            self._save()
        threads = [threading.Thread(target=end_process, args=(e["proc"],), daemon=True) for e in entries]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=RC_STOP_GRACE + 10)
        for entry in entries:
            entry["changed"].set()
        if entries:
            log.info("remote control: stopped %d server(s) (%s)", len(entries), why)

    def enforce(self) -> None:
        """From the heartbeat loop: stop servers past remote_control_max_hours, and all of them when
        allow_remote_control has been turned off; forget ended ones after a while."""
        cfg = self.runner.cfg
        now = self.clock()
        if not rc_allowed(cfg):
            with self.lock:
                running = any(e["state"] in ("starting", "running") for e in self.servers.values())
                if not running and self.chosen:
                    self.chosen.clear()
                    self._save()
            if running:
                self.stop_all(why="Remote Control was turned off in runner.json", forget=True)
        hours = rc_max_hours(cfg)
        with self.lock:
            too_old = [e for e in self.servers.values()
                       if e["state"] in ("starting", "running") and now - e["since"] >= hours * 3600]
            for key, entry in list(self.servers.items()):
                # A server that didn't start again when the runner started stays on the page longer (until its
                # time would have been up), so its owner can see why.
                shown_until = max((entry["ended"] or 0) + RC_KEEP_ENDED, entry.get("shown_until") or 0)
                if entry["state"] in ("failed", "stopped") and entry["ended"] and now > shown_until:
                    del self.servers[key]
        for entry in too_old:
            self.stop(entry["agent"], entry["project"], why=f"stopped after {hours_words(hours)}")

    # -- what the machine page shows -----------------------------------------
    def view(self) -> list[dict]:
        hours = rc_max_hours(self.runner.cfg)
        with self.lock:
            return [{"agent": e["agent"], "project": e["project"], "name": e["name"], "spawn": e["spawn"],
                     "state": e["state"], "since": e["since"], "stops_at": e["since"] + hours * 3600,
                     "url": e["url"], "note": e["note"], "error": e["error"],
                     "start_with_runner": (e["agent"], e["project"]) in self.chosen}
                    for e in self.servers.values()]

    # -- the state file --------------------------------------------------------
    def _save(self) -> None:
        """remote-control.json: the running servers' process ids (to clean up after a crash), the
        servers to start again after an update, and the ones to start again whenever the runner
        starts. Rewritten on every change."""
        with self.lock:
            data = {"servers": [{"agent": e["agent"], "project": e["project"], "pid": e["proc"].pid,
                                 "started": e["since"]}
                                for e in self.servers.values() if e["state"] in ("starting", "running")],
                    "restart_after_update": list(self.restart_after_update),
                    "start_with_runner": [{"agent": a, "project": p, "started": since}
                                          for (a, p), since in sorted(self.chosen.items())],
                    "at": self.clock()}
            try:
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                write_private(self.state_file, json.dumps(data, indent=2) + "\n")
            except OSError as err:
                log.warning("remote control: couldn't save %s (%s)", self.state_file, err)

    def restore(self) -> None:
        """At start-up: stop servers a crashed runner left behind, start again the ones an update
        stopped (if that was under 10 minutes ago and Remote Control is still allowed), and read the
        ones chosen to start with the runner (start_chosen starts them)."""
        try:
            data = json.loads(read_private(self.state_file))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as err:
            log.warning("remote control: couldn't read %s (%s); nothing is restarted", self.state_file, err)
            return
        if not isinstance(data, dict):
            return
        for server in data.get("servers") or []:
            pid = server.get("pid") if isinstance(server, dict) else None
            if IS_WINDOWS or not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
                continue
            if "remote-control" in pid_command_line(pid):
                log.info("remote control: stopping a server left behind (pid %s)", pid)
                try:
                    os.killpg(pid, signal.SIGTERM)
                except OSError:
                    continue
                for _ in range(int(RC_STOP_GRACE * 4)):
                    time.sleep(0.25)
                    if not pid_command_line(pid):
                        break
                else:
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
        again = [r for r in data.get("restart_after_update") or [] if isinstance(r, dict)]
        at = _num(data.get("at")) or 0
        with self.lock:
            self.restart_after_update = []
            # Kept in memory first, or this save would drop them: start_chosen starts them once the runner has
            # checked in with the hub.
            for item in data.get("start_with_runner") or []:
                if not isinstance(item, dict):
                    continue
                agent, project, started = item.get("agent"), item.get("project"), _num(item.get("started"))
                if isinstance(agent, str) and isinstance(project, str) and started is not None:
                    self.chosen[(agent, project)] = started
            self._save()
        if not again or self.clock() - at > RC_RESTART_WITHIN or not rc_allowed(self.runner.cfg):
            return
        for item in again:
            agent, project, started = item.get("agent"), item.get("project"), _num(item.get("started"))
            if isinstance(agent, str) and isinstance(project, str):
                log.info("remote control: starting %s in %s again after the update", agent, project)
                with self.lock:
                    self.restarting.add((agent, project))
                threading.Thread(target=self._start_again, args=(agent, project, started, "after the update"),
                                 daemon=True).start()

    def start_chosen(self, paused: bool = False) -> None:
        """Once per runner, after its first check-in with the hub (so the network is up, and the hub can show
        and stop them): start again the servers chosen to start when the runner starts. Each one is tried
        once; if it can't start, the choice is cleared and the machine page says why. Not on a machine that
        no longer allows Remote Control, or that is paused on the hub: the choices are cleared, and the log
        says why."""
        with self.lock:
            if self.chosen_started:
                return
            self.chosen_started = True
            if not self.chosen:
                return
            cfg = self.runner.cfg
            why = ("this machine no longer allows Remote Control (allow_remote_control in its runner.json)"
                   if not rc_allowed(cfg) else "this machine is paused on the hub" if paused else "")
            if why:
                log.warning("remote control: not starting %s again: %s",
                            ", ".join(f"{a} in {p}" for a, p in sorted(self.chosen)), why)
                self.chosen.clear()
                self._save()
                return
            hours = rc_max_hours(cfg)
            now = self.clock()
            todo = []
            for (agent, project), started in sorted(self.chosen.items()):
                current = self.servers.get((agent, project))
                if (agent, project) in self.restarting or (current and current["state"] in ("starting", "running")):
                    continue  # already running, or being started again after an update
                if now - started >= hours * 3600:
                    log.info("remote control: not starting %s in %s again: its %s are up", agent, project,
                             hours_words(hours))
                    del self.chosen[(agent, project)]
                    continue
                self.restarting.add((agent, project))
                todo.append((agent, project, started))
            self._save()
        for agent, project, started in todo:
            log.info("remote control: starting %s in %s again now that the runner has started", agent, project)
            threading.Thread(target=self._start_again, args=(agent, project, started, "when the runner started"),
                             daemon=True).start()

    def _start_again(self, agent: str, project: str, started: float | None, when: str) -> None:
        """Start a server again (after an update, or when the runner starts), keeping its first start time
        and its choice. A start that fails is logged, shown on the machine page, and not tried again."""
        try:
            with self.lock:
                keep = (agent, project) in self.chosen
            try:
                out = self.start(agent, project, started=started, keep=keep)
            except Exception as err:  # never take the runner down for this
                log.exception("remote control: starting %s in %s again failed", agent, project)
                out = {"status": "error", "error": f"Remote Control couldn't start: {err}"}
            if out.get("status") == "done":
                return
            error = str(out.get("error") or "Remote Control didn't start.")
            hours = rc_max_hours(self.runner.cfg)
            with self.lock:
                entry = self.servers.get((agent, project))
                if entry is not None and entry["state"] in ("starting", "running", "stopped"):
                    return  # started from the dashboard meanwhile, or stopped while it started: that decided it
                log.warning("remote control: %s in %s didn't start again %s: %s", agent, project, when,
                            error.splitlines()[0] if error.strip() else error)
                self.chosen.pop((agent, project), None)
                if entry is None:
                    # Refused before anything ran (the project is gone, too many servers ...): a row of its own.
                    machine = self._machine()
                    entry = {"agent": agent, "project": project, "spawn": "", "url": None, "note": "",
                             "name": f"{machine} {project}" if agent == "claude" else f"{machine} {project} ({agent})",
                             "since": started if started is not None else self.clock(), "ended": self.clock()}
                    self.servers[(agent, project)] = entry
                entry.update(state="failed", error=f"Didn't start again {when}. {error}",
                             shown_until=(started if started is not None else self.clock()) + hours * 3600)
                self._save()
        finally:
            with self.lock:
                self.restarting.discard((agent, project))


HEARTBEAT_SOON = 3  # seconds: the soonest an extra check-in follows the last one


class Runner:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.mtime = config_path.stat().st_mtime
        self.hub = Hub(lambda: self.cfg)
        self.jobs: dict[int, Job] = {}
        self.jobs_lock = threading.Lock()
        self.config_lock = threading.Lock()
        self.heartbeat_lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.context_version = None  # what this machine last synced
        # From the last sync: the server list, the hub's MCP token and its platform names, which a job's
        # picked servers are built from (claude_server_entries). In memory only: it holds the token.
        self.last_context: dict | None = None
        self.sync_notes: list[str] = []
        self.stop = threading.Event()
        self.restart_pending = False
        self.conflict_warned = -1e9
        # Read once: an id that changed while the runner runs would look like a second computer on the hub.
        self.install_id = install_id()
        self.limits = LimitStore()  # the newest plan-limit figures per agent (limits.json)
        self.last_beat = 0.0
        self.rc = RemoteControl(self)  # Remote Control servers started from the dashboard
        self.sign_in = SignInStore()  # each Claude agent's sign-in, from its CLI's "auth status"
        self.refused = 0  # check-ins answered 401 in a row

    def name(self) -> str:
        return self.cfg.get("name") or socket.gethostname()

    def reload_if_changed(self) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
            if mtime != self.mtime:
                self.cfg = load_config(self.config_path)
                self.mtime = mtime
                log.info("reloaded %s", self.config_path)
                self.note_credits_agent()
        except (OSError, ValueError) as err:
            log.error("could not reload config: %s", err)

    def note_credits_agent(self) -> None:
        """Say in the log which agent the hub may use for platform credits, or why the one in
        runner.json can't be."""
        name, why = credits_agent(self.cfg)
        if why:
            log.warning("%s", why)
        elif name:
            log.info("credits agent: %s (the hub may run background work on it, paid by platform credits)", name)

    def update_config(self, change: Callable[[dict], Any]) -> None:
        """Apply a change to runner.json safely (re-read, change, write, reload)."""
        with self.config_lock:
            cfg = load_config(self.config_path)
            change(cfg)
            save_config(self.config_path, cfg)
            self.cfg = cfg
            self.mtime = self.config_path.stat().st_mtime

    def heartbeat_payload(self) -> dict:
        with self.jobs_lock:
            running = list(self.jobs)
        cfg = self.cfg
        limits = getattr(self, "limits", None)
        rc = getattr(self, "rc", None)
        sign_in = getattr(self, "sign_in", None)
        agents = []
        for name in cfg["agents"]:
            seen = None
            if sign_in is not None:
                spec = agent_spec(cfg, name)
                seen = sign_in.view(name, sign_in.key(spec, build_env(spec)))
            status = agent_status(cfg, name, sign_in=seen)
            if limits is not None and "limits" in status.get("features", []):
                status["limits"] = limits.view(name)  # the newest figures, for the dashboard only
            agents.append(status)
        return {
            "name": self.name(), "hostname": socket.gethostname(),
            # Tells this computer apart from another one under the same name, even when its host name changes.
            "install_id": getattr(self, "install_id", ""),
            "os": os_description(), "version": VERSION,
            "agents": agents,
            "projects": sorted(cfg["projects"]),
            "max_parallel": int(cfg.get("max_parallel", 1)), "running": running,
            # Words for this machine (they count on the hub once the owner accepts them), and this
            # machine's clock, so the dashboard can show how far it is off.
            "tags": heartbeat_tags(cfg), "sent_at": time.time(),
            "info": {
                "projects_root": expand_path(cfg.get("projects_root", "")),
                "remote_projects": bool(cfg.get("allow_remote_projects")),
                "remote_update": bool(cfg.get("allow_remote_update")),
                "project_paths": {n: expand_path(p) for n, p in cfg["projects"].items()},
                "restart_pending": self.restart_pending,
                "context_synced": self.context_version,
                "sync_notes": self.sync_notes[:20],
                "manage_agent_settings": bool(cfg.get("manage_agent_settings", True)),
                "platform": PLATFORM,
                "accept_delegated_tasks": cfg.get("accept_delegated_tasks") is True,
                "allow_remote_control": rc_allowed(cfg),
                "allow_remote_sign_out": sign_out_allowed(cfg),
                "remote_control_max_hours": rc_max_hours(cfg),
                "remote_control": rc.view() if rc is not None else [],
                # This runner understands "Start again when the runner starts" (rc.start's start_with_runner).
                "remote_control_start_with_runner": True,
                # The agent the hub may use for background work paid by platform credits (only one that
                # signs in with an API key; see credits_agent).
                "credits_agent": credits_agent(cfg)[0],
                # Git accounts: names, hosts and kinds, the defaults and the problems (see git_account_info).
                **git_account_info(cfg),
            },
        }

    def sync_from_hub(self, emit: Callable[..., None] = lambda *a: None) -> dict:
        """Bring this machine's shared context up to date: pinned notes now,
        and (see wire_agents) each agent's memory file and MCP servers."""
        with self.sync_lock:
            # What this runner understands: hub-routed servers, on-demand ones (left out of the machine-wide
            # setup, given only to the jobs that pick them), and an MCP token of this machine's own, which it
            # sends back so the hub keeps it (see machine_mcp_token).
            # Across processes too (the service and a `runner.py sync` typed at the same time): reading the
            # kept token, asking the hub and keeping its answer happen as one step, so two of them can't each
            # be given a new token and leave the agents' settings with the one the hub no longer accepts.
            token_lock = FileLock(MCP_TOKEN_LOCK)
            if not token_lock.acquire(timeout=60):
                return {"status": "error", "error": "another sync was still asking the hub after a minute; "
                                                    "try again"}
            try:
                held = read_machine_mcp_token(self.cfg)
                ask: dict = {"features": ["hub-route", "on-demand", "machine-mcp-token"]}
                if held:
                    ask["mcp_token"] = held
                status, body = self.hub.call("/api/runner/context", ask, timeout=30)
                if status != 200 or not body:
                    return {"status": "error", "error": f"couldn't fetch shared context from the hub ({status})"}
                keep_machine_mcp_token(self.cfg, body, held)
            finally:
                token_lock.release()
            self.last_context = {k: body[k] for k in ("servers", "mcp_enabled", "mcp_token", "platforms") if k in body}
            CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
            wanted = set()
            for fname, text in (body.get("files") or {}).items():
                if not re.match(r"^[A-Za-z0-9._-]+\.md$", fname):
                    continue
                wanted.add(fname)
                dest = CONTEXT_DIR / fname
                if not dest.exists() or dest.read_text(encoding="utf-8") != text:
                    tmp = dest.with_suffix(".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    os.replace(tmp, dest)
                    emit(f"updated {dest}")
            for old in CONTEXT_DIR.glob("agent-*.md"):
                if old.name not in wanted:
                    old.unlink()
                    emit(f"removed {old}")
            notes = wire_agents(self.cfg, body, emit, self.config_path)
            self.context_version = body.get("version")
            self.sync_notes = notes
            return {"status": "done", "result": "Shared context is up to date on this machine."
                    + ("\n\n" + "\n".join(f"- {n}" for n in notes) if notes else "")}

    def _background_sync(self) -> None:
        if self.sync_lock.locked():
            return
        try:
            result = self.sync_from_hub(lambda *lines: [log.info("sync: %s", ln) for ln in lines])
            if result.get("status") != "done":
                log.warning("sync: %s", result.get("error"))
            else:
                self.send_heartbeat()  # so the dashboard shows "up to date" right away
        except Exception:  # keep the runner alive whatever happens
            log.exception("sync failed")

    def send_heartbeat(self) -> bool:
        """Tell the hub we're here (and what we offer); act on any cancel requests."""
        with self.heartbeat_lock:
            self.reload_if_changed()
            limits = getattr(self, "limits", None)
            if limits is not None:
                limits.changed.clear()  # this check-in carries them; a change from now on asks for another
            sign_in = getattr(self, "sign_in", None)
            if sign_in is not None:
                sign_in.changed.clear()
            self.last_beat = time.monotonic()
            try:
                status, body = self.hub.call("/api/runner/heartbeat", self.heartbeat_payload(), timeout=20)
            except (OSError, ValueError) as err:
                log.warning("heartbeat failed: %s", err)
                return False
        if status == 401:
            self.refused = getattr(self, "refused", 0) + 1
            rc = getattr(self, "rc", None)
            if self.refused == RC_HEARTBEATS_REFUSED and rc is not None:
                # The machine was forgotten or its token revoked: nobody can stop them from the hub now.
                rc.stop_all(why="the hub no longer accepts this machine", forget=True)
        elif status == 200:
            self.refused = 0
        if status == 200 and body:
            for job_id in body.get("cancel", []):
                with self.jobs_lock:
                    job = self.jobs.get(job_id)
                if job:
                    job.cancel.set()
            if body.get("context_version") is not None and body["context_version"] != self.context_version:
                threading.Thread(target=self._background_sync, daemon=True).start()
            # Paused on the dashboard: the hub hands this machine no new tasks (machine actions still come).
            # Said once each way; a hub from before pausing sends no "paused", which leaves things as they are.
            paused = body.get("paused")
            if isinstance(paused, bool) and paused != getattr(self, "hub_paused", False):
                self.hub_paused = paused
                if paused:
                    log.info("this machine is paused on the hub: it gets no new tasks until you resume it on its "
                             "page (machine actions such as updates and syncs still run)")
                else:
                    log.info("this machine was resumed on the hub: it takes new tasks again")
            rc = getattr(self, "rc", None)
            if rc is not None:
                try:  # once per runner: the servers chosen to start with it (not while the machine is paused)
                    rc.start_chosen(paused=getattr(self, "hub_paused", False))
                except Exception:  # keep checking in whatever happens
                    log.exception("remote control: starting the chosen servers failed")
            other = body.get("name_conflict")
            if other and time.monotonic() - self.conflict_warned > 600:
                self.conflict_warned = time.monotonic()
                log.warning("another computer (%s) also checks in to the hub as '%s'. Give one of them another "
                            "name ('name' in %s), or they take each other's jobs.", other, self.name(), self.config_path)
            return True
        if status == 401:
            log.error("the hub rejected this runner's token; check 'token' in %s (if this machine was "
                      "forgotten on the dashboard, add it again with a new code)", self.config_path)
        elif status == 403:
            log.error("the hub refused this machine: %s", (body or {}).get("detail"))
        else:
            log.warning("heartbeat: hub answered %s %s", status, body)
        return False

    def refresh_model_lists(self) -> None:
        """Keep each Gemini agent's model list current (at most every 12 hours, in the background)."""
        keys = {gemini_key(build_env(agent_spec(self.cfg, a))) for a in self.cfg["agents"]
                if agent_spec(self.cfg, a).get("preset") == "gemini"}
        for key in filter(None, keys):
            threading.Thread(target=gemini_models, args=(key, True), daemon=True).start()

    def heartbeat_loop(self) -> None:
        beats = 0
        while not self.stop.is_set():
            if beats % 240 == 0:  # at start, then about hourly (the list itself refreshes every 12 h)
                self.refresh_model_lists()
            beats += 1
            try:
                self.reload_if_changed()
                self.rc.enforce()  # Remote Control's time limit, and the setting turned off
            except Exception:  # keep checking in whatever happens
                log.exception("remote control check failed")
            try:
                self.sign_in.refresh_due(self.cfg, self.busy_agents())
            except Exception:
                log.exception("sign-in check failed")
            ok = self.send_heartbeat()
            # Retry sooner while the hub is unreachable. New plan-limit figures (from a job that is still
            # running) are sent within a few seconds rather than at the next regular check-in.
            deadline = time.monotonic() + (15 if ok else 5)
            while not self.stop.wait(1) and time.monotonic() < deadline:
                if ok and (self.limits.changed.is_set() or self.sign_in.changed.is_set()) \
                        and time.monotonic() - self.last_beat >= HEARTBEAT_SOON:
                    break

    def busy_agents(self) -> set:
        """The agents with a job running here now (a sign-in check waits for them)."""
        with self.jobs_lock:
            return {str(j.data.get("agent")) for j in self.jobs.values()
                    if j.data.get("kind") != "admin" and j.data.get("agent")}

    def start_job(self, data: dict) -> None:
        job = Job(self, data)
        with self.jobs_lock:
            self.jobs[job.id] = job

        def work() -> None:
            try:
                job.run()
            finally:
                with self.jobs_lock:
                    self.jobs.pop(job.id, None)

        threading.Thread(target=work, name=f"job-{job.id}", daemon=True).start()

    def restart(self) -> None:
        """Start the freshly updated runner in place of this one."""
        # First, on every platform: the Windows branch ends in os._exit, where atexit doesn't run.
        # The new runner starts these servers again.
        self.rc.stop_all(remember=True, why="the runner is updating")
        with self.sync_lock:  # never cut a sync off halfway
            log.info("restarting to load the updated runner")
            python = stable_python()
            argv = [python, os.path.abspath(sys.argv[0]), *sys.argv[1:]]
            if not IS_WINDOWS:
                os.execv(python, argv)  # same process, so systemd and launchd keep tracking it
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000  # no window
            task = "Agent Bullpen Runner"
            started_by_task = subprocess.run(["schtasks", "/Query", "/TN", task], capture_output=True,
                                             creationflags=0x08000000).returncode == 0
            if started_by_task:
                # Ask Task Scheduler to start the new copy a few seconds after this one exits, so the
                # runner stays the task's own process (Stop-/Start-ScheduledTask keep working).
                helper = ["cmd", "/c", f'ping -n 6 127.0.0.1 >nul & schtasks /Run /TN "{task}"']
            else:
                helper = argv  # started by hand: start the new copy directly
            try:
                subprocess.Popen(helper, cwd=os.getcwd(), close_fds=True, creationflags=flags | 0x01000000)
            except OSError:  # not allowed to break away from a job object: start it inside instead
                subprocess.Popen(helper, cwd=os.getcwd(), close_fds=True, creationflags=flags)
            os._exit(0)

    def run_forever(self) -> None:
        log.info("runner %s v%s starting; hub %s", self.name(), VERSION, self.cfg["hub_url"])
        self.note_credits_agent()
        clean_job_dirs()
        self.rc.restore()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        backoff = 2
        while not self.stop.is_set():
            with self.jobs_lock:
                agent_jobs = sum(1 for j in self.jobs.values() if j.data.get("kind") != "admin")
                admin_jobs = len(self.jobs) - agent_jobs
            if self.restart_pending:
                if not agent_jobs and not admin_jobs:
                    self.restart()
                time.sleep(1)
                continue
            # Admin actions (clone, update, ...) get their own slot so a busy machine still answers them.
            admin_only = agent_jobs >= int(self.cfg.get("max_parallel", 1))
            if admin_only and admin_jobs:
                time.sleep(1)
                continue
            try:
                status, body = self.hub.call("/api/runner/poll", {"name": self.name(), "admin_only": admin_only},
                                             timeout=40)
                backoff = 2
                if status == 200 and body and body.get("job"):
                    log.info("got job %s", body["job"]["id"])
                    self.start_job(body["job"])
                elif status in (401, 403):
                    log.error("the hub refused this runner (%s): %s", status, (body or {}).get("detail"))
                    time.sleep(30)
                elif status not in (200, 204):
                    log.warning("poll: hub answered %s %s", status, body)
                    time.sleep(5)
            except (OSError, ValueError) as err:
                log.warning("cannot reach the hub (%s); retrying in %ss", err, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)


# ================================================================ commands
LAUNCHD_LABEL = "bullpen.runner"  # the launchd agent the setup script makes on macOS


def under_launchd(env: Optional[Mapping[str, str]] = None) -> bool:
    """Started by the setup's launchd agent. Its stderr is ~/.bullpen/launchd.log, which nothing rotates,
    so log lines go only to the rotated runner.log there. BULLPEN_SERVICE comes from the plist; launchd
    itself sets XPC_SERVICE_NAME to the label, which covers agents made by older setups."""
    env = os.environ if env is None else env
    return env.get("BULLPEN_SERVICE") == "launchd" or env.get("XPC_SERVICE_NAME") == LAUNCHD_LABEL


def setup_logging(to_console: bool = True) -> None:
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        BASE_DIR / "runner.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")]
    if to_console and sys.stderr is not None and not under_launchd():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s")


_input_closed = False  # no keyboard (e.g. started from a script): every question takes its default


def ask(question: str, default: str = "") -> str:
    global _input_closed
    suffix = f" [{default}]" if default else ""
    try:
        answer = "" if _input_closed else input(f"{question}{suffix}: ").strip()
    except EOFError:
        _input_closed = True
        print()
        answer = ""
    return answer or default


def ask_yes(question: str, default: bool = True) -> bool:
    global _input_closed
    hint = "Y/n" if default else "y/N"
    try:
        answer = "" if _input_closed else input(f"{question} ({hint}): ").strip().lower()
    except EOFError:
        _input_closed = True
        print()
        answer = ""
    return default if not answer else answer.startswith("y")


def ask_tags(cfg: dict) -> list[str]:
    """init's question for this machine's tags, and why any word was left out."""
    current = clean_tags(cfg.get("tags", []))[0]
    hint = "'none' to remove them" if current else "blank for none"
    raw = ask(f"Tags for this machine, separated by commas (e.g. gpu, security-tools; {hint})", ", ".join(current))
    if raw.strip().lower() == "none":
        return []
    tags, skipped = clean_tags(raw)
    for word in skipped:
        if TAG_RE.match(word.lower()):
            print(f"  skipped '{word}': at most {MAX_OWN_TAGS} tags (the operating-system tag makes {MAX_TAGS})")
        elif len(word) > 32:
            print(f"  skipped '{word}': a tag has at most 32 characters")
        else:
            print(f"  skipped '{word}': tags use letters, digits, '.', '_' and '-'")
    for word in tags:
        if word in PLATFORM_TAGS:
            print(f"  skipped '{word}': every machine reports its own system ({PLATFORM}) by itself")
    return [t for t in tags if t not in PLATFORM_TAGS]


def make_chat_workspace() -> Path:
    chat = BASE_DIR / "chat"
    chat.mkdir(parents=True, exist_ok=True)
    for name in ("CLAUDE.md", "GEMINI.md", "AGENTS.md"):
        file = chat / name
        if not file.exists():
            file.write_text(CHAT_README, encoding="utf-8")
    return chat


def ask_int(question: str, default: int, low: int = 1, high: int = 256) -> int:
    while True:
        answer = ask(question, str(default))
        if answer.isdigit() and low <= int(answer) <= high:
            return int(answer)
        if _input_closed:
            return default
        print(f"  please enter a number from {low} to {high}")


def os_description() -> str:
    if IS_MACOS and platform.mac_ver()[0]:
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


def default_machine_name() -> str:
    """The host name, made into a dashboard name: 'Build-Box.local' -> 'build-box'."""
    name = re.sub(r"[^a-z0-9._-]+", "-", socket.gethostname().lower().split(".")[0]).strip("-._")[:64]
    return name or "machine"


def clean_agent_label(raw: str, prefix: str) -> str:
    """'Work Laptop' -> 'work-laptop'; 'claude-work' (for prefix 'claude') -> 'work'."""
    label = re.sub(r"[^a-z0-9-]+", "-", raw.strip().lower()).strip("-")
    if label == prefix:
        return ""
    return label[len(prefix) + 1:] if label.startswith(prefix + "-") else label


def clean_hub_url(raw: str) -> str:
    url = (raw or "").strip().rstrip("/")
    if url and not re.match(r"^https?://", url):
        url = "http://" + url  # people often type just the address and port
    return url


CODE_RE = re.compile(r"^[2-9A-HJ-NP-Z]{4}(-?[2-9A-HJ-NP-Z]{4}){3}$")


def looks_like_code(value: str) -> bool:
    """A one-time code from "Add a machine" (four groups of four letters and digits)."""
    return bool(CODE_RE.match(value.strip().upper()))


def enroll_with_code(cfg: dict, hub_url: str, code: str, name: str = "") -> tuple[bool, str]:
    """Trade a one-time code for this machine's own token and put it in cfg. (ok, message)."""
    try:
        payload = {"code": code.strip(), "name": name, "hostname": socket.gethostname(), "os": os_description(),
                   "install_id": install_id()}
        if cfg.get("token") and clean_hub_url(cfg.get("hub_url", "")) == clean_hub_url(hub_url):
            # Reconnecting: prove this is the same machine, and keep its name unless another is asked for.
            payload["current_token"] = cfg["token"]
            payload["current_name"] = cfg.get("name", "")
        status, body = post_json(hub_url.rstrip("/") + "/api/enroll", payload)
    except (OSError, ValueError) as err:
        return False, f"couldn't reach the hub at {hub_url} ({err})"
    if status != 200 or not body or not body.get("token"):
        return False, f"the hub didn't accept the code ({status}): {(body or {}).get('detail')}"
    cfg.update(name=body["name"], hub_url=hub_url.rstrip("/"), token=body["token"])
    return True, f"this machine is on the hub as '{body['name']}', with its own runner token"


def check_token(cfg: dict) -> tuple[bool, str]:
    try:
        # Name the machine: a shared token is refused for a name that has its own token.
        status, body = Hub(lambda: cfg).call("/api/runner/whoami", {"name": cfg.get("name", "")}, timeout=20)
    except (OSError, ValueError) as err:
        return False, f"couldn't reach the hub at {cfg.get('hub_url')} ({err})"
    if status == 404:  # a hub without whoami; the token is checked at the first check-in
        return True, "saved (this hub is too old to check the token now)"
    if status != 200 or not body:
        return False, f"the hub refused the token ({status}): {(body or {}).get('detail')}"
    if body.get("auth") == "machine":
        cfg["name"] = body.get("name") or cfg.get("name")
        return True, f"the token is this machine's own, for '{cfg['name']}'"
    return True, "the hub accepted its shared runner token"


def ask_hub(cfg: dict) -> None:
    """The hub part of init: address, name, and a one-time code or a token."""
    while True:
        cfg["hub_url"] = clean_hub_url(ask("Hub address (its LAN address, e.g. http://192.168.100.20:8080)",
                                           cfg.get("hub_url") or os.environ.get("BULLPEN_HUB", "")))
        if cfg["hub_url"] or _input_closed:
            break
        print("  The runner needs the hub's address. The dashboard shows it under Add a machine.")
    old_name = cfg.get("name", "")
    cfg["name"] = ask("Name for this machine on the dashboard",
                      cfg.get("name") or os.environ.get("BULLPEN_NAME") or default_machine_name())
    while not _input_closed:
        hint = " (Enter keeps the current token)" if cfg.get("token") else ""
        secret = ask("One-time code from 'Add a machine' on the dashboard, or the hub's shared runner token" + hint)
        if not secret:
            if cfg.get("token") and cfg["name"] != old_name:
                # A machine's own token is tied to its name on the hub: keeping it under a new name
                # would lock the machine out. Renaming takes a code made for the new name.
                trial = dict(cfg)
                ok, message = check_token(trial)
                if not ok or trial.get("name") != cfg["name"]:
                    print(f"  This machine's token is for '{old_name}'. To rename it, click Add a machine on the "
                          f"dashboard, type '{cfg['name']}' as the name, and enter that code here.")
                    cfg["name"] = old_name
                    continue
            if cfg.get("token") or _input_closed:
                return
            print("  This machine can't connect without one. On the dashboard, use Add a machine to get a code.")
            if not ask_yes("  Try again?", True):
                return
            continue
        if looks_like_code(secret):
            ok, message = enroll_with_code(cfg, cfg["hub_url"], secret, cfg.get("name", ""))
        else:
            trial = {**cfg, "token": secret}
            ok, message = check_token(trial)
            if ok:
                cfg.update(token=secret, name=trial.get("name") or cfg.get("name"))
        print(f"  {message}")
        if ok or not ask_yes("  Try again?", True):
            return


def describe_claude(entry: dict) -> str:
    env = entry.get("env") or {}
    where = env.get("CLAUDE_CONFIG_DIR") or "~/.claude"
    how = "API key" if env.get("ANTHROPIC_API_KEY") else "token" if env.get("CLAUDE_CODE_OAUTH_TOKEN") else "login"
    account = entry.get("account")
    return f"{entry.get('label') or 'Claude'}, {how} in {where}" + (f", account {account}" if account else "")


def ask_account(entry: dict, what: str) -> None:
    """An agent's account label, as its last setup question. Saved only when one is given: an agent
    without a label is an account of its own on the dashboard."""
    current = entry.get("account") if isinstance(entry.get("account"), str) else ""
    while True:
        answer = ask(f"  Account label for {what} (optional). Use the same word on every machine signed in to "
                     "this very account, and a different word for every other account. Blank for none"
                     + (", '-' to remove it" if current else ""), current).strip()
        if answer == "-":
            answer = ""
        if not answer or ACCOUNT_RE.match(answer):
            break
        if _input_closed:
            return  # no keyboard: leave what runner.json has
        print("  Use up to 40 letters, digits, spaces, dots, dashes or underscores, starting with a letter or digit.")
    if answer:
        entry["account"] = answer
    else:
        entry.pop("account", None)


def claude_account_hint(entry: dict) -> str:
    env = entry.get("env") or {}
    return "the Console organization" if "ANTHROPIC_API_KEY" in env else "the claude.ai account"


GEMINI_ACCOUNT_HINT = "the Google Cloud project behind the key"


def setup_claude_agents(agents: dict, claude: str, fresh: bool) -> None:
    print(f"\nClaude Code: {claude}")
    print("  Each Claude agent is one login with its own settings folder: 'claude' uses ~/.claude,")
    print("  'claude-<name>' uses ~/.claude-<name>. Add one for each Claude account you want to use here,")
    print("  and, if you like, one that uses an API key (billed per token).")
    for name in [n for n, e in agents.items() if e.get("preset") == "claude"]:
        if ask_yes(f"Keep '{name}' ({describe_claude(agents[name])})?", True):
            agents[name]["command"] = claude
            ask_account(agents[name], claude_account_hint(agents[name]))
        else:
            agents.pop(name)
    while ask_yes("Add a Claude agent?", fresh and not any(e.get("preset") == "claude" for e in agents.values())):
        taken = "claude" in agents
        label = clean_agent_label(ask("  Name it, e.g. work or personal" + (" (blank: plain 'claude')" if not taken else ""),
                                      ""), "claude")
        api = ask("  How does it sign in? 1 = a Claude subscription (browser login), 2 = an API key", "1").startswith("2")
        if api and not label:
            label = "api"  # an API-key agent gets its own folder, never the default login's
        name = f"claude-{label}" if label else "claude"
        if name in agents:
            print(f"  there is already an agent called '{name}'")
            continue
        if api:
            base = ask("  Gateway URL (blank: Anthropic's API directly; e.g. your LiteLLM gateway)", "")
            key = ask("  API key, or the gateway's virtual key", "")
            env = {"CLAUDE_CONFIG_DIR": str(HOME / f".{name}"), "ANTHROPIC_API_KEY": key}
            if base:
                env["ANTHROPIC_BASE_URL"] = base
            agents[name] = {"preset": "claude", "label": f"Claude · {label} (API key)", "command": claude, "env": env}
        elif name == "claude":
            # Claude Code's normal login, so no CLAUDE_CONFIG_DIR at all.
            agents[name] = {"preset": "claude", "label": "Claude", "command": claude,
                            "unset_env": ["CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR"]}
        else:
            agents[name] = {"preset": "claude", "label": f"Claude · {label}", "command": claude,
                            "env": {"CLAUDE_CONFIG_DIR": str(HOME / f".{name}")}}
        ask_account(agents[name], claude_account_hint(agents[name]))
        print(f"  added '{name}'" + ("" if api else "; the setup script signs you in to it next"))


def setup_gemini_agents(agents: dict, gemini: str, fresh: bool) -> None:
    print(f"\nGemini CLI: {gemini}")
    print("  Gemini CLI needs a Gemini API key from aistudio.google.com/apikey (billed per use). The first")
    print(f"  Gemini agent uses the key saved in {GEMINI_ENV}; others can have a key or default model of their own.")
    for name in [n for n, e in agents.items() if e.get("preset") == "gemini"]:
        if not ask_yes(f"Keep '{name}' ({agents[name].get('label') or 'Gemini'})?", True):
            agents.pop(name)
            continue
        agents[name]["command"] = gemini
        if not (agents[name].get("env") or {}).get("GEMINI_API_KEY"):
            if gemini_key_saved():
                if ask_yes(f"  A Gemini API key is saved in {GEMINI_ENV}. Replace it?", False):
                    save_gemini_key(ask("  Gemini API key"))
            else:
                save_gemini_key(ask("  Gemini API key (from aistudio.google.com/apikey)"))
        ask_account(agents[name], GEMINI_ACCOUNT_HINT)
    while ask_yes("Add a Gemini agent?", fresh and not any(e.get("preset") == "gemini" for e in agents.values())):
        others = [n for n, e in agents.items() if e.get("preset") == "gemini"]
        label = clean_agent_label(ask("  Name it" + (", e.g. work" if others else " (blank: plain 'gemini')"), ""),
                                  "gemini")
        name = f"gemini-{label}" if label else "gemini"
        if name in agents:
            print(f"  there is already an agent called '{name}'")
            continue
        entry: dict[str, Any] = {"preset": "gemini", "label": "Gemini" + (f" · {label}" if label else ""),
                                 "command": gemini}
        if others:
            key = ask(f"  Its own Gemini API key (blank: the one in {GEMINI_ENV})", "")
            if key:
                entry["env"] = {"GEMINI_API_KEY": key}
        elif gemini_key_saved():
            print(f"  It uses the key already saved in {GEMINI_ENV}.")
        else:
            save_gemini_key(ask("  Gemini API key (from aistudio.google.com/apikey)"))
        model = ask("  Default model (blank: Gemini CLI's own default)", "")
        if model:
            entry["model_name"] = model
        ask_account(entry, GEMINI_ACCOUNT_HINT)
        agents[name] = entry
        print(f"  added '{name}'")


def ask_credits_agent(cfg: dict) -> None:
    """Which agent the hub may use for background work paid by platform credits (runner.json
    "credits_agent"). Asked only when this machine has a Claude agent that signs in with an API key;
    the answer is none unless one is named."""
    agents = cfg.get("agents") or {}
    keyed = [n for n in agents if agents[n].get("preset") in ("claude", "selftest")
             and credits_agent({**cfg, "credits_agent": n})[0] == n]
    current = cfg.get("credits_agent") if cfg.get("credits_agent") in keyed else ""
    if not keyed:
        cfg.pop("credits_agent", None)
        return
    print("\n  Some Claude plans include monthly API credits in the Console organization linked to the plan.")
    print("  If you name an agent that uses an API key from that organization, the hub can run background work")
    print("  on it (only once you turn that on in the dashboard; see guide 8).")
    while True:
        answer = ask(f"Credits agent ({', '.join(keyed)}; " + ("'-' for none)" if current else "blank for none)"),
                     current).strip()
        if answer in ("", "-") or answer in keyed:
            break
        if _input_closed:
            return  # no keyboard: leave what runner.json has
        print(f"  Pick one of: {', '.join(keyed)}.")
    if answer in ("", "-"):
        cfg.pop("credits_agent", None)
    else:
        cfg["credits_agent"] = answer


def ask_secret(question: str) -> str:
    """ask() for a secret: what is typed isn't shown (getpass), and nothing is offered in [brackets]."""
    global _input_closed
    try:
        return "" if _input_closed else getpass.getpass(f"{question}: ").strip()
    except EOFError:
        _input_closed = True
        print()
        return ""


def ask_git_account(name: str, entry: dict) -> bool:
    """init's questions for one git account, changing `entry` in place. False when it can't be used then
    (the reason is printed; it holds no value from the entry)."""
    def text(key: str) -> str:
        return entry[key] if isinstance(entry.get(key), str) else ""

    def optional(key: str, question: str, blank: str = "blank for none") -> None:
        current = text(key)
        answer = ask(f"{question} (" + ("Enter keeps it, '-' removes it" if current else blank) + ")",
                     current).strip()
        if answer and answer != "-":
            entry[key] = answer
        else:
            entry.pop(key, None)

    while True:
        host = ask("  Host, e.g. github.com or gitlab.example.com (no https://)", text("host")).strip().lower()
        if GIT_HOST_RE.fullmatch(host):
            entry["host"] = host
            break
        if _input_closed:
            return False
        print("  Give the host name only, and its port if it has one: no https://, path or user@.")
    inferred = git_kind_of(host)
    while True:
        current = text("kind")
        kind = ask("  Kind: github, gitlab or other (" +
                   (f"'-' to go by the host: {inferred}" if current else f"blank: {inferred}, from the host") + ")",
                   current).strip()
        if kind in ("", "-") or kind in GIT_KINDS:
            break
        if _input_closed:
            return False
        print("  Use github, gitlab or other.")
    if kind in ("", "-"):
        entry.pop("kind", None)
    else:
        entry["kind"] = kind
    optional("name", "  Commit name, optional")
    optional("email", "  Commit email, optional")
    optional("ssh_key", "  SSH key file, optional, e.g. ~/.ssh/id_ed25519")
    if text("ssh_key") and not os.path.isfile(expand_path(text("ssh_key"))):
        print("  That key file doesn't exist on this machine (yet). Jobs that use this account are refused")
        print("  until it does.")
    current = text("token")
    token = ask_secret("  HTTPS token, optional; it isn't shown as you type ("
                       + ("Enter keeps the current one, '-' removes it)" if current else "blank for none)"))
    if token == "-" or not (token or current):
        entry.pop("token", None)
    elif token:
        entry["token"] = token
    if text("token"):
        optional("user", "  User name sent with the token", "blank: x-access-token")
    account, why = check_git_account(name, entry)
    if account is None:
        print(f"  {why}")
    return account is not None


def ask_git_accounts(cfg: dict) -> None:
    """init's step for git accounts (runner.json "git_accounts"): keep, change or remove each one, add more,
    then the default for the machine and the projects' own (project_git_accounts)."""
    raw = cfg.get("git_accounts", {})
    if not isinstance(raw, dict):
        print("\ngit_accounts in runner.json isn't an object of named accounts. Fix it by hand, then run init again.")
        return
    print("\n  Git accounts let a task use a named GitHub, GitLab or other git identity here: an SSH key, an HTTPS")
    print("  token, a commit name and email. They stay in runner.json.")
    print("  The hub learns their names, hosts and kinds, never a key or token.")
    accounts = dict(raw)
    removed = []
    for name in list(accounts):
        account, why = check_git_account(name, accounts[name])
        what = f"{account['host']}, {git_credentials(account)}" if account else f"can't be used: {why}"
        if not ask_yes(f"Keep git account '{_shown(name)}' ({what})?", True):
            accounts.pop(name)
            removed.append(name)
            print(f"  removed '{_shown(name)}'")
            continue
        if GIT_ACCOUNT_RE.fullmatch(name) and isinstance(accounts[name], dict) and ask_yes("  Change it?", False):
            entry = dict(accounts[name])
            if ask_git_account(name, entry):
                accounts[name] = entry
            else:
                print("  kept it as it was")
    while ask_yes("Add a git account?", False):
        name = ask("  Name for it (up to 32 lowercase letters, digits or dashes), e.g. personal or bot").strip()
        if not GIT_ACCOUNT_RE.fullmatch(name):
            print("  Use up to 32 lowercase letters, digits or dashes, starting with a letter or digit.")
            continue
        if name in accounts:
            print(f"  there is already a git account called '{name}'")
            continue
        entry: dict[str, Any] = {}
        if ask_git_account(name, entry):
            accounts[name] = entry
            print(f"  added '{name}'")
        else:
            print(f"  '{name}' was not added")
    if accounts:
        cfg["git_accounts"] = accounts
    else:
        cfg.pop("git_accounts", None)

    # A default that named an account removed just now goes with it.
    if cfg.get("default_git_account") in removed:
        print(f"  '{cfg.pop('default_git_account')}' was the default git account; now there is none")
    mapping = cfg.get("project_git_accounts", {})
    if isinstance(mapping, dict):
        for project in [p for p, value in mapping.items() if value in removed]:
            print(f"  project '{_shown(project)}' had '{mapping.pop(project)}' as its git account; "
                  "now it has the default")
        if not mapping:
            cfg.pop("project_git_accounts", None)
    valid = sorted(git_accounts(cfg)[0])
    if not valid or _input_closed:
        return  # nothing to name, or no keyboard: the defaults stay as runner.json has them

    current = cfg.get("default_git_account") if isinstance(cfg.get("default_git_account"), str) else ""
    while True:
        answer = ask(f"Default git account for every project ({', '.join(valid)}; "
                     + ("'-' for none)" if current else "blank for none)"), current).strip()
        if answer in ("", "-") or answer in valid:
            break
        if _input_closed:
            return  # no keyboard: leave what runner.json has
        print(f"  Pick one of: {', '.join(valid)}, or '-' for none.")
    if answer in ("", "-"):
        cfg.pop("default_git_account", None)
    else:
        cfg["default_git_account"] = answer

    if not isinstance(mapping, dict):
        print("  project_git_accounts in runner.json isn't an object of project and account names. Fix it by hand.")
        return
    mapping = dict(mapping)
    for project in list(mapping):
        value = mapping[project]
        state = "" if isinstance(value, str) and value in valid else " (not a valid git account)"
        if not ask_yes(f"Keep git account '{_shown(value)}' for project '{_shown(project)}'{state}?", True):
            mapping.pop(project)
    projects = sorted(cfg.get("projects") or {})
    while ask_yes("Give a project a git account of its own?", False):
        project = ask(f"  Which project ({', '.join(projects)})").strip()
        if project not in projects:
            print(f"  there is no project called '{_shown(project)}' on this machine")
            continue
        name = ask(f"  Git account for {project} ({', '.join(valid)})").strip()
        if name not in valid:
            print(f"  Pick one of: {', '.join(valid)}.")
            continue
        mapping[project] = name
    if mapping:
        cfg["project_git_accounts"] = mapping
    else:
        cfg.pop("project_git_accounts", None)


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    cfg = load_config(path) if path.exists() else {"agents": {}, "projects": {}}
    print(f"\nAgent Bullpen runner setup: writing {path}\n")
    print("Press Enter to keep the value shown in [brackets].\n")

    connected = bool(cfg.get("hub_url") and cfg.get("token"))
    if connected and getattr(args, "skip_hub", False):
        print(f"Hub: {cfg['hub_url']}, as '{cfg.get('name') or default_machine_name()}' (already set up)")
    elif connected and ask_yes(f"Keep the hub connection ({cfg['hub_url']}, as "
                               f"'{cfg.get('name') or default_machine_name()}')?", True):
        pass
    else:
        ask_hub(cfg)
    cfg.setdefault("name", default_machine_name())
    cfg["max_parallel"] = ask_int("How many jobs may run at once on this machine", int(cfg.get("max_parallel", 1)))
    cfg["tags"] = ask_tags(cfg)
    cfg["projects_root"] = ask("Folder for projects you create or clone from the dashboard",
                               cfg.get("projects_root") or str(HOME / "projects"))
    cfg["allow_remote_projects"] = ask_yes("Let the dashboard create, clone and remove projects in that folder?",
                                           cfg.get("allow_remote_projects", True))
    cfg["allow_remote_update"] = ask_yes("Let the dashboard update this runner with one click?",
                                         cfg.get("allow_remote_update", True))
    cfg["manage_agent_settings"] = ask_yes(
        "Let the hub add shared notes, the /remember skill and your central MCP servers to this "
        "machine's Claude, Gemini and Antigravity settings?", cfg.get("manage_agent_settings", True))
    cfg["accept_delegated_tasks"] = ask_yes(
        "Let agents on your other machines hand subtasks to this machine (only in tasks where you allow it)?",
        cfg.get("accept_delegated_tasks") is True)
    print("  Remote Control lets you drive Claude Code in a project here from claude.ai or the Claude app.")
    print("  While it runs, anyone signed in to that Claude account can do so. It stops after "
          f"{hours_words(rc_max_hours(cfg))}.")
    cfg["allow_remote_control"] = ask_yes("Let the dashboard start Remote Control on this machine?",
                                          rc_allowed(cfg))

    agents = cfg.setdefault("agents", {})
    fresh = not agents  # first run: suggest one agent of each kind that is installed
    claude = shutil.which("claude")
    if claude:
        setup_claude_agents(agents, claude, fresh)
    else:
        print("\nClaude Code was not found on PATH. Install it, then run init again to add Claude agents.")
    gemini = shutil.which("gemini")
    if gemini:
        setup_gemini_agents(agents, gemini, fresh)
    else:
        print("\nGemini CLI was not found on PATH (optional).")

    print()
    if ask_yes("Add the built-in 'selftest' agent (a fake agent for checking the plumbing)?",
               "selftest" in agents or fresh):
        agents["selftest"] = {"preset": "selftest", "label": "Self-test (fake agent)"}
    else:
        agents.pop("selftest", None)

    ask_credits_agent(cfg)

    projects = cfg.setdefault("projects", {})
    projects["chat"] = str(make_chat_workspace())
    print(f"\nThe 'chat' project points at {projects['chat']} (for questions that aren't about a code folder).")
    while ask_yes("Add a project folder now?", False):
        pname = ask("  Short name (no spaces), e.g. myapp")
        ppath = ask("  Full path to the folder")
        if pname and ppath and os.path.isdir(expand_path(ppath)):
            projects[pname] = ppath
            print(f"  added {pname}")
        else:
            print("  skipped: that folder doesn't exist")

    ask_git_accounts(cfg)  # after the projects, so one added just now can have an account of its own

    save_config(path, cfg)
    print(f"\nSaved {path}. Agents: {', '.join(agents) or 'none yet'}.")
    if cfg["tags"]:
        print("Accept the tags on the machine's page in the dashboard before tasks for a tag can run here.")
    print("Next: run 'python runner.py test <agent>' to check each agent, then start the runner.")
    return 0


def cmd_enroll(args: argparse.Namespace) -> int:
    """Connect this machine to the hub: with a one-time code (it gets its own token), or with a
    token you already have. Only the hub part of runner.json changes."""
    path = Path(args.config)
    cfg = load_config(path) if path.exists() else {"agents": {}, "projects": {}}
    hub_url = clean_hub_url(args.hub or cfg.get("hub_url", ""))
    if not hub_url:
        print("Give the hub's address with --hub, e.g. --hub http://192.168.100.20:8080")
        return 2
    code = args.code or os.environ.get("BULLPEN_ENROLL_CODE", "")
    token = args.token or os.environ.get("BULLPEN_TOKEN", "")
    name = args.name or os.environ.get("BULLPEN_NAME", "")
    if code:
        ok, message = enroll_with_code(cfg, hub_url, code, name)
    elif token:
        trial = {**cfg, "hub_url": hub_url, "token": token, "name": name or cfg.get("name") or default_machine_name()}
        ok, message = check_token(trial)
        if ok:
            cfg.update(hub_url=hub_url, token=token, name=trial["name"])
    else:
        print("Give a one-time code from 'Add a machine' (--code, or BULLPEN_ENROLL_CODE), "
              "or a runner token (--token, or BULLPEN_TOKEN).")
        return 2
    if not ok:
        print(f"Not connected: {message}.")
        return 1
    cfg.setdefault("projects", {}).setdefault("chat", str(make_chat_workspace()))
    save_config(path, cfg)
    print(f"Connected to {hub_url}: {message}. Saved in {path}.")
    return 0


def cmd_claude_accounts(args: argparse.Namespace) -> int:
    """For the setup scripts: one line per Claude agent, tab-separated:
    name, its settings folder, 'default' (the plain ~/.claude login) or 'own', and how it signs in
    ('login' for a subscription, 'key' or 'token' when no browser login is needed)."""
    cfg = load_config(Path(args.config))
    for name in cfg["agents"]:
        spec = agent_spec(cfg, name)
        if spec.get("preset") != "claude":
            continue
        env = build_env(spec)
        custom = (spec.get("env") or {}).get("CLAUDE_CONFIG_DIR")
        folder = expand_path(str(custom)) if custom else str(HOME / ".claude")
        how = "key" if env.get("ANTHROPIC_API_KEY") else "token" if env.get("CLAUDE_CODE_OAUTH_TOKEN") else "login"
        print(f"{name}\t{folder}\t{'own' if custom else 'default'}\t{how}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config(Path(args.config))
    print(f"Machine name: {cfg.get('name')}   hub: {cfg.get('hub_url')}   runner {VERSION}")
    print(f"Projects folder: {expand_path(cfg['projects_root'])}   "
          f"remote projects: {'on' if cfg.get('allow_remote_projects') else 'off'}   "
          f"remote update: {'on' if cfg.get('allow_remote_update') else 'off'}")
    print(f"Tags: {', '.join(heartbeat_tags(cfg))}   "
          f"subtasks from other machines: {'on' if cfg.get('accept_delegated_tasks') is True else 'off'}")
    skipped = clean_tags(cfg.get("tags", []))[1]
    if skipped:
        print(f"  left out of 'tags': {', '.join(skipped)} (at most {MAX_OWN_TAGS} words of letters, digits, "
              "'.', '_' and '-', up to 32 characters each)")
    print("\nAgents:")
    for name in cfg["agents"]:
        st = agent_status(cfg, name)
        mark = "ok " if st["ready"] else "!! "
        account = "account " + st["account"] if st.get("account") else "(no label)"
        print(f"  {mark}{name:<12} {st['label']:<28} {account:<20} {st['note']}")
    credits, why = credits_agent(cfg)
    if credits:
        print(f"\nCredits agent: {credits} (the hub may run background work on it, paid by platform credits, "
              "when you turn that on)")
    elif why:
        print(f"\nCredits agent: none ({why})")
    else:
        print("\nCredits agent: none")
    git = git_account_info(cfg)
    accounts = git_accounts(cfg)[0]
    if not (accounts or git["git_account_problems"] or git["default_git_account"] or git["project_git_accounts"]):
        print("\nGit accounts: none")
    else:
        # Which credentials each account has, never their values (nor its commit name or email).
        print("\nGit accounts:")
        for name, account in sorted(accounts.items()):
            print(f"  ok {name:<12} {account['host']:<24} {account['kind']:<7} {git_credentials(account)}")
        for problem in git["git_account_problems"]:
            print(f"  !! {problem}")
        print(f"  default for every project: {git['default_git_account'] or 'none'}")
        for project, name in sorted(git["project_git_accounts"].items()):
            later = "" if project in cfg["projects"] else " (applies when that project is cloned)"
            print(f"  default for {_shown(project)}: {name}{later}")
    print("\nProjects:")
    for name, p in sorted(cfg["projects"].items()):
        exists = "ok " if os.path.isdir(expand_path(p)) else "!! "
        print(f"  {exists}{name:<12} {p}")
    return 0


def cmd_add_project(args: argparse.Namespace) -> int:
    path = Path(args.config)
    cfg = load_config(path)
    folder = expand_path(args.path)
    if not os.path.isdir(folder):
        print(f"{folder} is not a folder")
        return 1
    cfg["projects"][args.name] = args.path
    save_config(path, cfg)
    print(f"Added project {args.name} -> {args.path}. A running runner picks this up within 15 seconds.")
    return 0


def cmd_remove_project(args: argparse.Namespace) -> int:
    path = Path(args.config)
    cfg = load_config(path)
    if cfg["projects"].pop(args.name, None) is None:
        print(f"No project called {args.name}")
        return 1
    save_config(path, cfg)
    print(f"Removed project {args.name}.")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run one tiny prompt through an agent locally, without the hub."""
    cfg = load_config(Path(args.config))
    if args.agent not in cfg["agents"]:
        print(f"No agent called {args.agent}. Configured: {', '.join(cfg['agents'])}")
        return 1
    print(f"Asking {args.agent} a test question (this can take up to a minute)...")
    result: dict = {}

    class LocalHub:
        def call(self, path: str, payload: dict, timeout: float = 30):
            if path.endswith("/log"):
                for line in payload["lines"]:
                    print("   ", line)
            else:
                result.update(payload)
            return 200, {}

    fake_runner = argparse.Namespace(cfg=cfg, hub=LocalHub(), limits=LimitStore(path=None))
    job = Job(fake_runner, {"id": 0, "agent": args.agent, "project": args.project, "autonomy": "readonly",
                            "prompt": "Reply with exactly one word: pong", "options": {"timeout_min": 3}})
    job.run()
    if result.get("status") == "done":
        print(f"\nOK: {args.agent} answered: {(result.get('result') or '').strip()[:200]}")
        return 0
    print(f"\nFAILED: {result.get('error')}")
    return 1


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} minutes ago"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} hours ago"
    return f"{round(seconds / 86400)} days ago"


def _status_current(w: dict) -> bool:
    """A window's status still counts unless a newer figure for how much is used came after it
    (Check limits reports no status). The same rule as the dashboard's statusCurrent()."""
    status_at = _num(w.get("status_at")) or 0
    used_at = _num(w.get("used_at")) or 0
    return _num(w.get("used")) is None or not status_at or not used_at or used_at <= status_at + 1


def _window_expired(w: dict, now: float) -> bool:
    resets = _num(w.get("resets_at"))
    return resets is not None and resets < now


def limit_reached(status: dict | None, windows: dict, now: float) -> tuple:
    """Whether Claude Code still refuses requests on the account: (True, the time jobs work again,
    or None when unknown) or (False, None). Only the account's own top-level 'rejected' counts, and
    only until a window resets or a newer figure says otherwise: the same rule as the dashboard's
    limitReachedUntil()."""
    if not isinstance(status, dict) or status.get("value") != "rejected":
        return False, None
    listed = [w for w in (windows or {}).values() if isinstance(w, dict)]
    still = [w for w in listed if w.get("status") == "rejected" and not _window_expired(w, now)
             and (_status_current(w) or (_num(w.get("used")) or 0) >= 1)]
    if still:
        times = [t for t in (_num(w.get("resets_at")) for w in still) if t is not None]
        return True, (max(times) if times else None)
    since = (_num(status.get("observed_at")) or 0) + 1
    newer = any(max(_num(w.get("used_at")) or 0, _num(w.get("status_at")) or 0) > since for w in listed)
    reset = any(_num(w.get("resets_at")) is not None and since < _num(w.get("resets_at")) <= now for w in listed)
    return (False, None) if newer or reset else (True, None)


def cmd_limits(args: argparse.Namespace) -> int:
    """Show the Claude plan-limit figures this machine keeps for its agents (as the dashboard gets them)."""
    cfg = load_config(Path(args.config))
    if args.agent and args.agent not in cfg["agents"]:
        print(f"No agent called {args.agent}. Configured: {', '.join(cfg['agents'])}")
        return 1
    store = LimitStore(LIMITS_FILE)
    now = time.time()
    shown = 0
    for name in cfg["agents"]:
        if args.agent and name != args.agent:
            continue
        spec = agent_spec(cfg, name)
        view = store.view(name)
        if "limits" not in agent_features(spec, build_env(spec)) and not view["windows"]:
            continue
        shown += 1
        account = cfg["agents"][name].get("account")
        print(f"{name}" + (f" (account {account})" if account else " (no account label)"))
        if not view["windows"]:
            print("  No figures yet. They come with this agent's next job, or with Check limits on the dashboard.")
        for wid in sorted(view["windows"], key=window_order):
            w = view["windows"][wid]
            source = "Check limits" if w.get("source") == "check" else "a job"
            reading = f"from {source}, {_ago(now - (_num(w.get('observed_at')) or now))}"
            if _window_expired(w, now):
                # The figure is from before the reset, so it says nothing about now (as on the dashboard).
                gone = f"reset at {_local_time(w.get('resets_at'))}; no reading since"
                print(f"  {window_label(wid):<20} {gone:<52} {reading}")
                continue
            used = _num(w.get("used"))
            status = w.get("status") if _status_current(w) else None
            figure = f"{round(used * 100)}% used" if used is not None else "use unknown"
            if used is not None and used > 1:
                figure += " (over the limit)"
            elif status != "rejected" and (used is None or used < 1) and (
                    status == "allowed_warning" or (used is not None and used >= 0.9)):
                figure += " (getting close)"
            when = _local_time(w.get("resets_at")) or w.get("resets_text") or ""
            print(f"  {window_label(wid):<20} {figure:<28} " + (f"resets {when:<16} " if when else " " * 24)
                  + reading)
        reached, until = limit_reached(view.get("status"), view["windows"], now)
        if reached:
            print("  Limit reached: Claude Code refuses requests on this account until "
                  + (f"{_local_time(until)}." if _local_time(until) else "the reset."))
        overage = view.get("overage") or {}
        if overage.get("using"):
            print("  Using extra usage (billed separately).")
    if not shown:
        print("No agent on this machine reports Claude plan limits.")
    else:
        print("\nTimes are this machine's local time.")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    """Save a note to the shared memory (used by the /remember skill as a fallback)."""
    cfg = load_config(Path(args.config))
    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        print("Nothing to remember: give the note as text or on stdin.")
        return 1
    who = args.source or os.environ.get("BULLPEN_AGENT") or "cli"
    status, body = Hub(lambda: cfg).call("/api/runner/notes/add", {
        "text": text, "scope": args.scope, "tags": args.tags, "pinned": args.pin,
        "source": f"{who}@{cfg.get('name') or socket.gethostname()}"})
    if status != 200:
        print(f"The hub refused the note ({status}): {(body or {}).get('detail')}")
        return 1
    requested = "pin-requested" in (body.get("tags") or "").split(",")
    print(f"Saved note #{body['id']} ({body['scope']}"
          f"{'; pin requested, approve it on the Memory page' if requested else ''}).")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """Search the shared memory and print matching notes."""
    cfg = load_config(Path(args.config))
    status, body = Hub(lambda: cfg).call("/api/runner/notes/search", {
        "query": " ".join(args.query), "scope": args.scope or "", "limit": args.limit})
    if status != 200:
        print(f"The hub answered {status}: {(body or {}).get('detail')}")
        return 1
    notes = body.get("notes") or []
    if not notes:
        print("No matching notes.")
    for n in notes:
        tags = f" #{' #'.join(n['tags'].split(','))}" if n.get("tags") else ""
        pin = " [pinned]" if n.get("pinned") else ""
        print(f"- (#{n['id']}, {n['scope']}{pin}{tags}) {n['text']}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Fetch the shared context now and wire it into this machine's agents."""
    runner = Runner(Path(args.config))
    result = runner.sync_from_hub(lambda *lines: [print("  " + ln) for ln in lines])
    print(result.get("result") or result.get("error"))
    return 0 if result.get("status") == "done" else 1


# ================================================================= uninstall
# `runner.py uninstall` undoes the machine setup (guides 5 and 6): the runner's service, its folder
# (~/.bullpen), the bullpen command, the commands made for extra Claude accounts, and what the settings sync
# wrote into the agents' own settings. It takes away only what it can tell is ours: a file with our marker or
# our name, a block between our markers, an MCP server the sync recorded in wired.json (or the hub's own
# 'bullpen'), a service that runs the runner from this folder. The agents' logins, credentials and sessions
# are never touched.
SYSTEMD_UNIT = "bullpen-runner.service"
WINDOWS_TASK = "Agent Bullpen Runner"
SETUP_WRAPPER_RE = re.compile(r"made by the bullpen setup|Claude Code signed in to account")
WRAPPER_DIR_RES = (re.compile(r'export CLAUDE_CONFIG_DIR="([^"\r\n]+)"'),
                   re.compile(r'set "CLAUDE_CONFIG_DIR=([^"\r\n]+)"'))
SMALL_FILE = 64 * 1024  # our commands and add-ons are far smaller (Claude Code itself lives in ~/.local/bin too)
PATH_LINE_MARK = "# added by bullpen setup"
TASK_QUERY = ("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
              f"$t = Get-ScheduledTask -TaskName '{WINDOWS_TASK}' -ErrorAction SilentlyContinue; "
              "if (-not $t) { exit 3 }; $t.Actions | ForEach-Object { $_.Execute + ' ' + $_.Arguments }")
TASK_REMOVE = (f"Stop-ScheduledTask -TaskName '{WINDOWS_TASK}' -ErrorAction SilentlyContinue; "
               f"Unregister-ScheduledTask -TaskName '{WINDOWS_TASK}' -Confirm:$false -ErrorAction Stop")

Run = Callable[..., "tuple[int, str]"]


def run_quiet(argv: list[str], env: Optional[dict] = None, timeout: float = 90) -> tuple[int, str]:
    """Run a program for the uninstaller: (exit code, what it printed). 127 when it isn't there."""
    try:
        res = subprocess.run(argv, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=timeout, stdin=subprocess.DEVNULL, creationflags=0x08000000 if IS_WINDOWS else 0)
    except FileNotFoundError:
        return 127, f"{argv[0]} not found"
    except (OSError, subprocess.TimeoutExpired) as err:
        return 1, str(err)
    return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()


class UninstallPlan:
    """What `uninstall` takes away (each with the step that does it), and what it leaves, with why."""

    def __init__(self, home: Path) -> None:
        self.steps: list[tuple[str, Callable[[], None]]] = []
        self.kept: list[str] = []
        # Paths in the home folder are shown as ~/... (~\... on Windows), which reads more easily.
        self._home_re = re.compile(re.escape(str(home)) + r"(?=[\\/]|$|[\s,:.)])")

    def short(self, text: str) -> str:
        return self._home_re.sub("~", text)

    def remove(self, what: str, action: Callable[[], None]) -> None:
        self.steps.append((self.short(what), action))

    def keep(self, what: str) -> None:
        what = self.short(what)
        if what not in self.kept:
            self.kept.append(what)

    def describe(self) -> str:
        lines = []
        if self.steps:
            lines += ["This removes:"] + [f"  - {what}" for what, _ in self.steps]
        if self.kept:
            lines += ["This leaves alone:"] + [f"  - {what}" for what in self.kept]
        return "\n".join(lines)

    def run(self, say: Callable[[str], Any] = print) -> list[str]:
        """Take each step in order; one that fails doesn't stop the others. Returns what failed."""
        failed = []
        for what, action in self.steps:
            try:
                action()
            except Exception as err:  # say what is left and go on: running uninstall again finishes it
                failed.append(what)
                say(f"  NOT removed: {what} ({self.short(str(err))})")
            else:
                say(f"  removed: {what}")
        return failed


def without_block(text: str) -> str | None:
    """`text` without our managed block and the blank line set_block put before it; None when it has none."""
    found = BLOCK_RE.search(text)
    if not found:
        return None
    before, after = text[:found.start()].rstrip("\r\n"), text[found.end():].lstrip("\r\n")
    if before.strip() and after.strip():
        return before + "\n\n" + after
    rest = before if before.strip() else after
    return rest.rstrip("\r\n") + "\n" if rest.strip() else ""


def _small_text(path: Path) -> str | None:
    """A small plain file's text (never a link); None for anything else."""
    if not _plain_file(path):
        return None
    try:
        if path.stat().st_size > SMALL_FILE:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def made_by_runner(path: Path) -> bool:
    """A file the settings sync wrote: it carries our marker, or is an add-on file from the first release."""
    text = _small_text(path)
    return text is not None and (MARKER in text or hashlib.sha256(text.encode()).hexdigest() in LEGACY_KIT_SHA256)


def remove_folder(path: Path) -> None:
    """Delete a folder and everything in it; read-only files (git makes some) are made writable first."""
    def writable_then_retry(func: Callable, name: str, _err: Any) -> None:
        os.chmod(name, stat.S_IRWXU)
        func(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable_then_retry)
    else:
        shutil.rmtree(path, onerror=writable_then_retry)


def _plan_block(plan: UninstallPlan, path: Path, label: str) -> None:
    try:
        rest = without_block(path.read_text(encoding="utf-8", errors="replace")) if _plain_file(path) else None
    except OSError:
        rest = None
    if rest is None:
        return

    def remove() -> None:
        now = without_block(path.read_text(encoding="utf-8", errors="replace"))
        if now is None:
            return
        if now.strip():
            write_if_changed(path, now)
        else:
            path.unlink()

    plan.remove(f"{label}: the Agent Bullpen block in {path}"
                + ("" if rest.strip() else " (the file holds nothing else, so it goes too)"), remove)


def _plan_kit(plan: UninstallPlan, folder: Path, label: str) -> None:
    """The add-on files in `folder` that carry our marker, and the folders they leave empty."""
    if not folder.is_dir() or folder.is_symlink():
        return
    files = sorted(p for p in folder.rglob("*") if made_by_runner(p))
    if not files:
        return

    def remove() -> None:
        for file in files:
            file.unlink(missing_ok=True)
            parent = file.parent
            while parent != folder and _inside(parent, folder):
                try:
                    parent.rmdir()  # only when it is empty: a file of your own keeps it
                except OSError:
                    break
                parent = parent.parent

    names = sorted({f.relative_to(folder).parts[0] for f in files})
    plan.remove(f"{label}: the Agent Bullpen add-ons in {folder} ({', '.join(names)})", remove)


def _plan_mcp_file(plan: UninstallPlan, path: Path, ours: set, label: str) -> None:
    """Our MCP servers in a JSON settings file (Gemini CLI's settings.json, Antigravity's mcp_config.json)."""
    if not path.exists():
        return
    try:
        data, _ = load_jsonc(path)
    except (OSError, ValueError):
        plan.keep(f"{path}: it couldn't be read, so its MCP servers stay as they are")
        return
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    found = sorted(n for n in ours if isinstance(servers, dict) and n in servers)
    if not found:
        return

    def remove() -> None:
        data, had_comments = load_jsonc(path)
        current = data.get("mcpServers") or {}
        for name in found:
            current.pop(name, None)
        if current:
            data["mcpServers"] = current
        else:
            data.pop("mcpServers", None)
        if had_comments:  # the file is written back as plain JSON, so keep a copy with your comments
            shutil.copy2(path, path.with_name(path.name + ".bak-bullpen-uninstall"))
        write_if_changed(path, json.dumps(data, indent=2) + "\n", private=True)

    plan.remove(f"{label}: the MCP server{'s' if len(found) > 1 else ''} {', '.join(found)} in {path}", remove)


def _claude_folders(cfg: dict, home: Path, state: dict) -> list[tuple[Path, bool, dict | None]]:
    """Each Claude Code settings folder the sync may have written to: (folder, its own CLAUDE_CONFIG_DIR?,
    the spec of an agent that uses it, if any). The same folders Wiring.claude picks, and those it recorded."""
    found: dict[str, tuple[Path, bool, dict | None]] = {}
    for agent in cfg.get("agents") or {}:
        try:
            spec = agent_spec(cfg, agent)
        except (KeyError, TypeError, AttributeError):
            continue
        if spec.get("preset") != "claude":
            continue
        custom = build_env(spec).get("CLAUDE_CONFIG_DIR")
        folder = Path(custom) if custom else home / ".claude"
        found.setdefault(str(folder), (folder, bool(custom), spec))
    for key in state:
        if key.startswith("claude:"):
            folder = Path(key[len("claude:"):])
            found.setdefault(str(folder), (folder, folder != home / ".claude", None))
    found.setdefault(str(home / ".claude"), (home / ".claude", False, None))
    return list(found.values())


def _plan_claude_servers(plan: UninstallPlan, folder: Path, custom: bool, spec: dict | None, home: Path,
                         state: dict, run: Run) -> None:
    """Our MCP servers in one Claude Code account, taken out with Claude Code's own CLI (as the sync adds them)."""
    claude_json = folder / ".claude.json" if custom else home / ".claude.json"
    try:
        servers = json.loads(claude_json.read_text(encoding="utf-8")).get("mcpServers") if claude_json.exists() else {}
    except (OSError, ValueError, AttributeError):
        plan.keep(f"{claude_json}: it couldn't be read, so its MCP servers stay as they are")
        return
    names = sorted((set(state.get(f"claude:{folder}", [])) | RESERVED_SERVERS) & set(servers or {}))
    if not names:
        return
    exe = (resolve_command(spec) if spec else None) or shutil.which("claude")
    if not exe:
        plan.keep(f"the MCP servers {', '.join(names)} in {claude_json}: Claude Code wasn't found to take them out "
                  "(with Claude Code: claude mcp remove -s user NAME)")
        return
    env = build_env(spec) if spec else dict(os.environ)
    env.pop("CLAUDECODE", None)
    if custom:
        env["CLAUDE_CONFIG_DIR"] = str(folder)
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)

    def remove() -> None:
        for name in names:
            code, said = run([exe, "mcp", "remove", "-s", "user", name], env)
            if code:
                raise RuntimeError(f"'claude mcp remove' for {name} exited {code}: {redact(said[-300:])}")

    plan.remove(f"Claude Code: the MCP server{'s' if len(names) > 1 else ''} {', '.join(names)} in {claude_json}",
                remove)


def _plan_service(plan: UninstallPlan, home: Path, base_dir: Path, system: str, run: Run) -> None:
    """The service that starts the runner: only one that runs the runner from `base_dir`."""
    if system == "windows":
        code, said = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", TASK_QUERY], None)
        if code == 3:
            return
        if code:
            plan.keep(f"the scheduled task '{WINDOWS_TASK}': couldn't look at it ({said[-200:] or code}). Remove it "
                      "in Task Scheduler if it is there")
        elif str(base_dir).lower() in said.lower():
            def remove_task() -> None:
                code, said = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", TASK_REMOVE], None)
                if code:
                    raise RuntimeError(said[-300:] or f"PowerShell exited {code}")

            plan.remove(f"the scheduled task '{WINDOWS_TASK}' that starts the runner (stopped first)", remove_task)
        else:
            plan.keep(f"the scheduled task '{WINDOWS_TASK}': it starts a runner in another folder, not {base_dir}")
        return
    if system == "macos":
        plist = home / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        text = _small_text(plist)
        if text is None:
            return
        escaped = str(base_dir).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        if escaped not in text:
            plan.keep(f"{plist}: it starts a runner in another folder, not {base_dir}")
            return

        uid = os.getuid() if hasattr(os, "getuid") else 0

        def remove_agent() -> None:
            run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"], None)  # fails when it isn't loaded: fine
            plist.unlink(missing_ok=True)

        plan.remove(f"the launchd agent {LAUNCHD_LABEL} that starts the runner (stopped first), {plist}", remove_agent)
        return
    unit = home / ".config" / "systemd" / "user" / SYSTEMD_UNIT
    text = _small_text(unit)
    if text is None:
        return
    if str(base_dir) not in text:
        plan.keep(f"{unit}: it starts a runner in another folder, not {base_dir}")
        return

    def remove_unit() -> None:
        code, said = run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT], None)
        wanted = unit.parent / "default.target.wants" / SYSTEMD_UNIT
        if wanted.is_symlink():
            wanted.unlink()
        unit.unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], None)
        if code and code != 127:
            raise RuntimeError(f"systemctl couldn't stop it ({said[-200:]}); its file is gone, so it won't start "
                               "again. Log in as this user and run uninstall again, or restart the computer")

    plan.remove(f"the systemd user service bullpen-runner (stopped first), {unit}", remove_unit)


def _plan_commands(plan: UninstallPlan, home: Path) -> None:
    """The bullpen command, and the commands the setup made for extra Claude accounts (claude-work, ...)."""
    bin_dir = home / ".local" / "bin"
    for name in ("bullpen", "bullpen.cmd"):
        path = bin_dir / name
        text = _small_text(path)
        if text is not None and MARKER in text:
            plan.remove(f"the bullpen command, {path}", lambda path=path: path.unlink(missing_ok=True))
        elif path.exists() or path.is_symlink():
            plan.keep(f"{path}: Agent Bullpen didn't make it")
    if not bin_dir.is_dir():
        return
    for path in sorted(bin_dir.iterdir()):
        if path.name in ("bullpen", "bullpen.cmd"):
            continue
        text = _small_text(path)
        if text is None or "CLAUDE_CONFIG_DIR" not in text or not SETUP_WRAPPER_RE.search(text):
            continue
        plan.remove(f"the command {path.stem} for an extra Claude account, {path}",
                    lambda path=path: path.unlink(missing_ok=True))
        for pattern in WRAPPER_DIR_RES:
            login = pattern.search(text)
            if login:
                plan.keep(f"{os.path.expandvars(login.group(1))}: the login and settings of the {path.stem} account")
                break


def _base_dir_problem(base_dir: Path, home: Path) -> str:
    """'' when the runner's folder can be deleted as a whole, else why not."""
    base, own_home = Path(os.path.realpath(base_dir)), Path(os.path.realpath(home))
    if base == Path(base.anchor) or base == own_home or _inside(own_home, base):
        return "it is your home folder, or holds it"
    if base.name != ".bullpen" and not (base / "runner.json").exists():
        return "it has no runner.json, so it may not be the runner's folder"
    return ""


def _plan_base_dir(plan: UninstallPlan, cfg: dict, base_dir: Path, home: Path) -> None:
    if not base_dir.is_dir():
        return
    problem = _base_dir_problem(base_dir, home)
    if problem:
        plan.keep(f"{base_dir}, BULLPEN_HOME: {problem}. Delete what the runner put there yourself")
        return
    inside = sorted(name for name, folder in (cfg.get("projects") or {}).items()
                    if name != "chat" and isinstance(folder, str) and _inside(Path(expand_path(folder)), base_dir))

    def remove() -> None:
        lock_file = base_dir / "runner.lock"
        if lock_file.exists():
            lock = FileLock(lock_file)  # the runner holds it while it runs; the service was stopped above
            if not lock.acquire(timeout=20):
                raise RuntimeError("a runner is still running on this machine. Stop it, then run uninstall again")
            lock.release()
        remove_folder(base_dir)

    plan.remove(f"{base_dir}: the runner, its settings and runner token, its logs and the chat workspace"
                + (f". It also holds the project folders {', '.join(inside)}" if inside else ""), remove)


def plan_uninstall(cfg: dict, home: Optional[Path] = None, base_dir: Optional[Path] = None,
                   system: str = PLATFORM, run: Run = run_quiet) -> UninstallPlan:
    """Everything `uninstall` would take away from this machine, in the order it does it: the service
    first (so no runner writes again), the agents' settings, the commands, and the runner's folder last."""
    home = HOME if home is None else home
    base_dir = BASE_DIR if base_dir is None else base_dir
    plan = UninstallPlan(home)
    try:
        state = json.loads((base_dir / "wired.json").read_text(encoding="utf-8"))
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}

    _plan_service(plan, home, base_dir, system, run)

    folders = _claude_folders(cfg, home, state)
    for folder, custom, spec in folders:
        if not folder.is_dir():
            continue
        _plan_block(plan, folder / "CLAUDE.md", "Claude Code")
        _plan_kit(plan, folder / "skills", "Claude Code")
        _plan_claude_servers(plan, folder, custom, spec, home, state, run)

    _plan_kit(plan, home / ".agents" / "skills", "Shared skills folder")

    gemini = Path(os.environ.get("GEMINI_CLI_HOME") or home) / ".gemini"
    if gemini.is_dir():
        _plan_block(plan, gemini / "GEMINI.md", "Gemini CLI")
        shared = gemini / "bullpen-shared.md"
        if _plain_file(shared):
            plan.remove(f"Gemini CLI: the copy of the shared notes, {shared}", lambda: shared.unlink(missing_ok=True))
        _plan_kit(plan, gemini / "commands", "Gemini CLI")
        _plan_kit(plan, gemini / "skills", "Gemini CLI")
        _plan_mcp_file(plan, gemini / "settings.json", set(state.get("gemini") or []) | RESERVED_SERVERS, "Gemini CLI")
        _plan_kit(plan, gemini / "config" / "skills", "Antigravity")
        _plan_kit(plan, gemini / "antigravity-cli" / "skills", "Antigravity CLI")
        _plan_mcp_file(plan, gemini / "config" / "mcp_config.json",
                       set(state.get("antigravity") or []) | RESERVED_SERVERS, "Antigravity")

    _plan_commands(plan, home)
    _plan_base_dir(plan, cfg, base_dir, home)

    # What stays, and why.
    logins = [str(f) for f, _, _ in folders if f.is_dir()] + ([str(gemini)] if gemini.is_dir() else [])
    if logins:
        plan.keep(f"the agents' own logins, sessions and settings: {', '.join(logins)}. Only what Agent Bullpen "
                  "added to them is taken out")
    if (gemini / ".env").exists():
        plan.keep(f"{gemini / '.env'}, which holds the Gemini API key")
    for backup in (gemini / "settings.json.bak-bullpen", gemini / "config" / "mcp_config.json.bak-bullpen"):
        if backup.exists():
            plan.keep(f"{backup}, a copy of your settings from before the first sync")
    rc_files = [str(rc) for rc in (home / ".bashrc", home / ".zshrc") if PATH_LINE_MARK in (_small_text(rc) or "")]
    if rc_files:
        plan.keep(f"the PATH line the setup added to {' and '.join(rc_files)}: Claude Code and Gemini CLI use it too")
    projects_root = Path(expand_path(str(cfg.get("projects_root") or home / "projects")))
    if projects_root.is_dir() and not _inside(projects_root, base_dir):
        plan.keep(f"your projects in {projects_root}")
    plan.keep("Claude Code, Gemini CLI, Python and Git themselves")
    return plan


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove Agent Bullpen from this machine: say what goes and what stays, ask, then do it."""
    path = Path(args.config)
    cfg: dict = {"agents": {}, "projects": {}}
    if path.exists():
        try:
            cfg = load_config(path)
        except (OSError, ValueError) as err:
            print(f"Couldn't read {path} ({err}); going on without it.")
    name = cfg.get("name") or default_machine_name()
    print(f"\nRemoving Agent Bullpen from this machine ('{name}' on the dashboard).\n")
    plan = plan_uninstall(cfg)
    print(plan.describe())
    if not plan.steps:
        print("\nNothing of Agent Bullpen's was found here.")
        return 0
    if args.dry_run:
        print("\nDry run: nothing was changed.")
        return 0
    if not args.yes and not ask_yes("\nRemove all of this?", False):
        print("Nothing was changed.")
        return 1
    print()
    failed = plan.run()
    if failed:
        print("\nSome of it is still there (see above). Run uninstall again once that is fixed: it takes away "
              "only what is left.")
    else:
        print("\nAgent Bullpen is removed from this machine.")
    print(f"On the hub's dashboard, click Forget this machine on the card for '{name}' once it shows as offline. "
          "That cancels its runner token and its MCP token.")
    return 1 if failed else 0


def cmd_run(args: argparse.Namespace) -> int:
    setup_logging()
    path = Path(args.config)
    if not path.exists():
        log.error("no config at %s; run 'python runner.py init' first", path)
        return 1
    # One runner per machine: a second copy would steal and lose the first one's jobs. After a
    # self-update the old copy may still be exiting, so wait a little for the lock.
    instance_lock = FileLock(BASE_DIR / "runner.lock")
    if not instance_lock.acquire(timeout=20):
        log.error("another runner is already running on this machine (%s is locked); exiting",
                  BASE_DIR / "runner.lock")
        return 0
    runner = Runner(path)

    # Remote Control servers end with the runner: on SIGTERM (systemd, launchd), Ctrl+C, and at exit.
    def stop_and_exit(signum: int, frame: Any) -> None:
        runner.rc.stop_all(why="the runner stopped")
        sys.exit(0)

    if not IS_WINDOWS:
        signal.signal(signal.SIGTERM, stop_and_exit)
        signal.signal(signal.SIGINT, stop_and_exit)
    atexit.register(runner.rc.stop_all, False, "the runner stopped")
    runner.run_forever()
    return 0


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Agent Bullpen runner")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help=f"config file (default {DEFAULT_CONFIG})")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="connect to the hub and run jobs (default)")
    p = sub.add_parser("init", help="interactive setup")
    p.add_argument("--skip-hub", action="store_true",
                   help="keep the hub address, name and token already in the config (set by 'enroll')")
    p = sub.add_parser("enroll", help="connect this machine to the hub with a one-time code (or a token)")
    p.add_argument("--hub", default="", help="the hub's address, e.g. http://192.168.100.20:8080")
    p.add_argument("--code", default="", help="one-time code from 'Add a machine' (or set BULLPEN_ENROLL_CODE)")
    p.add_argument("--token", default="", help="a runner token instead of a code (or set BULLPEN_TOKEN)")
    p.add_argument("--name", default="", help="this machine's name on the dashboard (or set BULLPEN_NAME)")
    sub.add_parser("claude-accounts", help="list the Claude agents and their settings folders (for scripts)")
    sub.add_parser("list", help="show agents and projects")
    p = sub.add_parser("limits", help="show the Claude plan limits this machine last saw")
    p.add_argument("agent", nargs="?", default="", help="only this agent")
    p = sub.add_parser("test", help="send one test prompt to an agent")
    p.add_argument("agent")
    p.add_argument("--project", default="chat")
    p = sub.add_parser("add-project", help="offer a folder to the hub")
    p.add_argument("name")
    p.add_argument("path")
    p = sub.add_parser("remove-project", help="stop offering a folder")
    p.add_argument("name")
    p = sub.add_parser("remember", help="save a note to the shared memory")
    p.add_argument("text", nargs="*", help="the note (or pass it on stdin)")
    p.add_argument("--scope", default="global", help="global, agent:<name> or project:<name>")
    p.add_argument("--tags", default="", help="comma-separated tags")
    p.add_argument("--pin", action="store_true", help="ask for it to be pinned (you approve on the Memory page)")
    p.add_argument("--source", default="", help="who is saving it (default: the agent running this)")
    p = sub.add_parser("recall", help="search the shared memory")
    p.add_argument("query", nargs="*")
    p.add_argument("--scope", default="")
    p.add_argument("--limit", type=int, default=15)
    sub.add_parser("sync", help="update shared notes and agent settings from the hub now")
    p = sub.add_parser("uninstall", help="remove Agent Bullpen from this machine (shows what goes, then asks)")
    p.add_argument("--yes", action="store_true", help="don't ask before removing")
    p.add_argument("--dry-run", action="store_true", help="only show what would be removed and what stays")
    args = parser.parse_args()
    commands = {"init": cmd_init, "enroll": cmd_enroll, "claude-accounts": cmd_claude_accounts,
                "list": cmd_list, "limits": cmd_limits, "test": cmd_test, "add-project": cmd_add_project,
                "remove-project": cmd_remove_project, "remember": cmd_remember, "recall": cmd_recall,
                "sync": cmd_sync, "uninstall": cmd_uninstall, "run": cmd_run, None: cmd_run}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
