"""Adding machines: one-time enrollment codes, per-machine tokens and the bootstrap scripts.

How it works
------------
1. On the dashboard, "Add a machine" asks the hub for a code (POST /api/enroll-codes). The code
   is random, works once and only for an hour. The hub keeps only its sha256.
2. The dashboard shows a one-line command with the code in it. On the new machine it downloads
   a short bootstrap script from /enroll/<code>/install.sh (Linux, macOS) or install.ps1
   (Windows). No dashboard login is needed for that: the unguessable, short-lived code is the
   secret. Reading the script doesn't use the code up.
3. The bootstrap downloads the runner and the machine setup script from the hub
   (/enroll/<code>/files/<name>, checked against sha256 sums written into the bootstrap), then
   runs the setup. The setup trades the code for the machine's own runner token
   (POST /api/enroll), which is shown once, saved in ~/.bullpen/runner.json, and kept on the hub
   only as a sha256.

This module has the pieces that don't need the web framework; the routes are in app.py.
The bootstrap templates are install.sh and install.ps1 next to this file.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HUB_DIR = HERE.parent

CODE_TTL = 3600                                   # seconds a code stays valid
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"     # no 0/O or 1/I/L, so it can be read aloud or typed
CODE_GROUPS = 4                                   # 4 groups of 4 = 80 random bits
CODE_RE = re.compile(r"^[2-9A-HJ-NP-Z]{16}$")
HINT_RE = re.compile(r"^[2-9A-HJ-NP-Z]{4}$")
TOKEN_PREFIX = "bp_"                              # machine tokens start with this (handy when redacting logs)

# The machine setup scripts the bootstrap runs. The installer copies them next to the hub
# (setup_dist/); in a development checkout they are in ../scripts.
SETUP_DIST = Path(os.environ.get("HUB_SETUP_DIST") or (
    HUB_DIR / "setup_dist" if (HUB_DIR / "setup_dist").is_dir() else HUB_DIR.parent / "scripts"))
SETUP_SCRIPTS = {"sh": "setup-workstation.sh", "ps1": "Setup-Workstation.ps1"}
RUNNER_FILE_RE = re.compile(r"^[A-Za-z0-9_]+\.py$")

# Hub addresses as the bootstrap scripts embed them: scheme, host (or [IPv6]), optional port and
# path, and nothing a shell or PowerShell would treat specially.
URL_RE = re.compile(r"^https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?$")


# ------------------------------------------------------------------ secrets
def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(4 * CODE_GROUPS))
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def normalize_code(raw: str | None) -> str | None:
    """'k7qm-2xta 9rfd wp3h' -> 'K7QM2XTA9RFDWP3H'; None if it can't be a code."""
    code = re.sub(r"[\s-]", "", raw or "").upper()
    return code if CODE_RE.match(code) else None


def pretty_code(code: str) -> str:
    return "-".join(code[i:i + 4] for i in range(0, len(code), 4))


def code_hint(code: str) -> str:
    return code[:4]


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


# ------------------------------------------------------------- input checks
def clean_hub_url(raw: str | None) -> str | None:
    """A hub address machines can use, without a trailing slash; None if it isn't one."""
    url = (raw or "").strip().rstrip("/")
    if not URL_RE.match(url):
        return None
    m = re.match(r"^(https?)://([^/:]+|\[[^\]]+\])(?::([0-9]+))?", url)
    if m and m.group(3) and not 0 < int(m.group(3)) < 65536:
        return None
    return url


def short_host(hostname: str | None) -> str:
    """Compare computers by their short host name, ignoring case: 'Build-Box.lan' == 'build-box'."""
    return (hostname or "").strip().lower().split(".")[0]


def name_from_hostname(hostname: str) -> str:
    """A dashboard name for a computer that didn't ask for one: its host name, made safe."""
    name = re.sub(r"[^a-z0-9._-]+", "-", short_host(hostname)).strip("-._")[:64]
    return name or "machine"


