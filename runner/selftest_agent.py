#!/usr/bin/env python3
"""A pretend agent for testing the hub and runner without any real login.

It reads the prompt from stdin and prints events in the same format as
`claude -p --output-format stream-json`, so the whole pipeline (dashboard ->
hub -> runner -> agent -> back) can be checked in a minute. It never calls
any AI service. The only network it may use is the hub's job tools
(/mcp/job), from the config file the runner hands it with --mcp-config.

Like Claude Code, every answer reports its tokens and cost (made-up figures: each turn is
1000 input, 200 output, 300 cache read and 100 cache write tokens, $0.01), a usage-limit event
comes with every turn, and the prompt "/usage" prints plan usage the way Claude Code does.
The session's totals are kept in $BULLPEN_HOME/selftest/sessions/ when it exits normally, so a
resumed session reports them too.

With --gemini as the first argument it prints Gemini CLI's stream-json instead; its job tools
then come from the settings file in $GEMINI_CLI_SYSTEM_DEFAULTS_PATH, as Gemini CLI's would: only
when root owns that file and every folder above it, none writable by group or others (on Windows,
never). Otherwise it says on stderr that it skipped the file, as Gemini CLI does. With
--input-format stream-json it keeps its input open like Claude Code in a live session: every
user message on it (one JSON line each) is one turn, answered "Self-test reply <n>: <first
line>", until its input closes. With remote-control as the first argument it pretends to be
"claude remote-control": it prints an address and waits until it is stopped.

Environment knobs (for testing):
  SELFTEST_DELAY     seconds to "think" before answering (default 1)
  SELFTEST_FAIL      set to 1 to make the run fail
  SELFTEST_LIMITS    set to 0 to send no usage-limit event
  SELFTEST_UTIL      the 5-hour window's use in that event, as a fraction (default 0.23)
  SELFTEST_NO_SINCE  set to 1 to leave out the per-turn figures (model_usage_since_last_result)
  SELFTEST_USAGE     what "/usage" prints: "text" (no structured report), "none" (a login that
                     can't read plan usage); unset for both the report and the text
  SELFTEST_RC        how remote-control behaves: "untrusted", "apikey" or "token" (it fails the
                     way Claude Code does), "nourl" (it starts but never shows an address); unset
                     for a server that shows its address
  SELFTEST_GEMINI_DEFAULTS  set to "trusted" to read the --gemini settings file whoever owns it (for
                     tests that pretend the runner runs as root)

Directives: lines in the prompt that start with "SELFTEST:" make it do something
more. Their answer lines come first in the answer, before the usual text.
  SELFTEST: job-info        ask the hub's job tools which job this is
  SELFTEST: ask Bash npm test
                            ask the owner, through the permission prompt tool, whether a tool
                            call may run (as Claude Code does before a command), and wait
  SELFTEST: ask-big 25000   the same with a command of that many characters
  SELFTEST: leak-token      print the job token to stderr (the runner must mask it)
  SELFTEST: machines        list the machines this job may hand subtasks to
  SELFTEST: delegate machine=beta agent=three wait=60 prompt=Reply with pong
                            hand a subtask over (keys: machine, tag, agent, project, autonomy,
                            account, model, title, timeout_min; wait: seconds to wait for its
                            answer, default 60), then wait for it and answer with its first line
  SELFTEST: delegate-many 4 machine=beta agent=three prompt=...
                            hand that many subtasks over at once, without waiting
  SELFTEST: status 12       one of this job's subtasks: its status
  SELFTEST: limit-rejected  fail the way Claude Code does at a usage limit (signed in with a subscription;
                            with ANTHROPIC_API_KEY set there is no plan limit, and it answers as usual)
  SELFTEST: credits-low     with ANTHROPIC_API_KEY set, fail the way Claude Code does when the Console
                            organization has no credit left; without one it answers as usual
  SELFTEST: gemini-429      (with --gemini) fail the way Gemini CLI does at a quota limit
"""

from __future__ import annotations

import datetime
import itertools
import json
import math
import os
import re
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

JOB_SERVER = "bullpenjob"  # the hub's job tools, as the runner names them in the --mcp-config file
MCP_PROTOCOL_VERSION = "2025-11-25"
CLI_VERSION = "2.1.400"  # the Claude Code version it says it is
# What every turn costs, in Claude Code's modelUsage shape.
TURN_USAGE = {"inputTokens": 1000, "outputTokens": 200, "cacheReadInputTokens": 300,
              "cacheCreationInputTokens": 100, "costUSD": 0.01}
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens", "costUSD")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
# Claude Code's own words at a usage limit.
LIMIT_TEXT = "You've hit your 5-hour limit · resets 3pm (UTC)"
# The Anthropic API's words when the organization behind an API key has no credit left (HTTP 400).
CREDIT_TEXT = ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade "
               "or purchase credits.")
