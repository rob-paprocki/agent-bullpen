"""The hub's own MCP endpoint (/mcp): shared memory tools for every agent.

Any MCP client (Claude Code, Gemini CLI, Antigravity, claude.ai through a
connector) can call:

  remember(text, scope, tags, pin)   save a note everyone can see
  recall(query, scope, limit)        search the notes
  list_notes(scope, pinned_only)     list recent or pinned notes

Built on the official MCP Python SDK (package `mcp`, v2). Tested with mcp 2.3.0,
which serves both the 2026-07-28 protocol and older 2025 clients.

Authentication: `Authorization: Bearer <token>`. The token lives in the hub's
database and is handed to each runner, which writes it into its agents'
MCP settings. Optionally (HUB_MCP_TRUST_ACCESS=1 plus HUB_CF_ACCESS_TEAM and
HUB_CF_ACCESS_AUD) a request carrying a valid, signed Cloudflare Access token is
accepted without the bearer token, so a claude.ai connector protected by Access
Managed OAuth works.

Agents can't pin notes (pinned notes go into every session everywhere); they can
only ask, which tags the note for the user to review on the Memory page.
"""

from __future__ import annotations

import contextvars
import hmac
import logging
from typing import Any, Callable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from memory import Memory

log = logging.getLogger("hub.mcp")

# Who is calling, from the X-Bullpen-Agent header the runners put in each agent's config.
CALLER: contextvars.ContextVar[str] = contextvars.ContextVar("caller", default="mcp")

INSTRUCTIONS = (
    "Shared memory for all of the user's AI agents and accounts, on every machine. "
    "Use `remember` to save a durable fact, preference or decision the user wants kept "
    "(or that other agents will need later); use `recall` to search before asking the user "
    "something they may have told another agent. Pinned notes are already in your context."
)


def build(memory: Memory):
    """Create the MCP server. Returns (server, starlette_app) or (None, None) if the SDK is missing."""
    try:
        from mcp.server import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings
    except ImportError as err:  # hub still works without MCP
        log.warning("MCP endpoint disabled: the 'mcp' package is not installed (%s)", err)
        return None, None

    server = MCPServer("bullpen", instructions=INSTRUCTIONS, log_level="WARNING")

    def note_view(n: dict) -> dict[str, Any]:
        return {"id": n["id"], "text": n["text"], "scope": n["scope"],
                "tags": [t for t in (n["tags"] or "").split(",") if t],
                "pinned": bool(n["pinned"]), "source": n["source"], "updated_at": n["updated_at"]}

    @server.tool()
    async def remember(text: str, scope: str = "global", tags: list[str] = [],  # noqa: B006
                       pin: bool = False) -> dict[str, Any]:
        """Save a note to the user's shared memory, which every AI agent and account on every
        machine can read. Write it as a short, self-contained fact ("The user prefers PowerShell 7
        on Windows machines"). scope: 'global' (everyone, the default), 'agent:<name>' or 'project:<name>'.
        pin=true only REQUESTS pinning (always-in-context); the user approves it on the hub's
        Memory page."""
        try:
            note = memory.add(text, scope, tags, pinned=False, source=CALLER.get(), request_pin=pin)
        except ValueError as err:
            return {"saved": False, "error": str(err)}
        out: dict[str, Any] = {"saved": True, "note": note_view(note)}
        if pin:
            out["pin"] = "requested: the user can pin it on the hub's Memory page"
        return out

    @server.tool()
    async def recall(query: str = "", scope: str = "", limit: int = 10) -> dict[str, Any]:
        """Search the user's shared memory (notes saved by any agent or by the user).
        query: words to look for (empty = most recent). scope: optional filter such as
        'global', 'agent:<name>' or 'project:myapp'."""
        notes = memory.search(query, scope or None, max(1, min(limit, 50)))
        return {"count": len(notes), "notes": [note_view(n) for n in notes]}

    @server.tool()
    async def list_notes(scope: str = "", pinned_only: bool = False, limit: int = 20) -> dict[str, Any]:
        """List recent notes from the shared memory, optionally only pinned ones or one scope."""
        notes = memory.search("", scope or None, max(1, min(limit, 100)))
        if pinned_only:
            notes = [n for n in notes if n["pinned"]]
        return {"count": len(notes), "notes": [note_view(n) for n in notes]}

    # The hub checks a bearer token itself, so the SDK's localhost-only Host check would only
    # get in the way of LAN and Cloudflare addresses.
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return server, app


