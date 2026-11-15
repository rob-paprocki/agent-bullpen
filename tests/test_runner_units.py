#!/usr/bin/env python3
"""Unit tests for the runner (runner/runner.py and runner/selftest_agent.py).

Standard library only, like the runner itself, and they run on Python 3.9 and newer on Linux,
macOS and Windows. Nothing here talks to a real hub or an AI service: jobs run the fake
'selftest' agent through a stand-in for the hub, and the hub's job tools (/mcp/job) are faked by
a small local web server.

    python3 tests/test_runner_units.py
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import datetime
import http.server
import io
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
# Before the runner is imported: everything it keeps (job folders, logs) goes to a temporary folder.
os.environ["BULLPEN_HOME"] = tempfile.mkdtemp(prefix="bullpen-runner-units-")
sys.path.insert(0, str(ROOT / "runner"))

import runner  # noqa: E402
import selftest_agent  # noqa: E402

TOKEN = "bpj_" + "T0k3n-" * 7 + "x"  # the shape of a job token (bpj_ + 43 url-safe characters)


def no_git_url_check():
    """Without git installed (the Python 3.9 CI container), the check that reads git's URL settings before an
    account job can't run, and fails closed. A machine with a git account always has git, so it is a no-op then
    and the rest of the job is still tested; the check's own tests need git."""
    if runner.shutil.which("git"):
        return contextlib.nullcontext()
    return mock.patch.object(runner, "check_git_credential_urls", lambda *args, **kwargs: None)
POSIX = os.name != "nt"
# Real events from Claude Code 2.1.319 (a job, then a follow-up that resumed it).
FIXTURES = [json.loads(line) for line in
            (ROOT / "tests" / "fixtures" / "claude-code-events.jsonl").read_text(encoding="utf-8").splitlines() if line]
RUN1_LIMIT, RUN1_RESULT, RUN2_RESULT = FIXTURES
AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")


class LocalHub:
    """Stands in for the hub: keeps the log lines and the finish report."""

    def __init__(self) -> None:
        self.lines: list = []
        self.finished: dict = {}

    def call(self, path: str, payload: dict, timeout: float = 30):
        if path.endswith("/log"):
            self.lines.extend(payload["lines"])
        elif path.endswith("/finish"):
            self.finished = dict(payload)
        return 200, {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeJobTools:
    """A stand-in for the hub's /mcp/job: answers tools/list, job_info and approve for one token, as
    an event stream with a progress notification before the reply (as the real one can).
    `decision`: approve's answer (default: allow, with the input as it was asked); `lists_ok`: how
    many tools/list calls are answered before the token stops working (default: all)."""

    def __init__(self, token: str = TOKEN, job_id: int = 7, machine: str = "build-box", tools=("job_info",),
                 decision=None, lists_ok=None, answers=None):
        # answers: {tool name: function(arguments) -> its JSON answer} for the subtask tools.
        answers = answers or {}
        self.calls: list = []
        self.lists = 0
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                params = msg.get("params") or {}
                fake.calls.append({"auth": self.headers.get("Authorization"), "method": msg.get("method"),
                                   "tool": params.get("name"), "agent": self.headers.get("X-Bullpen-Agent"),
                                   "arguments": params.get("arguments"), "meta": params.get("_meta")})
                if msg.get("method") == "tools/list":
                    fake.lists += 1
                    if lists_ok is not None and fake.lists > lists_ok:
                        return self.answer(401, "application/json", b'{"error": "invalid_token"}')
                if self.headers.get("Authorization") != "Bearer " + token:
                    return self.answer(401, "application/json", b'{"error": "invalid_token"}')
                if msg.get("method") == "tools/list":
                    result = {"tools": [{"name": t, "inputSchema": {"type": "object"}} for t in tools]}
                elif msg.get("method") == "tools/call" and params.get("name") == "job_info":
                    info = {"ok": True, "job_id": job_id, "machine": machine, "tools": list(tools)}
                    result = {"content": [{"type": "text", "text": json.dumps(info)}]}
                elif msg.get("method") == "tools/call" and params.get("name") == "approve":
                    asked = params.get("arguments") or {}
                    answer = decision or {"behavior": "allow", "updatedInput": asked.get("input")}
                    result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
                elif msg.get("method") == "tools/call" and params.get("name") in answers:
                    answer = answers[params["name"]](params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": json.dumps(answer)}]}
                    if answer.get("ok") is False:
                        result["isError"] = True
                else:
                    result = {"isError": True, "content": [{"type": "text", "text": "unknown"}]}
                progress = {"jsonrpc": "2.0", "method": "notifications/progress",
                            "params": {"progressToken": 1, "progress": 1}}
                reply = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
                body = f"event: message\ndata: {json.dumps(progress)}\n\nevent: message\ndata: {json.dumps(reply)}\n\n"
                self.answer(200, "text/event-stream", body.encode())

            def answer(self, code: int, kind: str, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def make_cfg(workdir: str, agents: dict, hub_url: str = "http://127.0.0.1:9") -> dict:
    return {"hub_url": hub_url, "token": "bp_" + "m" * 43, "name": "build-box", "agents": agents,
            "projects": {"chat": workdir}, "max_parallel": 1}


def run_job(cfg: dict, data: dict, **stub) -> tuple:
    """Run one job the way `runner.py test` does: a stub with only cfg and hub (and what `stub`
    adds, e.g. limits). Returns the finish report, the log lines and every command line started."""
    hub = LocalHub()
    started: list = []
    real_popen = subprocess.Popen

    def popen(argv, *args, **kwargs):
        started.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    job = runner.Job(argparse.Namespace(cfg=cfg, hub=hub, **stub), {"options": {"timeout_min": 2}, **data})
    with mock.patch.object(runner.subprocess, "Popen", popen):
        job.run()
    return hub.finished, hub.lines, started


def read_json(path: Path, wait_s: float = 2.0):
    """Read a JSON file the runner may be replacing at that moment. Windows refuses to open a file
    while os.replace swaps it (PermissionError for a few milliseconds), so try again there."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if os.name != "nt" or time.monotonic() > deadline:
                raise
            time.sleep(0.01)


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="bullpen-unit-")
        self.selftest_env = {"SELFTEST_DELAY": "0"}


# ------------------------------------------------------------------ job tools on the command line
class AddAllowedTools(unittest.TestCase):
    def test_after_the_flag_when_the_level_has_it(self):
        level = ["--permission-mode", "dontAsk", "--allowedTools", "mcp__bullpen__recall", "mcp__bullpen__list_notes"]
        out = runner.add_allowed_tools(level, "--allowedTools", ["mcp__bullpenjob__job_info"])
        self.assertEqual(out, ["--permission-mode", "dontAsk", "--allowedTools", "mcp__bullpenjob__job_info",
                               "mcp__bullpen__recall", "mcp__bullpen__list_notes"])
        self.assertEqual(level[3], "mcp__bullpen__recall", "the level's own list is not changed")

    def test_with_the_flag_at_the_end_when_the_level_lacks_it(self):
        out = runner.add_allowed_tools(["--permission-mode", "auto"], "--allowedTools",
                                       ["mcp__bullpenjob__job_info", "mcp__bullpenjob__list_machines"])
        self.assertEqual(out, ["--permission-mode", "auto", "--allowedTools", "mcp__bullpenjob__job_info",
                               "mcp__bullpenjob__list_machines"])

    def test_approve_is_never_allowed(self):
        names = runner.allowed_job_tool_names(["job_info", "approve", "delegate_task"])
        self.assertEqual(names, ["mcp__bullpenjob__job_info", "mcp__bullpenjob__delegate_task"])
        out = runner.add_allowed_tools(["--allowedTools", "x"], "--allowedTools",
                                       ["mcp__bullpenjob__approve", "mcp__bullpenjob__job_info"])
        self.assertNotIn("mcp__bullpenjob__approve", out)
        self.assertEqual(runner.add_allowed_tools(["-a"], "--allowedTools", ["mcp__bullpenjob__approve"]), ["-a"])

    def test_after_the_flag_in_its_other_spelling(self):
        # allow_tools_flag "--allowed-tools" with a level that says "--allowedTools": one flag, not two.
        for level_flag, flag in (("--allowedTools", "--allowed-tools"), ("--allowed-tools", "--allowedTools")):
            with self.subTest(flag=flag):
                out = runner.add_allowed_tools(["--permission-mode", "dontAsk", level_flag, "Read"], flag,
                                               ["mcp__bullpenjob__job_info"])
                self.assertEqual(out, ["--permission-mode", "dontAsk", level_flag, "mcp__bullpenjob__job_info", "Read"])
        # Another flag isn't taken for it.
        self.assertEqual(runner.add_allowed_tools(["--disallowedTools", "Read"], "--allowed-tools",
                                                  ["mcp__bullpenjob__job_info"]),
                         ["--disallowedTools", "Read", "--allowed-tools", "mcp__bullpenjob__job_info"])

    def test_nothing_to_add(self):
        self.assertEqual(runner.add_allowed_tools(["-a"], "--allowedTools", []), ["-a"])
        self.assertEqual(runner.add_allowed_tools(["-a"], "", ["mcp__bullpenjob__job_info"]), ["-a"])


# ------------------------------------------------------------------ what an agent reports
class ClaudeAuth(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(runner.claude_auth({}), "login")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_API_KEY": "sk-ant-x"}), "api_key")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_AUTH_TOKEN": "gateway-key"}), "api_key")
        self.assertEqual(runner.claude_auth({"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x"}), "token")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_API_KEY": "k", "CLAUDE_CODE_OAUTH_TOKEN": "t"}), "api_key")
        self.assertEqual(runner.claude_auth({"CLAUDE_CODE_USE_BEDROCK": "1"}), "other")
        self.assertEqual(runner.claude_auth({"CLAUDE_CODE_USE_VERTEX": "1"}), "other")
        self.assertEqual(runner.claude_auth({"CLAUDE_CODE_USE_FOUNDRY": "1"}), "other")

    def test_base_url(self):
        self.assertEqual(runner.claude_auth({"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}), "login")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_BASE_URL": "https://api.anthropic.com/"}), "login")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_BASE_URL": "http://192.168.100.20:4000"}), "other")
        self.assertEqual(runner.claude_auth({"ANTHROPIC_BASE_URL": "https://api.anthropic.com.example.org"}),
                         "other")


class AgentFeatures(unittest.TestCase):
    def spec(self, **entry) -> dict:
        return runner.agent_spec({"agents": {"a": entry}}, "a")

    def test_presets(self):
        login = ["job_mcp", "approvals", "mcp_pick", "live", "levels", "summary", "limits", "usage_check",
                 "remote_control"]
        self.assertEqual(runner.agent_features(self.spec(preset="claude"), {}), login)
        self.assertEqual(runner.agent_features(self.spec(preset="selftest"), {}), login)
        with mock.patch.object(runner, "gemini_job_tools_problem", return_value=None):
            self.assertEqual(runner.agent_features(self.spec(preset="gemini"), {}), ["job_mcp", "levels", "summary"],
                             "where Gemini CLI would read the job's settings file, Gemini takes the subtask tools "
                             "(but can't ask the owner)")
            self.assertEqual(runner.agent_features(self.spec(preset="gemini", job_mcp=None), {}), ["levels", "summary"])
        with mock.patch.object(runner, "gemini_job_tools_problem", return_value="the runner doesn't run as root"):
            self.assertEqual(runner.agent_features(self.spec(preset="gemini"), {}), ["levels", "summary"],
                             "elsewhere it takes subtasks but can't hand them out")
        self.assertEqual(runner.agent_features(self.spec(preset="text", command="mycli"), {}), [])
        self.assertEqual(runner.agent_features(self.spec(command="mycli"), {}), [], "no preset is a text agent")

    def test_limits_by_sign_in(self):
        spec = self.spec(preset="claude")
        both = ["job_mcp", "approvals", "mcp_pick", "live", "levels", "summary"]  # whatever the sign-in
        self.assertEqual(runner.agent_features(spec, {}), both + ["limits", "usage_check", "remote_control"],
                         "a full login reports limits, can check them and can run Remote Control")
        self.assertEqual(runner.agent_features(spec, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}), both + ["limits"],
                         "a setup-token token reports limits but can't read /usage or run Remote Control")
        self.assertEqual(runner.agent_features(spec, {"ANTHROPIC_API_KEY": "k"}), both,
                         "an API key has no plan limits")
        self.assertEqual(runner.agent_features(spec, {"CLAUDE_CODE_USE_BEDROCK": "1"}), both)
        self.assertEqual(runner.agent_features(spec, {"ANTHROPIC_BASE_URL": "http://192.168.100.20:4000"}), both,
                         "a gateway isn't Anthropic's own service")
        self.assertEqual(runner.agent_features(self.spec(preset="claude", usage_check=None), {}),
                         both + ["limits", "remote_control"], "no usage_check settings, no Check limits")
        self.assertEqual(runner.agent_features(self.spec(preset="claude", remote_control=None), {}),
                         both + ["limits", "usage_check"], "no remote_control settings, no Remote Control")

    def test_own_levels_lose_levels(self):
        spec = self.spec(preset="selftest", autonomy={"readonly": ["--permission-mode", "dontAsk"]})
        self.assertTrue(spec["autonomy_overridden"])
        self.assertEqual(runner.agent_features(spec, {}),
                         ["job_mcp", "approvals", "mcp_pick", "live", "summary", "limits", "usage_check",
                          "remote_control"])
        spec = self.spec(preset="claude", autonomy_env={"full": {"X": "1"}})
        self.assertEqual(runner.agent_features(spec, {}),
                         ["job_mcp", "approvals", "mcp_pick", "live", "summary", "limits", "usage_check",
                          "remote_control"])
        self.assertFalse(self.spec(preset="claude")["autonomy_overridden"])
        spec = self.spec(preset="claude", autonomy={"ask": None})
        self.assertNotIn("approvals", runner.agent_features(spec, {}), "no 'ask' level, no approvals")

    def test_job_tools_can_be_turned_off(self):
        self.assertEqual(runner.agent_features(self.spec(preset="claude", job_mcp=None), {}),
                         ["mcp_pick", "live", "levels", "summary", "limits", "usage_check", "remote_control"],
                         "without the job tools nothing can ask the owner (a task's picked servers still reach it)")

    def test_live_needs_the_prompt_on_stdin(self):
        self.assertNotIn("live", runner.agent_features(self.spec(preset="claude", prompt_via="arg"), {}))
        self.assertNotIn("live", runner.agent_features(self.spec(preset="claude", live=None), {}))
        self.assertNotIn("live", runner.agent_features(self.spec(preset="gemini"), {}), "Gemini answers turn by turn")

    def test_agent_status_fields(self):
        agents = {"one": {"preset": "selftest", "account": "work"},
                  "two": {"preset": "selftest", "account": "-bad label"},
                  "three": {"preset": "selftest"},
                  "tok": {"preset": "selftest", "env": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-token-0123"}},
                  "gem": {"preset": "gemini", "command": sys.executable},
                  "txt": {"preset": "text", "command": sys.executable},
                  "gone": {"preset": "claude", "command": "no-such-program-here"}}
        cfg = {"agents": agents}
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL",
                        "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
                os.environ.pop(key, None)
            st = {name: runner.agent_status(cfg, name) for name in agents}
        self.assertEqual(st["one"]["account"], "work")
        self.assertNotIn("account", st["two"], "a label that isn't valid is not sent")
        self.assertNotIn("account", st["three"], "no label unless runner.json sets one")
        self.assertEqual((st["one"]["auth"], st["tok"]["auth"], st["gem"]["auth"], st["txt"]["auth"]),
                         ("login", "token", "api_key", ""))
        self.assertEqual(st["one"]["features"], ["job_mcp", "approvals", "mcp_pick", "live", "levels", "summary",
                                                 "limits", "usage_check", "remote_control"])
        self.assertEqual(st["tok"]["features"], ["job_mcp", "approvals", "mcp_pick", "live", "levels", "summary",
                                                 "limits"])
        self.assertEqual(st["one"]["terminal"], "selftest --resume {session_id}")
        self.assertFalse(st["gone"]["ready"])
        self.assertEqual(st["gone"]["features"], ["job_mcp", "approvals", "mcp_pick", "live", "levels", "summary",
                                                  "limits", "usage_check", "remote_control"],
                         "reported even when not installed")


class Terminal(unittest.TestCase):
    def spec(self, preset: str, name: str = "a", **entry) -> dict:
        return runner.agent_spec({"agents": {name: {"preset": preset, **entry}}}, name)

    def test_claude(self):
        spec = self.spec("claude", "claude")
        self.assertEqual(runner.agent_terminal(spec, {}), "claude --resume {session_id}")
        self.assertEqual(runner.agent_terminal(spec, {"ANTHROPIC_API_KEY": "k"}), "")
        self.assertEqual(runner.agent_terminal(spec, {"CLAUDE_CODE_OAUTH_TOKEN": "t"}), "")
        self.assertEqual(runner.agent_terminal(spec, {"CLAUDE_CODE_USE_BEDROCK": "1"}), "")

    def test_claude_with_its_own_folder(self):
        spec = self.spec("claude", "claude-work")
        env = {"CLAUDE_CONFIG_DIR": "/home/me/it's mine/.claude-work"}
        with mock.patch.object(runner.shutil, "which", lambda name: "/home/me/.local/bin/" + name):
            self.assertEqual(runner.agent_terminal(spec, env), "claude-work --resume {session_id}",
                             "the command the setup script made for this login")
        with mock.patch.object(runner.shutil, "which", lambda name: None):
            with mock.patch.object(runner, "IS_WINDOWS", False):
                self.assertEqual(runner.agent_terminal(spec, env),
                                 "CLAUDE_CONFIG_DIR='/home/me/it'\"'\"'s mine/.claude-work' claude --resume {session_id}")
            with mock.patch.object(runner, "IS_WINDOWS", True):
                self.assertEqual(runner.agent_terminal(spec, {"CLAUDE_CONFIG_DIR": "C:\\Users\\me\\it's\\.claude-work"}),
                                 "$env:CLAUDE_CONFIG_DIR='C:\\Users\\me\\it''s\\.claude-work'; claude --resume {session_id}")

    def test_wrapper_only_for_a_plain_name(self):
        spec = self.spec("claude", "work; rm -rf x")
        with mock.patch.object(runner.shutil, "which", lambda name: "/bin/" + name), \
                mock.patch.object(runner, "IS_WINDOWS", False):
            self.assertEqual(runner.agent_terminal(spec, {"CLAUDE_CONFIG_DIR": "/d"}),
                             "CLAUDE_CONFIG_DIR=/d claude --resume {session_id}")

    def test_other_presets(self):
        self.assertEqual(runner.agent_terminal(self.spec("gemini"), {}), "gemini --resume {session_id}")
        self.assertEqual(runner.agent_terminal(self.spec("gemini", env={"GEMINI_API_KEY": "own-key-123"}), {}), "")
        self.assertEqual(runner.agent_terminal(self.spec("selftest"), {}), "selftest --resume {session_id}")
        self.assertEqual(runner.agent_terminal(self.spec("text", command="mycli"), {}), "")

    def test_runner_json_wins(self):
        self.assertEqual(runner.agent_terminal(self.spec("text", terminal="mycli resume {session_id}"), {}),
                         "mycli resume {session_id}")
        self.assertEqual(runner.agent_terminal(self.spec("claude", terminal=""), {}), "")
        self.assertEqual(runner.agent_terminal(self.spec("selftest", terminal=None), {}), "")


class Tags(unittest.TestCase):
    def test_clean_tags(self):
        self.assertEqual(runner.clean_tags(["GPU", "gpu", "bad tag", "pool", 5]), (["gpu", "pool"], ["bad tag", "5"]))
        self.assertEqual(runner.clean_tags("Kali, security-tools"), (["kali", "security-tools"], []))
        self.assertEqual(runner.clean_tags(" a  b,,c "), (["a", "b", "c"], []))
        self.assertEqual(runner.clean_tags(None), ([], []))
        self.assertEqual(runner.clean_tags({"gpu": True}), ([], []))
        self.assertEqual(runner.clean_tags(["-x", "x" * 33, "ok.1_2-3"]), (["ok.1_2-3"], ["-x", "x" * 33]))
        tags, skipped = runner.clean_tags([f"t{i}" for i in range(20)])
        self.assertEqual(len(tags), 15, "the operating-system tag is the 16th")
        self.assertEqual(skipped, ["t15", "t16", "t17", "t18", "t19"])

    def test_heartbeat_tags(self):
        with mock.patch.object(runner, "PLATFORM", "linux"):
            self.assertEqual(runner.heartbeat_tags({}), ["linux"])
            self.assertEqual(runner.heartbeat_tags({"tags": ["pool", "windows", "GPU", "linux"]}),
                             ["gpu", "linux", "pool"], "another platform's name is not this machine's tag")
            many = runner.heartbeat_tags({"tags": [f"t{i:02}" for i in range(20)]})
            self.assertEqual(len(many), 16)
            self.assertIn("linux", many)


# ------------------------------------------------------------------ running jobs
class Levels(TempDirCase):
    def test_a_missing_level_fails_the_job(self):
        cfg = make_cfg(self.tmp, {"levelsoff": {"preset": "selftest", "env": self.selftest_env,
                                                "autonomy": {"readonly": ["--permission-mode", "dontAsk"],
                                                             "edits": None}}})
        finished, _, started = run_job(cfg, {"id": 1, "agent": "levelsoff", "project": "chat", "autonomy": "edits",
                                             "prompt": "hello"})
        self.assertEqual(finished["status"], "error")
        self.assertIn("levelsoff has no 'edits' level", finished["error"])
        self.assertEqual(started, [], "nothing was started")

    def test_a_level_the_preset_lacks_fails_the_job(self):
        cfg = make_cfg(self.tmp, {"txt": {"preset": "text", "command": sys.executable}})
        finished, _, started = run_job(cfg, {"id": 2, "agent": "txt", "project": "chat", "autonomy": "ask",
                                             "prompt": "hello"})
        self.assertEqual(finished["status"], "error")
        self.assertIn("txt has no 'ask' level", finished["error"])
        self.assertEqual(started, [])

    def test_ask_without_job_tools_fails_the_job(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}})
        finished, _, started = run_job(cfg, {"id": 2, "agent": "one", "project": "chat", "autonomy": "ask",
                                             "prompt": "hello"})
        self.assertEqual(finished["status"], "error")
        self.assertIn("the hub sent no job tools", finished["error"])
        self.assertEqual(started, [], "never with fewer questions than asked for")

    def test_own_levels_keep_the_presets_other_levels(self):
        spec = runner.agent_spec({"agents": {"a": {"preset": "claude", "autonomy": {"readonly": ["--x"]}}}}, "a")
        self.assertEqual(spec["autonomy"]["readonly"], ["--x"])
        self.assertEqual(spec["autonomy"]["edits"], runner.PRESETS["claude"]["autonomy"]["edits"])


class MinimalStub(TempDirCase):
    def test_a_job_runs_with_a_stub_that_has_only_cfg_and_hub(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}})
        finished, lines, started = run_job(cfg, {"id": 3, "agent": "one", "project": "chat", "autonomy": "readonly",
                                                 "model": "fake-small", "prompt": "Reply with exactly one word: pong"})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "pong")
        self.assertTrue(any(ln.startswith("▶ one in chat") for ln in lines))
        # An ordinary job's command line is what it always was.
        self.assertEqual(started, [[sys.executable, str(ROOT / "runner" / "selftest_agent.py"),
                                    "--permission-mode", "dontAsk", "--model", "fake-small"]])


class JobTools(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = FakeJobTools(tools=("job_info", "list_machines"))
        self.addCleanup(self.fake.close)

    def test_prepare_writes_a_private_config(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest"}}, hub_url="http://192.168.100.20:8080/")
        cfg.update(cf_access_client_id="cid.access", cf_access_client_secret="csecret-123")
        spec = runner.agent_spec(cfg, "one")
        job = {"id": 41, "agent": "one", "job_token": TOKEN, "job_tools": ["job_info", "approve"],
               "approval_timeout_s": 600}
        args, env, folder = runner.prepare_job_tools(cfg, spec, job, "build-box")
        self.addCleanup(lambda: folder and runner.shutil.rmtree(folder, ignore_errors=True))
        config = runner.JOBS_DIR / "41" / "mcp.json"
        self.assertEqual((args, env, folder), (["--mcp-config", str(config)], {}, runner.JOBS_DIR / "41"))
        entry = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["bullpenjob"]
        self.assertEqual(entry["type"], "http")
        self.assertEqual(entry["url"], "http://192.168.100.20:8080/mcp/job")
        self.assertEqual(entry["headers"], {"Authorization": "Bearer " + TOKEN, "X-Bullpen-Agent": "one@build-box job 41",
                                            "CF-Access-Client-Id": "cid.access",
                                            "CF-Access-Client-Secret": "csecret-123"})
        self.assertEqual(entry["timeout"], (600 + 60) * 1000)
        self.assertIs(entry["alwaysLoad"], True)
        if POSIX:
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        _, _, folder2 = runner.prepare_job_tools(cfg, spec, {**job, "id": 42, "job_tools": ["job_info"]}, "build-box")
        self.addCleanup(lambda: runner.shutil.rmtree(folder2, ignore_errors=True))
        entry = json.loads((folder2 / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["bullpenjob"]
        self.assertEqual(entry["timeout"], 120000)

    def test_prepare_does_nothing_without_a_token_or_support(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest"}, "gem": {"preset": "gemini", "job_mcp": None},
                                  "txt": {"preset": "text", "command": "mycli"}})
        job = {"id": 43, "job_token": TOKEN, "job_tools": ["job_info", "approve"]}
        for name in ("gem", "txt"):
            self.assertEqual(runner.prepare_job_tools(cfg, runner.agent_spec(cfg, name), job, "m"), ([], {}, None))
        spec = runner.agent_spec(cfg, "one")
        self.assertEqual(runner.prepare_job_tools(cfg, spec, {**job, "job_token": ""}, "m"), ([], {}, None))
        self.assertEqual(runner.prepare_job_tools(cfg, spec, {**job, "job_tools": ["../x"]}, "m"), ([], {}, None))
        self.assertFalse((runner.JOBS_DIR / "43").exists())

    def test_check_job_tools(self):
        cfg = make_cfg(self.tmp, {}, hub_url=self.fake.url)
        self.assertIsNone(runner.check_job_tools(cfg, TOKEN))
        self.assertEqual(runner.check_job_tools(cfg, "bpj_wrong-token-0123456789"), "the hub answered 401")
        closed = make_cfg(self.tmp, {}, hub_url=f"http://127.0.0.1:{free_port()}")
        self.assertIsInstance(runner.check_job_tools(closed, TOKEN, timeout=5), str)

    def test_a_job_with_a_token_gets_the_tools_and_never_shows_the_token(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}}, hub_url=self.fake.url)
        prompt = "SELFTEST: job-info\nSELFTEST: leak-token\nPlease check the plumbing."
        finished, lines, started = run_job(cfg, {
            "id": 7, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": prompt,
            "job_token": TOKEN, "job_tools": ["job_info", "list_machines"]})
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue(finished["result"].startswith("job-info: #7 on build-box tools=job_info,list_machines\nleaked\n"),
                        finished["result"])
        self.assertIn("stderr: token=***", lines)
        self.assertNotIn(TOKEN, json.dumps([lines, finished]))
        argv = started[0]
        config = str(runner.JOBS_DIR / "7" / "mcp.json")
        self.assertEqual(argv[2:9], ["--permission-mode", "dontAsk", "--allowedTools", "mcp__bullpenjob__job_info",
                                     "mcp__bullpenjob__list_machines", "--mcp-config", config])
        self.assertNotIn(TOKEN, " ".join(argv), "the token is never on a command line")
        self.assertFalse((runner.JOBS_DIR / "7").exists(), "the job's folder is removed afterwards")
        # The runner's check before the start, the agent's connection, and its job_info call.
        seen = [(c["method"], c["tool"]) for c in self.fake.calls]
        self.assertEqual(seen, [("tools/list", None), ("tools/list", None), ("tools/call", "job_info")])
        self.assertTrue(all(c["auth"] == "Bearer " + TOKEN for c in self.fake.calls))
        self.assertEqual(self.fake.calls[-1]["agent"], "one@build-box job 7")

    def test_subtask_tools_that_cant_be_reached_are_left_out(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}},
                       hub_url=f"http://127.0.0.1:{free_port()}")
        finished, lines, started = run_job(cfg, {
            "id": 8, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "SELFTEST: job-info",
            "job_token": TOKEN, "job_tools": ["job_info", "list_machines", "delegate_task"]})
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue(any(ln.startswith("⚠ Subtask tools unavailable: ") for ln in lines), lines)
        self.assertNotIn("--mcp-config", started[0])
        self.assertNotIn("--allowedTools", started[0])
        self.assertTrue(finished["result"].startswith("job-info: not available (no job tools)"))
        self.assertFalse((runner.JOBS_DIR / "8").exists())

    def test_job_info_only_needs_no_check(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}}, hub_url=self.fake.url)
        finished, _, started = run_job(cfg, {
            "id": 9, "agent": "one", "project": "chat", "autonomy": "edits", "prompt": "hi",
            "job_token": TOKEN, "job_tools": ["job_info"]})
        self.assertEqual(finished["status"], "done", finished)
        self.assertIn("--mcp-config", started[0])
        self.assertEqual([c["method"] for c in self.fake.calls], ["tools/list"], "only the agent's own connection")

    def test_token_masking_in_lines_and_the_answer(self):
        self.assertEqual(runner.mask_job_token(f"a {TOKEN} b", TOKEN), "a *** b")
        self.assertEqual(runner.mask_job_token("x bpj_" + "a1B2-c_" * 6 + "z y", ""), "x *** y")
        self.assertEqual(runner.mask_job_token("bpj_short", ""), "bpj_short")
        self.assertEqual(runner.mask_job_token("bpj_" + "a" * 44, ""), "bpj_" + "a" * 44)  # not a job token's shape
        self.assertEqual(runner.redact("Bearer " + "bpj_" + "z" * 20 + "-"), "Bearer ***")
        self.assertEqual(runner.redact("key bpj_" + "z" * 43), "key ***")
        script = ("import sys; print('found ' + 'bpj_' + 'Q' * 43); "
                  "print('mine ' + sys.argv[1], file=sys.stderr)")
        cfg = make_cfg(self.tmp, {"echo": {"preset": "text", "command": sys.executable,
                                           "args": ["-c", script, TOKEN], "prompt_via": "stdin"}})
        finished, lines, _ = run_job(cfg, {"id": 10, "agent": "echo", "project": "chat", "autonomy": "readonly",
                                           "prompt": "hi", "job_token": TOKEN, "job_tools": ["job_info"]})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "found ***")
        self.assertIn("found ***", lines)
        self.assertIn("stderr: mine ***", lines)


class BullpenTokenShapes(unittest.TestCase):
    # Machine tokens are "bp_" and job tokens "bpj_", each followed by secrets.token_urlsafe(32):
    # always 43 URL-safe characters. Only that shape is masked, so ordinary names that happen to
    # start with bp_ (Flask blueprints, for example) stay readable, also on approval cards.
    def test_real_tokens_are_masked(self):
        for token in ("bp_" + "Ab3-_x9" * 6 + "Q", "bpj_" + "Zz0_-k" * 7 + "1"):
            self.assertEqual(len(token.split("_", 1)[1]), 43)
            self.assertEqual(runner.redact(f"echo {token} done"), "echo *** done", token)
            self.assertEqual(runner.redact(f"({token})"), "(***)", token)

    def test_names_that_only_start_like_a_token_are_kept(self):
        for text in ("app.register_blueprint(bp_admin_dashboard_routes)", "bp_user_settings_page",
                     "from views import bp_api_v2_endpoints_for_reports",
                     "bpj_" + "a" * 44, "bp_" + "a" * 42, "bp_" + "a" * 43 + "_routes", "x_bp_" + "a" * 43):
            self.assertEqual(runner.redact(text), text)


class GitLabTokensAndCloneUrls(unittest.TestCase):
    def test_gitlab_tokens_are_masked(self):
        self.assertEqual(runner.redact("token glpat-" + "a" * 20), "token ***")
        self.assertEqual(runner.redact("x gldt-" + "Ab_-9" * 5 + " y"), "x *** y")
        self.assertEqual(runner.redact("x glrtr-" + "a" * 20), "x ***")
        self.assertEqual(runner.redact("x glwt-" + "a" * 20), "x ***")

    def test_url_has_userinfo(self):
        import clone_url_cases
        for url in clone_url_cases.BAD + clone_url_cases.NO_HOST_USERINFO:
            self.assertTrue(runner.url_has_userinfo(url), url)
        for url in clone_url_cases.OK:
            self.assertFalse(runner.url_has_userinfo(url), url)

    def test_clone_with_userinfo_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {"projects_root": tmp, "projects": {"chat": tmp}}
            job = runner.Job(argparse.Namespace(cfg=cfg, hub=LocalHub()), {"id": 1, "kind": "admin"})
            import clone_url_cases
            with mock.patch.object(job, "_run_logged") as ran:
                outs = [job._project("project.clone", {"name": "myapp", "url": u}) for u in clone_url_cases.BAD]
                no_host = [job._project("project.clone", {"name": "myapp", "url": u}) for u in clone_url_cases.NO_HOST]
            ran.assert_not_called()
        self.assertEqual(outs, [{"status": "error",
                                 "error": "Put credentials in a git account on the machine, not in the URL."}]
                         * len(clone_url_cases.BAD))
        self.assertTrue(all(o["status"] == "error" for o in no_host), no_host)


class MaskingTime(unittest.TestCase):
    # Run in a child process, so a pattern that backtracks fails the test instead of hanging it.
    SCRIPT = r"""
import json, sys, time
sys.path.insert(0, sys.argv[1])
import runner
worst = [0.0, ""]
for piece, end, size in json.loads(sys.argv[2]):
    text = (piece * (size // len(piece) + 1))[:size - len(end)] + end
    start = time.perf_counter()
    runner.redact(text)
    took = time.perf_counter() - start
    if took > worst[0]:
        worst = [took, repr(piece) + " repeated to " + str(size) + " characters, then " + repr(end)]
print(json.dumps(worst))
"""

    def test_crafted_lines_are_masked_quickly(self):
        # Lines from agents and their tools are masked before they are logged. The old patterns
        # backtracked: "key-" repeated to 20,000 characters took hours, "a." about a second.
        pieces = ("a-", "key-", "k-ey-", "a.", "eyJ-", "x://", "key: ")
        cases = [[p, e, 20_000] for p in pieces for e in ("", "=ab", "://", ".abcdefghij.")]
        cases += [["eyJ-", "", 200_000], ["a.", "://", 200_000]]
        try:
            done = subprocess.run([sys.executable, "-c", self.SCRIPT, str(ROOT / "runner"), json.dumps(cases)],
                                  capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            self.fail("masking the crafted lines took over a minute")
        self.assertEqual(done.returncode, 0, done.stderr)
        took, case = json.loads(done.stdout)
        self.assertLess(took, 0.5, f"masking {case} took {took:.2f} s")


class JobFolders(unittest.TestCase):
    def test_leftovers_older_than_a_day_are_removed(self):
        old, new = runner.JOBS_DIR / "900", runner.JOBS_DIR / "901"
        for folder in (old, new):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "mcp.json").write_text("{}", encoding="utf-8")
        day_ago = time.time() - runner.JOB_DIR_MAX_AGE - 60
        os.utime(old, (day_ago, day_ago))
        runner.clean_job_dirs()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        runner.shutil.rmtree(new, ignore_errors=True)


# ------------------------------------------------------------------ the fake agent
class SelftestAgent(TempDirCase):
    def test_flag(self):
        args = ["--permission-mode", "dontAsk", "--allowedTools", "x", "y", "--mcp-config", "/tmp/m.json",
                "--model=fake-large", "--max-turns", "1"]
        self.assertEqual(selftest_agent.flag(args, "--mcp-config"), "/tmp/m.json")
        self.assertEqual(selftest_agent.flag(args, "--model"), "fake-large")
        self.assertEqual(selftest_agent.flag(args, "--resume", "none"), "none")
        self.assertIsNone(selftest_agent.flag(["--resume"], "--resume"))

    def run_agent(self, *args: str, prompt: str = "hello") -> tuple:
        env = dict(os.environ, SELFTEST_DELAY="0", PYTHONIOENCODING="utf-8")
        res = subprocess.run([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), *args], input=prompt,
                             capture_output=True, text=True, encoding="utf-8", cwd=self.tmp, env=env, timeout=60)
        events = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
        return res.returncode, events, res.stderr

    def write_config(self, url: str) -> str:
        path = Path(self.tmp) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"bullpenjob": {
            "type": "http", "url": url + "/mcp/job", "headers": {"Authorization": "Bearer " + TOKEN}}}}),
            encoding="utf-8")
        return str(path)

    def test_init_reports_the_job_tools(self):
        fake = FakeJobTools()
        self.addCleanup(fake.close)
        code, events, _ = self.run_agent("--mcp-config", self.write_config(fake.url))
        self.assertEqual(code, 0)
        self.assertEqual(events[0]["mcp_servers"], [{"name": "bullpenjob", "status": "connected"}])
        code, events, _ = self.run_agent("--mcp-config", self.write_config(f"http://127.0.0.1:{free_port()}"))
        self.assertEqual(events[0]["mcp_servers"], [{"name": "bullpenjob", "status": "failed"}])
        code, events, _ = self.run_agent()
        self.assertNotIn("mcp_servers", events[0], "no job tools, no entry")

    def test_directives(self):
        fake = FakeJobTools(job_id=12, tools=("job_info",))
        self.addCleanup(fake.close)
        prompt = "First line\nSELFTEST: job-info\n  SELFTEST: leak-token\nSELFTEST: not-yet-known\n"
        code, events, stderr = self.run_agent("--mcp-config", self.write_config(fake.url), prompt=prompt)
        result = events[-1]["result"]
        self.assertEqual(result.splitlines()[:3], ["job-info: #12 on build-box tools=job_info", "leaked", ""])
        self.assertIn("> First line", result)
        self.assertIn("token=" + TOKEN, stderr)
        code, events, stderr = self.run_agent(prompt="SELFTEST: job-info\nSELFTEST: leak-token")
        self.assertTrue(events[-1]["result"].startswith("job-info: not available (no job tools)\nleaked\n"))
        self.assertIn("token=(none)", stderr)
        bad = Path(self.tmp) / "bad.json"
        bad.write_text(json.dumps({"mcpServers": {"bullpenjob": {"type": "http", "url": fake.url + "/mcp/job",
                                                               "headers": {"Authorization": "Bearer nope"}}}}),
                       encoding="utf-8")
        code, events, _ = self.run_agent("--mcp-config", str(bad), prompt="SELFTEST: job-info")
        self.assertTrue(events[-1]["result"].startswith("job-info: not available (HTTP 401)"), events[-1]["result"])


# ------------------------------------------------------------------ plan limits (step 2)
def epoch(*parts, tz=datetime.timezone.utc) -> float:
    return datetime.datetime(*parts, tzinfo=tz).timestamp()


NOW = epoch(2026, 11, 6, 12, 0)  # a Friday, noon UTC


class LimitValues(unittest.TestCase):
    def test_limit_epoch(self):
        self.assertEqual(runner.limit_epoch(1794423600), 1794423600.0)
        self.assertEqual(runner.limit_epoch(1794423600000), 1794423600.0, "milliseconds")
        self.assertEqual(runner.limit_epoch("1794423600"), 1794423600.0)
        self.assertEqual(runner.limit_epoch(1794423600.5), 1794423600.5)
        at = epoch(2026, 11, 6, 23, 0)
        self.assertEqual(runner.limit_epoch("2026-11-06T23:00:00Z"), at)
        self.assertEqual(runner.limit_epoch("2026-11-06t23:00:00z"), at)
        self.assertEqual(runner.limit_epoch("2026-11-07T01:00:00+02:00"), at)
        self.assertEqual(runner.limit_epoch("2026-11-07T01:00:00+0200"), at)
        self.assertEqual(runner.limit_epoch("2026-11-06T23:00:00.000Z"), at, "three fraction digits (JavaScript)")
        self.assertAlmostEqual(runner.limit_epoch("2026-11-06T23:00:00.1234567Z"), at + 0.123456, places=6)
        self.assertAlmostEqual(runner.limit_epoch("2026-11-06T23:00:00.5+00:00"), at + 0.5, places=6)
        self.assertEqual(runner.limit_epoch("2026-11-06T23:00:00"), at, "no zone is read as UTC")
        self.assertEqual(runner.limit_epoch("2026-11-06 23:00"), at)
        for junk in ("soon", "", None, True, 12345, [], {}, "2026-13-45T99:00:00Z", float("nan")):
            self.assertIsNone(runner.limit_epoch(junk), junk)

    def test_limit_fraction(self):
        self.assertEqual(runner.limit_fraction(0.23), 0.23)
        self.assertEqual(runner.limit_fraction(0.85), 0.85)
        self.assertEqual(runner.limit_fraction(1.2), 1.2, "past the cap is kept, not read as 1.2 %")
        self.assertEqual(runner.limit_fraction(2), 2.0)
        self.assertEqual(runner.limit_fraction(85), 0.85, "over 2 can only be a percentage")
        self.assertEqual(runner.limit_fraction("23"), 0.23)
        self.assertEqual(runner.limit_fraction(-1), 0.0)
        self.assertEqual(runner.limit_fraction(900), 5.0)
        for junk in (None, True, False, "x", [], float("inf")):
            self.assertIsNone(runner.limit_fraction(junk), junk)

    def test_the_real_event(self):
        windows, overage, top = runner.normalize_rate_limit(RUN1_LIMIT["rate_limit_info"], NOW)
        self.assertEqual(windows, {"seven_day": {"used": 0.85, "status": "allowed_warning", "resets_at": 1794423600.0,
                                                 "threshold": 0.75, "resets_text": None, "observed_at": NOW,
                                                 "source": "job"}})
        self.assertEqual(overage, {"using": False, "status": "rejected", "resets_at": None,
                                   "disabled_reason": "org_level_disabled", "observed_at": NOW})
        self.assertEqual(top, "allowed_warning")

    def test_main_window_only(self):
        windows, overage, top = runner.normalize_rate_limit(
            {"status": "allowed", "resetsAt": 1794423600, "rateLimitType": "five_hour", "utilization": 0.3}, NOW)
        self.assertEqual(list(windows), ["five_hour"])
        self.assertEqual((windows["five_hour"]["used"], windows["five_hour"]["status"]), (0.3, "allowed"))
        self.assertIsNone(overage, "no overage fields, no overage")
        self.assertEqual(top, "allowed")

    def test_unified_windows_only(self):
        windows, _, top = runner.normalize_rate_limit({"unifiedWindows": {
            "five_hour": {"utilization": 0.1, "resetsAt": 1794423600, "status": "allowed"},
            "seven_day_opus": {"utilization": 0.5}, "Bad Name": {"utilization": 0.9}, "x": "not a dict"}}, NOW)
        self.assertEqual(sorted(windows), ["five_hour", "seven_day_opus"], "no main window without its fields")
        self.assertIsNone(top)

    def test_main_and_unified(self):
        windows, _, _ = runner.normalize_rate_limit({
            "status": "allowed_warning", "rateLimitType": "five_hour", "utilization": 0.5, "resetsAt": 1794423600,
            "unifiedWindows": {"five_hour": {"utilization": 0.6, "resetsAt": 1794427200},
                               "seven_day": {"utilization": 0.2, "status": "allowed"}}}, NOW)
        five = windows["five_hour"]
        self.assertEqual((five["used"], five["resets_at"]), (0.6, 1794427200.0), "the window's own figures win")
        self.assertEqual(five["status"], "allowed_warning", "the top-level status fills a missing one")
        self.assertEqual(windows["seven_day"]["status"], "allowed")

    def test_missing_window_type_and_odd_values(self):
        windows, overage, top = runner.normalize_rate_limit(
            {"status": "rejected", "utilization": "50", "isUsingOverage": True,
             "overageDisabledReason": "Org Level Disabled!", "overageStatus": "maybe"}, NOW)
        self.assertEqual(windows["current"]["used"], 0.5)
        self.assertEqual(top, "rejected")
        self.assertEqual((overage["using"], overage["status"], overage["disabled_reason"]), (True, None, None))
        self.assertEqual(runner.normalize_rate_limit("nope", NOW), ({}, None, None))
        self.assertEqual(runner.normalize_rate_limit({"status": "weird"}, NOW), ({}, None, None))


class UsageCheckParsing(unittest.TestCase):
    REPORT = {"rate_limits": {"limits": [
        {"kind": "session", "group": "session", "percent": 31, "resets_at": "2026-11-06T23:00:00.000Z",
         "severity": "normal", "is_active": True},
        {"kind": "weekly_all", "percent": 47, "resets_at": "2026-11-10T09:00:00.000Z", "severity": "warning"},
        {"kind": "weekly_scoped", "percent": 12, "resets_at": "2026-11-10T09:00:00.000Z",
         "scope": {"model": {"display_name": "Fable"}}},
        {"kind": "weekly_scoped", "percent": 3, "scope": {"model": {"display_name": "Sonnet"}}, "severity": "error"},
        {"kind": "monthly_new", "percent": 99}, "not a dict"]}}

    def test_report(self):
        windows = runner.parse_usage_report(self.REPORT, NOW)
        self.assertEqual(sorted(windows), ["five_hour", "seven_day", "seven_day_overage_included", "seven_day_sonnet"],
                         "unknown kinds are left out; Fable is the window Claude Code's job events call the same")
        self.assertEqual(windows["five_hour"], {"used": 0.31, "status": "allowed", "resets_at": epoch(2026, 11, 6, 23),
                                                "resets_text": None, "threshold": None, "observed_at": NOW,
                                                "source": "check"})
        self.assertEqual((windows["seven_day"]["used"], windows["seven_day"]["status"]), (0.47, "allowed_warning"))
        self.assertEqual(windows["seven_day_overage_included"]["used"], 0.12)
        self.assertEqual(windows["seven_day_sonnet"]["status"], "rejected")
        self.assertIsNone(windows["seven_day_overage_included"]["status"])

    def test_report_without_figures(self):
        self.assertEqual(runner.parse_usage_report({"rate_limits": None}, NOW), {})
        self.assertEqual(runner.parse_usage_report(None, NOW), {})
        self.assertEqual(runner.parse_usage_report({"rate_limits": {"limits": "x"}}, NOW), {})

    def test_window_for(self):
        cases = [("session", None, "five_hour"), ("Session", "", "five_hour"), ("week", None, "seven_day"),
                 ("week", "all models", "seven_day"), ("week", "Sonnet only", "seven_day_sonnet"),
                 ("week", "Opus", "seven_day_opus"), ("week", "Fable", "seven_day_overage_included"),
                 ("weekly", "Claude Haiku 5 only", "seven_day_claude_haiku_5"), ("week", "!!", "seven_day"),
                 ("month", None, "current")]
        for kind, model, wid in cases:
            self.assertEqual(runner.window_for(kind, model), wid, (kind, model))
        self.assertTrue(runner.WINDOW_ID_RE.match(runner.window_for("week", "x" * 80)))

    def test_text_in_the_real_format(self):
        text = ("You are currently using your subscription to power your Claude Code usage\n"
                "Current session: 31% used · resets Nov 6, 11pm (UTC)\n"
                "Current week (all models): 47% used · resets Nov 10, 9am (UTC)\n"
                "Current week (Sonnet only): 3% used · resets Nov 10, 9am (UTC)\n"
                "Current week (Fable): 12% used · resets Nov 10, 9am (UTC)\n")
        windows = runner.parse_usage_text(text, NOW)
        self.assertEqual(sorted(windows), ["five_hour", "seven_day", "seven_day_overage_included", "seven_day_sonnet"])
        self.assertEqual(windows["five_hour"], {"used": 0.31, "status": None, "resets_at": epoch(2026, 11, 6, 23),
                                                "resets_text": "Nov 6, 11pm (UTC)", "threshold": None,
                                                "observed_at": NOW, "source": "check"})
        self.assertEqual(windows["seven_day"]["resets_at"], epoch(2026, 11, 10, 9))
        self.assertEqual(windows["seven_day_sonnet"]["used"], 0.03)
        self.assertEqual(windows["seven_day_overage_included"]["used"], 0.12)

    def test_text_with_bars_and_colours(self):
        text = ("\x1b[1mCurrent session\x1b[22m\n"
                "████▌                 15% used\n"
                "Resets 3pm (UTC)\n\n"
                "\x1b[1mCurrent week (all models)\x1b[22m\n"
                "█████░░░  45.5% used\n"
                "Resets Mon 9am (UTC)\n")
        windows = runner.parse_usage_text(text, NOW)
        self.assertEqual(windows["five_hour"]["used"], 0.15)
        self.assertEqual(windows["five_hour"]["resets_text"], "3pm (UTC)")
        self.assertEqual(windows["five_hour"]["resets_at"], epoch(2026, 11, 6, 15))
        self.assertEqual(windows["seven_day"]["used"], 0.455)
        self.assertEqual(windows["seven_day"]["resets_at"], epoch(2026, 11, 9, 9))

    def test_text_with_nothing_usable(self):
        self.assertEqual(runner.parse_usage_text("Usage data is not available right now.", NOW), {})
        self.assertEqual(runner.parse_usage_text("31% used · resets 3pm\nbefore any heading", NOW), {})
        self.assertEqual(runner.parse_usage_text("Current session\nnothing here", NOW), {})
        self.assertEqual(runner.parse_usage_text(None, NOW), {})
        limited = runner.RATE_LIMITED_RE.search("Error: rate limited until 5:30pm (UTC)")
        self.assertEqual(limited.group(1), "5:30pm (UTC)")

    def test_reset_text(self):
        self.assertEqual(runner.parse_reset_text("Nov 6, 11pm (UTC)", NOW), epoch(2026, 11, 6, 23))
        self.assertEqual(runner.parse_reset_text("Nov 6, 2027, 11pm (UTC)", NOW), epoch(2027, 11, 6, 23))
        self.assertEqual(runner.parse_reset_text("Nov 6, 11:30pm (UTC)", NOW), epoch(2026, 11, 6, 23, 30))
        self.assertEqual(runner.parse_reset_text("in 2h 5m", NOW), NOW + 2 * 3600 + 5 * 60)
        self.assertEqual(runner.parse_reset_text("in 3 days", NOW), NOW + 3 * 86400)
        self.assertEqual(runner.parse_reset_text("Mon 9am (UTC)", NOW), epoch(2026, 11, 9, 9))
        self.assertEqual(runner.parse_reset_text("Fri 9am (UTC)", NOW), epoch(2026, 11, 13, 9), "9am today has passed")
        self.assertEqual(runner.parse_reset_text("3pm (UTC)", NOW), epoch(2026, 11, 6, 15))
        self.assertEqual(runner.parse_reset_text("11am (UTC)", NOW), epoch(2026, 11, 7, 11), "the next 11am")
        self.assertEqual(runner.parse_reset_text("15:00 (UTC)", NOW), epoch(2026, 11, 6, 15))
        self.assertEqual(runner.parse_reset_text("12am (UTC)", NOW), epoch(2026, 11, 7, 0))
        december = epoch(2026, 12, 20, 12)
        self.assertEqual(runner.parse_reset_text("Nov 6, 11pm (UTC)", december), epoch(2027, 11, 6, 23),
                         "a date more than a day in the past is next year's")
        for junk in ("whenever", "", "Nov 31, 3pm (UTC)", "25:00 (UTC)", "13pm (UTC)", None, "Foo 9am"):
            self.assertIsNone(runner.parse_reset_text(junk, NOW), junk)

    def test_reset_text_time_zones(self):
        try:
            from zoneinfo import ZoneInfo
            berlin = ZoneInfo("Europe/Berlin")
        except Exception:  # no time zone data here (Windows without tzdata): local time is used
            berlin = None
        expected = datetime.datetime(2026, 11, 6, 23, 30, tzinfo=berlin).timestamp()
        self.assertEqual(runner.parse_reset_text("Nov 6, 11:30pm (Europe/Berlin)", NOW), expected)
        self.assertEqual(runner.parse_reset_text("Nov 6, 11pm (Mars/Olympus_Mons)", NOW),
                         datetime.datetime(2026, 11, 6, 23, 0).timestamp(), "an unknown zone is this machine's time")

    def test_summary(self):
        windows = runner.parse_usage_report(UsageCheckParsing.REPORT, NOW)
        line = runner.usage_summary({k: windows[k] for k in ("five_hour", "seven_day", "seven_day_overage_included")})
        self.assertTrue(line.startswith("5-hour session 31% used, resets "), line)
        self.assertTrue(line.endswith(" · week 47% · Fable 12%"), line)
        self.assertEqual(runner.window_label("seven_day_claude_haiku"), "Week (Claude Haiku)")
        self.assertEqual(runner.window_label("seven_day", lower=True), "week (all models)")


def since_result(base: dict, since: dict, cumulative: dict, cost: float) -> dict:
    """A result shaped like the real ones, with other figures."""
    return {**base, "modelUsage": cumulative, "model_usage_since_last_result": since, "total_cost_usd": cost}


class Usage(unittest.TestCase):
    def test_a_resumed_job(self):
        tracker = runner.UsageTracker("claude")
        tracker.cli_version = "2.1.319"
        tracker.add_claude(RUN2_RESULT)
        total = tracker.total()
        self.assertEqual({k: total["reported"][k] for k in runner.USAGE_FIELDS + ("cost_usd",)},
                         {"input_tokens": 6, "output_tokens": 8, "cache_read_tokens": 24978, "cache_write_tokens": 9289,
                          "cost_usd": 0.039746}, "the conversation's total, from modelUsage and total_cost_usd")
        self.assertEqual({k: total["own"][k] for k in runner.USAGE_FIELDS + ("cost_usd",)},
                         {"input_tokens": 4, "output_tokens": 4, "cache_read_tokens": 13596, "cache_write_tokens": 3545,
                          "cost_usd": 0.015588}, "this run's share, from model_usage_since_last_result")
        self.assertEqual(RUN2_RESULT["usage"]["cache_read_input_tokens"], 13596, "usage is this run's only")
        self.assertEqual(sorted(total["own"]["models"]), ["claude-sonnet-5-5"])
        self.assertEqual((total["source"], total["turns"], total["cli_version"]), ("claude", 1, "2.1.319"))

    def test_two_turns_in_one_process(self):
        # As in live2.py: two results from one process, cumulative modelUsage and per-turn shares
        # (the run1 and run2 shares as the two turns).
        one = RUN1_RESULT["model_usage_since_last_result"]
        two = RUN2_RESULT["model_usage_since_last_result"]
        both = {m: {k: (v + two[m][k]) if isinstance(v, (int, float)) and not isinstance(v, bool) else v
                    for k, v in one[m].items()} for m in one}
        tracker = runner.UsageTracker("claude")
        tracker.add_claude(since_result(RUN1_RESULT, one, one, 0.0241582))
        tracker.add_claude(since_result(RUN2_RESULT, two, both, 0.0397458))
        total = tracker.total()
        self.assertEqual(total["turns"], 2)
        self.assertEqual(total["reported"]["output_tokens"], 8, "the last cumulative figure, never a sum of them")
        self.assertEqual(total["own"]["output_tokens"], 8, "the sum of the shares")
        self.assertEqual(total["own"]["cache_read_tokens"], 11382 + 13596)
        self.assertEqual(total["own"]["cost_usd"], round(0.0241582 + 0.0155876, 6))
        self.assertEqual(total["own"]["models"]["claude-sonnet-5-5"]["input_tokens"], 6)

    def test_a_zeroed_crash_result_is_skipped(self):
        tracker = runner.UsageTracker("claude")
        tracker.add_claude(RUN1_RESULT)
        zero = {"claude-sonnet-5-5": {"inputTokens": 0, "outputTokens": 0, "cacheReadInputTokens": 0,
                                      "cacheCreationInputTokens": 0, "costUSD": 0}}
        tracker.add_claude({"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "modelUsage": zero, "model_usage_since_last_result": zero, "total_cost_usd": 0,
                            "usage": {"input_tokens": 0, "output_tokens": 0}})
        tracker.add_claude({"type": "result", "subtype": "error_during_execution", "is_error": True})
        total = tracker.total()
        self.assertEqual(total["turns"], 1)
        self.assertEqual(total["reported"]["cache_write_tokens"], 5744)

    def test_without_shares_own_is_unknown(self):
        tracker = runner.UsageTracker("claude")
        tracker.add_claude(RUN1_RESULT)
        tracker.add_claude({k: v for k, v in RUN2_RESULT.items() if k != "model_usage_since_last_result"})
        total = tracker.total()
        self.assertIsNone(total["own"], "the hub then subtracts the job this one continues")
        self.assertEqual(total["reported"]["output_tokens"], 8)

    def test_an_old_cli_without_model_usage(self):
        tracker = runner.UsageTracker("claude")
        tracker.add_claude({"type": "result", "usage": {"input_tokens": 5, "output_tokens": 7,
                                                        "cache_read_input_tokens": 1, "cache_creation_input_tokens": 2},
                            "total_cost_usd": 0.5})
        total = tracker.total()
        self.assertEqual(total["reported"], {"input_tokens": 5, "output_tokens": 7, "cache_read_tokens": 1,
                                             "cache_write_tokens": 2, "cost_usd": 0.5, "models": {}, "scope": "run"})
        self.assertIsNone(total["own"])
        self.assertIsNone(runner.UsageTracker("claude").total(), "nothing reported, nothing sent")
        tracker = runner.UsageTracker("claude")
        tracker.add_claude({"type": "result", "result": "no figures"})
        self.assertIsNone(tracker.total())

    def test_gemini(self):
        tracker = runner.UsageTracker("gemini")
        tracker.add_gemini({"total_tokens": 600, "input_tokens": 500, "output_tokens": 100, "cached": 50, "input": 450,
                            "models": {"gemini-fake": {"input_tokens": 500, "output_tokens": 100, "cached": 50,
                                                       "input": 450}, "odd": "x"}})
        total = tracker.total()
        self.assertEqual(total["reported"], total["own"])
        self.assertEqual({k: total["own"][k] for k in runner.USAGE_FIELDS + ("cost_usd",)},
                         {"input_tokens": 450, "output_tokens": 100, "cache_read_tokens": 50, "cache_write_tokens": 0,
                          "cost_usd": None}, "input without the cached tokens, and no dollar figure")
        self.assertEqual(list(total["own"]["models"]), ["gemini-fake"])
        self.assertEqual((total["source"], total["turns"]), ("gemini", 1))
        tracker = runner.UsageTracker("gemini")
        tracker.add_gemini({"input_tokens": 500, "output_tokens": 100, "cached": 50})
        self.assertEqual(tracker.total()["own"]["input_tokens"], 450, "input_tokens minus cached when 'input' is missing")


class LimitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        # The fixtures are dated NOW, and the store forgets windows older than LIMITS_MAX_AGE by the
        # real clock. Pin the clock just after NOW, or these tests expire eight days after NOW.
        clock = mock.patch.object(runner.time, "time", return_value=NOW + 60)
        clock.start()
        self.addCleanup(clock.stop)

    def obs(self, at: float, **fields) -> dict:
        base = {"used": None, "status": None, "resets_at": None, "threshold": None, "resets_text": None,
                "observed_at": at, "source": "job"}
        return {**base, **fields}

    def test_merged_field_by_field(self):
        store = runner.LimitStore(path=None)
        store.record("one", {"five_hour": self.obs(NOW, used=0.3, status="allowed", resets_at=NOW + 3600)}, None)
        store.record("one", {"five_hour": self.obs(NOW + 60, status="allowed_warning")}, None, "allowed_warning",
                     now=NOW + 60)
        w = store.view("one")["windows"]["five_hour"]
        self.assertEqual((w["used"], w["used_at"]), (0.3, NOW), "an event with only a status keeps the number")
        self.assertEqual((w["status"], w["status_at"]), ("allowed_warning", NOW + 60))
        self.assertEqual(w["resets_at"], NOW + 3600)
        self.assertEqual(w["observed_at"], NOW + 60)
        self.assertEqual(store.view("one")["status"], {"value": "allowed_warning", "observed_at": NOW + 60})
        store.record("one", {"five_hour": self.obs(NOW + 120, used=0.31, source="check", resets_text="3pm")}, None)
        store.record("one", {"five_hour": self.obs(NOW + 30, used=0.99)}, None)
        w = store.view("one")["windows"]["five_hour"]
        self.assertEqual((w["used"], w["source"], w["resets_text"]), (0.31, "check", "3pm"),
                         "an older observation doesn't replace a newer figure")

    def test_overage_and_views_are_copies(self):
        store = runner.LimitStore(path=None)
        store.record("one", {}, {"using": True, "status": "allowed", "resets_at": None, "disabled_reason": None,
                                 "observed_at": NOW})
        view = store.view("one")
        self.assertTrue(view["overage"]["using"])
        view["overage"]["using"] = False
        self.assertTrue(store.view("one")["overage"]["using"])
        self.assertEqual(store.view("nobody"), {"windows": {}, "overage": None, "status": None})

    def test_changed(self):
        store = runner.LimitStore(path=None)
        store.record("one", {}, None)
        self.assertFalse(store.changed.is_set(), "nothing new, nothing to send")
        self.assertEqual(store.agents, {})
        store.record("one", {"five_hour": self.obs(NOW, used=0.2)}, None)
        self.assertTrue(store.changed.is_set())
        store.changed.clear()
        store.record("one", {"five_hour": self.obs(NOW, used=0.2)}, None)
        self.assertFalse(store.changed.is_set(), "the same observation again changes nothing")

    def test_old_windows_are_pruned(self):
        store = runner.LimitStore(path=None)
        store.record("one", {"seven_day_opus": self.obs(NOW - 9 * 86400, used=0.5)}, None, "rejected",
                     now=NOW - 9 * 86400)
        store.record("one", {"five_hour": self.obs(NOW, used=0.1)}, None, now=NOW)
        view = store.view("one")
        self.assertEqual(list(view["windows"]), ["five_hour"])
        self.assertIsNone(view["status"], "an 8-day-old status is gone too")
        many = {f"w{i:02}": self.obs(NOW + i, used=0.1) for i in range(20)}
        store.record("two", many, None, now=NOW)
        self.assertEqual(len(store.view("two")["windows"]), runner.MAX_WINDOWS)
        self.assertIn("w19", store.view("two")["windows"], "the newest are kept")

    def test_file_round_trip(self):
        path = Path(tempfile.mkdtemp(prefix="bullpen-limits-")) / "limits.json"
        store = runner.LimitStore(path)
        store.record("one", {"five_hour": self.obs(time.time(), used=0.4, status="allowed")},
                     {"using": False, "status": "rejected", "resets_at": None, "disabled_reason": "org_level_disabled",
                      "observed_at": time.time()}, "allowed")
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["agents"]["one"]["windows"]["five_hour"]["used"], 0.4)
        if POSIX:
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        again = runner.LimitStore(path)
        self.assertEqual(again.view("one"), store.view("one"), "the figures survive a restart")
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(runner.LimitStore(path).agents, {})
        path.write_text(json.dumps({"version": 1, "agents": {"one": {"windows": {"BAD ID": {}, "ok": "x"}}}}),
                        encoding="utf-8")
        self.assertEqual(runner.LimitStore(path).view("one")["windows"], {})

    def test_in_memory_only(self):
        before = set(Path(runner.BASE_DIR).glob("limits*.json"))
        store = runner.LimitStore(path=None)
        store.record("one", {"five_hour": self.obs(NOW, used=0.4)}, None)
        self.assertIsNone(store.path)
        self.assertEqual(set(Path(runner.BASE_DIR).glob("limits*.json")), before, "nothing written")


class ClaudeLimitSignals(unittest.TestCase):
    def feed(self, events: list, **kw) -> tuple:
        seen: list = []
        parser = runner.ClaudeStreamParser(on_limits=lambda *a: seen.append(a), **kw)
        lines: list = []
        for ev in events:
            lines += parser.feed(json.dumps(ev))
        return parser, lines, seen

    def test_the_real_events(self):
        init = {"type": "system", "subtype": "init", "session_id": "s", "model": "m", "claude_code_version": "2.1.319"}
        parser, lines, seen = self.feed([init, RUN1_LIMIT, RUN1_LIMIT, RUN1_RESULT, {**init, "claude_code_version": "9"}])
        self.assertEqual(len(seen), 2, "every event is kept")
        windows, overage, top = seen[0]
        self.assertEqual((windows["seven_day"]["used"], overage["disabled_reason"], top),
                         (0.85, "org_level_disabled", "allowed_warning"))
        self.assertEqual([ln for ln in lines if ln.startswith("⚠")],
                         ["⚠ Getting close to the week (all models) limit (85% used)"], "once per window per job")
        self.assertEqual(sum(ln.startswith("▶ session started") for ln in lines), 1, "only the first init counts")
        self.assertEqual(parser.cli_version, "2.1.319")
        self.assertIsNone(parser.error)
        self.assertEqual(parser.usage.total()["cli_version"], "2.1.319")
        self.assertEqual(parser.turns, [{"turn_no": 1, "result": "pong", "is_error": False}])

    def test_a_refused_request(self):
        reset = epoch(2026, 11, 7, 15)
        events = [{"type": "rate_limit_event", "rate_limit_info": {
                       "status": "rejected", "resetsAt": reset, "rateLimitType": "five_hour", "utilization": 1.0,
                       "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": reset, "status": "rejected"}}}},
                  {"type": "assistant", "error": "rate_limit", "message": {"content": [
                      {"type": "text", "text": "You've hit your 5-hour limit · resets 3pm (UTC)"}]}},
                  {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429,
                   "result": "You've hit your 5-hour limit · resets 3pm (UTC)"}]
        parser, lines, _ = self.feed(events)
        self.assertIn("⛔ Usage limit reached: 5-hour session, resets 2026-11-07 15:00 UTC", lines)
        self.assertEqual(parser.error_kind, "claude_limit")
        self.assertEqual(parser.error, "Claude usage limit reached (5-hour session). It resets at 2026-11-07 15:00 UTC. "
                                       "Wait for the reset: the hub doesn't move work to another account (guide 1). "
                                       "Claude said: You've hit your 5-hour limit · resets 3pm (UTC)")
        parser.start_turn()
        self.assertEqual((parser.error, parser.error_kind, parser.limit_signal), (None, None, False))

    def test_the_text_alone(self):
        parser, _, _ = self.feed([{"type": "result", "is_error": True,
                                   "result": "You’ve hit your 5-hour limit · resets 3pm (UTC)"}])
        self.assertEqual(parser.error_kind, "claude_limit")
        self.assertTrue(parser.error.startswith("Claude usage limit reached (5-hour). It resets at "), parser.error)
        parser, _, _ = self.feed([{"type": "result", "is_error": True, "api_error_status": "429", "result": "Error"}])
        self.assertEqual(parser.error, "Claude usage limit reached. Wait for the reset: the hub doesn't move work to "
                                       "another account (guide 1). Claude said: Error")

    def test_other_errors_stay_as_they_were(self):
        parser, _, _ = self.feed([{"type": "result", "subtype": "error_max_turns", "is_error": True,
                                   "result": "Reached the maximum number of turns"}])
        self.assertEqual(parser.error, "error_max_turns: Reached the maximum number of turns")
        self.assertIsNone(parser.error_kind)
        self.assertIsNone(parser.exit_error())

    def test_an_exit_after_a_refusal(self):
        parser, _, _ = self.feed([{"type": "assistant", "error": "rate_limit", "message": {"content": [
            {"type": "text", "text": "API Error: Rate limit reached"}]}}])
        self.assertIsNone(parser.error)
        self.assertEqual(parser.exit_error(), "Claude usage limit reached. Wait for the reset: the hub doesn't move work "
                                              "to another account (guide 1). Claude said: API Error: Rate limit reached")
        self.assertEqual(parser.error_kind, "claude_limit")


class GeminiQuota(unittest.TestCase):
    MESSAGE = selftest_agent.GEMINI_QUOTA_MESSAGE

    def test_the_real_shape(self):
        parser = runner.GeminiStreamParser()
        parser.feed(json.dumps({"type": "result", "status": "error",
                                "error": {"type": "TerminalQuotaError", "message": self.MESSAGE},
                                "stats": {"input_tokens": 0, "output_tokens": 0}}))
        self.assertNotIn("429", self.MESSAGE)
        self.assertEqual(parser.error_kind, "quota")
        self.assertTrue(parser.error.startswith("Gemini quota hit: the Google Cloud project behind this agent's API key"))
        self.assertIn("https://aistudio.google.com/rate-limit", parser.error)
        self.assertIn("Gemini said: [API Error: You exceeded your current quota", parser.error)
        self.assertEqual(parser.usage.total()["turns"], 1)

    def test_text_error_events_and_stderr(self):
        parser = runner.GeminiStreamParser()
        parser.feed(json.dumps({"type": "result", "status": "error",
                                "error": {"type": "Error", "message": "got status: 429 RESOURCE_EXHAUSTED"}}))
        self.assertEqual(parser.error_kind, "quota")
        # Only the result itself says how the run ended (GeminiQuotaWords has the retried-429 cases).
        parser = runner.GeminiStreamParser()
        parser.feed(json.dumps({"type": "error", "severity": "error", "message": "Quota exceeded for metric"}))
        parser.feed(json.dumps({"type": "result", "status": "error", "error": {"type": "Error", "message": "failed"}}))
        self.assertEqual((parser.error_kind, parser.error), (None, "failed"))
        parser = runner.GeminiStreamParser()
        parser.feed(json.dumps({"type": "error", "severity": "error", "message": "Quota exceeded for metric"}))
        self.assertTrue(parser.exit_error().startswith("Gemini quota hit"), "no result: what it said last counts")
        parser = runner.GeminiStreamParser()
        parser.note_stderr('Error when talking to Gemini API _ApiError: {"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}')
        self.assertTrue(parser.exit_error().startswith("Gemini quota hit"))
        self.assertEqual(parser.error_kind, "quota")

    def test_other_errors(self):
        parser = runner.GeminiStreamParser()
        parser.note_stderr("some warning")
        parser.feed(json.dumps({"type": "result", "status": "error",
                                "error": {"type": "FatalToolExecutionError", "message": "the tool failed"}}))
        self.assertEqual((parser.error, parser.error_kind), ("the tool failed", None))
        self.assertIsNone(parser.exit_error())


def clean_auth_env():
    """The runner's own environment without sign-in variables (CI may have some)."""
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    for key in AUTH_VARS:
        os.environ.pop(key, None)
    return patcher


class LimitJobs(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(clean_auth_env().stop)
        self.store = runner.LimitStore(path=None)
        gemfake = {"preset": "gemini", "command": sys.executable,
                   "args": [str(ROOT / "runner" / "selftest_agent.py"), "--gemini"], "env": self.selftest_env}
        self.cfg = make_cfg(self.tmp, {
            "one": {"preset": "selftest", "env": dict(self.selftest_env, SELFTEST_UTIL="0.55")},
            "nosince": {"preset": "selftest", "env": dict(self.selftest_env, SELFTEST_NO_SINCE="1")},
            "gemfake": gemfake})

    def test_a_job_keeps_the_limits_and_reports_its_tokens(self):
        beats: list = []
        finished, lines, _ = run_job(self.cfg, {"id": 20, "agent": "one", "project": "chat", "prompt": "hello"},
                                     limits=self.store, send_heartbeat=lambda: beats.append(1))
        self.assertEqual(finished["status"], "done", finished)
        windows = self.store.view("one")["windows"]
        self.assertEqual((windows["five_hour"]["used"], windows["seven_day"]["used"]), (0.55, 0.41))
        self.assertGreater(windows["five_hour"]["resets_at"], time.time())
        self.assertEqual(beats, [1], "a check-in as soon as the job ends, so the page is up to date")
        usage = finished["usage"]
        self.assertEqual((usage["own"]["output_tokens"], usage["own"]["cost_usd"]), (200, 0.01))
        self.assertEqual((usage["source"], usage["turns"], usage["cli_version"]), ("claude", 1, "2.1.400"))
        self.assertNotIn("error_kind", finished)
        follow, _, started = run_job(self.cfg, {"id": 21, "agent": "one", "project": "chat", "prompt": "more",
                                                "resume_session": finished["session_id"]}, limits=self.store)
        self.assertEqual(follow["usage"]["own"]["output_tokens"], 200, "only what the follow-up added")
        self.assertEqual(follow["usage"]["reported"]["output_tokens"], 400, "the conversation so far")
        self.assertIn("--resume", started[0])

    def test_without_per_turn_figures(self):
        finished, _, _ = run_job(self.cfg, {"id": 22, "agent": "nosince", "project": "chat", "prompt": "hi"})
        self.assertIsNone(finished["usage"]["own"])
        self.assertEqual(finished["usage"]["reported"]["output_tokens"], 200)

    def test_a_usage_limit(self):
        finished, lines, _ = run_job(self.cfg, {"id": 23, "agent": "one", "project": "chat",
                                                "prompt": "SELFTEST: limit-rejected"}, limits=self.store)
        self.assertEqual((finished["status"], finished["error_kind"]), ("error", "claude_limit"))
        self.assertIn("(5-hour session)", finished["error"])
        self.assertTrue(any(ln.startswith("⛔ Usage limit reached: 5-hour session") for ln in lines), lines)
        self.assertEqual(self.store.view("one")["status"]["value"], "rejected")

    def test_gemini_quota_and_tokens(self):
        finished, _, _ = run_job(self.cfg, {"id": 24, "agent": "gemfake", "project": "chat",
                                            "prompt": "SELFTEST: gemini-429"})
        self.assertEqual((finished["status"], finished["error_kind"]), ("error", "quota"))
        self.assertTrue(finished["error"].startswith("Gemini quota hit"))
        finished, _, _ = run_job(self.cfg, {"id": 25, "agent": "gemfake", "project": "chat", "prompt": "hi"})
        self.assertEqual(finished["status"], "done", finished)
        own = finished["usage"]["own"]
        self.assertEqual((own["input_tokens"], own["cache_read_tokens"], own["cost_usd"]), (450, 50, None))

    def test_a_stub_without_limits_still_runs(self):
        finished, _, _ = run_job(self.cfg, {"id": 26, "agent": "one", "project": "chat", "prompt": "hi"})
        self.assertEqual(finished["status"], "done")
        self.assertIn("usage", finished)


class CheckLimits(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(clean_auth_env().stop)
        self.store = runner.LimitStore(path=None)
        env = self.selftest_env
        script = "import sys; sys.stdin.read(); print('Error: usage is rate limited until 5pm (UTC)')"
        self.cfg = make_cfg(self.tmp, {
            "one": {"preset": "selftest", "env": env},
            "text": {"preset": "selftest", "env": dict(env, SELFTEST_USAGE="text")},
            "quiet": {"preset": "selftest", "env": dict(env, SELFTEST_USAGE="none")},
            "apikey": {"preset": "selftest", "env": dict(env, ANTHROPIC_API_KEY="sk-unit-apikey-0123456789")},
            "tokenagent": {"preset": "selftest", "env": dict(env, CLAUDE_CODE_OAUTH_TOKEN="fake-oauth-token-0123")},
            "limited": {"preset": "selftest", "usage_check": {"args": ["-c", script]}},
            "broken": {"preset": "selftest", "usage_check": {"args": ["-c", "import sys; sys.exit(3)"]}},
            "gem": {"preset": "gemini", "command": sys.executable}})

    def check(self, agents: str, **stub) -> tuple:
        stub.setdefault("limits", self.store)
        return run_job(self.cfg, {"id": 30, "kind": "admin", "agent": None, "prompt": "",
                                  "options": {"action": "usage.check", "params": {"agents": agents}}}, **stub)

    def test_the_report(self):
        before = time.time()
        finished, lines, started = self.check("one")
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(started, [[sys.executable, str(ROOT / "runner" / "selftest_agent.py"), "-p", "--output-format",
                                    "stream-json", "--verbose", "--max-turns", "1", "--permission-mode", "dontAsk"]],
                         "the CLI as it is, with /usage on its input")
        self.assertIn("| one | 5-hour session | 31% |", finished["result"])
        self.assertIn("| one | Week (Fable) | 12% |", finished["result"])
        windows = self.store.view("one")["windows"]
        five = windows["five_hour"]
        self.assertEqual((five["used"], five["source"]), (0.31, "check"))
        self.assertEqual(five["resets_at"] % 3600, 0, "the report's own reset time")
        self.assertTrue(before + 1.5 * 3600 <= five["resets_at"] <= time.time() + 2.6 * 3600)
        self.assertEqual(windows["seven_day"]["used"], 0.47)
        self.assertEqual(windows["seven_day_overage_included"]["used"], 0.12)
        self.assertTrue(any(ln.startswith("one: 5-hour session 31% used, resets ") and ln.endswith("Fable 12%")
                            for ln in lines), lines)

    def test_the_text_when_there_is_no_report(self):
        finished, _, _ = self.check("text")
        self.assertEqual(finished["status"], "done", finished)
        five = self.store.view("text")["windows"]["five_hour"]
        self.assertEqual(five["used"], 0.31)
        self.assertTrue(five["resets_text"].endswith("(UTC)"), five)
        self.assertEqual(five["resets_at"], runner.parse_reset_text(five["resets_text"], time.time()))

    def test_a_login_that_cant_read_plan_usage(self):
        finished, _, _ = self.check("quiet")
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "quiet's login can't read plan usage (Claude Code returned none). "
                                             "A full login (\"selftest auth login\") can.")
        self.assertEqual(self.store.view("quiet")["windows"], {})

    def test_nothing_usable(self):
        finished, _, _ = self.check("limited")
        self.assertEqual(finished["status"], "done", finished)
        self.assertIn("Claude Code printed nothing the hub could read as plan usage for limited.", finished["result"])
        self.assertIn("Claude Code says it is rate limited until 5pm (UTC).", finished["result"])

    def test_agents_that_cant_check(self):
        finished, _, started = self.check("apikey,tokenagent,gem,nosuch,broken")
        self.assertEqual(finished["status"], "error", "every agent failed")
        self.assertIn("apikey can't check plan limits: it signs in with an API key", finished["error"])
        self.assertIn("tokenagent can't check plan limits: it signs in with a long-lived token (claude setup-token)",
                      finished["error"])
        self.assertIn("gem can't check plan limits", finished["error"])
        self.assertIn("nosuch: there is no agent with that name on this machine", finished["error"])
        self.assertIn("broken: Claude Code exited with code 3", finished["error"])
        self.assertEqual(len(started), 1, "only the agent that may check was started")
        finished, _, _ = self.check("one,apikey")
        self.assertEqual(finished["status"], "done", "one agent ran")
        self.assertIn("- apikey can't check plan limits", finished["result"])

    def test_every_agent_that_can(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env},
                                  "apikey": {"preset": "selftest", "env": {"ANTHROPIC_API_KEY": "sk-unit-0123456789ab"}}})
        finished, _, started = run_job(cfg, {"id": 31, "kind": "admin", "options": {"action": "usage.check",
                                                                                    "params": {}}}, limits=self.store)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(len(started), 1)
        cfg = make_cfg(self.tmp, {"gem": {"preset": "gemini", "command": sys.executable}})
        finished, _, _ = run_job(cfg, {"id": 32, "kind": "admin", "options": {"action": "usage.check", "params": {}}})
        self.assertEqual(finished, {"status": "error", "error": "No agent on this machine can check plan limits."})

    def test_a_stub_without_limits(self):
        finished, _, _ = self.check("one", limits=None)
        self.assertEqual(finished["status"], "done", finished)

    def test_no_answer_in_time(self):
        cfg = make_cfg(self.tmp, {"sleepy": {"preset": "selftest",
                                             "usage_check": {"args": ["-c", "import time; time.sleep(30)"]}}})
        with mock.patch.object(runner, "USAGE_CHECK_TIMEOUT", 1):
            started = time.monotonic()
            finished, _, _ = run_job(cfg, {"id": 33, "kind": "admin", "options": {"action": "usage.check",
                                                                                  "params": {"agents": "sleepy"}}})
        self.assertEqual(finished["status"], "error")
        self.assertIn("sleepy: no answer within 1 seconds", finished["error"])
        self.assertLess(time.monotonic() - started, 20)


class RunnerLimits(TempDirCase):
    def test_heartbeat_carries_limits(self):
        self.addCleanup(clean_auth_env().stop)
        path = Path(self.tmp) / "runner.json"
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "account": "work"},
                                  "gem": {"preset": "gemini", "command": sys.executable}})
        path.write_text(json.dumps(cfg), encoding="utf-8")
        r = runner.Runner(path)
        r.limits = runner.LimitStore(path=None)
        # observed now: the store forgets windows older than LIMITS_MAX_AGE by the real clock
        r.limits.record("one", {"five_hour": {"used": 0.2, "observed_at": time.time(), "source": "job"}}, None)
        agents = {a["name"]: a for a in r.heartbeat_payload()["agents"]}
        self.assertEqual(agents["one"]["limits"]["windows"]["five_hour"]["used"], 0.2)
        self.assertEqual(agents["one"]["account"], "work")
        self.assertNotIn("limits", agents["gem"], "only agents that report limits")
        hub = LocalHub()
        hub.calls = []
        r.hub = mock.Mock(call=lambda path, payload, timeout=30: (hub.calls.append(path), (200, {"cancel": []}))[1])
        self.assertTrue(r.limits.changed.is_set())
        r.send_heartbeat()
        self.assertFalse(r.limits.changed.is_set(), "a check-in carries the figures")
        self.assertEqual(hub.calls, ["/api/runner/heartbeat"])

    def test_runner_test_and_limits_commands(self):
        self.addCleanup(clean_auth_env().stop)
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps(make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}})),
                        encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.cmd_test(argparse.Namespace(config=str(path), agent="one", project="chat"))
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("OK", out.getvalue())
        with mock.patch.object(runner, "LIMITS_FILE", Path(self.tmp) / "limits.json"):
            runner.LimitStore(runner.LIMITS_FILE).record(
                "one", {"five_hour": {"used": 1.05, "resets_at": time.time() + 600, "observed_at": time.time(),
                                      "source": "job"}}, None, "rejected")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = runner.cmd_limits(argparse.Namespace(config=str(path), agent=""))
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("one (no account label)", text)
        self.assertIn("5-hour session", text)
        self.assertIn("105% used (over the limit)", text)
        self.assertIn("Limit reached", text)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.cmd_limits(argparse.Namespace(config=str(path), agent="nosuch")), 1)

    def limits_text(self, path: Path, store: "runner.LimitStore") -> str:
        out = io.StringIO()
        with mock.patch.object(runner, "LIMITS_FILE", store.path), contextlib.redirect_stdout(out):
            self.assertEqual(runner.cmd_limits(argparse.Namespace(config=str(path), agent="one")), 0)
        return out.getvalue()

    def test_limits_command_ends_a_refusal_like_the_dashboard(self):
        self.addCleanup(clean_auth_env().stop)
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps(make_cfg(self.tmp, {"one": {"preset": "selftest"}})), encoding="utf-8")
        now = time.time()
        # Refused 6 hours ago; the window reset an hour ago and nothing has been read since.
        store = runner.LimitStore(Path(self.tmp) / "expired.json")
        store.record("one", {"five_hour": {"used": 1.0, "status": "rejected", "resets_at": now - 3600,
                                           "observed_at": now - 6 * 3600, "source": "job"}},
                     None, "rejected", now=now - 6 * 3600)
        text = self.limits_text(path, store)
        self.assertNotIn("Limit reached", text)
        self.assertIn("reset at ", text)
        self.assertIn("no reading since", text)
        self.assertNotIn("resets ", text)
        self.assertNotIn("100% used", text, "a figure from before the reset isn't shown")
        # Refused 10 minutes ago, then Check limits read 31% used: the refusal is over.
        store = runner.LimitStore(Path(self.tmp) / "checked.json")
        store.record("one", {"five_hour": {"used": 1.0, "status": "rejected", "resets_at": now + 3600,
                                           "observed_at": now - 600, "source": "job"}},
                     None, "rejected", now=now - 600)
        self.assertIn("Limit reached", self.limits_text(path, store), "still refused before the check")
        store.record("one", {"five_hour": {"used": 0.31, "resets_at": now + 3600, "observed_at": now - 60,
                                           "source": "check"}}, None, now=now - 60)
        text = self.limits_text(path, store)
        self.assertIn("31% used", text)
        self.assertNotIn("Limit reached", text)
        self.assertNotIn("over the limit", text)
        # Exactly at the limit is not over it (as on the Usage page); close to it says so.
        store = runner.LimitStore(Path(self.tmp) / "full.json")
        store.record("one", {"five_hour": {"used": 1.0, "observed_at": now, "source": "job"},
                             "seven_day": {"used": 0.92, "observed_at": now, "source": "job"}}, None)
        text = self.limits_text(path, store)
        self.assertIn("100% used ", text)
        self.assertNotIn("over the limit", text)
        self.assertIn("92% used (getting close)", text)

    def test_limit_reached_rule(self):
        now = NOW
        rejected = {"value": "rejected", "observed_at": now - 600}
        win = {"used": 1.0, "used_at": now - 600, "status": "rejected", "status_at": now - 600,
               "resets_at": now + 3600}
        self.assertEqual(runner.limit_reached(rejected, {"five_hour": win}, now), (True, now + 3600))
        self.assertEqual(runner.limit_reached({"value": "allowed", "observed_at": now}, {"five_hour": win}, now),
                         (False, None))
        self.assertEqual(runner.limit_reached(None, {"five_hour": win}, now), (False, None))
        self.assertEqual(runner.limit_reached(rejected, {}, now), (True, None), "nothing has said otherwise yet")
        expired = dict(win, resets_at=now - 60)
        self.assertEqual(runner.limit_reached(rejected, {"five_hour": expired}, now), (False, None))
        lower = dict(win, used=0.31, used_at=now - 30)
        self.assertEqual(runner.limit_reached(rejected, {"five_hour": lower}, now), (False, None))
        # A newer figure still over the limit keeps the window refused.
        over = dict(win, used=1.02, used_at=now - 30)
        self.assertEqual(runner.limit_reached(rejected, {"five_hour": over}, now), (True, now + 3600))
        # The refusal named no window: a later reading of any window ends it.
        allowed = {"used": 0.5, "used_at": now - 30, "status": "allowed", "status_at": now - 30, "resets_at": now + 60}
        self.assertEqual(runner.limit_reached(rejected, {"seven_day": allowed}, now), (False, None))
        older = dict(allowed, used_at=now - 900, status_at=now - 900)
        self.assertEqual(runner.limit_reached(rejected, {"seven_day": older}, now), (True, None))


class SelftestUsage(TempDirCase):
    def run_agent(self, *args: str, prompt: str = "hello", **env: str) -> tuple:
        full = dict(os.environ, SELFTEST_DELAY="0", PYTHONIOENCODING="utf-8", **env)
        res = subprocess.run([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), *args], input=prompt,
                             capture_output=True, text=True, encoding="utf-8", cwd=self.tmp, env=full, timeout=60)
        return res.returncode, [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]

    def test_turn_figures_and_session_totals(self):
        home = Path(self.tmp) / "home"
        code, events = self.run_agent(BULLPEN_HOME=str(home))
        self.assertEqual(code, 0)
        self.assertEqual(events[0]["claude_code_version"], "2.1.400")
        self.assertEqual([e["type"] for e in events], ["system", "assistant", "rate_limit_event", "result"])
        info = events[2]["rate_limit_info"]
        self.assertEqual((info["rateLimitType"], info["utilization"], info["status"]), ("five_hour", 0.23, "allowed"))
        self.assertEqual(info["unifiedWindows"]["seven_day"]["utilization"], 0.41)
        result = events[-1]
        self.assertEqual(result["usage"], {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 300,
                                           "cache_creation_input_tokens": 100})
        self.assertEqual(result["modelUsage"]["selftest-1"]["outputTokens"], 200)
        self.assertEqual(result["model_usage_since_last_result"]["selftest-1"]["outputTokens"], 200)
        self.assertEqual(result["total_cost_usd"], 0.01)
        session = result["session_id"]
        self.assertTrue((home / "selftest" / "sessions" / f"{session}.json").exists())
        code, events = self.run_agent("--resume", session, "--model", "fake-large", BULLPEN_HOME=str(home))
        result = events[-1]
        self.assertEqual(result["usage"]["output_tokens"], 200, "usage is this turn only")
        self.assertEqual(result["modelUsage"]["selftest-1"]["outputTokens"], 200, "the resumed totals")
        self.assertEqual(result["modelUsage"]["fake-large"]["outputTokens"], 200)
        self.assertEqual(result["total_cost_usd"], 0.02)
        self.assertEqual(list(result["model_usage_since_last_result"]), ["fake-large"])
        code, events = self.run_agent("--resume", session, BULLPEN_HOME=str(home), SELFTEST_FAIL="1")
        self.assertEqual(code, 1)
        self.assertEqual(events[-1]["subtype"], "error_during_execution")
        code, events = self.run_agent("--resume", session, BULLPEN_HOME=str(home), SELFTEST_NO_SINCE="1",
                                      SELFTEST_LIMITS="0", SELFTEST_UTIL="0.9")
        self.assertEqual(events[-1]["total_cost_usd"], 0.03, "the failed run saved nothing")
        self.assertNotIn("model_usage_since_last_result", events[-1])
        self.assertNotIn("rate_limit_event", [e["type"] for e in events])

    def test_usage_command(self):
        code, events = self.run_agent(prompt="/usage\n")
        self.assertEqual(code, 0)
        self.assertEqual([e["type"] for e in events], ["system", "assistant", "result"])
        message = events[1]["message"]
        self.assertEqual(message["model"], "<synthetic>")
        limits = message["usage_report"]["rate_limits"]["limits"]
        self.assertEqual([(e["kind"], e["percent"]) for e in limits],
                         [("session", 31), ("weekly_all", 47), ("weekly_scoped", 12)])
        self.assertEqual(limits[2]["scope"]["model"]["display_name"], "Fable")
        self.assertTrue(limits[0]["resets_at"].endswith(":00:00.000Z"))
        result = events[-1]
        self.assertEqual((result["num_turns"], result["total_cost_usd"]), (0, 0))
        lines = result["result"].splitlines()
        self.assertEqual(lines[0], "You are currently using your subscription to power your Claude Code usage")
        self.assertTrue(lines[1].startswith("Current session: 31% used · resets "), lines)
        self.assertTrue(lines[1].endswith(" (UTC)"))
        windows = runner.parse_usage_text(result["result"], time.time())
        self.assertEqual(windows["five_hour"]["resets_at"], runner.limit_epoch(limits[0]["resets_at"]),
                         "the text and the report say the same")
        code, events = self.run_agent(prompt="/usage", SELFTEST_USAGE="text")
        self.assertNotIn("usage_report", events[1]["message"])
        code, events = self.run_agent(prompt="/usage", SELFTEST_USAGE="none")
        self.assertEqual(events[1]["message"]["usage_report"], {"rate_limits": None})
        self.assertEqual(events[-1]["result"], "Usage data is not available right now.")

    def test_limit_rejected(self):
        code, events = self.run_agent(prompt="Do it\nSELFTEST: limit-rejected")
        self.assertEqual(code, 1)
        self.assertEqual([e["type"] for e in events], ["system", "rate_limit_event", "assistant", "result"])
        self.assertEqual(events[1]["rate_limit_info"]["status"], "rejected")
        self.assertEqual(events[2]["error"], "rate_limit")
        self.assertEqual((events[3]["is_error"], events[3]["api_error_status"]), (True, 429))
        self.assertEqual(events[3]["result"], "You've hit your 5-hour limit · resets 3pm (UTC)")

    def test_gemini_mode(self):
        code, events = self.run_agent("--gemini", "--approval-mode", "default", "--model", "gem-x", prompt="say pong")
        self.assertEqual(code, 0)
        self.assertEqual([e["type"] for e in events], ["init", "message", "result"])
        self.assertEqual((events[0]["model"], events[1]["content"], events[1]["role"]), ("gem-x", "pong", "assistant"))
        stats = events[-1]["stats"]
        self.assertEqual((stats["input_tokens"], stats["cached"], stats["input"]), (500, 50, 450))
        self.assertEqual(list(stats["models"]), ["gem-x"])
        code, events = self.run_agent("--gemini", "--resume", "abc", prompt="SELFTEST: gemini-429")
        self.assertEqual(code, 1)
        self.assertEqual(events[0]["session_id"], "abc")
        self.assertEqual(events[-1]["error"]["type"], "TerminalQuotaError")
        self.assertNotIn("429", events[-1]["error"]["message"])


# ------------------------------------------------------------------ live sessions and approvals (step 3)
class LiveHub:
    """Stands in for the hub during a live session, the way hub/app.py answers log uploads: it
    keeps each turn once (by its number), hands over the messages the runner hasn't written yet,
    and says "end" once End session was pressed. `script(hub, turn_no)` runs when a turn first
    arrives, to post messages or press End. The first upload that carries `fail_turn` fails."""

    def __init__(self, script=None, fail_turn=None) -> None:
        self.lines: list = []
        self.uploads: list = []
        self.turns: dict = {}
        self.turn_uploads = 0
        self.messages: list = []
        self.end = False
        self.finished: dict = {}
        self.script = script
        self.fail_turn = fail_turn
        self.lock = threading.Lock()

    def post(self, text: str) -> None:
        self.messages.append({"id": len(self.messages) + 41, "text": text})

    def call(self, path: str, payload: dict, timeout: float = 30):
        with self.lock:
            if path.endswith("/finish"):
                self.finished = dict(payload)
                return 200, {}
            self.uploads.append(json.loads(json.dumps(payload)))
            numbers = [t["turn_no"] for t in payload.get("turns") or []]
            if self.fail_turn in numbers:
                self.fail_turn = None
                raise OSError("connection reset (on purpose)")
            self.lines.extend(payload["lines"])
            if numbers:
                self.turn_uploads += 1
            for turn in payload.get("turns") or []:
                if turn["turn_no"] not in self.turns:
                    self.turns[turn["turn_no"]] = turn
                    if self.script is not None:
                        self.script(self, turn["turn_no"])
            after = payload.get("after_message") or 0
            closed = payload.get("session_open") is False
            return 200, {"cancel": False, "end": self.end or closed,
                         "messages": [] if closed else [m for m in self.messages if m["id"] > after]}


def run_with(hub, cfg: dict, data: dict, **stub) -> tuple:
    """Like run_job, with a hub of the test's own."""
    started: list = []
    real_popen = subprocess.Popen

    def popen(argv, *args, **kwargs):
        started.append(list(argv))
        return real_popen(argv, *args, **kwargs)

    job = runner.Job(argparse.Namespace(cfg=cfg, hub=hub, **stub), {"options": {"timeout_min": 2}, **data})
    with mock.patch.object(runner.subprocess, "Popen", popen):
        job.run()
    return hub.finished, started


class LiveSessions(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("BULLPEN_TEST_IDLE_SECONDS", None)
        self.cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}})

    def test_messages_then_end_in_the_same_answer(self):
        def script(hub, turn_no):
            if turn_no == 1:
                hub.post("second message")
            elif turn_no == 2:
                hub.post("third message")  # posted just before End session: still answered
                hub.end = True

        hub = LiveHub(script, fail_turn=2)
        finished, started = run_with(hub, self.cfg, {
            "id": 21, "agent": "one", "project": "chat", "autonomy": "readonly", "live": True, "idle_min": 15,
            "prompt": "first message\nwith a second line", "options": {"timeout_min": 2, "live": True, "idle_min": 15}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "Self-test reply 3: third message")
        self.assertEqual({n: t["result"] for n, t in hub.turns.items()},
                         {1: "Self-test reply 1: first message", 2: "Self-test reply 2: second message",
                          3: "Self-test reply 3: third message"})
        self.assertFalse(any(t["is_error"] for t in hub.turns.values()))
        self.assertEqual(finished["usage"]["turns"], 3)
        self.assertEqual(finished["usage"]["own"]["output_tokens"], 600)
        self.assertIn("🧑 You: second message", hub.lines)
        self.assertIn("🧑 You: third message", hub.lines)
        self.assertIn("⏹ Session ended from the dashboard.", hub.lines)
        self.assertFalse(any(ln.startswith("🧑 You: first message") for ln in hub.lines), "the prompt isn't repeated")
        self.assertIs(hub.uploads[-1]["session_open"], False)
        self.assertEqual(hub.uploads[-1]["after_message"], 42)
        resent = [u for u in hub.uploads if 2 in [t["turn_no"] for t in u.get("turns") or []]]
        self.assertEqual(len(resent), 2, "a batch whose upload failed is sent again (the hub keeps it once)")
        # The command line: the live arguments come right after the preset's own.
        self.assertEqual(started[0][2:6], ["--input-format", "stream-json", "--permission-mode", "dontAsk"])

    def test_idle_time_ends_the_session(self):
        os.environ["BULLPEN_TEST_IDLE_SECONDS"] = "1"
        hub = LiveHub()
        began = time.monotonic()
        finished, _ = run_with(hub, self.cfg, {
            "id": 22, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "only message",
            "options": {"timeout_min": 2, "live": True, "idle_min": 1}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "Self-test reply 1: only message")
        self.assertIn("⏹ Session ended after 1 second without a message.", hub.lines)
        self.assertLess(time.monotonic() - began, 20)
        self.assertIs(hub.uploads[-1]["session_open"], False)
        self.assertEqual(runner.live_idle_seconds({"idle_min": 15}), 1.0, "the test setting wins")
        del os.environ["BULLPEN_TEST_IDLE_SECONDS"]
        self.assertEqual(runner.live_idle_seconds({"idle_min": 15}), 900)
        self.assertEqual(runner.live_idle_seconds({}), 900)
        self.assertEqual(runner.duration_words(900), "15 minutes")
        self.assertEqual(runner.duration_words(60), "1 minute")

    def test_the_time_limit_between_turns_ends_the_session_normally(self):
        hub = LiveHub()
        finished, _ = run_with(hub, self.cfg, {
            "id": 23, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "hello there",
            "options": {"timeout_min": 0.05, "live": True, "idle_min": 15}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "Self-test reply 1: hello there")
        self.assertIn("⏹ Session ended at the job's time limit.", hub.lines)

    def test_the_time_limit_during_a_turn_stops_it(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": {"SELFTEST_DELAY": "20"}}})
        hub = LiveHub()
        began = time.monotonic()
        finished, _ = run_with(hub, cfg, {
            "id": 24, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "slow",
            "options": {"timeout_min": 0.03, "live": True}})
        self.assertEqual(finished["status"], "error")
        self.assertIn("time limit", finished["error"])
        self.assertLess(time.monotonic() - began, 15)

    def test_messages_after_the_session_closed_are_left_for_the_hub(self):
        job = runner.Job(argparse.Namespace(cfg=self.cfg, hub=LocalHub()), {"id": 25, "agent": "one"})
        proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE,
                                text=True)

        def cleanup() -> None:
            try:
                proc.wait(timeout=10)  # it ends when the session closes its input
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            with contextlib.suppress(OSError, ValueError):
                proc.stdin.close()

        self.addCleanup(cleanup)
        live = runner.LiveSession(job, proc, runner.ClaudeStreamParser(), 60)
        live.start("the job's prompt")
        live.offer([{"id": 5, "text": "b"}, {"id": 4, "text": "a"}, {"id": 4, "text": "a"}, {"id": "x"}, "junk"])
        self.assertEqual([m["id"] for m in live.queue], [4, 5], "in id order, each once")
        self.assertIsNone(live.tick(), "a turn is still running (the first prompt's)")
        live.on_result()
        live.tick()
        self.assertEqual((live.received_max, live.turn_active), (4, True))
        live.close("Session ended from the dashboard.")
        live.offer([{"id": 6, "text": "late"}])
        self.assertEqual([m["id"] for m in live.queue], [5], "nothing is taken after the close")
        self.assertEqual(live.received_max, 4, "only messages written to the agent count as delivered")
        self.assertIn("⏹ Session ended from the dashboard.", job.lines)
        self.assertIn("🧑 You: a", job.lines)

    def test_an_agent_that_cant_keep_a_session_open_answers_once(self):
        cfg = make_cfg(self.tmp, {"gem": {"preset": "gemini", "command": sys.executable,
                                          "args": [str(ROOT / "runner" / "selftest_agent.py"), "--gemini"],
                                          "env": self.selftest_env}})
        hub = LiveHub()
        finished, started = run_with(hub, cfg, {"id": 26, "agent": "gem", "project": "chat", "autonomy": "readonly",
                                                "prompt": "say pong", "options": {"timeout_min": 2, "live": True}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertNotIn("--input-format", started[0])
        self.assertIn("⚠ This agent can't keep a session open, so it answers once.", hub.lines)
        self.assertNotIn("session_open", hub.uploads[-1], "an ordinary job's uploads are as they were")

    def test_user_message_line(self):
        line = runner.user_message_line("héllo\nworld")
        self.assertTrue(line.endswith(b"\n") and line.count(b"\n") == 1)
        self.assertEqual(json.loads(line)["message"]["content"][0]["text"], "héllo\nworld")
        line.decode("ascii")  # plain ASCII, whatever the text


class Approvals(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.cfg_with = lambda fake, **agent: make_cfg(
            self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env, **agent}}, hub_url=fake.url)

    def test_an_ask_job_asks_through_the_permission_prompt_tool(self):
        fake = FakeJobTools(tools=("job_info", "approve"))
        self.addCleanup(fake.close)
        finished, lines, started = run_job(self.cfg_with(fake), {
            "id": 31, "agent": "one", "project": "chat", "autonomy": "ask", "prompt": "SELFTEST: ask Bash npm test",
            "job_token": TOKEN, "job_tools": ["job_info", "approve"], "approval_timeout_s": 600})
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue(finished["result"].startswith("approval: allow (npm test)\n"), finished["result"])
        config = str(runner.JOBS_DIR / "31" / "mcp.json")
        self.assertEqual(started[0][2:], ["--permission-mode", "acceptEdits", "--permission-prompt-tool",
                                          "mcp__bullpenjob__approve", "--allowedTools", "mcp__bullpenjob__job_info",
                                          "--mcp-config", config])
        asked = [c for c in fake.calls if c["tool"] == "approve"][0]
        self.assertEqual(asked["arguments"]["tool_name"], "Bash")
        self.assertEqual(asked["arguments"]["input"], {"command": "npm test", "description": "selftest"})
        self.assertTrue(asked["arguments"]["tool_use_id"].startswith("toolu_selftest_"))
        self.assertIn("progressToken", asked["meta"], "progress keeps a long wait alive")

    def test_claudes_own_ask_level(self):
        spec = runner.agent_spec({"agents": {"c": {"preset": "claude"}}}, "c")
        argv = runner.add_allowed_tools(spec["autonomy"]["ask"], "--allowedTools",
                                        runner.allowed_job_tool_names(["job_info", "approve"]))
        self.assertEqual(argv, ["--permission-mode", "acceptEdits", "--allowedTools", "mcp__bullpenjob__job_info",
                                "mcp__bullpen", "--permission-prompt-tool", "mcp__bullpenjob__approve"])

    def test_a_deny_reaches_the_agent(self):
        fake = FakeJobTools(tools=("job_info", "approve"),
                            decision={"behavior": "deny", "message": "The owner denied this: not now"})
        self.addCleanup(fake.close)
        finished, _, _ = run_job(self.cfg_with(fake), {
            "id": 32, "agent": "one", "project": "chat", "autonomy": "ask",
            "prompt": "SELFTEST: ask Bash rm -rf build\nSELFTEST: ask-big 30",
            "job_token": TOKEN, "job_tools": ["job_info", "approve"]})
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"].splitlines()[:2], ["approval: deny (The owner denied this: not now)"] * 2)
        big = [c for c in fake.calls if c["tool"] == "approve"][1]
        self.assertEqual(big["arguments"]["input"]["command"], "x" * 30)

    def test_an_agent_that_couldnt_connect_is_stopped_at_once(self):
        # The runner's own check gets through; the agent's connection is then refused.
        fake = FakeJobTools(tools=("job_info", "approve"), lists_ok=1)
        self.addCleanup(fake.close)
        finished, lines, _ = run_job(self.cfg_with(fake, env={"SELFTEST_DELAY": "20"}), {
            "id": 33, "agent": "one", "project": "chat", "autonomy": "ask", "prompt": "hello",
            "job_token": TOKEN, "job_tools": ["job_info", "approve"]})
        self.assertEqual(finished["status"], "error")
        self.assertIn("can't reach the hub's job tools at " + fake.url + "/mcp/job (Claude Code couldn't connect",
                      finished["error"])
        self.assertIn("couldn't ask you before commands", finished["error"])

    def test_a_missing_prompt_tool_on_stderr_gives_the_same_message(self):
        fake = FakeJobTools(tools=("job_info", "approve"))
        self.addCleanup(fake.close)
        script = ("import json, sys, time\n"
                  "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's1', "
                  "'mcp_servers': [{'name': 'bullpenjob', 'status': 'connected'}]}), flush=True)\n"
                  "print('Error: MCP tool mcp__bullpenjob__approve (passed via --permission-prompt-tool) not found. "
                  "Available MCP tools: none', file=sys.stderr, flush=True)\n"
                  "time.sleep(30)\n")
        began = time.monotonic()
        finished, _, _ = run_job(self.cfg_with(fake, command=sys.executable, args=["-c", script]), {
            "id": 34, "agent": "one", "project": "chat", "autonomy": "ask", "prompt": "hello",
            "job_token": TOKEN, "job_tools": ["job_info", "approve"]})
        self.assertEqual(finished["status"], "error")
        self.assertIn("can't reach the hub's job tools", finished["error"])
        self.assertIn("couldn't find the approve tool", finished["error"])
        self.assertLess(time.monotonic() - began, 20, "stopped at once, not when the agent gives up")

    def test_only_the_first_init_counts(self):
        parser = runner.ClaudeStreamParser()
        parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s",
                                "mcp_servers": [{"name": "bullpenjob", "status": "connected"}]}))
        parser.feed(json.dumps({"type": "system", "subtype": "init", "session_id": "s",
                                "mcp_servers": [{"name": "bullpenjob", "status": "failed"}]}))
        self.assertEqual(parser.mcp_servers, {"bullpenjob": "connected"})


class SelftestStep3(TempDirCase):
    def run_agent(self, *args: str, stdin: str = "", env: dict | None = None) -> tuple:
        full = dict(os.environ, SELFTEST_DELAY="0", PYTHONIOENCODING="utf-8", BULLPEN_HOME=self.tmp, **(env or {}))
        res = subprocess.run([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), *args], input=stdin,
                             capture_output=True, text=True, encoding="utf-8", cwd=self.tmp, env=full, timeout=60)
        return res.returncode, res.stdout, res.stderr

    def test_live_mode(self):
        msgs = [json.dumps({"type": "user", "message": {"role": "user", "content": [
                    {"type": "text", "text": "Hello\nthere"}, {"type": "text", "text": " friend"}]}}),
                "not json",
                json.dumps({"type": "user", "message": {"role": "user", "content": "second one"}})]
        code, out, _ = self.run_agent("--input-format", "stream-json", "--resume", "live-1", stdin="\n".join(msgs) + "\n")
        self.assertEqual(code, 0)
        events = [json.loads(ln) for ln in out.splitlines()]
        results = [e for e in events if e["type"] == "result"]
        self.assertEqual([r["result"] for r in results], ["Self-test reply 1: Hello", "Self-test reply 2: second one"])
        self.assertEqual(sum(1 for e in events if e.get("subtype") == "init"), 2, "one init per turn, as Claude Code")
        self.assertEqual(results[-1]["modelUsage"]["selftest-1"]["outputTokens"], 400, "cumulative")
        self.assertEqual(results[-1]["model_usage_since_last_result"]["selftest-1"]["outputTokens"], 200)
        saved = json.loads((Path(self.tmp) / "selftest" / "sessions" / "live-1.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["selftest-1"]["outputTokens"], 400, "totals saved when the input closes")

    def test_ask_needs_the_prompt_tool_and_the_job_tools(self):
        code, out, _ = self.run_agent(stdin="SELFTEST: ask Bash ls")
        self.assertTrue(json.loads(out.splitlines()[-1])["result"].startswith(
            "approval: not available (no permission prompt tool)"))
        code, out, _ = self.run_agent("--permission-prompt-tool", "mcp__bullpenjob__approve", stdin="SELFTEST: ask Bash ls")
        self.assertTrue(json.loads(out.splitlines()[-1])["result"].startswith("approval: not available (no job tools)"))

    @unittest.skipUnless(POSIX, "signals as on Linux and macOS (Windows stops it with taskkill: see "
                                "RemoteControlTests.test_start_and_stop and test_windows_job_object)")
    def test_remote_control_mode(self):
        proc = subprocess.Popen([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), "remote-control",
                                 "--name", "build-box myapp (one)", "--spawn", "same-dir"], stdout=subprocess.PIPE,
                                text=True, env=dict(os.environ, BULLPEN_HOME=self.tmp, PYTHONIOENCODING="utf-8"))
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        first = proc.stdout.readline()
        self.assertRegex(first, r"^Remote Control is ready · https://claude\.ai/code/selftest-[0-9a-f]{8}$")
        pid_file = Path(self.tmp) / "selftest" / "rc-build-box-myapp-one.pid"
        self.assertEqual(pid_file.read_text(encoding="utf-8"), str(proc.pid))
        proc.terminate()
        self.assertEqual(proc.wait(timeout=10), 0)
        proc.stdout.close()
        self.assertFalse(pid_file.exists())

    def test_remote_control_failures(self):
        for mode, words in (("untrusted", "Workspace not trusted"), ("apikey", "requires claude.ai subscription"),
                            ("token", "full-scope login token")):
            code, out, _ = self.run_agent("remote-control", "--name", "x", env={"SELFTEST_RC": mode})
            self.assertEqual(code, 1)
            self.assertIn(words, out)
        self.assertEqual(selftest_agent.rc_slug("Build-Box  myapp (two)"), "build-box-myapp-two")


# ------------------------------------------------------------------ Remote Control (step 3)
class RemoteControlTests(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for key in AUTH_VARS + ("SELFTEST_RC",):
            os.environ.pop(key, None)
        self.project = Path(self.tmp) / "myapp"
        self.project.mkdir()
        agents = {"one": {"preset": "selftest"},
                  "apikey": {"preset": "selftest", "env": {"ANTHROPIC_API_KEY": "sk-unit-apikey-0123456789"}},
                  "fresh": {"preset": "selftest", "env": {"SELFTEST_RC": "untrusted"}},
                  "tok": {"preset": "selftest", "env": {"SELFTEST_RC": "token"}},
                  "slow": {"preset": "selftest", "env": {"SELFTEST_RC": "nourl"}}}
        self.cfg = make_cfg(self.tmp, agents)
        self.cfg["projects"]["myapp"] = str(self.project)
        self.cfg["allow_remote_control"] = True
        self.rc = runner.RemoteControl(argparse.Namespace(cfg=self.cfg, name=lambda: "build-box"))
        self.rc.state_file = Path(self.tmp) / "remote-control.json"
        self.addCleanup(self.rc.stop_all)

    def pid_file(self, agent: str) -> Path:
        return runner.BASE_DIR / "selftest" / f"rc-build-box-myapp-{agent}.pid"

    def test_start_and_stop(self):
        git = runner.shutil.which("git")
        if git:
            subprocess.run([git, "init", "-q", str(self.project)], check=True)
        out = self.rc.start("one", "myapp")
        self.assertEqual(out["status"], "done", out)
        entry = self.rc.view()[0]
        self.assertEqual((entry["agent"], entry["project"], entry["state"], entry["name"]),
                         ("one", "myapp", "running", "build-box myapp (one)"))
        self.assertEqual(entry["spawn"], "worktree" if git else "same-dir")
        self.assertRegex(entry["url"], r"^https://claude\.ai/code/selftest-[0-9a-f]{8}$")
        self.assertEqual(entry["stops_at"] - entry["since"], 8 * 3600)
        self.assertIn("Remote Control is running for one in myapp as 'build-box myapp (one)'", out["result"])
        self.assertIn("(after 8 hours)", out["result"])
        self.assertTrue(out["result"].endswith(entry["url"]))
        proc = self.rc.servers[("one", "myapp")]["proc"]
        saved = read_json(self.rc.state_file)
        self.assertEqual(saved["servers"], [{"agent": "one", "project": "myapp", "pid": proc.pid,
                                             "started": entry["since"]}])
        again = self.rc.start("one", "myapp")
        self.assertEqual(again["status"], "error")
        self.assertIn("already running", again["error"])
        began = time.monotonic()
        stopped = self.rc.stop("one", "myapp", why="stopped from the dashboard")
        self.assertLess(time.monotonic() - began, runner.RC_STOP_GRACE, "Stop doesn't wait out the grace period")
        self.assertEqual(stopped["status"], "done")
        self.assertIsNotNone(proc.poll(), "the process is gone")
        self.assertEqual((self.rc.view()[0]["state"], self.rc.view()[0]["note"]), ("stopped", "stopped from the dashboard"))
        if POSIX:
            self.assertFalse(self.pid_file("one").exists(), "it was asked to stop, not killed")
        else:
            self.assertTrue(self.pid_file("one").exists(), "Windows ends it at once with taskkill, so it can't tidy up")
        self.assertEqual(read_json(self.rc.state_file)["servers"], [])
        self.assertEqual(self.rc.stop("one", "myapp")["status"], "done", "Stop takes an ended one off the page")
        self.assertEqual(self.rc.view(), [])
        self.assertEqual(self.rc.stop("one", "myapp")["status"], "error")

    def test_the_time_limit_and_the_setting(self):
        self.assertEqual(self.rc.start("one", "myapp")["status"], "done")
        self.rc.enforce()
        self.assertEqual(self.rc.view()[0]["state"], "running", "not yet")
        self.rc.clock = lambda: time.time() + 8 * 3600 + 5
        self.rc.enforce()
        self.assertEqual((self.rc.view()[0]["state"], self.rc.view()[0]["note"]), ("stopped", "stopped after 8 hours"))
        self.rc.clock = time.time
        self.assertEqual(self.rc.start("one", "myapp")["status"], "done")
        self.cfg["allow_remote_control"] = False
        self.rc.enforce()
        self.assertEqual(self.rc.view()[0]["note"], "Remote Control was turned off in runner.json")
        self.assertIn("allow_remote_control", self.rc.start("one", "myapp")["error"])
        self.rc.clock = lambda: time.time() + runner.RC_KEEP_ENDED + 60
        self.rc.enforce()
        self.assertEqual(self.rc.view(), [], "ended servers leave the machine page after a while")
        self.cfg["remote_control_max_hours"] = 0.1
        self.assertEqual(runner.rc_max_hours(self.cfg), 1.0)
        self.cfg["remote_control_max_hours"] = 500
        self.assertEqual(runner.rc_max_hours(self.cfg), 72.0)

    def test_a_server_that_shows_no_address(self):
        with mock.patch.object(runner, "RC_URL_WAIT", 1):
            out = self.rc.start("slow", "myapp")
        self.assertEqual(out["status"], "done", out)
        self.assertIn("It hasn't shown its address yet; its last lines:\nStarting Remote Control…", out["result"])
        entry = self.rc.view()[0]
        self.assertEqual((entry["state"], entry["url"], entry["note"]),
                         ("running", None, "started, but hasn't shown its address"))

    def test_failures_get_a_clear_message(self):
        out = self.rc.start("fresh", "myapp")
        self.assertEqual(out["status"], "error")
        self.assertIn("one-time answer in a terminal on build-box", out["error"])
        self.assertIn("selftest remote-control", out["error"])
        self.assertIn("Workspace not trusted", out["error"])
        self.assertLessEqual(len(out["error"].split("Its last lines:\n")[1].splitlines()), 5)
        self.assertEqual(self.rc.view()[0]["state"], "failed")
        out = self.rc.start("tok", "myapp")
        self.assertTrue(out["error"].startswith("tok on build-box has no full claude.ai login that Remote Control "
                                                "can use"), out["error"])
        self.assertNotIn("tok signs in with", out["error"], "its env has no token, so that isn't said for sure")
        out = self.rc.start("apikey", "myapp")
        self.assertIn("signs in with an API key", out["error"])
        self.assertNotIn(("apikey", "myapp"), self.rc.servers, "refused before anything was started")

    def test_the_most_at_once(self):
        self.cfg["max_remote_control"] = 1
        self.cfg["projects"]["other"] = self.tmp
        self.assertEqual(self.rc.start("one", "myapp")["status"], "done")
        out = self.rc.start("one", "other")
        self.assertIn("max_remote_control", out["error"])

    def test_messages_show_five_lines_without_addresses(self):
        entry = {"tail": [f"line {n}" for n in range(8)] + ["signed in as owner@example.com"]}
        tail = self.rc._tail(entry)
        self.assertEqual(len(tail.splitlines()), 5)
        self.assertNotIn("example.com", tail)

    def test_the_servers_output_is_masked_as_it_is_read(self):
        script = ("import sys; print('key sk-live-abcdefghijklmnopqrst for owner@example.com'); "
                  "print('env zz-envsecret-9876543210'); sys.exit(1)")
        self.cfg["agents"]["leak"] = {"preset": "selftest", "command": sys.executable,
                                      "env": {"MY_SERVICE_TOKEN": "zz-envsecret-9876543210"},
                                      "remote_control": ["-c", script]}
        out = self.rc.start("leak", "myapp")
        self.assertEqual(out["status"], "error", out)
        tail = self.rc.servers[("leak", "myapp")]["tail"]
        self.assertEqual(tail, ["key *** for owner@example.com", "env ***"],
                         "pattern secrets and the agent's own env secrets are masked before they are kept")
        for secret in ("sk-live", "zz-envsecret"):
            self.assertNotIn(secret, out["error"])
        self.assertIn("key *** for …@…", out["error"])
        self.assertNotIn("example.com", out["error"])

    def test_stop_all_remembers_for_an_update_and_restore_starts_them_again(self):
        self.assertEqual(self.rc.start("one", "myapp")["status"], "done")
        since = self.rc.view()[0]["since"]
        self.rc.stop_all(remember=True, why="the runner is updating")
        saved = read_json(self.rc.state_file)
        self.assertEqual(saved["restart_after_update"], [{"agent": "one", "project": "myapp", "started": since}])
        later = runner.RemoteControl(argparse.Namespace(cfg=self.cfg, name=lambda: "build-box"))
        later.state_file = self.rc.state_file
        self.addCleanup(later.stop_all)
        later.restore()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (later.view() and later.view()[0]["state"] == "running"):
            time.sleep(0.1)
        self.assertEqual(later.view()[0]["state"], "running")
        self.assertEqual(later.view()[0]["since"], since, "the time limit still counts from the first start")
        self.assertEqual(read_json(later.state_file)["restart_after_update"], [])

    def test_restore_after_a_long_break_or_with_the_setting_off_starts_nothing(self):
        for at, allowed in ((time.time() - runner.RC_RESTART_WITHIN - 60, True), (time.time(), False)):
            self.cfg["allow_remote_control"] = allowed
            self.rc.state_file.write_text(json.dumps({"servers": [], "at": at, "restart_after_update": [
                {"agent": "one", "project": "myapp", "started": at}]}), encoding="utf-8")
            self.rc.restore()
            time.sleep(0.5)
            self.assertEqual(self.rc.view(), [])

    def restart_state(self) -> None:
        """The state file as an update leaves it: one server to start again, saved just now."""
        now = time.time()
        self.rc.state_file.write_text(json.dumps({"servers": [], "at": now, "restart_after_update": [
            {"agent": "one", "project": "myapp", "started": now}]}), encoding="utf-8")

    @contextlib.contextmanager
    def windows_has_it_open(self, refusals: int):
        """Act as Windows does while another thread or program has the state file open: the first
        `refusals` reads of it are refused. Sleeps are skipped to keep the test fast. Yields the
        list of reads of the state file."""
        reads: list = []
        real_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path == self.rc.state_file:
                reads.append(path)
                if len(reads) <= refusals:
                    raise PermissionError(13, "Permission denied", str(path))
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(runner, "IS_WINDOWS", True), mock.patch.object(runner.time, "sleep"), \
                mock.patch.object(Path, "read_text", read_text):
            yield reads

    def test_restore_retries_a_state_file_windows_has_open(self):
        self.restart_state()
        # The lock keeps the server's start (a thread restore() begins) waiting until the patches
        # are undone, so it starts the way this system starts it, not the way Windows does.
        with self.rc.lock, self.windows_has_it_open(refusals=2) as reads:
            self.rc.restore()
        self.assertEqual(len(reads), 3, "two refused reads, then one that worked")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not (self.rc.view() and self.rc.view()[0]["state"] == "running"):
            time.sleep(0.1)
        self.assertEqual([e["state"] for e in self.rc.view()], ["running"])

    def test_restore_logs_a_state_file_it_cannot_read(self):
        self.restart_state()
        with self.windows_has_it_open(refusals=10**6) as reads, self.assertLogs(runner.log, "WARNING") as logs:
            self.rc.restore()
        time.sleep(0.5)
        self.assertEqual(self.rc.view(), [])
        self.assertEqual(len(reads), 20, "tried as often as write_private tries")
        self.assertEqual(len(logs.records), 1, logs.output)
        said = logs.records[0].getMessage()
        self.assertTrue(said.startswith(f"remote control: couldn't read {self.rc.state_file} ("), said)
        self.assertTrue(said.endswith("); nothing is restarted"), said)

        self.rc.state_file.write_text('{"servers": [', encoding="utf-8")
        with self.assertLogs(runner.log, "WARNING") as logs:
            self.rc.restore()
        self.assertEqual(len(logs.records), 1, logs.output)
        self.assertTrue(logs.records[0].getMessage().endswith("; nothing is restarted"), logs.output)

        self.rc.state_file.unlink()
        with self.windows_has_it_open(refusals=0) as reads, self.assertLogs(runner.log, "WARNING") as logs:
            self.rc.restore()
            runner.log.warning("restore is done")  # assertLogs needs one line (assertNoLogs is Python 3.10+)
        self.assertEqual(logs.output, ["WARNING:runner:restore is done"], "a missing file is no news")
        self.assertEqual(len(reads), 1, "a missing file isn't tried again")

    def test_read_private_raises_at_once_off_windows(self):
        read_text = mock.Mock(side_effect=PermissionError(13, "Permission denied"))
        with mock.patch.object(runner, "IS_WINDOWS", False), mock.patch.object(Path, "read_text", read_text):
            with self.assertRaises(PermissionError):
                runner.read_private(self.rc.state_file)
        self.assertEqual(read_text.call_count, 1)

    @unittest.skipUnless(POSIX, "orphans are cleaned up by process id on Linux and macOS")
    def test_restore_stops_a_server_a_crashed_runner_left(self):
        orphan = subprocess.Popen([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), "remote-control",
                                   "--name", "build-box myapp (one)"], stdout=subprocess.DEVNULL,
                                  start_new_session=True)
        self.addCleanup(lambda: orphan.poll() is None and orphan.kill())
        time.sleep(0.5)
        self.rc.state_file.write_text(json.dumps({"servers": [{"agent": "one", "project": "myapp", "pid": orphan.pid,
                                                               "started": time.time()}], "at": time.time(),
                                                  "restart_after_update": []}), encoding="utf-8")
        self.rc.restore()
        self.assertEqual(orphan.wait(timeout=10), 0)

    def test_admin_actions(self):
        stub = argparse.Namespace(cfg=self.cfg, hub=LocalHub(), name=lambda: "build-box", rc=self.rc)
        start = {"id": 51, "kind": "admin", "options": {"action": "rc.start", "params": {"agent": "one",
                                                                                        "project": "myapp"}}}
        job = runner.Job(stub, start)
        job.run()
        self.assertEqual(stub.hub.finished["status"], "done", stub.hub.finished)
        self.assertTrue(any(ln.startswith("▶ Starting Remote Control for one in myapp") for ln in stub.hub.lines))
        self.cfg["allow_remote_control"] = False
        job = runner.Job(stub, {**start, "id": 52})
        job.run()
        self.assertIn("allow_remote_control", stub.hub.finished["error"])
        job = runner.Job(stub, {"id": 53, "kind": "admin", "options": {"action": "rc.stop", "params": {
            "agent": "one", "project": "myapp"}}})
        job.run()
        self.assertEqual(stub.hub.finished["status"], "done", "Stop works whatever runner.json says")
        self.assertEqual(self.rc.view()[0]["state"], "stopped")

    def test_restart_stops_remote_control_first(self):
        order: list = []
        rc = mock.Mock()
        rc.stop_all.side_effect = lambda **kw: order.append(("stop_all", kw))
        stub = argparse.Namespace(rc=rc, sync_lock=threading.Lock())

        class Replaced(Exception):
            """Where the real runner would be replaced by the new one."""

        def end(what: str):
            def record(*args, **kwargs):
                order.append(what)
                raise Replaced()
            return record

        with mock.patch.object(runner.os, "execv", side_effect=end("execv")), \
                mock.patch.object(runner.subprocess, "Popen", side_effect=lambda *a, **k: order.append("Popen")), \
                mock.patch.object(runner.subprocess, "run", return_value=mock.Mock(returncode=1)), \
                mock.patch.object(runner.os, "_exit", side_effect=end("_exit")):
            with self.assertRaises(Replaced):
                runner.Runner.restart(stub)
        self.assertEqual(order[0], ("stop_all", {"remember": True, "why": "the runner is updating"}))
        self.assertEqual(order[1:], ["execv"] if POSIX else ["Popen", "_exit"])

    def test_heartbeat(self):
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps(self.cfg), encoding="utf-8")
        real = runner.Runner(path)
        info = real.heartbeat_payload()["info"]
        self.assertEqual((info["allow_remote_control"], info["remote_control_max_hours"], info["remote_control"]),
                         (True, 8, []))
        real.rc = mock.Mock()
        real.rc.view.return_value = []
        with mock.patch.object(real.hub, "call", return_value=(401, {"detail": "no"})):
            for _ in range(5):
                real.send_heartbeat()
        real.rc.stop_all.assert_called_once_with(why="the hub no longer accepts this machine")


# ------------------------------------------------------------------ tags and subtasks (step 4)
DELEGATION_TOOLS = ("job_info", "list_machines", "delegate_task", "task_status", "cancel_task")


class InitTags(unittest.TestCase):
    def ask(self, answer: str, cfg=None) -> tuple:
        out = io.StringIO()
        with mock.patch("builtins.input", return_value=answer) as asked, contextlib.redirect_stdout(out), \
                mock.patch.object(runner, "_input_closed", False), mock.patch.object(runner, "PLATFORM", "linux"):
            tags = runner.ask_tags(cfg or {})
        return tags, out.getvalue(), asked.call_args[0][0]

    def test_words_are_cleaned_and_the_rest_explained(self):
        tags, out, question = self.ask("Kali, security-tools, tag!, -x, linux, " + "x" * 33)
        self.assertEqual(tags, ["kali", "security-tools"])
        self.assertIn("blank for none", question)
        self.assertIn("  skipped 'tag!': tags use letters, digits, '.', '_' and '-'", out)
        self.assertIn("  skipped '-x': tags use letters, digits, '.', '_' and '-'", out)
        self.assertIn(f"  skipped '{'x' * 33}': a tag has at most 32 characters", out)
        self.assertIn("  skipped 'linux': every machine reports its own system (linux) by itself", out)

    def test_enter_keeps_the_tags_and_none_removes_them(self):
        tags, _, question = self.ask("", {"tags": ["gpu", "pool"]})
        self.assertEqual(tags, ["gpu", "pool"])
        self.assertIn("'none' to remove them", question)
        self.assertTrue(question.endswith("[gpu, pool]: "), question)
        self.assertEqual(self.ask("None", {"tags": ["gpu"]})[0], [])
        self.assertEqual(self.ask("", {"tags": "gpu pool"})[0], ["gpu", "pool"], "a string in runner.json works too")
        self.assertEqual(self.ask("")[0], [])

    def test_at_most_fifteen(self):
        tags, out, _ = self.ask(", ".join(f"t{i}" for i in range(18)))
        self.assertEqual(tags, [f"t{i}" for i in range(15)])
        self.assertIn("  skipped 't15': at most 15 tags (the operating-system tag makes 16)", out)
        tags, out, _ = self.ask("linux, " + ", ".join(f"t{i}" for i in range(15)))
        self.assertEqual(len(tags), 15, "a platform word doesn't take one of the 15 places")
        self.assertNotIn("at most", out)

    def test_every_kept_tag_is_reported(self):
        words = [f"t{i:02}" for i in range(16)]
        kept, skipped = runner.clean_tags(words)
        self.assertEqual((kept, skipped), (words[:15], ["t15"]))
        with mock.patch.object(runner, "PLATFORM", "linux"):
            self.assertEqual(runner.heartbeat_tags({"tags": words}), sorted(words[:15] + ["linux"]))
            self.assertEqual(runner.heartbeat_tags({"tags": ["macos"] + words[:15]}), sorted(words[:15] + ["linux"]))
        path = Path(tempfile.mkdtemp(prefix="bullpen-unit-")) / "runner.json"
        path.write_text(json.dumps({"name": "build-box", "hub_url": "http://127.0.0.1:9", "agents": {},
                                    "tags": words + ["Bad!"]}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.cmd_list(argparse.Namespace(config=str(path)))
        self.assertIn("  left out of 'tags': t15, Bad! (at most 15 words", out.getvalue(), "runner.py list says so")


# ------------------------------------------------------------------ platform credits
CREDIT_SAID = ('API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Your credit '
               'balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase '
               'credits."}}')


class CreditSignals(unittest.TestCase):
    def parser(self, api_key=True):
        parser = runner.ClaudeStreamParser()
        parser.api_key = api_key
        return parser

    def failed(self, said, status=400):
        return json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": said,
                           "api_error_status": status, "session_id": "s"})

    def test_the_api_refusing_for_lack_of_credit(self):
        parser = self.parser()
        parser.feed(self.failed(CREDIT_SAID))
        self.assertEqual(parser.error_kind, "credits")
        self.assertTrue(parser.error.startswith("Platform credits used up"))
        self.assertIn("Your credit balance is too low", parser.error)
        parser = self.parser()
        parser.feed(json.dumps({"type": "assistant", "error": "billing_error", "session_id": "s",
                                "message": {"content": [{"type": "text", "text": "Credit balance is too low"}]}}))
        parser.feed(self.failed("Execution error", status=0))
        self.assertEqual(parser.error_kind, "credits", "the assistant's error message said so")
        parser = self.parser()
        parser.feed(self.failed("CREDIT BALANCE TOO LOW"))
        self.assertEqual(parser.error_kind, "credits", "matched loosely")

    def test_only_for_an_agent_with_an_api_key(self):
        parser = self.parser(api_key=False)
        parser.feed(self.failed(CREDIT_SAID))
        self.assertIsNone(parser.error_kind)
        parser.note_stderr(CREDIT_SAID)
        self.assertIsNone(parser.exit_error())

    def test_on_stderr_when_it_exits_without_a_result(self):
        parser = self.parser()
        parser.note_stderr("some other line")
        parser.note_stderr(CREDIT_SAID)
        self.assertTrue((parser.exit_error() or "").startswith("Platform credits used up"))
        self.assertEqual(parser.error_kind, "credits")
        self.assertIsNone(self.parser().exit_error())

    def test_other_errors_stay_what_they_were(self):
        parser = self.parser()
        parser.feed(self.failed("You've hit your 5-hour limit · resets 3pm (UTC)", status=429))
        self.assertEqual(parser.error_kind, "claude_limit")
        parser = self.parser()
        parser.feed(self.failed("Invalid API key · Please run /login", status=401))
        self.assertIsNone(parser.error_kind)
        self.assertIn("credits", runner.ERROR_KINDS)


class CreditsAgent(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(clean_auth_env().stop)
        self.agents = {
            "sub": {"preset": "selftest", "env": self.selftest_env},
            "paid": {"preset": "selftest", "account": "Work",
                     "env": dict(self.selftest_env, ANTHROPIC_API_KEY="sk-unit-credits-0123456789")},
            "token": {"preset": "claude", "command": sys.executable,
                      "env": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-token-0123456789"}},
            "gem": {"preset": "gemini", "command": sys.executable, "env": {"GEMINI_API_KEY": "fake-gemini-key"}},
            "text": {"preset": "text", "command": sys.executable, "env": {"ANTHROPIC_API_KEY": "sk-unit-0123456789"}},
        }

    def cfg(self, value):
        cfg = make_cfg(self.tmp, self.agents)
        if value is not ...:
            cfg["credits_agent"] = value
        return cfg

    def test_only_a_claude_agent_with_an_api_key(self):
        self.assertEqual(runner.credits_agent(self.cfg("paid")), ("paid", ""))
        self.assertEqual(runner.credits_agent(self.cfg(...)), (None, ""))
        self.assertEqual(runner.credits_agent(self.cfg("")), (None, ""))
        for value, words in (("sub", "doesn't sign in with an API key"), ("token", "doesn't sign in with an API key"),
                             ("gem", "isn't a Claude agent"), ("text", "isn't a Claude agent"),
                             ("nope", "isn't one of this machine's agents"), (7, "isn't one of this machine's agents")):
            name, why = runner.credits_agent(self.cfg(value))
            self.assertIsNone(name, value)
            self.assertIn(words, why)
            self.assertIn("it is ignored", why)

    def test_heartbeat_list_and_log(self):
        path = Path(self.tmp) / "runner.json"
        for value, reported in (("paid", "paid"), ("sub", None), (..., None)):
            path.write_text(json.dumps(self.cfg(value)), encoding="utf-8")
            r = runner.Runner(path)
            self.assertEqual(r.heartbeat_payload()["info"]["credits_agent"], reported)
            with self.assertLogs("runner", level="INFO") as logs:
                r.note_credits_agent()
                runner.log.info("end")
            if value == "sub":
                self.assertTrue(any("WARNING" in ln and "doesn't sign in with an API key" in ln for ln in logs.output))
            if value == "paid":
                self.assertTrue(any("credits agent: paid" in ln for ln in logs.output))
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                runner.cmd_list(argparse.Namespace(config=str(path)))
            self.assertIn("Credits agent: paid" if value == "paid" else "Credits agent: none", out.getvalue())

    def test_a_job_reports_credits_used_up(self):
        cfg = self.cfg("paid")
        finished, _, _ = run_job(cfg, {"id": 30, "agent": "paid", "project": "chat", "prompt": "SELFTEST: credits-low"})
        self.assertEqual((finished["status"], finished["error_kind"]), ("error", "credits"), finished)
        self.assertTrue(finished["error"].startswith("Platform credits used up"))
        finished, _, _ = run_job(cfg, {"id": 31, "agent": "sub", "project": "chat", "prompt": "SELFTEST: credits-low"})
        self.assertEqual(finished["status"], "done", "a subscription agent has no platform credits to run out of")
        finished, _, _ = run_job(cfg, {"id": 32, "agent": "paid", "project": "chat",
                                       "prompt": "SELFTEST: limit-rejected\nhi"})
        self.assertEqual(finished["status"], "done", "an API key has no plan limit")

    def test_init_asks_only_when_there_is_an_api_key_agent(self):
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps({**self.cfg(...), "hub_url": "http://192.168.100.20:8080"}), encoding="utf-8")
        asked: list = []
        answers = ["", "", "", "", "", "", "", "", "y", "paid", "n"]

        def answer(question: str) -> str:
            asked.append(question)
            return answers.pop(0) if answers else ""

        with mock.patch("builtins.input", side_effect=answer), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(runner, "_input_closed", False), \
                mock.patch.object(runner.shutil, "which", return_value=None):
            runner.cmd_init(argparse.Namespace(config=str(path), skip_hub=True))
        question = next(q for q in asked if q.startswith("Credits agent"))
        self.assertIn("(paid; blank for none)", question)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["credits_agent"], "paid")
        self.assertEqual(runner.credits_agent(json.loads(path.read_text(encoding="utf-8"))), ("paid", ""))


class InitQuestions(TempDirCase):
    def run_init(self, answers: list) -> tuple:
        path = Path(self.tmp) / "runner.json"
        if not path.exists():
            path.write_text(json.dumps({"hub_url": "http://192.168.100.20:8080", "token": "bp_" + "m" * 43,
                                        "name": "build-box", "agents": {}, "projects": {}}), encoding="utf-8")
        asked: list = []

        def answer(question: str) -> str:
            asked.append(question)
            return answers.pop(0) if answers else ""

        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=answer), contextlib.redirect_stdout(out), \
                mock.patch.object(runner, "_input_closed", False), \
                mock.patch.object(runner.shutil, "which", return_value=None):
            code = runner.cmd_init(argparse.Namespace(config=str(path), skip_hub=True))
        self.assertEqual(code, 0)
        return json.loads(path.read_text(encoding="utf-8")), out.getvalue(), asked

    def test_tags_and_subtasks(self):
        # jobs, tags, folder, three switches, subtasks, Remote Control, selftest, project folders
        cfg, out, asked = self.run_init(["", "GPU, pool", "", "", "", "", "y", "", "n", "n"])
        self.assertEqual(cfg["tags"], ["gpu", "pool"])
        self.assertIs(cfg["accept_delegated_tasks"], True)
        self.assertIs(cfg["allow_remote_control"], False)
        self.assertIn("Accept the tags on the machine's page in the dashboard before tasks for a tag can run here.",
                      out)
        self.assertTrue(asked[1].startswith("Tags for this machine"), asked)
        self.assertTrue(asked[6].startswith("Let agents on your other machines hand subtasks to this machine"))
        self.assertIn("(y/N)", asked[6], "No unless you say yes")
        self.assertTrue(asked[7].startswith("Let the dashboard start Remote Control"), asked)
        # Again with Enter everywhere: the answers are kept.
        cfg, out, asked = self.run_init([])
        self.assertEqual((cfg["tags"], cfg["accept_delegated_tasks"]), (["gpu", "pool"], True))
        self.assertIn("(Y/n)", asked[6])

    def test_defaults_are_no_tags_and_no_subtasks(self):
        cfg, out, _ = self.run_init([])
        self.assertEqual(cfg["tags"], [])
        self.assertIs(cfg["accept_delegated_tasks"], False)
        self.assertNotIn("Accept the tags", out)


# ------------------------------------------------------------------ git accounts (runner.json)
GIT_INFO_KEYS = ("git_accounts", "default_git_account", "project_git_accounts", "git_account_problems")


class GitAccounts(TempDirCase):
    # Token-shaped values made here, at run time: nothing in this file looks like a real token.
    BOT_TOKEN = "github_pat_" + "Unit0Fake" * 4
    LAB_TOKEN = "glpat-" + "Unit1Fake" * 3
    # What must never leave the machine, or show in 'runner.py list': tokens, the key path, commit names, emails.
    PRIVATE = (BOT_TOKEN, LAB_TOKEN, "id_ed25519", "Your Name", "build-box bot", "@example.com")

    def setUp(self) -> None:
        super().setUp()
        # A key file that exists: a missing one is reported as a problem (git_accounts).
        self.KEY = os.path.join(self.tmp, "id_ed25519")
        Path(self.KEY).write_text("not a real key\n", encoding="utf-8")

    def cfg(self, **extra) -> dict:
        """The design's three accounts, with a default for the machine and one for a project."""
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": dict(self.selftest_env)}})
        cfg["projects"]["homelab"] = self.tmp
        cfg["git_accounts"] = {
            "personal": {"host": "github.com", "name": "Your Name", "email": "you@example.com", "ssh_key": self.KEY},
            "bot": {"host": "github.com", "token": self.BOT_TOKEN, "name": "build-box bot", "email": "bot@example.com"},
            "lab": {"host": "gitlab.example.com", "kind": "gitlab", "token": self.LAB_TOKEN, "name": "Your Name",
                    "email": "you@example.com"},
        }
        cfg.update(default_git_account="personal", project_git_accounts={"homelab": "lab"})
        cfg.update(extra)
        return cfg

    def spec(self, cfg: dict) -> dict:
        return runner.agent_spec(cfg, "one")

    def test_the_example_accounts(self):
        accounts, problems = runner.git_accounts(self.cfg())
        self.assertEqual(problems, [])
        self.assertEqual({n: a["kind"] for n, a in accounts.items()},
                         {"personal": "github", "bot": "github", "lab": "gitlab"})
        self.assertEqual(accounts["personal"], {"host": "github.com", "kind": "github", "name": "Your Name",
                                                "email": "you@example.com", "ssh_key": os.path.expanduser(self.KEY)})
        self.assertEqual(accounts["bot"], {"host": "github.com", "kind": "github", "name": "build-box bot",
                                           "email": "bot@example.com", "token": self.BOT_TOKEN})
        self.assertEqual(accounts["lab"]["host"], "gitlab.example.com")

    def test_kind_from_the_host_and_optional_fields(self):
        accounts, problems = runner.git_accounts({"git_accounts": {
            "a": {"host": "gitlab.com", "token": self.LAB_TOKEN},
            "b": {"host": "git.example.com:2222", "ssh_key": self.KEY},
            "c": {"host": "GitHub.com", "name": "Your Name", "email": None, "token": ""},
            "d": {"host": "github.com", "kind": "other", "token": self.BOT_TOKEN, "user": "ci"},
            "e": {"host": "github.com", "ssh_key": "~/.ssh/id_bullpen_unit_missing"},
            "f": {"host": "github.com", "ssh_key": self.tmp},
        }})
        # A key file that isn't there is reported (no path), but the account stays: the job that uses it is
        # refused when it starts (git_account_env checks again then).
        self.assertEqual(problems, ["e: its ssh key file is missing", "f: its ssh key file is missing"])
        self.assertEqual({n: (a["host"], a["kind"]) for n, a in accounts.items()},
                         {"a": ("gitlab.com", "gitlab"), "b": ("git.example.com:2222", "other"),
                          "c": ("github.com", "github"), "d": ("github.com", "other"),
                          "e": ("github.com", "github"), "f": ("github.com", "github")})
        self.assertEqual(accounts["e"]["ssh_key"], os.path.expanduser("~/.ssh/id_bullpen_unit_missing"), "~ expanded")
        self.assertEqual(accounts["c"], {"host": "github.com", "kind": "github", "name": "Your Name"},
                         "blank and null fields are left out")
        self.assertEqual(accounts["d"]["user"], "ci")
        self.assertEqual(runner.git_accounts({}), ({}, []))
        self.assertEqual(runner.git_accounts({"git_accounts": {}}), ({}, []))

    def test_the_kind_comes_from_the_host_without_its_port(self):
        # Ruling G14 (F7). A port in the host is for ssh: github.com:22 is still github, so gh is signed out for an
        # account without a token. A host of your own is "other" unless kind says otherwise.
        accounts, problems = runner.git_accounts({"git_accounts": {
            "a": {"host": "github.com:22", "ssh_key": self.KEY}, "b": {"host": "GitLab.com:2222", "ssh_key": self.KEY},
            "c": {"host": "gitlab.example.com:2222", "ssh_key": self.KEY}}})
        self.assertEqual(problems, [])
        self.assertEqual({n: a["kind"] for n, a in accounts.items()}, {"a": "github", "b": "gitlab", "c": "other"})
        # init offers the same kind: add, name, host, kind (blank), commit name, email, ssh key, (token), add no more
        cfg = make_cfg(self.tmp, {})
        asked, _, _ = self.init_step(cfg, ["y", "bot", "github.com:22", "", "", "", self.KEY, "n"], [""])
        self.assertTrue([q for q in asked if q.startswith("  Kind: github, gitlab or other (blank: github, from the "
                                                          "host)")], asked)
        self.assertEqual(cfg["git_accounts"], {"bot": {"host": "github.com:22", "ssh_key": self.KEY}})

    def test_the_heartbeat_says_when_git_is_too_old_for_accounts(self):
        # Ruling G14 (F8): every git account needs git 2.31 or later (GIT_CONFIG_COUNT), so the machine page says so
        # before a job is refused for it. The version is the one git_version keeps.
        old = "git on this machine is older than 2.31, so git accounts can't be used"
        with mock.patch.object(runner, "git_version", return_value=(2, 30)):
            self.assertEqual(runner.git_account_info(self.cfg())["git_account_problems"], [old])
            self.assertEqual(runner.git_account_info({})["git_account_problems"], [], "no accounts: nothing to say")
        for version in ((2, 31), (3, 0), None):
            with mock.patch.object(runner, "git_version", return_value=version):
                self.assertEqual(runner.git_account_info(self.cfg())["git_account_problems"], [], version)

    def test_entries_that_cant_be_used(self):
        raw = {
            "Personal": {"host": "github.com", "token": self.BOT_TOKEN},
            "scheme": {"host": "https://github.com", "token": self.BOT_TOKEN},
            "userinfo": {"host": "git@github.com", "token": self.BOT_TOKEN},
            "path": {"host": "github.com/someone", "token": self.BOT_TOKEN},
            "no-host": {"token": self.BOT_TOKEN},
            "kind": {"host": "git.example.com", "kind": "gitea", "token": self.BOT_TOKEN},
            "key": {"host": "github.com", "ssh_key": [self.KEY]},
            "token": {"host": "github.com", "token": 12345678},
            "nothing": {"host": "github.com", "user": "ci"},
            "text": self.BOT_TOKEN,
            "typo": {"host": "github.com", "sshkey": self.KEY, "name": "Your Name"},
            # The key path goes into an ssh config file in quotes, and user and token into a credential
            # helper's output, one line each: nothing in them may end the line or the quotes.
            "key-quote": {"host": "github.com", "ssh_key": '/keys/a"b'},
            "key-newline": {"host": "github.com", "ssh_key": "/keys/a\nProxyCommand someone"},
            "key-control": {"host": "github.com", "ssh_key": "/keys/a\x1bb"},
            "user-newline": {"host": "github.com", "token": self.BOT_TOKEN, "user": "ci\nprotocol=someone"},
            "token-control": {"host": "github.com", "token": "Unit2Fake\rUnit2Fake"},
            # Nor a backslash or a double quote, which don't belong in either.
            "token-backslash": {"host": "github.com", "token": "Unit2Fake\\nprotocol=someone"},
            "user-quote": {"host": "github.com", "token": self.BOT_TOKEN, "user": 'ci"someone'},
            # A token the log couldn't mask without hiding ordinary words too.
            "token-short": {"host": "github.com", "token": "Unit5Fk"},
            "fine": {"host": "github.com", "token": self.BOT_TOKEN},
        }
        accounts, problems = runner.git_accounts({"git_accounts": raw})
        self.assertEqual(list(accounts), ["fine"])
        names = [n for n in raw if n != "fine"]
        self.assertEqual(len(problems), len(names), problems)
        reasons = {}
        for name, problem in zip(names, problems):
            self.assertTrue(problem.startswith(name + ": "), problem)
            reasons[name] = problem[len(name) + 2:]
        self.assertIn("name", reasons["Personal"])
        for name in ("scheme", "userinfo", "path", "no-host"):
            self.assertIn("host", reasons[name])
        self.assertIn("kind", reasons["kind"])
        self.assertIn("ssh_key", reasons["key"])
        self.assertIn("token", reasons["token"])
        self.assertIn("nothing to use", reasons["nothing"])
        self.assertIn("sshkey", reasons["typo"])
        for name in ("key-quote", "key-newline", "key-control"):
            self.assertIn("ssh_key", reasons[name])
        self.assertIn("user", reasons["user-newline"])
        self.assertIn("token", reasons["token-control"])
        self.assertTrue(reasons["token-backslash"].startswith("token can't hold"), reasons["token-backslash"])
        self.assertIn("backslash", reasons["token-backslash"])
        self.assertTrue(reasons["user-quote"].startswith("user can't hold"), reasons["user-quote"])
        self.assertIn("double quote", reasons["user-quote"])
        self.assertEqual(reasons["token-short"], "token is too short: a git token has at least 8 characters")
        self.assertEqual(runner.check_git_account("ok", {"host": "github.com", "token": "Unit5Fak"})[1], "",
                         "8 characters are enough")
        text = "\n".join(problems)
        for value in (self.BOT_TOKEN, "Unit0Fake", "Unit2Fake", "Unit5Fk", "12345678", ".ssh", "id_ed25519",
                      "someone", "Your Name", "/keys", "ProxyCommand", "protocol="):
            self.assertNotIn(value, text, "a reason never holds a value from the entry")
        self.assertEqual(runner.git_accounts({"git_accounts": {"bot\n": {"host": "github.com", "name": "x"}}})[0], {},
                         "the whole name must match")
        accounts, problems = runner.git_accounts({"git_accounts": ["bot"]})
        self.assertEqual(accounts, {})
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("git_accounts: "), problems)

    def test_choosing_an_account(self):
        cfg = self.cfg()
        spec = self.spec(cfg)
        choose = runner.choose_git_account
        self.assertEqual(choose(cfg, spec, "myapp", "-"), (None, "", ""), "'-' is none")
        self.assertEqual(choose(cfg, spec, "homelab", "-"), (None, "", ""), "even with a default")
        self.assertEqual(choose(cfg, spec, "myapp", "bot"), ("bot", "", ""))
        self.assertEqual(choose(cfg, spec, "homelab", "bot"), ("bot", "", ""), "a pick wins over the defaults")
        self.assertEqual(choose(cfg, spec, "myapp", "nope"),
                         (None, "there is no valid git account 'nope' on build-box", ""))
        self.assertEqual(choose(cfg, spec, "homelab", None), ("lab", "", ""), "the project's default first")
        self.assertEqual(choose(cfg, spec, "myapp", None), ("personal", "", ""), "then the machine's")
        self.assertEqual(choose(cfg, None, "myapp", None), ("personal", "", ""), "no agent (a clone or pull)")
        bare = self.cfg()
        del bare["default_git_account"], bare["project_git_accounts"]
        self.assertEqual(choose(bare, spec, "homelab", None), (None, "", ""), "no default: none")
        self.assertEqual(choose(self.cfg(default_git_account="", project_git_accounts={}), spec, "myapp", None),
                         (None, "", ""), "a blank default is none")
        broken = self.cfg()
        broken["git_accounts"]["bot"]["host"] = "https://github.com"
        self.assertEqual(choose(broken, spec, "myapp", "bot"),
                         (None, "there is no valid git account 'bot' on build-box", ""), "only a valid one")

    def test_a_default_that_names_no_valid_account_refuses(self):
        choose = runner.choose_git_account
        cfg = self.cfg(default_git_account="persnal")
        name, error, note = choose(cfg, self.spec(cfg), "myapp", None)
        self.assertEqual((name, note), (None, ""))
        for words in ("default_git_account", "'persnal'", "build-box"):
            self.assertIn(words, error)
        self.assertEqual(choose(cfg, self.spec(cfg), "myapp", "bot"), ("bot", "", ""), "a pick still works")
        self.assertEqual(choose(cfg, self.spec(cfg), "myapp", "-"), (None, "", ""), "and none")
        self.assertEqual(choose(cfg, self.spec(cfg), "homelab", None), ("lab", "", ""), "the project's own is fine")

        cfg = self.cfg(project_git_accounts={"homelab": "gone"})
        name, error, _ = choose(cfg, self.spec(cfg), "homelab", None)
        self.assertIsNone(name)
        for words in ("project_git_accounts", "homelab", "'gone'"):
            self.assertIn(words, error)
        self.assertEqual(choose(cfg, self.spec(cfg), "myapp", None), ("personal", "", ""), "other projects")

        cfg = self.cfg()
        cfg["git_accounts"]["personal"]["kind"] = "gitea"
        name, error, _ = choose(cfg, self.spec(cfg), "myapp", None)
        self.assertIsNone(name)
        self.assertIn("'personal'", error, "an account that isn't valid is no default either")
        self.assertNotIn("id_ed25519", error)

        for broken, project in (({"default_git_account": 7}, "myapp"), ({"project_git_accounts": ["lab"]}, "myapp"),
                                ({"project_git_accounts": ["lab"]}, "homelab"),
                                ({"project_git_accounts": {"homelab": None}}, "homelab")):
            cfg = self.cfg(**broken)
            name, error, _ = choose(cfg, self.spec(cfg), project, None)
            self.assertIsNone(name, broken)
            self.assertTrue(error, broken)
        cfg = self.cfg(default_git_account=7)
        self.assertEqual(choose(cfg, self.spec(cfg), "homelab", None), ("lab", "", ""),
                         "a broken machine default doesn't apply where the project has its own")

    def test_an_agent_with_its_own_git_setup_skips_the_default(self):
        choose = runner.choose_git_account
        for var in runner.GIT_ENV_VARS:
            cfg = self.cfg()
            cfg["agents"]["one"]["env"][var] = self.BOT_TOKEN
            spec = self.spec(cfg)
            name, error, note = choose(cfg, spec, "myapp", None)
            self.assertEqual((name, error), (None, ""), var)
            self.assertIn(var, note)
            self.assertIn("'personal'", note)
            self.assertNotIn(self.BOT_TOKEN, note)
            self.assertEqual(choose(cfg, spec, "homelab", None)[:2], (None, ""), "a project's default too")
            self.assertEqual(choose(cfg, spec, "myapp", "bot"), ("bot", "", ""), "a pick still applies")
        cfg = self.cfg()
        cfg["agents"]["one"]["env"]["GIT_TRACE"] = "1"
        self.assertEqual(choose(cfg, self.spec(cfg), "myapp", None), ("personal", "", ""),
                         "other variables don't count")
        # Such an agent never uses a default, so a broken one doesn't refuse its jobs (the machine page still
        # shows the problem).
        for broken, project in (({"default_git_account": "persnal"}, "myapp"), ({"default_git_account": 7}, "myapp"),
                                ({"project_git_accounts": {"homelab": "gone"}}, "homelab"),
                                ({"project_git_accounts": ["lab"]}, "myapp")):
            cfg = self.cfg(**broken)
            cfg["agents"]["one"]["env"]["GIT_SSH_COMMAND"] = "ssh -i /keys/own"
            name, error, note = choose(cfg, self.spec(cfg), project, None)
            self.assertEqual((name, error), (None, ""), broken)
            self.assertIn("GIT_SSH_COMMAND", note, broken)
            self.assertNotIn("/keys/own", note)
            self.assertEqual(choose(cfg, self.spec(cfg), project, "bot"), ("bot", "", ""), "a pick still applies")

    def test_subtasks_get_none(self):
        cfg = self.cfg()
        for choice in (None, "bot", "-", "nope"):
            self.assertEqual(runner.choose_git_account(cfg, self.spec(cfg), "homelab", choice, subtask=True),
                             (None, "", ""), choice)
        cfg = self.cfg(default_git_account="persnal")
        self.assertEqual(runner.choose_git_account(cfg, self.spec(cfg), "myapp", None, subtask=True), (None, "", ""))

    def test_the_heartbeat_has_names_and_hosts_only(self):
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps(make_cfg(self.tmp, {})), encoding="utf-8")
        info = runner.Runner(path).heartbeat_payload()["info"]
        self.assertEqual({k: info[k] for k in GIT_INFO_KEYS},
                         {"git_accounts": [], "default_git_account": None, "project_git_accounts": {},
                          "git_account_problems": []}, "the keys are there without accounts too")

        cfg = self.cfg()
        cfg["git_accounts"]["broken"] = {"host": "https://github.com", "token": self.BOT_TOKEN}
        cfg["projects"]["myapp"] = self.tmp
        cfg["project_git_accounts"]["myapp"] = "gone"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        info = runner.Runner(path).heartbeat_payload()["info"]
        self.assertEqual(info["git_accounts"], [{"name": "bot", "host": "github.com", "kind": "github"},
                                                {"name": "lab", "host": "gitlab.example.com", "kind": "gitlab"},
                                                {"name": "personal", "host": "github.com", "kind": "github"}])
        self.assertEqual(info["default_git_account"], "personal")
        self.assertEqual(info["project_git_accounts"], {"homelab": "lab", "myapp": "gone"})
        problems = info["git_account_problems"]
        self.assertEqual(len(problems), 2, problems)
        self.assertTrue(problems[0].startswith("broken: "), problems)
        self.assertIn("myapp", problems[1])
        self.assertIn("'gone'", problems[1])
        text = json.dumps({k: info[k] for k in GIT_INFO_KEYS})
        for value in self.PRIVATE:
            self.assertNotIn(value, text)
        self.assertNotIn(self.BOT_TOKEN, json.dumps(info))

        path.write_text(json.dumps(self.cfg(default_git_account="persnal")), encoding="utf-8")
        info = runner.Runner(path).heartbeat_payload()["info"]
        self.assertEqual(info["default_git_account"], "persnal", "as set, so the machine page can show it")
        self.assertEqual(len(info["git_account_problems"]), 1)
        self.assertIn("default_git_account", info["git_account_problems"][0])
        self.assertIn("'persnal'", info["git_account_problems"][0])

    def test_problems_that_show_before_a_job_fails(self):
        # A key file that isn't there is reported in the heartbeat and by 'runner.py list', without the path. A
        # project_git_accounts entry for a project that isn't here yet is no problem: a clone of that project
        # uses it ('runner.py list' says so). One that names no valid account still is.
        path = Path(self.tmp) / "runner.json"
        cfg = self.cfg()
        cfg["git_accounts"]["personal"]["ssh_key"] = os.path.join(self.tmp, "id_gone")
        cfg["project_git_accounts"].update(webshop="bot", blog="gone")
        path.write_text(json.dumps(cfg), encoding="utf-8")
        info = runner.Runner(path).heartbeat_payload()["info"]
        self.assertEqual(info["git_account_problems"][0], "personal: its ssh key file is missing")
        self.assertEqual(len(info["git_account_problems"]), 2, info["git_account_problems"])
        self.assertIn("blog", info["git_account_problems"][1])
        self.assertIn("'gone'", info["git_account_problems"][1])
        self.assertNotIn("webshop", json.dumps(info["git_account_problems"]))
        self.assertEqual(info["project_git_accounts"]["webshop"], "bot")
        self.assertIn({"name": "personal", "host": "github.com", "kind": "github"}, info["git_accounts"],
                      "still offered: the job that uses it says what is wrong")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.cmd_list(argparse.Namespace(config=str(path)))
        self.assertIn("!! personal: its ssh key file is missing", out.getvalue())
        self.assertIn("default for webshop: bot (applies when that project is cloned)\n", out.getvalue())
        self.assertIn("default for homelab: lab\n", out.getvalue(), "a project that is here: no note")
        self.assertNotIn("!! project_git_accounts (webshop)", out.getvalue())
        for text in (json.dumps(info), out.getvalue()):
            self.assertNotIn("id_gone", text)
        # Choosing is unchanged: the account is still the default, and its job is refused when it starts.
        self.assertEqual(runner.choose_git_account(cfg, self.spec(cfg), "myapp", None), ("personal", "", ""))

    def test_list_shows_accounts_without_their_secrets(self):
        path = Path(self.tmp) / "runner.json"
        cfg = self.cfg()
        cfg["git_accounts"]["broken"] = {"host": "git@github.com", "token": self.BOT_TOKEN}
        path.write_text(json.dumps(cfg), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.cmd_list(argparse.Namespace(config=str(path)))
        text = out.getvalue()
        self.assertRegex(text, r"ok bot +github\.com +github +token\n")
        self.assertRegex(text, r"ok lab +gitlab\.example\.com +gitlab +token\n")
        self.assertRegex(text, r"ok personal +github\.com +github +ssh key\n")
        self.assertIn("!! broken: bad host", text)
        self.assertIn("default for every project: personal", text)
        self.assertIn("default for homelab: lab", text)
        for value in self.PRIVATE:
            self.assertNotIn(value, text)

        path.write_text(json.dumps(make_cfg(self.tmp, {})), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.cmd_list(argparse.Namespace(config=str(path)))
        self.assertIn("Git accounts: none", out.getvalue())

    def init_step(self, cfg: dict, answers: list, secrets: list) -> tuple:
        """runner.ask_git_accounts with typed answers (input) and hidden ones (getpass)."""
        asked: list = []
        hidden: list = []

        def answer(question: str) -> str:
            asked.append(question)
            return answers.pop(0) if answers else ""

        def secret(question: str) -> str:
            hidden.append(question)
            return secrets.pop(0) if secrets else ""

        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=answer), \
                mock.patch.object(runner.getpass, "getpass", side_effect=secret), \
                contextlib.redirect_stdout(out), mock.patch.object(runner, "_input_closed", False):
            runner.ask_git_accounts(cfg)
        return asked, hidden, out.getvalue()

    def test_init_adds_changes_and_removes_accounts(self):
        cfg = make_cfg(self.tmp, {})
        cfg["projects"]["homelab"] = self.tmp
        # add, name, host, kind, commit name, email, ssh key, (token), user, add, default,
        # a project's own: yes, which, account, no more
        asked, hidden, out = self.init_step(cfg, ["y", "bot", "github.com", "", "build-box bot", "bot@example.com", "",
                                                  "", "n", "bot", "y", "homelab", "bot", "n"], [self.BOT_TOKEN])
        self.assertEqual(cfg["git_accounts"], {"bot": {"host": "github.com", "name": "build-box bot",
                                                       "email": "bot@example.com", "token": self.BOT_TOKEN}})
        self.assertEqual((cfg["default_git_account"], cfg["project_git_accounts"]), ("bot", {"homelab": "bot"}))
        self.assertEqual(len(hidden), 1, "the token is read without echo")
        self.assertIn("token", hidden[0])
        self.assertNotIn(self.BOT_TOKEN, out + "".join(asked + hidden))
        self.assertIn("They stay in runner.json. The hub learns their names, hosts and kinds, never a key or token.",
                      " ".join(out.split()))

        # keep, change: host and kind kept, commit name and email removed, a key, the token kept, a user name;
        # add, default none, drop the project's
        asked, hidden, out = self.init_step(cfg, ["", "y", "", "", "-", "-", "~/.ssh/id_unit", "ci-bot", "n", "-",
                                                  "n", "n"], [""])
        self.assertEqual(cfg["git_accounts"], {"bot": {"host": "github.com", "ssh_key": "~/.ssh/id_unit",
                                                       "token": self.BOT_TOKEN, "user": "ci-bot"}})
        self.assertNotIn("default_git_account", cfg)
        self.assertNotIn("project_git_accounts", cfg)
        self.assertTrue(asked[0].startswith("Keep git account 'bot' (github.com, token)?"), asked[0])
        self.assertNotIn(self.BOT_TOKEN, out + "".join(asked + hidden))
        self.assertNotIn("id_unit", out, "the note about a missing key file doesn't print its path")
        self.assertIn("doesn't exist", out)

        cfg["default_git_account"] = "bot"
        asked, hidden, out = self.init_step(cfg, ["n", "n"], [])
        self.assertNotIn("git_accounts", cfg)
        self.assertNotIn("default_git_account", cfg, "the default went with the account")
        self.assertEqual(hidden, [])

    def test_init_refuses_an_account_it_cant_use(self):
        cfg = make_cfg(self.tmp, {})
        # add, a bad name, add, name, a bad host then a good one, kind, commit name, email, ssh key, (no token), add
        asked, _, out = self.init_step(cfg, ["y", "Bot!", "y", "bot", "https://github.com", "github.com", "", "", "",
                                             "", "n"], [""])
        self.assertNotIn("git_accounts", cfg, "nothing to use: not added")
        self.assertIn("nothing to use", out)

    def test_init_says_when_a_projects_account_isnt_valid(self):
        cfg = make_cfg(self.tmp, {})
        cfg["projects"]["homelab"] = self.tmp
        cfg["git_accounts"] = {"bot": {"host": "github.com", "token": self.BOT_TOKEN}}
        cfg["project_git_accounts"] = {"homelab": "gone"}
        asked, _, _ = self.init_step(cfg, [], [])  # Enter to everything: keep it all
        self.assertTrue(any(q.startswith("Keep git account 'gone' for project 'homelab' (not a valid git account)?")
                            for q in asked), asked)
        self.assertEqual(cfg["project_git_accounts"], {"homelab": "gone"})

    def test_init_keeps_the_accounts_on_enter(self):
        path = Path(self.tmp) / "runner.json"
        cfg = self.cfg()
        path.write_text(json.dumps(cfg), encoding="utf-8")
        asked: list = []

        def answer(question: str) -> str:
            asked.append(question)
            return ""

        out = io.StringIO()
        with mock.patch("builtins.input", side_effect=answer), contextlib.redirect_stdout(out), \
                mock.patch.object(runner.getpass, "getpass", side_effect=AssertionError("no token asked")), \
                mock.patch.object(runner, "_input_closed", False), \
                mock.patch.object(runner.shutil, "which", return_value=None):
            self.assertEqual(runner.cmd_init(argparse.Namespace(config=str(path), skip_hub=True)), 0)
        saved = json.loads(path.read_text(encoding="utf-8"))
        for key in ("git_accounts", "default_git_account", "project_git_accounts"):
            self.assertEqual(saved[key], cfg[key], key)
        self.assertTrue(any(q.startswith("Keep git account 'lab' (gitlab.example.com, token)") for q in asked), asked)
        for value in (self.BOT_TOKEN, self.LAB_TOKEN):
            self.assertNotIn(value, out.getvalue() + "".join(asked))


def slashed(path) -> str:
    """A path as the runner writes it into an ssh config and GIT_SSH_COMMAND (forward slashes on Windows)."""
    return str(path).replace("\\", "/") if os.name == "nt" else str(path)


class GitAccountEnv(TempDirCase):
    """What a job, a clone and a pull get from a git account: that account alone for its host (R-A1)."""
    BOT_TOKEN = GitAccounts.BOT_TOKEN
    LAB_TOKEN = GitAccounts.LAB_TOKEN
    PLAIN_TOKEN = "Unit3Fake" * 4  # no known token prefix: only the exact value can mask it
    HELPER = '!f() { printf \'%s\\n\' "username=${BULLPEN_GIT_USER}" "password=${BULLPEN_GIT_TOKEN}"; }; f'
    # The ssh config's block for every other host: the machine user's own settings, always named.
    OTHER_HOSTS = 'Host * !{}\n  Include "~/.ssh/config"\n'
    TOKEN_VARS =("BULLPEN_GIT_TOKEN", "BULLPEN_GIT_USER", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN")
    # The git variables a job could get from an account (compared with the runner's own environment).
    JOB_VARS = ("BULLPEN_GIT_TOKEN", "BULLPEN_GIT_USER", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND", "GH_CONFIG_DIR",
                "GH_TOKEN", "GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL")
    # Prints what the agent sees, and leaks the token on both outputs, as a failing push might.
    ECHO = ("import os, sys; tok = os.environ.get('BULLPEN_GIT_TOKEN', 'none'); "
            "print('push failed for ' + tok); print('stderr ' + tok, file=sys.stderr)")
    # Claude Code's stream-json, with the token where the log cuts lines short: 120 characters into a tool's
    # command (shown up to 140) and 285 into a tool's error (shown up to 300).
    STREAM = "\n".join([
        "import json, os",
        "tok = os.environ.get('BULLPEN_GIT_TOKEN', 'none')",
        "url = 'https://x-access-token:' + 'a' * 70 + tok + '@github.com/org/repo.git'",
        "for ev in ({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': 'm'},",
        "           {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Bash',",
        "            'input': {'command': 'git remote set-url origin ' + url}}]}},",
        "           {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'is_error': True,",
        "            'content': 'b' * 285 + tok}]}},",
        "           {'type': 'result', 'subtype': 'success', 'result': 'done', 'session_id': 's1'}):",
        "    print(json.dumps(ev), flush=True)",
    ])

    @staticmethod
    def cleared(host: str) -> list:
        """The entries after the https:// helpers: no http:// helper, and no extra HTTP headers, for `host`."""
        return [(f"credential.http://{host}.helper", ""), (f"http.https://{host}/.extraHeader", ""),
                (f"http.http://{host}/.extraHeader", "")]

    @staticmethod
    def entries(env: dict) -> list:
        """The GIT_CONFIG_COUNT entries in `env`, as (key, value) pairs in order."""
        return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(int(env["GIT_CONFIG_COUNT"]))]

    def setUp(self) -> None:
        super().setUp()
        self.home = Path(self.tmp) / "home"  # the machine user's home: no ~/.ssh/config unless a test writes one
        self.home.mkdir()
        self.key = Path(self.tmp) / "id_unit"
        self.key.write_text("not a real key\n", encoding="utf-8")
        self.folder = Path(self.tmp) / "job"
        self.folder.mkdir()
        # No system-wide ssh config unless a test writes one (system_ssh_config).
        for name, value in (("HOME", self.home), ("SYSTEM_SSH_CONFIG", Path(self.tmp) / "etc_ssh_config")):
            patch = mock.patch.object(runner, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # Jobs get the runner's environment: its HOME is this home too, so no ~/.netrc or git config of the person
        # running the tests counts.
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        without_git = no_git_url_check()
        without_git.__enter__()
        self.addCleanup(without_git.__exit__, None, None, None)

    def system_ssh_config(self) -> Path:
        """A system-wide ssh config (/etc/ssh/ssh_config) that sets a keep-alive for every host."""
        runner.SYSTEM_SSH_CONFIG.write_text("Host *\n  ServerAliveInterval 17\n", encoding="utf-8")
        return runner.SYSTEM_SSH_CONFIG

    def env(self, account: dict, base: dict | None = None, name: str = "acct", version=(2, 45)) -> tuple:
        with mock.patch.object(runner, "git_version", return_value=version):
            return runner.git_account_env(account, self.folder, dict(base or {}), name)

    def user_ssh_config(self) -> Path:
        """A ~/.ssh/config of the machine user's own, that gives every host another key."""
        (self.home / ".ssh").mkdir()
        config = self.home / ".ssh" / "config"
        config.write_text(f"Host *\n  IdentityFile {Path(self.tmp) / 'users_own_key'}\n", encoding="utf-8")
        if POSIX:
            os.chmod(config, 0o600)
        return config

    # -- the environment ------------------------------------------------------------------
    def test_an_ssh_key_account(self):
        account = {"host": "github.com", "kind": "github", "ssh_key": str(self.key), "name": "Your Name",
                   "email": "you@example.com"}
        env, secrets = self.env(account)
        config = self.folder / "ssh_config"
        self.assertEqual(config.read_text(encoding="utf-8"),
                         f'Host github.com\n  HostName github.com\n  IdentityFile "{slashed(self.key)}"\n'
                         "  IdentitiesOnly yes\n" + self.OTHER_HOSTS.format("github.com"))
        self.assertEqual(env["GIT_SSH_COMMAND"], f'ssh -F "{slashed(config)}"')
        self.assertEqual(self.entries(env), [("credential.https://github.com.helper", ""), *self.cleared("github.com")],
                         "the machine's helpers and extra headers for the host are cleared")
        self.assertEqual(len([k for k in env if k.startswith("GIT_CONFIG_")]), 9)
        for var in self.TOKEN_VARS:
            if var != "GH_TOKEN":
                self.assertNotIn(var, env)
        self.assertEqual(env["GH_TOKEN"], runner.GH_NO_TOKEN, "gh gets a stand-in, not the machine user's login")
        self.assertEqual(secrets, [])
        self.assertEqual(env["GH_CONFIG_DIR"], str(self.folder / "gh"))
        self.assertEqual(list((self.folder / "gh").iterdir()), [], "gh gets an empty folder of its own")
        self.assertNotIn("GLAB_CONFIG_DIR", env)
        self.assertEqual([env[k] for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
                                           "GIT_COMMITTER_EMAIL")],
                         ["Your Name", "Your Name", "you@example.com", "you@example.com"])
        if POSIX:
            self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE((self.folder / "gh").stat().st_mode), 0o700)

    def test_the_users_own_ssh_config_is_kept_for_other_hosts(self):
        # The port in the account's host is its ssh port: the HTTPS entries are for the host name (port 443).
        account = {"host": "git.example.com:2222", "kind": "other", "ssh_key": str(self.key)}
        text = (f'Host git.example.com\n  HostName git.example.com\n  Port 2222\n'
                f'  IdentityFile "{slashed(self.key)}"\n  IdentitiesOnly yes\n'
                + self.OTHER_HOSTS.format("git.example.com"))
        # The user's config, named from ~ as ssh finds it (Git for Windows' ssh reads a full path such as
        # C:/Users/... in an Include as one under ~/.ssh), with or without one where the runner looks: ssh skips
        # an Include that matches no file, and its ~ can be another folder than the runner's home on Windows.
        env, _ = self.env(account)
        self.assertEqual((self.folder / "ssh_config").read_text(encoding="utf-8"), text)
        self.user_ssh_config()
        env, _ = self.env(account)
        self.assertEqual((self.folder / "ssh_config").read_text(encoding="utf-8"), text)
        self.assertEqual(self.entries(env),
                         [("credential.https://git.example.com.helper", ""), *self.cleared("git.example.com")])
        for var in ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR"):
            self.assertNotIn(var, env, "kind other: no gh or glab")
        # The system-wide settings follow the user's, for other hosts too: ssh -F doesn't read them itself.
        system = self.system_ssh_config()
        env, _ = self.env(account)
        self.assertEqual((self.folder / "ssh_config").read_text(encoding="utf-8"),
                         text + ("" if os.name == "nt" else f'  Include "{system}"\n'))

    def ssh_settings(self, env: dict, host: str) -> dict:
        """What ssh would use for `host` when git runs it in a job with `env`: git itself runs the job's
        GIT_SSH_COMMAND (through its own shell, with whichever ssh that finds, as on Windows), told to print
        its settings (ssh -G) to a file instead of connecting. {setting: [values]}."""
        out = Path(self.tmp) / "ssh_settings.txt"
        if out.exists():
            out.unlink()
        gitconfig = Path(self.tmp) / "empty.gitconfig"
        gitconfig.write_text("", encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SSH_"))}
        # "#" ends the command there: git adds the arguments again after it.
        command = f'{env["GIT_SSH_COMMAND"]} -G "$@" >"{slashed(out)}" 2>&1 #'
        subprocess.run(["git", "ls-remote", f"ssh://{host}/org/repo.git"], capture_output=True, text=True,
                       timeout=60, cwd=self.tmp, env={**base, "HOME": str(self.home), "GIT_SSH_COMMAND": command,
                                                      "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"})
        self.assertTrue(out.is_file(), "git ran the job's ssh command")
        settings: dict = {}
        for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, value = line.partition(" ")
            settings.setdefault(key.lower(), []).append(value)
        self.assertIn("hostname", settings, out.read_text(encoding="utf-8", errors="replace"))
        return settings

    @staticmethod
    def key_names(settings: dict) -> list:
        return [value.replace("\\", "/").rsplit("/", 1)[-1] for value in settings.get("identityfile", [])]

    @unittest.skipUnless(runner.shutil.which("git") and (os.name == "nt" or runner.shutil.which("ssh")),
                         "needs git and ssh")
    def test_ssh_uses_the_accounts_key_for_its_host_only(self):
        # No ~/.ssh/config at all: the Include of it matches no file, which ssh takes as nothing to read.
        env, _ = self.env({"host": "github.com", "kind": "github", "ssh_key": str(self.key)})
        self.assertEqual(self.key_names(self.ssh_settings(env, "github.com")), ["id_unit"])
        self.assertIn("hostname", self.ssh_settings(env, "submodule.example.com"))
        # The system-wide settings (here, a keep-alive) reach other hosts, not the account's.
        if POSIX:
            self.system_ssh_config()
            env, _ = self.env({"host": "github.com", "kind": "github", "ssh_key": str(self.key)})
            self.assertEqual(self.ssh_settings(env, "submodule.example.com")["serveraliveinterval"], ["17"])
            self.assertEqual(self.ssh_settings(env, "github.com")["serveraliveinterval"], ["0"])
        # The user's own config gives every host another key; with it included for every host, ssh would try
        # that key too when the account's is refused. Another host (a submodule's, say) keeps the user's setup.
        (self.home / ".ssh").mkdir()
        config = self.home / ".ssh" / "config"
        config.write_text(f"Host *\n  IdentityFile {slashed(Path(self.tmp) / 'users_own_key')}\n", encoding="utf-8")
        if POSIX:
            os.chmod(config, 0o600)
        env, _ = self.env({"host": "github.com", "kind": "github", "ssh_key": str(self.key)})
        if POSIX:
            self.assertEqual(shlex.split(env["GIT_SSH_COMMAND"]), ["ssh", "-F", str(self.folder / "ssh_config")])
        self.assertEqual(self.key_names(self.ssh_settings(env, "github.com")), ["id_unit"])
        self.assertEqual(self.key_names(self.ssh_settings(env, "submodule.example.com")), ["users_own_key"])

        # An account without a key: ssh to its host can't sign in at all, so a git@ remote fails rather than
        # go out as the machine user (its key, its ssh agent, a password). Other hosts are as before.
        for account in ({"host": "github.com", "kind": "github", "token": self.BOT_TOKEN},
                        {"host": "github.com", "kind": "github", "name": "Your Name", "email": "you@example.com"}):
            env, _ = self.env(account)
            there = self.ssh_settings(env, "github.com")
            self.assertEqual(there["identityfile"], ["none"], "no identity file at all, not even ssh's defaults")
            self.assertEqual(there["identityagent"], ["none"], "and no ssh agent")
            self.assertEqual(there["identitiesonly"], ["yes"])
            for option in ("pubkeyauthentication", "passwordauthentication", "kbdinteractiveauthentication",
                           "hostbasedauthentication"):
                self.assertIn(there[option][0], ("no", "false"), option)
            # An ssh built without GSSAPI doesn't list it (and can't use it).
            self.assertIn(there.get("gssapiauthentication", ["no"])[0], ("no", "false"))
            self.assertEqual(self.key_names(self.ssh_settings(env, "submodule.example.com")), ["users_own_key"])

    WINDOWS_SSH = Path(os.environ.get("SystemRoot") or "C:/Windows") / "System32" / "OpenSSH" / "ssh.exe"

    @unittest.skipUnless(os.name == "nt" and WINDOWS_SSH.is_file(), "needs Windows' own OpenSSH")
    def test_windows_own_openssh_takes_the_jobs_settings(self):
        # Windows' own OpenSSH (not Git for Windows' ssh, which the test above gets through git) is the ssh git
        # finds when Git for Windows was installed to use it. It must take the job's settings file as the runner
        # writes it: from the job's folder (no permission check refuses it), with forward slashes, and with the
        # Include of ~/.ssh/config, read or matching no file, but not refused.
        (self.home / ".ssh").mkdir()
        (self.home / ".ssh" / "config").write_text(
            f"Host *\n  IdentityFile {slashed(Path(self.tmp) / 'users_own_key')}\n", encoding="utf-8")
        for account in ({"host": "github.com", "kind": "github", "ssh_key": str(self.key)},
                        {"host": "github.com", "kind": "github", "token": self.BOT_TOKEN}):
            env, _ = self.env(account)
            config = slashed(self.folder / "ssh_config")
            self.assertEqual(env["GIT_SSH_COMMAND"], f'ssh -F "{config}"')

            def run(host: str) -> tuple:
                done = subprocess.run([str(self.WINDOWS_SSH), "-F", config, "-v", "-G", host], capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", timeout=60,
                                      env={**os.environ, "HOME": str(self.home)})
                self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
                settings: dict = {}
                for line in done.stdout.splitlines():
                    key, _, value = line.partition(" ")
                    settings.setdefault(key.lower(), []).append(value)
                return settings, done.stderr

            there, _ = run("github.com")
            if "ssh_key" in account:
                self.assertEqual(self.key_names(there), ["id_unit"], "the account's key only")
            else:
                self.assertNotIn("users_own_key", self.key_names(there))
                self.assertEqual((there["identitiesonly"], there["pubkeyauthentication"][0] in ("no", "false")),
                                 (["yes"], True))
            _, log = run("submodule.example.com")
            self.assertRegex(log, r"(?i)include .*config matched no files|reading configuration data .*[\\/]config")

    def test_a_token_account(self):
        base = {"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.autocrlf", "GIT_CONFIG_VALUE_0": "false",
                "GIT_CONFIG_KEY_1": "user.useConfigOnly", "GIT_CONFIG_VALUE_1": "true"}
        env, secrets = self.env({"host": "github.com", "kind": "github", "token": self.BOT_TOKEN}, base)
        self.assertEqual(self.entries({**base, **env}),
                         [("core.autocrlf", "false"), ("user.useConfigOnly", "true"),
                          ("credential.https://github.com.helper", ""),
                          ("credential.https://github.com.helper", self.HELPER), *self.cleared("github.com")],
                         "appended after the entries already there, which are kept")
        kept = {"GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0", "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"}
        self.assertFalse(kept & set(env), "and left as they are")
        self.assertEqual((env["BULLPEN_GIT_USER"], env["BULLPEN_GIT_TOKEN"]), ("x-access-token", self.BOT_TOKEN))
        self.assertEqual(env["GH_TOKEN"], self.BOT_TOKEN)
        for var in ("GH_HOST", "GH_ENTERPRISE_TOKEN", "GLAB_CONFIG_DIR"):
            self.assertNotIn(var, env)
        # No key: ssh to the host can't sign in, rather than go out with the machine user's own key or ssh agent.
        config = self.folder / "ssh_config"
        self.assertEqual(config.read_text(encoding="utf-8"),
                         "Host github.com\n  HostName github.com\n  IdentitiesOnly yes\n  IdentityFile none\n"
                         "  IdentityAgent none\n  PubkeyAuthentication no\n  PasswordAuthentication no\n"
                         "  KbdInteractiveAuthentication no\n  GSSAPIAuthentication no\n  HostbasedAuthentication no\n"
                         + self.OTHER_HOSTS.format("github.com"))
        self.assertEqual(env["GIT_SSH_COMMAND"], f'ssh -F "{slashed(config)}"')
        config = {**base, **env}
        entries = [config[k] for k in config if k.startswith("GIT_CONFIG_")] + [k for k in config]
        self.assertFalse([e for e in entries if self.BOT_TOKEN in e], "the token is in no config entry")
        basic = base64.b64encode(f"x-access-token:{self.BOT_TOKEN}".encode()).decode()
        self.assertEqual(sorted(secrets), sorted([self.BOT_TOKEN, basic]), "the token and its Basic auth form")

        env, _ = self.env({"host": "git.example.com", "kind": "github", "token": self.BOT_TOKEN})
        self.assertEqual((env["GH_HOST"], env["GH_ENTERPRISE_TOKEN"]), ("git.example.com", self.BOT_TOKEN))
        self.assertNotIn("GH_TOKEN", env)
        # gh sends GH_TOKEN, not GH_ENTERPRISE_TOKEN, to a subdomain of ghe.com.
        env, _ = self.env({"host": "octo.ghe.com", "kind": "github", "token": self.BOT_TOKEN})
        self.assertEqual((env["GH_HOST"], env["GH_TOKEN"]), ("octo.ghe.com", self.BOT_TOKEN))
        self.assertNotIn("GH_ENTERPRISE_TOKEN", env)

        env, secrets = self.env({"host": "gitlab.example.com", "kind": "gitlab", "token": self.LAB_TOKEN,
                                 "user": "ci"})
        self.assertEqual((env["GITLAB_TOKEN"], env["GITLAB_HOST"], env["BULLPEN_GIT_USER"]),
                         (self.LAB_TOKEN, "gitlab.example.com", "ci"))
        self.assertIn(base64.b64encode(f"ci:{self.LAB_TOKEN}".encode()).decode(), secrets)
        glab = self.folder / "glab"
        self.assertEqual(env["GLAB_CONFIG_DIR"], str(glab))
        # glab reads the first config.yml it finds, its own folder's first: an empty one there, or it would
        # read the machine user's.
        self.assertEqual([p.name for p in glab.iterdir()], ["config.yml"])
        self.assertEqual((glab / "config.yml").read_text(encoding="utf-8"), "")
        for var in ("GH_CONFIG_DIR", "GH_TOKEN"):
            self.assertNotIn(var, env)
        env, _ = self.env({"host": "gitlab.com", "kind": "gitlab", "token": self.LAB_TOKEN})
        self.assertEqual(env["GITLAB_HOST"], "gitlab.com")
        # A port in the host is its ssh port: glab's API and the HTTPS helper are on the host name.
        env, _ = self.env({"host": "gitlab.example.com:2222", "kind": "gitlab", "token": self.LAB_TOKEN})
        self.assertEqual((env["GITLAB_HOST"], env["GIT_CONFIG_KEY_1"]),
                         ("gitlab.example.com", "credential.https://gitlab.example.com.helper"))
        env, _ = self.env({"host": "git.example.com", "kind": "other", "token": self.PLAIN_TOKEN})
        for var in ("GH_CONFIG_DIR", "GLAB_CONFIG_DIR", "GH_TOKEN", "GITLAB_TOKEN"):
            self.assertNotIn(var, env)
        self.assertEqual(env["BULLPEN_GIT_TOKEN"], self.PLAIN_TOKEN)

    def test_tokens_already_in_the_environment_dont_stand_in_for_the_account(self):
        fake = "Unit4Fake" * 4
        base = {var: fake for var in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITLAB_TOKEN",
                                      "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")}
        ssh = {"ssh_key": str(self.key)}
        env, _ = self.env({"host": "github.com", "kind": "github", **ssh}, base)
        self.assertEqual({k: env.get(k) for k in ("GH_TOKEN", "GITHUB_TOKEN")},
                         {"GH_TOKEN": runner.GH_NO_TOKEN, "GITHUB_TOKEN": ""})
        self.assertNotIn("GH_ENTERPRISE_TOKEN", env, "a token for other hosts is left alone")
        env, _ = self.env({"host": "git.example.com", "kind": "github", **ssh},
                          {**base, "GITHUB_ENTERPRISE_TOKEN": fake})
        self.assertEqual({k: env.get(k) for k in ("GH_HOST", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")},
                         {"GH_HOST": "git.example.com", "GH_ENTERPRISE_TOKEN": runner.GH_NO_TOKEN,
                          "GITHUB_ENTERPRISE_TOKEN": ""})
        self.assertNotIn("GH_TOKEN", env)
        env, _ = self.env({"host": "github.com", "kind": "github", "token": self.BOT_TOKEN}, base)
        self.assertEqual(env["GH_TOKEN"], self.BOT_TOKEN)
        self.assertNotIn("GITHUB_TOKEN", env, "GH_TOKEN comes first for gh")
        env, _ = self.env({"host": "gitlab.example.com", "kind": "gitlab", **ssh}, base)
        self.assertEqual({k: env.get(k) for k in ("GITLAB_TOKEN", "GITLAB_ACCESS_TOKEN", "OAUTH_TOKEN")},
                         {"GITLAB_TOKEN": runner.GH_NO_TOKEN, "GITLAB_ACCESS_TOKEN": "", "OAUTH_TOKEN": ""})
        env, _ = self.env({"host": "git.example.com", "kind": "other", **ssh}, base)
        self.assertFalse([k for k in base if k in env], "kind other: gh and glab aren't touched")

    def test_gh_without_a_token_gets_one_its_host_refuses(self):
        # gh with no token in its variables and none in its (empty) config folder takes the machine user's own
        # login from the system keychain: a stand-in the host refuses keeps it from doing that. glab gets one too,
        # so neither falls back to a login of the machine user's. A port in the host is for ssh, not their API.
        name_only = {"name": "Your Name", "email": "you@example.com"}
        for kind, host, variables in (
                ("github", "github.com", {"GH_TOKEN": runner.GH_NO_TOKEN}),
                ("github", "octo.ghe.com", {"GH_TOKEN": runner.GH_NO_TOKEN, "GH_HOST": "octo.ghe.com"}),
                ("github", "git.example.com:2222", {"GH_ENTERPRISE_TOKEN": runner.GH_NO_TOKEN,
                                                    "GH_HOST": "git.example.com"}),
                ("gitlab", "gitlab.com", {"GITLAB_TOKEN": runner.GH_NO_TOKEN, "GITLAB_HOST": "gitlab.com"}),
                ("gitlab", "gitlab.example.com:2222", {"GITLAB_TOKEN": runner.GH_NO_TOKEN,
                                                       "GITLAB_HOST": "gitlab.example.com"})):
            for fields in ({"ssh_key": str(self.key)}, name_only):
                env, secrets = self.env({"host": host, "kind": kind, **fields})
                got = {k: env[k] for k in ("GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_HOST", "GITLAB_TOKEN", "GITLAB_HOST")
                       if k in env}
                self.assertEqual(got, variables, host)
                self.assertEqual(secrets, [], "the stand-in isn't a secret")
        self.assertNotRegex(runner.GH_NO_TOKEN, r"^(gh[a-z]_|github_pat_|glpat-)", "nothing like a real token")
        self.assertEqual((self.folder / "glab" / "config.yml").read_text(encoding="utf-8"), "")
        # The job's log says so, for gh and glab only, and only without a token.
        for kind, words in (("github", ["gh is signed out for this job: git account 'acct' has no token."]),
                            ("gitlab", ["glab is signed out for this job: git account 'acct' has no token."]),
                            ("other", [])):
            self.assertEqual(runner.git_account_notes({"host": "git.example.com", "kind": kind}, "acct", {}), words)
            self.assertEqual(runner.git_account_notes({"host": "git.example.com", "kind": kind}, "acct", {},
                                                      tools=False), [], "a clone or pull runs neither")
            self.assertEqual(runner.git_account_notes({"host": "git.example.com", "kind": kind,
                                                       "token": self.BOT_TOKEN}, "acct", {}), [])

    def test_what_refuses_an_account(self):
        account = {"host": "github.com", "kind": "github", "token": self.BOT_TOKEN}
        with self.assertRaises(runner.GitAccountProblem) as caught:
            self.env(account, version=(2, 30))
        self.assertEqual(str(caught.exception), "git accounts need git 2.31 or later; this machine has 2.30")
        with self.assertRaises(runner.GitAccountProblem) as caught:
            self.env(account, version=None)
        self.assertIn("2.31", str(caught.exception))
        for missing in (Path(self.tmp) / "id_gone", Path(self.tmp)):
            with self.assertRaises(runner.GitAccountProblem) as caught:
                self.env({"host": "github.com", "kind": "github", "ssh_key": str(missing)}, name="personal")
            self.assertEqual(str(caught.exception), "the ssh key file of git account 'personal' doesn't exist")
        with self.assertRaises(runner.GitAccountProblem) as caught:
            self.env(account, {"GIT_CONFIG_COUNT": "two"})
        self.assertIn("GIT_CONFIG_COUNT", str(caught.exception))
        self.assertEqual(list(self.folder.iterdir()), [], "nothing was written")

    # -- what else would sign in instead of the account (ruling G14) ---------------------------
    @staticmethod
    def netrc(home: Path, text: str, name: str = ".netrc") -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, encoding="utf-8")

    def test_a_netrc_entry_curl_would_use_refuses_the_account(self):
        # git's https:// goes through curl, which takes a login from the netrc file for the host (or from its
        # default entry) before any credential helper is asked: the machine user's login would be used, not the
        # account's. The host is compared without its port, in any case. Fake values, built here.
        login, password = "machine-user", "netrc-" + "Unit8Fake" * 2
        account = {"host": "github.com:2222", "kind": "github", "token": self.BOT_TOKEN}
        for_host = ("~/.netrc has a login for github.com, which git would use instead of git account 'bot'. Remove "
                    "that entry from ~/.netrc, or pick None for the job.")
        default = ("~/.netrc has a default login, which git would use for github.com instead of git account 'bot'. "
                   "Remove that entry from ~/.netrc, or pick None for the job.")
        for text, words in ((f"machine GitHub.COM login {login} password {password}\n", for_host),
                            (f"machine gitlab.com login x password y\n\nDEFAULT login {login} password {password}\n",
                             default),
                            # A file a netrc parser refuses: the plain scan still finds the entry.
                            ('machine github.com\n  login "unclosed\n  macdef\n', for_host)):
            self.netrc(self.home, text)
            with self.assertRaises(runner.GitAccountProblem) as caught:
                self.env(account, name="bot")
            self.assertEqual(str(caught.exception), words)
            self.assertEqual(list(self.folder.iterdir()), [], "nothing was written")
        # Another host's entry, or no file: the account is used.
        self.netrc(self.home, f"machine gitlab.com login {login} password {password}\n"
                              "machine github.com.example.com login x password y\n")
        self.assertEqual(self.env(account, name="bot")[0]["GH_TOKEN"], self.BOT_TOKEN)
        (self.home / ".netrc").unlink()
        self.assertEqual(self.env(account, name="bot")[0]["GH_TOKEN"], self.BOT_TOKEN)
        # curl looks in the job's HOME: one in its environment comes before the runner's own home.
        agent_home = Path(self.tmp) / "agent-home"
        self.netrc(agent_home, f"default login {login} password {password}\n")
        with self.assertRaises(runner.GitAccountProblem) as caught:
            self.env(account, {"HOME": str(agent_home)}, name="bot")
        self.assertEqual(str(caught.exception), default)
        self.assertNotIn(password, str(caught.exception))

    def test_on_windows_curl_also_reads_underscore_netrc(self):
        # Fake values. On Windows curl reads _netrc too, in HOME, else in USERPROFILE.
        windows_home = Path(self.tmp) / "windows-home"
        self.netrc(windows_home, "default login x password y\n", "_netrc")
        problem = runner.git_netrc_problem
        with mock.patch.object(runner, "IS_WINDOWS", True):
            self.assertEqual(problem({"USERPROFILE": str(windows_home)}, "github.com", "bot"),
                             "%USERPROFILE%\\_netrc has a default login, which git would use for github.com instead of "
                             "git account 'bot'. Remove that entry from %USERPROFILE%\\_netrc, or pick None for the job.")
            self.assertTrue(problem({"HOME": str(windows_home), "USERPROFILE": str(self.home)}, "github.com", "bot"))
            self.assertEqual(problem({"USERPROFILE": str(self.home)}, "github.com", "bot"), "")
        with mock.patch.object(runner, "IS_WINDOWS", False):
            self.assertEqual(problem({"USERPROFILE": str(windows_home)}, "github.com", "bot"), "",
                             "elsewhere curl reads ~/.netrc only")

    def test_the_account_gets_an_askpass_that_answers_nothing(self):
        # Ruling G14 (F3). An account without a token has no answer for https://: git fails there rather than ask the
        # machine's own program (core.askPass, SSH_ASKPASS) or a terminal for a password.
        env, _ = self.env({"host": "github.com", "kind": "github", "ssh_key": str(self.key)})
        helper = self.folder / ("askpass.cmd" if os.name == "nt" else "askpass")
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_ASKPASS"]), ("0", str(helper)))
        if POSIX:
            self.assertEqual(stat.S_IMODE(helper.stat().st_mode), 0o700)
        done = subprocess.run([str(helper), "Password for 'https://github.com': "], capture_output=True, text=True,
                              timeout=30)
        self.assertEqual((done.returncode, done.stdout), (0, ""))
        for var in ("GIT_CONFIG_PARAMETERS", "SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"):
            self.assertIn(var, runner.GIT_ACCOUNT_UNSET)
        # A job's agent goes without the machine's SSH_ASKPASS, and the log has no note about it.
        job = {"id": 102, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        with mock.patch.dict(os.environ, {"SSH_ASKPASS": "/usr/bin/ssh-askpass", "SSH_ASKPASS_REQUIRE": "force"}):
            os.environ.pop("GIT_CONFIG_PARAMETERS", None)
            finished, lines, started = self.run_job(self.cfg(), job)
        self.assertEqual(finished["status"], "done", finished)
        env = started[0][1]
        self.assertFalse({"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"} & set(env))
        self.assertEqual((env["GIT_TERMINAL_PROMPT"], env["GIT_ASKPASS"]),
                         ("0", str(runner.JOBS_DIR / "102" / helper.name)))
        self.assertFalse([ln for ln in lines if "GIT_CONFIG_PARAMETERS" in ln], lines)

    @unittest.skipUnless(POSIX and runner.shutil.which("git"), "needs git; the machine's askpass is a shell script")
    def test_an_account_without_a_token_fails_over_http_instead_of_asking_another_program(self):
        version = runner.git_version()
        if version is None or version < (2, 31):
            self.skipTest("needs git 2.31 or later")
        # A local server that wants a login (no https: that needs a certificate; git asks the same way over http).
        seen: list = []

        class Refuse(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                seen.append(self.headers.get("Authorization") or "")
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="unit"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Refuse)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/org/repo.git"
        # The machine user's own program, which answers with a password (fake) and says it was asked.
        asked = Path(self.tmp) / "asked.txt"
        other = Path(self.tmp) / "machine-askpass"
        other.write_text(f'#!/bin/sh\necho asked >> "{asked}"\necho machine-secret-2\n', encoding="utf-8")
        os.chmod(other, 0o755)
        gitconfig = Path(self.tmp) / "askpass.gitconfig"
        gitconfig.write_text(f"[core]\n\taskPass = {other}\n", encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GCM_", "SSH_ASKPASS"))}
        base.update(HOME=str(self.home), GIT_CONFIG_GLOBAL=str(gitconfig), GIT_CONFIG_NOSYSTEM="1",
                    SSH_ASKPASS=str(other), SSH_ASKPASS_REQUIRE="force")

        def ls_remote(env: dict) -> subprocess.CompletedProcess:
            return subprocess.run(["git", "ls-remote", url], env=env, capture_output=True, text=True, timeout=60,
                                  stdin=subprocess.DEVNULL, cwd=self.tmp)

        without = ls_remote(base)
        self.assertTrue(asked.is_file(), "without an account, git asks the machine's program (the test's check)")
        self.assertNotEqual(without.returncode, 0)
        asked.unlink()
        seen.clear()
        env, _ = self.env({"host": "127.0.0.1", "kind": "other", "ssh_key": str(self.key)}, base)
        job = {k: v for k, v in base.items() if k not in runner.GIT_ACCOUNT_UNSET}
        job.update(env)
        done = ls_remote(job)
        self.assertNotEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(asked.exists(), "the machine's program was never asked")
        sent = [base64.b64decode(h.split(" ", 1)[1]).decode("utf-8", "replace") for h in seen if " " in h]
        self.assertFalse([s for s in sent if "machine-secret" in s], sent)

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_a_url_with_credentials_for_the_accounts_host_refuses_the_job(self):
        # Ruling G14 (F2). git uses credentials in a URL before any helper, so a remote that holds them, or a
        # url.<base>.insteadOf or pushInsteadOf setting that rewrites to such a URL, would push as someone else
        # than the account. Checked before the agent starts, with the job's environment, in its folder. The host
        # is compared as for a clone. Fake values, built here.
        self.machine_git_config()
        cred = "u:" + "Unit7Fake" * 2
        repo = Path(self.tmp) / "myapp"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

        cfg = self.cfg()
        cfg["projects"]["myapp"] = str(repo)
        job = {"id": 104, "agent": "echo", "project": "myapp", "autonomy": "readonly", "prompt": "push it"}

        def refused(words: str, data: dict = job) -> None:
            finished, _, started = self.run_job(cfg, data)
            self.assertEqual((finished["status"], finished.get("error")), ("error", words))
            self.assertNotIn("Unit7Fake", json.dumps(finished))
            self.assertEqual(started, [], "nothing was started")

        def runs(data: dict = job) -> None:
            finished, _, started = self.run_job(cfg, data)
            self.assertEqual(finished["status"], "done", finished)
            self.assertEqual(len(started), 1)

        remote = ("remote 'origin' has credentials for github.com in its URL, which git would use instead of git "
                  "account 'bot'. " + runner.NO_URL_CREDENTIALS)
        rewrite = ("a url.<base>.{} setting here gives URLs credentials for github.com, which git would use instead of "
                   "git account 'bot'. " + runner.NO_URL_CREDENTIALS)
        git("remote", "add", "origin", f"https://{cred}@github.com/org/myapp.git")
        refused(remote)
        git("remote", "set-url", "origin", "https://github.com/org/myapp.git")
        runs()
        git("config", f"url.https://{cred}@github.com/.insteadOf", "https://github.com/")
        refused(rewrite.format("insteadOf"))
        git("config", "--unset", f"url.https://{cred}@github.com/.insteadOf")
        git("config", f"url.https://{cred}@github.com/.pushInsteadOf", "https://github.com/")
        refused(rewrite.format("pushInsteadOf"))
        git("config", "--unset", f"url.https://{cred}@github.com/.pushInsteadOf")
        # A rewrite that adds credentials to every https:// URL names no host, so it would reach the account's host
        # too: refused on its own, with or without a remote it rewrites.
        git("config", f"url.https://{cred}@.insteadOf", "https://")
        refused(rewrite.format("insteadOf"))
        git("config", "--unset", f"url.https://{cred}@.insteadOf")
        git("config", "remote.origin.pushurl", f"https://{cred}@github.com/org/myapp.git")
        refused(remote)
        git("config", "--unset", "remote.origin.pushurl")
        # Over http:// as well: git would send those credentials to the account's host just the same.
        git("remote", "set-url", "origin", f"http://{cred}@github.com/org/myapp.git")
        refused(remote)
        git("remote", "set-url", "origin", "https://github.com/org/myapp.git")
        git("config", f"url.http://{cred}@github.com/.insteadOf", "https://github.com/")
        refused(rewrite.format("insteadOf"))
        git("config", "--unset", f"url.http://{cred}@github.com/.insteadOf")
        # Credentials for another host, and an ssh user without a password, are not the account's business.
        git("remote", "add", "elsewhere", f"https://{cred}@gitlab.com/org/myapp.git")
        git("remote", "add", "ssh", "ssh://git@github.com/org/myapp.git")
        git("config", f"url.https://{cred}@gitlab.com/.insteadOf", "https://gitlab.example.com/")
        runs()
        # The machine's own settings count in a folder that isn't a repository too: the agent may clone there.
        self.machine_git_config(f'[url "https://{cred}@github.com/"]\n\tinsteadOf = https://github.com/\n')
        refused(rewrite.format("insteadOf"), {**job, "project": "chat"})
        refused(rewrite.format("insteadOf"))
        runs({**job, "options": {"timeout_min": 2, "git_account": "-"}})
        self.machine_git_config(f'[url "https://{cred}@"]\n\tinsteadOf = https://\n')
        refused(rewrite.format("insteadOf"), {**job, "project": "chat"})

    def test_paths_that_would_end_the_quotes_are_refused(self):
        # _ssh_path: GIT_SSH_COMMAND is read by a shell, inside double quotes, where $, a backtick, a double quote and
        # a backslash would end them or run something. An ssh config file only needs the double quote kept out.
        chars = ["$", "`", '"'] + (["\\"] if POSIX else [])  # Windows' paths take forward slashes instead
        for char in chars:
            with self.assertRaises(runner.GitAccountProblem, msg=char):
                runner._ssh_path(Path(self.tmp) / f"job{char}x" / "ssh_config", "the job folder", shell=True)
        self.assertTrue(runner._ssh_path(Path(self.tmp) / "id$x", "the key").endswith("id$x"))
        # A job folder holding $(x) is refused before anything is written there.
        folder = Path(self.tmp) / "jobs$(x)"
        folder.mkdir()
        with mock.patch.object(runner, "git_version", return_value=(2, 45)), \
                self.assertRaises(runner.GitAccountProblem) as caught:
            runner.git_account_env({"host": "github.com", "kind": "other", "ssh_key": str(self.key)}, folder, {}, "bot")
        self.assertEqual(str(caught.exception), "the job folder has a character that can't go into an ssh setting")
        self.assertEqual(list(folder.iterdir()), [], "no ssh_config")
        # ssh reads %x in an IdentityFile as a token: a percent sign in the key's path is written as %%.
        key = Path(self.tmp) / "id_100%d"
        key.write_text("not a real key\n", encoding="utf-8")
        self.env({"host": "github.com", "kind": "other", "ssh_key": str(key)})
        self.assertIn(f'  IdentityFile "{slashed(key).replace("%", "%%")}"\n',
                      (self.folder / "ssh_config").read_text(encoding="utf-8"))

    def test_git_version(self):
        done = lambda out: subprocess.CompletedProcess(["git", "--version"], 0, out, "")  # noqa: E731
        found = os.path.abspath("/usr/bin/git")
        with mock.patch.dict(runner._GIT_VERSION, clear=True), \
                mock.patch.object(runner.shutil, "which", return_value=found):
            with mock.patch.object(runner.subprocess, "run", side_effect=OSError("no git")):
                self.assertIsNone(runner.git_version())
            with mock.patch.object(runner.subprocess, "run", return_value=done("git version 2.39.5 (Apple Git-154)\n")):
                self.assertEqual(runner.git_version(), (2, 39))
            with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("cached")):
                self.assertEqual(runner.git_version(), (2, 39))
        with mock.patch.dict(runner._GIT_VERSION, clear=True), \
                mock.patch.object(runner.shutil, "which", return_value=found), \
                mock.patch.object(runner.subprocess, "run", return_value=done("git version 2.45.1.windows.1\n")):
            self.assertEqual(runner.git_version(), (2, 45))
        with mock.patch.object(runner.shutil, "which", return_value=None), \
                mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("no git to ask")):
            self.assertIsNone(runner.git_version("/nowhere"))

    @unittest.skipUnless(POSIX, "fake git programs are shell scripts")
    def test_the_git_the_job_finds_is_the_one_checked(self):
        # The agent runs git from its own PATH, which may find an older git first than the runner's: one older
        # than 2.31 ignores GIT_CONFIG_COUNT, and with it the account's settings, without a word.
        def fake_git(folder: str, version: str) -> str:
            path = Path(self.tmp) / folder
            path.mkdir()
            (path / "git").write_text(f"#!/bin/sh\necho 'git version {version}'\n", encoding="utf-8")
            os.chmod(path / "git", 0o755)
            return str(path)

        old, new = fake_git("old", "2.30.9"), fake_git("new", "2.45.2")
        account = {"host": "github.com", "kind": "github", "token": self.BOT_TOKEN}
        with mock.patch.dict(runner._GIT_VERSION, clear=True):
            with self.assertRaises(runner.GitAccountProblem) as caught:
                runner.git_account_env(account, self.folder, {"PATH": os.pathsep.join([old, new])}, "bot")
            self.assertEqual(str(caught.exception), "git accounts need git 2.31 or later; this machine has 2.30")
            env, _ = runner.git_account_env(account, self.folder, {"PATH": os.pathsep.join([new, old])}, "bot")
            self.assertEqual(env["GH_TOKEN"], self.BOT_TOKEN)
            with self.assertRaises(runner.GitAccountProblem):
                runner.git_account_env(account, self.folder, {"PATH": str(Path(self.tmp) / "none")}, "bot")
            # Each program's answer is kept by its path.
            self.assertEqual(runner._GIT_VERSION,
                             {os.path.join(old, "git"): (2, 30), os.path.join(new, "git"): (2, 45)})
            with mock.patch.object(runner.subprocess, "run", side_effect=AssertionError("asked again")):
                self.assertEqual(runner.git_version(os.pathsep.join([old, new])), (2, 30))

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_real_git_uses_the_account_alone_for_its_host(self):
        version = runner.git_version()
        if version is None or version < (2, 31):
            self.skipTest("needs git 2.31 or later")
        # The machine user's own git setup: a helper that answers for every host.
        gitconfig = self.home / ".gitconfig"
        gitconfig.write_text('[credential]\n\thelper = "!f() { echo username=machine-user; '
                             'echo password=machine-secret-0; }; f"\n', encoding="utf-8")
        base = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "GCM_"))}
        base.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / "xdg"), GIT_CONFIG_GLOBAL=str(gitconfig),
                    GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never",
                    GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="false")

        def git(env: dict, *args: str, ask: str = "") -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], env=env, input=ask, capture_output=True, text=True, timeout=60,
                                  cwd=self.tmp)

        ask = "protocol=https\nhost=example.com\n\n"
        self.assertIn("password=machine-secret-0", git(base, "credential", "fill", ask=ask).stdout)
        env, _ = self.env({"host": "example.com", "kind": "other", "token": self.PLAIN_TOKEN}, base)
        job = {**base, **env}
        got = git(job, "config", "--get-all", "credential.https://example.com.helper")
        self.assertEqual(got.stdout, "\n" + self.HELPER + "\n", "the empty value, then the account's helper")
        filled = git(job, "credential", "fill", ask=ask)
        self.assertIn("username=x-access-token\n", filled.stdout)
        self.assertIn(f"password={self.PLAIN_TOKEN}\n", filled.stdout)
        self.assertNotIn("machine-secret", filled.stdout)
        self.assertEqual(git(job, "config", "core.autocrlf").stdout.strip(), "false", "earlier entries are kept")
        other = git(job, "credential", "fill", ask="protocol=https\nhost=other.example.com\n\n")
        self.assertIn("password=machine-secret-0", other.stdout, "other hosts are not touched")
        # The helper prints the values as they are: echo in dash or macOS's /bin/sh would turn "\n" into a line
        # break, and "\c" would end the output there.
        odd = "Unit6Fake\\nprotocol=x\\tUnit6\\cFake%s\\\\"
        filled = git({**job, "BULLPEN_GIT_TOKEN": odd}, "credential", "fill", ask=ask)
        self.assertIn(f"\npassword={odd}\n", filled.stdout)
        # http:// to the host gets no helper at all (the account's token isn't sent in clear text): the account's
        # askpass answers, with nothing, which the host refuses.
        plain = git(job, "credential", "fill", ask="protocol=http\nhost=example.com\n\n")
        self.assertIn("\nusername=\npassword=\n", plain.stdout)
        self.assertNotIn("machine-secret", plain.stdout)
        self.assertNotIn(self.PLAIN_TOKEN, plain.stdout)
        # An Authorization header the repository (or the machine) sets for the host is cleared, for both schemes.
        repo = Path(self.tmp) / "checkout"
        git(base, "init", "-q", str(repo))
        for scheme in ("https", "http"):
            git(base, "-C", str(repo), "config", f"http.{scheme}://example.com/.extraheader",
                "AUTHORIZATION: basic machine-secret-1")
        for scheme in ("https", "http"):
            url = f"{scheme}://example.com/org/repo.git"
            header = ["-C", str(repo), "config", "--get-urlmatch", "http.extraHeader", url]
            self.assertIn("machine-secret-1", git(base, *header).stdout)
            self.assertEqual(git(job, *header).stdout, "\n", scheme)
        # A port in the host is its ssh port: https:// to the host name gets the account's token.
        env, _ = self.env({"host": "example.com:2222", "kind": "other", "token": self.PLAIN_TOKEN}, base)
        filled = git({**base, **env}, "credential", "fill", ask=ask)
        self.assertIn(f"password={self.PLAIN_TOKEN}\n", filled.stdout)
        # An account without a token: HTTPS to its host gets an empty login (which the host refuses) rather than the
        # machine user's identity.
        env, _ = self.env({"host": "example.com", "kind": "other", "ssh_key": str(self.key)}, base)
        filled = git({**base, **env}, "credential", "fill", ask=ask)
        self.assertIn("\nusername=\npassword=\n", filled.stdout)
        self.assertNotIn("machine-secret", filled.stdout)

    # -- masking ----------------------------------------------------------------------------
    def test_emit_masks_the_accounts_secrets(self):
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 61})
        job.emit("plain line")
        job.mask_values = [self.BOT_TOKEN[:12], self.BOT_TOKEN]  # the longer one first, whatever the order
        job.emit("push failed for " + self.BOT_TOKEN, "half " + self.BOT_TOKEN[:12] + " only")
        self.assertEqual(job.lines, ["plain line", "push failed for ***", "half *** only"])
        both = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 62, "job_token": TOKEN})
        both.mask_values = [self.PLAIN_TOKEN]
        both.emit(f"{TOKEN} {self.PLAIN_TOKEN}")
        self.assertEqual(both.lines, ["*** ***"])

    def test_run_logged_gets_the_env_and_masks_the_token(self):
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 63, "kind": "admin"})
        script = ("import os; print('pushing as ' + os.environ['BULLPEN_GIT_USER'] + ' with ' "
                  "+ os.environ['BULLPEN_GIT_TOKEN'])")
        code = job._run_logged([sys.executable, "-c", script], env_extra={"BULLPEN_GIT_USER": "x-access-token",
                                                                          "BULLPEN_GIT_TOKEN": self.PLAIN_TOKEN},
                               secrets=[self.PLAIN_TOKEN])
        self.assertEqual(code, 0)
        self.assertIn("pushing as x-access-token with ***", job.lines)
        self.assertNotIn(self.PLAIN_TOKEN, "\n".join(job.lines))
        # Every exact secret it is given, however short (the machine's own secrets need 6 characters or more).
        job.lines.clear()
        job._run_logged([sys.executable, "-c", "print('value k5x9z here')"], env_extra={"BULLPEN_GIT_USER": "ci"},
                        secrets=["k5x9z", ""])
        self.assertIn("value *** here", job.lines)

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_git_config_parameters_are_left_out(self):
        # git reads GIT_CONFIG_PARAMETERS (git -c settings) after the GIT_CONFIG_COUNT entries: a credential helper
        # in it would come back after the account's empty entry. A job, clone or pull with an account goes without
        # it, and its log says so; one without an account keeps it, as today.
        self.machine_git_config()
        os.environ["GIT_CONFIG_PARAMETERS"] = "'credential.helper=!echo machine-helper-P'"
        os.environ.pop("GIT_SSH", None)  # (machine_git_config's patch puts the environment back afterwards)
        note = ("⚠ GIT_CONFIG_PARAMETERS (settings given with git -c) was left out of the environment. Git reads it "
                "after the git account's settings, so its credential helpers would come back.")
        admin = self.admin_job(self.cfg(), 95)
        env, _ = self.env({"host": "example.com", "kind": "other", "ssh_key": str(self.key)}, dict(os.environ))
        self.assertEqual(admin._run_logged(["git", "config", "--list"], env_extra=env), 0)
        self.assertNotIn("machine-helper-P", "\n".join(admin.lines))
        self.assertIn("credential.https://example.com.helper=", admin.lines)
        admin.lines.clear()
        self.assertEqual(admin._run_logged(["git", "config", "--list"]), 0)
        self.assertIn("credential.helper=!echo machine-helper-P", admin.lines, "without an account, as today")

        job = {"id": 96, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        finished, lines, started = self.run_job(self.cfg(), job)
        self.assertEqual(finished["status"], "done", finished)
        self.assertNotIn("GIT_CONFIG_PARAMETERS", started[0][1])
        start = [i for i, ln in enumerate(lines) if ln.startswith("▶ ")][0]
        self.assertEqual(lines[start + 1:start + 3], ["Git account: bot (github.com)", note])
        finished, lines, started = self.run_job(self.cfg(), {**job, "id": 97,
                                                             "options": {"timeout_min": 2, "git_account": "-"}})
        self.assertEqual(started[0][1]["GIT_CONFIG_PARAMETERS"], os.environ["GIT_CONFIG_PARAMETERS"])
        self.assertNotIn(note, lines)

        clone = self.admin_job(self.cfg(), 98)
        with mock.patch.object(clone, "_run_logged", return_value=0), \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            out = clone._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git"})
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(clone.lines, ["Git account: bot (github.com)", note])
        self.assertFalse([p for p in ("95", "96", "97", "98") if (runner.JOBS_DIR / p).exists()])

    def test_a_short_git_secret_is_masked_in_a_job(self):
        # check_git_account refuses a token under 8 characters; whatever git_account_env gives is masked in full.
        cfg = self.cfg()
        job = {"id": 99, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        with mock.patch.object(runner, "git_account_env", return_value=({"BULLPEN_GIT_TOKEN": "k5x9z"}, ["k5x9z"])):
            finished, lines, _ = self.run_job(cfg, job)
        self.assertEqual(finished["result"], "push failed for ***")
        self.assertIn("stderr: stderr ***", lines)

    def test_a_secret_the_log_cuts_short_is_still_masked(self):
        # Masked as each line comes, before the log cuts it: a token cut in two would no longer match.
        cfg = self.cfg()
        cfg["agents"]["stream"] = {"preset": "text", "command": sys.executable, "args": ["-c", self.STREAM],
                                   "parser": "claude-stream", "prompt_via": "stdin"}
        job = {"id": 89, "agent": "stream", "project": "chat", "autonomy": "readonly", "prompt": "fix the remote",
               "options": {"timeout_min": 2, "git_account": "lab"}}
        finished, lines, started = self.run_job(cfg, job)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(started[0][1]["BULLPEN_GIT_TOKEN"], self.PLAIN_TOKEN)
        cut = [ln for ln in lines if "🔧" in ln or "✖" in ln]
        self.assertEqual(len(cut), 2, lines)
        self.assertTrue(all("***" in ln for ln in cut), cut)
        text = json.dumps([lines, finished])
        token = self.PLAIN_TOKEN
        self.assertFalse([token[i:i + 5] for i in range(len(token) - 4) if token[i:i + 5] in text],
                         "no piece of the token is left")

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_the_diff_is_masked_before_it_is_cut(self):
        repo = Path(self.tmp) / "notes"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        # "=== new file notes.txt ===" and 60 characters before it: the token starts at 87, the cut is at 100.
        (repo / "notes.txt").write_text("x" * 60 + self.PLAIN_TOKEN + "\n", encoding="utf-8")
        self.assertIn(self.PLAIN_TOKEN[:12], runner.capture_diff(str(repo), None, limit=100),
                      "masked after the cut, part of the token would stay")
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 64})
        job.mask_values = [self.PLAIN_TOKEN]
        diff = runner.capture_diff(str(repo), None, limit=100, mask=job.mask)
        self.assertIn("x" * 60 + "***", diff)
        self.assertNotIn(self.PLAIN_TOKEN[:5], diff)

    def test_a_live_answer_is_masked_before_it_is_cut(self):
        # A live session's answers go to the hub cut to LIVE_TURN_CHARS: cut first, a token at the cut would
        # stay there in part. Here the cut falls 8 characters into the token.
        hub = LiveHub()
        reply = "Self-test reply 1: "
        job = {"id": 100, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": self.PLAIN_TOKEN,
               "options": {"timeout_min": 2, "live": True, "idle_min": 1, "git_account": "lab"}}
        with mock.patch.dict(os.environ, {"BULLPEN_TEST_IDLE_SECONDS": "1"}), \
                mock.patch.object(runner, "LIVE_TURN_CHARS", len(reply) + 8), \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            finished, _ = run_with(hub, self.cfg(), job)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(hub.turns[1]["result"], reply + "***")
        text = json.dumps([hub.turns, hub.lines, finished])
        token = self.PLAIN_TOKEN
        self.assertFalse([token[i:i + 5] for i in range(len(token) - 4) if token[i:i + 5] in text],
                         "no piece of the token is left")

    # -- jobs -------------------------------------------------------------------------------
    def cfg(self, **extra) -> dict:
        cfg = make_cfg(self.tmp, {"echo": {"preset": "text", "command": sys.executable, "args": ["-c", self.ECHO],
                                           "prompt_via": "stdin"},
                                  "one": {"preset": "selftest", "env": dict(self.selftest_env)}})
        cfg["projects_root"] = str(Path(self.tmp) / "projects")
        cfg["git_accounts"] = {
            "bot": {"host": "github.com", "token": self.BOT_TOKEN, "name": "build-box bot", "email": "bot@example.com"},
            "personal": {"host": "github.com", "ssh_key": str(self.key)},
            "lab": {"host": "example.com", "token": self.PLAIN_TOKEN},
        }
        cfg["default_git_account"] = "bot"
        cfg.update(extra)
        return cfg

    def run_job(self, cfg: dict, data: dict, version=(2, 45)) -> tuple:
        """runner's run_job, also keeping each started program's environment (the agent's: not the git the
        runner asks about the job's git settings)."""
        hub = LocalHub()
        started: list = []
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if list(argv)[:1] != ["git"]:
                started.append((list(argv), dict(kwargs.get("env") or {})))
            return real_popen(argv, *args, **kwargs)

        job = runner.Job(argparse.Namespace(cfg=cfg, hub=hub), {"options": {"timeout_min": 2}, **data})
        with mock.patch.object(runner.subprocess, "Popen", popen), \
                mock.patch.object(runner, "git_version", return_value=version):
            job.run()
        return hub.finished, hub.lines, started

    def test_a_job_with_an_account_gets_it_and_never_shows_its_token(self):
        cfg = self.cfg()
        job = {"id": 81, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        finished, lines, started = self.run_job(cfg, job)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"], "push failed for ***")
        self.assertIn("stderr: stderr ***", lines)
        start = [i for i, ln in enumerate(lines) if ln.startswith("▶ ")][0]
        self.assertEqual(lines[start + 1], "Git account: bot (github.com)")
        self.assertNotIn(self.BOT_TOKEN, json.dumps([lines, finished]))
        (argv, env), = started
        self.assertEqual(argv, [sys.executable, "-c", self.ECHO], "the command line is what it always was")
        self.assertEqual((env["BULLPEN_GIT_TOKEN"], env["GH_TOKEN"], env["GIT_AUTHOR_NAME"]),
                         (self.BOT_TOKEN, self.BOT_TOKEN, "build-box bot"))
        self.assertEqual(env["GH_CONFIG_DIR"], str(runner.JOBS_DIR / "81" / "gh"))
        self.assertFalse((runner.JOBS_DIR / "81").exists(), "the job's folder is removed afterwards")

        self.assertFalse([ln for ln in lines if "signed out" in ln], "bot has a token")

        finished, lines, started = self.run_job(cfg, {**job, "id": 82, "options": {"timeout_min": 2,
                                                                                   "git_account": "personal"}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertIn("Git account: personal (github.com)", lines)
        self.assertIn("gh is signed out for this job: git account 'personal' has no token.", lines)
        env = started[0][1]
        self.assertEqual(env["GIT_SSH_COMMAND"], f'ssh -F "{slashed(runner.JOBS_DIR / "82" / "ssh_config")}"')
        self.assertNotIn("BULLPEN_GIT_TOKEN", env)
        self.assertFalse((runner.JOBS_DIR / "82").exists())

        finished, lines, started = self.run_job(cfg, {**job, "id": 83, "options": {"timeout_min": 2,
                                                                                   "git_account": "-"}})
        self.assertEqual(finished["result"], "push failed for none")
        self.assertFalse([ln for ln in lines if ln.startswith("Git account")])
        env = started[0][1]
        for var in self.JOB_VARS:
            self.assertEqual(env.get(var), os.environ.get(var), var)

    def test_a_job_whose_account_cant_be_used_fails_before_the_agent_starts(self):
        job = {"id": 84, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        cases = [
            (self.cfg(), {"git_account": "nope"}, (2, 45), "there is no valid git account 'nope' on build-box"),
            (self.cfg(default_git_account="gone"), {}, (2, 45),
             "the default git account (default_git_account) on build-box names 'gone', which is not a valid git "
             "account. Fix runner.json, or pick a git account or None for the job."),
            (self.cfg(project_git_accounts=["chat"]), {}, (2, 45),
             "project_git_accounts in runner.json on build-box isn't an object of project and git account names, so "
             "the default git account isn't known. Fix runner.json, or pick a git account or None for the job."),
            (self.cfg(), {"git_account": "bot"}, (2, 30), "git accounts need git 2.31 or later; this machine has 2.30"),
        ]
        missing = self.cfg(default_git_account="personal")
        missing["git_accounts"]["personal"]["ssh_key"] = str(Path(self.tmp) / "id_gone")
        cases.append((missing, {}, (2, 45), "the ssh key file of git account 'personal' doesn't exist"))
        for cfg, options, version, words in cases:
            finished, _, started = self.run_job(cfg, {**job, "options": {"timeout_min": 2, **options}}, version)
            self.assertEqual(finished["status"], "error", options)
            self.assertIn(words, finished["error"])
            self.assertNotIn("id_gone", finished["error"])
            self.assertEqual(started, [], "nothing was started")
            self.assertFalse((runner.JOBS_DIR / "84").exists())
        # An agent with its own git setup never uses a default, so a broken one doesn't stop it.
        cfg = self.cfg(default_git_account="gone")
        cfg["agents"]["echo"]["env"] = {"GIT_SSH_COMMAND": "ssh -i /keys/own"}
        finished, lines, _ = self.run_job(cfg, job)
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue([ln for ln in lines if ln.startswith("The default git account") and "GIT_SSH_COMMAND" in ln],
                        lines)

    def test_a_subtask_gets_no_git_account(self):
        cfg = self.cfg(accept_delegated_tasks=True)
        job = {"id": 85, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "Reply with pong"}
        for options in ({}, {"git_account": "bot"}):
            finished, lines, started = self.run_job(cfg, {**job, "options": {"timeout_min": 2, "delegated": True,
                                                                             **options}})
            self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
            self.assertFalse([ln for ln in lines if "Git account" in ln], lines)
            env = started[0][1]
            for var in self.JOB_VARS:
                self.assertEqual(env.get(var), os.environ.get(var), var)

    # -- clone and pull ---------------------------------------------------------------------
    def test_url_host(self):
        for url, host in (("https://github.com/org/repo.git", "github.com"),
                          ("https://GitHub.com:8443/org/repo", "github.com:8443"),
                          ("ssh://git@git.example.com:2222/org/repo.git", "git.example.com:2222"),
                          ("ssh://gitlab.example.com/org/repo", "gitlab.example.com"),
                          ("git@github.com:org/repo.git", "github.com"),
                          ("deploy@host.example.com:repo", "host.example.com"),
                          # The scheme's own port is the same as none.
                          ("https://github.com:443/org/repo", "github.com"),
                          ("ssh://git@git.example.com:22/org/repo.git", "git.example.com"),
                          ("https://git.example.com:22/org/repo", "git.example.com:22"),
                          ("ssh://git.example.com:443/org/repo", "git.example.com:443"),
                          ("https://git.example.com:4443/org/repo", "git.example.com:4443")):
            self.assertEqual(runner.url_host(url), host, url)
        for url in ("/srv/repo", "C:/repo", "http://github.com/x", "git://github.com/x", "github.com:org/repo",
                    "https:///github.com/x", "", "file:///srv/repo"):
            self.assertIsNone(runner.url_host(url), url)

    def test_the_port_in_an_accounts_host_is_its_ssh_port(self):
        # https:// goes to the host name on 443, where the account's HTTPS entries apply; ssh to the host and port
        # (22 when the account gives none). Anything else is another host: refused.
        problem = runner.git_account_url_problem
        key = {"kind": "other", "ssh_key": str(self.key)}
        for host, fine, other in (
                ("git.example.com:2222",
                 ("https://git.example.com/org/r.git", "https://git.example.com:443/org/r.git",
                  "ssh://git@git.example.com:2222/org/r.git"),
                 {"https://git.example.com:2222/org/r.git": ("git.example.com:2222", "git.example.com"),
                  "https://git.example.com:8443/org/r.git": ("git.example.com:8443", "git.example.com"),
                  "ssh://git@git.example.com/org/r.git": ("git.example.com", "git.example.com:2222"),
                  "git@git.example.com:org/r.git": ("git.example.com", "git.example.com:2222")}),
                ("git.example.com",
                 ("https://git.example.com/org/r.git", "ssh://git@git.example.com:22/org/r.git",
                  "ssh://git.example.com/org/r.git", "git@git.example.com:org/r.git"),
                 {"https://git.example.com:8443/org/r.git": ("git.example.com:8443", "git.example.com"),
                  "ssh://git@git.example.com:2222/org/r.git": ("git.example.com:2222", "git.example.com")}),
                ("git.example.com:22", ("ssh://git.example.com/org/r.git", "git@git.example.com:org/r.git"), {})):
            for url in fine:
                self.assertEqual(problem(url, "acct", {"host": host, **key}), "", (host, url))
            for url, (there, want) in other.items():
                self.assertEqual(problem(url, "acct", {"host": host, **key}),
                                 f"this URL is on {there}, but git account 'acct' is for {want}", (host, url))
        # Without a key, ssh to the account's host can't sign in: an ssh URL is refused upfront.
        token = {"host": "git.example.com", "kind": "other", "token": self.PLAIN_TOKEN}
        for url in ("ssh://git@git.example.com/org/r.git", "git@git.example.com:org/r.git"):
            self.assertEqual(problem(url, "bot", token), "git account 'bot' has no ssh key: use the https URL")
        self.assertEqual(problem("https://git.example.com/org/r.git", "bot", token), "")

    def admin_job(self, cfg: dict, job_id: int) -> "runner.Job":
        stub = argparse.Namespace(cfg=cfg, hub=LocalHub(), update_config=lambda change: change(cfg))
        return runner.Job(stub, {"id": job_id, "kind": "admin"})

    def machine_git_config(self, text: str = "") -> None:
        """The machine user's own git config, for the rest of the test: `text` alone (no system config)."""
        path = Path(self.tmp) / "machine.gitconfig"
        path.write_text(text, encoding="utf-8")
        patch = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(path), "GIT_CONFIG_NOSYSTEM": "1",
                                             "GIT_CONFIG_COUNT": "0"})
        patch.start()
        self.addCleanup(patch.stop)

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_clone_with_an_account(self):
        self.machine_git_config()
        cfg = self.cfg()
        seen: list = []

        def ran(argv, cwd=None, timeout_s=1200, env_extra=None, secrets=()):
            seen.append((argv, dict(env_extra or {}), list(secrets)))
            ssh = (env_extra or {}).get("GIT_SSH_COMMAND")
            if ssh:
                self.assertTrue((runner.JOBS_DIR / "86" / "ssh_config").is_file(), "there while git runs")
            return 0

        job = self.admin_job(cfg, 86)
        with mock.patch.object(job, "_run_logged", side_effect=ran), \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            out = job._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git"})
            self.assertEqual(out["status"], "done", out)
            out = job._project("project.clone", {"name": "other", "url": "git@github.com:org/other.git",
                                                 "git_account": "personal"})
            self.assertEqual(out["status"], "done", out)
            out = job._project("project.clone", {"name": "third", "url": "https://gitlab.com/org/third.git",
                                                 "git_account": "-"})
            self.assertEqual(out["status"], "done", out)
        (argv, env, secrets), (_, ssh_env, _), (_, none_env, _) = seen
        self.assertEqual(argv[:2], ["git", "clone"])
        self.assertEqual(env["BULLPEN_GIT_TOKEN"], self.BOT_TOKEN, "the machine's default account")
        self.assertIn(self.BOT_TOKEN, secrets)
        self.assertIn("GIT_SSH_COMMAND", ssh_env)
        self.assertEqual(none_env, {}, "'-': none, as today")
        self.assertIn("Git account: bot (github.com)", job.lines)
        self.assertFalse((runner.JOBS_DIR / "86").exists(), "the folder for the ssh config is removed")

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_clone_checks_the_url_git_would_use(self):
        # The machine's url.<base>.insteadOf settings rewrite a URL before git uses it: over ssh, or to another
        # host. The check is on where git would go, not on what was typed.
        self.machine_git_config('[url "git@github.com:"]\n\tinsteadOf = https://github.com/\n'
                                '[url "https://gitlab.com/"]\n\tinsteadOf = https://example.com/\n')
        cfg = self.cfg()
        job = self.admin_job(cfg, 90)
        no_key = "git account 'bot' has no ssh key: use the https URL"
        with mock.patch.object(job, "_run_logged", return_value=0) as ran, \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            typed_ssh = job._project("project.clone", {"name": "myapp", "url": "git@github.com:org/myapp.git"})
            made_ssh = job._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git"})
            moved = job._project("project.clone", {"name": "myapp", "url": "https://example.com/org/myapp.git",
                                                   "git_account": "lab"})
            ran.assert_not_called()
            self.assertEqual(typed_ssh, {"status": "error", "error": no_key}, "a token account can't sign in over ssh")
            self.assertEqual(made_ssh, typed_ssh)
            self.assertEqual(moved, {"status": "error",
                                     "error": "this URL is on gitlab.com, but git account 'lab' is for example.com"})
            out = job._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git",
                                                 "git_account": "personal"})
            self.assertEqual(out["status"], "done", "an account with a key can go over ssh")
            # A repository the runner happens to run in doesn't count: git clone reads none's settings.
            here = Path(self.tmp) / "runner-cwd"
            subprocess.run(["git", "init", "-q", str(here)], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(here), "config", "url.https://example.com/.insteadOf",
                            "https://gitlab.com/"], check=True, capture_output=True)
            before = os.getcwd()
            os.chdir(here)
            try:
                out = job._project("project.clone", {"name": "other", "url": "https://gitlab.com/org/other.git",
                                                     "git_account": "lab"})
            finally:
                os.chdir(before)
            self.assertEqual(out, {"status": "error",
                                   "error": "this URL is on gitlab.com, but git account 'lab' is for example.com"})
        self.assertEqual(len(ran.call_args_list), 1)
        self.assertFalse((runner.JOBS_DIR / "90").exists())

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_clone_on_another_host_is_refused(self):
        job = self.admin_job(self.cfg(), 87)
        with mock.patch.object(job, "_run_logged") as ran, \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            picked = job._project("project.clone", {"name": "myapp", "url": "https://gitlab.com/org/myapp.git",
                                                    "git_account": "bot"})
            default = job._project("project.clone", {"name": "myapp", "url": "ssh://gitlab.com/org/myapp.git"})
            unknown = job._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git",
                                                     "git_account": "nope"})
        ran.assert_not_called()
        self.assertEqual(picked, {"status": "error",
                                  "error": "this URL is on gitlab.com, but git account 'bot' is for github.com"})
        self.assertEqual(default, picked)
        self.assertEqual(unknown, {"status": "error", "error": "there is no valid git account 'nope' on build-box"})

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_pull_checks_the_url_git_pull_uses(self):
        self.machine_git_config()
        repo = Path(self.tmp) / "myapp"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], check=True,
                                capture_output=True, text=True).stdout.strip()
        cfg = self.cfg()
        cfg["projects"]["myapp"] = str(repo)
        job = self.admin_job(cfg, 88)

        def run(*args: str) -> None:
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

        def pull(url: str | None, **params) -> tuple:
            subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], capture_output=True)
            if url:
                run("remote", "add", "origin", url)
            with mock.patch.object(job, "_run_logged", return_value=0) as ran, \
                    mock.patch.object(runner, "git_version", return_value=(2, 45)):
                out = job._project("project.pull", {"name": "myapp", **params})
            return out, ran

        out, ran = pull("https://gitlab.com/org/myapp.git")
        self.assertEqual(out, {"status": "error",
                               "error": "this URL is on gitlab.com, but git account 'bot' is for github.com"})
        ran.assert_not_called()
        out, ran = pull("https://github.com/org/myapp.git")
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(ran.call_args.kwargs["env_extra"]["BULLPEN_GIT_TOKEN"], self.BOT_TOKEN)
        out, ran = pull(None)
        self.assertEqual(out, {"status": "error", "error": "git can't say which remote 'myapp' pulls from, so the "
                                                           "runner can't check that it is on github.com, the host of "
                                                           "git account 'bot'"})
        ran.assert_not_called()
        # A token account can't sign in over ssh: an ssh origin is refused.
        out, ran = pull("git@github.com:org/myapp.git")
        self.assertEqual(out["status"], "error")
        self.assertIn("has no ssh key", out["error"])
        ran.assert_not_called()
        # git pull fetches from the current branch's remote, which needn't be origin; and after insteadOf.
        run("remote", "add", "upstream", "https://gitlab.com/org/myapp.git")
        run("config", f"branch.{branch}.remote", "upstream")
        out, ran = pull("https://github.com/org/myapp.git")
        self.assertEqual(out, {"status": "error",
                               "error": "this URL is on gitlab.com, but git account 'bot' is for github.com"})
        ran.assert_not_called()
        # And the other way round: origin elsewhere, the branch's remote on the account's host.
        run("remote", "set-url", "upstream", "https://github.com/org/myapp.git")
        out, ran = pull("https://gitlab.com/org/myapp.git")
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(ran.call_args.kwargs["env_extra"]["BULLPEN_GIT_TOKEN"], self.BOT_TOKEN)
        # No branch remote and no origin: the only remote, as git pull takes it.
        run("config", "--unset", f"branch.{branch}.remote")
        run("remote", "set-url", "upstream", "https://gitlab.com/org/myapp.git")
        out, ran = pull(None)
        self.assertEqual(out, {"status": "error",
                               "error": "this URL is on gitlab.com, but git account 'bot' is for github.com"})
        ran.assert_not_called()
        run("remote", "remove", "upstream")
        run("config", "url.ssh://github.com/.insteadOf", "https://github.com/")
        out, ran = pull("https://github.com/org/myapp.git")
        self.assertIn("has no ssh key", out["error"])
        ran.assert_not_called()
        run("config", "--unset", "url.ssh://github.com/.insteadOf")
        # Credentials in the remote's URL would be used instead of the account's: refused, without the URL.
        import clone_url_cases
        out, ran = pull(clone_url_cases.BAD[0], git_account="lab")
        self.assertEqual(out["status"], "error")
        self.assertIn(runner.NO_URL_CREDENTIALS, out["error"])
        self.assertNotIn("u:p", out["error"])
        ran.assert_not_called()
        out, ran = pull("https://gitlab.com/org/myapp.git", git_account="-")
        self.assertEqual(out["status"], "done", "no account: the pull runs as today")
        self.assertFalse(ran.call_args.kwargs.get("env_extra"))
        self.assertFalse((runner.JOBS_DIR / "88").exists())

    # -- an ssh command of the machine's own --------------------------------------------------
    # git runs GIT_SSH_COMMAND before core.sshCommand and GIT_SSH, so with a git account it doesn't run the ssh
    # command those name (Windows' own OpenSSH, for Windows' ssh agent, say). The log says so, without it.
    OWN_SSH = "C:/Windows/System32/OpenSSH/ssh.exe"
    SSH_NOTE = ("⚠ git is set here to use an ssh command of its own ({}). With a git account, git uses the ssh on "
                "its PATH with the account's settings instead, so an ssh key that only that command's ssh agent "
                "holds can't be used.")

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_an_ssh_command_git_wont_use_is_named(self):
        def gitconfig(name: str, text: str) -> str:
            path = Path(self.tmp) / name
            path.write_text(text, encoding="utf-8")
            return str(path)

        base = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        base.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1")
        plain = {**base, "GIT_CONFIG_GLOBAL": gitconfig("plain.gitconfig", "")}
        own = {**base, "GIT_CONFIG_GLOBAL": gitconfig("own.gitconfig", f"[core]\n\tsshCommand = {self.OWN_SSH}\n")}
        here = Path(self.tmp) / "plain"
        here.mkdir()
        self.assertEqual(runner.git_ssh_program_note(here, plain), "")
        self.assertEqual(runner.git_ssh_program_note(here, own), self.SSH_NOTE.format("core.sshCommand"))
        self.assertEqual(runner.git_ssh_program_note(here, {**plain, "GIT_SSH": "plink"}),
                         self.SSH_NOTE.format("GIT_SSH"))
        self.assertEqual(runner.git_ssh_program_note(here, {**own, "GIT_SSH": "plink"}),
                         self.SSH_NOTE.format("core.sshCommand, GIT_SSH"))
        # A repository's own setting counts where git runs in it (a job, a pull), not for a clone's empty folder.
        repo = Path(self.tmp) / "myapp"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=plain)
        subprocess.run(["git", "-C", str(repo), "config", "core.sshCommand", self.OWN_SSH], check=True,
                       capture_output=True, env=plain)
        self.assertEqual(runner.git_ssh_program_note(repo, plain), self.SSH_NOTE.format("core.sshCommand"))
        (repo / "jobs" / "93").mkdir(parents=True)
        self.assertEqual(runner.git_ssh_program_note(repo / "jobs" / "93", plain, alone=True), "")
        self.assertEqual(runner.git_ssh_program_note(repo / "jobs" / "93", own, alone=True),
                         self.SSH_NOTE.format("core.sshCommand"))

    @unittest.skipUnless(runner.shutil.which("git"), "needs git")
    def test_a_job_clone_and_pull_say_they_dont_use_the_machines_ssh_command(self):
        self.machine_git_config(f"[core]\n\tsshCommand = {self.OWN_SSH}\n")
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("GIT_SSH", None)
        note = self.SSH_NOTE.format("core.sshCommand")
        cfg = self.cfg()
        job = {"id": 91, "agent": "echo", "project": "chat", "autonomy": "readonly", "prompt": "push it"}
        finished, lines, started = self.run_job(cfg, job)
        self.assertEqual(finished["status"], "done", finished)
        start = [i for i, ln in enumerate(lines) if ln.startswith("▶ ")][0]
        self.assertEqual(lines[start + 1:start + 3], ["Git account: bot (github.com)", note])
        self.assertNotIn("System32", json.dumps(lines))
        self.assertEqual(started[0][1]["GIT_SSH_COMMAND"], f'ssh -F "{slashed(runner.JOBS_DIR / "91" / "ssh_config")}"')
        # No account: git runs the machine's ssh command, as always, and there is nothing to say.
        finished, lines, _ = self.run_job(cfg, {**job, "id": 92, "options": {"timeout_min": 2, "git_account": "-"}})
        self.assertEqual(finished["status"], "done", finished)
        self.assertNotIn(note, lines)

        clone = self.admin_job(cfg, 93)
        with mock.patch.object(clone, "_run_logged", return_value=0), \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            out = clone._project("project.clone", {"name": "myapp", "url": "https://github.com/org/myapp.git"})
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(clone.lines, ["Git account: bot (github.com)", note])

        # A pull: the repository's own setting counts.
        self.machine_git_config()
        repo = Path(self.tmp) / "pulled"
        subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        for args in (("remote", "add", "origin", "https://github.com/org/pulled.git"),
                     ("config", "core.sshCommand", self.OWN_SSH)):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        cfg["projects"]["pulled"] = str(repo)
        pull = self.admin_job(cfg, 94)
        with mock.patch.object(pull, "_run_logged", return_value=0), \
                mock.patch.object(runner, "git_version", return_value=(2, 45)):
            out = pull._project("project.pull", {"name": "pulled"})
        self.assertEqual(out["status"], "done", out)
        self.assertEqual(pull.lines, ["Git account: bot (github.com)", note])
        self.assertFalse([p for p in ("91", "93", "94") if (runner.JOBS_DIR / p).exists()])


class SubtaskRefusal(TempDirCase):
    def test_only_where_the_machine_takes_subtasks(self):
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env}})
        job = {"id": 20, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "Reply with pong",
               "options": {"timeout_min": 2, "delegated": True}}
        for value in (None, False, "yes"):
            if value is not None:
                cfg["accept_delegated_tasks"] = value
            finished, _, started = run_job(cfg, job)
            self.assertEqual(finished["status"], "error")
            self.assertEqual(finished["error"], "This machine doesn't take subtasks from other agents "
                                                "(accept_delegated_tasks in its runner.json).")
            self.assertEqual(started, [], "nothing was started")
        finished, _, _ = run_job(cfg, {**job, "options": {"timeout_min": 2}, "delegated_by": 19})
        self.assertEqual(finished["status"], "error", "the hub's delegated_by counts too")
        cfg["accept_delegated_tasks"] = True
        finished, _, started = run_job(cfg, job)
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"))
        cfg["accept_delegated_tasks"] = False
        finished, _, _ = run_job(cfg, {**job, "options": {"timeout_min": 2}})
        self.assertEqual(finished["status"], "done", "other jobs run as before")

    def test_heartbeat_says_whether_the_machine_takes_subtasks(self):
        path = Path(self.tmp) / "runner.json"
        cfg = make_cfg(self.tmp, {})
        path.write_text(json.dumps(cfg), encoding="utf-8")
        self.assertIs(runner.Runner(path).heartbeat_payload()["info"]["accept_delegated_tasks"], False)
        path.write_text(json.dumps({**cfg, "accept_delegated_tasks": True, "tags": ["GPU"]}), encoding="utf-8")
        payload = runner.Runner(path).heartbeat_payload()
        self.assertIs(payload["info"]["accept_delegated_tasks"], True)
        self.assertEqual(payload["tags"], sorted(["gpu", runner.PLATFORM]))


class GeminiDefaults(TempDirCase):
    def test_where_gemini_looks(self):
        where = runner.gemini_system_defaults_path
        self.assertEqual(where({"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": "/srv/d.json",
                                "GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/opt/gem/settings.json"}), "/srv/d.json")
        self.assertEqual(where({"GEMINI_CLI_SYSTEM_SETTINGS_PATH": "/opt/gem/settings.json"}, windows=False, macos=False),
                         "/opt/gem/system-defaults.json", "next to the system settings file")
        self.assertEqual(where({}, windows=False, macos=False), "/etc/gemini-cli/system-defaults.json")
        self.assertEqual(where({}, windows=False, macos=True),
                         "/Library/Application Support/GeminiCli/system-defaults.json")
        self.assertEqual(where({}, windows=True, macos=False), "C:\\ProgramData\\gemini-cli\\system-defaults.json")
        self.assertEqual(where({"GEMINI_CLI_SYSTEM_SETTINGS_PATH": "D:\\gem\\settings.json"}, windows=True, macos=False),
                         "D:\\gem\\system-defaults.json")

    def test_a_copy_of_the_machine_defaults_with_the_job_tools(self):
        source = Path(self.tmp) / "system-defaults.json"
        source.write_text('{\n  // set by the administrator\n  "general": {"preferredEditor": "vim"},\n'
                          '  "mcpServers": {"other": {"command": "x"}, "bullpenjob": {"url": "http://elsewhere"}},\n}\n',
                          encoding="utf-8")
        entry = {"url": "http://192.168.100.20:8080/mcp/job"}
        data = runner.gemini_job_defaults(str(source), entry)
        self.assertEqual(data, {"general": {"preferredEditor": "vim"},
                                "mcpServers": {"other": {"command": "x"}, "bullpenjob": entry}})
        alone = {"mcpServers": {"bullpenjob": entry}}
        self.assertEqual(runner.gemini_job_defaults(str(Path(self.tmp) / "missing.json"), entry), alone)
        self.assertEqual(runner.gemini_job_defaults(self.tmp, entry), alone, "a folder isn't a settings file")
        for text in ("not json {", "[1, 2]", ""):
            source.write_text(text, encoding="utf-8")
            self.assertEqual(runner.gemini_job_defaults(str(source), entry), alone, text)
        source.write_bytes(b'{"x": "\xff"}')
        self.assertEqual(runner.gemini_job_defaults(str(source), entry), alone, "not UTF-8")
        source.write_text('{"mcpServers": ["odd"], "ui": {"theme": "x"}}', encoding="utf-8")
        self.assertEqual(runner.gemini_job_defaults(str(source), entry), {"mcpServers": {"bullpenjob": entry},
                                                                          "ui": {"theme": "x"}})
        with mock.patch.object(runner, "GEMINI_DEFAULTS_MAX_BYTES", 10):
            self.assertEqual(runner.gemini_job_defaults(str(source), entry), alone, "too big to be one")

    def test_prepare_writes_a_private_defaults_file(self):
        source = Path(self.tmp) / "defaults.json"
        source.write_text('{"general": {"vimMode": true}}', encoding="utf-8")
        before = source.read_bytes()
        cfg = make_cfg(self.tmp, {"gem": {"preset": "gemini", "env": {"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(source)}}},
                       hub_url="http://192.168.100.20:8080/")
        cfg.update(cf_access_client_id="cid.access", cf_access_client_secret="csecret-123")
        job = {"id": 44, "agent": "gem", "job_token": TOKEN, "job_tools": ["job_info", "approve", "list_machines"]}
        self.gemini_reads_it()
        args, env, folder = runner.prepare_job_tools(cfg, runner.agent_spec(cfg, "gem"), job, "build-box")
        self.addCleanup(lambda: folder and runner.shutil.rmtree(folder, ignore_errors=True))
        target = runner.JOBS_DIR / "44" / "gemini-system-defaults.json"
        self.assertEqual((args, env, folder), ([], {"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(target)},
                                               runner.JOBS_DIR / "44"))
        data = json.loads(target.read_text(encoding="utf-8"))
        self.assertEqual(data["general"], {"vimMode": True}, "the machine's own defaults still apply")
        self.assertEqual(data["mcpServers"]["bullpenjob"], {
            "url": "http://192.168.100.20:8080/mcp/job", "type": "http",
            "headers": {"Authorization": "Bearer " + TOKEN, "X-Bullpen-Agent": "gem@build-box job 44",
                        "CF-Access-Client-Id": "cid.access", "CF-Access-Client-Secret": "csecret-123"},
            "trust": True, "timeout": 120000, "includeTools": ["job_info", "list_machines"]})
        self.assertEqual(source.read_bytes(), before, "the machine's file is only read")
        if POSIX:
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        # Without a variable of the agent's own, the runner's is used.
        other = Path(self.tmp) / "runner-defaults.json"
        other.write_text('{"ui": {"theme": "GitHub"}}', encoding="utf-8")
        cfg["agents"]["gem"].pop("env")
        with mock.patch.dict(os.environ, {"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(other)}):
            _, env, folder2 = runner.prepare_job_tools(cfg, runner.agent_spec(cfg, "gem"), {**job, "id": 45}, "m")
        self.addCleanup(lambda: runner.shutil.rmtree(folder2, ignore_errors=True))
        data = json.loads(Path(env["GEMINI_CLI_SYSTEM_DEFAULTS_PATH"]).read_text(encoding="utf-8"))
        self.assertEqual(data["ui"], {"theme": "GitHub"})

    def gemini_reads_it(self) -> None:
        """Pretend Gemini CLI would read the job's settings file (as when root owns it and every folder above
        it): the runner's copy of Gemini CLI's check passes, and so does the stand-in's."""
        for name in ("gemini_job_tools_problem", "gemini_settings_problem"):
            patch = mock.patch.object(runner, name, return_value=None)
            patch.start()
            self.addCleanup(patch.stop)
        self.selftest_env = {**self.selftest_env, "SELFTEST_GEMINI_DEFAULTS": "trusted"}

    def gemfake(self, hub_url: str) -> dict:
        env = {**self.selftest_env, "GEMINI_CLI_SYSTEM_DEFAULTS_PATH": str(Path(self.tmp) / "none.json")}
        return make_cfg(self.tmp, {"gem": {"preset": "gemini", "command": sys.executable,
                                           "args": [str(ROOT / "runner" / "selftest_agent.py"), "--gemini"],
                                           "env": env}}, hub_url=hub_url)

    def test_a_gemini_job_gets_the_tools_through_its_settings(self):
        fake = FakeJobTools(tools=DELEGATION_TOOLS, answers={
            "list_machines": lambda a: {"ok": True, "machines": [{"name": "beta"}]}})
        self.addCleanup(fake.close)
        self.gemini_reads_it()
        finished, lines, started = run_job(self.gemfake(fake.url), {
            "id": 7, "agent": "gem", "project": "chat", "autonomy": "readonly",
            "prompt": "SELFTEST: job-info\nSELFTEST: machines\nSELFTEST: leak-token\nhello",
            "job_token": TOKEN, "job_tools": list(DELEGATION_TOOLS)})
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue(finished["result"].startswith(
            "job-info: #7 on build-box tools=job_info,list_machines,delegate_task,task_status,cancel_task\n"
            "machines: beta\nleaked\n"), finished["result"])
        self.assertEqual(started, [[sys.executable, str(ROOT / "runner" / "selftest_agent.py"), "--gemini",
                                    "--approval-mode", "default"]], "Gemini's command line doesn't change")
        self.assertIn("stderr: token=***", lines)
        self.assertNotIn(TOKEN, json.dumps([lines, finished]))
        self.assertFalse((runner.JOBS_DIR / "7").exists(), "the job's folder is removed afterwards")
        seen = [(c["method"], c["tool"]) for c in fake.calls]
        self.assertEqual(seen, [("tools/list", None), ("tools/call", "job_info"), ("tools/list", None),
                                ("tools/call", "list_machines")])
        self.assertTrue(all(c["auth"] == "Bearer " + TOKEN for c in fake.calls))
        self.assertEqual(fake.calls[-1]["agent"], "gem@build-box job 7")

    def test_unreachable_tools_are_left_out(self):
        self.gemini_reads_it()
        finished, lines, _ = run_job(self.gemfake(f"http://127.0.0.1:{free_port()}"), {
            "id": 8, "agent": "gem", "project": "chat", "autonomy": "readonly", "prompt": "SELFTEST: job-info",
            "job_token": TOKEN, "job_tools": list(DELEGATION_TOOLS)})
        self.assertEqual(finished["status"], "done", finished)
        self.assertTrue(any(ln.startswith("⚠ Subtask tools unavailable: ") for ln in lines), lines)
        self.assertTrue(finished["result"].startswith("job-info: not available (no job tools)"), finished["result"])
        self.assertFalse((runner.JOBS_DIR / "8").exists())


    def test_no_tools_where_gemini_would_skip_the_file(self):
        fake = FakeJobTools(tools=DELEGATION_TOOLS)
        self.addCleanup(fake.close)
        finished, lines, _ = run_job(self.gemfake(fake.url), {
            "id": 9, "agent": "gem", "project": "chat", "autonomy": "readonly", "prompt": "SELFTEST: job-info",
            "job_token": TOKEN, "job_tools": list(DELEGATION_TOOLS)})
        self.assertEqual(finished["status"], "done", finished)
        note = next((ln for ln in lines if ln.startswith("⚠ Subtask tools unavailable: ")), "")
        self.assertIn("Gemini CLI reads", note)
        self.assertTrue(finished["result"].startswith("job-info: not available (no job tools)"), finished["result"])
        self.assertFalse(any("Security Warning" in ln for ln in lines),
                         "the variable isn't set, so Gemini CLI has no file of ours to skip")
        self.assertFalse((runner.JOBS_DIR / "9").exists())
        self.assertEqual(fake.calls, [], "the hub isn't even asked")

    def test_a_skipped_file_on_stderr_is_reported(self):
        for name in ("gemini_job_tools_problem", "gemini_settings_problem"):
            patch = mock.patch.object(runner, name, return_value=None)  # the runner thinks it will be read ...
            patch.start()
            self.addCleanup(patch.stop)
        fake = FakeJobTools(tools=DELEGATION_TOOLS)
        self.addCleanup(fake.close)
        finished, lines, _ = run_job(self.gemfake(fake.url), {  # ... but the stand-in, like Gemini CLI, skips it
            "id": 10, "agent": "gem", "project": "chat", "autonomy": "readonly", "prompt": "SELFTEST: job-info",
            "job_token": TOKEN, "job_tools": list(DELEGATION_TOOLS)})
        self.assertEqual(finished["status"], "done", finished)
        self.assertIn("⚠ Subtask tools unavailable: Gemini CLI skipped the job's settings file (its warning is above)",
                      lines)
        self.assertTrue(finished["result"].startswith("job-info: not available (no job tools)"), finished["result"])

    def test_the_check_is_gemini_clis_own(self):
        def stats(table):
            def stat_fn(path):
                if path not in table:
                    raise FileNotFoundError(path)
                uid, mode = table[path]
                return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))
            return stat_fn

        good = {"/": (0, 0o40755), "/etc": (0, 0o40755), "/etc/gemini-cli": (0, 0o40755),
                "/etc/gemini-cli/d.json": (0, 0o100644)}
        check = runner.gemini_settings_problem
        kw = {"windows": False, "realpath": lambda p: p}
        self.assertIsNone(check("/etc/gemini-cli/d.json", stat_fn=stats(good), lstat_fn=stats(good), **kw))
        for path, (uid, mode), why in (("/etc/gemini-cli/d.json", (1000, 0o100600), "not owned by root"),
                                       ("/etc/gemini-cli", (0, 0o40775), "writable by group or others"),
                                       ("/etc", (0, 0o40757), "writable by group or others"),
                                       ("/", (1000, 0o40755), "not owned by root")):
            table = {**good, path: (uid, mode)}
            said = check("/etc/gemini-cli/d.json", stat_fn=stats(table), lstat_fn=stats(table), **kw) or ""
            self.assertIn(f"{path} is {why}", said)
        linked = {**good, "/etc/gemini-cli": (1000, 0o120777)}
        self.assertIn("/etc/gemini-cli is a link not owned by root",
                      check("/etc/gemini-cli/d.json", stat_fn=stats(good), lstat_fn=stats(linked), **kw) or "")
        real = {**good, "/srv": (0, 0o40777), "/srv/d.json": (0, 0o100644)}
        self.assertIn("/srv is writable by group or others",
                      check("/etc/gemini-cli/d.json", stat_fn=stats(real), lstat_fn=stats(real), windows=False,
                            realpath=lambda p: "/srv/d.json") or "", "where a link leads counts too")
        self.assertIn("Windows", check("C:\\ProgramData\\gemini-cli\\d.json", windows=True) or "")
        if POSIX:
            target = Path(self.tmp) / "d.json"
            target.write_text("{}", encoding="utf-8")
            self.assertIsNotNone(check(str(target)), "a temporary folder is writable by everyone")

    def test_only_root_can_hand_gemini_the_tools(self):
        if not POSIX:
            self.assertIn("Windows", runner.gemini_job_tools_problem() or "")
            return
        with mock.patch.object(runner.os, "geteuid", return_value=1000):
            self.assertIn("doesn't run as root", runner.gemini_job_tools_problem() or "")
        with mock.patch.object(runner.os, "geteuid", return_value=0), \
                mock.patch.object(runner, "gemini_settings_problem", return_value=None) as checked:
            self.assertIsNone(runner.gemini_job_tools_problem())
        self.assertTrue(str(runner.JOBS_DIR).startswith(checked.call_args[0][0]), checked.call_args)
        with mock.patch.object(runner.os, "geteuid", return_value=0):
            self.assertIsNotNone(runner.gemini_job_tools_problem(), "the units' home is in a temporary folder")


class SelftestDelegation(TempDirCase):
    def run_agent(self, *args: str, prompt: str, env=None) -> str:
        full = dict(os.environ, SELFTEST_DELAY="0", PYTHONIOENCODING="utf-8")
        full.pop("GEMINI_CLI_SYSTEM_DEFAULTS_PATH", None)
        full.update(env or {})
        res = subprocess.run([sys.executable, str(ROOT / "runner" / "selftest_agent.py"), *args], input=prompt,
                             capture_output=True, text=True, encoding="utf-8", cwd=self.tmp, env=full, timeout=60)
        events = [json.loads(ln) for ln in res.stdout.splitlines() if ln.strip()]
        # The answer: Claude's result, or Gemini's assistant message.
        return next(e.get("result") if e.get("type") == "result" else e["content"] for e in events
                    if (e.get("type") == "result" and "result" in e) or e.get("type") == "message")

    def config(self, fake: FakeJobTools, **extra) -> str:
        path = Path(self.tmp) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"bullpenjob": {
            "type": "http", "url": fake.url + "/mcp/job", "headers": {"Authorization": "Bearer " + TOKEN}, **extra}}}),
            encoding="utf-8")
        return str(path)

    def fake(self, **answers) -> FakeJobTools:
        fake = FakeJobTools(tools=DELEGATION_TOOLS, answers=answers)
        self.addCleanup(fake.close)
        return fake

    def calls(self, fake: FakeJobTools, tool: str) -> list:
        return [c for c in fake.calls if c["tool"] == tool]

    def test_delegate_args(self):
        args, wait = selftest_agent.delegate_args(
            " machine=beta agent=three wait=5 timeout_min=3 bogus=1 prompt=SELFTEST: delegate machine=alpha prompt=x")
        self.assertEqual(args, {"prompt": "SELFTEST: delegate machine=alpha prompt=x", "machine": "beta",
                                "agent": "three", "timeout_min": 3})
        self.assertEqual(wait, 5.0)
        self.assertEqual(selftest_agent.delegate_args("agent=a"), ({"prompt": "", "agent": "a"}, 60.0))
        self.assertEqual(selftest_agent.delegate_args("tag=kali agent=a wait=soon prompt= hi there ")[0:2],
                         ({"prompt": "hi there", "tag": "kali", "agent": "a"}, 60.0))

    def test_delegate_waits_for_the_answer(self):
        looks: list = []

        def status(args):
            looks.append(args)
            if len(looks) == 1:
                return {"ok": True, "job_id": 50, "status": "running"}
            return {"ok": True, "job_id": 50, "status": "done", "result": "\npong\nmore"}

        fake = self.fake(delegate_task=lambda a: {"ok": True, "job_id": 50, "status": "queued", "target": "beta"},
                         task_status=status)
        result = self.run_agent("--mcp-config", self.config(fake),
                                prompt="SELFTEST: delegate machine=beta agent=three prompt=Reply with pong\nHello")
        self.assertEqual(result.splitlines()[:4],
                         ["delegated: #50", "subtask status: done", "subtask result: pong", ""], result)
        self.assertEqual(self.calls(fake, "delegate_task")[0]["arguments"],
                         {"prompt": "Reply with pong", "machine": "beta", "agent": "three"})
        self.assertEqual(looks, [{"job_id": 50, "wait_s": 60}, {"job_id": 50, "wait_s": 60}])
        self.assertTrue(all((c["meta"] or {}).get("progressToken") for c in self.calls(fake, "task_status")),
                        "a long wait asks for progress notifications")

    def test_delegate_without_waiting_and_with_errors(self):
        fake = self.fake(delegate_task=lambda a: {"ok": True, "job_id": 51, "status": "queued"},
                         task_status=lambda a: {"ok": True, "job_id": 51, "status": "queued"})
        result = self.run_agent("--mcp-config", self.config(fake), prompt="SELFTEST: delegate agent=x wait=0 prompt=y")
        self.assertEqual(result.splitlines()[:2], ["delegated: #51", "subtask status: queued"])
        self.assertEqual([c["arguments"] for c in self.calls(fake, "task_status")], [{"job_id": 51, "wait_s": 0}])
        self.assertIsNone(self.calls(fake, "task_status")[0]["meta"])
        fake = self.fake(delegate_task=lambda a: {"ok": False, "error": "No machine with the accepted tag 'kali'\nhas it."},
                         task_status=lambda a: {"ok": False, "error": "This job has ended."})
        result = self.run_agent("--mcp-config", self.config(fake), prompt="SELFTEST: delegate tag=kali agent=x prompt=y")
        self.assertEqual(result.splitlines()[0], "delegate: error (No machine with the accepted tag 'kali' has it.)")
        fake = self.fake(delegate_task=lambda a: {"ok": True, "job_id": 52, "status": "queued"},
                         task_status=lambda a: {"ok": False, "error": "This job has ended."})
        result = self.run_agent("--mcp-config", self.config(fake), prompt="SELFTEST: delegate agent=x prompt=y")
        self.assertEqual(result.splitlines()[:2], ["delegated: #52", "subtask status: error (This job has ended.)"])
        fake = self.fake(delegate_task=lambda a: {"ok": True, "job_id": 53, "status": "queued"},
                         task_status=lambda a: {"ok": True, "job_id": 53, "status": "error", "error_kind": "claude_limit",
                                                "error": "You've hit your 5-hour limit\nresets 3pm"})
        result = self.run_agent("--mcp-config", self.config(fake), prompt="SELFTEST: delegate agent=x prompt=y")
        self.assertEqual(result.splitlines()[:3], ["delegated: #53", "subtask status: error",
                                                   "subtask error: You've hit your 5-hour limit"])

    def test_delegate_many(self):
        made: list = []

        def delegate(args):
            made.append(args)
            if len(made) > 2:
                return {"ok": False, "error": "This job has already handed out 2 subtasks (the most allowed)."}
            return {"ok": True, "job_id": 59 + len(made), "status": "queued"}

        fake = self.fake(delegate_task=delegate)
        result = self.run_agent("--mcp-config", self.config(fake),
                                prompt="SELFTEST: delegate-many 3 machine=beta agent=three prompt=Reply with pong")
        self.assertEqual(result.splitlines()[:2], ["delegated: #60,#61", "delegate: error (This job has already "
                                                                          "handed out 2 subtasks (the most allowed).)"])
        self.assertEqual(made, [{"prompt": "Reply with pong", "machine": "beta", "agent": "three"}] * 3)
        self.assertEqual(self.calls(fake, "task_status"), [], "no waiting")

    def test_machines_and_status(self):
        def status(args):
            if args.get("job_id") == 50:
                return {"ok": True, "job_id": 50, "status": "done", "result": "pong"}
            return {"ok": False, "error": f"Job #{args.get('job_id')} isn't one of your subtasks."}

        fake = self.fake(list_machines=lambda a: {"ok": True, "machines": [{"name": "alpha"}, {"name": "beta"}]},
                         task_status=status)
        result = self.run_agent("--mcp-config", self.config(fake),
                                prompt="SELFTEST: machines\nSELFTEST: status #50\nSELFTEST: status 99\nSELFTEST: status")
        self.assertEqual(result.splitlines()[:4], ["machines: alpha,beta", "status: done",
                                                   "status: error (Job #99 isn't one of your subtasks.)",
                                                   "status: error (say: SELFTEST: status <job id>)"])

    def test_not_available_without_the_tools(self):
        prompt = "SELFTEST: machines\nSELFTEST: delegate agent=x prompt=y\nSELFTEST: delegate-many 2 agent=x prompt=y\n" \
                 "SELFTEST: status 5"
        missing = ["machines: not available", "delegate: not available", "delegate: not available",
                   "status: not available"]
        fake = FakeJobTools(tools=("job_info", "approve"))
        self.addCleanup(fake.close)
        self.assertEqual(self.run_agent("--mcp-config", self.config(fake), prompt=prompt).splitlines()[:4], missing)
        self.assertEqual([c["method"] for c in fake.calls if c["method"] != "tools/list"], [],
                         "nothing is called that isn't listed")
        self.assertEqual(self.run_agent(prompt=prompt).splitlines()[:4], missing, "no job tools at all")

    def test_gemini_mode_reads_its_defaults_file(self):
        fake = self.fake(list_machines=lambda a: {"ok": True, "machines": [{"name": "beta"}]})
        path = self.config(fake, includeTools=["job_info", "list_machines"], trust=True)
        prompt = "SELFTEST: job-info\nSELFTEST: machines\nSELFTEST: delegate agent=x prompt=y\nhello"
        result = self.run_agent("--gemini", prompt=prompt, env={"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": path})
        self.assertTrue(result.startswith("job-info: not available (no job tools)"),
                        "like Gemini CLI, it skips a file that root doesn't own")
        result = self.run_agent("--gemini", prompt=prompt, env={"GEMINI_CLI_SYSTEM_DEFAULTS_PATH": path,
                                                                 "SELFTEST_GEMINI_DEFAULTS": "trusted"})
        self.assertEqual(result.splitlines()[:3], [
            "job-info: #7 on build-box tools=job_info,list_machines,delegate_task,task_status,cancel_task",
            "machines: beta", "delegate: not available"], "only the tools in includeTools are seen")
        result = self.run_agent("--gemini", prompt=prompt)
        self.assertEqual(result.splitlines()[:3], ["job-info: not available (no job tools)", "machines: not available",
                                                   "delegate: not available"])

# ------------------------------------------------------------------ review fixes (runner side)
class PermissionFlags(TempDirCase):
    """Flags outside the levels that change what an agent may do: a later --permission-mode wins over
    the level's, so the hub can't rely on the level names (no 'levels', no subtasks)."""

    def spec(self, **entry) -> dict:
        return runner.agent_spec({"agents": {"a": entry}}, "a")

    def test_permission_flags_lose_levels(self):
        for entry in ({"preset": "claude", "extra_args": ["--permission-mode", "auto"]},
                      {"preset": "claude", "extra_args": ["--permission-mode=bypassPermissions"]},
                      {"preset": "claude", "args": ["-p", "--output-format", "stream-json", "--verbose",
                                                    "--dangerously-skip-permissions"]},
                      {"preset": "claude", "extra_args": ["--allow-dangerously-skip-permissions"]},
                      {"preset": "claude", "extra_args": ["--allowed-tools", "Bash"]},
                      {"preset": "claude", "extra_args": ["--allowedTools=Bash"]},
                      {"preset": "claude", "extra_args": ["--disallowedTools", "Write"]},
                      {"preset": "claude", "extra_args": ["--settings", "/srv/rules.json"]},
                      {"preset": "claude", "extra_args": ["--setting-sources", "user"]},
                      {"preset": "claude", "extra_args": ["--permission-prompt-tool", "mcp__x__y"]},
                      {"preset": "claude", "extra_args": ["--permission-prompts", "none"]},
                      {"preset": "claude", "extra_args": ["--agents", "{}"]},
                      {"preset": "claude", "extra_args": ["--plugin-dir", "/srv/plugins"]},
                      {"preset": "claude", "resume": ["--resume", "{session_id}", "--add-dir", "/"]},
                      {"preset": "claude", "live": ["--input-format", "stream-json", "--permission-mode", "auto"]},
                      {"preset": "claude", "model": ["--model", "{model}", "--tools", "default"]},
                      {"preset": "claude", "new_session": ["--session-id", "{new_session_id}", "--agent", "x"]},
                      {"preset": "claude", "mcp_config_args": ["--mcp-config", "{job_mcp_config}",
                                                               "--permission-mode", "auto"]},
                      {"preset": "claude", "strict_mcp_args": ["--strict-mcp-config", "--permission-mode", "auto"]},
                      {"preset": "claude", "allow_tools_flag": "--dangerously-skip-permissions"},
                      # Spellings Claude Code doesn't take as --allowedTools still count (fail closed).
                      {"preset": "claude", "allow_tools_flag": "--allowedtools"},
                      {"preset": "claude", "allow_tools_flag": "--ALLOWED-TOOLS"},
                      {"preset": "claude", "allow_tools_flag": "--allowedTools=Bash"},
                      {"preset": "claude", "allow_tools_flag": "--disallowed-tools"},
                      {"preset": "selftest", "extra_args": ["--permission-mode", "auto"]},
                      {"preset": "gemini", "extra_args": ["--yolo"]},
                      {"preset": "gemini", "extra_args": ["-y"]},
                      {"preset": "gemini", "extra_args": ["-sy"]},
                      {"preset": "gemini", "extra_args": ["--approval-mode=yolo"]},
                      {"preset": "gemini", "extra_args": ["--approvalMode", "yolo"]},
                      {"preset": "gemini", "args": ["--output-format", "stream-json", "--skip-trust",
                                                    "--allowed-tools", "run_shell_command"]},
                      {"preset": "gemini", "extra_args": ["--policy", "/srv/allow.toml"]},
                      {"preset": "gemini", "extra_args": ["--include-directories", "/"]}):
            with self.subTest(entry=entry):
                spec = self.spec(**entry)
                self.assertTrue(spec["permission_flags"])
                features = runner.agent_features(spec, {})
                self.assertNotIn("levels", features)
                # The same flag would win over the 'ask' level's, so the job would run without asking.
                self.assertNotIn("approvals", features)

    def test_other_flags_keep_levels(self):
        for entry in ({"preset": "claude"}, {"preset": "selftest"}, {"preset": "gemini"},
                      {"preset": "claude", "extra_args": ["--effort", "high"]},
                      {"preset": "claude", "model": ["--model", "{model}", "--fallback-model", "sonnet"]},
                      {"preset": "claude", "command": "/opt/claude/bin/claude"},
                      {"preset": "claude", "allow_tools_flag": "--allowedTools"},
                      # The same Claude Code flag in its other spelling.
                      {"preset": "claude", "allow_tools_flag": "--allowed-tools"},
                      {"preset": "selftest", "allow_tools_flag": "--allowed-tools"},
                      {"preset": "gemini", "extra_args": ["--sandbox"]},
                      {"preset": "gemini", "extra_args": ["-s", "-d"]}):
            with self.subTest(entry=entry):
                spec = self.spec(**entry)
                self.assertEqual(spec["permission_flags"], [])
                features = runner.agent_features(spec, {})
                self.assertIn("levels", features)
                if spec["preset"] != "gemini":
                    self.assertIn("approvals", features)
        self.assertEqual(self.spec(preset="claude", extra_args=["--verbose", "--permission-mode", "auto",
                                                                "--add-dir=/srv"])["permission_flags"],
                         ["--permission-mode", "--add-dir"])

    def test_the_runner_refuses_a_subtask_too(self):
        """The hub stops sending subtasks once the runner stops reporting 'levels'; the runner refuses one
        that was already on its way."""
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env,
                                          "extra_args": ["--permission-mode", "auto"]},
                                  "own": {"preset": "selftest", "env": self.selftest_env,
                                          "autonomy": {"readonly": ["--permission-mode", "auto"]}}})
        cfg["accept_delegated_tasks"] = True
        job = {"id": 30, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "Reply with pong",
               "options": {"timeout_min": 2, "delegated": True}}
        finished, _, started = run_job(cfg, job)
        self.assertEqual(finished["status"], "error")
        self.assertEqual(finished["error"], "one can't take subtasks here: its entry in runner.json has permission "
                                            "flags outside its levels (--permission-mode), so a read-only subtask "
                                            "might not only read. Remove them, or send the subtask to another agent.")
        self.assertEqual(started, [], "nothing was started")
        finished, _, started = run_job(cfg, {**job, "agent": "own"})
        self.assertIn("own can't take subtasks here: its entry in runner.json has autonomy flags of its own",
                      finished["error"])
        self.assertEqual(started, [])
        finished, _, _ = run_job(cfg, {**job, "options": {"timeout_min": 2}})
        self.assertEqual(finished["status"], "done", "the owner's own tasks run as before")


    def test_the_runner_refuses_an_ask_job_too(self):
        """The hub stops offering 'ask' once the runner stops reporting 'approvals'; the runner refuses
        an 'ask' job that was already on its way, rather than run it without asking. The check-in names
        the flags, so the dashboard can say why."""
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env,
                                          "extra_args": ["--permission-mode", "auto"]},
                                  "plain": {"preset": "selftest", "env": self.selftest_env}})
        job = {"id": 31, "agent": "one", "project": "chat", "autonomy": "ask", "prompt": "Reply with pong",
               "options": {"timeout_min": 2}}
        finished, _, started = run_job(cfg, job)
        self.assertEqual(finished["status"], "error")
        self.assertEqual(finished["error"], "one can't ask you before commands here: its entry in runner.json has "
                                            "permission flags outside its levels (--permission-mode), which would "
                                            "win over asking. Move them into its autonomy levels, or pick another "
                                            "level.")
        self.assertEqual(started, [], "nothing was started")
        self.assertEqual(runner.agent_status(cfg, "one")["permission_flags"], ["--permission-mode"])
        self.assertNotIn("permission_flags", runner.agent_status(cfg, "plain"))
        finished, _, _ = run_job(cfg, {**job, "autonomy": "readonly"})
        self.assertEqual(finished["status"], "done", "the owner's other levels run as before")


class GeminiQuotaWords(unittest.TestCase):
    """Only the run's own end counts as a quota limit: Gemini CLI logs every 429 it retries on stderr."""
    RETRY = ('Attempt 1 failed with status 429. Retrying with backoff... _ApiError: {"error":{"code":429,'
             '"message":"Resource has been exhausted (e.g. check quota).","status":"RESOURCE_EXHAUSTED"}}')
    TRACE = ["    at async retryWithBackoff (file:///x/bundle/chunk.js:309418:23) {", "  status: 429", "}"]
    BAD_REQUEST = ('[API Error: {"error":{"code":400,"message":"Request contains an invalid argument.",'
                   '"status":"INVALID_ARGUMENT"}}]')

    def run_parser(self, stderr: list, events: list):
        parser = runner.GeminiStreamParser()
        for line in stderr:
            parser.note_stderr(line)
        for event in events:
            parser.feed(json.dumps(event))
        return parser

    def result(self, kind: str, message: str) -> dict:
        return {"type": "result", "status": "error", "error": {"type": kind, "message": message}}

    def test_a_retried_429_then_another_error(self):
        parser = self.run_parser([self.RETRY, *self.TRACE], [
            {"type": "init", "session_id": "s"},
            {"type": "tool_use", "tool_name": "list_directory", "parameters": {}},
            {"type": "tool_result", "status": "success"}, self.result("unknown", self.BAD_REQUEST)])
        self.assertIsNone(parser.error_kind)
        self.assertEqual(parser.error, self.BAD_REQUEST)
        parser = self.run_parser([self.RETRY, *self.TRACE],
                                 [self.result("FatalTurnLimitedError", "Reached max session turns for this session.")])
        self.assertEqual((parser.error_kind, parser.error), (None, "Reached max session turns for this session."))

    def test_retry_lines_and_stack_frames_on_stderr_are_not_a_quota(self):
        for lines in ([self.RETRY, *self.TRACE],
                      ["Attempt 1 failed with 429 error (no Retry-After header). Retrying with backoff..."],
                      ["    at async retryWithBackoff (file:///x/bundle/chunk.js:309418:429)"],
                      ["Error at chunk.js:309418:429"]):
            with self.subTest(lines=lines):
                parser = self.run_parser(lines, [])
                self.assertIsNone(parser.exit_error())
                self.assertIsNone(parser.error_kind)

    def test_a_real_quota_limit_still_counts(self):
        parser = self.run_parser([self.RETRY], [self.result("TerminalQuotaError", "You exceeded your current quota")])
        self.assertEqual(parser.error_kind, "quota")
        self.assertTrue(parser.error.startswith("Gemini quota hit"))
        parser = self.run_parser([], [self.result("unknown", "Please wait. To increase your limits, request a quota "
                                                             "increase through AI Studio")])
        self.assertEqual(parser.error_kind, "quota")
        said = ('Error when talking to Gemini API _ApiError: {"error":{"code":429,"message":"You exceeded your '
                'current quota","status":"RESOURCE_EXHAUSTED"}}')
        parser = self.run_parser([said], [{"type": "init", "session_id": "s"}])
        self.assertTrue((parser.exit_error() or "").startswith("Gemini quota hit"))
        self.assertEqual(parser.error_kind, "quota")
        parser = self.run_parser([], [{"type": "error", "severity": "error", "message": "RESOURCE_EXHAUSTED"}])
        self.assertEqual(parser.error_kind, None)
        self.assertTrue((parser.exit_error() or "").startswith("Gemini quota hit"), "an error event said so")

    def test_a_later_answer_clears_what_was_said_before(self):
        said = '_ApiError: {"error":{"code":429,"status":"RESOURCE_EXHAUSTED"}}'
        for event in ({"type": "message", "role": "assistant", "content": "ok", "delta": True},
                      {"type": "tool_use", "tool_name": "read_file", "parameters": {}},
                      {"type": "tool_result", "status": "success"}):
            with self.subTest(event=event):
                parser = self.run_parser([said], [{"type": "error", "message": "RESOURCE_EXHAUSTED"}, event])
                self.assertIsNone(parser.exit_error())

    def test_a_quota_crash_keeps_its_last_lines(self):
        """A Gemini CLI that exits without a result: the message says quota, and its own lines stay."""
        tmp = tempfile.mkdtemp(prefix="bullpen-unit-")
        script = ("import sys; sys.stderr.write('_ApiError: {\"error\":{\"code\":429,\"status\":\"RESOURCE_EXHAUSTED\"}}"
                  "\\nTypeError: boom\\n'); sys.exit(1)")
        cfg = make_cfg(tmp, {"gem": {"preset": "gemini", "command": sys.executable, "args": ["-c", script]}})
        finished, _, _ = run_job(cfg, {"id": 31, "agent": "gem", "project": "chat", "prompt": "hi"})
        self.assertEqual((finished["status"], finished["error_kind"]), ("error", "quota"))
        self.assertTrue(finished["error"].startswith("Gemini quota hit"), finished["error"])
        self.assertIn("TypeError: boom", finished["error"], "the real end isn't hidden")
        script = ("import sys; sys.stderr.write('Attempt 1 failed with status 429. Retrying with backoff...\\n"
                  "TypeError: boom\\n'); sys.exit(1)")
        cfg["agents"]["gem"]["args"] = ["-c", script]
        finished, _, _ = run_job(cfg, {"id": 32, "agent": "gem", "project": "chat", "prompt": "hi"})
        self.assertNotIn("error_kind", finished)
        self.assertTrue(finished["error"].startswith("gem exited with code 1."), finished["error"])


class RemoteControlWords(TempDirCase):
    """What Start Remote Control says for each of Claude Code's own refusals."""
    FULL_SCOPE = ("Error: Remote Control requires a full-scope login token. Long-lived tokens (from `claude "
                  "setup-token` or CLAUDE_CODE_OAUTH_TOKEN) are limited to inference-only for security reasons. "
                  "Run `claude auth login` to use Remote Control.")

    def failure(self, said: str, env=None) -> str:
        rc = runner.RemoteControl(argparse.Namespace(cfg={"agents": {}}, name=lambda: "build-box"))
        spec = runner.agent_spec({"agents": {"claude-work": {"preset": "selftest"}}}, "claude-work")
        entry = {"agent": "claude-work", "spec": spec, "env": env or {}, "tail": said.splitlines(),
                 "workdir": self.tmp, "begun": time.monotonic()}
        return rc._failure(entry, 1)

    def test_no_full_login(self):
        text = self.failure(self.FULL_SCOPE)
        self.assertTrue(text.startswith("claude-work on build-box has no full claude.ai login that Remote Control "
                                        "can use: it isn't signed in, or it signs in with a long-lived token "
                                        "(claude setup-token) set in its Claude Code settings. Run 'selftest auth "
                                        "login' there once."), text)
        text = self.failure(self.FULL_SCOPE, env={"CLAUDE_CODE_OAUTH_TOKEN": "fake-oauth-token-0123"})
        self.assertTrue(text.startswith("claude-work signs in with a long-lived token (claude setup-token)."), text)

    def test_an_account_that_isnt_enabled(self):
        for said in ("Remote Control isn't enabled for this account. If you recently changed plans, run `claude auth "
                     "logout` then `claude auth login` to refresh your entitlements, or `claude doctor` for details.",
                     "Remote Control is not yet enabled for your account."):
            with self.subTest(said=said):
                text = self.failure(said)
                self.assertTrue(text.startswith("Remote Control isn't enabled for the Claude account claude-work uses "
                                                "on build-box."), text)
                self.assertIn("run 'selftest auth logout' then 'selftest auth login' there", text)
                self.assertNotIn("Owner", text.split("\n\n")[0])

    def test_an_organization_policy(self):
        text = self.failure("Remote Control is disabled by your organization's policy. Contact your organization "
                            "admin for access.").split("\n\n")[0]
        self.assertTrue(text.startswith("Remote Control is turned off by a policy for the Claude account claude-work "
                                        "uses on build-box."), text)
        for words in ("an Owner turns it on in the admin settings", "On a Pro or Max plan",
                      "'selftest auth logout' then 'selftest auth login'", "disableRemoteControl"):
            self.assertIn(words, text)

    def test_feature_flags_turned_off(self):
        for var in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_GROWTHBOOK",
                    "CLAUDE_CODE_DISABLE_FEATURE_FLAGS"):
            with self.subTest(var=var):
                text = self.failure(f"Remote Control requires feature-flag evaluation, which is disabled because {var} "
                                    "is set. Unset it (or run in a shell without it) to use Remote Control.")
                first = text.split("\n\n")[0]
                self.assertTrue(first.startswith(f"Claude Code on build-box can't check whether Remote Control is "
                                                 f"available: {var} turns off its feature flags."), first)
                self.assertIn("the env block of the agent's Claude Code settings.json", first)

    def test_anthropic_out_of_reach(self):
        for said in ("Couldn't verify Remote Control eligibility, the feature-flag service was unreachable (offline "
                     "or blocked). Retry, or run with --debug for details.",
                     "Couldn't verify your organization's policy for remote control. Check your network connection "
                     "and try again."):
            with self.subTest(said=said):
                first = self.failure(said).split("\n\n")[0]
                self.assertTrue(first.startswith("Claude Code on build-box couldn't reach Anthropic to check whether "
                                                 "Remote Control is available"), first)
                self.assertNotIn("settings.json", first)


class StoppingOnWindows(unittest.TestCase):
    def test_taskkill_at_once(self):
        """Ctrl+Break can't reach a server on a console of its own (and Claude Code would only die of it), so
        Windows ends the tree at once instead of waiting RC_STOP_GRACE first."""
        done: list = []
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        proc.wait.side_effect = lambda timeout=None: done.append(("wait", timeout))
        proc.kill.side_effect = lambda: done.append("kill")
        with mock.patch.object(runner, "IS_WINDOWS", True), \
                mock.patch.object(runner.os, "kill", side_effect=lambda *a: done.append("os.kill")), \
                mock.patch.object(runner.subprocess, "run", side_effect=lambda argv, **kw: done.append(argv)):
            runner.end_process(proc)
        self.assertEqual(done, [["taskkill", "/PID", "4242", "/T", "/F"], "kill", ("wait", 5)])

    @unittest.skipUnless(POSIX, "the polite stop is a signal on Linux and macOS")
    def test_a_signal_first_elsewhere(self):
        done: list = []
        proc = mock.Mock(pid=4242)
        proc.poll.return_value = None
        proc.wait.side_effect = lambda timeout=None: done.append(("wait", timeout))
        with mock.patch.object(runner.os, "killpg", side_effect=lambda pid, sig: done.append(("killpg", sig))):
            runner.end_process(proc, grace=3)
        self.assertEqual(done, [("killpg", runner.signal.SIGTERM), ("wait", 3)])


@unittest.skipUnless(os.name == "nt", "a kill-on-close Job Object is Windows only")
class WindowsJobObjectTests(TempDirCase):
    def test_windows_job_object(self):
        """A server is in the runner's kill-on-close Job Object, so it ends when the runner's process does
        (here: when the Job Object's last handle is closed, as Windows does when a process ends)."""
        import ctypes
        from ctypes import wintypes
        project = Path(self.tmp) / "myapp"
        project.mkdir()
        cfg = make_cfg(self.tmp, {"one": {"preset": "selftest"}})
        cfg["projects"]["myapp"] = str(project)
        cfg["allow_remote_control"] = True
        rc = runner.RemoteControl(argparse.Namespace(cfg=cfg, name=lambda: "build-box"))
        rc.state_file = Path(self.tmp) / "remote-control.json"
        self.addCleanup(rc.stop_all)
        with mock.patch.dict(os.environ):
            for key in AUTH_VARS + ("SELFTEST_RC",):
                os.environ.pop(key, None)
            out = rc.start("one", "myapp")
        self.assertEqual(out["status"], "done", out)
        self.assertIsNotNone(rc.job_object, "the Job Object was made")
        self.assertFalse(rc.job_object_failed)
        job, proc = rc.job_object, rc.servers[("one", "myapp")]["proc"]
        kernel32 = job.kernel32
        kernel32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        inside = wintypes.BOOL()
        self.assertTrue(kernel32.IsProcessInJob(int(proc._handle), job.handle, ctypes.byref(inside)))
        self.assertTrue(inside.value, "the server is in the runner's Job Object")
        rc.job_object = None  # its handle is closed next
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        self.assertTrue(kernel32.CloseHandle(job.handle))
        self.assertIsNotNone(proc.wait(timeout=10), "the server ended with the Job Object")


class OnDemandServers(TempDirCase):
    """An on-demand MCP server is only for the jobs that pick it: the machine-wide wiring leaves it
    out, and claude_server_entries (the code Wiring.claude uses) builds it for a job from the context
    the runner keeps in memory after a sync. Entries from an older hub have no on_demand key."""

    MCP_TOKEN = "bpm_" + "H0b-" * 10
    EVERYWHERE = ["linux", "macos", "windows"]
    WHO = "claude@build-box"

    @classmethod
    def server(cls, name: str, **kw) -> dict:
        return {"name": name, "transport": "http", "route": "direct", "url": "", "headers": {}, "command": "",
                "args": [], "env": {}, "targets": ["claude", "gemini"], "platforms": cls.EVERYWHERE, **kw}

    def context(self, *extra: dict) -> dict:
        servers = [
            self.server("relayed", route="hub"),
            self.server("direct-http", url="https://mcp.example.com/mcp",
                        headers={"Authorization": "Bearer direct-secret"}, on_demand=False),
            self.server("files", transport="stdio", command="npx", args=["-y", "files-server"],
                        env={"FILES_KEY": "f-secret"}, targets=["claude"]),
            self.server("gemini-only", url="https://gemini.example.com/mcp", targets=["gemini"]),
            self.server("elsewhere", url="https://elsewhere.example.com/mcp", platforms=[]),
        ]
        return {"version": "v1", "files": {}, "kit": {}, "mcp_enabled": True, "mcp_token": self.MCP_TOKEN,
                "servers": servers + list(extra), "platforms": self.EVERYWHERE}

    def on_demand(self) -> list:
        return [self.server("picked-only", url="https://picked.example.com/mcp",
                            headers={"X-Api-Key": "picked-secret"}, on_demand=True),
                self.server("relayed-picked", route="hub", on_demand=True)]

    def cfg(self) -> dict:
        cfg_dir = Path(self.tmp) / "claude-home"
        cfg_dir.mkdir(exist_ok=True)
        return make_cfg(self.tmp, {"claude": {"preset": "claude", "env": {"CLAUDE_CONFIG_DIR": str(cfg_dir)}}})

    def claude_calls(self, ctx: dict) -> list:
        """The commands Wiring.claude runs for one Claude Code account that has no MCP servers yet."""
        calls: list = []
        wiring = runner.Wiring(self.cfg(), ctx, lambda *a: None)
        with mock.patch.object(runner, "resolve_command", return_value="claude-cli"), \
                mock.patch.object(runner, "STATE_FILE", Path(self.tmp) / "wired.json"), \
                mock.patch.object(runner.Wiring, "run_cli", lambda _self, argv, env: calls.append(argv) or True):
            wiring.claude()
        self.assertEqual(wiring.notes, [])
        return calls

    def expected_claude_calls(self) -> list:
        """What Wiring.claude ran for context() before on-demand servers existed (the bytes it hands to
        `claude mcp add-json`, key order included)."""
        hub = "http://127.0.0.1:9/mcp"
        auth = {"Authorization": "Bearer " + self.MCP_TOKEN, "X-Bullpen-Agent": self.WHO}
        command, args = (("cmd", ["/c", "npx", "-y", "files-server"]) if runner.IS_WINDOWS
                         else ("npx", ["-y", "files-server"]))
        expected = [
            ("bullpen", {"type": "http", "url": hub, "headers": auth}),
            ("relayed", {"type": "http", "url": hub + "/s/relayed", "headers": auth}),
            ("direct-http", {"type": "http", "url": "https://mcp.example.com/mcp",
                             "headers": {"Authorization": "Bearer direct-secret"}}),
            ("files", {"type": "stdio", "command": command, "args": args, "env": {"FILES_KEY": "f-secret"}}),
        ]
        return [["claude-cli", "mcp", "add-json", "-s", "user", name, json.dumps(conf)] for name, conf in expected]

    def test_claude_wiring_is_unchanged(self):
        self.assertEqual(self.claude_calls(self.context()), self.expected_claude_calls())

    def test_machine_wide_setup_leaves_on_demand_servers_out(self):
        ctx = self.context(*self.on_demand())
        wiring = runner.Wiring(self.cfg(), ctx, lambda *a: None)
        self.assertEqual([s["name"] for s in wiring.servers_for("claude")], ["relayed", "direct-http", "files"],
                         "kept: no on_demand key (an older hub) and on_demand false")
        self.assertEqual([s["name"] for s in wiring.servers_for("gemini")], ["relayed", "direct-http", "gemini-only"])
        self.assertEqual([s["name"] for s in wiring.servers_for("antigravity")], [])
        self.assertEqual(self.claude_calls(ctx), self.expected_claude_calls(),
                         "Claude Code's own settings never get an on-demand server")

    def test_claude_server_entries(self):
        cfg, ctx = self.cfg(), self.context(*self.on_demand())
        hub = "http://127.0.0.1:9/mcp"
        auth = {"Authorization": "Bearer " + self.MCP_TOKEN, "X-Bullpen-Agent": self.WHO}
        self.assertEqual(runner.claude_server_entries(cfg, ctx, ["relayed", "direct-http"], self.WHO), {
            "relayed": {"type": "http", "url": hub + "/s/relayed", "headers": auth},
            "direct-http": {"type": "http", "url": "https://mcp.example.com/mcp",
                            "headers": {"Authorization": "Bearer direct-secret"}}})
        picked = runner.claude_server_entries(cfg, ctx, ["picked-only", "relayed-picked", "bullpen"], self.WHO)
        self.assertEqual(picked, {
            "bullpen": {"type": "http", "url": hub, "headers": auth},
            "picked-only": {"type": "http", "url": "https://picked.example.com/mcp",
                            "headers": {"X-Api-Key": "picked-secret"}},
            "relayed-picked": {"type": "http", "url": hub + "/s/relayed-picked", "headers": auth}},
            "on-demand servers are what a job picks; bullpen is the hub's own")
        self.assertEqual(runner.claude_server_entries(cfg, ctx, ["gemini-only", "elsewhere", "nope"], self.WHO), {},
                         "not for Claude Code, not for this platform, unknown: left out")
        self.assertEqual(list(runner.claude_server_entries(cfg, {**ctx, "mcp_enabled": False},
                                                           ["bullpen", "relayed", "direct-http"], self.WHO)),
                         ["direct-http"], "the hub can't serve its own or relayed servers with its MCP support off")
        with mock.patch.object(runner, "IS_WINDOWS", True):
            self.assertEqual(runner.claude_server_entries(cfg, ctx, ["files"], self.WHO), {
                "files": {"type": "stdio", "command": "cmd", "args": ["/c", "npx", "-y", "files-server"],
                          "env": {"FILES_KEY": "f-secret"}}})
        written = {argv[5]: json.loads(argv[6]) for argv in self.claude_calls(self.context())}
        self.assertEqual(runner.claude_server_entries(cfg, self.context(), list(written), self.WHO), written,
                         "the same entries Wiring.claude writes")

    def test_the_last_context_is_kept_in_memory_only(self):
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps({**make_cfg(self.tmp, {}), "manage_agent_settings": False}), encoding="utf-8")
        r = runner.Runner(path)
        self.assertIsNone(r.last_context)
        ctx = self.context(*self.on_demand())
        ctx["files"] = {"shared.md": "Shared notes\n"}
        ctx["kit"] = {"skills/remember/SKILL.md": "Remember things\n"}
        context_dir = Path(self.tmp) / "context"
        with mock.patch.object(runner, "CONTEXT_DIR", context_dir):
            r.hub = mock.Mock(call=lambda path, payload, timeout=30: (200, ctx))
            self.assertEqual(r.sync_from_hub()["status"], "done")
            self.assertEqual(r.last_context, {"servers": ctx["servers"], "mcp_enabled": True,
                                              "mcp_token": self.MCP_TOKEN, "platforms": self.EVERYWHERE})
            self.assertTrue((context_dir / "shared.md").exists())
            r.hub = mock.Mock(call=lambda path, payload, timeout=30: (502, None))
            self.assertEqual(r.sync_from_hub()["status"], "error")
        self.assertEqual(r.last_context["mcp_token"], self.MCP_TOKEN, "a failed sync keeps the last context")
        for folder in (runner.BASE_DIR, Path(self.tmp)):
            for f in folder.rglob("*"):
                with contextlib.suppress(FileNotFoundError):  # another test's file that went away meanwhile
                    if f.is_file():
                        self.assertNotIn(self.MCP_TOKEN.encode(), f.read_bytes(), f"the MCP token is in {f}")
        self.assertNotIn(self.MCP_TOKEN, json.dumps(r.heartbeat_payload()))

    def test_the_sync_says_this_runner_takes_on_demand_servers(self):
        # Ruling G14 (F4): the hub sends on-demand servers only to a runner that says it leaves them out of the
        # machine-wide setup ('on-demand'), as it sends hub-routed ones only with 'hub-route'.
        path = Path(self.tmp) / "runner.json"
        path.write_text(json.dumps({**make_cfg(self.tmp, {}), "manage_agent_settings": False}), encoding="utf-8")
        r = runner.Runner(path)
        sent: list = []
        with mock.patch.object(runner, "CONTEXT_DIR", Path(self.tmp) / "context"):
            r.hub = mock.Mock(call=lambda path, payload, timeout=30: sent.append((path, payload)) or (200, self.context()))
            self.assertEqual(r.sync_from_hub()["status"], "done")
        self.assertEqual(sent, [("/api/runner/context", {"features": ["hub-route", "on-demand"]})])


class JobMcpPicks(TempDirCase):
    """The MCP servers a task picked (its options mcp_add and mcp_only) reach its Claude Code agent: one
    --mcp-config file in the job's folder holds the job tools (when the hub sent a job token), the added servers,
    and for a task narrowed to some servers those plus the hub's own `bullpen`, with --strict-mcp-config. Their
    tools are allowed at every level, read only included. A job without picks keeps its command line."""

    MCP_TOKEN = "bpm_" + "P1ck-" * 8
    DIRECT_SECRET = "direct-pick-secret-1"
    FILES_SECRET = "files-secret-22"
    SELFTEST = str(ROOT / "runner" / "selftest_agent.py")
    # Says what is in the --mcp-config file it was given, secrets included, on both outputs (as an agent's tool
    # might), and its git account's token on stderr, then answers in Claude Code's stream-json.
    PEEK = "\n".join([
        "import json, os, sys",
        "args = sys.argv[1:]",
        "with open(args[args.index('--mcp-config') + 1], encoding='utf-8') as fh:",
        "    servers = json.load(fh)['mcpServers']",
        "seen = json.dumps({name: [e.get('headers'), e.get('env')] for name, e in servers.items()}, sort_keys=True)",
        "print('saw ' + seen, file=sys.stderr, flush=True)",
        "print('git ' + os.environ.get('BULLPEN_GIT_TOKEN', 'none'), file=sys.stderr, flush=True)",
        "for ev in ({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': 'm'},",
        "           {'type': 'result', 'subtype': 'success', 'result': seen, 'session_id': 's1'}):",
        "    print(json.dumps(ev), flush=True)",
    ])

    def context(self, **extra) -> dict:
        """What the runner keeps after a sync: two on-demand servers (one the hub relays, one direct with a
        secret header), a stdio server with a secret in its env, and an on-demand one for Gemini CLI only."""
        server = OnDemandServers.server
        return {"mcp_enabled": True, "mcp_token": self.MCP_TOKEN, "platforms": OnDemandServers.EVERYWHERE,
                "servers": [
                    server("relayed-pick", route="hub", on_demand=True, targets=["claude"]),
                    server("direct-pick", url="https://mcp.example.com/mcp",
                           headers={"Authorization": "Bearer " + self.DIRECT_SECRET}, on_demand=True),
                    server("files", transport="stdio", command="npx", args=["-y", "files-server"],
                           env={"FILES_KEY": self.FILES_SECRET}, targets=["claude"]),
                    server("gemini-only", url="https://gemini.example.com/mcp", targets=["gemini"], on_demand=True),
                ], **extra}

    def cfg(self, hub_url: str = "http://127.0.0.1:9", **agents) -> dict:
        return make_cfg(self.tmp, {"one": {"preset": "selftest", "env": self.selftest_env},
                                   # The Claude Code preset's own levels, with the fake agent in place of claude.
                                   "cc": {"preset": "claude", "command": sys.executable, "args": [self.SELFTEST],
                                          "env": self.selftest_env},
                                   **agents}, hub_url=hub_url)

    def job(self, job_id: int, agent: str = "one", autonomy: str = "readonly", **options) -> dict:
        return {"id": job_id, "agent": agent, "project": "chat", "autonomy": autonomy,
                "prompt": "Reply with exactly one word: pong", "options": {"timeout_min": 2, **options}}

    @staticmethod
    def config(job_id: int) -> str:
        return str(runner.JOBS_DIR / str(job_id) / "mcp.json")

    @staticmethod
    def servers(job_id: int) -> dict:
        return read_json(Path(JobMcpPicks.config(job_id)))["mcpServers"]

    def test_added_servers_share_one_file_with_the_job_tools(self):
        cfg, ctx = self.cfg(), self.context()
        spec = runner.agent_spec(cfg, "one")
        job = {"id": 51, "agent": "one", "job_token": TOKEN, "job_tools": ["job_info"],
               "options": {"mcp_add": ["direct-pick"]}}
        args, folder, picked, secrets = runner.prepare_job_mcp(cfg, spec, job, "build-box", ctx)
        self.addCleanup(runner.shutil.rmtree, runner.JOBS_DIR / "51", True)
        self.assertEqual((args, folder, picked), (["--mcp-config", self.config(51)], runner.JOBS_DIR / "51",
                                                  ["direct-pick"]), "no --strict-mcp-config when only adding")
        who = "one@build-box job 51"
        self.assertEqual(self.servers(51), {"bullpenjob": runner.job_server_entry(cfg, TOKEN, who, 120_000),
                                            **runner.claude_server_entries(cfg, ctx, ["direct-pick"], who)},
                         "the job tools as prepare_job_tools writes them, and the added server as the machine-wide "
                         "wiring would")
        self.assertEqual(self.servers(51)["direct-pick"]["headers"], {"Authorization": "Bearer " + self.DIRECT_SECRET})
        self.assertIn(self.DIRECT_SECRET, secrets)
        if POSIX:
            self.assertEqual(stat.S_IMODE(Path(self.config(51)).stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        # Without a job token: the added server alone.
        _, _, picked, _ = runner.prepare_job_mcp(cfg, spec, {**job, "id": 52, "job_token": None}, "build-box", ctx)
        self.addCleanup(runner.shutil.rmtree, runner.JOBS_DIR / "52", True)
        self.assertEqual((list(self.servers(52)), picked), (["direct-pick"], ["direct-pick"]))

    def test_added_servers_tools_are_allowed_at_every_level(self):
        fake = FakeJobTools(job_id=53, tools=("job_info", "approve"))
        self.addCleanup(fake.close)
        cfg = self.cfg(hub_url=fake.url)
        token = {"job_token": TOKEN, "job_tools": ["job_info", "approve"], "approval_timeout_s": 60}
        for job_id, autonomy, extra, level in (
                (53, "ask", token, ["--permission-mode", "acceptEdits", "--allowedTools", "mcp__direct-pick",
                                    "mcp__bullpenjob__job_info", "mcp__bullpen",
                                    "--permission-prompt-tool", "mcp__bullpenjob__approve"]),
                (54, "readonly", {}, ["--permission-mode", "dontAsk", "--allowedTools", "mcp__direct-pick",
                                      "mcp__bullpen__recall", "mcp__bullpen__list_notes"]),
                (55, "edits", {}, ["--permission-mode", "acceptEdits", "--allowedTools", "mcp__direct-pick",
                                   "mcp__bullpen"]),
                (56, "full", {}, ["--permission-mode", "auto", "--allowedTools", "mcp__direct-pick", "mcp__bullpen"])):
            with self.subTest(autonomy=autonomy):
                finished, lines, started = run_job(cfg, {**self.job(job_id, "cc", autonomy, mcp_add=["direct-pick"]),
                                                         **extra}, last_context=self.context())
                self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
                self.assertEqual(started, [[sys.executable, self.SELFTEST, *level, "--mcp-config", self.config(job_id)]])
                self.assertIn("MCP servers for this job: direct-pick. Their tools are allowed even in Read only.",
                              lines)
                self.assertFalse((runner.JOBS_DIR / str(job_id)).exists(), "the job's folder is removed afterwards")

    def test_only_these_servers_and_the_hubs_own(self):
        cfg, ctx = self.cfg(), self.context()
        spec = runner.agent_spec(cfg, "cc")
        job = {"id": 57, "agent": "cc", "job_token": TOKEN, "job_tools": ["job_info"],
               "options": {"mcp_only": ["files", "relayed-pick"]}}
        args, _, picked, secrets = runner.prepare_job_mcp(cfg, spec, job, "build-box", ctx)
        self.addCleanup(runner.shutil.rmtree, runner.JOBS_DIR / "57", True)
        self.assertEqual((args, picked), (["--mcp-config", self.config(57), "--strict-mcp-config"],
                                          ["files", "relayed-pick"]))
        servers = self.servers(57)
        self.assertEqual(sorted(servers), ["bullpen", "bullpenjob", "files", "relayed-pick"])
        who = "cc@build-box job 57"
        self.assertEqual(servers["bullpen"], runner.claude_server_entries(cfg, ctx, ["bullpen"], who)["bullpen"])
        self.assertEqual(servers["relayed-pick"]["headers"]["Authorization"], "Bearer " + self.MCP_TOKEN)
        self.assertTrue({self.MCP_TOKEN, self.FILES_SECRET} <= set(secrets), "the hub's token and the env value")
        self.assertNotIn(who, secrets, "the agent's name is no secret")
        # Added and narrowed at once: one list of picks, narrowed to them.
        both = {**job, "id": 58, "options": {"mcp_add": ["direct-pick", "files"], "mcp_only": ["files"]}}
        args, _, picked, _ = runner.prepare_job_mcp(cfg, spec, both, "build-box", ctx)
        self.addCleanup(runner.shutil.rmtree, runner.JOBS_DIR / "58", True)
        self.assertEqual((args[-1], picked), ("--strict-mcp-config", ["direct-pick", "files"]))
        self.assertEqual(sorted(self.servers(58)), ["bullpen", "bullpenjob", "direct-pick", "files"])

        # The job: the picked servers' tools pre-approved, the hub's own only as the level allows.
        finished, lines, started = run_job(cfg, self.job(59, "cc", mcp_only=["files", "relayed-pick"]),
                                           last_context=ctx)
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        self.assertEqual(started, [[sys.executable, self.SELFTEST, "--permission-mode", "dontAsk", "--allowedTools",
                                    "mcp__files", "mcp__relayed-pick", "mcp__bullpen__recall",
                                    "mcp__bullpen__list_notes", "--mcp-config", self.config(59),
                                    "--strict-mcp-config"]])
        self.assertIn("MCP servers for this job: files, relayed-pick. Their tools are allowed even in Read only.",
                      lines)

    def test_a_job_without_picks_keeps_its_command_line(self):
        fake = FakeJobTools(tools=("job_info",))
        self.addCleanup(fake.close)
        cfg = self.cfg(hub_url=fake.url)
        cfg["accept_delegated_tasks"] = True
        plain = [sys.executable, self.SELFTEST, "--permission-mode", "dontAsk"]
        token = {"job_token": TOKEN, "job_tools": ["job_info"]}
        for job_id, options, extra, argv in (
                (60, {}, {}, plain),
                (61, {"mcp_add": [], "mcp_only": []}, {}, plain),  # an empty list turns a choice off
                (62, {"delegated": True, "mcp_add": ["direct-pick"], "mcp_only": ["files"]}, {}, plain),  # a subtask
                (63, {}, token, plain + ["--allowedTools", "mcp__bullpenjob__job_info", "--mcp-config", self.config(63)]),
                (64, {"mcp_only": []}, token,
                 plain + ["--allowedTools", "mcp__bullpenjob__job_info", "--mcp-config", self.config(64)])):
            with self.subTest(job_id=job_id):
                finished, lines, started = run_job(cfg, {**self.job(job_id, **options), **extra},
                                                   last_context=self.context())
                self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
                self.assertEqual(started, [argv], "the command line is what it always was")
                self.assertFalse([ln for ln in lines if ln.startswith("MCP servers")], lines)
        # prepare_job_mcp without picks is prepare_job_tools: the same arguments, folder and file.
        spec = runner.agent_spec(cfg, "one")
        job = {"id": 65, "agent": "one", **token, "options": {"mcp_add": []}}
        args, _, folder = runner.prepare_job_tools(cfg, spec, job, "build-box")
        self.addCleanup(runner.shutil.rmtree, runner.JOBS_DIR / "65", True)
        today = Path(self.config(65)).read_text(encoding="utf-8")
        self.assertEqual(runner.prepare_job_mcp(cfg, spec, job, "build-box", self.context()), (args, folder, [], []))
        self.assertEqual(Path(self.config(65)).read_text(encoding="utf-8"), today)
        self.assertEqual(runner.prepare_job_mcp(cfg, spec, {**job, "id": 66, "job_token": None}, "build-box",
                                                self.context()), ([], None, [], []))
        self.assertFalse((runner.JOBS_DIR / "66").exists())

    def test_a_server_it_cant_give_fails_the_job_before_it_starts(self):
        cfg = self.cfg(gem={"preset": "gemini", "command": sys.executable},
                       loose={"preset": "selftest", "env": self.selftest_env, "strict_mcp_args": []})
        ctx = self.context()
        no_list = " (this runner hasn't had the hub's list of MCP servers yet)"
        for job_id, (agent, options, context, error) in enumerate((
                ("one", {"mcp_add": ["nope"]}, ctx, "MCP server 'nope' isn't available on build-box"),
                ("one", {"mcp_only": ["files", "gemini-only"]}, ctx,
                 "MCP server 'gemini-only' isn't available on build-box"),
                ("one", {"mcp_add": ["bullpen"]}, ctx, "MCP server 'bullpen' isn't available on build-box"),
                ("one", {"mcp_only": ["bullpenjob"]}, ctx, "MCP server 'bullpenjob' isn't available on build-box"),
                ("one", {"mcp_add": [{"name": "files"}]}, ctx, "MCP server '(not a name)' isn't available on build-box"),
                ("one", {"mcp_add": ["relayed-pick"]}, self.context(mcp_enabled=False),
                 "MCP server 'relayed-pick' isn't available on build-box"),
                ("one", {"mcp_add": ["direct-pick"]}, None, "MCP server 'direct-pick' isn't available on build-box"
                                                            + no_list),
                ("gem", {"mcp_add": ["direct-pick"]}, ctx,
                 "gem can't be given a task's MCP servers (only Claude Code agents can)"),
                ("loose", {"mcp_only": ["files"]}, ctx,
                 "loose can't be limited to the task's MCP servers: it has no strict_mcp_args in runner.json")),
                start=70):
            with self.subTest(agent=agent, options=options):
                finished, _, started = run_job(cfg, {**self.job(job_id, agent, **options), "job_token": TOKEN,
                                                     "job_tools": ["job_info"]}, last_context=context)
                self.assertEqual((finished["status"], finished["error"]), ("error", error))
                self.assertEqual(started, [], "nothing was started")
                self.assertFalse((runner.JOBS_DIR / str(job_id)).exists())
        # A stub without a context at all (runner.py test).
        finished, _, started = run_job(cfg, self.job(80, mcp_add=["direct-pick"]))
        self.assertEqual(finished["error"], "MCP server 'direct-pick' isn't available on build-box" + no_list)
        self.assertEqual(started, [])
        # Adding needs no strict_mcp_args.
        finished, _, started = run_job(cfg, self.job(81, "loose", mcp_add=["files"]), last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(started[0][-2:], ["--mcp-config", self.config(81)])

    def test_the_files_secrets_never_show(self):
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.PEEK]})
        job = {**self.job(82, "peek", mcp_add=["direct-pick"], mcp_only=["files", "relayed-pick"]),
               "job_token": TOKEN, "job_tools": ["job_info"]}
        with self.assertLogs("runner", level="INFO") as logs:
            finished, lines, started = run_job(cfg, job, last_context=self.context())
        self.assertEqual(finished["status"], "done", finished)
        seen = json.loads(finished["result"])
        self.assertEqual(sorted(seen), ["bullpen", "bullpenjob", "direct-pick", "files", "relayed-pick"])
        self.assertEqual(seen["bullpen"][0], {"Authorization": "***", "X-Bullpen-Agent": "peek@build-box job 82"})
        self.assertEqual(seen["direct-pick"][0], {"Authorization": "***"})
        self.assertEqual(seen["files"][1], {"FILES_KEY": "***"})
        self.assertTrue([ln for ln in lines if ln.startswith("stderr: saw ")], lines)
        text = json.dumps([lines, finished, started, logs.output])
        for secret in (self.MCP_TOKEN, self.DIRECT_SECRET, self.FILES_SECRET, TOKEN):
            self.assertNotIn(secret, text)

    def test_a_short_secret_is_masked_too(self):
        # A header or env value is masked however short (known_secrets leaves out values under 6 characters).
        server = OnDemandServers.server
        ctx = self.context()
        ctx["servers"] = ctx["servers"] + [
            server("short-key", url="https://short.example.com/mcp", headers={"X-Api-Key": "ab12c"}, on_demand=True),
            server("short-env", transport="stdio", command="npx", args=[], env={"SHORT_KEY": "k3y9"},
                   targets=["claude"], on_demand=True)]
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.PEEK]})
        finished, lines, _ = run_job(cfg, self.job(84, "peek", mcp_add=["short-key", "short-env"]), last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        seen = json.loads(finished["result"])
        self.assertEqual((seen["short-key"][0], seen["short-env"][1]), ({"X-Api-Key": "***"}, {"SHORT_KEY": "***"}))
        self.assertTrue([ln for ln in lines if ln.startswith("stderr: saw ")], lines)
        text = json.dumps([lines, finished])
        for secret in ("ab12c", "k3y9"):
            self.assertNotIn(secret, text)

    # Prints the whole --mcp-config file it was given (URLs and arguments too) on stderr and in its answer.
    PEEK_FILE = "\n".join([
        "import json, sys",
        "args = sys.argv[1:]",
        "with open(args[args.index('--mcp-config') + 1], encoding='utf-8') as fh:",
        "    text = fh.read()",
        "print('file ' + ' '.join(text.split()), file=sys.stderr, flush=True)",
        "for ev in ({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': 'm'},",
        "           {'type': 'result', 'subtype': 'success', 'result': text, 'session_id': 's1'}):",
        "    print(json.dumps(ev), flush=True)",
    ])

    def test_secrets_in_a_url_or_the_arguments_are_masked(self):
        # The file is the only place on the machine an on-demand server's URL and arguments are, and the agent can
        # read it: a direct server's userinfo, query values and path token, and a stdio server's arguments.
        user, password, path_token, auth = "pick-user", "pw-7Qx", "Pth8Tok3n", "q5"
        arg_secret, eq_secret = "Arg-K3y-901", "Eq-K3y-902"
        server = OnDemandServers.server
        ctx = self.context()
        ctx["servers"] = ctx["servers"] + [
            server("url-secrets", url=f"https://{user}:{password}@mcp.example.com/s/{path_token}/mcp?auth={auth}",
                   on_demand=True),
            server("keyed", transport="stdio", command="npx", targets=["claude"], on_demand=True,
                   args=["-y", "keyed-server", "--api-key", arg_secret, f"--token={eq_secret}"])]
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.PEEK_FILE]})
        job = self.job(85, "peek", mcp_add=["url-secrets", "keyed", "relayed-pick"])
        with self.assertLogs("runner", level="INFO") as logs:
            finished, lines, started = run_job(cfg, job, last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        shown = json.loads(finished["result"])["mcpServers"]
        self.assertEqual(shown["url-secrets"]["url"], "https://***@mcp.example.com/s/***/mcp?auth=***")
        args = shown["keyed"]["args"]
        if args[:2] == ["/c", "npx"]:  # Windows runs npx through cmd /c (claude_server_entries)
            args = args[2:]
        self.assertEqual(args[0], "-y", "short words like -y stay readable")
        self.assertEqual(args[3:], ["***", "***"])
        self.assertEqual(shown["relayed-pick"]["url"], "http://127.0.0.1:9/mcp/s/relayed-pick",
                         "the hub's own address is no secret, and the server's name stays readable")
        self.assertIn("MCP servers for this job: url-secrets, keyed, relayed-pick. Their tools are allowed even in "
                      "Read only.", lines)
        self.assertTrue([ln for ln in lines if ln.startswith("stderr: file ")], lines)
        text = json.dumps([lines, finished, started, logs.output])
        for secret in (password, path_token, f"auth={auth}", arg_secret, eq_secret, self.MCP_TOKEN):
            self.assertNotIn(secret, text)

    def test_which_parts_of_an_entry_are_masked(self):
        # A URL's password and every header value (but true or false) are secrets whatever their length, and so is
        # an env or query value whose name says it is one (key, token, auth, pin, ...), digits too, unless it is
        # true or false. Any other value, a URL's user and path segments, a query part without a name and a stdio
        # server's arguments are often words ("1", "info", "x", "s", "mcp", "-y", "/c", "."), so they count only
        # from 6 characters up. The hub's own entries have nothing secret in their URL.
        entries = {
            "bullpen": {"type": "http", "url": "http://127.0.0.1:9/mcp",
                        "headers": {"Authorization": "Bearer " + self.MCP_TOKEN, "X-Bullpen-Agent": "cc@build-box"}},
            "relayed-pick": {"type": "http", "url": "http://127.0.0.1:9/mcp/s/relayed-pick",
                             "headers": {"Authorization": "Bearer " + self.MCP_TOKEN, "X-Bullpen-Agent": "cc@build-box"}},
            "direct": {"type": "http", "url": "https://x:pw@mcp.example.com/s/Pth8Tok3n/v1?key=a1&flag&readonly=true&v=2",
                       "headers": {"X-Api-Key": "ab1", "X-Mode": "ro", "X-Upstream": "Bearer b4r"}},
            "tool": {"type": "stdio", "command": "cmd", "args": ["/c", "npx", "-y", ".", "--key=Arg-K3y-901"],
                     "env": {"N_KEY": "z9", "API_KEY": "12345678", "LOG_FORMAT": "pretty-json", "READ_ONLY": "1",
                             "LOG_LEVEL": "info", "DEBUG": "*", "MAX_TOKENS": "4096", "AUTH_REQUIRED": "true",
                             "COOKIE_SECURE": "False"}},
        }
        secrets = runner.mcp_entry_secrets(entries, "http://127.0.0.1:9")
        for value in (self.MCP_TOKEN, "pw", "Pth8Tok3n", "a1", "ab1", "ro", "b4r", "z9", "12345678", "4096",
                      "pretty-json", "--key=Arg-K3y-901", "Arg-K3y-901"):
            self.assertIn(value, secrets)
        for value in ("cc@build-box", "relayed-pick", "x", "s", "mcp", "v1", "/c", "npx", "-y", ".", "", "flag",
                      "true", "2", "1", "info", "*", "False"):
            self.assertNotIn(value, secrets)

    def test_a_short_value_counts_by_its_name(self):
        # Fake values, built here. A short env or query value is a secret when its name has a secret's word in it:
        # SECRET_NAME_RE's, or pin, pwd, pass, passphrase, sig, signature, session, sid or otp as a whole word (the
        # name split on _ - . and where a lower-case letter meets an upper-case one). Digits too; true and false
        # never. Every header value counts, and so does what follows a flag named like a secret.
        pin, api_key, x_pin = "12" + "34", str(12345), str(99)
        def secrets(**entry) -> list:
            return runner.mcp_entry_secrets({"one": {"type": "stdio", **entry}})

        for name in ("PIN", "API_KEY", "DB_PWD", "PASS", "GPG_PASSPHRASE", "URL_SIG", "signature", "SESSION",
                     "sessionId", "userPin", "SID", "OTP", "pin.code", "x-sig", "MAX_TOKENS"):
            self.assertIn(pin, secrets(env={name: pin}), name)
            self.assertNotIn("true", secrets(env={name: "true"}), name)
            self.assertNotIn("FALSE", secrets(env={name: "FALSE"}), name)
        self.assertIn(api_key, secrets(env={"API_KEY": api_key}), "all digits under a secret's name")
        for name in ("READ_ONLY", "DEBUG", "SPIN", "OPTIONS", "SIGNAL", "PASSIVE", "BYPASS", "Sessions", "SIDE",
                     "OTPS", "region", "hTTPSession"):
            self.assertEqual(secrets(env={name: "1"}), [], name)
            self.assertEqual(secrets(env={name: "us"}), [], name)
        self.assertEqual(secrets(env={"DEBUG": "true", "READ_ONLY": "1"}), [])
        self.assertEqual(secrets(env={"REGION": "us-east-1"}), ["us-east-1"], "6 characters or more: any name")
        url = runner.mcp_entry_secrets({"one": {"type": "http",
                                                "url": f"https://mcp.example.com/mcp?pin={pin}&sig=a7&page=2"}})
        self.assertIn(pin, url)
        self.assertIn("a7", url)
        self.assertNotIn("2", url)
        # Every header value but the agent's name, however short, and a "Bearer <token>" value's token on its own.
        headers = runner.mcp_entry_secrets({"one": {"type": "http", "url": "https://mcp.example.com/mcp", "headers": {
            "X-Pin": x_pin, "X-Mode": "ro", "X-Upstream": "Bearer b4r", "X-Read-Only": "true", "X-Debug": "False",
            "X-Bullpen-Agent": "cc"}}})
        for value in (x_pin, "ro", "Bearer b4r", "b4r"):
            self.assertIn(value, headers)
        for value in ("true", "False", "cc"):
            self.assertNotIn(value, headers)
        # A stdio server's argument after a flag named like a secret, and what follows "=" in one, whatever its
        # length; flags named otherwise keep their short values readable.
        self.assertIn("ab12c", secrets(args=["--api-key", "ab12c"]))
        self.assertIn("ab12c", secrets(args=["--api-key=ab12c"]))
        for args in (["--pin", pin], [f"--pin={pin}"], ["-p", "x", "--session-id", pin], ["--otp", pin, "-y"],
                     ["--userPass", pin]):
            self.assertIn(pin, secrets(args=args), args)
        for args in (["-y", "ab"], ["--port", pin], [f"--port={pin}"], ["--spin", pin], ["ab", "--api-key"],
                     ["--api-key=", "ab"]):
            self.assertEqual([s for s in secrets(args=args) if len(s) < runner.KNOWN_SECRET_MIN], [], args)

    def test_a_flag_named_like_a_secret_takes_no_word_and_no_other_flag(self):
        # Ruling G14 (F6). After --api-key, "true" is a setting, and "-y" after --token is the next flag: neither is a
        # secret, and masking them would scramble the answer ("construed", "npx -y"). Fake values, built here.
        found = runner.mcp_entry_secrets({"one": {"type": "stdio", "command": "npx",
                                                  "args": ["--api-key", "true", "--token", "-y"]}})
        self.assertNotIn("true", found)
        self.assertNotIn("-y", found)
        self.assertEqual([s for s in runner.mcp_entry_secrets({"one": {"type": "stdio", "args": [
            "--api-key=TRUE", "--token=false", "--pin", "--verbose"]}}) if len(s) < runner.KNOWN_SECRET_MIN], [])
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 92})
        job.mask_values = found
        said = "The clause is construed narrowly; run npx -y files-server, true."
        self.assertEqual(job.mask(said), said)
        # A value after the flag still counts, and so does one after a flag that came without one.
        value = "ab" + "12c"
        self.assertIn(value, runner.mcp_entry_secrets({"one": {"type": "stdio", "args": ["--token", "-y",
                                                                                         "--api-key", value]}}))

    def test_a_url_password_holding_a_question_mark_or_hash_counts(self):
        # Ruling G14 (F5): the userinfo is read up to the first "/" after the slashes, as url_has_userinfo reads it.
        for mark in ("?", "#"):
            password = "p" + mark + "x7"
            found = runner.mcp_entry_secrets({"one": {"type": "http",
                                                      "url": f"https://u:{password}@mcp.example.com/mcp"}})
            self.assertIn(password, found, mark)

    def test_settings_that_arent_secret_stay_readable(self):
        # Masking replaces each value in the log, the answer and the diff, so a short setting ("1", "info", "*",
        # "true") would turn copies of it there into ***. A short env or query value is masked only when its name
        # says it is a secret (digits too: MAX_TOKENS), and true or false never; every header value but true or
        # false is masked.
        server = OnDemandServers.server
        ctx = self.context()
        settings = {"READ_ONLY": "1", "LOG_LEVEL": "info", "DEBUG": "*", "AUTH_REQUIRED": "true"}
        ctx["servers"] = ctx["servers"] + [
            server("settings", url="https://settings.example.com/mcp?readonly=true&key=q5",
                   headers={"X-Mode": "ro", "X-Read-Only": "true"}, on_demand=True),
            server("tuned", transport="stdio", command="npx", args=["-y", "tuned-server"], targets=["claude"],
                   env={**settings, "MAX_TOKENS": "4096", "API_KEY": "k3y9"}, on_demand=True)]
        repo = Path(self.tmp) / "notes"
        has_git = bool(runner.shutil.which("git"))
        if has_git:
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        else:
            repo.mkdir()
        changed = "READ_ONLY = 1\nLOG_LEVEL = info\nfiles = *\nreadonly = true\nmode = ro\nrole = robot\n"
        (repo / "settings.txt").write_text(changed, encoding="utf-8")
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.PEEK_FILE]})
        cfg["projects"]["notes"] = str(repo)
        job = {**self.job(86, "peek", mcp_add=["settings", "tuned"], capture_diff=True), "project": "notes"}
        finished, lines, _ = run_job(cfg, job, last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        shown = json.loads(finished["result"])["mcpServers"]
        self.assertEqual(shown["settings"]["url"], "https://settings.example.com/mcp?readonly=true&key=***")
        self.assertEqual(shown["settings"]["headers"], {"X-Mode": "***", "X-Read-Only": "true"})
        self.assertEqual(shown["tuned"]["env"], {**settings, "MAX_TOKENS": "***", "API_KEY": "***"})
        if has_git:
            self.assertIn("=== new file settings.txt ===\n" + changed.replace("mode = ro", "mode = ***").strip(),
                          finished["diff"], "the diff is the project's changes, with the header's ro alone masked "
                                            "(not the ro in role or robot)")
        self.assertTrue([ln for ln in lines if ln.startswith("stderr: file ")], lines)
        self.assertIn("MCP servers for this job: settings, tuned. Their tools are allowed even in Read only.", lines)
        text = json.dumps([lines, finished])
        for secret in ("key=q5", "k3y9"):
            self.assertNotIn(secret, text)

    # Says its prompt back, on stderr and as its answer, and the --mcp-config file it was given on stderr.
    ECHO = "\n".join([
        "import json, sys",
        "args = sys.argv[1:]",
        "with open(args[args.index('--mcp-config') + 1], encoding='utf-8') as fh:",
        "    print('file ' + ' '.join(fh.read().split()), file=sys.stderr, flush=True)",
        "text = sys.stdin.read()",
        "for line in text.splitlines():",
        "    print('said ' + line, file=sys.stderr, flush=True)",
        "for ev in ({'type': 'system', 'subtype': 'init', 'session_id': 's1', 'model': 'm'},",
        "           {'type': 'result', 'subtype': 'success', 'result': text, 'session_id': 's1'}):",
        "    print(json.dumps(ev), flush=True)",
    ])

    def test_a_short_secret_is_masked_where_it_stands_alone(self):
        # Fake values, built here. PIN=1234, API_KEY=12345, an X-Pin: 99 header and the values of --api-key are
        # masked in the log, the answer and the diff. The all-digit ones, PIN and X-Pin only where no letter or digit
        # touches them: inside a longer word or number they stay, and so do READ_ONLY=1, DEBUG=true and every other
        # "1". The values of --api-key and the URL's password are strong secrets, masked inside other words too.
        pin, api_key, x_pin, short_pw = "12" + "34", str(12345), str(99), "k" + "7"
        flag_secret, eq_secret = "ab" + "12c", "cd" + "34e"
        server = OnDemandServers.server
        ctx = self.context()
        ctx["servers"] = ctx["servers"] + [
            server("pinned", transport="stdio", command="npx", targets=["claude"], on_demand=True,
                   args=["--api-key", flag_secret, f"--api-key={eq_secret}"],
                   env={"PIN": pin, "API_KEY": api_key, "READ_ONLY": "1", "DEBUG": "true"}),
            server("x-pin", url=f"https://x:{short_pw}@pin.example.com/mcp", headers={"X-Pin": x_pin},
                   on_demand=True)]
        said = (f"PIN={pin} API_KEY={api_key} X-Pin: {x_pin}\n"
                f"flags: {flag_secret} and ({eq_secret}).\n"
                f"inside: x{pin} {pin}0 {api_key}6 {x_pin}9 v{x_pin} {flag_secret}d z{eq_secret} {short_pw}z\n"
                "READ_ONLY=1 DEBUG=true: step 1 of 10, v1.1, 2001, true\n")
        masked = said.replace(f"PIN={pin} API_KEY={api_key} X-Pin: {x_pin}", "PIN=*** API_KEY=*** X-Pin: ***")
        masked = masked.replace(f"{flag_secret} and ({eq_secret})", "*** and (***)")
        masked = masked.replace(f"{flag_secret}d z{eq_secret} {short_pw}z", "***d z*** ***z")
        self.assertEqual(masked.count("***"), 8)
        repo = Path(self.tmp) / "pins"
        has_git = bool(runner.shutil.which("git"))
        if has_git:
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        else:
            repo.mkdir()
        (repo / "pins.txt").write_text(said, encoding="utf-8")
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.ECHO]})
        cfg["projects"]["pins"] = str(repo)
        job = {**self.job(87, "peek", mcp_add=["pinned", "x-pin"], capture_diff=True), "project": "pins",
               "prompt": said}
        finished, lines, _ = run_job(cfg, job, last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"].strip(), masked.strip(), "the answer")
        self.assertEqual([ln for ln in lines if ln.startswith("stderr: said ")],
                         ["stderr: said " + ln for ln in masked.splitlines()], "the log")
        if has_git:
            self.assertIn("=== new file pins.txt ===\n" + masked.strip(), finished["diff"], "the diff")
        file_line = [ln for ln in lines if ln.startswith("stderr: file ")]
        self.assertEqual(len(file_line), 1, lines)
        for shown in ('"PIN": "***"', '"API_KEY": "***"', '"READ_ONLY": "1"', '"DEBUG": "true"', '"X-Pin": "***"',
                      '"https://x:***@pin.example.com/mcp"'):
            self.assertIn(shown, file_line[0])
        self.assertIn("MCP servers for this job: pinned, x-pin. Their tools are allowed even in Read only.", lines)

    def test_a_short_strong_secret_is_masked_inside_other_words(self):
        # Rulings G13b and G13c. Fake values, built here. A short URL password, a short Bearer token and a short
        # value named like a key are masked even glued to other letters (xab1cy) in the log, the answer and the
        # diff. API_KEY=12345, PIN=1234 and X-Pin: 99 only where they stand alone. X-Scope: * and Accept: */* are
        # never secrets, so the answer's asterisks stay as they are.
        password, bearer, key = "ab" + "1", "b4" + "r", "q" + "5z"
        pin, api_key, x_pin = "12" + "34", str(12345), str(99)
        server = OnDemandServers.server
        ctx = self.context()
        ctx["servers"] = ctx["servers"] + [
            server("glued", url=f"https://x:{password}@glued.example.com/mcp", on_demand=True,
                   headers={"Authorization": "Bearer " + bearer, "X-Pin": x_pin, "X-Scope": "*", "Accept": "*/*"}),
            server("glued-env", transport="stdio", command="npx", targets=["claude"], on_demand=True,
                   env={"PIN": pin, "API_KEY": api_key, "SHORT_KEY": key})]
        said = (f"glued: x{password}cy x{bearer}y {key}s ({password}) Bearer {bearer}\n"
                f"alone: PIN={pin} API_KEY={api_key} X-Pin: {x_pin}\n"
                f"inside: x{pin} {pin}0 {api_key}6 v{x_pin} {x_pin}9\n"
                "**bold** */* a - b. 2 * 3, - and .\n")
        masked = (f"glued: x***cy x***y ***s (***) ***\n"
                  "alone: PIN=*** API_KEY=*** X-Pin: ***\n"
                  f"inside: x{pin} {pin}0 {api_key}6 v{x_pin} {x_pin}9\n"
                  "**bold** */* a - b. 2 * 3, - and .\n")
        repo = Path(self.tmp) / "glued"
        has_git = bool(runner.shutil.which("git"))
        if has_git:
            subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True)
        else:
            repo.mkdir()
        (repo / "glued.txt").write_text(said, encoding="utf-8")
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.ECHO]})
        cfg["projects"]["glued"] = str(repo)
        job = {**self.job(90, "peek", mcp_add=["glued", "glued-env"], capture_diff=True), "project": "glued",
               "prompt": said}
        finished, lines, _ = run_job(cfg, job, last_context=ctx)
        self.assertEqual(finished["status"], "done", finished)
        self.assertEqual(finished["result"].strip(), masked.strip(), "the answer")
        self.assertEqual([ln for ln in lines if ln.startswith("stderr: said ")],
                         ["stderr: said " + ln for ln in masked.splitlines()], "the log")
        if has_git:
            self.assertIn("=== new file glued.txt ===\n" + masked.strip(), finished["diff"], "the diff")
        file_line = [ln for ln in lines if ln.startswith("stderr: file ")]
        self.assertEqual(len(file_line), 1, lines)
        for shown in ('"https://x:***@glued.example.com/mcp"', '"Authorization": "***"', '"X-Pin": "***"',
                      '"X-Scope": "*"', '"Accept": "*/*"', '"PIN": "***"', '"API_KEY": "***"', '"SHORT_KEY": "***"'):
            self.assertIn(shown, file_line[0])

    def test_mask_takes_a_short_value_only_where_it_stands_alone(self):
        # Fake values, built here. A StandAloneSecret shorter than KNOWN_SECRET_MIN: replaced where no ASCII letter
        # or digit touches it on either side. Longer: replaced wherever it is, as before (a git account's token has 8
        # or more). Any other value, however short (a strong secret, a git secret), wherever it is too.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        alone = runner.StandAloneSecret
        pin, long_secret, strong = alone("12" + "34"), alone("lng" + "-s3cret-77"), "ab" + "1"
        job.mask_values = [pin, long_secret, "", alone("a.c")]
        self.assertEqual(job.mask(f"PIN={pin} ({pin}) '{pin}' {pin}. _{pin}_ é{pin} -{pin}-"),
                         "PIN=*** (***) '***' ***. _***_ é*** -***-")
        self.assertEqual(job.mask(f"x{pin} {pin}0 {pin}{pin} 0{pin} {pin}a"), f"x{pin} {pin}0 {pin}{pin} 0{pin} {pin}a")
        self.assertEqual(job.mask(f"xx{long_secret}yy {long_secret}"), "xx***yy ***")
        self.assertEqual(job.mask("a.c abc a.cd"), "*** abc a.cd", "the value itself, not a pattern")
        self.assertEqual(job.mask(pin), "***")
        job.mask_values.append(strong)
        self.assertEqual(job.mask(f"x{strong}cy ({strong}) x{pin}"), f"x***cy (***) x{pin}")

    def test_a_short_value_with_no_letter_or_digit_is_never_masked(self):
        # Rulings G13c and G13d: a short *, */*, -, . can't be a credential, and masking it would scramble markdown
        # and the *** of values masked before it. One of KNOWN_SECRET_MIN characters or more is masked whatever it
        # holds, as values from known_secrets and git secrets always were.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        text = "**bold** */* a - b. 2 * 3 ___"
        job.mask_values = ["*", "*/*", "-", ".", "___", runner.StandAloneSecret("*"), "ab" + "1"]
        self.assertEqual(job.mask(text + " ab1"), text + " ***")
        long_punctuation = "!@#$%" + "^&*("  # 9 characters, built here
        job.mask_values.append(long_punctuation)
        self.assertEqual(job.mask(f"pw {long_punctuation} end"), "pw *** end")
        entries = {"one": {"type": "http", "url": "https://x:..@mcp.example.com/mcp?key=**&pin=-",
                           "headers": {"X-Scope": "*", "Accept": "*/*", "Authorization": "Bearer --"}},
                   "two": {"type": "stdio", "command": "npx", "args": ["--key", "*", "--pin", "-", "------"],
                           "env": {"SEP_KEY": "-", "DOT_TOKEN": ".", "PIN": "__", "LINE": "=" * 8}}}
        self.assertCountEqual(runner.mcp_entry_secrets(entries), ["Bearer --", "=" * 8, "------"],
                              "the value with letters in it, and the long ones whatever they hold")

    def test_the_job_token_is_masked_before_any_shorter_value(self):
        # Ruling G13d. A short strong secret whose text also occurs inside the job token must not break the token up
        # and leave the rest of it visible: the token is masked first.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        job.token = TOKEN
        piece = TOKEN[4:7]  # a short strong secret (a plain str) that the token also contains
        job.mask_values = [piece]
        masked = job.mask(f"token {TOKEN} and {piece}")
        self.assertEqual(masked, "token *** and ***")
        self.assertNotIn("3n-", masked)

    def test_mask_finds_every_secret_in_the_text_as_it_came(self):
        # Ruling G13e. Every match of the token, of anything shaped like a job token and of each value is found in
        # the original text, overlapping ones merged and each merged stretch replaced once, so no value replaced
        # first can hide another from the mask. Fake values, built here.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        job.token = TOKEN
        job.mask_values = ["pre" + "SECRETpart" + TOKEN]  # a value that holds the token
        self.assertEqual(job.mask("h: preSECRETpart" + TOKEN), "h: ***")
        job.mask_values = [runner.StandAloneSecret("T0k" + "3n")]  # it stands alone inside the token: -T0k3n-
        self.assertEqual(job.mask(f"t {TOKEN} T0k3n"), "t *** ***")
        job.mask_values = ["abc" + "def12", "f12" + "xyz"]  # they overlap, and neither holds the other
        self.assertEqual(job.mask("abcdef12xyz ok"), "*** ok")

    def test_mask_finds_a_job_token_shape_without_a_job_token(self):
        # Ruling G13e: anything shaped like a job token is masked whether or not this job has one.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        job.token = ""
        shaped = "bpj_" + "Qr-34_" * 7 + "y"
        job.mask_values = ["Qr" + "-"]
        masked = job.mask(f"x {shaped} y")
        self.assertEqual(masked, "x *** y")
        self.assertNotIn("34_", masked)

    def test_mask_finds_a_secret_written_as_json(self):
        # Ruling G13e. stdout lines are masked while still raw JSON, where a secret holding " or \ (or a character
        # outside ASCII) is written escaped. Fake values, built here.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        quoted, slashed, accented = 'Kx"9vQ' + "eLm42pw", "ab\\" + "cd1234", "ünï" + "côdé9"
        job.mask_values = [quoted, "9vQ" + "e", slashed, accented]
        for line in (json.dumps({"text": f"pw is {quoted}, {slashed}, {accented}"}),
                     json.dumps({"text": f"pw is {quoted}, {slashed}, {accented}"}, ensure_ascii=False)):
            masked = job.mask(line)
            for piece in ("9vQe", "eLm42pw", "cd1234", "côdé9", "\\u00f4d"):
                self.assertNotIn(piece, masked, line)
        self.assertEqual(job.mask(f"pw is {quoted}"), "pw is ***")

    def test_a_whitespace_value_is_never_masked(self):
        # Ruling G13e: a value of spaces or tabs alone can't be a credential, and masking it would hide indents.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        job.mask_values = [" " * 8, "\t" * 6, "ok" + "1"]
        text = "def f():\n        return ok1\n\t\t\t\t\t\tx"
        self.assertEqual(job.mask(text), "def f():\n        return ***\n\t\t\t\t\t\tx")
        entries = {"one": {"type": "stdio", "command": "npx", "env": {"PAD": " " * 8, "TABS_KEY": "\t" * 3}}}
        self.assertEqual(runner.mcp_entry_secrets(entries), [])

    def test_the_punctuation_rule_stops_at_known_secret_min(self):
        # Rulings G13c and G13d: a punctuation-only value of KNOWN_SECRET_MIN characters is masked, a shorter one not.
        job = runner.Job(argparse.Namespace(cfg={}, hub=LocalHub()), {"id": 88})
        self.assertEqual(runner.KNOWN_SECRET_MIN, 6)
        six, five = "!@#$%" + "^", "!@#" + "$%"
        job.mask_values = [six]
        self.assertEqual(job.mask(f"a {six} b"), "a *** b")
        job.mask_values = [five]
        self.assertEqual(job.mask(f"a {five} b"), f"a {five} b")

    def test_a_strong_short_secret_counts_wherever_it_is(self):
        # Ruling G13b. Fake values, built here. A short URL password, Authorization, Proxy-Authorization or Cookie
        # value and its token after the scheme word, and a header, env, query or flag value under a name SECRET_NAME_RE
        # knows, are masked wherever they are (a plain str); all-digit ones, those only MCP_SECRET_WORDS calls secret,
        # and other headers' values only where they stand alone (StandAloneSecret).
        alone = runner.StandAloneSecret
        pw, tok, pin = "ab" + "1", "b4" + "r", "12" + "34"

        def kinds(**entry) -> list:
            found = runner.mcp_entry_secrets({"one": entry})
            return [(s, isinstance(s, alone)) for s in found if len(s) < runner.KNOWN_SECRET_MIN]

        self.assertEqual(kinds(url=f"https://x:{pw}@mcp.example.com/mcp"), [(pw, False)])
        self.assertEqual(kinds(url=f"https://x:{pin}@mcp.example.com/mcp"), [(pin, True)], "all digits")
        self.assertEqual(kinds(url="https://mcp.example.com/mcp", headers={
            "Authorization": "Bearer " + tok, "Proxy-Authorization": "Basic c5", "Cookie": "s=a1", "X-Api-Key": pw,
            "X-Auth-Pin": pin}),
            [(tok, False), ("c5", False), ("s=a1", False), (pw, False), (pin, True)])
        self.assertEqual(kinds(url="https://mcp.example.com/mcp", headers={"X-Pin": "99", "X-Upstream": "Bearer " + tok}),
                         [("99", True), (tok, True)], "a header value only because it is one")
        self.assertEqual(kinds(env={"API_KEY": "k3y", "MAX_TOKENS": "4096", "PIN": "ab", "SESSION": "s1"}),
                         [("k3y", False), ("4096", True), ("ab", True), ("s1", True)])
        self.assertEqual(kinds(url=f"https://mcp.example.com/mcp?key=q5&pin={pin}&sig=a7&page=2"),
                         [("q5", False), (pin, True), ("a7", True)])
        self.assertEqual(kinds(args=["--api-key", "ab12c", "--token=cd3", "--api-key", "123", "--pin", "ab",
                                     "--sig=a7"]),
                         [("ab12c", False), ("cd3", False), ("123", True), ("ab", True), ("a7", True)])

    def test_a_job_with_picks_and_a_git_account_masks_both(self):
        # One masking list for both features: the git account's token joins the MCP file's secrets.
        git_token = GitAccountEnv.PLAIN_TOKEN  # no known token shape: only the exact value can mask it
        cfg = self.cfg(peek={"preset": "claude", "command": sys.executable, "args": ["-c", self.PEEK]})
        cfg["git_accounts"] = {"lab": {"host": "example.com", "token": git_token}}
        with mock.patch.object(runner, "git_version", return_value=(2, 45)), no_git_url_check():
            finished, lines, _ = run_job(cfg, self.job(83, "peek", mcp_add=["direct-pick"], git_account="lab"),
                                         last_context=self.context())
        self.assertEqual(finished["status"], "done", finished)
        self.assertIn("Git account: lab (example.com)", lines)
        self.assertIn("stderr: git ***", lines)
        self.assertEqual(json.loads(finished["result"])["direct-pick"][0], {"Authorization": "***"})
        text = json.dumps([lines, finished])
        for secret in (git_token, self.DIRECT_SECRET):
            self.assertNotIn(secret, text)
        self.assertFalse((runner.JOBS_DIR / "83").exists(), "one job folder, removed afterwards")

    def test_claude_agents_report_mcp_pick(self):
        def spec(**entry) -> dict:
            return runner.agent_spec({"agents": {"a": entry}}, "a")

        for preset in ("claude", "selftest"):
            self.assertEqual(runner.PRESETS[preset]["strict_mcp_args"], ["--strict-mcp-config"])
            self.assertIn("mcp_pick", runner.agent_features(spec(preset=preset), {}))
        with mock.patch.object(runner, "gemini_job_tools_problem", return_value=None):
            self.assertNotIn("mcp_pick", runner.agent_features(spec(preset="gemini"), {}), "Gemini CLI: later")
        self.assertNotIn("strict_mcp_args", runner.PRESETS["gemini"])
        self.assertNotIn("mcp_pick", runner.agent_features(spec(preset="claude", mcp_config_args=None), {}))
        self.assertNotIn("mcp_pick", runner.agent_features(spec(preset="text", command="mycli"), {}))

    def test_the_selftest_agent_takes_strict_mcp_config(self):
        path = Path(self.tmp) / "mcp.json"
        path.write_text(json.dumps({"mcpServers": {"files": {"type": "stdio", "command": "npx", "args": [],
                                                             "env": {}}}}), encoding="utf-8")
        res = subprocess.run([sys.executable, self.SELFTEST, "--permission-mode", "dontAsk", "--mcp-config", str(path),
                              "--strict-mcp-config"], input="Reply with exactly one word: pong", capture_output=True,
                             text=True, encoding="utf-8", cwd=self.tmp, timeout=60,
                             env=dict(os.environ, SELFTEST_DELAY="0", PYTHONIOENCODING="utf-8"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(json.loads(res.stdout.splitlines()[-1])["result"], "pong")


class PrivateFiles(TempDirCase):
    """write_private replaces a file in one step: CI once caught a reader of remote-control.json
    finding it empty while a server's watcher thread was rewriting it."""

    def test_readers_never_see_half_a_file(self):
        path = Path(self.tmp) / "state.json"
        runner.write_private(path, json.dumps({"n": 0}))
        stop = threading.Event()

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                runner.write_private(path, json.dumps({"n": n, "pad": "x" * 2000}))

        thread = threading.Thread(target=writer)
        thread.start()
        bad = 0
        try:
            for _ in range(3000):
                try:
                    read_json(path)  # on Windows, a refused open is retried: only content counts here
                except ValueError:
                    bad += 1
        finally:
            stop.set()
            thread.join()
        self.assertEqual(bad, 0, "a reader saw the file empty or half written")
        self.assertEqual([p.name for p in Path(self.tmp).iterdir()], ["state.json"], "no temporary file is left")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


# ------------------------------------------------------------------ job summaries for shared memory
class SummaryTurn(TempDirCase):
    """After a job that ends done with options.summarize, the runner resumes the job's session once with the agent's
    readonly level and SUMMARY_PROMPT: no job tools, no MCP picks or other MCP servers (--strict-mcp-config where the
    agent has it), no git account, a 3-minute limit, and never with permission flags outside the levels. The answer,
    masked with the job's own mask and then cut to 2,000 characters, goes back as the result's `summary`. A summary
    turn that fails leaves the job as it was and adds one log line."""

    SELFTEST = str(ROOT / "runner" / "selftest_agent.py")
    PROMPT = ("Summarize this conversation for a shared notes file other AI agents will read. At most 8 short lines: "
              "what was asked; what you did or found; files you changed; anything left open. Plain text, no preamble.")
    START = "Writing a summary for shared memory (up to 3 minutes)."
    GIT_TOKEN = "Unit4Fake" * 4  # no known token prefix: only the exact value can mask it
    MCP_SECRET = "summary-pick-secret-7"
    # Claude Code's stream-json from a script: the session id $SESSION (none when it is empty); to the summary
    # prompt it answers nothing and exits with $SUMMARY_EXIT.
    FAKE = "\n".join([
        "import json, os, sys",
        "prompt = sys.stdin.read()",
        "if prompt.startswith('Summarize this conversation'):",
        "    sys.exit(int(os.environ.get('SUMMARY_EXIT', '0')))",
        "ev = {'type': 'result', 'subtype': 'success', 'result': 'done'}",
        "if os.environ.get('SESSION'):",
        "    ev['session_id'] = os.environ['SESSION']",
        "print(json.dumps(ev), flush=True)",
    ])

    def setUp(self) -> None:
        super().setUp()
        home = Path(self.tmp) / "home"  # no ~/.netrc or git config of the person running the tests counts
        home.mkdir()
        for patch in (mock.patch.object(runner, "HOME", home), mock.patch.dict(os.environ, {"HOME": str(home)}),
                      mock.patch.object(runner, "git_version", return_value=(2, 45))):
            patch.start()
            self.addCleanup(patch.stop)
        without_git = no_git_url_check()  # a job with a git account, where git isn't installed (CI's 3.9 image)
        without_git.__enter__()
        self.addCleanup(without_git.__exit__, None, None, None)
        os.environ.pop("BULLPEN_TEST_IDLE_SECONDS", None)  # (put back with the environment)

    def cfg(self, **env) -> dict:
        return make_cfg(self.tmp, {"one": {"preset": "selftest", "env": {**self.selftest_env, **env}}})

    @staticmethod
    def job(job_id: int, agent: str = "one", **options) -> dict:
        return {"id": job_id, "agent": agent, "project": "chat", "autonomy": "edits", "model": "fake-small",
                "prompt": "Reply with exactly one word: pong", "options": {"timeout_min": 2, **options}}

    def summary_turn(self, session: str) -> list:
        """The summary turn's command line for the selftest agent: the readonly level, no MCP server at all, the job's
        model, its session."""
        return [sys.executable, self.SELFTEST, "--permission-mode", "dontAsk", "--strict-mcp-config",
                "--model", "fake-small", "--resume", session]

    @staticmethod
    def not_written(lines: list) -> list:
        return [ln for ln in lines if ln.startswith("Summary not written")]

    @staticmethod
    def run_job(cfg: dict, data: dict, **stub) -> tuple:
        """run_job's finish report and log, and each agent started with its environment: (argv, env) pairs (not the
        git the runner asks about the job's git settings, nor the taskkill that stops a turn on Windows)."""
        hub = LocalHub()
        started: list = []
        real_popen = subprocess.Popen

        def popen(argv, *args, **kwargs):
            if list(argv)[:1] not in (["git"], ["taskkill"]):
                started.append((list(argv), dict(kwargs.get("env") or {})))
            return real_popen(argv, *args, **kwargs)

        job = runner.Job(argparse.Namespace(cfg=cfg, hub=hub, **stub), {"options": {"timeout_min": 2}, **data})
        with mock.patch.object(runner.subprocess, "Popen", popen):
            job.run()
        return hub.finished, hub.lines, started

    def test_a_done_job_that_asks_for_one_gets_a_summary(self):
        finished, lines, started = self.run_job(self.cfg(), self.job(201, summarize=True))
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        self.assertEqual(finished["summary"], selftest_agent.SUMMARY_ANSWER)
        self.assertEqual([argv for argv, _ in started],
                         [[sys.executable, self.SELFTEST, "--permission-mode", "acceptEdits", "--model", "fake-small"],
                          self.summary_turn(finished["session_id"])],
                         "a second run: the readonly level whatever the job's, the same model, the job's session")
        self.assertIn(self.START, lines)
        self.assertEqual(self.not_written(lines), [])
        # The summary turn's tokens count toward the job's (ruling C6): one more result of the same conversation.
        self.assertEqual(finished["usage"]["turns"], 2)
        self.assertEqual(finished["usage"]["own"]["output_tokens"], 400, "both turns' shares")
        self.assertEqual(finished["usage"]["reported"]["output_tokens"], 400, "the conversation's newest total")
        # A follow-up's summary continues the session it continued.
        finished, _, started = self.run_job(self.cfg(), {**self.job(202, summarize=True), "resume_session": "s-202"})
        self.assertEqual((finished["session_id"], finished["summary"]), ("s-202", selftest_agent.SUMMARY_ANSWER))
        self.assertEqual(started[1][0], self.summary_turn("s-202"))

    def test_only_a_done_job_that_asks_for_one(self):
        cfg = self.cfg()
        cfg["accept_delegated_tasks"] = True
        failing = self.cfg(SELFTEST_FAIL="1")
        for job_id, config, options in ((211, cfg, {}), (212, cfg, {"summarize": False}),
                                        (213, cfg, {"summarize": "yes"}),  # only true counts
                                        (214, failing, {"summarize": True}),  # a failed job
                                        (215, cfg, {"summarize": True, "delegated": True})):  # a subtask never
            with self.subTest(job_id=job_id):
                finished, lines, started = self.run_job(config, self.job(job_id, **options))
                self.assertEqual(finished["status"], "error" if config is failing else "done", finished)
                self.assertEqual(len(started), 1, "no second run")
                self.assertNotIn("summary", finished)
                self.assertEqual([ln for ln in lines if ln == self.START or ln.startswith("Summary")], [])

    def test_a_summary_turn_that_fails_leaves_the_job_as_it_was(self):
        for job_id, env, reason in ((221, {"SELFTEST_SUMMARY": "fail"}, "the agent reported an error."),
                                    (222, {"SELFTEST_SUMMARY": "empty"}, "the agent's answer was empty.")):
            with self.subTest(reason=reason):
                finished, lines, started = self.run_job(self.cfg(**env), self.job(job_id, summarize=True))
                self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
                self.assertNotIn("summary", finished)
                self.assertNotIn("error", finished)
                self.assertEqual(len(started), 2)
                self.assertEqual(self.not_written(lines), ["Summary not written: " + reason])
                self.assertLess(lines.index(self.START), lines.index("Summary not written: " + reason))

    def test_why_a_summary_wasnt_written(self):
        resume = {"resume": ["--resume", "{session_id}"]}
        for job_id, entry, env, reason, runs in (
                (223, resume, {"SESSION": "s-223", "SUMMARY_EXIT": "3"}, "the agent exited with code 3.", 2),
                (224, resume, {"SESSION": "s-224"}, "the agent's answer was empty.", 2),
                (225, resume, {"SESSION": ""}, "the agent reported no session to continue.", 1),
                # After --resume, an id that starts with "-" would be read as another flag.
                (227, resume, {"SESSION": "--dangerously-skip-permissions"},
                 "the job's session id isn't a plain id (letters, digits, '.', '_', ':' and '-').", 1),
                # Its entry changed after the hub last heard from it: no resume by the session's id.
                (226, {"resume": ["--continue"]}, {"SESSION": "s-226"},
                 "fake can't resume the job's session by its id with a readonly level here.", 1)):
            with self.subTest(reason=reason):
                agent = {"preset": "text", "command": sys.executable, "args": ["-c", self.FAKE], "label": "fake",
                         "parser": "claude-stream", "env": env, **entry}
                cfg = make_cfg(self.tmp, {"fake": agent})
                finished, lines, started = self.run_job(cfg, {**self.job(job_id, "fake", summarize=True), "model": ""})
                self.assertEqual((finished["status"], finished["result"]), ("done", "done"), finished)
                self.assertNotIn("summary", finished)
                self.assertEqual(len(started), runs)
                self.assertEqual(self.not_written(lines), ["Summary not written: " + reason])

    def test_a_summary_turn_that_takes_too_long(self):
        with mock.patch.object(runner, "SUMMARY_TIMEOUT", 0.5):
            finished, lines, started = self.run_job(self.cfg(SELFTEST_DELAY="1.5"), self.job(231, summarize=True))
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        self.assertNotIn("summary", finished)
        self.assertEqual(len(started), 2)
        self.assertIn("Writing a summary for shared memory (up to 0.5 seconds).", lines)
        self.assertEqual(self.not_written(lines), ["Summary not written: no answer within 0.5 seconds."])
        self.assertEqual(finished["usage"]["turns"], 1, "the turn that was stopped reported nothing")

    def test_no_job_tools_picks_or_git_account_and_the_summary_is_masked(self):
        cfg = self.cfg(SELFTEST_SUMMARY=f"pushed with {self.GIT_TOKEN}, asked with {TOKEN}, read with {self.MCP_SECRET}")
        cfg["git_accounts"] = {"lab": {"host": "example.com", "token": self.GIT_TOKEN}}
        ctx = {"mcp_enabled": True, "mcp_token": "bpm_" + "S0m-" * 10, "platforms": OnDemandServers.EVERYWHERE,
               "servers": [OnDemandServers.server("direct-pick", url="https://mcp.example.com/mcp", on_demand=True,
                                                  headers={"Authorization": "Bearer " + self.MCP_SECRET})]}
        data = {**self.job(241, summarize=True, git_account="lab", mcp_add=["direct-pick"]),
                "job_token": TOKEN, "job_tools": ["job_info"]}
        finished, lines, started = self.run_job(cfg, data, last_context=ctx)
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        (job_argv, job_env), (argv, env) = started
        self.assertIn("--mcp-config", job_argv)
        self.assertIn("--allowedTools", job_argv)
        self.assertEqual(job_env["BULLPEN_GIT_TOKEN"], self.GIT_TOKEN, "the job itself had them all")
        self.assertEqual(argv, self.summary_turn(finished["session_id"]), "no --mcp-config, no --allowedTools")
        self.assertEqual({var: env.get(var) for var in GitAccountEnv.JOB_VARS},
                         {var: os.environ.get(var) for var in GitAccountEnv.JOB_VARS},
                         "the runner's own environment: no git account")
        self.assertEqual(env["PWD"], self.tmp)
        self.assertEqual(finished["summary"], "pushed with ***, asked with ***, read with ***")
        text = json.dumps([lines, finished])
        for secret in (self.GIT_TOKEN, TOKEN, self.MCP_SECRET):
            self.assertNotIn(secret, text)

    def test_a_narrowed_tasks_summary_reaches_no_mcp_server(self):
        # The job may use only the server it lists (--strict-mcp-config); its summary turn gets none at all, never the
        # machine's other servers.
        ctx = {"mcp_enabled": True, "mcp_token": "bpm_" + "S0m-" * 10, "platforms": OnDemandServers.EVERYWHERE,
               "servers": [OnDemandServers.server("docs", url="https://mcp.example.com/mcp", on_demand=True)]}
        finished, _, started = self.run_job(self.cfg(), self.job(242, summarize=True, mcp_only=["docs"]),
                                            last_context=ctx)
        self.assertEqual((finished["status"], finished["summary"]), ("done", selftest_agent.SUMMARY_ANSWER), finished)
        (job_argv, _), (argv, _) = started
        self.assertIn("--mcp-config", job_argv)
        self.assertIn("--strict-mcp-config", job_argv)
        self.assertEqual(argv, self.summary_turn(finished["session_id"]), "--strict-mcp-config with no --mcp-config")

    def test_permission_flags_outside_the_levels_mean_no_summary(self):
        # A later --permission-mode or --dangerously-skip-permissions would win over the readonly level.
        for job_id, extra in ((243, ["--dangerously-skip-permissions"]),
                              (244, ["--permission-mode", "bypassPermissions"])):
            with self.subTest(extra=extra):
                cfg = self.cfg()
                cfg["agents"]["one"]["extra_args"] = extra
                finished, lines, started = self.run_job(cfg, self.job(job_id, summarize=True))
                self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
                self.assertNotIn("summary", finished)
                self.assertEqual(len(started), 1, "no summary turn")
                self.assertEqual(self.not_written(lines), [
                    f"Summary not written: one has permission flags outside its levels in runner.json ({extra[0]}), "
                    "so a readonly turn might not only read."])

    # Claude Code before CUMULATIVE_FROM, from a script: each run reports its own figures only (5 output tokens for
    # the job, 7 for the summary turn), in modelUsage or, with $NO_MODEL_USAGE, only in usage; no per-turn shares.
    OLD_CLI = "\n".join([
        "import json, os, sys",
        "summary = sys.stdin.read().startswith('Summarize this conversation')",
        "out = 7 if summary else 5",
        "for ev in ({'type': 'system', 'subtype': 'init', 'session_id': 's-old', 'claude_code_version': '2.1.276'},",
        "           {'type': 'result', 'subtype': 'success', 'session_id': 's-old', 'total_cost_usd': 0.01,",
        "            'result': 'It was a test.' if summary else 'done',",
        "            'usage': {'input_tokens': 1, 'output_tokens': out}}):",
        "    if ev['type'] == 'result' and not os.environ.get('NO_MODEL_USAGE'):",
        "        ev['modelUsage'] = {'m': {'inputTokens': 1, 'outputTokens': out, 'costUSD': 0.01}}",
        "    print(json.dumps(ev), flush=True)",
    ])

    def test_usage_of_a_claude_code_that_reports_each_run_alone(self):
        # Its figures don't cover the session it resumed, so the job's and the summary turn's are added up (ruling C6).
        for job_id, env, scope in ((245, {}, None), (246, {"NO_MODEL_USAGE": "1"}, "run")):
            with self.subTest(env=env):
                agent = {"preset": "text", "command": sys.executable, "args": ["-c", self.OLD_CLI], "label": "old",
                         "parser": "claude-stream", "resume": ["--resume", "{session_id}"], "env": env}
                finished, lines, started = self.run_job(make_cfg(self.tmp, {"old": agent}),
                                                        {**self.job(job_id, "old", summarize=True), "model": ""})
                self.assertEqual((finished["status"], finished["summary"]), ("done", "It was a test."), finished)
                usage = finished["usage"]
                self.assertEqual((usage["turns"], usage["reported"]["output_tokens"], usage["reported"]["cost_usd"]),
                                 (2, 12, 0.02), "the job's 5 and the summary's 7, not the summary's alone")
                self.assertIsNone(usage["own"], "no shares: the hub goes by reported")
                self.assertEqual(usage["reported"].get("scope"), scope, "a sum of run figures stays marked as such")
                self.assertEqual(usage["cli_version"], "2.1.276")

    def test_runs_added_up_by_claude_code_version(self):
        def tracker(out: int, version, model_usage: bool = True) -> runner.UsageTracker:
            t = runner.UsageTracker("claude")
            t.cli_version = version
            ev = {"type": "result", "usage": {"input_tokens": 1, "output_tokens": out}, "total_cost_usd": 0.01}
            if model_usage:
                ev["modelUsage"] = {"m": {"inputTokens": 1, "outputTokens": out, "costUSD": 0.01}}
            t.add_claude(ev)
            return t

        for version, model_usage, summary_out, want in (
                ("2.1.400", True, 12, 12),   # a total for the whole conversation: the newest one
                ("2.1.277", True, 12, 12),   # the first version that counts resumed totals
                (None, True, 12, 12),        # unknown counts as one that does, as on the hub
                ("2.1.276", True, 7, 12),    # this run's only: added up
                ("2.1.400", False, 7, 12)):  # no modelUsage, this run's usage only: added up
            with self.subTest(version=version, model_usage=model_usage):
                job = tracker(5, version, model_usage)
                job.add_run(tracker(summary_out, version, model_usage))
                self.assertEqual(job.total()["reported"]["output_tokens"], want)
                self.assertEqual(job.total()["reported"].get("scope"), None if model_usage else "run")
        self.assertEqual([runner.cumulative_cli(v) for v in ("2.1.276", "2.1.277", "2.2", "3", "", "2.x.1", None)],
                         [False, True, True, True, True, True, True])
        # The hub's copy of the version (hub/limits.py), read as text: the hub needs a newer Python than the runner.
        hub = (ROOT / "hub" / "limits.py").read_text(encoding="utf-8")
        found = re.search(r"^CUMULATIVE_FROM = \(([0-9, ]+)\)$", hub, re.M)
        self.assertIsNotNone(found)
        self.assertEqual(tuple(int(x) for x in found.group(1).split(",")), runner.CUMULATIVE_FROM)

    def test_the_summary_is_cut_after_it_is_masked(self):
        cfg = self.cfg(SELFTEST_SUMMARY="y" * 1990 + self.GIT_TOKEN + "z" * 600)
        cfg["git_accounts"] = {"lab": {"host": "example.com", "token": self.GIT_TOKEN}}
        finished, _, _ = self.run_job(cfg, self.job(251, summarize=True, git_account="lab"))
        self.assertEqual(finished["summary"], "y" * 1990 + "***" + "z" * 7, "cut first, part of the token would stay")
        self.assertEqual(len(finished["summary"]), runner.SUMMARY_MAX_CHARS)

    def test_a_live_session_is_summarized_after_it_closes(self):
        def end(hub, turn_no):
            hub.end = True

        hub = LiveHub(end)
        finished, started = run_with(hub, self.cfg(), {
            "id": 261, "agent": "one", "project": "chat", "autonomy": "readonly", "prompt": "only message",
            "options": {"timeout_min": 2, "live": True, "summarize": True}})
        self.assertEqual((finished["status"], finished["result"]), ("done", "Self-test reply 1: only message"), finished)
        self.assertEqual(finished["summary"], selftest_agent.SUMMARY_ANSWER)
        self.assertIn("--input-format", started[0])
        self.assertEqual(started[1], [sys.executable, self.SELFTEST, "--permission-mode", "dontAsk",
                                      "--strict-mcp-config", "--resume", finished["session_id"]], "not live")
        self.assertLess(hub.lines.index("⏹ Session ended from the dashboard."), hub.lines.index(self.START))

    def test_a_plain_jobs_summary_hides_the_machines_secrets(self):
        # A job with no git account and no MCP picks has no exact values of its own to mask (only job-token shapes):
        # its summary is masked with the machine's known secrets, an API key in the agent's env here.
        key = "Svc" + "Fake7" * 4  # no known prefix: only the exact value can mask it
        cfg = self.cfg(SERVICE_API_KEY=key, SELFTEST_SUMMARY=f"read the key {key}")
        finished, lines, _ = self.run_job(cfg, self.job(281, summarize=True))
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        self.assertEqual(finished["summary"], "read the key ***")
        self.assertNotIn(key, json.dumps([lines, finished]))

    def test_a_gemini_job(self):
        # With the job tools (in its settings file, through GEMINI_CLI_SYSTEM_DEFAULTS_PATH), which its summary
        # turn doesn't get.
        for name in ("gemini_job_tools_problem", "gemini_settings_problem"):
            patch = mock.patch.object(runner, name, return_value=None)
            patch.start()
            self.addCleanup(patch.stop)
        cfg = make_cfg(self.tmp, {"gem": {"preset": "gemini", "command": sys.executable,
                                          "args": [self.SELFTEST, "--gemini"], "env": self.selftest_env}})
        data = {**self.job(271, "gem", summarize=True), "job_token": TOKEN, "job_tools": ["job_info"]}
        with mock.patch.dict(os.environ):
            os.environ.pop(runner.GEMINI_DEFAULTS_VAR, None)
            finished, _, started = self.run_job(cfg, data)
        self.assertEqual((finished["status"], finished["result"]), ("done", "pong"), finished)
        self.assertEqual(finished["summary"], selftest_agent.SUMMARY_ANSWER)
        (_, job_env), (argv, env) = started
        self.assertIn(runner.GEMINI_DEFAULTS_VAR, job_env, "the job had its tools")
        self.assertNotIn(runner.GEMINI_DEFAULTS_VAR, env, "the summary turn has none")
        self.assertEqual(argv, [sys.executable, self.SELFTEST, "--gemini", "--approval-mode", "default",
                                "--model", "fake-small", "--resume", finished["session_id"]])
        usage = finished["usage"]
        self.assertEqual((usage["turns"], usage["reported"]["output_tokens"], usage["own"]["output_tokens"]),
                         (2, 200, 200), "Gemini CLI reports each run's own figures: added up")

    # Gemini CLI's stream-json from a script: an init event with the session id $SESSION (none when it is empty),
    # then an answer.
    GEMINI_FAKE = "\n".join([
        "import json, os, sys",
        "sys.stdin.read()",
        "ev = {'type': 'init', 'model': 'm'}",
        "if os.environ.get('SESSION'):",
        "    ev['session_id'] = os.environ['SESSION']",
        "for ev in (ev, {'type': 'message', 'role': 'assistant', 'content': 'done'},",
        "           {'type': 'result', 'status': 'success'}):",
        "    print(json.dumps(ev), flush=True)",
    ])

    def test_a_gemini_summary_continues_only_the_session_it_reported(self):
        # Gemini CLI reads "--resume latest" or a bare number as the folder's newest session or one by its index,
        # which may be another job's. So its summary turn continues only an id the agent reported in this run: never
        # the hub's resume id, "latest" or a number.
        for job_id, resume, session in ((272, "latest", ""), (273, "2", ""), (274, "s-274", ""),
                                        (275, "", "latest"), (276, "s-276", "7")):
            with self.subTest(resume=resume, session=session):
                agent = {"preset": "gemini", "command": sys.executable, "args": ["-c", self.GEMINI_FAKE],
                         "env": {"SESSION": session}}
                data = {**self.job(job_id, "gem", summarize=True), "model": "", "resume_session": resume}
                finished, lines, started = self.run_job(make_cfg(self.tmp, {"gem": agent}), data)
                self.assertEqual((finished["status"], finished["result"]), ("done", "done"), finished)
                self.assertNotIn("summary", finished)
                self.assertEqual(len(started), 1, "no summary turn")
                self.assertEqual(self.not_written(lines),
                                 ["Summary not written: the agent reported no session to continue."])

    def test_which_agents_can_write_one(self):
        def features(**entry) -> list:
            return runner.agent_features(runner.agent_spec({"agents": {"a": entry}}, "a"), {})

        with mock.patch.object(runner, "gemini_job_tools_problem", return_value=None):
            for preset in ("claude", "selftest", "gemini"):
                self.assertIn("summary", features(preset=preset), preset)
        self.assertNotIn("summary", features(preset="text", command="mycli"), "no resume, no session id")
        self.assertNotIn("summary", features(preset="selftest", resume=["--continue"]),
                         "a resume without the session's id could continue another job's session")
        self.assertNotIn("summary", features(preset="claude", resume=None))
        self.assertNotIn("summary", features(preset="claude", autonomy={"readonly": None}), "no readonly level")
        self.assertNotIn("summary", features(preset="claude", extra_args=["--dangerously-skip-permissions"]),
                         "a permission flag outside the levels would win over readonly")
        self.assertNotIn("summary", features(preset="gemini", args=["--output-format", "stream-json", "--yolo"]))
        resume = ["--resume", "{session_id}"]
        self.assertNotIn("summary", features(preset="text", command="mycli", resume=resume),
                         "a text agent reports no session id")
        self.assertIn("summary", features(preset="text", command="mycli", resume=resume,
                                          new_session=["--session-id", "{new_session_id}"]),
                      "the runner makes the session's id")

    def test_the_prompt_is_the_designs(self):
        self.assertEqual(runner.SUMMARY_PROMPT, self.PROMPT)
        self.assertEqual(selftest_agent.SUMMARY_PROMPT, runner.SUMMARY_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
