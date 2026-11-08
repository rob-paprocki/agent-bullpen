"""Shared memory: notes every agent on every machine can read and add to.

Two kinds of note:
  * pinned  - always in context. The hub compiles them into small markdown files
              that each runner copies to ~/.bullpen/context/ and that every agent's
              own memory file (CLAUDE.md, GEMINI.md, ...) imports.
  * regular - found on demand with the 'recall' tool or the Memory page.

Scopes say who a note is for:
  global              everyone (every agent and account: Claude, Gemini, Antigravity, ...)
  agent:<name>        one agent, e.g. agent:claude-work
  project:<name>      one project, e.g. project:myapp (found by recall)

Search uses SQLite's FTS5 full-text index when available, else a plain LIKE.
"""

from __future__ import annotations

import re
import sqlite3
import time

from db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    scope      TEXT NOT NULL DEFAULT 'global',
    text       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '',
    pinned     INTEGER NOT NULL DEFAULT 0,
    source     TEXT NOT NULL DEFAULT '',
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS notes_scope ON notes(scope, pinned);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(text, tags, scope, content='notes', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, text, tags, scope) VALUES (new.id, new.text, new.tags, new.scope);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, text, tags, scope) VALUES ('delete', old.id, old.text, old.tags, old.scope);
END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, text, tags, scope) VALUES ('delete', old.id, old.text, old.tags, old.scope);
  INSERT INTO notes_fts(rowid, text, tags, scope) VALUES (new.id, new.text, new.tags, new.scope);
END;
"""

SCOPE_RE = re.compile(r"^(global|agent:[A-Za-z0-9._-]{1,64}|project:[A-Za-z0-9._-]{1,64})$")
PIN_REQUEST_TAG = "pin-requested"


def inert(text: str) -> str:
    """Make note text safe to place in files that Claude Code and Gemini CLI read as memory.
    Both treat '@path' as 'import that file', so a note saying '@../runner.json' would pull
    a local secret into every session. A backslash stops that; Markdown still shows '@'.
    Line breaks are folded so a note stays one bullet and can't start its own headings."""
    return re.sub(r"\s*\n\s*", " ", text.replace("\\", "\\\\").replace("@", "\\@")).strip()
MAX_NOTE_CHARS = 8000
PINNED_WARN_CHARS = 12000  # pinned notes are loaded into every session, so keep them short

HEADER = """# Shared memory (managed by Agent Bullpen)

These notes are shared by every AI agent and account the user runs, on every machine.
Treat them as standing context about the user and their setup. They are edited on
the hub's Memory page; to add one, use the `remember` tool or the /remember command.
Don't edit this file by hand: it is rewritten on every sync.
"""


