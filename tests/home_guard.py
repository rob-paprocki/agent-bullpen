"""Keep the tests' runners out of the real home folder.

The runner finds its home with Path.home(), as Claude Code and Gemini CLI find theirs with Node's
os.homedir(). On Linux and macOS both read HOME; on Windows both read USERPROFILE and ignore HOME. A test
that gives a runner a home of its own must set both, or on Windows the runner's agent-settings sync (and
the real `claude mcp add-json`) writes into the real ~/.claude, ~/.claude.json, ~/.gemini and
~/.local/bin of whoever runs the tests.

    home_env(home, ...)   an environment whose home is `home`, checked before it is used
    snapshot()            what the runner could write in the real home, before the tests
    changed(before)       which of those the tests created, changed or removed

Standard library only, Python 3.9 and newer (tests/test_runner_units.py imports it).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"
# Settings that point an agent's own settings somewhere other than its home: a runner under test must not
# inherit this computer's values.
AGENT_HOME_VARS = ("CLAUDE_CONFIG_DIR", "GEMINI_CLI_HOME")

_checked: set = set()


def same_path(a, b) -> bool:
    a, b = os.path.normpath(str(a)), os.path.normpath(str(b))
    return os.path.normcase(a) == os.path.normcase(b)


def set_home(env: dict, home) -> dict:
    """Point every variable that names the home folder at `home` (HOME, and on Windows USERPROFILE,
    HOMEDRIVE and HOMEPATH too). Changes `env` in place and returns it."""
    home = str(home)
    env["HOME"] = home
    if IS_WINDOWS:
        env["USERPROFILE"] = home
        drive, rest = os.path.splitdrive(home)
        env["HOMEDRIVE"], env["HOMEPATH"] = drive, rest or "\\"
    return env


def check_home(env: dict, home) -> None:
    """Fail before anything starts unless a Python started with `env` finds its home at `home`."""
    key = (str(home), sys.executable)
    if key in _checked:
        return
    probe = "import os, pathlib; print(pathlib.Path.home()); print(os.path.expanduser('~'))"
    res = subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True, text=True, timeout=60)
    found = res.stdout.splitlines()
    if res.returncode != 0 or len(found) != 2 or not all(same_path(f, home) for f in found):
        raise AssertionError(
            f"the test's home folder isn't isolated: Python started with it finds its home at {found or res.stderr!r}, "
            f"not {home}. Stopped before any runner started, so nothing was written to the real home.")
    _checked.add(key)


def home_env(home, base: dict | None = None, bullpen_home: bool = True, **extra: str) -> dict:
    """A copy of `base` (this process's environment by default) for a runner or agent whose home is `home`:
    the home variables set, BULLPEN_HOME at home/.bullpen (unless `bullpen_home` is False), the agents'
    own settings folders left to follow the home, and `extra` on top. Checked with check_home()."""
    env = dict(os.environ if base is None else base)
    set_home(env, home)
    for key in AGENT_HOME_VARS:
        env.pop(key, None)
    if bullpen_home:
        env["BULLPEN_HOME"] = str(Path(home) / ".bullpen")
    env.update(extra)
    check_home(env, home)
    return env


# ------------------------------------------------------------------ the real home, before and after
# Everything the runner's settings sync, `runner.py init` and the setup stand-ins write under a home
# (runner/runner.py Wiring, init). Paths relative to the home. Kept to these, not the whole folders: the
# person running the tests may be using Claude Code or Gemini CLI meanwhile, which change other files there.
FILES = (".claude/CLAUDE.md", ".local/bin/bullpen", ".local/bin/bullpen.cmd", ".gemini/GEMINI.md",
         ".gemini/bullpen-shared.md", ".gemini/.env", ".gemini/config/mcp_config.json", "setup-args.txt",
         "setup-had-code.txt")
MCP_FILES = (".claude.json", ".gemini/settings.json")  # only their mcpServers: other keys change all the time
TREES = (".claude/skills/remember", ".claude/skills/second-opinion", ".agents/skills/remember", ".gemini/commands",
         ".gemini/skills", ".gemini/config/skills")
FOLDERS = (".gemini", ".bullpen", ".claude-work", ".claude-api")  # whether they exist
HOMES = "homes"  # the snapshot's key for the folders it looked in


def real_homes() -> list:
    """The home folders a runner might take for its own: Path.home(), and HOME when it differs (on Windows,
    Git Bash sets HOME)."""
    homes = [Path.home()]
    if os.environ.get("HOME") and not same_path(os.environ["HOME"], homes[0]):
        homes.append(Path(os.environ["HOME"]))
    return homes


def _file(path: Path):
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
    except OSError as err:
        return f"unreadable: {type(err).__name__}"


def _mcp_servers(path: Path):
    # Claude Code rewrites ~/.claude.json often, so a read can catch it halfway: try a few times.
    for attempt in range(5):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            time.sleep(0.2 * (attempt + 1))
            continue
        servers = data.get("mcpServers") if isinstance(data, dict) else None
        return hashlib.sha256(json.dumps(servers, sort_keys=True).encode()).hexdigest()
    return "unreadable"


def _tree(path: Path):
    if not path.is_dir():
        return None if not path.exists() else _file(path)
    digest = hashlib.sha256()
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(item.relative_to(path).as_posix().encode() + b"\0" + (_file(item) or "").encode() + b"\0")
    return digest.hexdigest()


def snapshot(homes: list | None = None) -> dict:
    """A fingerprint of each path above in each real home (real_homes() by default): a hash, never the
    contents (some hold tokens). Take it before a test changes this process's home variables."""
    homes = real_homes() if homes is None else homes
    out: dict = {HOMES: [str(h) for h in homes]}
    for n, home in enumerate(homes):
        label = "~" if n == 0 else "$HOME"
        for rel in FILES:
            out[f"{label}/{rel}"] = _file(home / rel)
        for rel in MCP_FILES:
            out[f"{label}/{rel} (mcpServers)"] = _mcp_servers(home / rel)
        for rel in TREES:
            out[f"{label}/{rel}/"] = _tree(home / rel)
        for rel in FOLDERS:
            out[f"{label}/{rel} (exists)"] = (home / rel).exists()
    return out


def changed(before: dict) -> list:
    """What differs from `before`, one line per path: created, changed or removed. Names only."""
    after = snapshot([Path(h) for h in before[HOMES]])
    lines = []
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old == new:
            continue
        if old in (None, False):
            lines.append(f"created: {key}")
        elif new in (None, False):
            lines.append(f"removed: {key}")
        else:
            lines.append(f"changed: {key}")
    return lines


def report(before: dict, what: str) -> str:
    """'' when the real home is as it was, else the message to fail with."""
    lines = changed(before)
    if not lines:
        return ""
    return (f"{what} wrote into the real home folder ({', '.join(before[HOMES])}), which a test "
            "must never touch:\n  " + "\n  ".join(lines) +
            "\nA test started a runner or agent without home_env(). (A real runner on this computer syncing during "
            "the tests, or you tidying up, can also change these.)")
