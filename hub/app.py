"""Agent Bullpen: one dashboard for every agent on every machine.

Run it with:   uvicorn app:app --host 0.0.0.0 --port 8080
(The install script sets this up as a systemd service for you.)

How it fits together
--------------------
* Each machine runs runner/runner.py. The runner checks in ("heartbeat"),
  then asks the hub for work ("poll"). The hub never connects *to* a machine,
  so nothing on the Windows boxes has to be exposed to the network.
* You use the web page (static/index.html). It talks to the /api/... routes
  below, which are protected by a password cookie. Put Cloudflare Access in
  front of it as well when it is reachable from the internet.
* A "council" (debate or build+review) is a parent job that council.py walks
  through several stages of ordinary jobs.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import secrets
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from approvals import Approvals, Refused as ApprovalRefused
from common import ACCOUNT_RE, LOST_JOB_GRACE, MODEL_RE, NAME_RE, ONLINE_WINDOW, TAG_RE, Autonomy, TargetError, \
    accepted_of, account_key, agent_features, effective_tags, job_tools_for, own_token, permission_flags, public_job, \
    reported_tags, runner_projects, tag_job_fits, tag_target, takes_subtasks, to_accept, valid_account_key, vtuple
from council import MAX_DEBATE_PARTICIPANTS, MAX_REVIEWERS, CouncilEngine
from db import Database
from delegation import Delegation
import delegation
from memory import Memory
from notify import Notifier
from registry import Registry
import credits
import limits
import mcp_server
import usage

VERSION = "1.0.0"
HERE = Path(__file__).resolve().parent
log = logging.getLogger("hub")

# ----------------------------------------------------------------- settings
DATA_DIR = Path(os.environ.get("HUB_DATA_DIR", HERE / "data"))
UI_PASSWORD = os.environ.get("HUB_UI_PASSWORD", "")
RUNNER_TOKEN = os.environ.get("HUB_RUNNER_TOKEN", "")
SECRET = (os.environ.get("HUB_SECRET") or secrets.token_hex(32)).encode()
SESSION_DAYS = int(os.environ.get("HUB_SESSION_DAYS", "30"))
# ONLINE_WINDOW (a runner counts as online) and LOST_JOB_GRACE (a job it stopped reporting is
# lost) live in common.py: the job tokens in db.py use them too.
LONG_POLL_SECONDS = 25      # how long a runner's poll waits for work before returning empty
COOKIE = "hub_session"
# The address people and OAuth providers use to reach the hub (e.g. https://hub.example.com).
# Used for the OAuth callback; when unset, the address the dashboard was opened at is used.
PUBLIC_URL_PROBLEM = ""


def _public_url(raw: str) -> str:
    """HUB_PUBLIC_URL as scheme://host[:port], or "" (with PUBLIC_URL_PROBLEM saying why)."""
    global PUBLIC_URL_PROBLEM
    raw = raw.strip().rstrip("/")
    if not raw:
        return ""
    from urllib.parse import urlsplit
    parts = urlsplit(raw)
    try:
        port = parts.port
    except ValueError:
        port = -1
    host_ok = bool(parts.hostname) and (re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*",
                                                      parts.hostname) is not None or ":" in parts.hostname)
    if parts.scheme.lower() not in ("http", "https") or not host_ok or port == -1 \
            or parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
        PUBLIC_URL_PROBLEM = (f"HUB_PUBLIC_URL={raw!r} is not an address like https://hub.example.com "
                              "(no path); it is ignored")
        logging.getLogger("hub").error(PUBLIC_URL_PROBLEM)
        return ""
    scheme, host = parts.scheme.lower(), parts.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    if port == {"http": 80, "https": 443}[scheme]:
        port = None
    return f"{scheme}://{host}{f':{port}' if port else ''}"


PUBLIC_URL = _public_url(os.environ.get("HUB_PUBLIC_URL", ""))
# Notifications when a job finishes, fails or waits for an approval (HUB_NOTIFY_*, hub/notify.py).
notifier = Notifier.from_env(os.environ, PUBLIC_URL)
PROXY_TIMEOUT = float(os.environ.get("HUB_MCP_PROXY_TIMEOUT", "120"))  # one tool call through the hub
SIGNIN_TIMEOUT = float(os.environ.get("HUB_OAUTH_SIGNIN_TIMEOUT", "600"))
ORIGIN_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


def same_origin(a: str, b: str) -> bool:
    """https://Hub.example.com:443 and https://hub.example.com are the same place."""
    def norm(u: str) -> tuple:
        from urllib.parse import urlsplit
        parts = urlsplit(u.strip())
        default = {"http": 80, "https": 443}.get(parts.scheme.lower())
        try:
            port = parts.port
        except ValueError:
            return (u,)
        return parts.scheme.lower(), (parts.hostname or "").lower(), None if port == default else port
    return norm(a) == norm(b)
# MODEL_RE and NAME_RE (model, machine, agent and project names) live in common.py.

# The runner code the hub hands out for one-click updates. The installer copies
# runner/*.py next to the hub; in a development checkout it's ../runner.
RUNNER_DIST = Path(os.environ.get("HUB_RUNNER_DIST") or (
    HERE / "runner_dist" if (HERE / "runner_dist").is_dir() else HERE.parent / "runner"))

db = Database(DATA_DIR / "hub.db")


def council_finished(council_id: int, status: str) -> None:
    """A council finished (done or error): tell the owner, if notifications are on."""
    notifier.send(status, db.job(council_id))


def top_level_task(job: dict) -> bool:
    """The owner's own task: not a council step, not a subtask, not a machine action."""
    return job.get("kind") == "task" and not job.get("parent_id") and not job.get("delegated_by")


# ---------------------------------------------------------- platform credits
# hub/credits.py has the rules. The settings live in the database ('credits'), and so do the
# organizations the API refused for lack of credit ('credits_refused': {org: {"at", "until", "job"}}).
def credit_settings() -> dict:
    return credits.clean_settings(db.setting("credits"))


def credit_refused() -> dict:
    raw = db.setting("credits_refused")
    return raw if isinstance(raw, dict) else {}


def credit_views(cost: dict | None = None) -> list[dict]:
    """Every organization's credits, from the spend figures given, or the last ones fetched (never
    fetching: machines waiting for work call this)."""
    cost = cost if cost is not None else usage.cached()
    orgs = (cost or {}).get("orgs") if isinstance(cost, dict) else None
    return credits.org_views(usage.org_names(), orgs, credit_settings(), credit_refused(), time.time())


def credit_states(views: list[dict] | None = None) -> dict[str, str]:
    return {v["name"]: v["state"] for v in (views if views is not None else credit_views())}


def credit_machines(views: list[dict]) -> list[dict]:
    """Each machine that names a credits agent: the agent, its organization and that organization's
    state, or why it can't be used."""
    states = credit_states(views)
    out = []
    for r in db.runners():
        agent, org, why = credits.credits_agent(r, usage.org_names())
        if agent is None:
            continue
        out.append({"runner": r["name"], "agent": agent, "org": org, "state": states.get(org) if org else None,
                    "problem": why})
    return out


def mark_credits_refused(job: dict) -> str | None:
    """The API refused a job for lack of credit: its agent's organization counts as exhausted until
    its next reset (a day without a reset day). Returns the organization, if the agent is linked to one."""
    org = credits.agent_org(db.runner(job.get("runner") or ""), job.get("agent") or "", usage.org_names())
    if org is None:
        return None
    now = time.time()
    with db.lock:
        refused = credit_refused()
        until = credits.exhausted_until(credits.org_setting(credit_settings(), org)["reset_day"], now)
        refused[org] = {"at": now, "until": until, "job": job["id"]}
        db.set_setting("credits_refused", {k: v for k, v in refused.items()
                                           if isinstance(v, dict) and (v.get("until") or 0) > now})
    log.info("job %s: the API refused %s for lack of credit", job["id"], org)
    return org


def retry_on_credits(job: dict, by_hand: bool = False) -> int:
    """Queue a task the owner started, which stopped at a subscription usage limit, once more on its
    machine's credits agent (credits.retry_target has the rules). Raises HTTPException with why not."""
    with db.lock:
        job = db.job(job["id"]) or job
        if db.query_one("SELECT id FROM jobs WHERE retry_of=?", (job["id"],)) is not None:
            raise HTTPException(409, "This job has already been retried on credits.")
        if job["status"] != "error":
            raise HTTPException(409, "Only a job that stopped with an error is retried.")
        agent, org, why = credits.retry_target(job, db.runner(job.get("runner") or ""), usage.org_names(),
                                               credit_states(), by_hand=by_hand)
        if agent is None:
            raise HTTPException(400, f"It can't be retried on platform credits: {why}.")
        opts = job["options"] if isinstance(job.get("options"), dict) else {}
        keep = {k: opts[k] for k in ("timeout_min", "capture_diff", "needs", "title_typed") if k in opts}
        new_id = db.create_job(
            kind="task", title=job["title"], runner=job["runner"], agent=agent, project=job["project"],
            autonomy=job["autonomy"], prompt=job["prompt"], model=job.get("model"), retry_of=job["id"],
            options={**keep, "no_credits": True},
            credits={"org": org, "how": "retry", "agent": agent, "named_agent": job["agent"], "from": job["id"],
                     "by_hand": by_hand})
    db.append_logs(job["id"], [f"↻ Retried on platform credits ({agent}, {org}) as job #{new_id}"])
    log.info("job %s stopped at a usage limit; retried on credits as %s (%s)", job["id"], new_id, agent)
    return new_id


councils = CouncilEngine(db, on_finish=council_finished)
memory = Memory(db)
_upstreams: dict | None = None  # the servers the hub proxies, rebuilt after every registry change


def registry_changed() -> None:
    global _upstreams
    _upstreams = None
    memory.bump()


registry = Registry(db, on_change=registry_changed)

# Agent add-ons (skills, Gemini commands) the runners install for every agent.
KIT_DIR = Path(os.environ.get("HUB_AGENT_KIT") or (
    HERE / "agent_kit" if (HERE / "agent_kit").is_dir() else HERE.parent / "agent-kit"))


def load_kit() -> dict[str, str]:
    files = {}
    if KIT_DIR.is_dir():
        for path in sorted(KIT_DIR.rglob("*")):
            if path.is_file() and not any(part.startswith(".") for part in path.relative_to(KIT_DIR).parts):
                files[path.relative_to(KIT_DIR).as_posix()] = path.read_text(encoding="utf-8")
    return files


KIT = load_kit()
KIT_DIGEST = hashlib.sha256(repr(sorted(KIT.items())).encode()).hexdigest()[:10]


def mcp_token() -> str:
    """The bearer token agents use for the hub's /mcp endpoint (created on first use)."""
    token = os.environ.get("HUB_MCP_TOKEN") or memory.get_setting("mcp_token")
    if not token:
        token = secrets.token_urlsafe(32)
        memory.set_setting("mcp_token", token)
    return token


WIRING = "w2"


def context_token() -> str:
    """Changes whenever runners should re-sync: notes, MCP list, add-ons, the MCP token,
    or whether the hub's MCP endpoint is available."""
    tok = hashlib.sha256(mcp_token().encode()).hexdigest()[:6]
    # WIRING: bump when what runners get changes shape, so every runner re-syncs after a hub update
    # (w2: servers relayed by the hub, only for runners that can wire them).
    return f"{memory.context_version()}-{KIT_DIGEST}-{tok}-{'m' if hub_mcp is not None else 'x'}-{WIRING}"


hub_mcp, hub_mcp_app = mcp_server.build(memory)
ACCESS = None

# MCP servers routed through the hub (/mcp/s/<name>) and their one-time OAuth sign-ins, and the
# job tools (/mcp/job), which are built at the end of this file once everything they use exists.
proxy = oauth = None
job_mcp = None
hub_job_mcp = None
if hub_mcp is not None:
    try:
        import job_mcp
    except ImportError as err:
        log.warning("job tools (/mcp/job) disabled: %s", err)
    try:
        import mcp_proxy
        import upstream_oauth
    except ImportError as err:
        log.warning("MCP proxy disabled: %s", err)
    else:
        def _forget_cached(server_id: int) -> None:
            server = registry.get(server_id)
            if server is not None and proxy is not None:
                proxy.forget(server["name"])

        oauth = upstream_oauth.OAuthManager(upstream_oauth.OAuthStore(db), registry.get,
                                            sign_in_timeout=SIGNIN_TIMEOUT, on_change=_forget_cached)

        def hub_upstreams() -> dict:
            global _upstreams
            if _upstreams is None:
                built = {}
                for s in registry.list():
                    if not (s["enabled"] and s["route"] == "hub" and s["transport"] == "http"):
                        continue
                    try:
                        built[s["name"]] = mcp_proxy.Upstream(
                            name=s["name"], url=s["url"], headers=s["headers"], call_timeout=PROXY_TIMEOUT,
                            oauth_id=s["id"] if s["auth"] == "oauth" else None)
                    except ValueError as err:
                        log.warning("not serving MCP server %s: %s", s["name"], err)
                _upstreams = built
            return _upstreams

        proxy = mcp_proxy.McpProxy(hub_upstreams, tokens=oauth)


async def keep_spend_fresh() -> None:
    """While "platform credits first" is on, keep the organizations' spend figures fresh (usage.summary
    fetches again every 15 minutes), so machines waiting for work never wait on the cost report."""
    while True:
        try:
            if credit_settings()["first"] and usage.org_names():
                await asyncio.to_thread(usage.summary)
        except Exception as err:  # noqa: BLE001 - try again next time
            log.warning("couldn't refresh API spend: %s", err)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # A mounted MCP app's own lifespan never runs, so the hub starts the session managers itself.
    async with AsyncExitStack() as stack:
        if hub_mcp is not None:
            await stack.enter_async_context(hub_mcp.session_manager.run())
        if proxy is not None:
            await stack.enter_async_context(proxy.run())
        if hub_job_mcp is not None:
            await stack.enter_async_context(hub_job_mcp.run())
        refresher = asyncio.create_task(keep_spend_fresh())
        try:
            yield
        finally:
            refresher.cancel()
            if oauth is not None:
                await oauth.close()  # let token renewals in flight store what the provider answered


app = FastAPI(title="Agent Bullpen", version=VERSION, docs_url=None, redoc_url=None, lifespan=lifespan)


def route_path(scope) -> str:
    """The path the router matches: without the root path (uvicorn --root-path), with a leading slash."""
    path, root = scope.get("path", ""), scope.get("root_path", "")
    if root and path.startswith(root):
        path = path[len(root):]
    return path if path.startswith("/") else "/" + path


