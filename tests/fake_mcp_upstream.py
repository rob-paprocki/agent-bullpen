"""A fake upstream MCP server protected by OAuth 2.1 (or an API key), built from the SDK's
server-side auth helpers.

It stands in for a real OAuth MCP server (Notion, Linear, Atlassian, ...) so the hub's
"sign in once on the hub" flow can be tested without a browser.

  * in-memory OAuthAuthorizationServerProvider: dynamic client registration (RFC 7591),
    authorize (auto-approves: redirects straight back with a code, plus RFC 9207 `iss`),
    code exchange with PKCE (checked by the SDK's TokenHandler), refresh with rotation
  * short access-token lifetime (default 20 s) so refresh can be exercised
  * tools: whoami() -> the token's client_id / subject / scopes;  slow(seconds) for concurrency tests
  * test-only admin routes (no auth!): /admin/stats, /admin/revoke_all, /admin/expire_access

Modes
  default      authorization server and MCP resource server on one origin (http://127.0.0.1:PORT)
  --as-port N  split: AS on http://127.0.0.1:N, MCP resource server on PORT (like many real providers)
  --no-dcr     registration endpoint disabled (provider "without DCR": use --preregister)
  --preregister CLIENT_ID:SECRET:AUTH_METHOD:REDIRECT_URI   a manually created OAuth app
  --sse        answer POSTs with SSE instead of JSON (most real servers do)
  --api-key K  no OAuth: every /mcp request needs the header `X-Api-Key: K` (like GitHub's PAT setup)
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import time
from collections import Counter

import uvicorn
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    ProviderTokenVerifier,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.routes import create_auth_routes
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPES = ["read"]


class MemoryAuthServer:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider (structural Protocol)."""

    def __init__(self, issuer: str, access_ttl: int, refresh_ttl: int | None = None):
        self.issuer = issuer
        self.access_ttl = access_ttl
        self.refresh_ttl = refresh_ttl
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.stats: Counter[str] = Counter(dict.fromkeys(("registrations", "authorizations", "code_exchanges", "refreshes"), 0))
        self.fail_token_requests = 0  # > 0: the next N POST /token answer 503 (AS outage)

    # --- clients
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.clients[client_info.client_id] = client_info
        self.stats["registrations"] += 1

    # --- authorization code grant
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # A real provider shows a login + consent page here. We auto-approve.
        self.stats["authorizations"] += 1
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject="user-test",
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state, iss=self.issuer)

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        code = self.codes.get(authorization_code)
        return code if code and code.client_id == client.client_id else None

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode) -> OAuthToken:
        self.codes.pop(authorization_code.code, None)  # single use
        self.stats["code_exchanges"] += 1
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource, authorization_code.subject)

    # --- refresh
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        token = self.refresh.get(refresh_token)
        return token if token and token.client_id == client.client_id else None

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        self.refresh.pop(refresh_token.token, None)  # rotate: the old refresh token dies now
        self.stats["refreshes"] += 1
        return self._issue(client.client_id, scopes, refresh_token.resource, refresh_token.subject)

    # --- access tokens
    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.access.get(token)
        if found and found.expires_at and found.expires_at < time.time():
            return None
        return found

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, subject: str | None) -> OAuthToken:
        access, refresh = "at_" + secrets.token_urlsafe(24), "rt_" + secrets.token_urlsafe(24)
        now = int(time.time())
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=now + self.access_ttl, resource=resource, subject=subject)
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                                             expires_at=now + self.refresh_ttl if self.refresh_ttl else None,
                                             resource=resource, subject=subject)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=self.access_ttl,
                          refresh_token=refresh, scope=" ".join(scopes))


def admin_routes(provider: MemoryAuthServer) -> list[Route]:
    async def stats(_: Request):
        return JSONResponse({**provider.stats, "clients": sorted(provider.clients),
                             "live_access_tokens": len(provider.access), "live_refresh_tokens": len(provider.refresh)})

    async def revoke_all(_: Request):
        n = len(provider.access) + len(provider.refresh)
        provider.access.clear()
        provider.refresh.clear()
        return JSONResponse({"revoked": n})

    async def expire_access(_: Request):
        # Forget access tokens but keep refresh tokens: the next call gets a 401 for a token the
        # client still believes is valid (what happens after a provider-side revocation or restart).
        n = len(provider.access)
        provider.access.clear()
        return JSONResponse({"expired": n})

    async def fail_token(request: Request):
        provider.fail_token_requests = int(request.query_params.get("n", "1"))
        return JSONResponse({"fail_token_requests": provider.fail_token_requests})

    return [Route("/admin/stats", stats, methods=["GET"]),
            Route("/admin/fail_token", fail_token, methods=["POST"]),
            Route("/admin/revoke_all", revoke_all, methods=["POST"]),
            Route("/admin/expire_access", expire_access, methods=["POST"])]


