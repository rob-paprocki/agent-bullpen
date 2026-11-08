"""SQLite storage for the hub.

Everything the hub knows lives in one small SQLite file:
  - runners:        every machine that checks in
  - jobs:           every task and every multi-agent "council" run
  - job_logs:       the live log lines a runner streams back while a job runs
  - machine_tokens: each enrolled machine's own runner token (only its sha256)
  - enroll_codes:   the one-time codes from "Add a machine" (only their sha256)
  - approvals:      questions from 'ask' jobs, answered on the dashboard
  - job_messages:   live sessions: what the owner typed, and the agent's answer to each turn
  - settings:       the owner's hub settings made on the dashboard (platform credits), as JSON

Each machine makes a few small requests every few seconds, so a single
connection guarded by a lock is enough and keeps the code easy to follow.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

from common import LOST_JOB_GRACE, MAX_TAGS, ONLINE_WINDOW, TAG_RE, clean_tags
from credits import claim_route

SCHEMA = """
CREATE TABLE IF NOT EXISTS runners (
    name         TEXT PRIMARY KEY,
    hostname     TEXT,
    os           TEXT,
    version      TEXT,
    agents       TEXT,     -- JSON list of {name, label, ready, note}
    projects     TEXT,     -- JSON list of project names
    max_parallel INTEGER,
    running      TEXT,     -- JSON list of job ids the runner says it is running
    last_seen    REAL,
    info         TEXT,     -- JSON: projects_root, remote management switches, ...
    auth         TEXT,     -- token of its last check-in: 'machine' (its own) or 'shared'
    conflict_host TEXT,    -- another computer that recently checked in under this name
    conflict_at  REAL,
    machine_tags TEXT,     -- JSON list: the tags it reports (not 'tags': see JSON_FIELDS)
    accepted     TEXT,     -- JSON {"tags": [...], "accounts": {agent: label}}: what the owner accepted
    clock_skew   REAL      -- seconds the hub's clock is ahead of the machine's, at its last check-in
);

-- Each enrolled machine's own runner token. The token is shown once, to the
-- machine, and never stored: only its sha256 is.
CREATE TABLE IF NOT EXISTS machine_tokens (
    name        TEXT PRIMARY KEY,      -- the machine name the token may act for
    token_hash  TEXT NOT NULL UNIQUE,
    hostname    TEXT,                  -- the computer it was issued to
    os          TEXT,
    created_at  REAL
);

-- One-time enrollment codes ("Add a machine"): valid for an hour, used once.
CREATE TABLE IF NOT EXISTS enroll_codes (
    code_hash   TEXT PRIMARY KEY,
    code_hint   TEXT NOT NULL,         -- the code's first group, to tell codes apart
    name        TEXT,                  -- machine name chosen on the dashboard (optional)
    hub_url     TEXT,                  -- the address the machine uses to reach the hub
    created_at  REAL,
    expires_at  REAL,
    used_at     REAL,
    used_by     TEXT                   -- the machine name it was exchanged for
);

CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id        INTEGER,          -- set for the steps of a council
    kind             TEXT NOT NULL,    -- 'task' or 'council'
    stage            TEXT,             -- council step: answer/critique/revise/judge/build/review/fix
    label            TEXT,             -- e.g. 'Response A' inside a council
    title            TEXT,
    runner           TEXT,
    agent            TEXT,
    project          TEXT,
    autonomy         TEXT,             -- readonly / edits / ask / full
    prompt           TEXT,
    status           TEXT NOT NULL,    -- queued / running / done / error / cancelled
    created_at       REAL,
    started_at       REAL,
    finished_at      REAL,
    exit_code        INTEGER,
    result           TEXT,
    diff             TEXT,
    error            TEXT,
    session_id       TEXT,             -- agent session id reported back (for follow-ups)
    resume_session   TEXT,             -- session id to resume when this job starts
    follow_up_of     INTEGER,
    options          TEXT,             -- JSON
    meta             TEXT,             -- JSON (council configuration and progress)
    cancel_requested INTEGER DEFAULT 0,
    model            TEXT,             -- model chosen for this job (blank = agent default)
    token_hash       TEXT,             -- sha256 of the job token (jobs that use the job tools only)
    delegated_by     INTEGER,          -- a subtask: the job that handed it over
    target_tag       TEXT,             -- aimed at any machine with this tag (not one by name)
    end_requested    INTEGER DEFAULT 0, -- live session: End session was pressed, or it ended
    usage            TEXT,             -- JSON: tokens and cost this job added
    usage_reported   TEXT,             -- JSON: the figures as the agent reported them
    error_kind       TEXT,             -- e.g. 'quota', 'claude_limit' or 'credits', when the error is known
    named_agent      TEXT,             -- the agent the seat or subtask named, when it ran on credits instead
    credits          TEXT,             -- JSON: how platform credits were involved (hub/credits.py)
    retry_of         INTEGER           -- a retry on credits: the job that stopped at a usage limit
);

