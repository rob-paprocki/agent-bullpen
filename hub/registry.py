"""Central MCP list: the MCP servers every agent on every machine should have.

You add a server once on the dashboard's MCP page. Each runner then writes it
into every agent's own settings on its machine:

  Claude Code (each account)  `claude mcp add-json -s user`  (in that account's config dir)
  Gemini CLI                  ~/.gemini/settings.json -> mcpServers
  Antigravity                 ~/.gemini/config/mcp_config.json -> mcpServers

Each http server is reached one of two ways (`route`):

  hub     agents connect to the hub at /mcp/s/<name> with the hub's own token; the
          real URL, its secret headers or its OAuth sign-in (done once, on the
          dashboard) never leave the hub. See mcp_proxy.py and upstream_oauth.py.
  direct  the URL and headers are copied into every agent's settings.

Local programs (stdio) always run on each machine. See docs/10-shared-memory-and-mcp.md.

Platforms: each server lists the kinds of machine it goes to: "linux", "macos" and
"windows", and a machine gets exactly the servers that list its kind.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from db import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    transport  TEXT NOT NULL DEFAULT 'http',   -- http | stdio
    url        TEXT NOT NULL DEFAULT '',
    headers    TEXT NOT NULL DEFAULT '{}',     -- JSON object
    command    TEXT NOT NULL DEFAULT '',
    args       TEXT NOT NULL DEFAULT '[]',     -- JSON list
    env        TEXT NOT NULL DEFAULT '{}',     -- JSON object
    targets    TEXT NOT NULL DEFAULT '["claude","gemini","antigravity"]',
    platforms  TEXT NOT NULL DEFAULT '["linux","macos","windows"]',
    enabled    INTEGER NOT NULL DEFAULT 1,
    note       TEXT NOT NULL DEFAULT '',
    updated_at REAL,
    route      TEXT NOT NULL DEFAULT 'hub',      -- hub (proxied by the hub) | direct (copied to each machine)
    auth       TEXT NOT NULL DEFAULT 'headers',  -- headers | oauth (signed in once, on the hub)
    oauth_client_id     TEXT NOT NULL DEFAULT '',
    oauth_client_secret TEXT NOT NULL DEFAULT '',
    oauth_scopes        TEXT NOT NULL DEFAULT '',
    oauth_auth_method   TEXT NOT NULL DEFAULT 'basic'  -- your own OAuth app: secret via HTTP Basic | post (body)
);
"""
ADDED_COLUMNS = {  # for databases created by an earlier version
    "route": "TEXT NOT NULL DEFAULT 'direct'",  # older entries were copied to machines
    "auth": "TEXT NOT NULL DEFAULT 'headers'",
    "oauth_client_id": "TEXT NOT NULL DEFAULT ''",
    "oauth_client_secret": "TEXT NOT NULL DEFAULT ''",
    "oauth_scopes": "TEXT NOT NULL DEFAULT ''",
    # A table made without this column keeps sending the secret in the body ('post').
    "oauth_auth_method": "TEXT NOT NULL DEFAULT 'post'",
}

# No underscores: Gemini CLI names tools mcp_<server>_<tool> and splits on the first underscore.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e\t]*$")  # what HTTP allows: visible ASCII, space, tab
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
TARGETS = ("claude", "gemini", "antigravity")
PLATFORMS = ("linux", "macos", "windows")
RESERVED = {"bullpen", "bullpenjob"}  # the hub's own servers: shared memory, and the job tools


def _no_newlines(value: str, what: str) -> str:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{what} can't contain line breaks")
    return value


