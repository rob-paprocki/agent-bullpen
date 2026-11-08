"""OAuth for MCP servers routed through the hub: sign in ONCE, on the dashboard, for every agent.

Built on the MCP Python SDK's own client OAuth support (mcp 2.3):
  mcp.client.auth.OAuthClientProvider   RFC 9728 + RFC 8414 discovery, RFC 7591 dynamic client
                                        registration, PKCE S256, RFC 8707 resource, token refresh
  mcp.client.auth.TokenStorage          implemented here on the hub's SQLite database

Sign-in (a person, in a browser):
  dashboard  POST /api/mcp/{id}/signin
     hub     opens an MCP session with an *interactive* provider in a background task. The server
             answers 401, the SDK discovers its authorization server, registers the hub as a
             client if it has none yet, builds the authorization URL and calls on_redirect(url),
             which files the flow under its `state` and hands the URL back to the dashboard.
  browser    goes to the provider, signs in, and is sent to GET /oauth/callback?code&state
     hub     finish_sign_in() finds the flow by `state`, checks the browser is the one that
             started it (a cookie), and gives the code to the SDK, which exchanges it (the PKCE
             verifier never leaves the hub).
A sign-in keeps its new client registration and tokens in memory and stores them only when it
succeeds, and only if the server's address and client are still the ones it started with; an
abandoned or outdated sign-in changes nothing. Changing the address or client on the MCP page
cancels a pending sign-in.

Agents' tool calls never involve a person. token() hands the proxy (mcp_proxy.py) the stored
access token, tied to the address it was issued for, and renews it when it has expired or the
server refused it:
  * one renewal per stored sign-in at a time, in a task the hub owns: callers only wait for it,
    so a cancelled agent call can't lose a refresh token the provider has just rotated;
  * a sign-in is dropped only when the provider says the grant is dead (invalid_grant,
    invalid_client, unauthorized_client); any other trouble (outage, odd answer) keeps it, and
    callers within the next 15 s are told so straight away instead of retrying;
  * the request is built exactly as the SDK builds it (token endpoint, RFC 8707 resource,
    client authentication), from the metadata stored at sign-in.

Not implemented: revoking tokens at the provider on sign-out (most MCP providers have no
revocation endpoint). Sign-out forgets them here; revoke the app at the provider if needed.
"""

from __future__ import annotations

import asyncio
import contextvars
import hmac
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

import httpx2
from mcp import Client
from mcp.client.auth import OAuthClientProvider, OAuthFlowError, OAuthRegistrationError, OAuthTokenError
from mcp.client.auth.oauth2 import OAuthContext
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from mcp_types import LATEST_PROTOCOL_VERSION
from pydantic import ValidationError

from db import Database
from mcp_proxy import TokenUnavailable, capped_transport

RENEW_TIMEOUT = 60.0              # one token renewal request (generous: giving up early can lose a
                                  # refresh token the provider has already rotated)
DOWN_FOR = 15.0                   # after a failed renewal, callers are told so without retrying
REFUSED_FOR = 60.0                # a just-renewed token the server refused: don't renew again sooner
GIVE_UP_AFTER = 1800.0            # renewals failing this long (3+ tries) with a 4xx: the sign-in is dead
TEMPORARY_ERRORS = ("temporarily_unavailable", "server_error", "slow_down")
MAX_TOKEN_BYTES = 256 * 1024      # largest token endpoint answer read
MAX_SIGNIN_BYTES = 4 * 1024 * 1024
DEAD_GRANT = ("invalid_grant", "invalid_client", "unauthorized_client")

log = logging.getLogger("hub.oauth")

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_oauth (
    server_id         INTEGER PRIMARY KEY,  -- mcp_servers.id
    url               TEXT NOT NULL DEFAULT '',  -- the server URL these tokens are for
    client_source     TEXT,                 -- 'dcr' (registered automatically) or 'manual'
    client_info       TEXT,                 -- JSON OAuthClientInformationFull (client_id, secret, ...)
    tokens            TEXT,                 -- JSON OAuthToken (access + refresh)
    token_expires_at  REAL,                 -- unix time the access token expires
    resource_metadata TEXT,                 -- JSON RFC 9728 metadata found at sign-in
    as_metadata       TEXT,                 -- JSON RFC 8414 metadata found at sign-in (token endpoint)
    status            TEXT NOT NULL DEFAULT 'needs_sign_in',  -- needs_sign_in|signing_in|connected
    detail            TEXT,
    updated_at        REAL,
    public            INTEGER NOT NULL DEFAULT 0  -- 1: the last sign-in found the server needs no OAuth
);
"""


class NeedsSignIn(TokenUnavailable):
    """Only the user can fix this: sign in to the server on the hub's MCP page."""


class AuthServerDown(TokenUnavailable):
    """The provider's authorization server failed a refresh; the sign-in is kept."""


class SignInError(Exception):
    """A sign-in attempt failed (the provider said no, timed out, unknown state, ...)."""