# Gemini CLI's words after its own retries of a 429 (they don't say 429).
GEMINI_QUOTA_MESSAGE = ("[API Error: You exceeded your current quota, please check your plan and billing details.]\n"
                        "Please wait and try again later. To increase your limits, request a quota increase through "
                        "AI Studio, or switch to another /auth method")
# Remote Control's own words when it can't start (SELFTEST_RC), as Claude Code prints them.
RC_FAILURES = {
    "untrusted": "Error: Workspace not trusted. Run claude in this folder once to trust it.",
    "apikey": "Remote Control requires claude.ai subscription auth",
    "token": ("Error: Remote Control requires a full-scope login token. Long-lived tokens (from `claude "
              "setup-token` or CLAUDE_CODE_OAUTH_TOKEN) are limited to inference-only for security reasons. "
              "Run `claude auth login` to use Remote Control."),
}
APPROVE_TIMEOUT = 900  # seconds an approval may wait for the owner
# Gemini mode: Gemini CLI's lowest settings layer, where the runner puts a job's tools.
GEMINI_DEFAULTS_VAR = "GEMINI_CLI_SYSTEM_DEFAULTS_PATH"
DELEGATE_WAIT = 60      # seconds "SELFTEST: delegate" waits for its subtask, unless it says wait=
STATUS_WAIT_MAX = 60    # the longest task_status waits in one call (the hub's limit)
MANY_MAX = 20           # "SELFTEST: delegate-many" hands over at most this many
DELEGATE_KEYS = ("machine", "tag", "agent", "project", "autonomy", "account", "model", "title", "timeout_min")
_ids = itertools.count(1)
_asks = itertools.count(1)


def emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def flag(args: list, name: str, default=None):
    """The value after `name` (or in `name=value`); `default` when it isn't there."""
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return default


# ------------------------------------------------------------ MCP client
def mcp_entry(config) -> dict | None:
    """The job tools' server entry from a --mcp-config value (a file path or JSON text)."""
    if isinstance(config, dict):
        return config
    if not config:
        return None
    try:
        if config.lstrip().startswith("{"):
            data = json.loads(config)
        else:
            with open(config, encoding="utf-8") as fh:
                data = json.load(fh)
    except (OSError, ValueError):
        return None
    entry = (data.get("mcpServers") or {}).get(JOB_SERVER) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) and entry.get("url") else None


