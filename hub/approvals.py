"""Approvals: an 'ask' job asks the owner on the dashboard before a command runs.

Claude Code runs such a job with `--permission-prompt-tool mcp__bullpenjob__approve`. When it
wants to run something that needs permission, it calls the `approve` tool at /mcp/job
(hub/job_mcp.py), which calls Approvals.request(): the question is stored, shown on the job
page (and the Overview), and the call waits for the owner's Allow or Deny there
(POST /api/jobs/{id}/approvals/{aid}, Approvals.decide()). No answer in time denies it.

Rules kept here:
  * The hub's own tools (mcp__bullpenjob__...) are never approved: the model calling them directly
    is denied without asking.
  * An input longer than MAX_INPUT_CHARS (as JSON), or a tool name longer than MAX_TOOL_NAME_CHARS,
    is denied unseen: the owner couldn't review it. That also bounds what is masked for the log.
  * Allow returns exactly the stored input the owner saw, never anything else.
  * The decision wakes the waiting call at once (an asyncio.Event per question); the database is
    also read every POLL_S as a backup, which notices a job that ended or was cancelled.
  * A question nobody waits for any more (the agent was stopped, timed out or lost its
    connection) is marked expired, so no card is left that nobody would act on.
  * The approval lines in the job's log are masked with hub/redact.py; the full input stays only in
    the approvals row, and is masked too once the job ends (Database.update_job).

Imports nothing from app.py and nothing from the `mcp` package.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from redact import redact

log = logging.getLogger("hub.approvals")


def _timeout_from_env() -> float:
    try:
        minutes = float(os.environ.get("HUB_APPROVAL_TIMEOUT_MIN", "10"))
    except ValueError:
        minutes = 10.0
    if minutes != minutes:  # NaN
        minutes = 10.0
    return min(max(minutes, 0.1), 120.0) * 60


TIMEOUT_S = _timeout_from_env()   # how long a question waits for the owner (HUB_APPROVAL_TIMEOUT_MIN)
MAX_PER_JOB = 200                 # questions one job may ask
MAX_INPUT_CHARS = 20_000          # longer inputs (as JSON) are denied unseen
MAX_TOOL_NAME_CHARS = 200         # longer tool names are denied unseen (no real tool has one)
MAX_REASON_CHARS = 2_000          # the reason typed with Deny
POLL_S = 5                        # backup database check; the decision itself wakes the waiter at once
PROGRESS_EVERY_S = 30             # "still waiting" progress notifications to the agent
SUMMARY_CHARS = 140               # the part of the input shown in the job log's approval line
OWN_TOOLS = "mcp__bullpenjob__"   # the hub's own job tools
# The input fields an approval line in the log shows, in this order (as the runner's summarize_tool).
SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt")
# Characters that could hide or disguise text in a log line: control characters, bidi controls,
# zero-width and other invisible characters (the dashboard's HIDDEN_RE in app.js, plus line breaks and
# tabs, since a log line is one line). Shown as visible escapes instead.
HIDDEN_RE = re.compile("[\x00-\x1f\x7f-\x9f\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                       "\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb"
                       "\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0000-\U000e0fff]")
# Characters that show as blank space: spaces of every width, no-break spaces and the braille blank (the
# dashboard's BLANK_LINE_RE), and line breaks.
BLANK = "\n \u00a0\u1680\u2000-\u200a\u202f\u205f\u2800\u3000"
BLANK_RE = re.compile(f"[{BLANK}]")
# A run of blank or hidden characters in the approval line (see gap()).
GAP_RE = re.compile(f"(?:[{BLANK}]|{HIDDEN_RE.pattern})+")
GAP_SHOWN = 3  # a run with a hidden character in it is written out up to this long, else counted

STARTUP_EXPIRED = "The hub restarted while this question waited."
STOPPED_WAITING = "The agent stopped waiting."

Progress = Callable[[int, str], Awaitable[None]]


class Refused(Exception):
    """A dashboard answer the hub can't take: `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def duration(seconds: float) -> str:
    """'15 seconds', '1 minute', '10 minutes', '2.5 minutes'."""
    if seconds < 60:
        n = max(1, round(seconds))
        return f"{n} second{'' if n == 1 else 's'}"
    minutes = seconds / 60
    text = f"{minutes:.1f}".rstrip("0").rstrip(".")
    return f"{text} minute{'' if text == '1' else 's'}"


