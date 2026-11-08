"""Proxy centrally configured MCP servers through the hub: /mcp/s/<name>.

Each upstream MCP server configured on the hub (URL plus secret headers, or an
OAuth sign-in done once on the dashboard; kept only on the hub) is served at its
own endpoint, /mcp/s/<name>. An agent adds that URL with the hub's bearer token
and sees the upstream's own tools under their own names, so per-server
permission rules (Claude Code's `mcp__github__*`, Gemini CLI's per-server
`includeTools`/`trust`) keep working.

One low-level `mcp.server.Server` serves every upstream. The ASGI endpoint on
/mcp/s/{name} records which upstream the request is for (in the ASGI scope and
in a contextvar) and hands the request to that server's session manager; the
`tools/list` and `tools/call` handlers then talk to that upstream with the SDK
client (`mcp.Client` over `streamable_http_client` with an `httpx2.AsyncClient`
carrying the stored headers).

* tools/list: fetched from the upstream once per `list_ttl` (single-flight per
  upstream) and returned as the upstream sent it. A stale list is served if a
  refresh fails; with no list at all the error is a JSON-RPC error naming the
  upstream and the reason.
* tools/call: one fresh MCP connection per call (stateless hub, nothing to
  expire), over a pooled HTTP connection per upstream. The upstream's result
  (content, structuredContent, isError, _meta) is passed back as is.
  Unreachable / auth failure / timeout -> `isError: true` with a clear text.
  A JSON-RPC error the upstream itself sent is passed through as that JSON-RPC
  error (MCPServer 2.3.0 reports unknown tools and bad arguments as isError
  results, so those arrive as ordinary results).
* The upstream's protocol era is remembered after the first connection, so a
  2026-07-28 upstream costs one POST per call and a 2025 upstream does not pay
  for a failing `server/discover` probe every time.
* OAuth upstreams (Upstream.oauth_id set): every request carries the access
  token from the token source (upstream_oauth.OAuthManager), which refreshes it
  when it expires. A 401 gets one forced refresh and one retry.

Tested with mcp 2.3.0 (server and client), httpx2 2.16.0, starlette 1.10.
"""

from __future__ import annotations

import base64
import contextvars
import json
import logging
import re
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import unquote

import anyio
import anyio.abc
import httpx2
import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.shared.inbound import x_mcp_header_map
from mcp_types import CONNECTION_CLOSED, INTERNAL_ERROR, INVALID_REQUEST, PARSE_ERROR, REQUEST_TIMEOUT
from pydantic import ValidationError
from mcp_types.version import MODERN_PROTOCOL_VERSIONS
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

log = logging.getLogger("hub.mcp_proxy")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")  # RFC 9110 token
_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e\t]*$")  # visible ASCII, space, tab
SCOPE_KEY = "bullpen.mcp_upstream"
UPSTREAM: contextvars.ContextVar[str | None] = contextvars.ContextVar("mcp_upstream", default=None)
# HTTP statuses the upstream answered during the current call (filled by an httpx2 event hook).
_HTTP_STATUSES: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar("upstream_statuses", default=None)
# Why an OAuth token could not be refreshed during the current call (filled by _OAuthBearer).
_AUTH_ERRORS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("upstream_auth_errors", default=None)
# Credentials sent during the current call that the configuration doesn't show (OAuth access
# tokens): scrubbed from error texts like the stored secrets.
_SENT_SECRETS: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("upstream_sent", default=None)
# WWW-Authenticate challenges of 401/403 answers during the current call.
_CHALLENGES: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("upstream_challenges", default=None)