# --------------------------------------------------------------- rate limit
class FailureLimiter:
    """Counts failed code look-ups per client address. After `limit` failures within `window`
    seconds that address is refused until the oldest failure is `window` seconds old. A code
    has 80 random bits, so this only stops a script from hammering the hub."""

    def __init__(self, limit: int = 10, window: float = 600):
        self.limit, self.window = limit, window
        self.failures: dict[str, list[float]] = {}
        self.lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self.failures.get(key, []) if now - t < self.window]
        if recent:
            self.failures[key] = recent
        else:
            self.failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        with self.lock:
            return len(self._recent(key, time.time())) >= self.limit

    def failed(self, key: str) -> None:
        now = time.time()
        with self.lock:
            if len(self.failures) > 10_000:  # don't let a flood of addresses grow this forever
                for k in list(self.failures):
                    self._recent(k, now)
            self.failures.setdefault(key, []).append(now)


# ------------------------------------------------------------------ scripts
def setup_script(kind: str) -> Path | None:
    """The machine setup script for 'sh' (Linux, macOS) or 'ps1' (Windows), if this hub has it."""
    name = SETUP_SCRIPTS[kind]
    for folder in (SETUP_DIST, SETUP_DIST / "linux", SETUP_DIST / "windows"):
        if (folder / name).is_file():
            return folder / name
    return None


def bootstrap_files(runner_dist: Path, kind: str) -> dict[str, Path]:
    """What a bootstrap script downloads: the runner files and the setup script for its platform."""
    files = {}
    if runner_dist.is_dir():
        for path in sorted(runner_dist.glob("*.py")):
            if RUNNER_FILE_RE.match(path.name):
                files[path.name] = path
    script = setup_script(kind)
    if script is not None:
        files[script.name] = script
    return files


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render(kind: str, hub_url: str, code: str, version: str, files: dict[str, Path], minutes_left: int) -> str:
    """Fill in a bootstrap template. Every value is checked first, so none can break out of the
    quotes it is placed in."""
    if not URL_RE.match(hub_url) or not CODE_RE.match(code.replace("-", "")):
        raise ValueError("unsafe value for a bootstrap script")
    if not re.match(r"^[0-9A-Za-z.+-]*$", version):
        version = "?"
    for name in files:
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            raise ValueError(f"unsafe file name {name!r}")
    sums = {name: file_sha256(path) for name, path in files.items()}
    if kind == "sh":
        listing = "\n".join(f"{name} {digest}" for name, digest in sums.items())
    else:
        listing = "\n".join(f"        '{name}' = '{digest}'" for name, digest in sums.items())
    template = (HERE / f"install.{kind}").read_text(encoding="utf-8")
    text = (template.replace("__HUB_URL__", hub_url).replace("__CODE__", pretty_code(code))
            .replace("__VERSION__", version).replace("__MINUTES__", str(max(0, minutes_left)))
            .replace("__SETUP__", SETUP_SCRIPTS[kind]).replace("__FILES__", listing))
    if kind == "ps1":
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")  # what Windows PowerShell expects
    return text


def problem_script(kind: str, message: str) -> str:
    """A script that only explains why it can't add the machine (an unknown, used or expired code).
    Sent with status 200 so `curl -f ... | bash` and `irm ... | iex` show the message instead of a
    bare HTTP error. The message is fixed text from the hub, never from the request."""
    safe = re.sub(r"[^A-Za-z0-9 .,:;()/~'\"?!-]", "", message)
    if kind == "sh":
        quoted = safe.replace("'", "'\\''")
        return ("#!/usr/bin/env bash\n# Agent Bullpen: this code can't add a machine.\n"
                f"printf '\\n\\033[31mAgent Bullpen:\\033[0m %s\\n\\n' '{quoted}' >&2\nexit 1\n")
    quoted = safe.replace("'", "''")
    # No 'exit' here: under `irm | iex` it would close the PowerShell window.
    return ("# Agent Bullpen: this code can't add a machine.\r\n"
            f"Write-Host ''; Write-Host 'Agent Bullpen: {quoted}' -ForegroundColor Red; Write-Host ''\r\n")