class SameOriginWrites:
    """Dashboard writes must come from the dashboard itself. The session cookie is SameSite=Strict,
    which stops other *sites* but not other pages of the same site (another port on the same host,
    a sibling subdomain such as the chat app). Two checks, for every write to /api/* except the
    runners' bearer-token API:
      * Sec-Fetch-Site, which browsers set themselves on https and localhost, must not name another
        origin;
      * the body must be declared as JSON. A page on another origin can only send JSON with a CORS
        preflight, which the hub never grants; forms and no-cors requests can't. The dashboard
        always sends JSON. (This works over plain http too, where browsers don't send
        Sec-Fetch-Site, and behind proxies that rewrite the Host header.)"""

    def __init__(self, app):  # the parameter must be called app: Starlette passes it by name
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
            path = route_path(scope)
            if path.startswith("/api/") and not path.startswith("/api/runner/"):
                from starlette.datastructures import Headers
                headers = Headers(scope=scope)
                site = headers.get("sec-fetch-site", "")
                kind = headers.get("content-type", "").split(";")[0].strip().lower()
                if site and site not in ("same-origin", "none"):
                    problem = (403, "Cross-origin request refused")
                elif kind != "application/json":
                    problem = (415, "Send the request body as application/json")
                else:
                    problem = None
                if problem:
                    await JSONResponse({"detail": problem[1]}, status_code=problem[0])(scope, receive, send)
                    return
        await self.app(scope, receive, send)


class NoFraming:
    """The dashboard and its API can't be shown inside a frame. Otherwise a page on the same site
    (another port, a sibling subdomain) could frame the dashboard and trick the owner into a click
    on a button such as Allow. Adds `Content-Security-Policy: frame-ancestors 'none'` and
    `X-Frame-Options: DENY` to / and every /api/ response."""

    HEADERS = ((b"content-security-policy", b"frame-ancestors 'none'"), (b"x-frame-options", b"DENY"))

    def __init__(self, app):  # the parameter must be called app: Starlette passes it by name
        self.app = app

    async def __call__(self, scope, receive, send):
        path = route_path(scope) if scope["type"] == "http" else ""
        if path != "/" and not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        async def send_framed(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {name.lower() for name, _ in headers}
                headers += [h for h in self.HEADERS if h[0] not in have]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_framed)


app.add_middleware(SameOriginWrites)
app.add_middleware(NoFraming)
if hub_mcp_app is not None:
    ACCESS = mcp_server.access_verifier_from_env(os.environ)
    # /mcp/job checks a job token of its own, so the shared MCP token check leaves it alone.
    app.add_middleware(mcp_server.McpAuth, get_token=mcp_token, access=ACCESS,
                       exempt=(job_mcp.PATH,) if job_mcp is not None else ())
    app.router.routes.extend(hub_mcp_app.routes)  # exactly /mcp, guarded by McpAuth above
    if proxy is not None:
        app.router.routes.extend(proxy.routes())  # /mcp/s/<name>, guarded by McpAuth as well


# --------------------------------------------------------------------- auth
def _sign(value: str) -> str:
    return hmac.new(SECRET, value.encode(), hashlib.sha256).hexdigest()