def mcp_call(config, method: str, params: dict, timeout: float = 30) -> dict:
    """One JSON-RPC call to the server in `config` (a --mcp-config value or an entry). Returns the
    JSON-RPC reply; raises OSError (HTTP errors included) or ValueError."""
    entry = mcp_entry(config)
    if entry is None:
        raise ValueError("no job tools in the MCP config")
    ident = next(_ids)
    headers = {str(k): str(v) for k, v in (entry.get("headers") or {}).items()}
    headers.update({"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": MCP_PROTOCOL_VERSION})
    body = json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}).encode()
    req = urllib.request.Request(entry["url"], data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        stream = resp.headers.get("content-type", "").startswith("text/event-stream")
    if not stream:
        reply = json.loads(raw or "{}")
        return reply if isinstance(reply, dict) else {}
    reply = {}
    for line in raw.splitlines():  # progress notifications may come before the reply
        if line.startswith("data:") and line[5:].strip():
            event = json.loads(line[5:].strip())
            if isinstance(event, dict) and event.get("id") == ident:
                reply = event
    return reply


def tool_call(config, name: str, arguments: dict, timeout: float = 30, progress: bool = False) -> dict:
    """Call one job tool (asking for progress notifications with `progress`, as a client does for
    a call that may take a while). Its JSON answer, or {"ok": False, "error": ...}."""
    params = {"name": name, "arguments": arguments}
    if progress:
        params["_meta"] = {"progressToken": f"selftest-{os.getpid()}-{next(_ids)}"}
    try:
        reply = mcp_call(config, "tools/call", params, timeout)
    except urllib.error.HTTPError as err:
        return {"ok": False, "error": f"HTTP {err.code}"}
    except (OSError, ValueError) as err:
        return {"ok": False, "error": str(err) or type(err).__name__}
    if reply.get("error"):
        error = reply["error"]
        return {"ok": False, "error": str(error.get("message") if isinstance(error, dict) else error)}
    result = reply.get("result") or {}
    text = next((c.get("text") for c in result.get("content") or []
                 if isinstance(c, dict) and c.get("type") == "text"), "") or ""
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        data = None
    if not isinstance(data, dict):
        data = {"ok": False, "error": text or "no answer"}
    if result.get("isError"):
        data["ok"] = False
        data.setdefault("error", text or "error")
    return data


def listed_tools(config):
    """The job tools the agent sees: the server's tools/list, narrowed by the entry's includeTools
    and excludeTools (as Gemini CLI narrows them). None without job tools; an empty set when the
    server doesn't answer."""
    entry = mcp_entry(config)
    if entry is None:
        return None
    try:
        reply = mcp_call(entry, "tools/list", {}, timeout=10)
    except (OSError, ValueError):
        return set()
    tools = (reply.get("result") or {}).get("tools")
    names = {t.get("name") for t in tools if isinstance(t, dict)} if isinstance(tools, list) else set()
    if isinstance(entry.get("includeTools"), list):
        names &= set(entry["includeTools"])
    if isinstance(entry.get("excludeTools"), list):
        names -= set(entry["excludeTools"])
    return names


def connect_status(config) -> str:
    """'connected' after one tools/list that answers, else 'failed' (as Claude Code reports it)."""
    try:
        reply = mcp_call(config, "tools/list", {}, timeout=10)
    except (OSError, ValueError):
        return "failed"
    return "connected" if isinstance((reply.get("result") or {}).get("tools"), list) else "failed"


# ------------------------------------------------------------ directives
def job_token(config) -> str:
    entry = mcp_entry(config) or {}
    auth = str((entry.get("headers") or {}).get("Authorization") or "")
    return auth[len("Bearer "):] if auth.startswith("Bearer ") else ""


def directive_texts(prompt: str) -> list:
    """The SELFTEST: lines in the prompt, without "SELFTEST:", in order."""
    return [raw.strip()[len("SELFTEST:"):].strip() for raw in prompt.splitlines()
            if raw.strip().startswith("SELFTEST:")]


def directives(prompt: str) -> list:
    """The SELFTEST: lines in the prompt, as lists of words, in order."""
    return [text.split() or [""] for text in directive_texts(prompt)]


def has_directive(prompt: str, name: str) -> bool:
    return any(words[0] == name for words in directives(prompt))


def ask_owner(config, prompt_tool, tool_name: str, command: str) -> str:
    """Ask through the permission prompt tool whether `tool_name` may run `command`, as Claude Code
    does before a command in 'ask' jobs, and wait for the answer. The answer line."""
    prefix = f"mcp__{JOB_SERVER}__"
    if not prompt_tool or not str(prompt_tool).startswith(prefix):
        return "approval: not available (no permission prompt tool)"
    if mcp_entry(config) is None:
        return "approval: not available (no job tools)"
    arguments = {"tool_name": tool_name, "input": {"command": command, "description": "selftest"},
                 "tool_use_id": f"toolu_selftest_{next(_asks)}"}
    params = {"name": str(prompt_tool)[len(prefix):], "arguments": arguments,
              "_meta": {"progressToken": f"selftest-{os.getpid()}-{next(_ids)}"}}
    try:
        reply = mcp_call(config, "tools/call", params, timeout=APPROVE_TIMEOUT)
    except urllib.error.HTTPError as err:
        return f"approval: not available (HTTP {err.code})"
    except (OSError, ValueError) as err:
        return f"approval: not available ({str(err) or type(err).__name__})"
    if reply.get("error"):
        error = reply["error"]
        return f"approval: not available ({error.get('message') if isinstance(error, dict) else error})"
    result = reply.get("result") or {}
    text = next((c.get("text") for c in result.get("content") or []
                 if isinstance(c, dict) and c.get("type") == "text"), "") or ""
    try:
        decision = json.loads(text)
    except ValueError:
        decision = None
    if not isinstance(decision, dict):
        return f"approval: not available ({text[:200] or 'no answer'})"
    if decision.get("behavior") == "allow":
        updated = decision.get("updatedInput") if isinstance(decision.get("updatedInput"), dict) else {}
        return f"approval: allow ({str(updated.get('command', ''))[:1000]})"
    if decision.get("behavior") == "deny":
        return f"approval: deny ({decision.get('message') or ''})"
    return f"approval: not available ({text[:200]})"


def one_line(text) -> str:
    """A message as one line of the answer."""
    return " ".join(str(text or "").split())[:1000]


def first_text_line(text) -> str:
    return next((ln.strip() for ln in str(text or "").splitlines() if ln.strip()), "")[:1000]


def delegate_args(text: str) -> tuple:
    """`k=v ... prompt=<rest of the line>` as delegate_task's arguments, and the seconds to wait."""
    found = re.search(r"(?:^|\s)prompt=", text)
    head, prompt = (text[:found.start()], text[found.end():]) if found else (text, "")
    args: dict = {"prompt": prompt.strip()}
    wait = float(DELEGATE_WAIT)
    for word in head.split():
        key, eq, value = word.partition("=")
        if not eq:
            continue
        if key == "wait":
            try:
                wait = max(0.0, float(value))
            except ValueError:
                pass
        elif key == "timeout_min":
            args[key] = int(value) if value.isdigit() else value
        elif key in DELEGATE_KEYS:
            args[key] = value
    return args, wait


def has_tool(config, name: str) -> bool:
    return name in (listed_tools(config) or set())


def delegate(config, text: str) -> list:
    """SELFTEST: delegate: hand a subtask over, then ask for its status (waiting up to a minute a
    time, as the tool's description says) until it has finished or the wait is up."""
    if not has_tool(config, "delegate_task"):
        return ["delegate: not available"]
    args, wait = delegate_args(text)
    made = tool_call(config, "delegate_task", args)
    if not made.get("ok"):
        return [f"delegate: error ({one_line(made.get('error'))})"]
    child = made.get("job_id")
    lines = [f"delegated: #{child}"]
    deadline = time.monotonic() + wait
    while True:
        wait_s = min(STATUS_WAIT_MAX, max(0, math.ceil(deadline - time.monotonic())))
        status = tool_call(config, "task_status", {"job_id": child, "wait_s": wait_s}, timeout=wait_s + 30,
                           progress=wait_s > 0)
        if not status.get("ok"):
            return lines + [f"subtask status: error ({one_line(status.get('error'))})"]
        if status.get("status") not in ("queued", "running") or time.monotonic() >= deadline:
            break
    lines.append(f"subtask status: {status.get('status')}")
    if status.get("status") == "done":
        lines.append(f"subtask result: {first_text_line(status.get('result'))}")
    elif status.get("error"):
        lines.append(f"subtask error: {first_text_line(status.get('error'))}")
    return lines


def delegate_many(config, text: str) -> list:
    """SELFTEST: delegate-many <n> ...: n subtasks at once, without waiting for them."""
    words = text.split(None, 1)
    count = int(words[0]) if words and words[0].isdigit() else 1
    if not has_tool(config, "delegate_task"):
        return ["delegate: not available"]
    args, _ = delegate_args(words[1] if len(words) > 1 else "")
    ids, errors = [], []
    for _ in range(min(count, MANY_MAX)):
        made = tool_call(config, "delegate_task", args)
        if made.get("ok"):
            ids.append(f"#{made.get('job_id')}")
        else:
            errors.append(f"delegate: error ({one_line(made.get('error'))})")
    return ["delegated: " + (",".join(ids) or "none")] + errors


def machines(config) -> str:
    """SELFTEST: machines: the names list_machines gives."""
    if not has_tool(config, "list_machines"):
        return "machines: not available"
    got = tool_call(config, "list_machines", {})
    if not got.get("ok"):
        return f"machines: error ({one_line(got.get('error'))})"
    names = [str(m.get("name")) for m in got.get("machines") or [] if isinstance(m, dict)]
    return "machines: " + (",".join(names) or "none")


def subtask_status(config, ident: str) -> str:
    """SELFTEST: status <id>: one look at a subtask."""
    if not has_tool(config, "task_status"):
        return "status: not available"
    ident = ident.lstrip("#")
    if not ident.isdigit():
        return "status: error (say: SELFTEST: status <job id>)"
    got = tool_call(config, "task_status", {"job_id": int(ident)})
    if not got.get("ok"):
        return f"status: error ({one_line(got.get('error'))})"
    return f"status: {got.get('status')}"


def run_directives(prompt: str, config, prompt_tool=None) -> list:
    """Answer lines for the SELFTEST: lines in the prompt, in order."""
    lines = []
    for text in directive_texts(prompt):
        words = text.split() or [""]
        what = words[0]
        if what == "job-info":
            if mcp_entry(config) is None:
                lines.append("job-info: not available (no job tools)")
                continue
            info = tool_call(config, "job_info", {})
            if info.get("ok"):
                tools = ",".join(str(t) for t in info.get("tools") or [])
                lines.append(f"job-info: #{info.get('job_id')} on {info.get('machine')} tools={tools}")
            else:
                lines.append(f"job-info: not available ({info.get('error')})")
        elif what == "ask":
            if len(words) < 3:
                lines.append("approval: not available (say: SELFTEST: ask <tool> <command>)")
                continue
            command = text.split(None, 2)[2]
            lines.append(ask_owner(config, prompt_tool, words[1], command))
        elif what == "ask-big":
            size = int(words[1]) if len(words) > 1 and words[1].isdigit() else 25000
            lines.append(ask_owner(config, prompt_tool, "Bash", "x" * size))
        elif what == "leak-token":
            print(f"token={job_token(config) or '(none)'}", file=sys.stderr, flush=True)
            lines.append("leaked")
        elif what == "machines":
            lines.append(machines(config))
        elif what == "delegate":
            lines.extend(delegate(config, text[len(what):]))
        elif what == "delegate-many":
            lines.extend(delegate_many(config, text[len(what):]))
        elif what == "status":
            lines.append(subtask_status(config, words[1] if len(words) > 1 else ""))
    return lines


# ------------------------------------------------------------ usage and limits (Claude Code's shapes)
def home() -> Path:
    return Path(os.environ.get("BULLPEN_HOME") or Path.home() / ".bullpen")


def session_file(session: str) -> Path | None:
    return home() / "selftest" / "sessions" / f"{session}.json" if SESSION_RE.match(session) else None


def load_totals(session: str) -> dict:
    """The session's totals per model, as saved when it last exited normally."""
    path = session_file(session)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {m: {k: v.get(k, 0) for k in USAGE_KEYS} for m, v in data.items() if isinstance(v, dict)}


def save_totals(session: str, totals: dict) -> None:
    """Saved only when a run ends normally, as Claude Code saves a session's totals."""
    path = session_file(session)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(totals), encoding="utf-8")
    except OSError:
        pass