class AccessVerifier:
    """Checks a Cloudflare Access token (Cf-Access-Jwt-Assertion): signature against the team's
    public keys, audience (the Access application's AUD tag), issuer and expiry."""

    def __init__(self, team: str, aud: str):
        import jwt  # PyJWT (with cryptography), installed with the hub's requirements
        team = team.strip().removeprefix("https://").rstrip("/")
        self.jwt, self.aud, self.issuer = jwt, aud.strip(), f"https://{team}"
        self.keys = jwt.PyJWKClient(f"https://{team}/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> dict | None:
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            return self.jwt.decode(token, key, algorithms=["RS256"], audience=self.aud, issuer=self.issuer,
                                   options={"require": ["exp", "iat", "aud", "iss"]})
        except Exception as err:  # any problem means "not authenticated"
            log.info("rejected a Cloudflare Access token: %s", err)
            return None


def access_verifier_from_env(env: dict) -> "AccessVerifier | None":
    if env.get("HUB_MCP_TRUST_ACCESS", "0") != "1":
        return None
    team, aud = env.get("HUB_CF_ACCESS_TEAM", ""), env.get("HUB_CF_ACCESS_AUD", "")
    if not team or not aud:
        log.error("HUB_MCP_TRUST_ACCESS=1 needs HUB_CF_ACCESS_TEAM and HUB_CF_ACCESS_AUD; "
                  "accepting only the bearer token")
        return None
    try:
        return AccessVerifier(team, aud)
    except ImportError:
        log.error("HUB_MCP_TRUST_ACCESS=1 needs PyJWT with cryptography; accepting only the bearer token")
        return None


class McpAuth:
    """ASGI middleware: /mcp and everything under it (the servers the hub relays, /mcp/s/<name>)
    need the hub's MCP bearer token. A valid Cloudflare Access token, when that is configured, is
    accepted for the shared memory at /mcp itself only: anyone the Access policy lets in would
    otherwise also get the keys and sign-ins of every relayed server.

    `exempt` names endpoints that check a token of their own (the job tools at /mcp/job): exactly
    that path, with or without a trailing slash, is passed through untouched. Every other path
    under the prefix keeps the check."""

    def __init__(self, app: ASGIApp, get_token: Callable[[], str],
                 access: "AccessVerifier | None" = None, prefix: str = "/mcp",
                 exempt: "tuple[str, ...] | list[str]" = ()) -> None:
        self.app, self.get_token, self.access, self.prefix = app, get_token, access, prefix
        self.exempt = {p.rstrip("/") for p in exempt if p.rstrip("/")}
        self.exempt |= {p + "/" for p in self.exempt}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path, root = scope.get("path", ""), scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]  # routing ignores the root path (uvicorn --root-path), so must we
        if not path.startswith("/"):
            path = "/" + path  # a raw request target without a leading slash still routes
        if scope["type"] != "http" or not (path == self.prefix or path.startswith(self.prefix + "/")) \
                or path in self.exempt:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        scheme, _, cred = headers.get("authorization", "").partition(" ")
        ok = scheme.lower() == "bearer" and hmac.compare_digest(cred.strip().encode(), self.get_token().encode())
        caller = headers.get("x-bullpen-agent", "")[:100] or "mcp client"
        if not ok and self.access is not None and headers.get("cf-access-jwt-assertion") \
                and path in (self.prefix, self.prefix + "/"):
            import anyio
            claims = await anyio.to_thread.run_sync(self.access.verify, headers["cf-access-jwt-assertion"])
            if claims:
                ok = True
                caller = str(claims.get("email") or claims.get("common_name") or "cloudflare-access")[:100]
        if not ok:
            await JSONResponse({"error": "invalid_token"}, status_code=401,
                               headers={"WWW-Authenticate": 'Bearer realm="bullpen"'})(scope, receive, send)
            return
        token = CALLER.set(caller)
        try:
            await self.app(scope, receive, send)
        finally:
            CALLER.reset(token)