def make_session_cookie() -> str:
    expires = str(int(time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(expires)}"


def valid_session(cookie: str | None) -> bool:
    if not cookie or "." not in cookie:
        return False
    expires, sig = cookie.split(".", 1)
    return hmac.compare_digest(sig, _sign(expires)) and expires.isdigit() and int(expires) > time.time()


def require_user(request: Request) -> None:
    if not UI_PASSWORD:
        return  # password disabled (only sensible on a trusted LAN behind Cloudflare Access)
    if not valid_session(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="Not logged in")


class RunnerAuth:
    """Who is calling the runner API: a machine with its own token (bound to one machine name),
    or anyone holding the shared HUB_RUNNER_TOKEN (for machines set up by hand)."""

    def __init__(self, kind: Literal["machine", "shared"], name: str | None = None):
        self.kind, self.name = kind, name


def require_runner(request: Request) -> RunnerAuth:
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if supplied:
        machine = db.machine_for_token(hashlib.sha256(supplied.encode("utf-8", "surrogateescape")).hexdigest())
        if machine is not None:
            return RunnerAuth("machine", machine["name"])
        if RUNNER_TOKEN and hmac.compare_digest(supplied.encode("utf-8", "surrogateescape"), RUNNER_TOKEN.encode()):
            return RunnerAuth("shared")
    raise HTTPException(status_code=401, detail="Bad runner token")


def check_runner_name(auth: RunnerAuth, name: str) -> None:
    """A machine's own token only acts for that machine. The shared token acts for any machine
    that has no token of its own (once a machine is enrolled, only its own token works for it)."""
    if auth.kind == "machine":
        if name != auth.name:
            raise HTTPException(403, f"This token belongs to the machine '{auth.name}', not '{name}'.")
    elif db.machine_token_of(name) is not None:
        raise HTTPException(403, f"'{name}' has its own runner token; the shared token can't act for it.")


def check_runner_job(auth: RunnerAuth, job: dict) -> None:
    # Only tasks and machine actions run on a machine; a council's own record has no machine, and
    # no token (shared or not) may write its log or result.
    if job.get("kind") not in ("task", "admin") or not job.get("runner"):
        raise HTTPException(403, "No machine runs this job.")
    check_runner_name(auth, job["runner"])


# ------------------------------------------------------------------ helpers
NAME_CONFLICT_WINDOW = 2 * ONLINE_WINDOW  # how long a clash of two computers on one name stays flagged


def runner_view(r: dict) -> dict:
    row = r
    r = dict(r)
    r["online"] = (time.time() - (r.get("last_seen") or 0)) < ONLINE_WINDOW
    # 'machine': it checks in with its own token; 'shared': with the hub's shared runner token.
    r["auth"] = r.get("auth") or "shared"
    # The tags the machine reports; only the accepted ones (and its platform tag) count for routing.
    r["tags"] = reported_tags(row)
    r["accepted_tags"] = effective_tags(row)
    # Each agent's account key for routing: its accepted label, or "<machine>/<agent>".
    r["accounts"] = {a["name"]: account_key(row, a["name"]) for a in row.get("agents") or []
                     if isinstance(a, dict) and isinstance(a.get("name"), str)}
    # What the machine reports that the owner hasn't accepted yet ({} when nothing waits), and
    # exactly what the owner has accepted (the machine page posts it back, less a withdrawn one).
    r["to_accept"] = to_accept(row)
    r["accepted"] = accepted_of(row)
    r.pop("machine_tags", None)
    # Each agent's plan limits, with their times on the hub's clock (display only).
    skew = r.pop("clock_skew", None)
    r["agents"] = [{**a, "limits": limits.agent_limits(a["limits"], skew)}
                   if isinstance(a, dict) and "limits" in a else a for a in (r.get("agents") or [])]
    other, at = r.pop("conflict_host", None), r.pop("conflict_at", None)
    if other and at and time.time() - at < NAME_CONFLICT_WINDOW:
        # Two computers are checking in under this one name: say which other one it is.
        r["info"] = {**(r["info"] if isinstance(r.get("info"), dict) else {}), "name_conflict": other}
    return r


def find_agent(runner: dict | None, agent: str) -> dict | None:
    for a in (runner or {}).get("agents") or []:
        if a.get("name") == agent:
            return a
    return None


def agent_has(runner: dict | None, agent: str, feature: str) -> bool:
    """Does this agent report the feature (section 4.1 of the runner's heartbeat)?"""
    features = (find_agent(runner, agent) or {}).get("features")
    return isinstance(features, list) and feature in features


NO_JOB_TOOLS = ("This hub has no MCP support (the 'mcp' package is missing), so approvals and subtasks are off. "
                "See the install guide.")
NO_PASSWORD = ("Set HUB_UI_PASSWORD to use approvals and subtasks: without it any program on your network could "
               "answer for you.")


def require_feature(runner_name: str, agent: str, feature: str, refusal: str) -> None:
    """Refuse an option the agent can't carry out. A runner from before agents reported their
    features gets the 'update the runner' message; a current one that lacks it gets `refusal`."""
    runner = db.runner(runner_name)
    features = (find_agent(runner, agent) or {}).get("features")
    if not isinstance(features, list):
        version = (runner or {}).get("version") or "?"
        raise HTTPException(400, f"{agent} on {runner_name} can't do this yet: update the runner on "
                                 f"{runner_name} (it runs {version}).")
    if feature not in features:
        raise HTTPException(400, refusal)


def check_job_tools_possible() -> None:
    """Approvals and subtasks need the job tools and a dashboard password (without one, any
    program on the network could answer for the owner)."""
    if hub_job_mcp is None:
        raise HTTPException(400, NO_JOB_TOOLS)
    if not UI_PASSWORD:
        raise HTTPException(400, NO_PASSWORD)


def check_autonomy(autonomy: str, runner_name: str | None, agent: str) -> None:
    """'ask' (edit files, ask me before commands) needs the job tools, a dashboard password and an
    agent that supports it. For a job aimed at a tag the agent's part is checked by
    check_tag_target, through the job's needs."""
    if autonomy != "ask":
        return
    check_job_tools_possible()
    if runner_name is None:
        return
    flags = permission_flags(db.runner(runner_name), agent)
    require_feature(runner_name, agent, "approvals",
                    f"{agent} on {runner_name} can't ask you before commands: its entry in runner.json has "
                    f"permission flags outside its levels ({', '.join(flags)}), which would win over asking. "
                    "Move them into its autonomy levels, or pick another level." if flags else
                    f"Asking before commands works with Claude agents on an up-to-date runner "
                    f"({agent} on {runner_name} can't).")


def check_live(runner_name: str, agent: str) -> None:
    """A session kept open for messages needs an agent that supports it (Claude agents)."""
    require_feature(runner_name, agent, "live",
                    f"Keeping a session open works with Claude agents only ({agent} can't).")


def check_target(runner_name: str, agent: str, project: str | None) -> None:
    """Refuse jobs aimed at a machine, agent or project that doesn't exist."""
    runner = db.runner(runner_name)
    if runner is None:
        raise HTTPException(400, f"Unknown machine '{runner_name}'. Has its runner checked in yet?")
    if find_agent(runner, agent) is None:
        raise HTTPException(400, f"Machine '{runner_name}' has no agent called '{agent}'.")
    if project and project not in (runner.get("projects") or []):
        raise HTTPException(400, f"Machine '{runner_name}' has no project called '{project}'.")


def check_tag_target(tag: str, agent: str, project: str | None, account: str | None = None,
                     needs: Any = (), delegated: bool = False) -> str:
    """Check a job aimed at "any machine tagged `tag`" and return the account key it runs under
    (common.tag_target has the rules and the wording). Tag jobs go only to machines with their own
    runner token and the accepted tag, and only to the agent with that account key."""
    try:
        return tag_target(db.runners(), tag, agent, project, account, needs, delegated)[0]
    except TargetError as err:
        raise HTTPException(400, str(err))


def tag_candidates(job: dict) -> dict:
    """For a queued tag job: how many machines could take it under the claim rule, and how many
    of them are online now."""
    opts = job["options"] if isinstance(job.get("options"), dict) else {}
    account = opts.get("account") if isinstance(opts.get("account"), str) else ""
    needs = opts.get("needs") if isinstance(opts.get("needs"), list) else []
    match = [r for r in db.runners()
             if tag_job_fits(r, job["target_tag"], job["agent"], job["project"], account, needs,
                             delegated=bool(opts.get("delegated")))]
    return {"match": len(match), "online": sum(1 for r in match if runner_view(r)["online"])}


# How many entries the owner's subtask scope may list (New task).
MAX_SCOPE_ENTRIES = 50
NO_SUBTASKS_SHARED = ("{m} still uses the shared runner token, and a task that hands out subtasks needs a machine "
                      "with its own token: use 'Make a setup command' on its page (guide 5).")


def delegation_options(body: Any, runner_name: str | None, agent: str) -> dict:
    """Check "May hand subtasks to other machines" and its scope (New task, follow-ups) and return
    the options to store. By default (the owner's decision of 2026-11-06) subtasks may use any of
    the owner's agents on the allowed machines and tags, whatever the account, in a project named
    like this task's or chat; delegate_accounts and delegate_agents narrow that."""
    check_job_tools_possible()
    if runner_name is not None:
        runner = db.runner(runner_name)
        if not own_token(runner):
            raise HTTPException(400, NO_SUBTASKS_SHARED.format(m=runner_name))
        require_feature(runner_name, agent, "job_mcp",
                        f"{agent} can't hand out subtasks (it needs the hub's job tools; for Gemini see the "
                        "troubleshooting guide).")
    to, runners = [], db.runners()
    for entry in body.delegate_to[:MAX_SCOPE_ENTRIES + 1]:
        kind, _, name = (entry or "").strip().partition(":")
        if kind == "machine" and NAME_RE.fullmatch(name):
            if not any(r["name"] == name for r in runners):
                raise HTTPException(400, f"Unknown machine '{name}' under 'Subtasks may go to'.")
        elif kind == "tag" and TAG_RE.fullmatch(name):
            if not any(name in reported_tags(r) for r in runners):
                raise HTTPException(400, f"No machine reports the tag '{name}' (under 'Subtasks may go to').")
        else:
            raise HTTPException(400, f"'{entry}' under 'Subtasks may go to' isn't machine:<name> or tag:<tag>.")
        to.append(f"{kind}:{name}")
    if len(to) > MAX_SCOPE_ENTRIES:
        raise HTTPException(400, f"List at most {MAX_SCOPE_ENTRIES} machines and tags for subtasks.")
    opts: dict[str, Any] = {"delegate": True, "delegate_to": sorted(set(to)),
                            "delegate_projects": body.delegate_projects}
    if body.delegate_accounts is not None:
        accounts = [(a or "").strip() for a in body.delegate_accounts]
        if len(accounts) > MAX_SCOPE_ENTRIES:
            raise HTTPException(400, f"List at most {MAX_SCOPE_ENTRIES} accounts for subtasks.")
        for a in accounts:
            if not valid_account_key(a):
                raise HTTPException(400, f"'{a}' isn't an account label or a <machine>/<agent> key.")
        opts["delegate_accounts"] = sorted(set(accounts))
    if body.delegate_agents is not None:
        agents = [(a or "").strip() for a in body.delegate_agents]
        if not agents:
            raise HTTPException(400, "Pick at least one agent for subtasks, or untick 'May hand subtasks to other "
                                     "machines'.")
        if len(agents) > MAX_SCOPE_ENTRIES or not all(NAME_RE.fullmatch(a) for a in agents):
            raise HTTPException(400, "List agents for subtasks by their names (at most 50).")
        opts["delegate_agents"] = sorted(set(agents))
    return opts


def runner_bundle() -> dict:
    """The runner files this hub can hand out, with their version and checksums."""
    files = {}
    version = ""
    for path in sorted(RUNNER_DIST.glob("*.py")) if RUNNER_DIST.is_dir() else []:
        data = path.read_bytes()
        files[path.name] = {"sha256": hashlib.sha256(data).hexdigest(),
                            "content": base64.b64encode(data).decode()}
        if path.name == "runner.py":
            m = re.search(rb'^VERSION = "([^"]+)"', data, re.M)
            version = m.group(1).decode() if m else ""
    return {"version": version, "files": files}


def latest_runner_version() -> str:
    try:
        m = re.search(r'^VERSION = "([^"]+)"', (RUNNER_DIST / "runner.py").read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


MIN_ADMIN_VERSION = (0, 2, 0)  # the oldest runner version that carries out machine actions


def check_model(model: str | None) -> str | None:
    model = (model or "").strip()
    if not model:
        return None
    if not MODEL_RE.match(model):
        raise HTTPException(400, f"'{model}' doesn't look like a model name.")
    return model


def mark_lost_jobs(runner_name: str, reported: list[int]) -> None:
    now = time.time()
    for job in db.query(
        "SELECT id, kind, parent_id, delegated_by, title, options, started_at FROM jobs WHERE status='running' "
        "AND kind IN ('task', 'admin') AND runner=?", (runner_name,),
    ):
        if job["id"] not in reported and (job["started_at"] or now) < now - LOST_JOB_GRACE:
            db.update_job(job["id"], status="error", finished_at=now,
                          error="Lost: the runner stopped reporting this job (it probably restarted).")
            if job["parent_id"]:
                councils.advance(job["parent_id"])
            if top_level_task(job):
                notifier.send("error", {**job, "status": "error"})


# ======================================================================= UI
@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(HERE / "static" / "index.html")


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


class Login(BaseModel):
    password: str


@app.post("/api/login")
async def login(body: Login, response: Response) -> dict:
    if not UI_PASSWORD or hmac.compare_digest(body.password, UI_PASSWORD):
        response.set_cookie(COOKIE, make_session_cookie(), max_age=SESSION_DAYS * 86400,
                            httponly=True, samesite="strict",
                            secure=os.environ.get("HUB_COOKIE_SECURE", "0") == "1")
        return {"ok": True}
    await asyncio.sleep(1.5)  # slow down password guessing
    raise HTTPException(401, "Wrong password")


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/state", dependencies=[Depends(require_user)])
def state() -> dict:
    return {
        "version": VERSION,
        "runners": [runner_view(r) for r in db.runners()],
        # Recent tasks and councils, and machine admin jobs (updates, syncs, projects) on their own,
        # so a "Sync all" across many machines doesn't push real work off the Overview.
        "jobs": recent_jobs(admin=False, limit=15),
        "admin_jobs": recent_jobs(admin=True, limit=10),
        "admin_open": db.query_one("SELECT COUNT(*) AS n FROM jobs WHERE parent_id IS NULL AND kind='admin' "
                                   "AND status IN ('queued', 'running')")["n"],
        "limits": {"debate_participants": MAX_DEBATE_PARTICIPANTS, "reviewers": MAX_REVIEWERS},
        "links": usage.links(),
        "password_enabled": bool(UI_PASSWORD),
        "runner_latest": latest_runner_version(),
        "context_token": context_token(),
        "mcp_enabled": hub_mcp is not None,
        # The address machines use (HUB_RUNNER_URL), to prefill "Add a machine".
        "runner_url": RUNNER_URL,
        # The job tools at /mcp/job (approvals and subtasks need them), and whether approvals can
        # be offered at all: they also need a dashboard password.
        "job_tools": hub_job_mcp is not None,
        "approvals_possible": bool(UI_PASSWORD) and hub_job_mcp is not None,
        # Questions from 'ask' jobs waiting for an answer: how many, and the oldest ten.
        "approvals_pending": db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status='pending'")["n"],
        "approvals": db.query("SELECT a.job_id, j.title, a.tool_name, a.created_at FROM approvals a "
                              "JOIN jobs j ON j.id = a.job_id WHERE a.status='pending' ORDER BY a.id LIMIT 10"),
        # Whether the hub sends notifications (HUB_NOTIFY_URL).
        "notify": notifier.enabled,
        # Subtasks: the most one may do (HUB_DELEGATE_MAX_AUTONOMY), and how many a job may hand
        # out in all and at once.
        "delegation": {"max_autonomy": delegation.MAX_AUTONOMY, "max_children": delegation.MAX_CHILDREN,
                       "max_active": delegation.MAX_ACTIVE},
        # Platform credits: "credits first" and, per machine with a credits agent, its state (from the
        # spend figures last fetched).
        "credits": credits_state(),
    }


def credits_state() -> dict:
    views = credit_views()
    return {"first": credit_settings()["first"], "machines": credit_machines(views)}


class NewTask(BaseModel):
    # Exactly one of runner (a machine by name) and target_tag ("any machine tagged ..."). For a
    # tag, account picks the account key when the agent is a different account on different
    # machines with that tag. Follow-ups ignore all three: they go to the machine that ran the job.
    runner: str = ""
    target_tag: Optional[str] = None
    account: Optional[str] = None
    agent: str
    project: str = "chat"
    prompt: str = Field(min_length=1)
    title: Optional[str] = None
    autonomy: Autonomy = "readonly"
    model: Optional[str] = None
    timeout_min: int = Field(default=60, ge=1, le=24 * 60)
    capture_diff: bool = False
    follow_up_of: Optional[int] = None
    # Keep the session open for messages typed on the job's page, until End session, this many
    # minutes without a message, or the time limit.
    live: bool = False
    idle_min: int = Field(default=15, ge=1, le=240)
    # "May hand subtasks to other machines", and where they may go (see delegation_options):
    # machine:<name> / tag:<tag> entries (none: every machine that takes subtasks); "same": only
    # projects named like this task's, and chat; the accounts (besides this task's own) and the
    # agents subtasks may use, None meaning any.
    delegate: bool = False
    delegate_to: list[str] = []
    delegate_projects: Literal["same", "any"] = "same"
    delegate_accounts: Optional[list[str]] = None
    delegate_agents: Optional[list[str]] = None


@app.post("/api/tasks", dependencies=[Depends(require_user)])
def create_task(body: NewTask) -> dict:
    resume = None
    model = check_model(body.model)
    tag = None
    if not body.follow_up_of:
        body.runner = body.runner.strip()
        tag = (body.target_tag or "").strip().lower() or None
        if bool(body.runner) == bool(tag):
            raise HTTPException(400, "Pick a machine or a tag.")
    if body.follow_up_of:
        prev = db.job(body.follow_up_of)
        if prev is None:
            raise HTTPException(404, "The job you are following up on doesn't exist.")
        if prev["kind"] != "task":
            raise HTTPException(400, "Only a task, or a step of a council, can be followed up.")
        if prev["status"] not in ("done", "error", "cancelled"):
            raise HTTPException(409, "That job is still running. Send a message instead, or wait until it finishes.")
        if not prev.get("session_id"):
            raise HTTPException(400, "That job has no session to continue.")
        # A follow-up continues the same conversation, so it must go to the same place. Autonomy and
        # model come from the request, with the same checks as a new task. It is the owner's own
        # task: never a subtask, and never a council step, even when the job it follows was one.
        # A follow-up of a job aimed at a tag goes to the machine that ran it, by name: its tag and
        # account are not copied.
        body.runner, body.agent, body.project = prev["runner"], prev["agent"], prev["project"]
        resume = prev["session_id"]
    # What the machine's agent must support, for claiming the job.
    needs = ((["approvals"] if body.autonomy == "ask" else []) + (["live"] if body.live else [])
             + (["job_mcp"] if body.delegate else []))
    options: dict[str, Any] = {"timeout_min": body.timeout_min, "capture_diff": body.capture_diff}
    if tag:
        check_autonomy(body.autonomy, None, body.agent)
        if body.delegate:
            options.update(delegation_options(body, None, body.agent))
        options["account"] = check_tag_target(tag, body.agent, body.project, (body.account or "").strip() or None,
                                              needs=needs)
    else:
        check_target(body.runner, body.agent, body.project)
        check_autonomy(body.autonomy, body.runner, body.agent)
        if body.live:
            check_live(body.runner, body.agent)
        if body.delegate:
            options.update(delegation_options(body, body.runner, body.agent))
    typed = (body.title or "").strip()
    title = typed or body.prompt.strip().splitlines()[0][:80]
    if typed:
        options["title_typed"] = True  # the owner typed it, so a notification may show it
    if body.live:
        options.update(live=True, idle_min=body.idle_min)
    if needs:
        options["needs"] = needs
    job_id = db.create_job(
        kind="task", title=title, runner=None if tag else body.runner, target_tag=tag, agent=body.agent,
        project=body.project, autonomy=body.autonomy, prompt=body.prompt, resume_session=resume, model=model,
        follow_up_of=body.follow_up_of, options=options,
    )
    return {"id": job_id}


class Seat(BaseModel):
    # Exactly one of runner (a machine by name) and tag ("any machine tagged ..."); account picks
    # the account key for a tag seat (see NewTask).
    runner: str = ""
    tag: Optional[str] = None
    account: Optional[str] = None
    agent: str
    project: Optional[str] = None
    model: Optional[str] = None


class NewCouncil(BaseModel):
    mode: Literal["debate", "review"]
    prompt: str = Field(min_length=1)
    title: Optional[str] = None
    participants: list[Seat] = []      # debate
    judge: Optional[Seat] = None       # debate
    revise: bool = False               # debate: add a revise round
    builder: Optional[Seat] = None     # review
    reviewers: list[Seat] = []         # review
    fix: bool = True                   # review: let the builder answer the reviews
    autonomy: Autonomy = "edits"       # review: what the builder may do
    timeout_min: int = Field(default=60, ge=1, le=24 * 60)


@app.post("/api/councils", dependencies=[Depends(require_user)])
def create_council(body: NewCouncil) -> dict:
    if body.mode == "debate":
        if len(body.participants) < 2:
            raise HTTPException(400, "A debate needs at least two participants.")
        if len(body.participants) > MAX_DEBATE_PARTICIPANTS:
            raise HTTPException(400, f"A debate can have at most {MAX_DEBATE_PARTICIPANTS} participants.")
        if body.judge is None:
            raise HTTPException(400, "Pick a judge.")
        seats = body.participants + [body.judge]
    else:
        if body.builder is None or not body.reviewers:
            raise HTTPException(400, "Pick a builder and at least one reviewer.")
        if len(body.reviewers) > MAX_REVIEWERS:
            raise HTTPException(400, f"Build + review can have at most {MAX_REVIEWERS} reviewers.")
        if not body.builder.project:
            raise HTTPException(400, "The builder needs a project to work in.")
        seats = [body.builder] + body.reviewers
    for seat in seats:
        seat.runner = seat.runner.strip()
        seat.tag = (seat.tag or "").strip().lower() or None
        if bool(seat.runner) == bool(seat.tag):
            raise HTTPException(400, "Pick a machine or a tag for every seat.")
        seat.model = check_model(seat.model)
        if seat.runner:
            check_target(seat.runner, seat.agent, seat.project)
            seat.account = None
        else:
            # The builder of a build + review that asks before commands needs agents that can ask.
            ask = body.mode == "review" and seat is body.builder and body.autonomy == "ask"
            # A step without a project of its own runs in chat (CouncilEngine._step).
            seat.account = check_tag_target(seat.tag, seat.agent, seat.project or "chat",
                                            (seat.account or "").strip() or None,
                                            needs=["approvals"] if ask else [])
    if body.mode == "review":
        check_autonomy(body.autonomy, body.builder.runner or None, body.builder.agent)
    spec: dict[str, Any] = body.model_dump()
    council_id = councils.create(spec)
    return {"id": council_id}


# --------------------------------------------------------------- job lists
# The Overview shows the newest jobs and History pages through all of them. Both get a short
# summary of each job (no prompt, result, diff or log), so a list stays small however long the
# jobs were. Council steps aren't listed on their own: they are inside their council.
JOB_LIST_COLUMNS = ("id, kind, title, runner, agent, project, autonomy, model, status, created_at, "
                    "started_at, finished_at, substr(error, 1, 300) AS error, error_kind, meta, delegated_by, "
                    "target_tag, named_agent, credits, retry_of")
JOB_KINDS = ("task", "council", "admin")
JOB_STATUSES = ("queued", "running", "done", "error", "cancelled")


def job_summaries(rows: list[dict]) -> list[dict]:
    """Trim job rows for a list. A council keeps only its mode and stage from its settings, and
    gets `machines`: the machines its steps ran on. A task gets `subtasks`: how many subtasks it
    handed out."""
    council_ids = [r["id"] for r in rows if r["kind"] == "council"]
    task_ids = [r["id"] for r in rows if r["kind"] == "task"]
    subtasks: dict[int, int] = {}
    if task_ids:
        marks = ",".join("?" * len(task_ids))
        subtasks = {r["delegated_by"]: r["n"] for r in db.query(
            f"SELECT delegated_by, COUNT(*) AS n FROM jobs WHERE delegated_by IN ({marks}) GROUP BY delegated_by",
            tuple(task_ids))}
    machines: dict[int, list[str]] = {}
    # Jobs waiting for the owner's approval (a council when one of its steps is).
    waiting: set[int] = set()
    for a in db.query("SELECT DISTINCT a.job_id, j.parent_id FROM approvals a JOIN jobs j ON j.id = a.job_id "
                      "WHERE a.status='pending'"):
        waiting.update(x for x in (a["job_id"], a["parent_id"]) if x)
    if council_ids:
        marks = ",".join("?" * len(council_ids))
        for step in db.query(f"SELECT DISTINCT parent_id, runner FROM jobs WHERE parent_id IN ({marks}) "
                             "AND runner IS NOT NULL ORDER BY runner", tuple(council_ids)):
            machines.setdefault(step["parent_id"], []).append(step["runner"])
    out = []
    for row in rows:
        row = dict(row)
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
        row["meta"] = {}
        row["waiting_approval"] = row["id"] in waiting
        if row["kind"] == "task":
            row["subtasks"] = subtasks.get(row["id"], 0)
        if row["kind"] == "council":
            row["meta"] = {"mode": meta.get("mode"), "stage": meta.get("stage")}
            row["machines"] = machines.get(row["id"], [])
        out.append(row)
    return out


def recent_jobs(admin: bool, limit: int) -> list[dict]:
    """The newest top-level jobs: machine admin jobs, or everything else (tasks and councils)."""
    kind = "kind = 'admin'" if admin else "kind != 'admin'"
    return job_summaries(db.query(
        f"SELECT {JOB_LIST_COLUMNS} FROM jobs WHERE parent_id IS NULL AND {kind} ORDER BY id DESC LIMIT ?",
        (limit,)))


@app.get("/api/jobs", dependencies=[Depends(require_user)])
def list_jobs(limit: int = 50, before_id: Optional[int] = None, runner: str = "", agent: str = "",
              kind: str = "", status: str = "", q: str = "") -> dict:
    """History: top-level jobs, newest first, `limit` at a time. For the next (older) page, pass
    this page's `next_before_id` as `before_id`. `status` may list several, separated by commas.
    A council matches a machine (or an agent) when any of its steps ran there. The first page
    also lists every machine and agent that has jobs, for the filters."""
    limit = max(1, min(limit, 200))
    where, args = ["parent_id IS NULL"], []
    if before_id is not None:
        where.append("id < ?")
        args.append(before_id)
    if kind:
        if kind not in JOB_KINDS:
            raise HTTPException(400, f"kind must be one of: {', '.join(JOB_KINDS)}.")
        where.append("kind = ?")
        args.append(kind)
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        if not wanted or any(s not in JOB_STATUSES for s in wanted):
            raise HTTPException(400, f"status must be one or more of: {', '.join(JOB_STATUSES)}.")
        where.append(f"status IN ({','.join('?' * len(wanted))})")
        args += wanted
    for column, value in (("runner", runner.strip()), ("agent", agent.strip())):
        if value:  # `column` is one of the two names above, never user input
            where.append(f"({column} = ? OR id IN (SELECT parent_id FROM jobs WHERE parent_id IS NOT NULL "
                         f"AND {column} = ?))")
            args += [value, value]
    if q.strip():
        where.append("title LIKE ? ESCAPE '\\'")
        args.append("%" + re.sub(r"([\\%_])", r"\\\1", q.strip()[:200]) + "%")
    rows = db.query(f"SELECT {JOB_LIST_COLUMNS} FROM jobs WHERE {' AND '.join(where)} "
                    "ORDER BY id DESC LIMIT ?", (*args, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    out: dict[str, Any] = {"jobs": job_summaries(rows), "has_more": more,
                           "next_before_id": rows[-1]["id"] if more else None}
    if before_id is None:
        for column, key in (("runner", "runners"), ("agent", "agents")):
            out[key] = [r["v"] for r in db.query(
                f"SELECT DISTINCT {column} AS v FROM jobs WHERE {column} IS NOT NULL AND {column} != '' "
                "ORDER BY v")]
    return out


# A conversation: a job, the jobs it followed up, and the follow-ups after it.
THREAD_MAX = 50              # jobs walked in each direction
THREAD_PROMPT_CHARS = 20_000
THREAD_RESULT_CHARS = 60_000
SESSION_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# Characters PowerShell also reads as single quotes, so a path holding one can't be quoted safely.
PS_QUOTES = "\u2018\u2019\u201a\u201b"


def thread_ids(job: dict) -> list[int]:
    """The ids of a job's conversation, oldest first: back along follow_up_of from the job, then
    forward along the newest follow-up of each job (at most THREAD_MAX each way)."""
    seen = {job["id"]}
    back: list[int] = []
    prev = job.get("follow_up_of")
    while prev and prev not in seen and len(back) < THREAD_MAX:
        row = db.query_one("SELECT id, follow_up_of FROM jobs WHERE id=?", (prev,))
        if row is None:
            break
        back.append(row["id"])
        seen.add(row["id"])
        prev = row["follow_up_of"]
    forward: list[int] = []
    current = job["id"]
    while len(forward) < THREAD_MAX:
        row = db.query_one("SELECT id FROM jobs WHERE follow_up_of=? ORDER BY id DESC LIMIT 1", (current,))
        if row is None or row["id"] in seen:
            break
        forward.append(row["id"])
        seen.add(row["id"])
        current = row["id"]
    return back[::-1] + [job["id"]] + forward


def _plain(text: str) -> bool:
    """No control characters (a newline in a command shown for copying would run a second one)."""
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in text)


def continue_command(job: dict) -> str | None:
    """The command that continues a finished job's session in a terminal on its machine, when
    the machine says how (the agent's `terminal` template) and where the project is."""
    if job["kind"] != "task" or job["status"] in ("queued", "running") or not job.get("runner"):
        return None
    session = job.get("session_id") or ""
    if not SESSION_ID_RE.fullmatch(session):
        return None
    runner = db.runner(job["runner"])
    template = (find_agent(runner, job["agent"]) or {}).get("terminal")
    info = (runner or {}).get("info")
    info = info if isinstance(info, dict) else {}
    paths = info.get("project_paths")
    path = paths.get(job["project"]) if isinstance(paths, dict) else None
    if not isinstance(template, str) or not template.strip() or not isinstance(path, str) or not path:
        return None
    if not _plain(template) or not _plain(path):
        return None
    command = template.strip().replace("{session_id}", session)
    if info.get("platform") == "windows":
        if any(q in path for q in PS_QUOTES):
            return None
        return "Set-Location -LiteralPath '" + path.replace("'", "''") + "'; " + command
    return "cd '" + path.replace("'", "'\\''") + "' && " + command


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_user)])
def get_job(job_id: int, since: int = 0, messages_since: int = 0) -> dict:
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    out: dict[str, Any] = {"job": public_job(job), "logs": db.logs(job_id, since)}
    if job["kind"] == "council":
        out["steps"] = [public_job(step) for step in db.children(job_id)]
    if job["kind"] == "task":
        out["thread_len"] = len(thread_ids(job))
        # Live sessions: the messages after `messages_since`, like `since` for log lines.
        out["messages"] = db.messages(job_id, messages_since)
        out["continue_command"] = continue_command(job)
        # Questions from an 'ask' job: pending ones first, then the 20 newest answered ones.
        out["approvals"] = [approval_view(a) for a in db.approvals_for_job(job_id)]
        # A subtask's or a council step's prompt was written by an agent, not by the owner.
        out["asked_by_agent_prompt"] = bool(job.get("delegated_by") or job.get("parent_id"))
        # The subtasks this job handed out, and for a queued job aimed at a tag, how many machines
        # could take it (and how many of those are online).
        out["subtasks"] = [subtask_view(s) for s in db.subtasks(job_id)]
        if job["status"] == "queued" and job.get("target_tag"):
            out["tag_candidates"] = tag_candidates(job)
        # Platform credits: the retry of this job on credits, if there is one; otherwise, for a job
        # that stopped at a usage limit, whether "Retry on credits" can queue one, or why not.
        retry = db.query_one("SELECT id, status, agent FROM jobs WHERE retry_of=?", (job_id,))
        out["retried_as"] = retry
        if retry is None and job.get("error_kind") == "claude_limit" and job["status"] == "error":
            agent, org, why = credits.retry_target(job, db.runner(job.get("runner") or ""), usage.org_names(),
                                                   credit_states(), by_hand=True)
            out["credits_retry"] = {"agent": agent, "org": org, "why": why}
    return out