# ----------------------------------------------------------------------------- storage
class OAuthStore:
    COLUMNS = {"url", "client_source", "client_info", "tokens", "token_expires_at",
               "resource_metadata", "as_metadata", "status", "detail", "public"}

    def __init__(self, db: Database):
        self.db = db
        with db.lock:
            db.conn.executescript(SCHEMA)
            have = {r["name"] for r in db.conn.execute("PRAGMA table_info(mcp_oauth)")}
            if "public" not in have:  # a table made without the column
                db.conn.execute("ALTER TABLE mcp_oauth ADD COLUMN public INTEGER NOT NULL DEFAULT 0")

    def get(self, server_id: int) -> dict[str, Any] | None:
        return self.db.query_one("SELECT * FROM mcp_oauth WHERE server_id=?", (server_id,))

    def ensure(self, server_id: int, url: str) -> dict[str, Any]:
        with self.db.lock:
            row = self.get(server_id)
            if row is None:
                self.db.conn.execute("INSERT INTO mcp_oauth (server_id, url, updated_at) VALUES (?, ?, ?)",
                                     (server_id, url, time.time()))
                row = self.get(server_id)
            return row  # type: ignore[return-value]

    def update(self, server_id: int, **fields: Any) -> None:
        bad = set(fields) - self.COLUMNS
        if bad:
            raise ValueError(f"unknown columns {bad}")
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE mcp_oauth SET {sets} WHERE server_id=?", (*fields.values(), server_id))

    def compare_and_set_tokens(self, server_id: int, expected: str | None, tokens: str | None,
                               expires_at: float | None, **extra: Any) -> bool:
        """Replace the tokens only if they are still `expected` (a refresh, or dropping a dead
        sign-in, must not undo a sign-out or a newer sign-in that happened meanwhile)."""
        bad = set(extra) - {"status", "detail"}
        if bad:
            raise ValueError(f"unknown columns {bad}")
        sets = "".join(f", {k}=?" for k in extra)
        with self.db.lock:
            cur = self.db.conn.execute(
                f"UPDATE mcp_oauth SET tokens=?, token_expires_at=?, updated_at=?{sets} WHERE server_id=? AND tokens IS ?",
                (tokens, expires_at, time.time(), *extra.values(), server_id, expected))
            return cur.rowcount == 1

    def commit_sign_in(self, server_id: int, *, url: str, client_before: str | None, client_info: str | None,
                       client_source: str | None, tokens: str | None, expires_at: float | None,
                       resource_metadata: str | None, as_metadata: str | None) -> bool:
        """Store a finished sign-in, only if the server still has the address and client the sign-in
        started with."""
        with self.db.lock:
            cur = self.db.conn.execute(
                "UPDATE mcp_oauth SET client_info=?, client_source=?, tokens=?, token_expires_at=?, "
                "resource_metadata=?, as_metadata=?, public=?, updated_at=? "
                "WHERE server_id=? AND url=? AND client_info IS ?",
                (client_info, client_source, tokens, expires_at, resource_metadata, as_metadata,
                 0 if tokens else 1, time.time(), server_id, url, client_before))
            return cur.rowcount == 1

    def forget_client(self, server_id: int, expected: str | None) -> None:
        """Drop an automatically registered client the provider no longer knows (if still stored)."""
        self.db.execute("UPDATE mcp_oauth SET client_info=NULL, client_source=NULL, updated_at=? "
                        "WHERE server_id=? AND client_source='dcr' AND client_info IS ?",
                        (time.time(), server_id, expected))

    def reset_interrupted_sign_ins(self) -> None:
        """Pending sign-ins live in memory; after a restart none of them can finish."""
        self.db.execute(
            "UPDATE mcp_oauth SET status = CASE WHEN tokens IS NOT NULL OR public = 1 THEN 'connected' "
            "ELSE 'needs_sign_in' END, "
            "detail = 'sign-in interrupted by a hub restart; start it again' WHERE status = 'signing_in'")

    def delete(self, server_id: int) -> None:
        self.db.execute("DELETE FROM mcp_oauth WHERE server_id=?", (server_id,))


class PendingSignIn:
    """mcp.client.auth.TokenStorage for an interactive sign-in. It starts without tokens (so the SDK
    really signs in) and keeps what the sign-in produces in memory; OAuthManager stores it only when
    the sign-in succeeds. The stored sign-in keeps working meanwhile."""

    def __init__(self, client_info: OAuthClientInformationFull | None):
        self.client_info = client_info
        self.registered = False          # True: the SDK registered a new client (DCR)
        self.tokens: OAuthToken | None = None
        self.expires_at: float | None = None

    async def get_tokens(self) -> OAuthToken | None:
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens
        self.expires_at = expiry(tokens.expires_in)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info, self.registered = client_info, True


# ----------------------------------------------------------------------------- helpers
def expiry(expires_in: int | None) -> float | None:
    """When to treat an access token as expired: a little before the provider says (a fifth of its
    lifetime, at most 30 s), so it isn't sent at the last moment; never earlier than issued."""
    if expires_in is None:
        return None
    lifetime = max(int(expires_in), 0)
    return time.time() + lifetime - min(30.0, lifetime / 5)


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    """Exceptions inside (nested) exception groups, each followed by its cause/context chain."""
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
        return
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, BaseExceptionGroup):
            yield from _leaves(cur)
            return
        yield cur
        cur = cur.__cause__ or cur.__context__