def add_turn(totals: dict, model: str) -> None:
    mine = totals.setdefault(model, {k: 0 for k in USAGE_KEYS})
    for key in USAGE_KEYS:
        mine[key] = round(mine.get(key, 0) + TURN_USAGE[key], 6)


def model_usage(per_model: dict) -> dict:
    """Claude Code's modelUsage entries (the fields the runner reads, and two it doesn't)."""
    return {m: {**v, "webSearchRequests": 0, "contextWindow": 200000} for m, v in per_model.items()}


def result_event(answer: str, session: str, totals: dict, model: str, turn: bool = True, **extra) -> dict:
    """A result: usage is this turn only; modelUsage and total_cost_usd cover the conversation
    (resumed totals included); model_usage_since_last_result is this turn (left out with
    SELFTEST_NO_SINCE=1)."""
    this = dict(TURN_USAGE) if turn else {k: 0 for k in USAGE_KEYS}
    ev = {"type": "result", "subtype": "success", "is_error": False, "result": answer, "session_id": session,
          "num_turns": 2 if turn else 0, "duration_ms": 1000 if turn else 5,
          "usage": {"input_tokens": this["inputTokens"], "output_tokens": this["outputTokens"],
                    "cache_read_input_tokens": this["cacheReadInputTokens"],
                    "cache_creation_input_tokens": this["cacheCreationInputTokens"]},
          "modelUsage": model_usage(totals),
          "total_cost_usd": round(sum(v.get("costUSD", 0) for v in totals.values()), 6)}
    if os.environ.get("SELFTEST_NO_SINCE") != "1":
        ev["model_usage_since_last_result"] = model_usage({model: this}) if turn else {}
    ev.update(extra)
    return ev