def subtask_view(job: dict) -> dict:
    """One subtask for its parent's page."""
    opts = job["options"] if isinstance(job.get("options"), dict) else {}
    return {"id": job["id"], "title": job["title"], "runner": job["runner"], "target_tag": job.get("target_tag"),
            "agent": job["agent"], "project": job["project"], "autonomy": job["autonomy"],
            "named_agent": job.get("named_agent"),
            "account": opts.get("account"), "status": job["status"], "error_kind": job.get("error_kind"),
            "created_at": job["created_at"], "started_at": job["started_at"], "finished_at": job["finished_at"]}


def approval_view(a: dict) -> dict:
    """One question for the job page, its input parsed (masked once the job has ended)."""
    try:
        tool_input = json.loads(a["tool_input"]) if a["tool_input"] else None
    except ValueError:
        tool_input = a["tool_input"]
    return {"id": a["id"], "tool_name": a["tool_name"], "tool_input": tool_input, "status": a["status"],
            "message": a["message"], "created_at": a["created_at"], "expires_at": a["expires_at"],
            "decided_at": a["decided_at"]}


def _clip(text: str | None, limit: int) -> str | None:
    return text[:limit] + "…" if text and len(text) > limit else text


@app.get("/api/jobs/{job_id}/thread", dependencies=[Depends(require_user)])
def job_thread(job_id: int) -> dict:
    """A job's whole conversation (see thread_ids), each turn with its live-session messages."""
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    turns = []
    for tid in thread_ids(job):
        row = public_job(job if tid == job_id else db.job(tid))
        if row is None:
            continue
        turns.append({
            "job_id": row["id"], "status": row["status"], "runner": row["runner"], "agent": row["agent"],
            "model": row.get("model"), "autonomy": row["autonomy"],
            "prompt": _clip(row["prompt"], THREAD_PROMPT_CHARS), "result": _clip(row["result"], THREAD_RESULT_CHARS),
            "error": row["error"], "created_at": row["created_at"], "finished_at": row["finished_at"],
            "messages": [{"role": m["role"], "text": m["text"], "created_at": m["created_at"],
                          "is_error": m["is_error"], "undelivered": m["undelivered"]}
                         for m in db.messages(tid)],
        })
    return {"current": job_id, "turns": turns}


def cancel_one(job: dict) -> None:
    """Cancel a task, a council or a machine action. A running job on an online machine is asked
    to stop, and loses its job tools at once (Database.request_cancel); on an offline machine it is
    settled now. Its subtasks follow through the database's terminal rule.

    `job` was read a moment ago, and a machine may have claimed or finished it since: each change
    is made only if the job is still in the state it was read in. A queued job claimed meanwhile is
    then asked to stop like any running one, and a job that finished meanwhile keeps its result."""
    job_id = job["id"]
    if job["kind"] == "council":
        if job["status"] == "running":
            councils.cancel(job_id)
        return
    if job["status"] == "queued":
        if db.update_job(job_id, expect="queued", status="cancelled", finished_at=time.time()):
            if job["parent_id"]:
                councils.advance(job["parent_id"])
            return
        job = db.job(job_id) or job  # a machine claimed it meanwhile
    if job["status"] == "running":
        runner = db.runner(job["runner"]) if job["runner"] else None
        if runner is None or not runner_view(runner)["online"]:
            # Nobody is going to report back, so settle it now. The flags tell the machine to stop
            # the job if it comes back still running it: its next log upload answers cancel (and,
            # for a live session, end), so it doesn't keep an agent and a job slot busy.
            flags = {"cancel_requested": 1}
            if job["kind"] == "task" and isinstance(job.get("options"), dict) and job["options"].get("live"):
                flags["end_requested"] = 1
            if db.update_job(job_id, expect="running", status="cancelled", finished_at=time.time(),
                             error="Cancelled while the machine was offline.", **flags) and job["parent_id"]:
                councils.advance(job["parent_id"])
        else:
            db.request_cancel(job_id)


@app.post("/api/jobs/{job_id}/retry-on-credits", dependencies=[Depends(require_user)])
def retry_job_on_credits(job_id: int) -> dict:
    """Retry on credits: run a task that stopped at a subscription usage limit once more, on its
    machine's credits agent (also when it may have changed files: the owner decides)."""
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    return {"id": retry_on_credits(job, by_hand=True)}


@app.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(require_user)])
def cancel_job(job_id: int) -> dict:
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    cancel_one(job)
    return {"ok": True}


# ------------------------------------------------------------ live sessions
MAX_MESSAGE_CHARS = 20_000
MAX_WAITING_MESSAGES = 20    # user messages the agent hasn't read yet, per job
MAX_USER_MESSAGES = 200      # user messages per job in all
MAX_TURN_CHARS = 200_000     # one agent answer, as the runner sends it
SLASH_REFUSAL = "Slash commands can't be sent to a running job; type them in a terminal session."


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


def live_job(job_id: int) -> dict:
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    opts = job["options"] if isinstance(job.get("options"), dict) else {}
    if job["kind"] != "task" or not opts.get("live"):
        raise HTTPException(400, "This job isn't a session kept open for messages.")
    return job


@app.post("/api/jobs/{job_id}/messages", dependencies=[Depends(require_user)])
def send_message(job_id: int, body: MessageIn) -> dict:
    """Type to a live session. The runner picks the message up with its next log upload and
    writes it to the agent between turns."""
    if body.text.lstrip().startswith("/"):
        # Claude runs these as its own commands, and /clear would reset the conversation and its totals.
        raise HTTPException(400, SLASH_REFUSAL)
    if not body.text.strip():
        raise HTTPException(400, "Type a message first.")
    # Checked and stored in one go, so a message can't land after the session has ended.
    with db.lock:
        job = live_job(job_id)
        if job["status"] not in ("queued", "running") or job["end_requested"] or job["cancel_requested"]:
            raise HTTPException(409, "The session has ended.")
        counts = db.query_one("SELECT COUNT(*) AS total, COALESCE(SUM(delivered_at IS NULL AND undelivered=0), 0) "
                              "AS waiting FROM job_messages WHERE job_id=? AND role='user'", (job_id,))
        if counts["waiting"] >= MAX_WAITING_MESSAGES:
            raise HTTPException(409, f"{MAX_WAITING_MESSAGES} messages are already waiting for the agent. "
                                     "Send more once it has read them.")
        if counts["total"] >= MAX_USER_MESSAGES:
            raise HTTPException(409, f"This session has had {MAX_USER_MESSAGES} messages, the most one session "
                                     "takes. Follow up on the job once it has finished.")
        message_id = db.add_message(job_id, "user", body.text)
    return {"id": message_id}


@app.post("/api/jobs/{job_id}/end", dependencies=[Depends(require_user)])
def end_session(job_id: int) -> dict:
    """End session: the agent answers the messages it already has, then the job finishes. A
    queued session is cancelled; one on a machine that is offline is settled now, with the
    agent's last answer as its result when there is one."""
    job = live_job(job_id)
    if job["status"] == "queued":
        cancel_one(job)
    elif job["status"] == "running":
        runner = db.runner(job["runner"]) if job["runner"] else None
        if runner is None or not runner_view(runner)["online"]:
            # end_requested: if the machine comes back still running the session, its next log
            # upload answers end, so it closes the session instead of waiting for the idle limit.
            # Only if it is still running: it may have finished since it was read.
            last = db.query_one("SELECT text FROM job_messages WHERE job_id=? AND role='agent' "
                                "ORDER BY turn_no DESC, id DESC LIMIT 1", (job_id,))
            if last is not None:
                changed = db.update_job(job_id, expect="running", status="done", result=last["text"],
                                        finished_at=time.time(), end_requested=1)
            else:
                changed = db.update_job(job_id, expect="running", status="cancelled", finished_at=time.time(),
                                        error="Ended while the machine was offline.", end_requested=1)
            if changed and job["parent_id"]:
                councils.advance(job["parent_id"])
        else:
            db.execute("UPDATE jobs SET end_requested=1 WHERE id=? AND status='running'", (job_id,))
    return {"ok": True}


# ---------------------------------------------------------------- approvals
class ApprovalAnswer(BaseModel):
    decision: Literal["allow", "deny"]
    message: str = Field(default="", max_length=10_000)  # the reason sent with Deny (the first 2,000 characters)


@app.post("/api/jobs/{job_id}/approvals/{approval_id}", dependencies=[Depends(require_user)])
def answer_approval(job_id: int, approval_id: int, body: ApprovalAnswer) -> dict:
    """Allow or Deny a question from an 'ask' job. Only the dashboard login can answer, and only
    on a hub with a dashboard password: without one, any program on the network could answer."""
    if not UI_PASSWORD:
        raise HTTPException(403, NO_PASSWORD)
    try:
        return hub_approvals.decide(job_id, approval_id, body.decision, body.message)
    except ApprovalRefused as err:
        raise HTTPException(err.status, str(err))