_TOO_LARGE = -413  # pseudo status recorded when a response exceeds max_result_bytes
CLIENT_INFO = types.Implementation(name="bullpen-proxy", version="0.1")
LIST_PAGE_CAP = 50            # pages of tools/list read at most
MAX_TOOLS = 2000              # tools kept per upstream at most
MAX_TOOL_LIST_BYTES = 8 * 1024 * 1024  # total size of one upstream's tool list kept at most
# Header and query parameter names whose values are credentials (any value of 12+ characters counts too).
_SECRET_HEADER_RE = re.compile(r"(?i)(auth|token|key|secret|session|cookie|passw|credential|signature|sig$|code)")
MAX_ERROR_DATA_BYTES = 64 * 1024  # an upstream's extra error data larger than this is left out
INSTRUCTIONS_WAIT = 3.0  # seconds an agent's initialize waits for the upstream's instructions
# Messages the SDK's client transport synthesizes for HTTP-level failures (no JSON-RPC body).
_SYNTHETIC = ("Server returned an error response", "Not Found", "Session terminated", "Connection closed")


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)  # secrets; never leave the hub
    call_timeout: float = 60.0  # one upstream tools/call (or tools/list) request
    connect_timeout: float = 5.0  # TCP/TLS connect; also bounds the handshake
    list_ttl: float = 300.0  # seconds a fetched tool list is reused
    pool: bool = True  # keep one httpx2 connection pool per upstream
    max_result_bytes: int = 4 * 1024 * 1024  # largest upstream response body / SSE event accepted
    fail_ttl: float = 15.0  # seconds a failed tools/list is reported without asking the upstream again
    oauth_id: int | None = None  # set: requests carry the OAuth access token the token source holds

    def __post_init__(self) -> None:
        # Refuse what would fail at request time with an error text quoting a secret, which would
        # reach every agent holding the hub token: an unparsable URL (httpx quotes the bad part,
        # possibly the password), or header values h11 rejects (it quotes the whole value).
        try:
            httpx2.URL(self.url)
        except Exception:  # noqa: BLE001
            raise ValueError(f"upstream {self.name!r}: the URL can't be parsed") from None
        for k, v in self.headers.items():
            if not _HEADER_NAME_RE.match(k):
                raise ValueError(f"upstream {self.name!r}: invalid header name {k[:40]!r}")
            if not _HEADER_VALUE_RE.match(v):
                raise ValueError(f"upstream {self.name!r}: header {k!r} has a control or non-ASCII character")


@dataclass
class _State:
    config: Upstream
    tools: list[types.Tool] | None = None
    instructions: str | None = None
    fetched_at: float = 0.0
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    mode: str = "auto"  # 'auto' until the first successful connection, then the era it found
    prior_discover: types.DiscoverResult | None = None
    http: httpx2.AsyncClient | None = None
    truncated: str | None = None  # why the cached tool list is incomplete, if it is
    refreshing: bool = False      # a background refresh of an expired list is running
    failure: tuple[float, str] | None = None  # (monotonic time, reason) of the last failed tools/list


class UpstreamUnavailable(Exception):
    """The hub could not get an answer from the upstream (network, auth, timeout)."""


class TokenUnavailable(Exception):
    """Raised by a token source when it has no usable token; the text is shown to the agent as is
    (for example "sign in again on the hub's MCP page")."""


class TokenSource(Protocol):
    async def token(self, server_id: int, rejected: str | None = None, url: str | None = None) -> str | None:
        """A valid access token. `rejected`: a token the upstream just refused; the source returns a
        different one (refreshing only if the refused one is still current, so concurrent 401s share
        one refresh), or that same token / raises when it has nothing better."""
        ...

    def rejected(self, server_id: int, token: str, url: str | None = None) -> None:
        """The upstream refused `token` although it was freshly obtained."""
        ...


class _OAuthBearer(httpx2.Auth):
    """Puts the upstream's current OAuth access token on each request. On a 401 it asks the token
    source for a different token and retries once; if a fresh token is refused too, the token
    source is told so (it drops that sign-in)."""

    def __init__(self, tokens: TokenSource, server_id: int, url: str) -> None:
        # url: the address this pool's upstream had when it was built. Tokens are only handed out
        # for the address they were issued for, so a call still running against an old address
        # never gets the sign-in made for a new one.
        self.tokens, self.server_id, self.url = tokens, server_id, url

    async def _get(self, rejected: str | None = None) -> str | None:
        try:
            token = await self.tokens.token(self.server_id, rejected=rejected, url=self.url)
        except Exception as exc:  # noqa: BLE001 - reported to the agent through _AUTH_ERRORS
            errors = _AUTH_ERRORS.get()
            if errors is not None:
                errors.append(str(exc) if isinstance(exc, TokenUnavailable) else f"{type(exc).__name__}: {exc}")
            return None
        sent = _SENT_SECRETS.get()
        if token and sent is not None:
            sent.append(token)
        return token

    async def async_auth_flow(self, request: httpx2.Request):
        token = await self._get()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        response = yield request
        if response.status_code != 401 or not token:
            return
        fresh = await self._get(rejected=token)
        if not fresh or fresh == token:
            errors = _AUTH_ERRORS.get()
            if fresh == token and errors is not None:
                errors.append("its authorization server renewed the sign-in with the same, refused token; the hub "
                              "keeps the sign-in: if this keeps happening, sign in again on the hub's MCP page")
            return  # nothing better to try: the 401 stands, and _AUTH_ERRORS says why
        statuses = _HTTP_STATUSES.get()
        if statuses and statuses[-1] == 401:
            statuses.pop()  # retried with a new token: only the retry's answer counts
        request.headers["Authorization"] = f"Bearer {fresh}"
        response = yield request
        if response.status_code == 401:
            self.tokens.rejected(self.server_id, fresh, url=self.url)
            errors = _AUTH_ERRORS.get()
            if errors is not None:
                errors.append("it refused the renewed sign-in too; the hub keeps the sign-in: if this keeps "
                              "happening, sign in again on the hub's MCP page")