def find_exception(exc: BaseException, kind: type[BaseException] | tuple[type[BaseException], ...]):
    return next((e for e in _leaves(exc) if isinstance(e, kind)), None)


def explain(exc: BaseException) -> str:
    """One line a person can act on."""
    for kind in (NeedsSignIn, SignInError):
        if (found := find_exception(exc, kind)) is not None:
            return str(found)
    if (found := find_exception(exc, OAuthRegistrationError)) is not None:
        return ("the provider refused automatic client registration. Create an OAuth app in its settings with "
                f"this hub's callback address and enter its client ID and secret. ({safe_text(str(found), limit=200)})")
    if (found := find_exception(exc, OAuthTokenError)) is not None:
        return f"token request failed: {safe_text(str(found), limit=300)}"
    if (found := find_exception(exc, OAuthFlowError)) is not None:
        return f"OAuth error: {safe_text(str(found), limit=300)}"
    if (found := find_exception(exc, (httpx2.ConnectError, httpx2.ConnectTimeout))) is not None:
        return f"cannot reach the server: {found}"
    if (found := find_exception(exc, TimeoutError)) is not None:
        return "the server did not answer in time"
    leaf = next(_leaves(exc), exc)
    return f"{type(leaf).__name__}: {leaf}"


_SECRET_FIELDS = re.compile(
    r"""(?i)(["']?(?:access_token|refresh_token|id_token|client_secret|code|password)["']?\s*[:=]\s*["']?)[^"'&\s,}]+""")
_SECRET_SCHEMES = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}")