# ------------------------------------------------- managing machines remotely
# These become 'admin' jobs: the runner on that machine carries them out and
# reports back like any other job, so they show up (with logs) in the history.
ADMIN_ACTIONS = {
    "project.clone": "Clone a git repository as a new project",
    "project.create": "Create an empty project folder",
    "project.remove": "Stop offering a project (its files are kept)",
    "project.pull": "Pull the latest changes (git pull --ff-only)",
    "runner.update": "Update the runner",
    "sync": "Sync shared notes, add-ons and MCP servers",
    "usage.check": "Check Claude plan limits",
    "rc.start": "Start Remote Control",
    "rc.stop": "Stop Remote Control",
}
GIT_URL_RE = re.compile(r"^(https://|ssh://|git@)[^\s'\"`$;|&<>]+$")
# Check limits runs Claude Code's /usage at most once in this many seconds for one agent, and for
# one account (on any machine).
USAGE_CHECK_EVERY = 300
COMMAND_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Start Remote Control is dropped when its machine hasn't picked it up within this many seconds.
RC_START_EXPIRES = 120


class AdminAction(BaseModel):
    action: str
    params: dict[str, str] = {}


def _minutes(seconds: float) -> str:
    """'less than a minute', '1 minute', '4 minutes' (rounded up)."""
    if seconds < 60:
        return "less than a minute"
    n = math.ceil(seconds / 60)
    return f"{n} minute{'' if n == 1 else 's'}"


def recent_usage_checks(now: float) -> list[dict]:
    """The usage.check jobs created in the last USAGE_CHECK_EVERY seconds, whatever became of
    them: {"runner", "agents" (set), "accounts" (set), "at"}."""
    out = []
    for job in db.query("SELECT runner, options, created_at FROM jobs WHERE kind='admin' AND created_at > ? "
                        "AND options LIKE '%usage.check%'", (now - USAGE_CHECK_EVERY,)):
        opts = job["options"] if isinstance(job.get("options"), dict) else {}
        params = opts.get("params") if isinstance(opts.get("params"), dict) else {}
        if opts.get("action") != "usage.check":
            continue
        agents = params.get("agents")
        accounts = params.get("accounts")
        out.append({"runner": job["runner"], "at": job["created_at"] or 0,
                    "agents": {a.strip() for a in agents.split(",")} if isinstance(agents, str) else set(),
                    "accounts": {a for a in accounts if isinstance(a, str)} if isinstance(accounts, list) else set()})
    return out


def last_usage_check(checks: list[dict], runner: str | None, agent: str | None, account: str | None) -> float | None:
    """When this agent (on this machine) or this account (on any machine) was last checked."""
    times = [c["at"] for c in checks
             if (runner is not None and c["runner"] == runner and agent in c["agents"])
             or (account is not None and account in c["accounts"])]
    return max(times) if times else None


def agent_command(agent: dict) -> str:
    """How the owner starts this agent's CLI in a terminal, for '<command> auth login'. The hub
    only knows the agent's name and preset: the setup scripts make a command named after each
    extra Claude account's agent."""
    name, preset = str(agent.get("name") or ""), agent.get("preset")
    if preset == "claude":
        return name if COMMAND_WORD_RE.fullmatch(name) else "claude"
    if preset in ("gemini", "selftest"):
        return preset
    return name or "the agent's CLI"


def usage_check_refusal(runner: dict, agent: str) -> str:
    """Why an agent can't check its Claude plan limits."""
    machine = runner["name"]
    found = find_agent(runner, agent) or {}
    auth = found.get("auth")
    if auth == "api_key":
        return f"{agent} on {machine} signs in with an API key, which has no plan limits."
    if auth == "token":
        return (f"{agent} signs in with a long-lived token (claude setup-token), which can't read plan limits. "
                f"Run \"{agent_command(found)} auth login\" on {machine} to use Check limits.")
    if not isinstance(found.get("features"), list):
        return (f"{agent} on {machine} can't do this yet: update the runner on {machine} "
                f"(it runs {runner.get('version') or '?'}).")
    return f"{agent} on {machine} can't check Claude plan limits (only Claude agents with a full login can)."


def cant_check_limits(runner: dict) -> list[str]:
    """The agents that report plan limits from their jobs but that Check limits can't ask (a
    long-lived token, or no usage_check settings). The Usage page lists them like the others, so a
    check that leaves them out says so, and why (usage_check_refusal)."""
    return [a["name"] for a in runner.get("agents") or []
            if isinstance(a, dict) and isinstance(a.get("name"), str) and agent_has(runner, a["name"], "limits")
            and not agent_has(runner, a["name"], "usage_check")]


def usage_check_params(runner: dict, wanted: str) -> dict:
    """The params of a usage.check job: the agents it checks, named explicitly (every eligible
    agent when none is asked for), and their accounts for the rate limit. Agents checked in the
    last 5 minutes, here or (by account) on any machine, are left out; 429 when none is left."""
    machine = runner["name"]
    names = list(dict.fromkeys(n.strip() for n in wanted.split(",") if n.strip()))
    if names:
        for agent in names:
            if find_agent(runner, agent) is None:
                raise HTTPException(400, f"Machine '{machine}' has no agent called '{agent}'.")
            if not agent_has(runner, agent, "usage_check"):
                raise HTTPException(400, usage_check_refusal(runner, agent))
    else:
        names = [a["name"] for a in runner.get("agents") or []
                 if isinstance(a, dict) and isinstance(a.get("name"), str) and agent_has(runner, a["name"], "usage_check")]
        if not names:
            why = " ".join(usage_check_refusal(runner, agent) for agent in cant_check_limits(runner))
            raise HTTPException(400, f"No agent on {machine} can check plan limits." + (f" {why}" if why else ""))
    now = time.time()
    checks = recent_usage_checks(now)
    keep, accounts, blocked = [], [], []
    for agent in names:
        # The label as the machine reports it, accepted or not: this is a rate limit, not routing.
        account = limits.limit_account(runner, agent)[0]
        last = last_usage_check(checks, machine, agent, account)
        if last is None and account not in accounts:
            keep.append(agent)
            accounts.append(account)
        elif last is not None:
            blocked.append(last)
    if not keep:
        last = min(blocked)  # the agent that can be checked again soonest
        raise HTTPException(429, f"Checked {_minutes(now - last)} ago (here or on another machine signed in to the "
                                 f"same account). Plan limits can be checked once every 5 minutes; try again in "
                                 f"{_minutes(last + USAGE_CHECK_EVERY - now)}.")
    return {"agents": ",".join(keep), "accounts": accounts}


def remote_control_refusal(runner: dict, agent: str) -> str:
    """Why an agent can't run Remote Control (it needs a full claude.ai login)."""
    machine = runner["name"]
    found = find_agent(runner, agent) or {}
    auth = found.get("auth")
    if not isinstance(found.get("features"), list):
        return (f"{agent} on {machine} can't do this yet: update the runner on {machine} "
                f"(it runs {runner.get('version') or '?'}).")
    if auth == "api_key":
        return (f"{agent} on {machine} signs in with an API key, and Remote Control works only with a Claude "
                "subscription login.")
    if auth == "token":
        return (f"{agent} signs in with a long-lived token (claude setup-token). Remote Control needs a full login: "
                f"run \"{agent_command(found)} auth login\" on {machine}.")
    if auth == "other":
        return (f"{agent} uses another service than Anthropic's own (Bedrock, Vertex, Foundry or another API "
                "address in ANTHROPIC_BASE_URL), and Remote Control works only with Anthropic's own service.")
    if found.get("preset") in ("claude", "selftest"):
        return (f"{agent} on {machine} can't do this yet: update the runner on {machine} "
                f"(it runs {runner.get('version') or '?'}).")
    return f"{agent} on {machine} can't run Remote Control (only Claude agents with a full login can)."


def remote_control_entries(runner: dict) -> list[dict]:
    """The Remote Control servers a machine reports (info.remote_control)."""
    info = runner.get("info") if isinstance(runner.get("info"), dict) else {}
    entries = info.get("remote_control")
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def remote_control_params(runner: dict, action: str, params: dict[str, str]) -> dict:
    """Check a Start or Stop Remote Control request; returns the job's params."""
    name = runner["name"]
    agent, project = params.get("agent", ""), params.get("project", "")
    if not agent or not project:
        raise HTTPException(400, "Pick an agent and a project.")
    pair = [e for e in remote_control_entries(runner) if e.get("agent") == agent and e.get("project") == project]
    if action == "rc.stop":
        # Stop always works, whatever the machine's setting says now.
        if not pair:
            raise HTTPException(404, f"Remote Control isn't running for {agent} in {project} on {name}.")
        return {"agent": agent, "project": project}
    if not runner_view(runner)["online"]:
        raise HTTPException(409, f"{name} is offline. Start Remote Control when it is back.")
    if find_agent(runner, agent) is None:
        raise HTTPException(400, f"Machine '{name}' has no agent called '{agent}'.")
    if not agent_has(runner, agent, "remote_control"):
        raise HTTPException(400, remote_control_refusal(runner, agent))
    if project not in (runner.get("projects") or []):
        raise HTTPException(400, f"Machine '{name}' has no project called '{project}'.")
    info = runner.get("info") if isinstance(runner.get("info"), dict) else {}
    if info.get("allow_remote_control") is not True:
        raise HTTPException(400, f"{name} doesn't allow Remote Control from the dashboard "
                                 "(allow_remote_control in its runner.json).")
    if any(e.get("state") in ("starting", "running") for e in pair):
        raise HTTPException(409, f"Remote Control is already running for {agent} in {project} on {name}.")
    for job in db.query("SELECT options FROM jobs WHERE runner=? AND kind='admin' AND status IN ('queued', 'running') "
                        "AND options LIKE '%rc.start%'", (name,)):
        opts = job["options"] if isinstance(job.get("options"), dict) else {}
        started = opts.get("params") if isinstance(opts.get("params"), dict) else {}
        if opts.get("action") == "rc.start" and started.get("agent") == agent and started.get("project") == project:
            raise HTTPException(409, f"Remote Control for {agent} in {project} on {name} is already starting.")
    return {"agent": agent, "project": project}


def queue_admin(runner: dict, action: str, params: dict[str, str]) -> int:
    name = runner["name"]
    info = runner.get("info") or {}
    projects = runner.get("projects") or []
    if action not in ADMIN_ACTIONS:
        raise HTTPException(400, f"Unknown action '{action}'.")
    if vtuple(runner.get("version")) < MIN_ADMIN_VERSION:
        raise HTTPException(400, f"{name} runs runner {runner.get('version') or '?'}, which can't do machine "
                                 "actions. Update it by hand once (see the guide); after that it can update itself.")
    params = {k: (v or "").strip() for k, v in params.items()}
    if action.startswith("project."):
        if not info.get("remote_projects"):
            raise HTTPException(400, f"{name} doesn't allow managing projects from the dashboard "
                                     "(turn on 'allow_remote_projects' in its runner.json).")
        pname = params.get("name", "")
        if not NAME_RE.match(pname):
            raise HTTPException(400, "Project names may use letters, digits, '.', '_' and '-' (max 64).")
        if action in ("project.clone", "project.create") and pname in projects:
            raise HTTPException(400, f"{name} already has a project called '{pname}'.")
        if action in ("project.remove", "project.pull") and pname not in projects:
            raise HTTPException(400, f"{name} has no project called '{pname}'.")
        if action == "project.remove" and pname == "chat":
            raise HTTPException(400, "The 'chat' project is built in.")
        if action == "project.clone" and not GIT_URL_RE.match(params.get("url", "")):
            raise HTTPException(400, "Use an https://, ssh:// or git@ repository address.")
    if action == "runner.update":
        if not info.get("remote_update"):
            raise HTTPException(400, f"{name} doesn't allow updates from the dashboard "
                                     "(turn on 'allow_remote_update' in its runner.json).")
        params = {"version": latest_runner_version()}
        if not params["version"]:
            raise HTTPException(500, "The hub has no runner files to hand out (see the install guide).")
        if db.query_one("SELECT 1 AS x FROM jobs WHERE runner=? AND kind='admin' AND status IN ('queued','running') "
                        "AND options LIKE '%\"runner.update\"%'", (name,)):
            raise HTTPException(400, f"An update for {name} is already on its way.")
    options: dict[str, Any] = {}
    project = params.get("name")
    notes: list[str] = []
    if action == "usage.check":
        if not params.get("agents"):
            # Every agent that can: the job's log says which ones it left out, and why.
            notes = [f"Not checked: {usage_check_refusal(runner, agent)}" for agent in cant_check_limits(runner)]
        params = usage_check_params(runner, params.get("agents", ""))
        label = params["agents"].replace(",", ", ")
    elif action in ("rc.start", "rc.stop"):
        params = remote_control_params(runner, action, params)
        label, project = f"{params['agent']} in {params['project']}", params["project"]
        if action == "rc.start":
            # Not picked up in time (the machine went offline): dropped, not started much later.
            options["expires_at"] = time.time() + RC_START_EXPIRES
    else:
        label = params.get("name") or params.get("version") or ""
    job_id = db.create_job(kind="admin", title=f"{ADMIN_ACTIONS[action]}: {label}".rstrip(": "),
                           runner=name, project=project,
                           options={"action": action, "params": params, "timeout_min": 30, **options})
    db.append_logs(job_id, notes)
    return job_id


@app.post("/api/runners/{name}/actions", dependencies=[Depends(require_user)])
def runner_action(name: str, body: AdminAction) -> dict:
    runner = db.runner(name)
    if runner is None:
        raise HTTPException(404, f"No machine called '{name}'.")
    return {"id": queue_admin(runner, body.action, body.params)}


class AcceptIn(BaseModel):
    tags: list[str] = []
    accounts: dict[str, str] = {}


@app.post("/api/runners/{name}/accept", dependencies=[Depends(require_user)])
def accept_runner(name: str, body: AcceptIn) -> dict:
    """Accept what a machine reports about itself, so it counts for tag jobs and subtasks. The
    posted values replace what was accepted before (leaving one out withdraws it). Each tag must
    be one the machine reports now or one already accepted, and each label the agent's label as
    reported now or as already accepted: otherwise the page was out of date (409)."""
    stale = f"{name} reports something else now. Reload the page and check again."
    with db.lock:
        runner = db.runner(name)
        if runner is None:
            raise HTTPException(404, f"No machine called '{name}'.")
        if len(body.tags) > 100 or len(body.accounts) > 200:
            raise HTTPException(400, "Accept at most 100 tags and 200 account labels at once.")
        before = accepted_of(runner)
        reported = set(reported_tags(runner))
        tags = []
        for raw in body.tags:
            tag = (raw or "").strip().lower()
            if not TAG_RE.fullmatch(tag):
                raise HTTPException(400, f"'{raw}' isn't a tag: tags are lowercase letters, digits, '.', '_' "
                                         "and '-'.")
            if tag not in reported and tag not in before["tags"]:
                raise HTTPException(409, stale)
            tags.append(tag)
        accounts = {}
        for agent, raw in body.accounts.items():
            label = (raw or "").strip()
            if not ACCOUNT_RE.fullmatch(label):
                raise HTTPException(400, f"'{raw}' isn't an account label.")
            now_label = (find_agent(runner, agent) or {}).get("account")
            if label != now_label and label != before["accounts"].get(agent):
                raise HTTPException(409, stale)
            accounts[agent] = label
        accepted = {"tags": sorted(set(tags)), "accounts": dict(sorted(accounts.items()))}
        db.set_accepted(name, accepted)
    log.info("accepted for %s: tags %s, account labels %s", name, ", ".join(accepted["tags"]) or "none",
             ", ".join(f"{a}={label}" for a, label in accepted["accounts"].items()) or "none")
    return {"ok": True, "runner": runner_view(db.runner(name))}


