"""The job tools: a second MCP server inside the hub, at /mcp/job.

An agent running a hub job can reach the hub here, as that job and nothing else. Only a job
that needs more than `job_info` gets a job token when its runner picks it up: an 'ask' job,
which asks the owner on the dashboard before commands (`approve`), or a task the owner lets
hand out subtasks (`list_machines`, `delegate_task`, `task_status`, `cancel_task`). Ordinary
jobs get no token, and their command line is what it always was.

Authentication: `Authorization: Bearer <job token>` and nothing else. The hub keeps only the
token's sha256 (jobs.token_hash). The token works while its job runs, isn't being cancelled,
is within its time limit plus 5 minutes, and its machine checks in (Database.job_for_token).
The shared MCP token and Cloudflare Access tokens don't work here, and McpAuth lets this path
through untouched for that reason (see `exempt` in mcp_server.McpAuth).

One low-level `mcp.server.Server`, served by a stateless StreamableHTTPSessionManager like
mcp_proxy.py. `json_response=False`: an SSE response sends its headers at once, so a long call
(an approval, a wait for a subtask) doesn't trip Claude Code's 60-second first-byte timer.
Those calls also send an MCP progress notification every PROGRESS_EVERY_S, because Claude Code
gives up on a call with no response or progress for 5 minutes (SSE keepalive comments don't
count).

Tested with mcp 2.3.0.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

import anyio
import mcp_types as types
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from common import job_tools_for
from delegation import DelegationError

log = logging.getLogger("hub.job_mcp")

# The job id of the request being served (the ASGI scope carries it too, and is read first).
JOB: contextvars.ContextVar[int | None] = contextvars.ContextVar("bullpen_job", default=None)
SCOPE_KEY = "bullpen.job_id"
# What agents call this server: the key in the job's --mcp-config file and in Gemini's settings.
# No underscore, because Gemini CLI splits tool names on the first one.
SERVER_NAME = "bullpenjob"
PATH = "/mcp/job"
# Seconds between MCP progress notifications during a long call.
PROGRESS_EVERY_S = 30
# task_status with wait_s: how often it looks at the subtask again, and how often it tells the
# agent it is still waiting. A wait is at most MAX_WAIT_S, well under Claude Code's 2-minute
# automatic backgrounding of long MCP calls (whose result is lost in -p mode).
STATUS_EVERY_S = 2
STATUS_PROGRESS_S = 20
MAX_WAIT_S = 60

_ALWAYS_LOAD = {"anthropic/alwaysLoad": True}  # never deferred behind Claude Code's tool search

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "job_info": {"type": "object", "properties": {}},
    "approve": {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                       "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"],
    },
    "list_machines": {"type": "object", "properties": {}},
    "delegate_task": {
        "type": "object",
        "properties": {
            "agent": {"type": "string"},
            "prompt": {"type": "string"},
            "machine": {"type": "string"},
            "tag": {"type": "string"},
            "project": {"type": "string", "description": "Default: this job's project."},
            "autonomy": {"type": "string", "enum": ["readonly", "edits", "ask", "full"], "default": "readonly"},
            "account": {"type": "string"},
            "model": {"type": "string"},
            "title": {"type": "string"},
            "timeout_min": {"type": "integer", "minimum": 1, "maximum": 1440},
        },
        "required": ["agent", "prompt"],
    },
    "task_status": {
        "type": "object",
        "properties": {"job_id": {"type": "integer"}, "wait_s": {"type": "integer", "minimum": 0, "maximum": 60}},
        "required": ["job_id"],
    },
    "cancel_task": {"type": "object", "properties": {"job_id": {"type": "integer"}}, "required": ["job_id"]},
}

# The model reads these; keep the wording.
DESCRIPTIONS = {
    "job_info": ("Which hub job you are running as: its id, machine, agent, project, autonomy, and which hub "
                 "tools this job may use."),
    "approve": ("Used by Claude Code as its permission prompt tool. Asks the user on the hub's dashboard whether "
                "a tool call may run, and waits for the answer. Don't call it yourself."),
    "list_machines": ("The machines and projects the user lets this task hand subtasks to: online or not, "
                      "operating system, tags, how busy, and which agents (with their accounts) and projects each "
                      "offers. Use it before delegate_task."),
    "delegate_task": ("Hand a self-contained subtask to an agent on one of the user's machines that list_machines "
                      "shows. Name the machine OR a tag (any online machine with that tag), and always name the "
                      "agent. Returns a job id at once; the subtask runs in the background. Then call task_status "
                      "with wait_s=60 until it has finished, and stop before your own time limit. A subtask may do "
                      "at most what you may do, and only reads unless you ask for more. It may use only the "
                      "accounts the user allowed for this task. Usage limits aren't a reason to switch accounts: "
                      "if a subtask hits one, wait for the reset."),
    "task_status": ("Status of one of your subtasks, and its answer once it has finished. With wait_s, waits up to "
                    "that many seconds for it to finish before answering."),
    "cancel_task": "Cancel one of your subtasks.",
}

TOOLS: dict[str, types.Tool] = {
    name: types.Tool(name=name, description=DESCRIPTIONS[name], input_schema=schema, _meta=_ALWAYS_LOAD)
    for name, schema in TOOL_SCHEMAS.items()
}

NOT_AVAILABLE = "This tool isn't available to this job."


def ok(data: Mapping[str, Any]) -> types.CallToolResult:
    """A tool's answer: one text block holding JSON, with "ok": true."""
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps({"ok": True, **data}))])


