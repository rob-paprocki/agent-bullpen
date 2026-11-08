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
import contextlib
import datetime
import http.server
import io
import json
import os
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
        login = ["job_mcp", "approvals", "live", "levels", "limits", "usage_check", "remote_control"]
        self.assertEqual(runner.agent_features(self.spec(preset="claude"), {}), login)
        self.assertEqual(runner.agent_features(self.spec(preset="selftest"), {}), login)
        with mock.patch.object(runner, "gemini_job_tools_problem", return_value=None):
            self.assertEqual(runner.agent_features(self.spec(preset="gemini"), {}), ["job_mcp", "levels"],
                             "where Gemini CLI would read the job's settings file, Gemini takes the subtask tools "
                             "(but can't ask the owner)")
            self.assertEqual(runner.agent_features(self.spec(preset="gemini", job_mcp=None), {}), ["levels"])
        with mock.patch.object(runner, "gemini_job_tools_problem", return_value="the runner doesn't run as root"):
            self.assertEqual(runner.agent_features(self.spec(preset="gemini"), {}), ["levels"],
                             "elsewhere it takes subtasks but can't hand them out")
        self.assertEqual(runner.agent_features(self.spec(preset="text", command="mycli"), {}), [])
        self.assertEqual(runner.agent_features(self.spec(command="mycli"), {}), [], "no preset is a text agent")

    def test_limits_by_sign_in(self):
        spec = self.spec(preset="claude")
        both = ["job_mcp", "approvals", "live", "levels"]  # whatever the sign-in
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
                         ["job_mcp", "approvals", "live", "limits", "usage_check", "remote_control"])
        spec = self.spec(preset="claude", autonomy_env={"full": {"X": "1"}})
        self.assertEqual(runner.agent_features(spec, {}),
                         ["job_mcp", "approvals", "live", "limits", "usage_check", "remote_control"])
        self.assertFalse(self.spec(preset="claude")["autonomy_overridden"])
        spec = self.spec(preset="claude", autonomy={"ask": None})
        self.assertNotIn("approvals", runner.agent_features(spec, {}), "no 'ask' level, no approvals")

    def test_job_tools_can_be_turned_off(self):
        self.assertEqual(runner.agent_features(self.spec(preset="claude", job_mcp=None), {}),
                         ["live", "levels", "limits", "usage_check", "remote_control"],
                         "without the job tools nothing can ask the owner")

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
        self.assertEqual(st["one"]["features"], ["job_mcp", "approvals", "live", "levels", "limits", "usage_check",
                                                 "remote_control"])
        self.assertEqual(st["tok"]["features"], ["job_mcp", "approvals", "live", "levels", "limits"])
        self.assertEqual(st["one"]["terminal"], "selftest --resume {session_id}")
        self.assertFalse(st["gone"]["ready"])
        self.assertEqual(st["gone"]["features"], ["job_mcp", "approvals", "live", "levels", "limits", "usage_check",
                                                  "remote_control"], "reported even when not installed")


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
        r.limits.record("one", {"five_hour": {"used": 0.2, "observed_at": NOW, "source": "job"}}, None)
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
        for said in ("Couldn't verify Remote Control eligibility — the feature-flag service was unreachable (offline "
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