def admin_pending(action: str) -> set[str]:
    """The machines that already have an `action` job (e.g. "sync") waiting or running."""
    return {j["runner"] for j in db.query(
        "SELECT runner, options FROM jobs WHERE kind='admin' AND status IN ('queued', 'running')")
        if isinstance(j.get("options"), dict) and j["options"].get("action") == action}


def queue_for_all(action: str, skip_reason) -> dict:
    """Queue `action` on every machine that skip_reason(machine) doesn't skip. Returns the job ids,
    the machines they went to, and each skipped machine with the reason."""
    queued, machines, skipped, reasons = [], [], [], {}
    for r in db.runners():
        why = skip_reason(r)
        if why is None:
            continue  # nothing to do there (say, already up to date): not worth a mention
        if not why:
            try:
                queued.append(queue_admin(r, action, {}))
                machines.append(r["name"])
                continue
            except HTTPException as err:
                why = str(err.detail)
        skipped.append(r["name"])
        reasons[r["name"]] = why
    return {"queued": queued, "machines": machines, "skipped": skipped, "reasons": reasons}


@app.post("/api/runners-update-all", dependencies=[Depends(require_user)])
def update_all() -> dict:
    """Update every online machine whose runner is older than the hub's. Offline machines are
    skipped (and named): an update waiting for days would surprise whoever starts the machine."""
    latest = latest_runner_version()
    updating = admin_pending("runner.update")

    def skip(r: dict) -> str | None:
        if vtuple(r.get("version")) >= vtuple(latest):
            return None  # up to date, or newer than the hub
        if not runner_view(r)["online"]:
            return "offline"
        if not (r.get("info") or {}).get("remote_update"):
            return "remote updates are off"
        if vtuple(r.get("version")) < MIN_ADMIN_VERSION:
            return "too old to update itself"
        return "an update is already on its way" if r["name"] in updating else ""

    return {**queue_for_all("runner.update", skip), "version": latest}


@app.delete("/api/runners/{name}", dependencies=[Depends(require_user)])
def forget_runner(name: str) -> dict:
    # Its unfinished work can't report back any more: settle it now, so it doesn't stay "running"
    # for good, and let councils waiting on it carry on with their other seats (or stop).
    now = time.time()
    parents = set()
    for job in db.query("SELECT id, parent_id FROM jobs WHERE runner=? AND kind IN ('task', 'admin') "
                        "AND status IN ('queued', 'running')", (name,)):
        db.update_job(job["id"], status="cancelled", finished_at=now, error="The machine was forgotten.")
        if job["parent_id"]:
            parents.add(job["parent_id"])
    for parent in parents:
        councils.advance(parent)
    db.delete_runner(name)
    # A machine with its own token can't come back with it: adding it again needs a new code.
    revoked = db.revoke_machine_token(name)
    return {"ok": True, "token_revoked": revoked}


# --------------------------------------------------------------- shared memory
class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    scope: str = "global"
    tags: str = ""
    pinned: bool = False


class NoteUpdate(BaseModel):
    text: Optional[str] = None
    scope: Optional[str] = None
    tags: Optional[str] = None
    pinned: Optional[bool] = None


@app.get("/api/notes", dependencies=[Depends(require_user)])
def list_notes(q: str = "", scope: str = "", limit: int = 100) -> dict:
    return {"notes": memory.search(q, scope or None, limit), "pinned_chars": memory.pinned_size(),
            "pinned_warn": 12000, "context_version": memory.context_version()}


@app.post("/api/notes", dependencies=[Depends(require_user)])
def add_note(body: NoteIn) -> dict:
    try:
        return memory.add(body.text, body.scope, body.tags, body.pinned, source="dashboard")
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.put("/api/notes/{note_id}", dependencies=[Depends(require_user)])
def edit_note(note_id: int, body: NoteUpdate) -> dict:
    try:
        return memory.update(note_id, **{k: v for k, v in body.model_dump().items() if v is not None})
    except KeyError:
        raise HTTPException(404, "No such note")
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.delete("/api/notes/{note_id}", dependencies=[Depends(require_user)])
def delete_note(note_id: int) -> dict:
    if not memory.delete(note_id):
        raise HTTPException(404, "No such note")
    return {"ok": True}


@app.get("/api/notes/context", dependencies=[Depends(require_user)])
def preview_context() -> dict:
    return {"files": memory.context_files(), "version": memory.context_version()}


@app.post("/api/runners-sync-all", dependencies=[Depends(require_user)])
def sync_all() -> dict:
    """Ask every online machine to sync now. A machine that already has a sync waiting or running
    is skipped, so pressing the button twice doesn't queue every machine twice."""
    syncing = admin_pending("sync")

    def skip(r: dict) -> str:
        if not runner_view(r)["online"]:
            return "offline"
        if vtuple(r.get("version")) < MIN_ADMIN_VERSION:
            return "too old to sync from the dashboard"
        return "already syncing" if r["name"] in syncing else ""

    return queue_for_all("sync", skip)


# ------------------------------------------------------------ central MCP list
class McpServerIn(BaseModel):
    name: str
    transport: Literal["http", "stdio"] = "http"
    url: str = ""
    headers: dict[str, str] = {}
    command: str = ""
    args: list[str] = []
    env: dict[str, str] = {}
    targets: list[str] = ["claude", "gemini", "antigravity"]
    platforms: list[str] = ["linux", "macos", "windows"]
    enabled: bool = True
    note: str = ""
    route: Optional[Literal["hub", "direct"]] = None
    auth: Literal["headers", "oauth"] = "headers"
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scopes: str = ""
    oauth_auth_method: Literal["basic", "post"] = "basic"


def server_view(s: dict) -> dict:
    out = registry.masked(s)
    if s["auth"] == "oauth" and oauth is not None:
        out["oauth"] = oauth.view(s["id"])
    if s["route"] == "hub" and proxy is not None:
        out["health"] = proxy.health(s["name"])
    return out


# The MCP endpoints below are async on purpose: the OAuth manager and the proxy keep their state
# (pending sign-ins, token engines, connection pools) on the event loop, not in worker threads.
@app.get("/api/mcp", dependencies=[Depends(require_user)])
async def list_mcp() -> dict:
    token = mcp_token()
    return {"servers": [server_view(s) for s in registry.list()],
            "hub": {"enabled": hub_mcp is not None, "path": "/mcp", "token_hint": token[:4] + "…" + token[-4:],
                    "trust_access": hub_mcp is not None and ACCESS is not None,
                    "proxy": proxy is not None, "public_url": PUBLIC_URL, "public_url_problem": PUBLIC_URL_PROBLEM},
            "context_token": context_token()}


@app.get("/api/mcp/token", dependencies=[Depends(require_user)])
def reveal_mcp_token() -> dict:
    return {"token": mcp_token()}


@app.post("/api/mcp/token/rotate", dependencies=[Depends(require_user)])
def rotate_mcp_token() -> dict:
    if os.environ.get("HUB_MCP_TOKEN"):
        raise HTTPException(400, "The token is set by HUB_MCP_TOKEN in hub.env; change it there.")
    memory.set_setting("mcp_token", secrets.token_urlsafe(32))
    return {"ok": True, "context_token": context_token()}


@app.get("/api/mcp/{server_id}", dependencies=[Depends(require_user)])
def get_mcp(server_id: int) -> dict:
    server = registry.get(server_id)
    if server is None:
        raise HTTPException(404, "No such server")
    return server


def check_route(body: McpServerIn) -> None:
    if proxy is None and body.transport == "http" and body.route != "direct":
        raise HTTPException(400, "This hub can't route MCP servers through itself (the 'mcp' package is "
                                 "missing; see the install guide). Choose 'Directly from each machine'.")


@app.post("/api/mcp", dependencies=[Depends(require_user)])
async def add_mcp(body: McpServerIn) -> dict:
    check_route(body)
    try:
        return registry.save(body.model_dump())
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.put("/api/mcp/{server_id}", dependencies=[Depends(require_user)])
async def edit_mcp(server_id: int, body: McpServerIn) -> dict:
    check_route(body)
    before = registry.get(server_id)
    try:
        saved = registry.save(body.model_dump(), server_id)
    except KeyError:
        raise HTTPException(404, "No such server")
    except ValueError as err:
        raise HTTPException(400, str(err))
    if oauth is not None:
        if saved["auth"] != "oauth":
            oauth.forget(server_id)  # no longer signs in with OAuth: drop the stored sign-in
        else:
            oauth.settings_changed(server_id)  # a new address or client: cancel/drop the old sign-in
    if proxy is not None and before is not None:
        proxy.forget(before["name"])
    return saved


@app.delete("/api/mcp/{server_id}", dependencies=[Depends(require_user)])
async def delete_mcp(server_id: int) -> dict:
    server = registry.get(server_id)
    if server is None or not registry.delete(server_id):
        raise HTTPException(404, "No such server")
    if oauth is not None:
        oauth.forget(server_id)
    if proxy is not None:
        proxy.forget(server["name"])
    return {"ok": True}


class SignIn(BaseModel):
    origin: str = ""  # the address the dashboard is open at (location.origin)


def oauth_server(server_id: int) -> dict:
    server = registry.get(server_id)
    if server is None:
        raise HTTPException(404, "No such server")
    if oauth is None:
        raise HTTPException(400, "This hub has no OAuth support (the 'mcp' package is missing).")
    if server["auth"] != "oauth":
        raise HTTPException(400, f"{server['name']} doesn't use an OAuth sign-in.")
    return server


@app.post("/api/mcp/{server_id}/signin", dependencies=[Depends(require_user)])
async def mcp_sign_in(server_id: int, body: SignIn) -> JSONResponse:
    server = oauth_server(server_id)
    if not server["enabled"]:
        raise HTTPException(400, f"{server['name']} is turned off.")
    origin = body.origin.strip().rstrip("/")
    if PUBLIC_URL and origin and not same_origin(origin, PUBLIC_URL):
        # The provider sends the browser back to HUB_PUBLIC_URL, and the sign-in can only finish in
        # the browser (and at the address) where it started: the cookie below is set for this one.
        raise HTTPException(400, f"Open the hub at {PUBLIC_URL} to sign in: the provider sends your browser "
                                 "back there, and the sign-in has to finish where it started.")
    base = PUBLIC_URL or origin
    if not ORIGIN_RE.match(base) and not PUBLIC_URL:
        raise HTTPException(400, "Set HUB_PUBLIC_URL in hub.env (the address you open the hub at).")
    try:
        result = await oauth.start_sign_in(server_id, base + "/oauth/callback")
    except upstream_oauth.SignInError as err:
        raise HTTPException(502, str(err))
    binding = result.pop("binding", None)
    response = JSONResponse(result)
    if binding:
        # SameSite=Lax (the session cookie is Strict): it has to come along on the provider's
        # redirect back to /oauth/callback, which is a top-level navigation from another site.
        response.set_cookie(f"hub_oauth_{server_id}", binding, max_age=int(SIGNIN_TIMEOUT) + 60,
                            path="/oauth/callback", httponly=True, samesite="lax",
                            secure=base.startswith("https://"))
    return response


CALLBACK_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
                    "X-Content-Type-Options": "nosniff"}


@app.get("/oauth/callback", include_in_schema=False)
async def oauth_callback(request: Request) -> HTMLResponse:
    # No dashboard login here: the provider sends the browser back from another site, so the
    # (Strict) session cookie is not sent. The single-use state, the PKCE verifier kept on the
    # hub and the cookie set when "Sign in" was pressed protect it instead.
    params = dict(request.query_params)
    flow = oauth.flow_for_state(params.get("state", "")) if oauth is not None else None
    if oauth is None:
        status, message = 404, "This hub has no OAuth support."
    else:
        binding = request.cookies.get(f"hub_oauth_{flow.server_id}") if flow else None
        status, message = await oauth.finish_sign_in(params, binding)
    page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Sign-in</title><body style='font:16px system-ui,sans-serif;max-width:36em;margin:3em auto;"
            f"padding:0 1em'><p>{html.escape(message)}</p><p><a href='/'>Back to the hub</a></p></body>")
    response = HTMLResponse(page, status_code=status, headers=CALLBACK_HEADERS)
    if flow is not None and status != 403:
        response.delete_cookie(f"hub_oauth_{flow.server_id}", path="/oauth/callback")
    return response


@app.post("/api/mcp/{server_id}/signout", dependencies=[Depends(require_user)])
async def mcp_sign_out(server_id: int) -> dict:
    oauth_server(server_id)
    oauth.sign_out(server_id)
    return {"ok": True, "oauth": oauth.view(server_id)}


@app.post("/api/mcp/{server_id}/test", dependencies=[Depends(require_user)])
async def mcp_test(server_id: int) -> dict:
    """Ask the server for its tools, from the hub. For a server routed through the hub this is
    exactly what agents get (and refreshes it)."""
    server = registry.get(server_id)
    if server is None:
        raise HTTPException(404, "No such server")
    if server["transport"] != "http":
        raise HTTPException(400, "A local program (stdio) runs on each machine; the hub can't test it.")
    if proxy is None:
        raise HTTPException(400, "This hub can't test MCP servers (the 'mcp' package is missing).")
    try:
        upstream = mcp_proxy.Upstream(name=server["name"], url=server["url"], headers=server["headers"],
                                      call_timeout=PROXY_TIMEOUT,
                                      oauth_id=server_id if server["auth"] == "oauth" else None)
        tools = await proxy.test(upstream)
    except (ValueError, mcp_proxy.UpstreamUnavailable) as err:
        return {"ok": False, "error": str(err), "from": "hub"}
    return {"ok": True, "from": "hub", "tools": [{"name": t.name, "description": (t.description or "")[:200]}
                                                 for t in tools]}


@app.get("/api/usage", dependencies=[Depends(require_user)])
async def get_usage() -> dict:
    return await asyncio.to_thread(usage.summary)


@app.post("/api/usage/refresh", dependencies=[Depends(require_user)])
async def refresh_usage() -> dict:
    """Refresh on the Usage page: fetch API spend now, at most once a minute."""
    try:
        return await asyncio.to_thread(usage.summary, True)
    except usage.RefreshTooSoon as err:
        raise HTTPException(429, str(err))


class CreditOrgIn(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0, le=credits.MAX_AMOUNT)
    reset_day: Optional[int] = Field(default=None, ge=1, le=31)


class CreditSettingsIn(BaseModel):
    # Leave a field out to keep it. An organization's entry replaces its settings (null clears one).
    first: Optional[bool] = None
    orgs: dict[str, CreditOrgIn] = {}