def visible(text: str) -> str:
    """Text for one log line: hidden characters shown as escapes (\\n, \\x1b, \\u202e, \\u{e0041}),
    written as the dashboard's hiddenCode() in app.js writes them."""
    def esc(m: re.Match) -> str:
        ch = m.group(0)
        if ch in "\n\r\t":
            return {"\n": "\\n", "\r": "\\r", "\t": "\\t"}[ch]
        cp = ord(ch)
        if cp < 0x100:
            return f"\\x{cp:02x}"
        # Past U+FFFF the code goes in braces, so \u{e0041} can't be read as  followed by "1".
        return f"\\u{cp:04x}" if cp <= 0xFFFF else f"\\u{{{cp:x}}}"
    return HIDDEN_RE.sub(esc, text)


def gap(m: re.Match) -> str:
    """One run of blank or hidden characters in the approval line: one space when it is all blank, the
    hidden characters' escapes when it is short, else "[N hidden or blank characters]". So padding of
    any kind can't push the rest of a command past the cut, and hidden characters still show."""
    run = m.group(0)
    if not HIDDEN_RE.search(BLANK_RE.sub("", run)):  # a line break is blank here, not hidden
        return " "
    if len(run) <= GAP_SHOWN:
        return visible(BLANK_RE.sub(" ", run))
    return f"[{len(run):,} hidden or blank characters]"


def summary(tool_name: str, tool_input: dict) -> str:
    """'<tool_name>: <first 140 characters of the command, file, URL...>', secrets masked, for the
    job's log. Masked before it is cut, so a cut can't leave part of a key unmasked. Runs of blank
    and hidden characters are folded after masking (see gap()), so they can't push the rest of a
    command past the cut. A name or value longer than request() lets through isn't masked or shown
    at all."""
    if len(tool_name) > MAX_TOOL_NAME_CHARS:
        return "(a tool name too long to show)"
    name = visible(redact(tool_name))[:200]
    for key in SUMMARY_KEYS:
        value = tool_input.get(key)
        if value:
            text = str(value)
            if len(text) > MAX_INPUT_CHARS:
                return f"{name}: ({len(text):,} characters, too long to show)"
            text = GAP_RE.sub(gap, redact(text))
            return f"{name}: {text[:SUMMARY_CHARS]}{'…' if len(text) > SUMMARY_CHARS else ''}"
    return name


def deny(message: str) -> dict:
    return {"behavior": "deny", "message": message}