def fail(sentence: str) -> types.CallToolResult:
    """A tool's refusal: an error result whose JSON says why, in one plain sentence."""
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps({"ok": False, "error": sentence}))], is_error=True)


class _AsgiEndpoint:
    """Starlette treats functions and bound methods as request/response endpoints; only another
    kind of callable is mounted as a raw ASGI app, hence this small wrapper."""

    def __init__(self, handler: Callable[[Scope, Receive, Send], Awaitable[None]]) -> None:
        self.handler = handler

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.handler(scope, receive, send)


Handler = Callable[[Any, dict, dict], Awaitable[types.CallToolResult]]


class JobMcp:
    """The /mcp/job endpoint. `approvals` (hub/approvals.py) and `delegation` (hub/delegation.py)
    provide the tools beyond job_info; while one of them is None, its tools answer that they
    aren't available."""

    def __init__(self, db: Any, approvals: Any = None, delegation: Any = None) -> None:
        self.db, self.approvals, self.delegation = db, approvals, delegation
        self.handlers: dict[str, Handler] = {"job_info": self._job_info}
        if approvals is not None:
            self.handlers["approve"] = self._approve
        if delegation is not None:
            self.handlers.update(list_machines=self._list_machines, delegate_task=self._delegate_task,
                                 task_status=self._task_status, cancel_task=self._cancel_task)
        self.server: Server[Any] = Server(
            "bullpen-job",
            version="1.0",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
            get_tool_input_schema=lambda name: TOOL_SCHEMAS.get(name),
        )
        self.manager = StreamableHTTPSessionManager(
            app=self.server,
            json_response=False,
            stateless=True,
            # The endpoint checks the job token itself; the Host check would block LAN and Cloudflare names.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )

    # ------------------------------------------------------------- ASGI side
    def routes(self) -> list[Route]:
        # The trailing-slash twin is served directly: a redirect would not be followed for a POST.
        endpoint = _AsgiEndpoint(self._endpoint)
        return [Route(path, endpoint=endpoint, methods=["GET", "POST", "DELETE"]) for path in (PATH, PATH + "/")]

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Enter from the app's lifespan (a mounted app's own lifespan never runs)."""
        async with self.manager.run():
            yield

    async def _endpoint(self, scope: Scope, receive: Receive, send: Send) -> None:
        headers = Headers(scope=scope)
        scheme, _, cred = headers.get("authorization", "").partition(" ")
        cred = cred.strip()
        job = None
        if scheme.lower() == "bearer" and cred:
            digest = hashlib.sha256(cred.encode("utf-8", "surrogateescape")).hexdigest()
            job = await anyio.to_thread.run_sync(self.db.job_for_token, digest)
        if job is None:
            await JSONResponse({"error": "invalid_token"}, status_code=401,
                               headers={"WWW-Authenticate": 'Bearer realm="bullpen-job"'})(scope, receive, send)
            return
        scope = {**scope, SCOPE_KEY: job["id"]}
        token = JOB.set(job["id"])
        try:
            await self.manager.handle_request(scope, receive, send)
        finally:
            JOB.reset(token)

    # ---------------------------------------------------------- MCP handlers
    async def _job(self, ctx: Any) -> dict:
        """The job this request is for, read again now. A job that has ended or is being
        cancelled can't use its tools any more, even within one request."""
        request = getattr(ctx, "request", None)
        scope = getattr(request, "scope", None) or {}
        job_id = scope.get(SCOPE_KEY) or JOB.get()
        job = await anyio.to_thread.run_sync(self.db.job, job_id) if job_id else None
        if job is None or job["status"] != "running" or job["cancel_requested"]:
            raise MCPError(code=INVALID_REQUEST, message="This job has ended.")
        return job

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        job = await self._job(ctx)
        return types.ListToolsResult(tools=[TOOLS[name] for name in job_tools_for(job) if name in TOOLS])

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        job = await self._job(ctx)
        handler = self.handlers.get(params.name)
        if params.name not in job_tools_for(job) or handler is None:
            return fail(NOT_AVAILABLE)
        args = params.arguments if isinstance(params.arguments, dict) else {}
        return await handler(ctx, job, args)

    async def _progress(self, ctx: Any, n: int, message: str) -> None:
        """Tell the agent a long call is still going (only when it asked for progress). Never raises:
        a client that went away is noticed by the call itself."""
        try:
            meta = getattr(ctx, "meta", None)
            if not isinstance(meta, Mapping):
                return
            token = meta.get("progress_token", meta.get("progressToken"))
            if token is None:
                return
            await ctx.session.send_progress_notification(token, n, None, message, related_request_id=ctx.request_id)
        except Exception as err:  # noqa: BLE001
            log.debug("progress notification not sent: %s", err)

    # ------------------------------------------------------------------ tools
    async def _approve(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        """Claude Code's permission prompt tool: ask the owner, wait, and answer with Claude's
        permission format in the text. Never an error result: Claude reads the decision from the
        text, and an error would read as a failed call rather than a denial."""
        async def progress(n: int, message: str) -> None:
            await self._progress(ctx, n, message)

        decision = await self.approvals.request(job, args.get("tool_name"), args.get("input"),
                                                args.get("tool_use_id"), progress)
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(decision))])

    async def _job_info(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        subtasks = await anyio.to_thread.run_sync(self.db.subtasks, job["id"])
        return ok({"job_id": job["id"], "title": job["title"], "machine": job["runner"], "agent": job["agent"],
                   "project": job["project"], "autonomy": job["autonomy"], "tools": job_tools_for(job),
                   "subtasks": [s["id"] for s in subtasks]})

    # ------------------------------------------------------------- subtasks
    async def _delegation(self, call: Callable[..., dict], *args: Any) -> types.CallToolResult:
        """Run one of Delegation's methods in a worker thread; a refusal becomes an error result."""
        try:
            return ok(await anyio.to_thread.run_sync(call, *args))
        except DelegationError as err:
            return fail(str(err))

    async def _list_machines(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        return await self._delegation(self.delegation.machines, job)

    async def _delegate_task(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        return await self._delegation(self.delegation.delegate, job, args)

    async def _cancel_task(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        return await self._delegation(self.delegation.cancel, job, args.get("job_id"))

    async def _task_status(self, ctx: Any, job: dict, args: dict) -> types.CallToolResult:
        """A subtask's status. With wait_s (0-60), looks again every STATUS_EVERY_S until it has
        finished, the wait is up or this job ends, telling the agent every STATUS_PROGRESS_S that
        it is still waiting."""
        raw = args.get("wait_s")
        wait_s = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw == raw else 0
        wait_s = min(max(float(wait_s), 0.0), float(MAX_WAIT_S))
        try:
            status = await anyio.to_thread.run_sync(self.delegation.status, job, args.get("job_id"))
        except DelegationError as err:
            return fail(str(err))
        start = anyio.current_time()
        next_progress, n = start + STATUS_PROGRESS_S, 0
        while status["status"] in ("queued", "running") and anyio.current_time() - start < wait_s:
            await anyio.sleep(min(STATUS_EVERY_S, wait_s - (anyio.current_time() - start)))
            mine = await anyio.to_thread.run_sync(self.db.job, job["id"])
            if mine is None or mine["status"] != "running" or mine["cancel_requested"]:
                return fail("This job has ended.")
            try:
                status = await anyio.to_thread.run_sync(self.delegation.status, job, args.get("job_id"))
            except DelegationError as err:
                return fail(str(err))
            if anyio.current_time() >= next_progress:
                next_progress += STATUS_PROGRESS_S
                n += 1
                await self._progress(ctx, n, f"waiting for subtask #{status['job_id']}")
        return ok(status)