def utilization() -> float:
    try:
        return float(os.environ.get("SELFTEST_UTIL", "0.23"))
    except ValueError:
        return 0.23


def limit_event(session: str) -> dict:
    """The usage-limit event Claude Code sends with a turn."""
    now, util = int(time.time()), utilization()
    info = {"status": "allowed", "resetsAt": now + 3600, "rateLimitType": "five_hour", "utilization": util,
            "isUsingOverage": False,
            "unifiedWindows": {"five_hour": {"utilization": util, "resetsAt": now + 3600, "status": "allowed"},
                               "seven_day": {"utilization": 0.41, "resetsAt": now + 5 * 86400, "status": "allowed"}}}
    return {"type": "rate_limit_event", "rate_limit_info": info, "uuid": str(uuid.uuid4()), "session_id": session}


def next_utc_hour(hour: int) -> int:
    now = datetime.datetime.now(datetime.timezone.utc)
    when = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if when <= now:
        when += datetime.timedelta(days=1)
    return int(when.timestamp())


def reset_words(ts: float) -> str:
    """'Nov 6, 11pm (UTC)', with ', 2027' when the year isn't this one, as /usage prints it."""
    when = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)
    year = f", {when.year}" if when.year != datetime.datetime.now(datetime.timezone.utc).year else ""
    clock = f"{when.hour % 12 or 12}" + (f":{when.minute:02d}" if when.minute else "") + \
        ("am" if when.hour < 12 else "pm")
    return f"{when.strftime('%b')} {when.day}{year}, {clock} (UTC)"


def iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def usage_command(session: str) -> int:
    """'/usage' in print mode: answered locally (no turn, no cost), as one assistant message from
    the model "<synthetic>" with the text and a structured usage_report."""
    now = time.time()
    session_reset = round((now + 2 * 3600 + 5 * 60) / 3600) * 3600  # 2 h 5 min from now, to the hour
    week_reset = round((now + 4 * 86400) / 3600) * 3600
    what = os.environ.get("SELFTEST_USAGE", "")
    if what == "none":
        text, report = "Usage data is not available right now.", {"rate_limits": None}
    else:
        text = "\n".join([
            "You are currently using your subscription to power your Claude Code usage",
            f"Current session: 31% used · resets {reset_words(session_reset)}",
            f"Current week (all models): 47% used · resets {reset_words(week_reset)}",
            f"Current week (Fable): 12% used · resets {reset_words(week_reset)}"])
        report = {"rate_limits": {"limits": [
            {"kind": "session", "group": "session", "percent": 31, "resets_at": iso(session_reset),
             "severity": "normal", "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 47, "resets_at": iso(week_reset),
             "severity": "normal", "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 12, "resets_at": iso(week_reset),
             "severity": "normal", "is_active": False, "scope": {"model": {"display_name": "Fable"}}}]}}
    message = {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": text}]}
    if what != "text":
        message["usage_report"] = report
    emit({"type": "assistant", "session_id": session, "parent_tool_use_id": None, "message": message})
    emit(result_event(text, session, {}, "<synthetic>", turn=False))
    return 0


def limit_rejected(session: str, totals: dict, model: str) -> int:
    """Claude Code at a usage limit: a 'rejected' limit event, an assistant message marked
    rate_limit, and a failed result with the 429 status."""
    reset = next_utc_hour(15)
    info = {"status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour", "utilization": 1.0,
            "isUsingOverage": False, "overageStatus": "rejected", "overageDisabledReason": "org_level_disabled",
            "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": reset, "status": "rejected"}}}
    emit({"type": "rate_limit_event", "rate_limit_info": info, "uuid": str(uuid.uuid4()), "session_id": session})
    emit({"type": "assistant", "session_id": session, "parent_tool_use_id": None, "error": "rate_limit",
          "message": {"model": "<synthetic>", "role": "assistant", "content": [{"type": "text", "text": LIMIT_TEXT}]}})
    emit(result_event(LIMIT_TEXT, session, totals, model, turn=False, is_error=True, api_error_status=429))
    return 1


def uses_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def plan_limited(prompt: str) -> bool:
    """SELFTEST: limit-rejected, for an agent signed in with a subscription (an API key has no plan limit)."""
    return has_directive(prompt, "limit-rejected") and not uses_api_key()


def credits_low(prompt: str) -> bool:
    """SELFTEST: credits-low, for an agent that signs in with an API key."""
    return has_directive(prompt, "credits-low") and uses_api_key()


def credit_refused(session: str, totals: dict, model: str) -> int:
    """Claude Code with an API key whose organization has no credit left: the API's 400 on stderr, an
    assistant message marked as an error, and a failed result that quotes the API."""
    said = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"'
            + CREDIT_TEXT + '"}}')
    print(said, file=sys.stderr, flush=True)
    emit({"type": "assistant", "session_id": session, "parent_tool_use_id": None, "error": "billing_error",
          "message": {"model": "<synthetic>", "role": "assistant",
                      "content": [{"type": "text", "text": "Credit balance is too low"}]}})
    emit(result_event(said, session, totals, model, turn=False, is_error=True, api_error_status=400))
    return 1


def answer_for(prompt: str, edit_note: str, directive_lines: list) -> str:
    first = next((ln.strip() for ln in prompt.splitlines() if ln.strip()), "(empty prompt)")
    if "pong" in prompt.lower():
        answer = "pong"
    else:
        answer = (f"This is a self-test answer to a prompt of {len(prompt)} characters that starts with:\n"
                  f"> {first[:200]}\n\n{edit_note}\n\nSummary: the plumbing works.")
    if directive_lines:
        answer = "\n".join(directive_lines) + "\n\n" + answer
    return answer


