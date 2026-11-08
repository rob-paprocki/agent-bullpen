"""Notifications: a short message to a URL you choose when a job finishes, fails, or waits for
your approval.

Settings (hub.env):

  HUB_NOTIFY_URL      where to send them: an ntfy topic (https://ntfy.sh/<topic> or your own
                      ntfy server) or any address that takes a JSON POST. Off when unset.
  HUB_NOTIFY_FORMAT   "ntfy" or "json". Default: ntfy when the host name contains "ntfy".
  HUB_NOTIFY_EVENTS   which events: done, error, approval (default: all three).
  HUB_NOTIFY_HEADERS  extra headers, "Name=value;Name2=value2" (for example a token for a
                      protected ntfy topic). Refused over plain http://.
  HUB_NOTIFY_TITLES   0: never send a job's title, not even one you typed.

What a notification says: the event, the job's number, a title only when you typed it
(otherwise "Job #12"), the status and a link to the job. Subtasks and council steps send only
their numbers, because an agent may have written their titles. Never a prompt, a result, a log
line, a tool name or a tool's input. At most one approval notification per job every 5 minutes.

Each one is sent from its own background thread with a 10-second timeout and no retries, so a
slow or broken receiver never holds up the hub. Failures are logged with the URL's host only:
an ntfy topic's name works like a password.

Standard library only; imports nothing from app.py and nothing from the `mcp` package.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

log = logging.getLogger("hub.notify")

APPROVAL_EVERY_S = 300   # at most one approval notification per job in this time
SEND_TIMEOUT_S = 10      # one attempt, then give up
EVENTS = ("done", "error", "approval")
FORMATS = ("ntfy", "json")
Event = Literal["done", "error", "approval"]

# ntfy: the message's ending, its tag (an emoji name ntfy shows) and priority, per event.
NTFY = {
    "done": (": finished", "white_check_mark", None),
    "error": (": failed", "x", None),
    "approval": (": needs your approval", "raising_hand", "high"),
}
HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}")


def label(job: Mapping[str, Any], titles: bool = True) -> str:
    """How a notification names a job. A subtask or a council step: only its number and its
    parent's (an agent may have written its title). A job whose title the owner typed: the
    number and that title. Anything else: just the number (its title is the prompt's first line)."""
    job_id = job.get("id")
    if job.get("delegated_by"):
        return f"Job #{job_id} (subtask of #{job['delegated_by']})"
    if job.get("parent_id"):
        return f"Job #{job_id} (step of council #{job['parent_id']})"
    opts = job.get("options") if isinstance(job.get("options"), Mapping) else {}
    title = job.get("title")
    if titles and opts.get("title_typed") and isinstance(title, str) and title.strip():
        return f"#{job_id} {' '.join(title.split())}"
    return f"Job #{job_id}"


def _parse_headers(raw: str) -> tuple[dict[str, str], list[str]]:
    """HUB_NOTIFY_HEADERS: ({name: value}, [problems]). A problem names the header, never its value."""
    headers, problems = {}, []
    for part in raw.split(";"):
        if not part.strip():
            continue
        name, sep, value = part.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not HEADER_NAME_RE.fullmatch(name):
            problems.append(f"'{name[:40]}' isn't written as Name=value")
        elif not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            problems.append(f"the value of {name} is empty or holds control characters")
        else:
            try:
                value.encode("latin-1")
            except UnicodeEncodeError:
                problems.append(f"the value of {name} holds characters a header can't carry")
                continue
            headers[name] = value
    return headers, problems


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would send the headers (perhaps a token) on to another address: don't follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class Notifier:
    """Sends notifications for the hub. `enabled` is False when HUB_NOTIFY_URL is unset or wrong."""

    def __init__(self, url: str = "", fmt: str = "json", events: tuple[str, ...] | list[str] = EVENTS,
                 headers: Mapping[str, str] | None = None, titles: bool = True, public_url: str = "") -> None:
        self.url = url
        self.fmt = fmt if fmt in FORMATS else "json"
        self.events = frozenset(e for e in events if e in EVENTS)
        self.headers = dict(headers or {})
        self.titles = titles
        self.public_url = public_url.rstrip("/")
        self.enabled = bool(url)
        self._lock = threading.Lock()
        self._approval_sent: dict[int, float] = {}   # job id -> when its last approval notification went
        self._opener = urllib.request.build_opener(_NoRedirects)
        # How a send runs: its own daemon thread. Tests swap this for a direct call.
        self.start: Callable[[Callable[[], None]], None] = \
            lambda fn: threading.Thread(target=fn, name="hub-notify", daemon=True).start()

    @classmethod
    def from_env(cls, env: Mapping[str, str], public_url: str) -> Notifier:
        """Read the HUB_NOTIFY_* settings. A setting that can't be used is logged and turns
        notifications off (or, for a format or event name, is left out), never sent half-right."""
        url = (env.get("HUB_NOTIFY_URL") or "").strip()
        if not url:
            return cls()
        parts = urlsplit(url)
        try:
            parts.port  # noqa: B018 - raises for a port that isn't a number
            port_ok = True
        except ValueError:
            port_ok = False
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname or not port_ok \
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in url):
            log.error("HUB_NOTIFY_URL is not an http:// or https:// address; notifications are off")
            return cls()
        host = parts.hostname.lower()
        headers, problems = _parse_headers(env.get("HUB_NOTIFY_HEADERS") or "")
        if problems:
            log.error("HUB_NOTIFY_HEADERS can't be used (%s); notifications are off", "; ".join(problems))
            return cls()
        if headers and parts.scheme.lower() == "http":
            log.error("HUB_NOTIFY_HEADERS is set but HUB_NOTIFY_URL uses plain http://, which would send the "
                      "headers unencrypted; notifications are off. Use an https:// address.")
            return cls()
        fmt = (env.get("HUB_NOTIFY_FORMAT") or "").strip().lower()
        if fmt and fmt not in FORMATS:
            log.error("HUB_NOTIFY_FORMAT=%r is not ntfy or json; using the default", fmt[:20])
            fmt = ""
        fmt = fmt or ("ntfy" if "ntfy" in host else "json")
        raw_events = env.get("HUB_NOTIFY_EVENTS")
        words = [w.strip().lower() for w in re.split(r"[\s,]+", raw_events or "") if w.strip()]
        unknown = [w for w in words if w not in EVENTS]
        if unknown:
            log.warning("HUB_NOTIFY_EVENTS: unknown event(s) %s left out (use done, error, approval)",
                        ", ".join(w[:20] for w in unknown))
        events = tuple(w for w in words if w in EVENTS) if raw_events is not None and raw_events.strip() else EVENTS
        titles = (env.get("HUB_NOTIFY_TITLES") or "1").strip() != "0"
        log.info("notifications go to %s (%s; %s)", host, fmt, ", ".join(events) or "no events")
        return cls(url=url, fmt=fmt, events=events, headers=headers, titles=titles, public_url=public_url)

    # ------------------------------------------------------------------ sending
    def job_url(self, job_id: Any) -> str | None:
        return f"{self.public_url}/#/job/{job_id}" if self.public_url else None

    def message(self, event: Event, job: Mapping[str, Any]) -> tuple[bytes, dict[str, str]]:
        """The request body and headers for one notification."""
        name = label(job, self.titles)
        url = self.job_url(job.get("id"))
        status = event if event in ("done", "error") else str(job.get("status") or "running")
        if self.fmt == "ntfy":
            ending, tag, priority = NTFY[event]
            headers = {"Title": "Agent Bullpen", "Tags": tag, "Content-Type": "text/plain; charset=utf-8"}
            if priority:
                headers["Priority"] = priority
            if url:
                headers["Click"] = url
            body = (name + ending).encode("utf-8")
        else:
            headers = {"Content-Type": "application/json"}
            body = json.dumps({"event": event, "job_id": job.get("id"), "title": name, "status": status,
                               "url": url}).encode("utf-8")
        return body, {**headers, **self.headers}

    def send(self, event: Event, job: Mapping[str, Any] | None) -> None:
        """Send one notification in the background. Never blocks and never raises."""
        try:
            if not self.enabled or job is None or event not in self.events:
                return
            if event == "approval" and not self._approval_due(job.get("id")):
                return
            body, headers = self.message(event, job)
            self.start(lambda: self._post(body, headers))
        except Exception as err:  # noqa: BLE001 - a notification must never break what triggered it
            log.warning("notification not sent: %s", type(err).__name__)

    def _approval_due(self, job_id: Any) -> bool:
        """At most one approval notification per job every APPROVAL_EVERY_S (remembered in memory)."""
        now = time.monotonic()
        with self._lock:
            last = self._approval_sent.get(job_id)
            if last is not None and now - last < APPROVAL_EVERY_S:
                return False
            self._approval_sent[job_id] = now
            if len(self._approval_sent) > 1000:  # forget jobs whose time is long past
                self._approval_sent = {k: v for k, v in self._approval_sent.items() if now - v < APPROVAL_EVERY_S}
            return True

    def _post(self, body: bytes, headers: dict[str, str]) -> None:
        host = urlsplit(self.url).hostname or "?"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(request, timeout=SEND_TIMEOUT_S) as response:
                response.read(1024)
        except urllib.error.HTTPError as err:
            log.warning("notification to %s refused: HTTP %s", host, err.code)
        except Exception as err:  # noqa: BLE001
            log.warning("notification to %s failed: %s", host, type(err).__name__)