def _auth_error(exc: BaseException) -> TokenUnavailable | None:
    """A TokenUnavailable anywhere in an exception (group) or its cause chain."""
    seen: set[int] = set()
    todo = [exc]
    while todo:
        cur = todo.pop()
        if cur is None or id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, TokenUnavailable):
            return cur
        if isinstance(cur, BaseExceptionGroup):
            todo.extend(cur.exceptions)
        todo.extend([cur.__cause__, cur.__context__])
    return None


async def _record_status(response: httpx2.Response) -> None:
    statuses = _HTTP_STATUSES.get()
    if statuses is not None and response.request.method != "GET":  # not the local 405 below
        statuses.append(response.status_code)
    challenges = _CHALLENGES.get()
    if challenges is not None and response.status_code in (401, 403):
        challenges.append(response.headers.get("www-authenticate", ""))


def _trace_start(statuses: list[int]) -> tuple:
    """Per-call bookkeeping read by _reason: statuses, OAuth errors, tokens sent, challenges."""
    return (_HTTP_STATUSES.set(statuses), _AUTH_ERRORS.set([]), _SENT_SECRETS.set([]), _CHALLENGES.set([]))


def _trace_end(tokens: tuple) -> None:
    for var, token in zip((_HTTP_STATUSES, _AUTH_ERRORS, _SENT_SECRETS, _CHALLENGES), tokens):
        var.reset(token)


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