class Registry:
    def __init__(self, db: Database, on_change):
        self.db = db
        self.on_change = on_change
        with db.lock:
            db.conn.executescript(SCHEMA)
            have = {r["name"] for r in db.conn.execute("PRAGMA table_info(mcp_servers)")}
            for col, kind in ADDED_COLUMNS.items():
                if col not in have:
                    db.conn.execute(f"ALTER TABLE mcp_servers ADD COLUMN {col} {kind}")

    @staticmethod
    def _decode(row: dict | None) -> dict | None:
        if row is None:
            return None
        out = dict(row)
        for key in ("headers", "args", "env", "targets", "platforms"):
            try:
                out[key] = json.loads(out[key] or "null")
            except ValueError:
                out[key] = {} if key in ("headers", "env") else []
        out["enabled"] = bool(out["enabled"])
        return out

    def list(self) -> list[dict]:
        return [self._decode(r) for r in self.db.query("SELECT * FROM mcp_servers ORDER BY name")]

    def get(self, server_id: int) -> dict | None:
        return self._decode(self.db.query_one("SELECT * FROM mcp_servers WHERE id=?", (server_id,)))

    def validate(self, data: dict[str, Any]) -> dict[str, Any]:
        name = (data.get("name") or "").strip().lower()
        if not NAME_RE.match(name):
            raise ValueError("Name: lowercase letters, digits and '-' only (max 40), e.g. 'github' or 'gateway'.")
        if name in RESERVED:
            raise ValueError(f"'{name}' is reserved for the hub's own servers.")
        transport = data.get("transport") or "http"
        if transport not in ("http", "stdio"):
            raise ValueError("Transport must be 'http' or 'stdio'.")
        url = _no_newlines((data.get("url") or "").strip(), "URL")
        command = _no_newlines((data.get("command") or "").strip(), "Command")
        if transport == "http" and not re.match(r"^https?://\S+$", url):
            raise ValueError("An http server needs a URL starting with http:// or https://.")
        if transport == "stdio" and not command:
            raise ValueError("A stdio server needs a command, e.g. npx.")
        route = data.get("route") or ("hub" if transport == "http" else "direct")
        if route not in ("hub", "direct"):
            raise ValueError("Route must be 'hub' or 'direct'.")
        if transport == "stdio" and route == "hub":
            raise ValueError("Local programs (stdio) run on each machine: choose 'Directly on each machine'.")
        auth = data.get("auth") or "headers"
        if auth not in ("headers", "oauth"):
            raise ValueError("Sign-in must be 'headers' or 'oauth'.")
        if auth == "oauth" and route != "hub":
            raise ValueError("OAuth sign-in happens once, on the hub, so it needs 'Through the hub'.")
        client_id = _no_newlines((data.get("oauth_client_id") or "").strip(), "Client ID")
        client_secret = _no_newlines((data.get("oauth_client_secret") or "").strip(), "Client secret")
        scopes = _no_newlines((data.get("oauth_scopes") or "").strip(), "Scopes")
        auth_method = data.get("oauth_auth_method") or "basic"
        if auth_method not in ("basic", "post"):
            raise ValueError("How to send the client secret must be 'basic' or 'post'.")
        headers = {str(k).strip(): _no_newlines(str(v), "Header values")
                   for k, v in (data.get("headers") or {}).items() if str(k).strip()}
        for k, v in headers.items():
            if not HEADER_RE.match(k):
                raise ValueError(f"'{k}' isn't a valid header name.")
            if not HEADER_VALUE_RE.match(v):
                raise ValueError(f"The value of header '{k}' has a character HTTP headers can't carry "
                                 "(only plain ASCII letters, digits and punctuation).")
        if auth == "oauth":
            headers = {}  # the sign-in provides the credentials
        env = {str(k).strip(): _no_newlines(str(v), "Environment values")
               for k, v in (data.get("env") or {}).items() if str(k).strip()}
        for k in env:
            if not ENV_RE.match(k):
                raise ValueError(f"'{k}' isn't a valid environment variable name.")
        args = [_no_newlines(str(a), "Arguments") for a in (data.get("args") or []) if str(a) != ""]
        targets = [t for t in (TARGETS if data.get("targets") is None else data["targets"]) if t in TARGETS]
        platforms = [p for p in (PLATFORMS if data.get("platforms") is None else data["platforms"]) if p in PLATFORMS]
        if not targets:
            raise ValueError("Pick at least one kind of agent.")
        if not platforms:
            raise ValueError("Pick at least one platform.")
        return {"name": name, "transport": transport, "url": url if transport == "http" else "",
                "headers": headers if transport == "http" else {}, "command": command if transport == "stdio" else "",
                "args": args if transport == "stdio" else [], "env": env if transport == "stdio" else {},
                "targets": targets, "platforms": platforms, "enabled": bool(data.get("enabled", True)),
                "note": (data.get("note") or "").strip()[:500], "route": route, "auth": auth,
                "oauth_client_id": client_id if auth == "oauth" else "",
                "oauth_client_secret": client_secret if auth == "oauth" else "",
                "oauth_scopes": scopes if auth == "oauth" else "",
                "oauth_auth_method": auth_method if auth == "oauth" else "basic"}

    def save(self, data: dict[str, Any], server_id: int | None = None) -> dict:
        v = self.validate(data)
        cols = {**v, "headers": json.dumps(v["headers"]), "args": json.dumps(v["args"]),
                "env": json.dumps(v["env"]), "targets": json.dumps(v["targets"]),
                "platforms": json.dumps(v["platforms"]), "enabled": int(v["enabled"]), "updated_at": time.time()}
        clash = self.db.query_one("SELECT id FROM mcp_servers WHERE name=?", (v["name"],))
        if clash and clash["id"] != server_id:
            raise ValueError(f"There is already a server called '{v['name']}'.")
        if server_id is None:
            names = ", ".join(cols)
            marks = ", ".join("?" for _ in cols)
            server_id = self.db.execute(f"INSERT INTO mcp_servers ({names}) VALUES ({marks})", tuple(cols.values()))
        else:
            if self.get(server_id) is None:
                raise KeyError(server_id)
            sets = ", ".join(f"{k}=?" for k in cols)
            self.db.execute(f"UPDATE mcp_servers SET {sets} WHERE id=?", (*cols.values(), server_id))
        self.on_change()
        return self.get(server_id)

    def delete(self, server_id: int) -> bool:
        if self.get(server_id) is None:
            return False
        self.db.execute("DELETE FROM mcp_servers WHERE id=?", (server_id,))
        self.on_change()
        return True

    def for_runners(self) -> list[dict]:
        """What machines get. Hub-routed servers carry no URL or secrets: agents reach them
        through the hub at /mcp/s/<name> with the hub's own token."""
        out = []
        for s in self.list():
            if not s["enabled"]:
                continue
            if s["route"] == "hub":
                out.append({"name": s["name"], "transport": "http", "route": "hub", "url": "", "headers": {},
                            "command": "", "args": [], "env": {}, "targets": s["targets"], "platforms": s["platforms"]})
            else:
                out.append({**{k: s[k] for k in ("name", "transport", "url", "headers", "command", "args", "env",
                                                 "targets", "platforms")}, "route": "direct"})
        return out

    def by_name(self, name: str) -> dict | None:
        return self._decode(self.db.query_one("SELECT * FROM mcp_servers WHERE name=?", (name,)))

    @staticmethod
    def masked(server: dict) -> dict:
        """For the dashboard list: hide secret-looking values."""
        out = dict(server)
        out["headers"] = {k: (v[:6] + "…" if len(v) > 10 else "…") for k, v in server["headers"].items()}
        out["env"] = {k: (v[:4] + "…" if len(v) > 8 else "…") for k, v in server["env"].items()}
        out["oauth_client_secret"] = "…" if server.get("oauth_client_secret") else ""
        return out
