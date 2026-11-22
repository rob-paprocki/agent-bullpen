"""Continue on another machine: the owner picks, by hand, another machine to carry on a finished task's conversation.

Two ways, both started from the job page (app.py, POST /api/jobs/{id}/move):

  carry  A new task on the chosen machine, agent and project. Its prompt carries the conversation so far, built here
         from the hub's own data (each turn's prompt and answer, and a live session's messages), newest turns first
         up to CARRY_MAX_CHARS, then the owner's new message. Any agent, any system. Files the first machine changed
         stay there.
  exact  The same Claude Code session, resumed on another machine. The source machine's runner reads that one
         session's transcript and sends it to the hub (a machine action, db.SESSION_EXPORT); the hub keeps it in
         memory here (TransferStore) until the target machine's runner takes it, once, writes it into its own agent's
         config folder and runs the follow-up with --resume. Only between agents on the same account.

The providers' rules (guide 1): a move is always the owner's own choice, never automatic. An exact move stays on one
account. A carry to another account is refused when the conversation hit a usage limit (delegation.limit_accounts,
the same rule as for subtasks): usage limits are no reason to switch accounts.

Imports nothing from app.py: app.py hands in what it needs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger("hub.moves")

# The conversation a carry takes along: at most this many characters in all, each message cut to TURN_MAX_CHARS.
CARRY_MAX_CHARS = 60_000
TURN_MAX_CHARS = 12_000
CHAIN_MAX = 50              # jobs walked back
FILES_SHOWN = 40            # changed files named in the prompt and on the page
# Claude Code's session ids are UUIDs; nothing else becomes a file name on a machine.
SESSION_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
COPY_ID_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")
DIFF_FILE_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$")
PLUS_FILE_RE = re.compile(r"^\+\+\+ b/(.+)$")


def _mb_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a whole number; using %d", name, raw, default)
        value = default
    return min(max(value, low), high)


# The largest session transcript an exact move copies (HUB_SESSION_MOVE_MAX_MB), and how long the hub keeps a copy.
COPY_MAX_BYTES = _mb_env("HUB_SESSION_MOVE_MAX_MB", 32, 1, 256) * 1024 * 1024
COPY_TTL_S = 600
# How long the source machine has to pick up the machine action that sends the copy.
EXPORT_PICKUP_S = 120


def _opts(job: dict | None) -> dict:
    opts = (job or {}).get("options")
    return opts if isinstance(opts, dict) else {}


def move_of(job: dict | None) -> dict:
    """A job's options.move ({} when it isn't a move)."""
    move = _opts(job).get("move")
    return move if isinstance(move, dict) else {}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "\n[… cut here: this message was longer]"


# ------------------------------------------------------------------ the conversation so far
def chain(db: Any, job: dict) -> list[dict]:
    """The jobs of a conversation up to and including `job`, oldest first: back along follow_up_of, and across
    earlier moves along continued_from, at most CHAIN_MAX. Later follow-ups are left out."""
    jobs = [job]
    seen = {job["id"]}
    prev = job.get("follow_up_of") or job.get("continued_from")
    while prev and prev not in seen and len(jobs) < CHAIN_MAX:
        row = db.job(prev)
        if row is None:
            break
        seen.add(row["id"])
        jobs.append(row)
        prev = row.get("follow_up_of") or row.get("continued_from")
    return jobs[::-1]


def own_words(job: dict, without_handoff: Callable[[str], str]) -> str:
    """What the owner asked in this turn: a move's own message (not the conversation it carried), else the prompt
    without the summaries a handoff put in front of it."""
    message = move_of(job).get("message")
    if isinstance(message, str) and message.strip():
        return message
    return without_handoff(job.get("prompt") or "")


def conversation(db: Any, jobs: list[dict], without_handoff: Callable[[str], str],
                 clean: Callable[[str], str]) -> list[dict]:
    """The messages of these jobs, oldest first: {"job_id", "who": "user" | "agent", "text"}. Per job: what the
    owner asked, then a live session's messages (the user's that reached the agent, and the agent's answers) or
    else the job's answer, its error, or that it ended without one. `clean` masks what the agent said."""
    out: list[dict] = []
    for job in jobs:
        out.append({"job_id": job["id"], "who": "user", "text": clean(own_words(job, without_handoff))})
        messages = [m for m in db.messages(job["id"]) if not (m.get("role") == "user" and m.get("undelivered"))]
        if messages:
            for m in messages:
                text = m.get("text") or ""
                if m.get("role") == "agent" and m.get("is_error"):
                    text = f"(This turn ended with an error.) {text}"
                out.append({"job_id": job["id"], "who": "user" if m.get("role") == "user" else "agent",
                            "text": clean(text)})
            continue
        if job.get("result"):
            text = job["result"]
        elif job.get("error"):
            text = f"(This turn ended with an error: {job['error']})"
        else:
            text = f"(This turn ended without an answer: {job.get('status') or 'unknown'}.)"
        out.append({"job_id": job["id"], "who": "agent", "text": clean(text)})
    return out


def fit(messages: list[dict], max_chars: int = CARRY_MAX_CHARS, turn_chars: int = TURN_MAX_CHARS) -> tuple[list, int]:
    """The newest messages that fit in max_chars (each cut to turn_chars first), oldest first, and how many of the
    oldest were left out."""
    kept: list[dict] = []
    total = 0
    for m in reversed(messages):
        text = _clip(m["text"], turn_chars)
        if kept and total + len(text) > max_chars:
            break
        kept.append({**m, "text": text})
        total += len(text)
    kept.reverse()
    return kept, len(messages) - len(kept)