class Memory:
    def __init__(self, db: Database):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            try:
                db.conn.executescript(FTS_SCHEMA)
                self.fts = True
                # Index notes written before the index existed (no-op when in sync).
                db.conn.execute("INSERT INTO notes_fts(notes_fts) VALUES ('rebuild')")
            except sqlite3.OperationalError:
                self.fts = False

    # ----------------------------------------------------------- settings / versions
    def get_setting(self, key: str, default: str = "") -> str:
        return self._get(key, default)

    def set_setting(self, key: str, value: str) -> None:
        self._set(key, value)

    def _get(self, key: str, default: str = "") -> str:
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def _set(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def context_version(self) -> int:
        return int(self._get("context_version", "0") or 0)

    def bump(self) -> None:
        self._set("context_version", str(self.context_version() + 1))

    # ----------------------------------------------------------- notes
    @staticmethod
    def clean(scope: str | None, tags: str | list | None) -> tuple[str, str]:
        scope = (scope or "global").strip()
        if not SCOPE_RE.match(scope):
            raise ValueError("scope must be 'global', 'agent:<name>' or 'project:<name>'")
        if isinstance(tags, list):
            tags = ",".join(tags)
        tags = ",".join(t.strip().lower() for t in (tags or "").split(",") if t.strip())[:300]
        return scope, tags

    def add(self, text: str, scope: str | None = "global", tags: str | list | None = "",
            pinned: bool = False, source: str = "", request_pin: bool = False) -> dict:
        """pinned=True only for notes the user adds or pins on the dashboard. Agents can
        only *ask* for a pin (request_pin), which tags the note for the user to review."""
        text = (text or "").strip()
        if not text:
            raise ValueError("the note is empty")
        if len(text) > MAX_NOTE_CHARS:
            raise ValueError(f"notes are limited to {MAX_NOTE_CHARS} characters")
        scope, tags = self.clean(scope, tags)
        if request_pin and PIN_REQUEST_TAG not in tags.split(","):
            tags = ",".join(t for t in (tags, PIN_REQUEST_TAG) if t)
        self.check_pin(scope, pinned)
        now = time.time()
        note_id = self.db.execute(
            "INSERT INTO notes (scope, text, tags, pinned, source, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (scope, text, tags, int(bool(pinned)), source[:200], now, now))
        if pinned:
            self.bump()
        return self.get(note_id)

    @staticmethod
    def check_pin(scope: str, pinned) -> None:
        if pinned and scope.startswith("project:"):
            raise ValueError("Project notes can't be pinned (no agent loads them automatically). "
                             "Pin a global or agent note instead, or leave it for recall.")

    def get(self, note_id: int) -> dict | None:
        return self.db.query_one("SELECT * FROM notes WHERE id=?", (note_id,))

    def update(self, note_id: int, **fields) -> dict:
        before = self.get(note_id)
        if before is None:
            raise KeyError(note_id)
        text = fields.get("text", before["text"])
        scope, tags = self.clean(fields.get("scope", before["scope"]), fields.get("tags", before["tags"]))
        pinned = int(bool(fields.get("pinned", before["pinned"])))
        self.check_pin(scope, pinned)
        if pinned:  # the user has decided, so the request tag has done its job
            tags = ",".join(t for t in tags.split(",") if t and t != PIN_REQUEST_TAG)
        if not (text or "").strip() or len(text) > MAX_NOTE_CHARS:
            raise ValueError("a note must have 1 to %d characters" % MAX_NOTE_CHARS)
        self.db.execute("UPDATE notes SET text=?, scope=?, tags=?, pinned=?, updated_at=? WHERE id=?",
                        (text.strip(), scope, tags, pinned, time.time(), note_id))
        if before["pinned"] or pinned:
            self.bump()
        return self.get(note_id)

    def delete(self, note_id: int) -> bool:
        before = self.get(note_id)
        if before is None:
            return False
        self.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
        if before["pinned"]:
            self.bump()
        return True

    def search(self, query: str = "", scope: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        where, args = [], []
        if scope:
            where.append("n.scope = ?")
            args.append(scope)
        query = (query or "").strip()
        if query and self.fts:
            # Quote each word so punctuation can't break the FTS syntax; match all words (prefixes).
            words = [w for w in re.findall(r"[\w.-]+", query) if w][:12]
            if words:
                fts_q = " ".join('"%s"*' % w.replace('"', "") for w in words)
                sql = ("SELECT n.* FROM notes_fts f JOIN notes n ON n.id = f.rowid WHERE notes_fts MATCH ? "
                       + ("AND " + " AND ".join(where) + " " if where else "")
                       + "ORDER BY n.pinned DESC, bm25(notes_fts), n.updated_at DESC LIMIT ?")
                return self.db.query(sql, (fts_q, *args, limit))
        if query:
            for w in query.split()[:12]:
                where.append("(n.text LIKE ? OR n.tags LIKE ?)")
                args += [f"%{w}%", f"%{w}%"]
        sql = ("SELECT n.* FROM notes n " + ("WHERE " + " AND ".join(where) + " " if where else "")
               + "ORDER BY n.pinned DESC, n.updated_at DESC LIMIT ?")
        return self.db.query(sql, (*args, limit))

    # ----------------------------------------------------------- pinned context files
    def context_files(self) -> dict[str, str]:
        """Markdown files every runner writes to ~/.bullpen/context/."""
        pinned = self.db.query("SELECT * FROM notes WHERE pinned=1 ORDER BY scope, id")
        files: dict[str, list[str]] = {"shared.md": []}
        for note in pinned:
            if note["scope"] == "global":
                files["shared.md"].append(note)
            elif note["scope"].startswith("agent:"):
                files.setdefault(f"agent-{note['scope'][6:]}.md", []).append(note)
        out = {}
        for name, notes in files.items():
            title = "everyone" if name == "shared.md" else f"the {name[6:-3]} agent only"
            body = [HEADER, f"## Pinned notes for {title}\n"]
            if not notes:
                body.append("_(none yet)_\n")
            for n in notes:
                tags = f"  _(tags: {inert(n['tags'])})_" if n["tags"] else ""
                body.append(f"- {inert(n['text'])}{tags}")
            out[name] = "\n".join(body).rstrip() + "\n"
        return out

    def pinned_size(self) -> int:
        row = self.db.query_one("SELECT COALESCE(SUM(LENGTH(text)), 0) AS n FROM notes "
                                "WHERE pinned=1 AND scope NOT LIKE 'project:%'")
        return int(row["n"]) if row else 0