def delay() -> float:
    try:
        return float(os.environ.get("SELFTEST_DELAY", "1"))
    except ValueError:
        return 1.0


# ------------------------------------------------------------ the two formats
def gemini_stats(model: str, used: bool = True) -> dict:
    """Gemini CLI's stats: input_tokens includes the cached ones; "input" is the rest."""
    if used:
        figures = {"total_tokens": 600, "input_tokens": 500, "output_tokens": 100, "cached": 50, "input": 450}
    else:
        figures = {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached": 0, "input": 0}
    return {**figures, "duration_ms": 1000 if used else 0, "tool_calls": 0,
            "models": {model: dict(figures)} if used else {}}


def gemini_defaults(path) -> str | None:
    """The system-defaults file Gemini CLI 0.60 and later would read: like its own check, only one
    that root owns, with every folder above it, none writable by group or others."""
    if not path or not os.path.exists(path):
        return None
    if os.environ.get("SELFTEST_GEMINI_DEFAULTS") == "trusted":
        return path
    why = "only an administrator's file is read on Windows" if os.name == "nt" else ""
    item = os.path.abspath(path)
    while not why:
        st = os.stat(item)
        if st.st_uid != 0:
            why = f"'{item}' is not owned by root (uid 0)"
        elif st.st_mode & 0o022:
            why = f"'{item}' is writable by group or others"
        parent = os.path.dirname(item)
        if parent == item:
            break
        item = parent
    if not why:
        return path
    print(f"Security Warning: Skipping system defaults file '{path}': {why}", file=sys.stderr, flush=True)
    return None


def gemini_main(args: list, prompt: str) -> int:
    """Gemini CLI's stream-json (gemini --output-format stream-json)."""
    session = flag(args, "--resume") or str(uuid.uuid4())
    model = flag(args, "--model") or "gemini-fake"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    emit({"type": "init", "timestamp": stamp, "session_id": session, "model": model})
    time.sleep(delay())
    if has_directive(prompt, "gemini-429"):
        emit({"type": "result", "timestamp": stamp, "status": "error",
              "error": {"type": "TerminalQuotaError", "message": GEMINI_QUOTA_MESSAGE},
              "stats": gemini_stats(model, used=False)})
        return 1
    # Gemini CLI gets the job tools through its system-defaults settings layer (see the runner).
    answer = answer_for(prompt, "I did not change any files.",
                        run_directives(prompt, gemini_defaults(os.environ.get(GEMINI_DEFAULTS_VAR))))
    emit({"type": "message", "timestamp": stamp, "role": "assistant", "content": answer, "delta": False})
    emit({"type": "result", "timestamp": stamp, "status": "success", "stats": gemini_stats(model)})
    return 0


def first_line(text: str) -> str:
    return next((ln.strip() for ln in text.splitlines() if ln.strip()), "(empty prompt)")[:200]


def take_turn(prompt: str, ctx: dict, reply_no=None) -> int:
    """One turn of the Claude format: the assistant's first message, the usage-limit event, the
    directives, and a result. `reply_no` (live sessions) answers "Self-test reply <n>: <first
    line>". 1 when the turn failed on purpose (SELFTEST_FAIL), else 0."""
    session, model, totals = ctx["session"], ctx["model"], ctx["totals"]
    if plan_limited(prompt):
        limit_rejected(session, totals, model)
        return 0
    if credits_low(prompt):
        credit_refused(session, totals, model)
        return 0
    emit({"type": "assistant", "session_id": session, "parent_tool_use_id": None, "message": {"content": [
        {"type": "text", "text": f"Self-test agent here. Permission mode: {ctx['mode']}. Model: {model}."},
        {"type": "tool_use", "name": "Read", "input": {"file_path": os.getcwd()}},
    ]}})
    if os.environ.get("SELFTEST_LIMITS", "1") != "0":
        emit(limit_event(session))  # Claude Code sends it after the first assistant message
    time.sleep(delay())

    if os.environ.get("SELFTEST_FAIL") == "1":
        zero = {model: {k: 0 for k in USAGE_KEYS}}  # a crash's last result may carry all-zero figures
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True,
              "result": "Self-test failure requested.", "session_id": session, "num_turns": 1,
              "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0},
              "modelUsage": model_usage(zero), "total_cost_usd": 0,
              "model_usage_since_last_result": model_usage(zero)})
        return 1

    directive_lines = run_directives(prompt, ctx["mcp_config"], ctx["prompt_tool"])

    if ctx["mode"] in ("acceptEdits", "auto", "bypassPermissions"):
        with open("selftest-output.txt", "a", encoding="utf-8") as fh:
            fh.write(f"Written by the self-test agent at {time.ctime()}\n")
        edit_note = "I appended a line to selftest-output.txt."
    else:
        edit_note = "I did not change any files."

    if reply_no is None:
        answer = answer_for(prompt, edit_note, directive_lines)
    else:
        answer = "\n".join(directive_lines + [""]) + "\n" if directive_lines else ""
        answer += f"Self-test reply {reply_no}: {first_line(prompt)}"
    add_turn(totals, model)
    emit(result_event(answer, session, totals, model))
    return 0