_URL_RE = re.compile(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s'"<>()]+""")


def _secrets(up: Upstream) -> set[str]:
    """Everything that could be a secret: credential headers (and the bare credential after
    'Bearer'/'Basic'/'Token'), every part of the URL that can carry a key (host, user info and the
    Basic header httpx builds from it, query, long path segments such as Zapier-style keys), and
    the OAuth tokens sent during the current call."""
    found = {up.url, *(_SENT_SECRETS.get() or [])}
    for name, value in up.headers.items():
        if _SECRET_HEADER_RE.search(name) or len(value) >= 12:
            found.add(value)
            scheme, _, rest = value.strip().partition(" ")
            if rest and scheme.lower() in ("bearer", "basic", "token", "bot"):
                found.add(rest.strip())
    try:
        u = httpx2.URL(up.url)
        found.add(u.host)
        for key, value in u.params.multi_items():
            if _SECRET_HEADER_RE.search(key) or len(value) >= 12:
                found |= {value, u.query.decode()}
        found |= {seg for seg in u.path.split("/") if len(seg) >= 12}
        if u.userinfo:
            userinfo = u.userinfo.decode()
            user, _, password = userinfo.partition(":")
            found |= {userinfo, user, password, unquote(user), unquote(password),
                      base64.b64encode(userinfo.encode()).decode(),
                      base64.b64encode(f"{unquote(user)}:{unquote(password)}".encode()).decode()}
    except Exception:  # noqa: BLE001 - refused by Upstream; the raw string is still scrubbed
        pass
    return {s for s in found if s and len(s) >= 3}


def _scrub(text: str, up: Upstream, secrets: list[str] | None = None) -> str:
    for s in secrets if secrets is not None else sorted(_secrets(up), key=len, reverse=True):
        if s in text:
            text = text.replace(s, "<redacted>")
    return text


def _scrub_data(data: Any, up: Upstream) -> Any:
    """The upstream's extra error data with secrets scrubbed from every string in it (left out
    when it is large: scrubbing it would cost more than it is worth)."""
    try:
        if len(json.dumps(data, default=str)) > MAX_ERROR_DATA_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    secrets = sorted(_secrets(up), key=len, reverse=True)

    def walk(x: Any) -> Any:
        if isinstance(x, str):
            return _scrub(x, up, secrets)
        if isinstance(x, list):
            return [walk(v) for v in x]
        if isinstance(x, dict):
            return {_scrub(str(k), up, secrets): walk(v) for k, v in x.items()}
        return x
    return walk(data)


def _reason(exc: BaseException, statuses: list[int], up: Upstream, phase: str) -> str:
    """A one-line explanation for the agent. Never contains the upstream URL (it may carry a key),
    nor any other URL (a redirect target can repeat the key in its path)."""
    found = _auth_error(exc)
    text = str(found) if found is not None else _describe(_leaf(exc), statuses, up, phase)
    return _URL_RE.sub("<url>", _scrub(text, up))


def _describe(exc: BaseException, statuses: list[int], up: Upstream, phase: str) -> str:
    bad = [s for s in statuses if s >= 400]
    if _TOO_LARGE in statuses:
        return f"the upstream's answer is larger than the hub's limit of {up.max_result_bytes} bytes ({phase})"
    if bad and bad[-1] in (401, 403):
        if up.oauth_id is not None:
            errors = _AUTH_ERRORS.get() or []
            why = errors[-1] if errors else "sign in again on the hub's MCP page"
            return f"the upstream rejected the hub's sign-in (HTTP {bad[-1]}): {why}"
        challenge = ((_CHALLENGES.get() or [""])[-1:] or [""])[0].lower()
        if challenge.startswith("bearer") and "resource_metadata" in challenge and not up.headers:
            return (f"this server asks for an OAuth sign-in (HTTP {bad[-1]}): edit it on the hub's MCP page, "
                    "choose 'OAuth sign-in, done once on the hub' and press Sign in")
        return f"the upstream rejected the hub's credentials (HTTP {bad[-1]}); check its stored headers on the hub"
    if _is_unfollowed_redirect(exc):
        moved = [s for s in statuses if 300 <= s < 400]
        code = f" (HTTP {moved[-1]})" if moved else ""
        return (f"the upstream answered with a redirect{code} instead of serving MCP at its address; check the "
                f"server's URL on the hub's MCP page ({phase})")
    if isinstance(exc, MCPError) and _is_parse_error(exc):
        return f"the upstream's answer is not valid MCP (it could not be parsed) ({phase})"
    if isinstance(exc, ValidationError):
        return f"the upstream's answer doesn't match the MCP format ({phase})"
    if isinstance(exc, TimeoutError):
        return f"no answer within {up.connect_timeout + up.call_timeout:g}s ({phase})"
    if isinstance(exc, MCPError):
        if exc.code == REQUEST_TIMEOUT:
            return f"no answer within {up.call_timeout:g}s ({phase})"
        http = f", HTTP {bad[-1]}" if bad else ""
        return f"{exc.message} (code {exc.code}{http}, {phase})"
    if isinstance(exc, httpx2.ConnectError):
        return f"cannot connect to the upstream ({phase}): {exc or type(exc).__name__}"
    if isinstance(exc, httpx2.TimeoutException):
        return f"network timeout talking to the upstream ({phase}): {type(exc).__name__}"
    if isinstance(exc, httpx2.HTTPError):
        return f"HTTP error talking to the upstream ({phase}): {type(exc).__name__}: {exc}"
    return f"{type(exc).__name__}: {exc} ({phase})"


def _is_parse_error(exc: MCPError) -> bool:
    """The client transport's own error for an answer it couldn't parse (it quotes a truncated
    piece of the answer, which whole-value scrubbing can't catch)."""
    return exc.code == PARSE_ERROR and exc.message.startswith(
        ("Failed to parse JSON response", "Failed to parse SSE message"))


def _is_unfollowed_redirect(exc: BaseException) -> bool:
    """The client transport's own error for a redirect it would not follow (it names the target).
    A redirect it did follow (same origin) is not an error and says nothing about what failed."""
    return isinstance(exc, MCPError) and exc.message.startswith("Redirect to ") and \
        "not followed" in exc.message.split("\n", 1)[0]


def _passthrough(exc: BaseException, statuses: list[int], up: Upstream) -> MCPError | None:
    """The upstream's own JSON-RPC error (to pass on, with secrets scrubbed), or None for a
    transport failure."""
    if _auth_error(exc) is not None:
        return None
    exc = _leaf(exc)
    if not isinstance(exc, MCPError):
        return None
    if exc.code in (REQUEST_TIMEOUT, CONNECTION_CLOSED) or exc.message in _SYNTHETIC or _is_parse_error(exc) \
            or _is_unfollowed_redirect(exc):
        return None
    if any(s in (401, 403, 407, _TOO_LARGE) or s >= 500 for s in statuses):
        return None
    return MCPError(code=exc.code, message=_scrub(exc.message, up), data=_scrub_data(exc.error.data, up))


def _without_server_stamp(result: types.CallToolResult) -> types.CallToolResult:
    """Drop the upstream's display-only serverInfo stamp from `_meta` (the hub stamps its own on
    2026-07-28 responses); everything else in the result is passed on unchanged."""
    if result.meta and types.SERVER_INFO_META_KEY in result.meta:
        meta = {k: v for k, v in result.meta.items() if k != types.SERVER_INFO_META_KEY}
        result = result.model_copy(update={"meta": meta or None})
    return result


class _NoStandaloneStream(httpx2.AsyncBaseTransport):
    """Answer the client's standalone GET stream with a local 405 instead of opening it.

    After a 2025-era handshake the SDK client opens a GET SSE stream for server-initiated
    messages. A per-call upstream session has no use for it (the hub could not relay sampling
    or elicitation requests anyway), and each one would hold a pooled connection for the
    life of the call: 20 concurrent calls to a 2025 upstream exhausted a 20-connection pool.
    """

    def __init__(self, inner: httpx2.AsyncBaseTransport, max_bytes: int) -> None:
        self.inner, self.max_bytes = inner, max_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        if request.method == "GET":
            return httpx2.Response(405, headers={"Allow": "POST, DELETE"}, request=request)
        response = await self.inner.handle_async_request(request)
        response.stream = _CappedStream(response.stream, self.max_bytes)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


class _CappedStream(httpx2.AsyncByteStream):
    """Stop reading an upstream response body after max_bytes (a huge tool result would
    otherwise be held several times over in the hub's memory)."""

    def __init__(self, inner: Any, max_bytes: int) -> None:
        self.inner, self.max_bytes = inner, max_bytes

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen = 0
        async for chunk in self.inner:
            seen += len(chunk)
            if seen > self.max_bytes:
                statuses = _HTTP_STATUSES.get()
                if statuses is not None:
                    statuses.append(_TOO_LARGE)
                raise httpx2.ReadError(f"response larger than {self.max_bytes} bytes")
            yield chunk

    async def aclose(self) -> None:
        await self.inner.aclose()


class _CappedTransport(httpx2.AsyncBaseTransport):
    def __init__(self, max_bytes: int) -> None:
        self.inner, self.max_bytes = httpx2.AsyncHTTPTransport(), max_bytes

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        response = await self.inner.handle_async_request(request)
        response.stream = _CappedStream(response.stream, self.max_bytes)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def capped_transport(max_bytes: int) -> httpx2.AsyncBaseTransport:
    """An httpx2 transport that stops reading any response body after max_bytes."""
    return _CappedTransport(max_bytes)


class _AsgiEndpoint:
    def __init__(self, handler: Callable[[Scope, Receive, Send], Any]) -> None:
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.handler(scope, receive, send)


class McpProxy:
    """Serves every configured upstream at <prefix>/{name} through one low-level MCP server."""

    def __init__(self, get_upstreams: Callable[[], Mapping[str, Upstream]], *, path: str = "/mcp/s/{name}",
                 json_response: bool = False, tokens: TokenSource | None = None) -> None:
        self.get_upstreams = get_upstreams
        self.path = path
        self.tokens = tokens  # for upstreams with oauth_id
        self._states: dict[str, _State] = {}
        self._retired: list[httpx2.AsyncClient] = []  # replaced pools, closed a while later (or at shutdown)
        self._tg: anyio.abc.TaskGroup | None = None  # background tool-list fetches (set by run())
        self.server: Server[Any] = Server(
            "bullpen-proxy",
            version="0.1",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            # Mcp-Param-* header validation (2026-07-28) would otherwise run the tools/list
            # handler before every tools/call; answer it from the cache instead.
            get_tool_input_schema=self._cached_input_schema,
        )
        self.server.middleware.append(self._pass_instructions)
        self.manager = StreamableHTTPSessionManager(
            app=self.server,
            # False = SSE: a 2026-07-28 call silent for 15s switches to SSE with keepalive pings, which
            # keeps a long upstream call alive behind Cloudflare's 100s idle-response limit.
            json_response=json_response,
            stateless=True,
            # The hub checks its bearer token itself; the Host check would block LAN/Cloudflare names.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    # ------------------------------------------------------------- ASGI side
    def routes(self) -> list[Route]:
        # Starlette wraps functions and bound methods as request/response endpoints; only a
        # non-function callable is used as a raw ASGI app, hence the small wrapper object.
        # The trailing-slash twin is served directly: a redirect would not be followed for a POST.
        endpoint = _AsgiEndpoint(self._endpoint)
        return [Route(path, endpoint=endpoint, methods=["GET", "POST", "DELETE"])
                for path in (self.path, self.path.rstrip("/") + "/")]

    async def _endpoint(self, scope: Scope, receive: Receive, send: Send) -> None:
        name = scope.get("path_params", {}).get("name", "")
        if not NAME_RE.match(name) or name not in self.get_upstreams():
            await JSONResponse({"error": "unknown_mcp_server", "name": name[:64]}, status_code=404)(
                scope, receive, send)
            return
        scope = {**scope, SCOPE_KEY: name}
        token = UPSTREAM.set(name)
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            UPSTREAM.reset(token)

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Enter from the app's lifespan (a mounted app's own lifespan never runs)."""
        try:
            async with self.manager.run(), anyio.create_task_group() as tg:
                self._tg = tg
                try:
                    yield
                finally:
                    tg.cancel_scope.cancel()
        finally:
            self._tg = None
            for http in self._retired + [s.http for s in self._states.values() if s.http is not None]:
                await http.aclose()

    def _retire(self, state: _State) -> None:
        """A replaced connection pool: calls already running may still use it, so close it once
        the longest of them must have ended (or at shutdown)."""
        http = state.http  # left on the old state: a call that already holds it keeps a working pool
        if http is None or http in self._retired:
            return
        self._retired.append(http)
        if self._tg is not None:
            self._tg.start_soon(self._close_later, http, state.config.connect_timeout + state.config.call_timeout + 10)

    async def _close_later(self, http: httpx2.AsyncClient, delay: float) -> None:
        await anyio.sleep(delay)
        if http in self._retired:
            self._retired.remove(http)
            await http.aclose()

    # --------------------------------------------------------- upstream side
    def _upstream_for(self, ctx: Any) -> tuple[Upstream, _State]:
        request = getattr(ctx, "request", None)
        name = getattr(request, "scope", {}).get(SCOPE_KEY) if request is not None else None
        name = name or UPSTREAM.get()
        upstream = self.get_upstreams().get(name or "")
        if upstream is None:
            raise MCPError(code=INVALID_REQUEST, message="no such MCP server on this hub")
        return upstream, self._state(upstream)

    def _state(self, upstream: Upstream) -> _State:
        state = self._states.get(upstream.name)
        if state is None or state.config != upstream:  # new or reconfigured: start over
            if state is not None and state.http is not None:
                self._retire(state)
            state = self._states[upstream.name] = _State(config=upstream)
        return state

    def _new_http(self, up: Upstream) -> httpx2.AsyncClient:
        limits = httpx2.Limits(max_connections=32, max_keepalive_connections=8, keepalive_expiry=60)
        auth = _OAuthBearer(self.tokens, up.oauth_id, up.url) if up.oauth_id is not None and self.tokens else None
        return httpx2.AsyncClient(
            headers=dict(up.headers),
            auth=auth,
            timeout=httpx2.Timeout(connect=up.connect_timeout, read=up.call_timeout + 5, write=10, pool=10),
            transport=_NoStandaloneStream(httpx2.AsyncHTTPTransport(limits=limits), up.max_result_bytes),
            event_hooks={"response": [_record_status]},
        )

    async def _check_token(self, up: Upstream) -> None:
        """Fail fast, with the token source's own words, when an OAuth upstream has no usable
        sign-in (rather than sending a request that can only be refused)."""
        if up.oauth_id is not None:
            if self.tokens is None:
                raise TokenUnavailable("this hub has no OAuth support configured")
            token = await self.tokens.token(up.oauth_id, url=up.url)
            sent = _SENT_SECRETS.get()
            if token and sent is not None:
                sent.append(token)

    @asynccontextmanager
    async def _connect(self, up: Upstream, st: _State) -> AsyncIterator[Client]:
        async with AsyncExitStack() as stack:
            if up.pool and (st.http is not None or self._states.get(up.name) is st):
                if st.http is None:
                    st.http = self._new_http(up)
                http = st.http
            else:
                http = await stack.enter_async_context(self._new_http(up))
            client = Client(
                streamable_http_client(up.url, http_client=http, max_sse_event_size=up.max_result_bytes),
                mode=st.mode,
                prior_discover=st.prior_discover,
                read_timeout_seconds=up.call_timeout,
                cache=None,
                client_info=CLIENT_INFO,
            )
            try:
                await stack.enter_async_context(client)
            except Exception:
                st.mode, st.prior_discover = "auto", None  # negotiate afresh next time
                raise
            if st.mode == "auto":
                if client.protocol_version in MODERN_PROTOCOL_VERSIONS:
                    st.mode, st.prior_discover = client.protocol_version, client.session.discover_result
                else:
                    st.mode = "legacy"
                log.info("upstream %s speaks %s", up.name, client.protocol_version)
            yield client

    async def _fetch_tools(self, up: Upstream, st: _State) -> None:
        statuses: list[int] = []
        tokens = _trace_start(statuses)
        try:
            with anyio.fail_after(up.connect_timeout + up.call_timeout):
                await self._check_token(up)
                async with self._connect(up, st) as client:
                    tools: list[types.Tool] = []
                    cursor: str | None = None
                    size, truncated = 0, None
                    for _ in range(LIST_PAGE_CAP):
                        page = await client.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
                        for tool in page.tools:
                            size += len(tool.model_dump_json())
                            if len(tools) >= MAX_TOOLS or size > MAX_TOOL_LIST_BYTES:
                                truncated = f"only the first {len(tools)} tools are offered (the hub's limit)"
                                break
                            tools.append(tool)
                        cursor = page.next_cursor
                        if cursor is None or truncated:
                            break
                    else:
                        truncated = f"only the first {LIST_PAGE_CAP} pages of tools are offered (the hub's limit)"
                    if truncated:
                        log.warning("upstream %s: %s", up.name, truncated)
                    st.tools, st.instructions, st.fetched_at = tools, client.instructions, time.monotonic()
                    st.truncated = truncated
        except Exception as exc:  # noqa: BLE001 - every failure becomes a reason
            st.mode, st.prior_discover = "auto", None  # the upstream may have changed: renegotiate
            raise UpstreamUnavailable(_reason(exc, statuses, up, "tools/list")) from exc
        finally:
            _trace_end(tokens)

    async def _tools(self, up: Upstream, st: _State) -> list[types.Tool]:
        if st.tools is not None and time.monotonic() - st.fetched_at < up.list_ttl:
            return st.tools
        if st.tools is not None and self._tg is not None:
            # Expired: answer from the cache at once and refresh it in the background, so a slow or
            # hanging upstream doesn't hold every agent's tools/list.
            if not st.refreshing:
                st.refreshing = True

                async def refresh() -> None:
                    try:
                        await self._refresh_tools(up, st)
                    except UpstreamUnavailable:
                        pass  # kept in st.failure; the cached list stays
                    finally:
                        st.refreshing = False
                self._tg.start_soon(refresh)
            return st.tools
        return await self._refresh_tools(up, st)

    async def _refresh_tools(self, up: Upstream, st: _State) -> list[types.Tool]:
        async with st.lock:  # single flight: concurrent cold requests share one fetch
            if st.tools is not None and time.monotonic() - st.fetched_at < up.list_ttl:
                return st.tools
            if st.tools is None and st.failure and time.monotonic() - st.failure[0] < up.fail_ttl:
                raise UpstreamUnavailable(st.failure[1])  # waiters behind a failed fetch share its result
            try:
                await self._fetch_tools(up, st)
                st.failure = None
            except UpstreamUnavailable as err:
                st.failure = (time.monotonic(), str(err))
                if st.tools is None:
                    raise
                log.warning("upstream %s: refresh failed, serving the cached tool list: %s", up.name, err)
                st.fetched_at = time.monotonic() - up.list_ttl + 30  # retry the refresh in 30s
            return st.tools or []

    @staticmethod
    def _needs_param_headers(st: _State, name: str) -> bool:
        return any(t.name == name and x_mcp_header_map(t.input_schema) for t in st.tools or [])

    @staticmethod
    async def _list_until(client: Client, name: str) -> None:
        """List tools page by page until `name` has been seen: a fresh session mirrors
        x-mcp-header arguments into Mcp-Param-* headers only for tools it has listed."""
        cursor: str | None = None
        size = 0
        for _ in range(LIST_PAGE_CAP):
            page = await client.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            cursor = page.next_cursor
            size += sum(len(t.model_dump_json()) for t in page.tools)
            if cursor is None or any(t.name == name for t in page.tools) or size > MAX_TOOL_LIST_BYTES:
                return

    def _cached_input_schema(self, name: str) -> Mapping[str, Any] | None:
        st = self._states.get(UPSTREAM.get() or "")
        for tool in (st.tools if st is not None and st.tools else []):
            if tool.name == name:
                return tool.input_schema
        return None  # unknown here: the SDK skips Mcp-Param validation (fail-open)

    # ---------------------------------------------------------- MCP handlers
    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        up, st = self._upstream_for(ctx)
        try:
            tools = await self._tools(up, st)
        except UpstreamUnavailable as err:
            raise MCPError(code=INTERNAL_ERROR, message=f"hub: MCP server {up.name!r} is unavailable: {err}") from None
        return types.ListToolsResult(tools=tools)

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        up, st = self._upstream_for(ctx)
        request = types.CallToolRequest(params=types.CallToolRequestParams(name=params.name, arguments=params.arguments))
        statuses: list[int] = []
        tokens = _trace_start(statuses)
        started = time.monotonic()
        try:
            with anyio.fail_after(up.connect_timeout + up.call_timeout):
                await self._check_token(up)
                async with self._connect(up, st) as client:
                    if client.protocol_version in MODERN_PROTOCOL_VERSIONS and self._needs_param_headers(st, params.name):
                        # A fresh session mirrors x-mcp-header arguments into Mcp-Param-* headers only for
                        # tools it has listed; list first rather than eat a HEADER_MISMATCH round trip.
                        await self._list_until(client, params.name)
                    try:
                        # send_request, not call_tool: Client.call_tool would re-list the tools on every
                        # fresh connection to validate structuredContent (a second upstream round trip),
                        # and the agent's own client validates against the same outputSchema anyway.
                        result = await client.session.send_request(
                            request, types.CallToolResult, request_read_timeout_seconds=up.call_timeout)
                    except MCPError as err:
                        if err.code != types.HEADER_MISMATCH:
                            raise
                        # The tool declares x-mcp-header params: list so the client mirrors them, retry.
                        await self._list_until(client, params.name)
                        result = await client.session.send_request(
                            request, types.CallToolResult, request_read_timeout_seconds=up.call_timeout)
            return _without_server_stamp(result)
        except Exception as exc:  # noqa: BLE001
            own = _passthrough(exc, statuses, up)
            if own is not None:
                raise own from None  # the upstream's own JSON-RPC error, unchanged
            st.mode, st.prior_discover = "auto", None  # the upstream may have changed: renegotiate
            reason = _reason(exc, statuses, up, "tools/call")
            log.warning("upstream %s tool %s failed after %.2fs: %s", up.name, params.name,
                        time.monotonic() - started, reason)
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=f"hub: MCP server {up.name!r} failed: {reason}")],
                is_error=True,
            )
        finally:
            _trace_end(tokens)

    async def _pass_instructions(self, ctx: Any, call_next: Any) -> Any:
        """Hand the upstream's `instructions` to the agent on initialize / server/discover.

        Fetching them also warms the tool cache the agent's tools/list will use next. The agent
        waits at most INSTRUCTIONS_WAIT for them; the fetch carries on in the background, and an
        unreachable upstream never makes connecting to the hub fail or hang.
        """
        result = await call_next(ctx)
        if ctx.method not in ("initialize", "server/discover") or not isinstance(result, dict):
            return result
        up, st = self._upstream_for(ctx)
        if st.tools is None and self._tg is not None:
            done = anyio.Event()

            async def warm() -> None:
                try:
                    await self._tools(up, st)
                except UpstreamUnavailable:
                    pass  # tools/list will report it
                finally:
                    done.set()

            self._tg.start_soon(warm)
            with anyio.move_on_after(INSTRUCTIONS_WAIT):
                await done.wait()
        if st.instructions:
            result = {**result, "instructions": st.instructions}
        return result

    # ---------------------------------------------------------- dashboard
    def forget(self, name: str) -> None:
        """Drop what is cached for one upstream (after a sign-in or sign-out): the next request
        asks the upstream again."""
        state = self._states.pop(name, None)
        if state is not None and state.http is not None:
            self._retire(state)

    def health(self, name: str) -> dict[str, Any] | None:
        """What the hub last saw of an upstream, for the dashboard (None: not contacted yet)."""
        st = self._states.get(name)
        if st is None or (st.tools is None and st.failure is None):
            return None
        now = time.monotonic()
        return {"tools": len(st.tools) if st.tools is not None else None,
                "checked_ago": round(now - max(st.fetched_at, st.failure[0] if st.failure else 0)),
                "error": st.failure[1] if st.failure else None,
                "error_ago": round(now - st.failure[0]) if st.failure else None,
                "note": st.truncated}

    async def test(self, up: Upstream) -> list[types.Tool]:
        """Fetch the tool list now. For a served upstream this also refreshes what agents get;
        any other upstream (e.g. one machines reach directly) is checked on a throwaway connection.
        Raises UpstreamUnavailable with a readable reason."""
        served = self.get_upstreams().get(up.name)
        if served == up:
            st = self._state(up)
            async with st.lock:
                # A failed test keeps the cached list (agents keep working); a good one replaces it.
                try:
                    await self._fetch_tools(up, st)
                    st.failure = None
                except UpstreamUnavailable as err:
                    st.failure = (time.monotonic(), str(err))
                    raise
                return st.tools or []
        once = replace(up, pool=False)
        st = _State(config=once)
        await self._fetch_tools(once, st)
        return st.tools or []