@app.get("/api/credits", dependencies=[Depends(require_user)])
async def get_credits() -> dict:
    """Platform credits per organization (an estimate: see hub/credits.py), "credits first", and the
    machines with a credits agent."""
    cost = await asyncio.to_thread(usage.summary) if usage.org_names() else None
    views = credit_views(cost)
    return {"first": credit_settings()["first"], "orgs": views, "machines": credit_machines(views),
            "now": time.time(), "reserve_at": credits.RESERVE_AT}


@app.post("/api/credits", dependencies=[Depends(require_user)])
def save_credits(body: CreditSettingsIn) -> dict:
    """Change the credit settings: an organization's monthly amount and reset day, and "Use platform
    credits first for background work"."""
    names = usage.org_names()
    with db.lock:
        current = credit_settings()
        for name, value in body.orgs.items():
            org = credits.find_org(names, name)
            if org is None:
                raise HTTPException(400, f"'{name}' isn't an organization in HUB_ANTHROPIC_ADMIN_KEYS.")
            current["orgs"] = {k: v for k, v in current["orgs"].items() if k.lower() != org.lower()}
            current["orgs"][org] = credits.clean_org_setting(value.model_dump())
        if body.first is not None:
            current["first"] = body.first
        db.set_setting("credits", current)
    return {"ok": True, "first": current["first"], "orgs": credit_views()}


# Plan limits and tokens per job. Display only: nothing that picks, holds or moves work reads them.
@app.get("/api/usage/limits", dependencies=[Depends(require_user)])
def usage_limits() -> dict:
    """Each account's plan limits, from the figures the machines report (hub/limits.py)."""
    return limits.account_view(db.runners(), time.time())


@app.get("/api/usage/jobs", dependencies=[Depends(require_user)])
def usage_jobs(days: int = 30) -> dict:
    """Tokens and Claude Code's cost estimate per machine and agent, over the tasks that finished
    in the last `days` days (1-90), and how many Gemini jobs hit their quota."""
    days = max(1, min(days, 90))
    since = time.time() - days * 86400
    jobs = db.query("SELECT runner, agent, usage FROM jobs WHERE kind='task' AND finished_at >= ? "
                    "AND usage IS NOT NULL ORDER BY finished_at, id", (since,))
    hits = db.query_one("SELECT COUNT(*) AS n FROM jobs WHERE finished_at >= ? AND error_kind='quota'", (since,))["n"]
    return {"days": days, "gemini_quota_hits": hits, "rows": limits.usage_totals(jobs, db.runners())}


@app.post("/api/usage/check", dependencies=[Depends(require_user)])
def usage_check_all() -> dict:
    """Check limits now, for every account: on each online machine, the agents that can check
    their plan limits, one per account (the first machine by name whose agent and account weren't
    checked in the last 5 minutes). One machine action per machine. Like Sync all, it answers with
    the jobs queued and what was skipped, and why. An agent Check limits can't ask (cant_check_limits)
    is skipped as "<machine>/<agent>", with usage_check_refusal, only while the Usage page lists it as
    waiting for a reading and no agent that can be asked shares its account label: once its label
    has a reading, or another agent reads that label, there is nothing to explain."""
    now = time.time()
    checks = recent_usage_checks(now)
    runners = db.runners()
    waiting = {(w["runner"], w["agent"]) for w in limits.account_view(runners, now)["waiting"]}
    order: list[str] = []
    candidates: dict[str, list[tuple[dict, str]]] = {}
    cant: list[tuple[dict, str]] = []
    for r in runners:
        online = runner_view(r)["online"]
        cant += [(r, agent) for agent in cant_check_limits(r) if (r["name"], agent) in waiting]
        for a in r.get("agents") or []:
            if not (isinstance(a, dict) and isinstance(a.get("name"), str) and agent_has(r, a["name"], "usage_check")):
                continue
            account = limits.limit_account(r, a["name"])[0]
            if account not in candidates:
                order.append(account)
                candidates[account] = []
            if online:
                candidates[account].append((r, a["name"]))
    picks: dict[str, list[str]] = {}
    rows: dict[str, dict] = {}
    skipped, reasons = [], {}
    for account in order:
        last = last_usage_check(checks, None, None, account)
        choice = None
        if last is None:
            free = [(r, agent) for r, agent in candidates[account]
                    if last_usage_check(checks, r["name"], agent, None) is None]
            choice = free[0] if free else None
            if choice is None and candidates[account]:
                last = max(last_usage_check(checks, r["name"], agent, None) for r, agent in candidates[account])
        if choice is None:
            skipped.append(account)
            reasons[account] = f"checked {_minutes(now - last)} ago" if last is not None else "offline"
            continue
        r, agent = choice
        picks.setdefault(r["name"], []).append(agent)
        rows[r["name"]] = r
    queued, machines = [], []
    for name, agents in picks.items():
        try:
            queued.append(queue_admin(rows[name], "usage.check", {"agents": ",".join(agents)}))
            machines.append(name)
        except HTTPException as err:
            skipped.append(name)
            reasons[name] = str(err.detail)
    for r, agent in cant:
        if limits.limit_account(r, agent)[0] in candidates:
            continue  # its label is read (or was just now, or waits for a machine) through another agent
        skipped.append(f"{r['name']}/{agent}")
        reasons[f"{r['name']}/{agent}"] = usage_check_refusal(r, agent)
    return {"queued": queued, "machines": machines, "skipped": skipped, "reasons": reasons}


# ========================================================== adding machines
# "Add a machine" on the dashboard makes a one-time code; the command it shows fetches a
# bootstrap script from /enroll/<code>/..., which trades the code for the machine's own
# runner token. See enroll/__init__.py for the whole flow.
import enroll  # noqa: E402
from registry import PLATFORMS as MCP_PLATFORMS  # noqa: E402

# The address machines use to reach the hub (usually its LAN address), put into the commands
# "Add a machine" shows. Blank: the address the dashboard is open at.
RUNNER_URL = enroll.clean_hub_url(os.environ.get("HUB_RUNNER_URL", "")) or ""
if os.environ.get("HUB_RUNNER_URL", "").strip() and not RUNNER_URL:
    log.error("HUB_RUNNER_URL=%r is not an address like http://192.168.100.20:8080; it is ignored",
              os.environ["HUB_RUNNER_URL"])
enroll_failures = enroll.FailureLimiter()


def client_address(request: Request) -> str:
    return request.client.host if request.client else "?"


def lookup_code(raw: str, request: Request) -> tuple[dict | None, int, str]:
    """(code row, 0, '') for a code that can still be used, else (None, HTTP status, why).
    Failed look-ups count towards the caller's limit."""
    who = client_address(request)
    if enroll_failures.blocked(who):
        return None, 429, "Too many wrong or used codes from this address. Wait ten minutes and try again."
    code = enroll.normalize_code(raw)
    row = db.enroll_code(enroll.hash_secret(code)) if code else None
    if row is None:
        problem = 404, "This code isn't known to the hub. Copy the command again from Add a machine."
    elif row["used_at"]:
        problem = 410, ("This code was already used. Create a new one with Add a machine on the dashboard, "
                        "or run the setup script this machine saved in ~/.bullpen/setup.")
    elif row["expires_at"] <= time.time():
        problem = 410, "This code has expired (codes work for one hour). Create a new one with Add a machine."
    else:
        return row, 0, ""
    enroll_failures.failed(who)
    return None, *problem


def code_view(row: dict, code: str | None = None) -> dict:
    out = {"code_hint": row["code_hint"], "name": row["name"], "hub_url": row["hub_url"],
           "expires_in": max(0, int(row["expires_at"] - time.time())), "created_at": row["created_at"]}
    if code:  # only when it is made: the hub keeps no copy of the code itself
        pretty = enroll.pretty_code(code)
        out.update(code=pretty, linux=f"curl -fsSL '{row['hub_url']}/enroll/{pretty}/install.sh' | bash",
                   windows=f"irm '{row['hub_url']}/enroll/{pretty}/install.ps1' | iex")
    return out


class EnrollCodeIn(BaseModel):
    name: Optional[str] = None
    hub_url: Optional[str] = None


@app.post("/api/enroll-codes", dependencies=[Depends(require_user)])
def create_enroll_code(body: EnrollCodeIn, request: Request) -> dict:
    name = (body.name or "").strip() or None
    if name and not NAME_RE.match(name):
        raise HTTPException(400, "Machine names may use letters, digits, '.', '_' and '-' (max 64).")
    raw_url = (body.hub_url or "").strip() or RUNNER_URL or str(request.base_url)
    hub_url = enroll.clean_hub_url(raw_url)
    if hub_url is None:
        raise HTTPException(400, f"'{raw_url}' isn't an address machines can use, like http://192.168.100.20:8080.")
    active = {c["code_hint"] for c in db.active_enroll_codes()}
    code = enroll.normalize_code(enroll.new_code())
    while enroll.code_hint(code) in active:  # keep hints unique, so a code can be withdrawn by its hint
        code = enroll.normalize_code(enroll.new_code())
    row = db.add_enroll_code(enroll.hash_secret(code), enroll.code_hint(code), name, hub_url, enroll.CODE_TTL)
    out = code_view(row, code)
    out["expires_in"] = enroll.CODE_TTL
    return out


@app.get("/api/enroll-codes", dependencies=[Depends(require_user)])
def list_enroll_codes() -> dict:
    return {"codes": [code_view(c) for c in db.active_enroll_codes()]}


@app.delete("/api/enroll-codes/{code_hint}", dependencies=[Depends(require_user)])
def withdraw_enroll_code(code_hint: str) -> dict:
    if not enroll.HINT_RE.match(code_hint.upper()) or not db.delete_enroll_code(code_hint.upper()):
        raise HTTPException(404, "No unused code starts with that.")
    return {"ok": True}