class Approvals:
    """Questions from 'ask' jobs. `db` is hub/db.py's Database; `notifier` has send(event, job)."""

    def __init__(self, db: Any, notifier: Any = None, timeout_s: float = TIMEOUT_S) -> None:
        self.db = db
        self.notifier = notifier
        self.timeout_s = timeout_s
        self._events: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
        self._events_lock = threading.Lock()

    def expire_leftovers(self) -> int:
        """At start-up: questions pending from before the hub restarted are expired (their calls
        died with the old process)."""
        n = self.db.expire_pending_approvals(STARTUP_EXPIRED)
        if n:
            log.info("%d question(s) from before the restart expired", n)
        return n

    # ----------------------------------------------------------------- asking
    async def request(self, job: dict, tool_name: Any, tool_input: Any, tool_use_id: Any,
                      progress: Progress | None = None) -> dict:
        """Ask the owner whether `tool_name` may run with `tool_input`, and wait for the answer.
        Returns Claude Code's permission answer: {"behavior": "allow", "updatedInput": {...}} or
        {"behavior": "deny", "message": "..."}."""
        job_id = job["id"]
        tool_name = tool_name if isinstance(tool_name, str) else ""
        if not tool_name.strip():
            return deny("The request didn't name a tool.")
        if len(tool_name) > MAX_TOOL_NAME_CHARS:
            await self._log(job_id, f"✖ Denied unseen: a tool name of {len(tool_name):,} characters "
                                    "(too long to show)")
            return deny("The tool's name is too long to show the owner on the dashboard, so it was denied.")
        if tool_name.startswith(OWN_TOOLS):
            await self._log(job_id, f"✖ Denied: {visible(tool_name)[:200]} (the hub's own tools can't be approved)")
            return deny("The hub's own tools can't be approved.")
        if not isinstance(tool_input, dict):
            return deny("The request had no input to show the owner.")
        if await asyncio.to_thread(self.db.approval_count, job_id) >= MAX_PER_JOB:
            return deny("This job has asked too many times.")
        raw = json.dumps(tool_input, ensure_ascii=False, sort_keys=True)
        if len(raw) > MAX_INPUT_CHARS:
            await self._log(job_id, f"✖ Denied unseen: {visible(redact(tool_name))[:200]} (too long to review)")
            return deny("This request is too long to show the owner on the dashboard, so it was denied. "
                        "Split it into smaller steps.")
        use_id = tool_use_id[:200] if isinstance(tool_use_id, str) else None
        # From the moment the question is stored, a call dropped before it waits (its connection broke)
        # expires the question, as _wait does once it waits: no card is left that nobody waits for.
        # The store is shielded, so a question stored after the call was dropped is expired then.
        store = asyncio.ensure_future(asyncio.to_thread(self.db.add_approval_if_running, job_id, tool_name, raw,
                                                        use_id, self.timeout_s))
        try:
            row = await asyncio.shield(store)
        except asyncio.CancelledError:
            store.add_done_callback(self._expire_stored)
            raise
        if row is None:
            return deny("The job has ended.")
        try:
            await self._log(job_id, f"⏸ Waiting for your approval: {summary(tool_name, tool_input)}")
            if self.notifier is not None:
                self.notifier.send("approval", job)
        except asyncio.CancelledError:
            self._expire(row["id"])
            raise
        return await self._wait(job_id, row, progress)

    def _expire_stored(self, store: asyncio.Future) -> None:
        """The question stored for a call that was dropped meanwhile, if one was stored."""
        if not store.cancelled() and store.exception() is None and store.result() is not None:
            self._expire(store.result()["id"])

    def _expire(self, aid: int) -> None:
        """A question nobody waits for any more, still pending: expired. A plain call, not a thread:
        a cancelled task can't await any more."""
        try:
            self.db.decide_approval(aid, "expired", STOPPED_WAITING)
        except Exception as err:  # noqa: BLE001
            log.warning("couldn't close question %s: %s", aid, err)

    async def _wait(self, job_id: int, row: dict, progress: Progress | None) -> dict:
        aid = row["id"]
        event = asyncio.Event()
        with self._events_lock:
            self._events[aid] = (asyncio.get_running_loop(), event)
        event.set()  # read the row once at once: an answer may have come before the event was registered
        try:
            n = 0
            next_progress = time.monotonic() + PROGRESS_EVERY_S
            while True:
                wait = min(POLL_S, next_progress - time.monotonic(), (row["expires_at"] or 0) - time.time())
                if wait > 0:
                    try:
                        await asyncio.wait_for(event.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
                event.clear()
                row = await asyncio.to_thread(self.db.approval, aid)
                job = await asyncio.to_thread(self.db.job, job_id)
                answer = self._answer(row, job)
                if answer is not None:
                    if row is not None and row["status"] == "pending":  # the job ended or is being cancelled
                        await asyncio.to_thread(self.db.decide_approval, aid, "cancelled", None)
                    return answer
                if time.time() >= (row["expires_at"] or 0):
                    if await asyncio.to_thread(self.db.decide_approval, aid, "expired",
                                               f"No answer within {duration(self.timeout_s)}."):
                        await self._log(job_id, f"✖ No answer within {duration(self.timeout_s)}, so this was denied.")
                        return deny(f"No answer from the owner within {duration(self.timeout_s)}, so this was "
                                    "denied. Find another way, or finish and say what you needed.")
                    continue  # an answer came in at the same moment: read it
                if time.monotonic() >= next_progress:
                    # Moved on even with no one to tell, or every later pass would skip the wait above.
                    next_progress = time.monotonic() + PROGRESS_EVERY_S
                    if progress is not None:
                        n += 1
                        await progress(n, "waiting for the owner")
        finally:
            with self._events_lock:
                self._events.pop(aid, None)
            # Still pending here means nobody will read the answer: the call was cancelled (the agent
            # stopped waiting, was stopped, or its connection broke).
            self._expire(aid)

    def _answer(self, row: dict | None, job: dict | None) -> dict | None:
        """The answer for the agent once there is one, else None (still waiting)."""
        if row is None:
            return deny("The job ended.")
        status = row["status"]
        if status == "allowed":
            # Exactly what the owner saw on the card, never anything else.
            try:
                stored = json.loads(row["tool_input"] or "")
            except ValueError:
                stored = None
            if not isinstance(stored, dict):
                return deny("The approved request couldn't be read back, so it was denied.")
            return {"behavior": "allow", "updatedInput": stored}
        if status == "denied":
            reason = (row.get("message") or "").strip()
            return deny("The owner denied this" + (": " + reason if reason else "."))
        if status == "expired":
            return deny(f"No answer from the owner within {duration(self.timeout_s)}, so this was denied. "
                        "Find another way, or finish and say what you needed.")
        if status != "pending" or job is None or job["status"] != "running" or job["cancel_requested"]:
            return deny("The job ended.")
        return None

    # --------------------------------------------------------------- answering
    def decide(self, job_id: int, approval_id: int, decision: str, message: str = "") -> dict:
        """The owner's answer from the dashboard (runs in a worker thread). Raises Refused."""
        row = self.db.approval(approval_id)
        if row is None or row["job_id"] != job_id:
            raise Refused(404, "No such question for this job.")
        if decision not in ("allow", "deny"):
            raise Refused(400, "Answer allow or deny.")
        if row["status"] != "pending" or (row["expires_at"] or 0) <= time.time():
            raise Refused(409, "This question was already answered or has expired.")
        status = "allowed" if decision == "allow" else "denied"
        reason = (message or "").strip()[:MAX_REASON_CHARS] if status == "denied" else ""
        if not self.db.decide_approval(approval_id, status, reason or None):
            raise Refused(409, "This question was already answered or has expired.")
        name = visible(row["tool_name"] or "")[:200]
        if status == "allowed":
            line = f"✔ You allowed {name}"
        else:
            line = f"✖ You denied {name}" + (f": {visible(reason)}" if reason else "")
        self.db.append_logs(job_id, [line])
        self.wake(approval_id)
        return {"ok": True, "status": status}

    def wake(self, approval_id: int) -> None:
        """Wake the call waiting for this question, if any (safe from any thread)."""
        with self._events_lock:
            waiter = self._events.get(approval_id)
        if waiter is None:
            return
        loop, event = waiter
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # its loop has closed: nobody is waiting
            pass

    def wake_job(self, job_id: int) -> None:
        """Wake every call waiting for one of this job's questions (after Cancel, End or a
        settled job), so the agent hears "The job ended." now rather than at the next check."""
        with self._events_lock:
            ids = list(self._events)
        if not ids:
            return
        for row in self.db.query(f"SELECT id FROM approvals WHERE job_id=? AND id IN ({','.join('?' * len(ids))})",
                                 (job_id, *ids)):
            self.wake(row["id"])

    async def _log(self, job_id: int, line: str) -> None:
        await asyncio.to_thread(self.db.append_logs, job_id, [line])