CREATE INDEX IF NOT EXISTS jobs_queue  ON jobs(status, runner, kind);
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id);

CREATE TABLE IF NOT EXISTS job_logs (
    job_id INTEGER NOT NULL,
    seq    INTEGER NOT NULL,
    ts     REAL,
    line   TEXT,
    PRIMARY KEY (job_id, seq)
);

-- Permission questions from 'ask' jobs, answered on the dashboard.
CREATE TABLE IF NOT EXISTS approvals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    tool_name   TEXT,
    tool_input  TEXT,               -- JSON text exactly as returned to the agent (at most 20,000 characters;
                                    -- longer requests are denied unseen); redacted once the job ends
    tool_use_id TEXT,
    status      TEXT NOT NULL,      -- pending / allowed / denied / expired / cancelled
    message     TEXT,               -- the reason typed with Deny, or why it expired
    created_at  REAL,
    expires_at  REAL,
    decided_at  REAL
);
CREATE INDEX IF NOT EXISTS approvals_job ON approvals(job_id, status);

-- Live sessions: messages typed on the dashboard, and the agent's answer at the end of each turn.
CREATE TABLE IF NOT EXISTS job_messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       INTEGER NOT NULL,
    role         TEXT NOT NULL,     -- 'user' or 'agent'
    turn_no      INTEGER,           -- agent turns: the runner's turn number (1, 2, ...); NULL for user messages
    text         TEXT NOT NULL,
    is_error     INTEGER DEFAULT 0,
    created_at   REAL,
    delivered_at REAL,              -- user messages: when the runner picked it up
    undelivered  INTEGER DEFAULT 0, -- user messages: the session ended before it reached the agent
    UNIQUE (job_id, role, turn_no)
);
CREATE INDEX IF NOT EXISTS job_messages_job ON job_messages(job_id, id);

-- Settings the owner makes on the dashboard, one JSON value per key: 'credits' (amounts, reset days,
-- "platform credits first") and 'credits_refused' (organizations the API refused for lack of credit).
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT,
    updated_at  REAL
);
"""

# Columns holding JSON, decoded on the way out. This applies to a column of that name in every
# table, which is why a machine's tags are called machine_tags: notes.tags is a plain string.
JSON_FIELDS = {"agents", "projects", "running", "options", "meta", "info", "usage", "usage_reported",
               "machine_tags", "accepted", "credits"}

# Columns added after the first release; created on start-up if missing.
MIGRATIONS = {
    "jobs": {"model": "TEXT", "token_hash": "TEXT", "delegated_by": "INTEGER", "target_tag": "TEXT",
             "end_requested": "INTEGER DEFAULT 0", "usage": "TEXT", "usage_reported": "TEXT",
             "error_kind": "TEXT", "named_agent": "TEXT", "credits": "TEXT", "retry_of": "INTEGER"},
    "runners": {"info": "TEXT", "auth": "TEXT", "conflict_host": "TEXT", "conflict_at": "REAL",
                "machine_tags": "TEXT", "accepted": "TEXT", "clock_skew": "REAL"},
}

# Indexes on columns that MIGRATIONS may have just added: run after the migrations, not in SCHEMA.
POST_MIGRATION_SQL = [
    "CREATE INDEX IF NOT EXISTS jobs_token ON jobs(token_hash)",
    "CREATE INDEX IF NOT EXISTS jobs_delegated ON jobs(delegated_by)",
    "CREATE INDEX IF NOT EXISTS jobs_tag_queue ON jobs(status, target_tag)",
    "CREATE INDEX IF NOT EXISTS jobs_follow_up ON jobs(follow_up_of)",
    "CREATE INDEX IF NOT EXISTS jobs_retry ON jobs(retry_of)",
]
MAX_LOG_LINES_PER_JOB = 5000
TERMINAL_STATUSES = ("done", "error", "cancelled")
# A job token stops working when its machine hasn't checked in for this long, so a copied token
# dies with the machine even when the job is never settled.
JOB_TOKEN_MACHINE_WINDOW = ONLINE_WINDOW + LOST_JOB_GRACE
# A job token also stops working this long after the job's time limit.
JOB_TOKEN_GRACE_MIN = 5
# An agent's entry in a heartbeat larger than this loses its usage limits before it is stored.
MAX_AGENT_JSON = 16 * 1024


def _redacted_input(text: str | None) -> str | None:
    """An approval's stored input once its job has ended: with secrets masked by hub/redact.py
    (string by string, so it stays JSON). If masking fails, nothing is kept (the input is
    dropped), never the unmasked text."""
    if not text:
        return text
    try:
        from redact import redact_json
    except ImportError:
        return None
    try:
        return redact_json(text)
    except Exception:  # noqa: BLE001 - a masking problem must never keep the secret
        return None


# Why a machine action that waited too long (Start Remote Control) was dropped.
ADMIN_EXPIRED = "Not picked up within 2 minutes; press Start again."


def _options(options_text: str | None) -> dict:
    """A job's options column, decoded ({} when it holds nothing usable)."""
    try:
        opts = json.loads(options_text or "{}")
    except ValueError:
        return {}
    return opts if isinstance(opts, dict) else {}