def safe_text(text: str, *secrets_: str | None, limit: int = 300) -> str:
    """Text from a provider (error bodies, exception messages) made fit to show to agents and on
    the dashboard: known secrets and anything that looks like a token or secret removed, one line,
    and short."""
    for s in sorted({x for x in secrets_ if x and len(x) >= 4}, key=len, reverse=True):
        text = text.replace(s, "<redacted>")
    text = _SECRET_SCHEMES.sub(lambda m: f"{m.group(1)} <redacted>", text)
    text = _SECRET_FIELDS.sub(lambda m: f"{m.group(1)}<redacted>", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _state_of(url: str) -> str:
    return (parse_qs(urlparse(url).query).get("state") or [""])[0]


def _with_scope(url: str, scope: str) -> str:
    """The authorization URL with the user's own scopes (the SDK picks them from the server's
    metadata otherwise; the code exchange and refreshes don't send a scope)."""
    parts = urlparse(url)
    query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    query["scope"] = scope
    return urlunparse(parts._replace(query=urlencode(query)))


@dataclass
class SignInFlow:
    server_id: int
    deadline: float  # time.monotonic()
    redirect_uri: str
    scope: str = ""
    binding: str = field(default_factory=lambda: secrets.token_urlsafe(24))  # browser cookie value
    state: str | None = None
    authorization_url: asyncio.Future[str] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    code: asyncio.Future[AuthorizationCodeResult] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    result: asyncio.Future[str] = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    task: asyncio.Task[None] | None = None


# ----------------------------------------------------------------------------- manager
class OAuthManager:
    """Sign-in from the dashboard, tokens for the proxy.

    get_server(server_id) returns the registry's entry (name, url, oauth_client_id,
    oauth_client_secret, oauth_scopes, ...) or None. on_change(server_id) is called when a
    sign-in finishes or is dropped, so the proxy can forget what it cached for that server.
    """

    def __init__(self, store: OAuthStore, get_server: Callable[[int], dict | None], *,
                 client_name: str = "Agent Bullpen", sign_in_timeout: float = 600, discovery_timeout: float = 30,
                 on_change: Callable[[int], None] = lambda _id: None):
        self.store, self.get_server = store, get_server
        self.client_name = client_name
        self.sign_in_timeout, self.discovery_timeout = sign_in_timeout, discovery_timeout
        self.on_change = on_change
        self.flows: dict[str, SignInFlow] = {}      # pending sign-ins by OAuth `state`
        self._flow_for: dict[int, SignInFlow] = {}  # at most one pending sign-in per server
        # Renewals in flight, keyed by (server id, the stored tokens they renew): one per sign-in.
        self._renewing: dict[tuple[int, str], asyncio.Task[str]] = {}
        self._down: dict[tuple[int, str], tuple[float, str]] = {}  # failed renewal: (until, message)
        self._failing: dict[tuple[int, str], tuple[float, int]] = {}  # renewals failing with a 4xx: (since, tries)
        self._refused: dict[int, tuple[str, float]] = {}            # (just-renewed token refused, when)
        store.reset_interrupted_sign_ins()

    # --- state
    def _load(self, server_id: int) -> tuple[dict, dict]:
        """(registry entry, oauth row), with the row brought in line with the entry: tokens for
        another URL, or for a client the user replaced or removed, are dropped."""
        server = self.get_server(server_id)
        if server is None or server.get("auth") != "oauth":
            raise KeyError(server_id)
        row = self.store.ensure(server_id, server["url"])
        changes: dict[str, Any] = {}
        if row["url"] != server["url"]:
            changes.update(url=server["url"], resource_metadata=None, as_metadata=None, public=0,
                           detail="the address changed; sign in again")
            if row["client_source"] == "dcr":
                changes.update(client_info=None, client_source=None)
        stored = OAuthClientInformationFull.model_validate_json(row["client_info"]) if row["client_info"] else None
        want_id, want_secret = server.get("oauth_client_id") or "", server.get("oauth_client_secret") or ""
        # RFC 6749 2.3.1: every provider must accept HTTP Basic; some only accept the secret in the body.
        want_method = ("client_secret_post" if server.get("oauth_auth_method") == "post" else "client_secret_basic") \
            if want_secret else "none"
        if want_id:
            if row["client_source"] != "manual" or stored is None or stored.client_id != want_id \
                    or (stored.client_secret or "") != want_secret or stored.token_endpoint_auth_method != want_method:
                info = OAuthClientInformationFull(
                    client_id=want_id, client_secret=want_secret or None, token_endpoint_auth_method=want_method,
                    redirect_uris=None, grant_types=["authorization_code", "refresh_token"],
                    response_types=["code"], client_name=self.client_name)
                changes.update(client_info=info.model_dump_json(exclude_none=True), client_source="manual",
                               detail="client ID saved; sign in")
        elif row["client_source"] == "manual":
            changes.update(client_info=None, client_source=None, detail="client ID removed; sign in again")
        if changes:
            if "client_info" in changes or "url" in changes:
                changes.update(tokens=None, token_expires_at=None, status="needs_sign_in")
                # A sign-in started for the old address or client must not finish: its tokens
                # would be for the wrong server (it could not store them anyway, see commit_sign_in).
                self._cancel_flow(server_id)
            self.store.update(server_id, **changes)
            row = self.store.get(server_id) or row
        return server, row

    def _cancel_flow(self, server_id: int) -> None:
        flow = self._flow_for.pop(server_id, None)
        if flow is not None:
            if flow.state:
                self.flows.pop(flow.state, None)
            if flow.task is not None and not flow.task.done():
                flow.task.cancel()

    def view(self, server_id: int) -> dict[str, Any]:
        """For the dashboard: no secrets."""
        try:
            _server, row = self._load(server_id)
        except KeyError:
            return {}
        client = OAuthClientInformationFull.model_validate_json(row["client_info"]) if row["client_info"] else None
        asm = OAuthMetadata.model_validate_json(row["as_metadata"]) if row["as_metadata"] else None
        exp = row["token_expires_at"]
        return {"status": row["status"], "detail": row["detail"], "signed_in": bool(row["tokens"]),
                "client_source": row["client_source"], "client_id": client.client_id if client else None,
                "access_expires_in": round(exp - time.time()) if exp else None,
                "issuer": str(asm.issuer) if asm else None}

    def settings_changed(self, server_id: int) -> None:
        """Called right after the server was edited on the MCP page: a new address or client
        cancels an open sign-in and drops the stored one now, not at the next use."""
        try:
            self._load(server_id)
        except KeyError:
            pass

    def forget(self, server_id: int) -> None:
        """The server was deleted or no longer uses OAuth."""
        self._cancel_flow(server_id)
        self._refused.pop(server_id, None)
        self.store.delete(server_id)

    def sign_out(self, server_id: int) -> None:
        self._load(server_id)
        self.store.update(server_id, tokens=None, token_expires_at=None, status="needs_sign_in", detail="signed out")
        self._refused.pop(server_id, None)
        self.on_change(server_id)

    # --- sign-in (dashboard + browser)
    def client_metadata(self, redirect_uri: str, scope: str | None = None) -> OAuthClientMetadata:
        """What the hub registers as. A fresh object each time: the SDK edits .scope in place."""
        loopback = urlparse(redirect_uri).hostname in ("localhost", "127.0.0.1", "::1")
        return OAuthClientMetadata(
            redirect_uris=[redirect_uri], client_name=self.client_name, scope=scope or None,
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            application_type="native" if loopback else "web")

    async def start_sign_in(self, server_id: int, redirect_uri: str) -> dict[str, Any]:
        """Returns {"authorization_url", "binding"} for the browser, or a final status when no
        browser is needed (the server answered without asking for OAuth)."""
        try:
            server, _row = self._load(server_id)
        except KeyError:
            raise SignInError("this server was deleted or no longer uses an OAuth sign-in") from None
        scope = (server.get("oauth_scopes") or "").strip()
        previous = self._flow_for.get(server_id)
        if previous is not None and previous.task is not None and not previous.task.done() \
                and previous.redirect_uri == redirect_uri and previous.scope == scope \
                and previous.deadline - time.monotonic() > self.sign_in_timeout / 2:
            # Already pending (a second click, another tab): hand out the same authorization URL.
            await asyncio.wait({previous.authorization_url, previous.task}, timeout=self.discovery_timeout,
                               return_when=asyncio.FIRST_COMPLETED)
            if previous.authorization_url.done() and not previous.task.done() \
                    and self._flow_for.get(server_id) is previous:
                return {"status": "signing_in", "authorization_url": previous.authorization_url.result(),
                        "binding": previous.binding}
            if not previous.task.done() and self._flow_for.get(server_id) is previous:
                raise SignInError("a sign-in is already starting; try again in a moment")
        # Read the settings now: they may have changed while we waited for the previous sign-in.
        try:
            server, row = self._load(server_id)
        except KeyError:
            raise SignInError("this server was deleted or no longer uses an OAuth sign-in") from None
        if self._flow_for.get(server_id) is not None:
            # Replaced: from another address, close to its time limit, or settings changed. It
            # stored nothing, so cancelling it is harmless.
            self._cancel_flow(server_id)
        flow = SignInFlow(server_id, deadline=time.monotonic() + self.sign_in_timeout, redirect_uri=redirect_uri,
                          scope=(server.get("oauth_scopes") or "").strip())
        self._flow_for[server_id] = flow
        self.store.update(server_id, status="signing_in", detail="contacting the provider")
        flow.task = asyncio.create_task(self._sign_in(server, row, flow), name=f"sign-in:{server['name']}")
        await asyncio.wait({flow.authorization_url, flow.task}, timeout=self.discovery_timeout,
                           return_when=asyncio.FIRST_COMPLETED)
        if flow.authorization_url.done() and not flow.task.done():
            return {"status": "signing_in", "authorization_url": flow.authorization_url.result(),
                    "binding": flow.binding}
        if flow.task.done():
            row = self.store.get(server_id) or {}
            return {"status": row.get("status"), "detail": row.get("detail")}
        flow.task.cancel()
        raise SignInError("the provider did not answer in time (discovery or registration)")

    async def _sign_in(self, server: Mapping[str, Any], row: Mapping[str, Any], flow: SignInFlow) -> None:
        sid, name = server["id"], server["name"]
        client = OAuthClientInformationFull.model_validate_json(row["client_info"]) if row["client_info"] else None
        if client is not None and row["client_source"] == "dcr":
            expires = client.client_secret_expires_at
            if flow.redirect_uri not in [str(u) for u in client.redirect_uris or []] \
                    or (expires not in (None, 0) and expires < time.time() + 60) or not row["tokens"]:
                # Register again unless the stored registration is proven good (it holds a working
                # sign-in): the hub's address may have changed since, the secret may have expired,
                # or the provider may have forgotten it, and then Sign in could never succeed.
                client = None
        pending = PendingSignIn(client)
        was_public = bool(row["public"]) and not row["tokens"]
        scope = (server.get("oauth_scopes") or "").strip()

        async def on_redirect(authorization_url: str) -> None:
            state = _state_of(authorization_url)
            if not state:
                raise OAuthFlowError("the authorization URL carries no state")
            asm = provider.context.oauth_metadata
            methods = asm.code_challenge_methods_supported if asm else None
            if methods is not None and "S256" not in methods:
                raise OAuthFlowError(f"the authorization server does not support PKCE S256 (it lists {methods})")
            if methods is None:
                log.warning("%s: authorization server metadata does not list code_challenge_methods_supported", name)
            if scope:
                authorization_url = _with_scope(authorization_url, scope)
            flow.state = state
            self.flows[state] = flow
            self.store.update(sid, status="signing_in", detail="waiting for you to finish in the browser")
            if not flow.authorization_url.done():
                flow.authorization_url.set_result(authorization_url)

        async def on_callback() -> AuthorizationCodeResult:
            try:
                return await asyncio.wait_for(flow.code, timeout=max(flow.deadline - time.monotonic(), 0.01))
            except TimeoutError:
                raise SignInError(f"sign-in to {name} was not finished within {self.sign_in_timeout / 60:.0f} "
                                  "minutes; start it again") from None
            finally:
                if flow.state:
                    self.flows.pop(flow.state, None)

        provider = OAuthClientProvider(
            server_url=server["url"], client_metadata=self.client_metadata(flow.redirect_uri, scope),
            storage=pending, redirect_handler=on_redirect, callback_handler=on_callback)
        try:
            async with asyncio.timeout(self.sign_in_timeout + 2 * self.discovery_timeout):
                async with httpx2.AsyncClient(auth=provider, timeout=httpx2.Timeout(30.0, read=120.0),
                                              transport=capped_transport(MAX_SIGNIN_BYTES)) as http:
                    async with Client(streamable_http_client(server["url"], http_client=http)) as client:
                        tools = await client.list_tools()
        except (asyncio.CancelledError, Exception) as exc:
            log.info("sign-in to %s failed: %s", name, type(exc).__name__)
            ctx = provider.context
            if isinstance(exc, asyncio.CancelledError):
                why = "sign-in cancelled"
            elif find_exception(exc, OAuthRegistrationError) is not None \
                    and ctx.protected_resource_metadata is None and ctx.oauth_metadata is None:
                why = ("this server doesn't offer an OAuth sign-in (it publishes no OAuth metadata): edit it and "
                       "choose 'API key or token in a header'")
            else:
                why = f"sign-in failed: {explain(exc)}"
            why = safe_text(why, server.get("oauth_client_secret"), *self._secrets_of(pending), limit=700)
            if (self.store.get(sid) or {}).get("tokens"):  # the previous sign-in is untouched and still used
                self._finish(flow, sid, "connected", f"{why} (still using the previous sign-in)", ok=False)
            elif was_public:
                self._finish(flow, sid, "connected", f"{why} (it still works without a sign-in)", ok=False)
            else:
                self._finish(flow, sid, "needs_sign_in", why, ok=False)
            if isinstance(exc, asyncio.CancelledError):
                raise
            return
        ctx = provider.context
        now = self.get_server(sid) or {}
        unchanged = now.get("auth") == "oauth" and all(
            (now.get(k) or "") == (server.get(k) or "") for k in ("url", "oauth_client_id", "oauth_client_secret"))
        stored = unchanged and self._flow_for.get(sid) is flow and self.store.commit_sign_in(
            sid, url=server["url"], client_before=row["client_info"],
            client_info=pending.client_info.model_dump_json(exclude_none=True) if pending.client_info else None,
            client_source="dcr" if pending.registered else (row["client_source"] if pending.client_info else None),
            tokens=pending.tokens.model_dump_json(exclude_none=True) if pending.tokens else None,
            expires_at=pending.expires_at,
            resource_metadata=ctx.protected_resource_metadata.model_dump_json(exclude_none=True)
            if ctx.protected_resource_metadata else None,
            as_metadata=ctx.oauth_metadata.model_dump_json(exclude_none=True) if ctx.oauth_metadata else None)
        if not stored:
            why = "sign-in not saved: the server's address or client changed while it was open; start it again"
            has_tokens = bool((self.store.get(sid) or {}).get("tokens"))
            self._finish(flow, sid, "connected" if has_tokens else "needs_sign_in", why, ok=False)
            return
        self._refused.pop(sid, None)
        detail = f"{'signed in' if pending.tokens else 'no sign-in needed'}; {len(tools.tools)} tools"
        self._finish(flow, sid, "connected", detail, ok=True)
        self.on_change(sid)

    def _finish(self, flow: SignInFlow, sid: int, status: str, detail: str, ok: bool) -> None:
        if self._flow_for.get(sid) is flow:  # a newer sign-in owns the status otherwise
            del self._flow_for[sid]
            if self.store.get(sid) is not None:
                self.store.update(sid, status=status, detail=detail)
        if not flow.result.done():
            if ok:
                flow.result.set_result(detail)
            else:
                flow.result.set_exception(SignInError(detail))
                flow.result.exception()  # mark retrieved, so asyncio does not warn when nobody waits
        if flow.state:
            self.flows.pop(flow.state, None)

    def flow_for_state(self, state: str) -> SignInFlow | None:
        return self.flows.get(state or "")

    async def finish_sign_in(self, params: Mapping[str, str], binding: str | None) -> tuple[int, str]:
        """GET /oauth/callback. Returns (http status, message for the page)."""
        state = params.get("state") or ""
        flow = self.flows.get(state)
        if flow is None or flow.code.done():
            return 400, "This sign-in link is unknown, already used or expired. Start again from the hub."
        if not (binding and hmac.compare_digest(binding, flow.binding)):
            # Not the browser that pressed "Sign in" on the dashboard: leave the flow alone.
            return 403, "Finish the sign-in in the browser where you started it on the hub's MCP page."
        server = self.get_server(flow.server_id) or {"name": f"server {flow.server_id}"}
        name = server["name"]
        if params.get("error"):
            flow.code.set_exception(SignInError(
                f"{name} refused: {params['error']} {params.get('error_description') or ''}".strip()))
        elif not params.get("code"):
            flow.code.set_exception(SignInError(f"{name} sent no authorization code"))
        else:
            flow.code.set_result(AuthorizationCodeResult(code=params["code"], state=state, iss=params.get("iss")))
        try:
            detail = await asyncio.wait_for(asyncio.shield(flow.result), timeout=self.discovery_timeout)
        except SignInError as exc:
            return 502, str(exc)
        except TimeoutError:
            return 504, f"{name} took too long to issue tokens; check the hub's MCP page."
        return 200, f"Signed in to {name} ({detail}). You can close this tab."

    @staticmethod
    def _secrets_of(pending: PendingSignIn) -> list[str | None]:
        out: list[str | None] = []
        if pending.tokens is not None:
            out += [pending.tokens.access_token, pending.tokens.refresh_token]
        if pending.client_info is not None:
            out.append(pending.client_info.client_secret)
        return out

    # --- tokens for the proxy (never interactive)
    @staticmethod
    def _fresh(row: Mapping[str, Any]) -> bool:
        expires = row["token_expires_at"]
        return expires is None or expires > time.time()  # the safety margin is applied when stored (expiry())

    async def token(self, server_id: int, rejected: str | None = None, url: str | None = None) -> str | None:
        """A valid access token for this server. None: it needed no OAuth when it was signed in.

        url: the address the caller will send it to; a token is only handed out for the address
        the sign-in was made for (an agent call still running against an old address gets none).
        rejected: an access token the server just refused; the answer is a newer token (renewing
        only if the refused one is still the stored one), or TokenUnavailable.
        Raises NeedsSignIn (only a person can fix it), AuthServerDown (the sign-in is kept) or
        TokenUnavailable."""
        try:
            server, row = self._load(server_id)
        except KeyError:
            raise NeedsSignIn(f"server {server_id} no longer uses an OAuth sign-in") from None
        name = server["name"]
        if url is not None and url != server["url"]:
            raise TokenUnavailable(f"{name}: its address was changed on the hub while this call was running; "
                                   "try again")
        if not row["tokens"]:
            if row["public"]:
                return None  # the last sign-in found that the server wants no OAuth
            raise NeedsSignIn(self._not_signed_in(name, row["detail"]))
        current = OAuthToken.model_validate_json(row["tokens"]).access_token
        if rejected is None or rejected != current:
            if self._fresh(row):
                return current
        else:
            refused = self._refused.get(server_id)
            if refused is not None and refused[0] == rejected and time.monotonic() - refused[1] < REFUSED_FOR:
                raise TokenUnavailable(f"{name} refused the hub's sign-in even right after renewing it; if this "
                                       "keeps happening, sign in again on the hub's MCP page")
        token = await self._renewed(server, row)
        # The wait may have been long: hand the token out only if the address is still the one the
        # caller is talking to (a new sign-in made for a new address must never reach the old one).
        now = self.get_server(server_id)
        if url is not None and (now is None or now["url"] != url):
            raise TokenUnavailable(f"{name}: its address was changed on the hub while this call was running; "
                                   "try again")
        return token

    @staticmethod
    def _not_signed_in(name: str, detail: str | None) -> str:
        if detail and "doesn't offer an OAuth sign-in" in detail:
            return f"{name}: {detail}."
        return f"{name} is not signed in ({detail or 'never signed in'}). Press 'Sign in' for it on the hub's MCP page."

    async def _renewed(self, server: Mapping[str, Any], row: Mapping[str, Any]) -> str:
        key = (server["id"], row["tokens"])
        down = self._down.get(key)
        if down is not None:
            if down[0] > time.monotonic():
                raise AuthServerDown(down[1])
            del self._down[key]
        task = self._renewing.get(key)
        if task is None:
            # An empty context: the caller's per-call bookkeeping (mcp_proxy's status lists) must not
            # see the renewal's own HTTP traffic.
            task = asyncio.get_running_loop().create_task(self._renew(server, row), name=f"renew:{server['name']}",
                                                          context=contextvars.Context())
            self._renewing[key] = task

            def done(t: asyncio.Task, k: tuple[int, str] = key) -> None:
                self._renewing.pop(k, None)
                if not t.cancelled():
                    t.exception()  # retrieved: every waiter gets it through the shield
            task.add_done_callback(done)
        # The caller may be cancelled (agent gone, call timed out); the renewal still finishes and
        # stores what the provider answered.
        return await asyncio.shield(task)

    async def _renew(self, server: Mapping[str, Any], row: Mapping[str, Any]) -> str:
        sid, name, basis = server["id"], server["name"], row["tokens"]
        tokens = OAuthToken.model_validate_json(basis)
        client = OAuthClientInformationFull.model_validate_json(row["client_info"]) if row["client_info"] else None
        if not tokens.refresh_token or client is None:
            if self._fresh(row):  # refused before it expired; nothing to renew it with
                raise TokenUnavailable(f"{name} refused the hub's sign-in, which it can't renew; if this keeps "
                                       "happening, sign in again on the hub's MCP page")
            return self._dead(server, row, "the access token expired and the provider gave the hub no way to renew it")
        # Build the request the way the SDK's own refresh does.
        ctx = OAuthContext(server_url=server["url"],
                           client_metadata=self.client_metadata("http://localhost/oauth/callback"),
                           storage=PendingSignIn(None), redirect_handler=None, callback_handler=None)
        ctx.protocol_version = LATEST_PROTOCOL_VERSION
        ctx.client_info = client
        if row["resource_metadata"]:
            ctx.protected_resource_metadata = ProtectedResourceMetadata.model_validate_json(row["resource_metadata"])
        if row["as_metadata"]:
            ctx.oauth_metadata = OAuthMetadata.model_validate_json(row["as_metadata"])
        token_url = str(ctx.oauth_metadata.token_endpoint) if ctx.oauth_metadata and ctx.oauth_metadata.token_endpoint \
            else urljoin(ctx.get_authorization_base_url(server["url"]), "/token")
        data = {"grant_type": "refresh_token", "refresh_token": tokens.refresh_token, "client_id": client.client_id}
        if ctx.should_include_resource_param(ctx.protocol_version):
            data["resource"] = ctx.get_resource_url()
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        try:
            data, headers = ctx.prepare_token_auth(data, headers)
        except OAuthTokenError as exc:
            return self._dead(server, row, safe_text(str(exc)))
        try:
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(15.0, read=RENEW_TIMEOUT), follow_redirects=False,
                                          transport=capped_transport(MAX_TOKEN_BYTES)) as http:
                async with asyncio.timeout(RENEW_TIMEOUT + 30):
                    response = await http.post(token_url, data=data, headers=headers)
        except (httpx2.HTTPError, TimeoutError) as exc:
            return self._down_for(server, row, f"cannot reach its authorization server to renew the sign-in "
                                               f"({type(exc).__name__})")
        try:
            payload = json.loads(response.content)
        except ValueError:
            payload = None
        if response.status_code == 200 and isinstance(payload, dict) and payload.get("access_token"):
            try:
                new = OAuthToken.model_validate(payload)
            except ValidationError:
                return self._down_for(server, row, "its authorization server answered the renewal with something "
                                                   "that is not a usable token")
            if not new.refresh_token:
                new.refresh_token = tokens.refresh_token  # RFC 6749 6: no new one means keep the old one
            if new.scope is None:
                new.scope = tokens.scope
            extra = {}
            if self._refused.pop(sid, None) is not None or (row["detail"] or "").startswith(("refused", "renewal failing")):
                extra = {"detail": f"signed in (renewed at {time.strftime('%H:%M')})"}
            if self.store.compare_and_set_tokens(sid, basis, new.model_dump_json(exclude_none=True),
                                                 expiry(new.expires_in), **extra):
                self._failing.pop((sid, basis), None)
                return new.access_token
            return self._current(server)  # signed out or signed in again meanwhile
        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), str) else None
        error = error if error and re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", error) else ("other" if error else None)
        if error and error not in TEMPORARY_ERRORS and response.status_code in (400, 401):
            # A real OAuth error answer (RFC 6749 5.2): asking again with the same refresh token can't work.
            return self._dead(server, row, f"the provider refused to renew the sign-in ({error})",
                              forget_client=error == "invalid_client")
        what = f"HTTP {response.status_code}" + (f" ({error})" if error else "")
        if 400 <= response.status_code < 500 and response.status_code not in (408, 425, 429):
            first, tries = self._failing.get((sid, basis), (time.monotonic(), 0))
            self._failing[(sid, basis)] = (first, tries + 1)
            if tries + 1 >= 3 and time.monotonic() - first >= GIVE_UP_AFTER:
                return self._dead(server, row, f"renewing the sign-in kept failing for {GIVE_UP_AFTER / 60:.0f} "
                                               f"minutes ({what})")
        return self._down_for(server, row, f"its authorization server answered {what} to the renewal")

    def _current(self, server: Mapping[str, Any]) -> str:
        """The stored token now (the sign-in changed while this renewal ran), or why there is none.
        Only a token for the same address as the one being renewed is handed out."""
        cur = self.store.get(server["id"])
        if cur and cur["tokens"]:
            if cur["url"] == server["url"] and self._fresh(cur):
                return OAuthToken.model_validate_json(cur["tokens"]).access_token
            raise TokenUnavailable(f"{server['name']}: the sign-in changed while this call was running; try again")
        raise NeedsSignIn(self._not_signed_in(server["name"], (cur or {}).get("detail") or "signed out"))

    def _dead(self, server: Mapping[str, Any], row: Mapping[str, Any], why: str, forget_client: bool = False) -> str:
        sid = server["id"]
        if not self.store.compare_and_set_tokens(sid, row["tokens"], None, None, status="needs_sign_in", detail=why):
            return self._current(server)  # the tokens changed meanwhile: they are not the dead ones
        if forget_client:
            self.store.forget_client(sid, row["client_info"])  # the provider no longer knows this registration
        log.warning("%s: sign-in dropped: %s", server["name"], why)
        self.on_change(sid)
        raise NeedsSignIn(f"{server['name']}: {why}. Sign in again on the hub's MCP page.")

    def _down_for(self, server: Mapping[str, Any], row: Mapping[str, Any], why: str) -> str:
        message = f"{server['name']}: {why}; the sign-in is kept, try again later"
        now = time.monotonic()
        for key in [k for k, v in self._down.items() if v[0] <= now]:
            del self._down[key]
        self._down[(server["id"], row["tokens"])] = (now + DOWN_FOR, message)
        for key in [k for k, v in self._failing.items() if now - v[0] > 2 * GIVE_UP_AFTER]:
            del self._failing[key]
        if not (row["detail"] or "").startswith("renewal failing"):
            self.store.update(server["id"], detail=f"renewal failing since {time.strftime('%H:%M')}: {why}")
        log.warning("%s", message)
        raise AuthServerDown(message)

    def rejected(self, server_id: int, token: str, url: str | None = None) -> None:
        """The server refused `token` right after it was renewed. The sign-in is kept (a renewal
        that succeeded proves the grant is alive, and resource servers can lag behind), but it is
        not renewed again for a while, and the dashboard says what happened."""
        server = self.get_server(server_id)
        row = self.store.get(server_id)
        if server is None or row is None or not row["tokens"] or (url is not None and url != server["url"]) \
                or OAuthToken.model_validate_json(row["tokens"]).access_token != token:
            return
        self._refused[server_id] = (token, time.monotonic())
        self.store.update(server_id, detail=f"refused a just-renewed sign-in at {time.strftime('%H:%M')}; "
                                            "if agents keep failing, sign in again")
        log.warning("%s refused a just-renewed access token", server["name"])

    async def close(self, wait: float = RENEW_TIMEOUT + 5) -> None:
        """At hub shutdown: let renewals in flight finish, so a refresh token the provider has just
        rotated is stored rather than lost."""
        tasks = [t for t in self._renewing.values() if not t.done()]
        if tasks:
            log.info("waiting for %d token renewal(s) to finish", len(tasks))
            await asyncio.wait(tasks, timeout=wait)