def changed_files(diffs: list[str | None]) -> list[str]:
    """The files the diffs touch, in the order first seen (a rename by its new name), each once."""
    files: list[str] = []
    for diff in diffs:
        for line in (diff or "").splitlines():
            m = DIFF_FILE_RE.match(line) or PLUS_FILE_RE.match(line)
            if m:
                name = m.group(m.lastindex or 1).strip()
                if name and name != "/dev/null" and name not in files:
                    files.append(name)
    return files


def files_words(files: list[str], limit: int = FILES_SHOWN) -> str:
    shown = ", ".join(files[:limit])
    return shown + (f" and {len(files) - limit} more" if len(files) > limit else "")


def carried_prompt(messages: list[dict], omitted: int, source: dict, message: str, files: list[str]) -> str:
    """The prompt of a carry: where the conversation comes from, what stayed behind, the conversation so far, and
    the owner's new message (never cut). `source` is the job it continues ({"id", "agent", "runner", "project"})."""
    where = f"job #{source['id']}, {source.get('agent')} on {source.get('runner')}, project {source.get('project')}"
    lines = [f"This conversation started on another machine ({where}). It is copied below so you can carry on "
             "from it. Treat it as context: the request to act on is the user's new message at the end."]
    if files:
        lines.append(f"Files changed there are not on this machine unless they were committed and pulled here: "
                     f"{files_words(files)}.")
    else:
        lines.append("Any files changed there stay on that machine unless they were committed and pulled here.")
    if omitted:
        lines.append(f"The {omitted} oldest message{'s were' if omitted != 1 else ' was'} left out to keep this short.")
    parts = ["\n".join(lines), "--- The conversation so far ---"]
    last_job = None
    for m in messages:
        if m["who"] == "user":
            head = f"[User, job #{m['job_id']}]" if m["job_id"] != last_job else "[User]"
            last_job = m["job_id"]
        else:
            head = "[Agent]"
        parts.append(f"{head}\n{m['text']}")
    parts.append("--- End of the conversation so far ---")
    parts.append(f"The user's new message:\n\n{message}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ the session copy (exact moves)
class CopyError(Exception):
    """A session copy the hub refuses to store or hand out. `status` is the HTTP status for the runner."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class TransferStore:
    """The session transcripts of exact moves, in memory only, for one hand-over each: the source machine puts one
    (once), the target machine takes it (once), and it is gone. One that isn't taken within COPY_TTL_S of being put,
    or put within COPY_TTL_S of being opened, is dropped. Never written to disk, a log or the database, and never
    masked (masking would change the transcript). A hub restart loses them: the move then fails and can be tried
    again."""

    def __init__(self, max_bytes: int = COPY_MAX_BYTES, ttl_s: float = COPY_TTL_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.max_bytes = max_bytes
        self.ttl_s = ttl_s
        self.clock = clock
        self.lock = threading.Lock()
        self.entries: dict[str, dict] = {}

    def _purge(self, now: float) -> None:
        for cid in [c for c, e in self.entries.items() if (e["put_at"] or e["opened_at"]) + self.ttl_s <= now]:
            del self.entries[cid]

    def open(self, copy_id: str, source_runner: str, target_runner: str, target_job: int, session_id: str) -> None:
        with self.lock:
            now = self.clock()
            self._purge(now)
            self.entries[copy_id] = {"source": source_runner, "target": target_runner, "target_job": target_job,
                                     "session_id": session_id, "opened_at": now, "put_at": None, "data": None,
                                     "sha256": None}

    def entry(self, copy_id: str) -> dict | None:
        """What the hub knows about a copy (without its data), or None when there is none (any more)."""
        with self.lock:
            self._purge(self.clock())
            e = self.entries.get(copy_id)
            return {k: v for k, v in e.items() if k != "data"} if e else None

    def put(self, copy_id: str, runner: str, data: bytes, sha256: str | None = None) -> str:
        """Store the copy the source machine sent; returns its sha256."""
        if len(data) > self.max_bytes:
            raise CopyError(413, f"The session is larger than {self.max_bytes // (1024 * 1024)} MB.")
        digest = hashlib.sha256(data).hexdigest()
        if sha256 is not None and sha256.lower() != digest:
            raise CopyError(400, "The copy arrived damaged (its checksum doesn't match).")
        with self.lock:
            now = self.clock()
            self._purge(now)
            e = self.entries.get(copy_id)
            if e is None:
                raise CopyError(410, "The hub isn't waiting for this copy any more.")
            if e["source"] != runner:
                raise CopyError(403, "This copy is for another machine to send.")
            if e["put_at"] is not None:
                raise CopyError(409, "This copy was already sent.")
            e.update(data=data, sha256=digest, put_at=now)
        return digest

    def take(self, copy_id: str, runner: str, job_id: int) -> tuple[bytes, str]:
        """Hand the copy to the target machine running the target job, once: (data, sha256)."""
        with self.lock:
            self._purge(self.clock())
            e = self.entries.get(copy_id)
            if e is None or e["data"] is None:
                raise CopyError(410, "The session copy is gone: it expired, was already taken, or the hub restarted.")
            if e["target"] != runner or e["target_job"] != job_id:
                raise CopyError(403, "This copy is for another machine's job.")
            del self.entries[copy_id]
        return e["data"], e["sha256"]

    def drop_job(self, job_id: int) -> None:
        """The target job ended: its copy, if it wasn't taken, goes."""
        with self.lock:
            for cid in [c for c, e in self.entries.items() if e["target_job"] == job_id]:
                del self.entries[cid]