SCRIPT_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def bootstrap_script(code: str, request: Request, kind: Literal["sh", "ps1"]) -> Response:
    row, status, why = lookup_code(code, request)
    if row is None:
        # Status 200 on purpose: `curl -f | bash` and `irm | iex` then show why, not just an HTTP error.
        return Response(enroll.problem_script(kind, why), media_type="text/plain; charset=utf-8",
                        headers=SCRIPT_HEADERS)
    files = enroll.bootstrap_files(RUNNER_DIST, kind)
    text = enroll.render(kind, row["hub_url"], enroll.normalize_code(code), VERSION, files,
                         int((row["expires_at"] - time.time()) // 60))
    return Response(text, media_type="text/plain; charset=utf-8", headers=SCRIPT_HEADERS)


@app.get("/enroll/{code}/install.sh", include_in_schema=False)
def enroll_install_sh(code: str, request: Request) -> Response:
    return bootstrap_script(code, request, "sh")


@app.get("/enroll/{code}/install.ps1", include_in_schema=False)
def enroll_install_ps1(code: str, request: Request) -> Response:
    return bootstrap_script(code, request, "ps1")


@app.get("/enroll/{code}/files/{fname}", include_in_schema=False)
def enroll_file(code: str, fname: str, request: Request) -> Response:
    row, status, why = lookup_code(code, request)
    if row is None:
        raise HTTPException(status, why)
    files = {**enroll.bootstrap_files(RUNNER_DIST, "sh"), **enroll.bootstrap_files(RUNNER_DIST, "ps1")}
    if fname not in files:
        raise HTTPException(404, f"The hub has no file called {fname} to hand out.")
    return Response(files[fname].read_bytes(), media_type="application/octet-stream", headers=SCRIPT_HEADERS)


class EnrollIn(BaseModel):
    code: str = Field(max_length=64)
    name: str = Field(default="", max_length=64)        # blank: the name given on the dashboard, else the hostname
    hostname: str = Field(default="", max_length=255)
    os: str = Field(default="", max_length=200)
    current_token: str = Field(default="", max_length=200)  # a re-install: this machine's token so far
    current_name: str = Field(default="", max_length=64)    # ...and its name, kept when it holds that name's token


@app.post("/api/enroll")
def enroll_machine(body: EnrollIn, request: Request) -> dict:
    """Trade a one-time code for this machine's own runner token. No dashboard login: the code is
    the proof. The token is returned once; the hub keeps only its sha256."""
    row, status, why = lookup_code(body.code, request)
    if row is None:
        raise HTTPException(status, why)
    if not body.hostname.strip():
        raise HTTPException(400, "Send the computer's hostname, so the hub can tell machines apart.")
    code_name, asked = row["name"] or "", body.name.strip()
    if code_name and asked and asked != code_name:
        # The name is part of what the dashboard made the code for; it can't be swapped on the way.
        raise HTTPException(409, f"This code was made for '{code_name}'. Leave the name out, or make a new code "
                                 "with Add a machine.")
    kept = ""
    if body.current_token and body.current_name:
        held = db.machine_token_hash(body.current_name.strip())
        if held and hmac.compare_digest(enroll.hash_secret(body.current_token), held):
            kept = body.current_name.strip()  # the same machine reconnecting keeps its name
    name = code_name or asked or kept or enroll.name_from_hostname(body.hostname)
    if not NAME_RE.match(name):
        raise HTTPException(400, f"'{name}' can't be a machine name: use letters, digits, '.', '_' and '-' (max 64).")
    token = enroll.new_token()
    # Check and write in one go, so two enrollments for one name can't both win.
    with db.lock:
        holder = db.machine_token_hash(name)
        same_install = bool(body.current_token and holder) and \
            hmac.compare_digest(enroll.hash_secret(body.current_token), holder)
        if holder and code_name != name and not same_install:
            # A machine with its own token is replaced only on purpose: a code made for exactly that
            # name ("Make a setup command" on its page), or the same install proving it holds the
            # token. A matching host name proves nothing (cloned VMs, default host names).
            raise HTTPException(409, f"'{name}' is already on the hub with its own token. To set that machine up "
                                     f"again, use 'Make a setup command' on its page. For another computer, pick "
                                     f"another name, or Forget '{name}' first.")
        if not holder and code_name != name:
            # A machine still on the shared token: only the same computer may move to its own token
            # without a code made for its name.
            known = enroll.short_host((db.runner(name) or {}).get("hostname"))
            if known and enroll.short_host(body.hostname) != known:
                raise HTTPException(409, f"Another computer ({known}) is already on the hub as '{name}'. Give this "
                                         f"one another name, or Forget '{name}' on the dashboard first.")
        if not db.use_enroll_code(row["code_hash"], name):
            raise HTTPException(410, "This code was used a moment ago. Create a new one with Add a machine.")
        db.set_machine_token(name, enroll.hash_secret(token), body.hostname.strip()[:255], body.os.strip()[:200])
    log.info("machine %s (%s) enrolled with its own token", name, body.hostname)
    return {"name": name, "token": token, "hub_url": row["hub_url"]}


class WhoAmI(BaseModel):
    name: str = Field(default="", max_length=64)  # the name the runner means to use (optional)


@app.post("/api/runner/whoami")
def runner_whoami(body: WhoAmI = WhoAmI(), auth: RunnerAuth = Depends(require_runner)) -> dict:
    """Lets a runner check its token: which kind it is and which machine it is for. With a name, it
    also says whether this token may act for that machine (a shared token can't act for a machine
    that has its own)."""
    if body.name.strip() and auth.kind != "machine":
        check_runner_name(auth, body.name.strip())
    return {"auth": auth.kind, "name": auth.name, "hub_version": VERSION}


def name_conflict(prev: dict | None, hostname: str) -> str | None:
    """Another computer that checked in under this name within the online window, if any."""
    if prev is None or not prev.get("hostname") or not hostname:
        return None
    if enroll.short_host(prev["hostname"]) == enroll.short_host(hostname):
        return None  # the same computer, perhaps with another domain suffix today
    return prev["hostname"] if time.time() - (prev.get("last_seen") or 0) < ONLINE_WINDOW else None


# =================================================================== runners
class Heartbeat(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    hostname: str = ""
    os: str = ""
    version: str = ""
    agents: list[dict] = []
    projects: list[str] = []
    max_parallel: int = 1
    running: list[int] = []
    info: dict = {}
    # The machine's tags (cleaned and stored as machine_tags; they count once accepted) and the
    # machine's clock when it sent this, for the clock difference shown on the dashboard.
    # Accepted loosely: a heartbeat refused over a bad tag would make the machine look offline.
    tags: list[Any] = []
    sent_at: float = 0


class RunnerNote(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    scope: str = "global"
    tags: str = ""
    pinned: bool = False
    source: str = ""


class RunnerSearch(BaseModel):
    query: str = ""
    scope: str = ""
    limit: int = 20


@app.post("/api/runner/notes/add", dependencies=[Depends(require_runner)])
def runner_add_note(body: RunnerNote) -> dict:
    # Notes from machines (agents, the bullpen command) can only ask for a pin; the user decides.
    try:
        return memory.add(body.text, body.scope, body.tags, pinned=False, source=body.source or "runner",
                          request_pin=body.pinned)
    except ValueError as err:
        raise HTTPException(400, str(err))


@app.post("/api/runner/notes/search", dependencies=[Depends(require_runner)])
def runner_search_notes(body: RunnerSearch) -> dict:
    return {"notes": memory.search(body.query, body.scope or None, body.limit)}


class ContextAsk(BaseModel):
    features: list[str] = []  # what the runner understands; "hub-route": servers relayed by the hub


@app.post("/api/runner/context", dependencies=[Depends(require_runner)])
def runner_context(body: ContextAsk = ContextAsk()) -> dict:
    """Everything a runner wires into its agents: pinned notes, the hub's memory MCP
    server, the central MCP list and the agent add-ons."""
    # Servers relayed by the hub have no address of their own in this list; a runner that doesn't
    # know that would write them with an empty URL, so it doesn't get them (until it updates).
    relay = proxy is not None and "hub-route" in body.features
    servers = [s for s in registry.for_runners() if s["route"] != "hub" or relay]
    return {"version": context_token(), "files": memory.context_files(),
            "mcp_enabled": hub_mcp is not None, "mcp_token": mcp_token(),
            "servers": servers, "kit": KIT,
            # The platform names servers use. A runner on a Mac matches "macos" only when the hub
            # lists it here; otherwise it takes the "linux" servers.
            "platforms": list(MCP_PLATFORMS)}


@app.post("/api/runner/bundle", dependencies=[Depends(require_runner)])
def bundle() -> dict:
    """The runner files, for the runner.update action."""
    return runner_bundle()


@app.post("/api/runner/heartbeat")
def heartbeat(body: Heartbeat, auth: RunnerAuth = Depends(require_runner)) -> dict:
    check_runner_name(auth, body.name)
    prev = db.runner(body.name)
    clash = name_conflict(prev, body.hostname)
    db.upsert_runner(body.model_dump(), auth=auth.kind)
    if clash:
        db.note_name_conflict(body.name, clash)
        log.warning("two computers check in as %s: %s and %s", body.name, clash, body.hostname)
    recent_clash = clash or (prev and prev.get("conflict_at")
                             and time.time() - prev["conflict_at"] < NAME_CONFLICT_WINDOW and prev.get("conflict_host"))
    if not recent_clash:
        # With two computers on one name, each would report the other's jobs as missing.
        mark_lost_jobs(body.name, body.running)
    cancel = [j["id"] for j in db.query(
        "SELECT id FROM jobs WHERE runner=? AND status='running' AND cancel_requested=1", (body.name,))]
    return {"ok": True, "cancel": cancel, "hub_version": VERSION, "context_version": context_token(),
            "auth": auth.kind, "name_conflict": recent_clash or None}


class Poll(BaseModel):
    name: str
    admin_only: bool = False


def claim_offer(name: str, auth: RunnerAuth) -> dict:
    """What a machine offers for claiming jobs (Database.claim_next_job), from its last check-in:
    whether this poll came with its own runner token, its accepted tags, each agent's account key
    and features, its projects, and whether it takes subtasks."""
    row = db.runner(name) or {"name": name}
    agents = {a["name"]: {"account": account_key(row, a["name"]), "features": agent_features(row, a["name"])}
              for a in row.get("agents") or [] if isinstance(a, dict) and isinstance(a.get("name"), str)}
    # Platform credits first: the machine's credits agent, while its organization is available.
    plan = credits.plan(row, usage.org_names(), credit_states()) if credit_settings()["first"] else None
    # The poll's own token decides (a machine with its own token can't poll with the shared one).
    return {"auth": "machine" if auth.kind == "machine" else "shared", "tags": effective_tags(row), "agents": agents,
            "projects": runner_projects(row), "accept_delegated": takes_subtasks(row), "credits": plan}


@app.post("/api/runner/poll")
async def poll(body: Poll, auth: RunnerAuth = Depends(require_runner)) -> Response:
    check_runner_name(auth, body.name)
    deadline = time.monotonic() + LONG_POLL_SECONDS
    updating_seen = False
    while True:
        # While the runner installs an update it gets no new work, and as soon as the
        # update finishes we answer right away so it can restart without losing a job.
        if db.update_in_progress(body.name):
            updating_seen = True
            job = None
        elif updating_seen:
            return Response(status_code=204)
        else:
            # What the machine offers is read again on every pass, not once per poll: a tag or an
            # account label the owner withdraws, or one the machine stops reporting, stops counting
            # at once rather than when this poll ends.
            job = db.claim_next_job(body.name, body.admin_only, claim_offer(body.name, auth))
        if job:
            out = {
                "id": job["id"], "kind": job["kind"], "agent": job["agent"], "project": job["project"],
                "autonomy": job["autonomy"], "prompt": job["prompt"], "model": job.get("model"),
                "resume_session": job["resume_session"], "options": job["options"] or {},
            }
            if job["kind"] == "task":
                out.update(task_extras(job, body.name))
            return JSONResponse({"job": out})
        if time.monotonic() >= deadline:
            return Response(status_code=204)
        await asyncio.sleep(1)


def approval_timeout_s() -> int:
    """How long an 'ask' job waits for the owner's answer (HUB_APPROVAL_TIMEOUT_MIN)."""
    return math.ceil(hub_approvals.timeout_s)


def task_extras(job: dict, runner_name: str) -> dict:
    """What a runner gets with a task beyond the job itself (keys left out when they don't apply;
    older runners ignore keys they don't know). A job token only for a job that needs more than
    job_info (an 'ask' job, or one that may hand out subtasks), on a hub with the job tools, for an
    agent that can use them. An ordinary job gets none, and its command line stays as it was."""
    extra: dict[str, Any] = {}
    opts = job["options"] if isinstance(job.get("options"), dict) else {}
    if opts.get("live"):
        extra["live"] = True
        extra["idle_min"] = opts.get("idle_min") or 15
    if job.get("delegated_by"):
        extra["delegated_by"] = job["delegated_by"]
    tools = job_tools_for(job)
    if len(tools) > 1 and hub_job_mcp is not None and agent_has(db.runner(runner_name), job["agent"], "job_mcp"):
        token = "bpj_" + secrets.token_urlsafe(32)
        # Only the fingerprint is kept; the token itself goes to the runner once, in this answer.
        db.update_job(job["id"], token_hash=hashlib.sha256(token.encode()).hexdigest())
        extra["job_token"] = token
        extra["job_tools"] = tools
        if "approve" in tools:
            extra["approval_timeout_s"] = approval_timeout_s()
    return extra


class LogChunk(BaseModel):
    lines: list[str]
    # Live sessions. Accepted loosely and checked below: a log upload refused over a field would
    # leave the runner unable to report the job.
    after_message: Any = 0      # the highest message id the runner has received
    turns: Any = []             # [{"turn_no": 1, "result": "...", "is_error": false}], one per finished turn
    session_open: Any = None    # False once the runner has closed the session


def clean_turns(raw: Any) -> list[dict]:
    """The agent turns a runner sent, checked: whole turn numbers from 1, the answer as text (at
    most MAX_TURN_CHARS), is_error as a bool. Anything else is left out."""
    out = []
    for t in raw[:100] if isinstance(raw, list) else []:
        if not isinstance(t, dict):
            continue
        n = t.get("turn_no")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 1_000_000:
            continue
        result = t.get("result")
        result = result if isinstance(result, str) else ""
        out.append({"turn_no": n, "result": result[:MAX_TURN_CHARS], "is_error": t.get("is_error") is True})
    return out


@app.post("/api/runner/jobs/{job_id}/log")
def runner_log(job_id: int, body: LogChunk, auth: RunnerAuth = Depends(require_runner)) -> dict:
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    check_runner_job(auth, job)
    if body.lines:
        db.append_logs(job_id, body.lines)
    out: dict[str, Any] = {"cancel": bool(job["cancel_requested"])}
    opts = job["options"] if isinstance(job.get("options"), dict) else {}
    if job["kind"] != "task" or not opts.get("live"):
        return out
    # A live session: store the agent's answers (a batch sent again after a lost reply adds
    # nothing), note which messages the runner has, and hand it the ones it hasn't.
    for turn in clean_turns(body.turns):
        db.add_message(job_id, "agent", turn["result"], turn["is_error"], turn["turn_no"])
    after = body.after_message if isinstance(body.after_message, int) and not isinstance(body.after_message, bool) \
        and body.after_message > 0 else 0
    db.mark_delivered(job_id, after)
    closed = body.session_open is False
    if closed and not job["end_requested"]:
        db.execute("UPDATE jobs SET end_requested=1 WHERE id=?", (job_id,))  # no more messages from now on
    out["end"] = bool(job["end_requested"]) or closed
    out["messages"] = ([] if closed or job["status"] != "running" or job["cancel_requested"]
                       else db.messages_after(job_id, after))
    return out


class Finish(BaseModel):
    status: Literal["done", "error", "cancelled"]
    exit_code: Optional[int] = None
    result: Optional[str] = None
    error: Optional[str] = None
    diff: Optional[str] = None
    session_id: Optional[str] = None
    # Accepted loosely and checked in runner_finish: a finish refused over a field the hub doesn't
    # know would leave the runner retrying it for half an hour.
    usage: Any = None          # the tokens the agent reported (runner/runner.py UsageTracker.total)
    error_kind: Any = None     # "claude_limit" or "quota" at a usage limit, "credits" when credits ran out


# The error kinds the dashboard shows in their own way; anything else a runner sends is dropped.
ERROR_KINDS = ("claude_limit", "quota", "credits")


def usage_baseline(job: dict) -> dict | None:
    """The figures the job a follow-up continues reported: the newest other job of that session
    with figures, by when it finished. (A follow-up always runs on the same machine and agent, so
    a job on another machine is never taken for it.)"""
    row = db.query_one("SELECT usage_reported FROM jobs WHERE session_id=? AND id != ? AND runner=? AND agent=? "
                       "AND usage_reported IS NOT NULL ORDER BY finished_at DESC, id DESC LIMIT 1",
                       (job["resume_session"], job["id"], job["runner"], job["agent"]))
    return row["usage_reported"] if row and isinstance(row["usage_reported"], dict) else None


@app.post("/api/runner/jobs/{job_id}/finish")
def runner_finish(job_id: int, body: Finish, auth: RunnerAuth = Depends(require_runner)) -> dict:
    job = db.job(job_id)
    if job is None:
        raise HTTPException(404, "No such job")
    check_runner_job(auth, job)
    if job["status"] in ("done", "error", "cancelled"):
        return {"ok": True}  # already settled (for example marked lost earlier)
    fields: dict[str, Any] = {"error_kind": body.error_kind if body.error_kind in ERROR_KINDS else None}
    if body.status == "error" and fields["error_kind"] == "credits":
        mark_credits_refused(job)
        # A council step or subtask moved to the credits agent runs again, once, on the agent it named.
        if job.get("named_agent") and not job.get("resume_session") \
                and db.requeue_on_named_agent(job_id, body.error or ""):
            db.append_logs(job_id, [f"↻ Platform credits used up: running again on {job['named_agent']}, "
                                    "the agent this step named"])
            return {"ok": True}
    reported, own = limits.sanitize_usage(body.usage)
    if reported is not None:
        # What the agent reported, and this job's own share of it (a follow-up's share leaves out
        # the conversation before it).
        resumed = bool(job.get("resume_session"))
        baseline = usage_baseline(job) if resumed and own is None and reported.get("source") != "gemini" else None
        fields.update(usage_reported=reported, usage=limits.usage_share(reported, own, baseline, resumed))
    db.update_job(job_id, status=body.status, exit_code=body.exit_code, result=body.result,
                  error=body.error, diff=body.diff, session_id=body.session_id,
                  finished_at=time.time(), **fields)
    if job["parent_id"]:
        councils.advance(job["parent_id"])
    if body.status in ("done", "error") and top_level_task(job):
        notifier.send(body.status, {**job, "status": body.status})
    if body.status == "error" and fields["error_kind"] == "claude_limit" and top_level_task(job) \
            and credit_settings()["first"]:
        try:
            retry_on_credits(db.job(job_id) or job)
        except HTTPException as err:
            log.info("job %s stopped at a usage limit; not retried on credits: %s", job_id, err.detail)
    return {"ok": True}


@app.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"ok": True, "version": VERSION}


# ============================================================ the job tools
# Built last, once everything they use exists (approvals, the target checks, cancel_one and
# runner_view for subtasks). The lifespan above starts hub_job_mcp when it is set.
hub_approvals = Approvals(db, notifier)
# Questions still pending were asked of the hub process before this one: nobody waits for them.
hub_approvals.expire_leftovers()
# A job that ends or is being cancelled wakes its waiting questions at once ("The job ended.").
db.on_job_ended = hub_approvals.wake_job
hub_delegation = None
if hub_mcp is not None and job_mcp is not None:
    hub_delegation = Delegation(db, runner_view, check_target, check_tag_target, cancel_one,
                                lambda job_id, line: db.append_logs(job_id, [line]), account_key,
                                check_model=check_model)
    hub_job_mcp = job_mcp.JobMcp(db, hub_approvals, hub_delegation)
    app.router.routes.extend(hub_job_mcp.routes())  # exactly /mcp/job (and /mcp/job/): McpAuth lets it through