def _expired(options_text: str | None, now: float) -> bool:
    """Does a queued job's options carry an `expires_at` that has passed?"""
    expires = _options(options_text).get("expires_at")
    return isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires < now


# A claim gives up after settling this many jobs that may no longer start (subtasks whose parent's
# time ran out); the runner simply polls again.
CLAIM_TRIES = 5
# Queued tag jobs looked at per claim.
TAG_SCAN = 50


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        # Called with a job's id when it ends or is being cancelled (after the terminal rule ran), so
        # what waits on the job (an approval call) hears it at once. Must be quick and never block.
        self.on_job_ended: Callable[[int], None] | None = None
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.executescript(SCHEMA)
            for table, columns in MIGRATIONS.items():
                have = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
                for col, kind in columns.items():
                    if col not in have:
                        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {kind}")
            for sql in POST_MIGRATION_SQL:
                self.conn.execute(sql)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in JSON_FIELDS & out.keys():
            if out[key]:
                try:
                    out[key] = json.loads(out[key])
                except ValueError:
                    pass
        return out

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [self._row(r) for r in self.conn.execute(sql, args).fetchall()]

    def query_one(self, sql: str, args: tuple = ()) -> dict | None:
        with self.lock:
            return self._row(self.conn.execute(sql, args).fetchone())

    def execute(self, sql: str, args: tuple = ()) -> int:
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid

    # ------------------------------------------------------------------ runners
    def upsert_runner(self, info: dict[str, Any], auth: str = "shared") -> None:
        """Store a heartbeat. What the owner accepted for the machine (`accepted`) is never
        touched here: only the dashboard changes it."""
        now = time.time()
        agents = []
        for agent in info.get("agents") or []:
            if isinstance(agent, dict) and "limits" in agent and len(json.dumps(agent)) > MAX_AGENT_JSON:
                agent = {k: v for k, v in agent.items() if k != "limits"}
            agents.append(agent)
        try:
            sent_at = float(info.get("sent_at") or 0)
        except (TypeError, ValueError):
            sent_at = 0.0
        self.execute(
            """
            INSERT INTO runners (name, hostname, os, version, agents, projects, max_parallel, running, last_seen,
                                 info, auth, machine_tags, clock_skew)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                hostname=excluded.hostname, os=excluded.os, version=excluded.version,
                agents=excluded.agents, projects=excluded.projects,
                max_parallel=excluded.max_parallel, running=excluded.running,
                last_seen=excluded.last_seen, info=excluded.info, auth=excluded.auth,
                machine_tags=excluded.machine_tags, clock_skew=excluded.clock_skew
            """,
            (
                info["name"],
                info.get("hostname", ""),
                info.get("os", ""),
                info.get("version", ""),
                json.dumps(agents),
                json.dumps(info.get("projects", [])),
                int(info.get("max_parallel", 1)),
                json.dumps(info.get("running", [])),
                now,
                json.dumps(info.get("info", {})),
                auth,
                json.dumps(clean_tags(info.get("tags"))),
                now - sent_at if sent_at > 0 else None,
            ),
        )

    def set_accepted(self, name: str, accepted: dict) -> None:
        """Replace what the owner accepted for a machine: {"tags": [...], "accounts": {agent: label}}."""
        self.execute("UPDATE runners SET accepted=? WHERE name=?", (json.dumps(accepted), name))

    def note_name_conflict(self, name: str, other_hostname: str) -> None:
        self.execute("UPDATE runners SET conflict_host=?, conflict_at=? WHERE name=?",
                     (other_hostname, time.time(), name))

    def runners(self) -> list[dict]:
        return self.query("SELECT * FROM runners ORDER BY name")

    def runner(self, name: str) -> dict | None:
        return self.query_one("SELECT * FROM runners WHERE name=?", (name,))

    def delete_runner(self, name: str) -> None:
        self.execute("DELETE FROM runners WHERE name=?", (name,))

    # ------------------------------------------------------------ machine tokens
    def machine_for_token(self, token_hash: str) -> dict | None:
        return self.query_one("SELECT name, hostname, os, created_at FROM machine_tokens WHERE token_hash=?",
                              (token_hash,))

    def machine_token_hash(self, name: str) -> str | None:
        row = self.query_one("SELECT token_hash FROM machine_tokens WHERE name=?", (name,))
        return row["token_hash"] if row else None

    def machine_token_of(self, name: str) -> dict | None:
        return self.query_one("SELECT name, hostname, os, created_at FROM machine_tokens WHERE name=?", (name,))

    def set_machine_token(self, name: str, token_hash: str, hostname: str, os_name: str) -> None:
        """Give a machine name its own token. An earlier token for that name stops working."""
        self.execute(
            "INSERT INTO machine_tokens (name, token_hash, hostname, os, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET token_hash=excluded.token_hash, hostname=excluded.hostname, "
            "os=excluded.os, created_at=excluded.created_at",
            (name, token_hash, hostname, os_name, time.time()))

    def revoke_machine_token(self, name: str) -> bool:
        with self.lock:
            return self.conn.execute("DELETE FROM machine_tokens WHERE name=?", (name,)).rowcount > 0

    # ---------------------------------------------------------- enrollment codes
    def add_enroll_code(self, code_hash: str, hint: str, name: str | None, hub_url: str, ttl: float) -> dict:
        now = time.time()
        with self.lock:
            # Codes that ended more than a day ago are no use to anyone.
            self.conn.execute("DELETE FROM enroll_codes WHERE expires_at < ? OR used_at < ?",
                              (now - 86400, now - 86400))
            self.conn.execute(
                "INSERT INTO enroll_codes (code_hash, code_hint, name, hub_url, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (code_hash, hint, name, hub_url, now, now + ttl))
            return self.enroll_code(code_hash)

    def enroll_code(self, code_hash: str) -> dict | None:
        return self.query_one("SELECT * FROM enroll_codes WHERE code_hash=?", (code_hash,))

    def active_enroll_codes(self) -> list[dict]:
        return self.query("SELECT * FROM enroll_codes WHERE used_at IS NULL AND expires_at > ? "
                          "ORDER BY created_at", (time.time(),))

    def use_enroll_code(self, code_hash: str, used_by: str) -> bool:
        """Mark a code used, exactly once. False if it was used, withdrawn or ran out meanwhile."""
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "UPDATE enroll_codes SET used_at=?, used_by=? WHERE code_hash=? AND used_at IS NULL "
                "AND expires_at > ?", (now, used_by, code_hash, now))
            return cur.rowcount == 1

    def delete_enroll_code(self, hint: str) -> int:
        """Withdraw the unused code(s) with this hint. Returns how many were withdrawn."""
        with self.lock:
            return self.conn.execute("DELETE FROM enroll_codes WHERE code_hint=? AND used_at IS NULL",
                                     (hint,)).rowcount

    # --------------------------------------------------------------------- jobs
    def create_job(self, **fields: Any) -> int:
        fields.setdefault("status", "queued")
        fields.setdefault("created_at", time.time())
        for key in JSON_FIELDS & fields.keys():
            fields[key] = json.dumps(fields[key])
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", tuple(fields.values()))

    def update_job(self, job_id: int, expect: str | None = None, **fields: Any) -> bool:
        """Change a job. When the change ends it (status done, error or cancelled), the same call
        also revokes its job token, closes its open approvals (and masks their stored input),
        marks live messages that never reached the agent, and settles its subtasks (see
        _job_ended). Every way a job ends goes through here, so none of them can skip that.

        With `expect`, the change is made only if the job's status is still that, in the same step:
        for a decision taken on a job read a moment earlier (Cancel), which a machine may have
        claimed or finished since. Returns whether the job was changed."""
        if not fields:
            return False
        ending = fields.get("status") in TERMINAL_STATUSES
        if ending:
            fields["token_hash"] = None
        for key in JSON_FIELDS & fields.keys():
            fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k}=?" for k in fields)
        where, args = ("id=?", (job_id,)) if expect is None else ("id=? AND status=?", (job_id, expect))
        with self.lock:
            cur = self.conn.execute(f"UPDATE jobs SET {sets} WHERE {where}", (*fields.values(), *args))
            changed = cur.rowcount == 1
            if ending and changed:
                self._job_ended(job_id, fields["status"])
        if ending and changed:
            self._ended(job_id)
        return changed

    def _ended(self, job_id: int) -> None:
        if self.on_job_ended is not None:
            try:
                self.on_job_ended(job_id)
            except Exception:  # noqa: BLE001 - a waiter that can't be woken notices on its next check
                pass

    def _job_ended(self, job_id: int, status: str) -> None:
        """The terminal rule. Called with the lock held, right after the job's status changed."""
        now = time.time()
        conn = self.conn
        conn.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE job_id=? AND status='pending'",
                     (now, job_id))
        for row in conn.execute("SELECT id, tool_input FROM approvals WHERE job_id=? AND tool_input IS NOT NULL",
                                (job_id,)).fetchall():
            masked = _redacted_input(row["tool_input"])
            if masked != row["tool_input"]:
                conn.execute("UPDATE approvals SET tool_input=? WHERE id=?", (masked, row["id"]))
        conn.execute("UPDATE job_messages SET undelivered=1 WHERE job_id=? AND role='user' AND delivered_at IS NULL",
                     (job_id,))
        self._settle_subtasks(job_id, cancel=status == "cancelled")

    def _settle_subtasks(self, job_id: int, cancel: bool) -> None:
        """A parent job ended or is being cancelled. Its queued subtasks never start. When it was
        cancelled, its running subtasks are asked to stop and lose their job tools at once; when it
        ended otherwise, they carry on until their own time limit, which ends with the parent's.
        Called with the lock held."""
        now = time.time()
        for child in self.conn.execute("SELECT id, status FROM jobs WHERE delegated_by=? "
                                       "AND status IN ('queued', 'running')", (job_id,)).fetchall():
            if child["status"] == "queued":
                self.update_job(child["id"], status="cancelled", finished_at=now,
                                error=f"Its parent job #{job_id} ended before it started.")
            elif cancel:
                self._request_cancel(child["id"])

    def request_cancel(self, job_id: int) -> bool:
        """Ask the machine running this job to stop it. The job loses its job tools now, not when
        the machine reports back: its token is cleared, its open approvals are closed, and its
        subtasks are cancelled too. False if the job isn't running."""
        with self.lock:
            return self._request_cancel(job_id)

    def _request_cancel(self, job_id: int) -> bool:
        cur = self.conn.execute("UPDATE jobs SET cancel_requested=1, token_hash=NULL WHERE id=? AND status='running'",
                                (job_id,))
        if cur.rowcount != 1:
            return False
        self.conn.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE job_id=? AND status='pending'",
                          (time.time(), job_id))
        self._settle_subtasks(job_id, cancel=True)
        self._ended(job_id)
        return True

    def job_for_token(self, token_hash: str) -> dict | None:
        """The job a job token belongs to, while the token is good: the job is a running task, not
        being cancelled, within its time limit (plus JOB_TOKEN_GRACE_MIN), and its machine still
        checks in. Otherwise None."""
        if not token_hash:
            return None
        job = self.query_one("SELECT * FROM jobs WHERE token_hash=? AND status='running' AND kind='task' "
                             "AND cancel_requested=0", (token_hash,))
        if job is None:
            return None
        now = time.time()
        opts = job["options"] if isinstance(job.get("options"), dict) else {}
        try:
            limit_min = float(opts.get("timeout_min") or 60)
        except (TypeError, ValueError):
            limit_min = 60.0
        if (job["started_at"] or 0) + (limit_min + JOB_TOKEN_GRACE_MIN) * 60 <= now:
            return None
        runner = self.query_one("SELECT last_seen FROM runners WHERE name=?", (job["runner"],))
        if runner is None or now - (runner["last_seen"] or 0) > JOB_TOKEN_MACHINE_WINDOW:
            return None
        return job

    def subtasks(self, job_id: int) -> list[dict]:
        """The jobs this job handed over (delegated_by), oldest first."""
        return self.query("SELECT * FROM jobs WHERE delegated_by=? ORDER BY id", (job_id,))

    def job(self, job_id: int) -> dict | None:
        return self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def children(self, parent_id: int) -> list[dict]:
        return self.query("SELECT * FROM jobs WHERE parent_id=? ORDER BY id", (parent_id,))

    def claim_next_job(self, runner: str, admin_only: bool = False, offer: dict | None = None) -> dict | None:
        """Atomically hand this runner its next job, or None.

        `offer` says what the machine offers, built by poll() from its last check-in:
            {"auth": "machine" | "shared", "tags": [effective tags],
             "agents": {name: {"account": account key, "features": [...]}},
             "projects": [...], "accept_delegated": bool,
             "credits": None | {"agent", "org", "features", "subscription_agents"}}
        Two queries, so tag jobs can't starve jobs aimed at the machine by name:
          * named: the oldest queued job for this runner, machine actions first (a busy machine
            still answers them). A task that may hand out subtasks, or a subtask, is passed over on a
            machine that didn't check in with its own runner token.
          * tag (tasks only, own token only): the oldest queued job for one of the machine's
            accepted tags, for one of its agents with the job's account key, in one of its
            projects, needing only features the agent has; a subtask only where
            accept_delegated is true.
        A machine action is taken before anything, a named job before a newer tag job. A machine
        action past its `expires_at` (Start Remote Control) is settled as cancelled, and so is a
        subtask past its `not_after` (its parent's time limit ran out); neither is handed out. A
        subtask's time limit is cut to the minutes left until its `not_after` as it is handed out.
        With offer["credits"] (the owner turned on "platform credits first" and the machine's credits
        agent's organization is available), a council step or subtask named for one of the machine's
        subscription agents runs on its credits agent instead (credits.claim_route), and the job keeps
        the agent it named in named_agent."""
        offer = offer if isinstance(offer, dict) else {}
        own = offer.get("auth") == "machine"
        kinds = "('admin')" if admin_only else "('task', 'admin')"
        with self.lock:
            now = time.time()
            for row in self.conn.execute("SELECT id, options FROM jobs WHERE status='queued' AND kind='admin' "
                                         "AND runner=?", (runner,)).fetchall():
                if _expired(row["options"], now):
                    self.update_job(row["id"], status="cancelled", finished_at=now, error=ADMIN_EXPIRED)
            for _ in range(CLAIM_TRIES):
                named = self._named_job(runner, kinds, own)
                tagged = None if admin_only or not own else self._tag_job(offer)
                if named is not None and (named["kind"] == "admin" or tagged is None or named["id"] < tagged["id"]):
                    pick = named
                else:
                    pick = tagged
                if pick is None:
                    return None
                now = time.time()
                options_text = pick["options"]
                opts = _options(options_text)
                not_after = opts.get("not_after")
                if pick["kind"] == "task" and opts.get("delegated") and isinstance(not_after, (int, float)) \
                        and not isinstance(not_after, bool):
                    if not_after <= now:
                        self.update_job(pick["id"], status="cancelled", finished_at=now,
                                        error="Its parent's time limit ran out before it started.")
                        continue
                    # A subtask's time limit ends with its parent's: cut to the minutes left now.
                    try:
                        limit = int(opts.get("timeout_min") or 60)
                    except (TypeError, ValueError):
                        limit = 60
                    opts["timeout_min"] = max(1, min(limit, math.ceil((not_after - now) / 60)))
                    options_text = json.dumps(opts)
                moved = {}
                if pick["kind"] == "task" and offer.get("credits"):
                    row = self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (pick["id"],)).fetchone())
                    parent_options = None
                    if row and row.get("delegated_by"):
                        parent = self.conn.execute("SELECT options FROM jobs WHERE id=?",
                                                   (row["delegated_by"],)).fetchone()
                        parent_options = _options(parent["options"]) if parent else {"delegate_agents": []}
                    moved = claim_route(row or {}, offer["credits"], parent_options) or {}
                if moved:
                    cur = self.conn.execute(
                        "UPDATE jobs SET status='running', started_at=?, runner=?, options=?, agent=?, named_agent=?, "
                        "credits=? WHERE id=? AND status='queued'",
                        (now, runner, options_text, moved["agent"], moved["named_agent"], json.dumps(moved["credits"]),
                         pick["id"]))
                else:
                    cur = self.conn.execute("UPDATE jobs SET status='running', started_at=?, runner=?, options=? "
                                            "WHERE id=? AND status='queued'", (now, runner, options_text, pick["id"]))
                if cur.rowcount == 1:
                    return self.job(pick["id"])
            return None

    def _named_job(self, runner: str, kinds: str, own: bool) -> sqlite3.Row | None:
        """The oldest queued job aimed at this runner by name (machine actions first). Called with
        the lock held. Tasks that hand out subtasks, and subtasks, need the machine's own token."""
        for row in self.conn.execute(f"SELECT id, kind, options FROM jobs WHERE status='queued' AND kind IN {kinds} "
                                     "AND runner=? ORDER BY kind='admin' DESC, id", (runner,)):
            if row["kind"] == "task" and not own:
                opts = _options(row["options"])
                if opts.get("delegate") or opts.get("delegated"):
                    continue
            return row
        return None

    def _tag_job(self, offer: dict) -> sqlite3.Row | None:
        """The oldest queued task for one of the offer's accepted tags that this machine may take
        (see claim_next_job). Called with the lock held."""
        tags = [t for t in offer.get("tags") or [] if isinstance(t, str) and TAG_RE.fullmatch(t)][:MAX_TAGS]
        agents = offer.get("agents") if isinstance(offer.get("agents"), dict) else {}
        names = [n for n in agents if isinstance(n, str) and isinstance(agents[n], dict)][:100]
        if not tags or not names:
            return None
        projects = {p for p in offer.get("projects") or [] if isinstance(p, str)}
        rows = self.conn.execute(
            f"SELECT id, kind, agent, project, options FROM jobs WHERE status='queued' AND kind='task' "
            f"AND runner IS NULL AND target_tag IN ({','.join('?' * len(tags))}) "
            f"AND agent IN ({','.join('?' * len(names))}) ORDER BY id LIMIT {TAG_SCAN}", (*tags, *names)).fetchall()
        for row in rows:
            opts = _options(row["options"])
            agent = agents[row["agent"]]
            features = agent.get("features") if isinstance(agent.get("features"), list) else []
            needs = opts.get("needs") if isinstance(opts.get("needs"), list) else []
            account = opts.get("account")
            if not isinstance(account, str) or account != agent.get("account"):
                continue  # never under another account than the one the job was made for
            if row["project"] not in projects or not set(needs) <= set(features):
                continue
            if opts.get("delegated") and offer.get("accept_delegated") is not True:
                continue
            return row
        return None

    def requeue_on_named_agent(self, job_id: int, error: str) -> bool:
        """A background job that ran on a credits agent and found the credits used up runs again, once,
        on the agent it named: back in the queue for the same machine, as a new start. Only while the
        job is still running and not being cancelled. Its open approvals close, its job token goes and
        `credits` keeps what happened. Returns whether it was queued again."""
        with self.lock:
            row = self._row(self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if row is None or row["status"] != "running" or row["cancel_requested"] or not row.get("named_agent"):
                return False
            info = row["credits"] if isinstance(row.get("credits"), dict) else {}
            if info.get("requeued"):
                return False
            info = {**info, "requeued": True, "error": (error or "")[:500], "requeued_at": time.time()}
            opts = row["options"] if isinstance(row.get("options"), dict) else {}
            opts = {**opts, "no_credits": True}
            cur = self.conn.execute(
                "UPDATE jobs SET status='queued', started_at=NULL, agent=?, named_agent=NULL, credits=?, options=?, "
                "token_hash=NULL, error=NULL, error_kind=NULL, result=NULL, session_id=NULL, exit_code=NULL "
                "WHERE id=? AND status='running' AND cancel_requested=0",
                (row["named_agent"], json.dumps(info), json.dumps(opts), job_id))
            if cur.rowcount != 1:
                return False
            self.conn.execute("UPDATE approvals SET status='cancelled', decided_at=? WHERE job_id=? AND status='pending'",
                              (time.time(), job_id))
        self._ended(job_id)  # questions still waiting on the first run hear at once that it ended
        return True

    # ----------------------------------------------------------------- settings
    def setting(self, key: str) -> Any:
        """A dashboard setting's value (decoded JSON), or None."""
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None or row["value"] is None:
            return None
        try:
            return json.loads(row["value"])
        except ValueError:
            return None

    def set_setting(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                     "value=excluded.value, updated_at=excluded.updated_at", (key, json.dumps(value), time.time()))

    def update_in_progress(self, runner: str) -> bool:
        """Is this runner currently installing an update (so it shouldn't get new work)?"""
        # An update that has been "running" for over 35 minutes is stuck; don't let it block the machine.
        return self.query_one(
            "SELECT 1 AS x FROM jobs WHERE runner=? AND kind='admin' AND status='running' "
            "AND options LIKE '%\"runner.update\"%' AND started_at > ?", (runner, time.time() - 35 * 60)) is not None

    def recent_jobs(self, limit: int = 50, include_children: bool = False) -> list[dict]:
        where = "" if include_children else "WHERE parent_id IS NULL"
        return self.query(
            f"""SELECT id, parent_id, kind, stage, title, runner, agent, project, autonomy, model,
                       status, created_at, started_at, finished_at, error, meta
                FROM jobs {where} ORDER BY id DESC LIMIT ?""",
            (limit,),
        )

    # ---------------------------------------------------------------- approvals
    def add_approval_if_running(self, job_id: int, tool_name: str, tool_input: str, tool_use_id: str | None,
                                ttl: float) -> dict | None:
        """Record a pending question, but only while its job runs and isn't being cancelled (in one
        statement, so a question can't slip in after Cancel). None if the job has ended."""
        now = time.time()
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO approvals (job_id, tool_name, tool_input, tool_use_id, status, created_at, expires_at) "
                "SELECT ?, ?, ?, ?, 'pending', ?, ? WHERE EXISTS "
                "(SELECT 1 FROM jobs WHERE id=? AND status='running' AND cancel_requested=0)",
                (job_id, tool_name, tool_input, tool_use_id, now, now + ttl, job_id))
            if cur.rowcount != 1:
                return None
            return self.approval(cur.lastrowid)

    def approval(self, aid: int) -> dict | None:
        return self.query_one("SELECT * FROM approvals WHERE id=?", (aid,))

    def decide_approval(self, aid: int, status: str, message: str | None) -> bool:
        """Answer a question that is still pending. False if it was answered, expired or cancelled
        meanwhile (the first answer wins)."""
        with self.lock:
            cur = self.conn.execute("UPDATE approvals SET status=?, message=?, decided_at=? "
                                    "WHERE id=? AND status='pending'", (status, message, time.time(), aid))
            return cur.rowcount == 1

    def approval_count(self, job_id: int) -> int:
        """How many questions this job has asked (every status)."""
        return self.query_one("SELECT COUNT(*) AS n FROM approvals WHERE job_id=?", (job_id,))["n"]

    def approvals_for_job(self, job_id: int, decided: int = 20) -> list[dict]:
        """A job's questions for its page: every pending one (oldest first), then the `decided`
        newest answered, expired or cancelled ones (newest first)."""
        return (self.query("SELECT * FROM approvals WHERE job_id=? AND status='pending' ORDER BY id", (job_id,))
                + self.query("SELECT * FROM approvals WHERE job_id=? AND status!='pending' ORDER BY id DESC LIMIT ?",
                             (job_id, decided)))

    def expire_pending_approvals(self, message: str) -> int:
        """At start-up: questions still pending were asked of the hub process that stopped, and
        nobody waits for their answer any more. Returns how many there were."""
        with self.lock:
            return self.conn.execute("UPDATE approvals SET status='expired', message=?, decided_at=? "
                                     "WHERE status='pending'", (message, time.time())).rowcount

    def pending_approvals(self, job_id: int | None = None, limit: int = 20) -> list[dict]:
        """Questions still waiting for an answer, oldest first: one job's, or every job's."""
        if job_id is None:
            return self.query("SELECT * FROM approvals WHERE status='pending' ORDER BY id LIMIT ?", (limit,))
        return self.query("SELECT * FROM approvals WHERE status='pending' AND job_id=? ORDER BY id LIMIT ?",
                          (job_id, limit))

    # ------------------------------------------------------------- live messages
    def add_message(self, job_id: int, role: str, text: str, is_error: bool = False,
                    turn_no: int | None = None) -> int:
        """Store a live-session message and return its id. An agent turn already stored (the runner
        sends a batch again after a lost reply) is ignored, and 0 is returned for it."""
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO job_messages (job_id, role, turn_no, text, is_error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (job_id, role, turn_no, text, int(bool(is_error)), time.time()))
            return cur.lastrowid if cur.rowcount == 1 else 0

    def messages(self, job_id: int, after_id: int = 0) -> list[dict]:
        """A job's live-session messages with an id above after_id, oldest first."""
        rows = self.query("SELECT id, role, turn_no, text, is_error, created_at, delivered_at, undelivered "
                          "FROM job_messages WHERE job_id=? AND id>? ORDER BY id LIMIT 1000", (job_id, after_id))
        for row in rows:
            row["is_error"], row["undelivered"] = bool(row["is_error"]), bool(row["undelivered"])
        return rows

    def messages_after(self, job_id: int, after_id: int, role: str = "user") -> list[dict]:
        """Messages the runner hasn't received yet: id above after_id, oldest first."""
        return self.query("SELECT id, text FROM job_messages WHERE job_id=? AND role=? AND id>? "
                          "AND delivered_at IS NULL AND undelivered=0 ORDER BY id", (job_id, role, after_id))

    def mark_delivered(self, job_id: int, upto_id: int) -> None:
        """The runner has received every user message up to this id."""
        if upto_id > 0:
            self.execute("UPDATE job_messages SET delivered_at=? WHERE job_id=? AND role='user' AND id<=? "
                         "AND delivered_at IS NULL", (time.time(), job_id, upto_id))

    # --------------------------------------------------------------------- logs
    def append_logs(self, job_id: int, lines: list[str]) -> None:
        if not lines:
            return
        with self.lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS n FROM job_logs WHERE job_id=?", (job_id,)
            ).fetchone()
            seq = row["n"]
            if seq >= MAX_LOG_LINES_PER_JOB:
                return
            now = time.time()
            room = MAX_LOG_LINES_PER_JOB - seq
            batch = [str(line)[:4000] for line in lines[:room]]
            if len(lines) > room:
                batch[-1] = "… log limit reached; further lines dropped …"
            self.conn.executemany(
                "INSERT INTO job_logs (job_id, seq, ts, line) VALUES (?, ?, ?, ?)",
                [(job_id, seq + i + 1, now, line) for i, line in enumerate(batch)],
            )

    def logs(self, job_id: int, since: int = 0) -> list[dict]:
        return self.query(
            "SELECT seq, ts, line FROM job_logs WHERE job_id=? AND seq>? ORDER BY seq LIMIT 2000",
            (job_id, since),
        )