class TokenOutage:
    """ASGI wrapper: makes POST /token return 503 while provider.fail_token_requests > 0."""

    def __init__(self, app, provider: MemoryAuthServer):
        self.app, self.provider = app, provider

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/token" and self.provider.fail_token_requests > 0:
            self.provider.fail_token_requests -= 1
            await JSONResponse({"error": "temporarily_unavailable"}, status_code=503)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ApiKeyGate:
    """ASGI wrapper: /mcp needs `X-Api-Key: <key>`."""

    def __init__(self, app, key: str):
        self.app, self.key = app, key

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/mcp") and \
                Headers(scope=scope).get("x-api-key") != self.key:
            await JSONResponse({"error": "bad api key"}, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def build_api_key(key: str, sse: bool):
    server = MCPServer("fake-keyed-upstream", instructions="Use echo to repeat things.", log_level="WARNING")

    @server.tool()
    async def echo(text: str) -> dict[str, str]:
        """Repeat the text back."""
        return {"echo": text}

    @server.tool()
    async def boom() -> str:
        """Always fails, as a tool error."""
        raise ValueError("boom on purpose")

    app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=not sse, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return ApiKeyGate(app, key), None


def build(port: int, as_port: int | None, access_ttl: int, dcr: bool, preregister: list[str], sse: bool):
    rs_base = f"http://127.0.0.1:{port}"
    issuer = f"http://127.0.0.1:{as_port}" if as_port else rs_base
    provider = MemoryAuthServer(issuer, access_ttl)

    for spec in preregister:
        client_id, secret, method, redirect = spec.split(":", 3)
        provider.clients[client_id] = OAuthClientInformationFull(
            client_id=client_id, client_secret=secret or None, token_endpoint_auth_method=method,
            redirect_uris=[redirect], grant_types=["authorization_code", "refresh_token"],
            response_types=["code"], scope=" ".join(SCOPES), client_name="manually created app")

    registration = ClientRegistrationOptions(enabled=dcr, valid_scopes=SCOPES, default_scopes=SCOPES)
    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=f"{rs_base}/mcp",
        required_scopes=SCOPES,
        client_registration_options=registration,
        revocation_options=RevocationOptions(enabled=True),
        validate_token_resource=True,  # tokens must carry resource=<this /mcp URL> (RFC 8707)
    )
    if as_port:  # resource server only; tokens are checked against the same in-memory store
        server = MCPServer("fake-upstream", token_verifier=ProviderTokenVerifier(provider), auth=settings,
                           log_level="WARNING")
    else:
        server = MCPServer("fake-upstream", auth_server_provider=provider, auth=settings, log_level="WARNING")

    @server.tool()
    async def whoami() -> dict:
        """Who the upstream thinks is calling: the OAuth client and user behind the bearer token."""
        token = get_access_token()
        if token is None:
            return {"error": "no token in context"}
        return {"client_id": token.client_id, "subject": token.subject, "scopes": token.scopes,
                "resource": token.resource, "expires_in": (token.expires_at or 0) - int(time.time())}

    @server.tool()
    async def slow(seconds: float = 1.0) -> dict:
        """Sleep, then answer (for concurrency tests)."""
        await asyncio.sleep(min(seconds, 10))
        return {"slept": seconds, "client_id": (get_access_token() or AccessToken(token="", client_id="?", scopes=[])).client_id}

    if not as_port:
        for route in admin_routes(provider):
            server.custom_route(route.path, methods=sorted(route.methods - {"HEAD"}))(route.endpoint)

    rs_app = server.streamable_http_app(
        streamable_http_path="/mcp", json_response=not sse, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    as_app = None
    if as_port:
        as_app = Starlette(routes=[
            *create_auth_routes(provider, issuer_url=settings.issuer_url,
                                client_registration_options=registration,
                                revocation_options=settings.revocation_options),
            *admin_routes(provider)])
    if as_app is not None:
        return rs_app, TokenOutage(as_app, provider)
    return TokenOutage(rs_app, provider), None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--as-port", type=int)
    ap.add_argument("--access-ttl", type=int, default=20)
    ap.add_argument("--no-dcr", action="store_true")
    ap.add_argument("--preregister", action="append", default=[])
    ap.add_argument("--sse", action="store_true")
    ap.add_argument("--api-key")
    args = ap.parse_args()
    if args.api_key:
        rs_app, as_app = build_api_key(args.api_key, args.sse)
    else:
        rs_app, as_app = build(args.port, args.as_port, args.access_ttl, not args.no_dcr, args.preregister, args.sse)
    servers = [uvicorn.Server(uvicorn.Config(rs_app, host="127.0.0.1", port=args.port, log_level="warning"))]
    if as_app is not None:
        servers.append(uvicorn.Server(uvicorn.Config(as_app, host="127.0.0.1", port=args.as_port, log_level="warning")))
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