def message_text(ev: dict) -> str | None:
    """The text of a stream-json user message: its text blocks, or its content when it is text."""
    if not isinstance(ev, dict) or ev.get("type") != "user":
        return None
    content = (ev.get("message") or {}).get("content") if isinstance(ev.get("message"), dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text")
    return None


def claude_main(args: list, prompt) -> int:
    """Claude Code's print mode. `prompt` is the whole input, or None for a live session
    (--input-format stream-json), where every user message on the input is one turn."""
    resumed = flag(args, "--resume")
    session = resumed or str(uuid.uuid4())
    model = flag(args, "--model", "selftest-1")
    mcp_config = flag(args, "--mcp-config")
    ctx = {"session": session, "model": model, "mcp_config": mcp_config,
           "mode": flag(args, "--permission-mode", "default"),
           "prompt_tool": flag(args, "--permission-prompt-tool"),
           "totals": load_totals(session) if resumed else {}}

    init = {"type": "system", "subtype": "init", "session_id": session, "model": model,
            "claude_code_version": CLI_VERSION}
    if mcp_config:
        init["mcp_servers"] = [{"name": JOB_SERVER, "status": connect_status(mcp_config)}]
    if prompt is None:
        turns = 0
        for raw in iter(sys.stdin.buffer.readline, b""):
            try:
                text = message_text(json.loads(raw.decode("utf-8", "replace")))
            except ValueError:
                text = None
            if text is None:
                continue
            turns += 1
            emit(init)  # Claude Code sends one with every turn of a live session
            if take_turn(text, ctx, reply_no=turns):
                return 1
        save_totals(session, ctx["totals"])  # the input closed: a normal exit
        return 0
    emit(init)
    if prompt.strip() == "/usage":
        return usage_command(session)
    if plan_limited(prompt):
        return limit_rejected(session, ctx["totals"], model)
    if credits_low(prompt):
        return credit_refused(session, ctx["totals"], model)
    if take_turn(prompt, ctx):
        return 1
    save_totals(session, ctx["totals"])
    return 0


# ------------------------------------------------------------ remote-control
def rc_slug(name: str) -> str:
    """'build-box myapp (two)' -> 'build-box-myapp-two': the name in the pid file's name."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "rc"


def remote_control_main(args: list) -> int:
    """`claude remote-control --name N --spawn S`, pretended: print the address and wait until
    stopped (SIGTERM or SIGINT, like Claude Code; Windows ends it by force), or fail the way Claude
    Code does (SELFTEST_RC)."""
    what = os.environ.get("SELFTEST_RC", "")
    if what in RC_FAILURES:
        print(RC_FAILURES[what], flush=True)
        return 1
    name = flag(args, "--name") or "selftest"
    pid_file = home() / "selftest" / f"rc-{rc_slug(name)}.pid"

    def stop(signum, frame) -> None:
        try:
            pid_file.unlink()
        except OSError:
            pass
        sys.exit(0)

    for sig in ("SIGTERM", "SIGINT"):  # Claude Code's bridge handles these (and SIGHUP), not SIGBREAK
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), stop)
    try:
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    if what == "nourl":
        print("Starting Remote Control…", flush=True)
    else:
        print(f"Remote Control is ready · https://claude.ai/code/selftest-{uuid.uuid4().hex[:8]}", flush=True)
    while True:
        time.sleep(3600)


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "remote-control":
        return remote_control_main(args[1:])
    if args and args[0] == "--gemini":
        return gemini_main(args[1:], sys.stdin.read())
    if flag(args, "--input-format") == "stream-json":
        return claude_main(args, None)
    return claude_main(args, sys.stdin.read())


if __name__ == "__main__":
    sys.exit(main())
