"""Subtasks: an agent running a hub task hands work to agents on the owner's other machines.

The owner allows it per task ("May hand subtasks to other machines" on New task). Such a task
gets the job tools list_machines, delegate_task, task_status and cancel_task at /mcp/job
(hub/job_mcp.py), which call the four methods of Delegation below.

The owner's scope for a task (stored in its options):
  delegate_to        "machine:<name>" / "tag:<tag>" entries; empty: every machine that takes subtasks
  delegate_projects  "same": only projects named like the task's own, and chat; "any": every project
  delegate_accounts  absent: any of the owner's accounts (the default since the owner's decision of
                     2026-11-06); a list: the task's own account and these account keys only
  delegate_agents    absent: any agent; a list: only agents with these names

Rules kept here, whatever the scope says:
  * Depth 1: a subtask never hands out subtasks of its own.
  * A subtask may do at most what its parent may do, and at most HUB_DELEGATE_MAX_AUTONOMY
    (read only unless the owner raises it on purpose).
  * Subtasks go only to machines that check in with their own runner token, that take subtasks
    (accept_delegated_tasks in their runner.json), and only to agents whose autonomy levels the hub
    can check (the `levels` feature).
  * At most HUB_DELEGATE_MAX_CHILDREN subtasks per job, HUB_DELEGATE_MAX_ACTIVE queued or running
    at once. The checks and the new job are one step under the database lock, so parallel calls
    can't both slip under a cap, nor past the owner's Cancel.
  * A subtask's time ends with its parent's (its time limit here, `not_after` at claim time).
  * After a usage limit (error_kind claude_limit or quota) in the parent's conversation (the job
    and the earlier turns it continues) or on a subtask of any of them, a new subtask may use only
    the account that hit it: usage limits aren't a reason to switch accounts.
  * The hub never picks an agent: the parent names it, and for a tag the account it runs under is
    fixed when the subtask is made (the claim never hands it to another account).

Imports nothing from app.py and nothing from the `mcp` package: app.py hands in what it needs.
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from typing import Any

from common import AUTONOMY_RANK, MODEL_RE, NAME_RE, TAG_RE, account_key, agent_features, effective_tags, \
    own_token, permission_flags, runner_projects, tag_job_fits, takes_subtasks

log = logging.getLogger("hub.delegation")


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a whole number; using %d", name, raw, default)
        return default
    return min(max(value, low), high)


def _autonomy_env() -> str:
    raw = os.environ.get("HUB_DELEGATE_MAX_AUTONOMY", "").strip().lower()
    if raw and raw not in AUTONOMY_RANK:
        log.warning("HUB_DELEGATE_MAX_AUTONOMY=%r is not one of %s; subtasks may only read",
                    raw, ", ".join(AUTONOMY_RANK))
        return "readonly"
    return raw or "readonly"


MAX_CHILDREN = _int_env("HUB_DELEGATE_MAX_CHILDREN", 5, 1, 20)   # subtasks per job in all
MAX_ACTIVE = _int_env("HUB_DELEGATE_MAX_ACTIVE", 3, 1, 10)       # subtasks queued or running at once
MAX_AUTONOMY = _autonomy_env()                                    # the most any subtask may do
MAX_PROMPT_CHARS = 100_000
MAX_RESULT_CHARS = 60_000          # a subtask's answer as task_status returns it
DEFAULT_TIMEOUT_MIN = 60
LIMIT_KINDS = ("claude_limit", "quota")
# A usage limit in the parent's conversation older than this no longer pins its subtasks to that
# account (the longest Claude plan window is a week).
LIMIT_MEMORY_S = 7 * 86400
THREAD_MAX = 50
ACTIVE = ("queued", "running")

# "Subtasks on this hub may only <words>".
AUTONOMY_WORDS = {"readonly": "read", "edits": "read and edit files",
                  "ask": "edit files and run commands you approve", "full": "do anything"}

NOT_YOURS = "Job #{id} isn't one of your subtasks."
OWN_ACCOUNT_LIMIT = ("Usage limits aren't a reason to switch accounts; wait for the reset (guide 1).")


class DelegationError(Exception):
    """A subtask request the hub refuses. The message is one plain sentence for the agent."""


def _opts(job: dict | None) -> dict:
    opts = (job or {}).get("options")
    return opts if isinstance(opts, dict) else {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _whole(value: Any) -> int | None:
    """A whole number from a tool argument (JSON numbers like 5.0 count), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _recent_limit(job: dict, since: float) -> bool:
    """Did this job end with a usage limit at `since` or later?"""
    return job.get("error_kind") in LIMIT_KINDS and (job.get("finished_at") or 0) >= since


def _target_words(entry: str) -> str:
    kind, _, name = entry.partition(":")
    return f"any machine tagged {name}" if kind == "tag" else name


class Delegation:
    """The subtask tools' logic. All methods are synchronous (JobMcp calls them in a worker thread).

    `runner_view(row)` adds "online" to a runner row; `check_target(machine, agent, project)` and
    `check_tag_target(tag, agent, project, account, needs=..., delegated=...)` raise when a job
    can't go there (an HTTPException's detail or a ValueError's text becomes the agent's
    message), the latter returning the account key; `cancel_one(job)` cancels a job as the
    dashboard's Cancel does; `add_log(job_id, line)` adds a line to a job's log;
    `account_key(row, agent)` is common.account_key."""

    def __init__(self, db: Any, runner_view: Callable[[dict], dict], check_target: Callable[..., Any],
                 check_tag_target: Callable[..., str], cancel_one: Callable[[dict], None],
                 add_log: Callable[[int, str], None], account_key: Callable[[dict | None, str], str] = account_key,
                 *, check_model: Callable[[str | None], str | None] | None = None,
                 max_children: int | None = None, max_active: int | None = None,
                 max_autonomy: str | None = None) -> None:
        self.db = db
        self.runner_view = runner_view
        self.check_target = check_target
        self.check_tag_target = check_tag_target
        self.cancel_one = cancel_one
        self.add_log = add_log
        self.account_key = account_key
        self.check_model = check_model
        self.max_children = max_children or MAX_CHILDREN
        self.max_active = max_active or MAX_ACTIVE
        self.max_autonomy = max_autonomy if max_autonomy in AUTONOMY_RANK else MAX_AUTONOMY

    # ----------------------------------------------------------------- scope
    def own_key(self, parent: dict) -> str:
        """The parent's own account key: the one it was made for when it was aimed at a tag, else
        its agent's account on the machine running it."""
        account = _opts(parent).get("account")
        if parent.get("target_tag") and isinstance(account, str) and account:
            return account
        return self.account_key(self.db.runner(parent.get("runner") or ""), parent.get("agent") or "")

    def scope(self, parent: dict) -> dict:
        """The owner's scope for this task's subtasks, with the account rule worked out."""
        opts = _opts(parent)
        to = [e for e in opts.get("delegate_to") or [] if isinstance(e, str)]
        accounts = opts.get("delegate_accounts")
        agents = opts.get("delegate_agents")
        own = self.own_key(parent)
        children = self.db.subtasks(parent["id"])
        return {
            "to": to,
            "projects": "any" if opts.get("delegate_projects") == "any" else "same",
            "project": parent.get("project") or "chat",
            # None: any account (and any agent); otherwise the keys (names) allowed.
            "accounts": None if not isinstance(accounts, list) else
                        sorted({own, *(a for a in accounts if isinstance(a, str))}),
            "agents": None if not isinstance(agents, list) else sorted({a for a in agents if isinstance(a, str)}),
            "own": own,
            "limit": self._limit_keys(parent, own, children),
            "children": children,
        }

    def _limit_keys(self, parent: dict, own: str, children: list[dict]) -> list[str]:
        """The accounts that hit a usage limit in this task's conversation: its own account when
        the job or an earlier turn it continues hit one, then the account of each subtask that hit
        one, its own subtasks first and then those of the earlier turns. The earlier turns, and
        their subtasks, count for LIMIT_MEMORY_S (by then the limit has reset)."""
        since = time.time() - LIMIT_MEMORY_S
        earlier = self._earlier_turns(parent)
        keys: list[str] = []
        if parent.get("error_kind") in LIMIT_KINDS or any(_recent_limit(row, since) for row in earlier):
            keys.append(own)
        older: list[dict] = []
        if earlier:
            ids = [row["id"] for row in earlier]
            older = [row for row in self.db.query(
                f"SELECT options, error_kind, finished_at FROM jobs WHERE delegated_by IN ({','.join('?' * len(ids))}) "
                "ORDER BY id", tuple(ids)) if _recent_limit(row, since)]
        for child in [*children, *older]:
            key = _opts(child).get("account")
            if child.get("error_kind") in LIMIT_KINDS and isinstance(key, str) and key and key not in keys:
                keys.append(key)
        return keys

    def _earlier_turns(self, parent: dict) -> list[dict]:
        """The jobs this one continues (follow_up_of, newest first, at most THREAD_MAX): {"id",
        "follow_up_of", "error_kind", "finished_at"} each."""
        turns: list[dict] = []
        prev, seen = parent.get("follow_up_of"), {parent["id"]}
        while prev and prev not in seen and len(seen) <= THREAD_MAX:
            seen.add(prev)
            row = self.db.query_one("SELECT id, follow_up_of, error_kind, finished_at FROM jobs WHERE id=?", (prev,))
            if row is None:
                break
            turns.append(row)
            prev = row["follow_up_of"]
        return turns

    def _thread_hit_limit(self, parent: dict) -> bool:
        """Did an earlier turn of this conversation hit a usage limit, within LIMIT_MEMORY_S?"""
        since = time.time() - LIMIT_MEMORY_S
        return any(_recent_limit(row, since) for row in self._earlier_turns(parent))

    @staticmethod
    def _in_to(scope: dict, row: dict) -> bool:
        """Is this machine inside "Subtasks may go to" (named, or carrying an allowed tag)?"""
        if not scope["to"]:
            return True
        tags = set(effective_tags(row))
        return any(e == f"machine:{row['name']}" or (e.startswith("tag:") and e[4:] in tags) for e in scope["to"])

    @staticmethod
    def _project_ok(scope: dict, project: str) -> bool:
        return scope["projects"] == "any" or project in (scope["project"], "chat")

    @staticmethod
    def _account_ok(scope: dict, key: str) -> bool:
        if scope["limit"]:
            return key in scope["limit"]
        return scope["accounts"] is None or key in scope["accounts"]

    @staticmethod
    def _agent_ok(scope: dict, agent: str) -> bool:
        return scope["agents"] is None or agent in scope["agents"]

    def _busy(self, machine: str) -> int:
        return self.db.query_one("SELECT COUNT(*) AS n FROM jobs WHERE runner=? AND kind='task' "
                                 "AND status='running'", (machine,))["n"]

    # ------------------------------------------------------------- the tools
    def machines(self, parent: dict) -> dict:
        """list_machines: the machines, agents and projects this task may hand subtasks to. Machines
        outside the scope aren't listed at all; no host names, paths or tokens."""
        scope = self.scope(parent)
        out = []
        for row in self.db.runners():
            if not own_token(row) or not takes_subtasks(row) or not self._in_to(scope, row):
                continue
            projects = [p for p in runner_projects(row) if self._project_ok(scope, p)]
            agents = []
            for a in row.get("agents") or []:
                if not isinstance(a, dict) or not isinstance(a.get("name"), str):
                    continue
                key = self.account_key(row, a["name"])
                if "levels" not in agent_features(row, a["name"]) or not self._agent_ok(scope, a["name"]) \
                        or not self._account_ok(scope, key):
                    continue
                agents.append({"name": a["name"], "label": a.get("label") or a["name"], "account": key,
                               "kind": a.get("preset") or "", "ready": a.get("ready") is not False,
                               "features": agent_features(row, a["name"])})
            if not agents or not projects:
                continue
            view = self.runner_view(row)
            info = row.get("info") if isinstance(row.get("info"), dict) else {}
            out.append({"name": row["name"], "online": bool(view.get("online")), "os": row.get("os") or "",
                        "platform": info.get("platform") or "", "tags": effective_tags(row),
                        "busy": self._busy(row["name"]), "slots": int(row.get("max_parallel") or 1),
                        "agents": agents, "projects": projects})
        done = len(scope["children"])
        active = sum(1 for c in scope["children"] if c["status"] in ACTIVE)
        about = {
            "this_task": {"machine": parent.get("runner"), "agent": parent.get("agent"),
                          "project": parent.get("project"), "account": scope["own"],
                          "autonomy": parent.get("autonomy")},
            "projects": ("any project" if scope["projects"] == "any"
                         else f"only '{scope['project']}' and chat"),
            "accounts": "any of the user's accounts" if scope["accounts"] is None else scope["accounts"],
            "most_a_subtask_may_do": self._most(parent),
            "subtasks_left": max(0, self.max_children - done),
            "may_start_now": max(0, min(self.max_active - active, self.max_children - done)),
        }
        if scope["limit"]:
            about["usage_limit"] = (f"A usage limit was hit on {', '.join(scope['limit'])}, so new subtasks may use "
                                    "only that account. " + OWN_ACCOUNT_LIMIT)
        return {"ok": True, "machines": out, "scope": about}

    def _most(self, parent: dict) -> str:
        mine = parent.get("autonomy") if parent.get("autonomy") in AUTONOMY_RANK else "readonly"
        return min(mine, self.max_autonomy, key=lambda a: AUTONOMY_RANK[a])

    def delegate(self, parent: dict, args: dict) -> dict:
        """delegate_task: check everything and queue the subtask, as one step under the database
        lock (so parallel calls and the owner's Cancel can't race the checks). Raises
        DelegationError with the reason."""
        with self.db.lock:
            return self._delegate(parent["id"], args if isinstance(args, dict) else {})

    def _delegate(self, parent_id: int, args: dict) -> dict:
        # 0. The parent as it is now: still running, and not being cancelled.
        parent = self.db.job(parent_id)
        if parent is None or parent["status"] != "running" or parent["cancel_requested"]:
            raise DelegationError("This job has ended.")
        # 1. Allowed by the owner, and not a subtask itself (depth 1).
        if not _opts(parent).get("delegate") or parent.get("delegated_by") or parent.get("kind") != "task":
            raise DelegationError("This job can't hand out subtasks.")
        # 2. What and where.
        agent, prompt = _text(args.get("agent")), args.get("prompt")
        if not NAME_RE.fullmatch(agent):
            raise DelegationError("Name the agent for the subtask, as list_machines shows it.")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            raise DelegationError(f"Give the subtask a prompt of 1 to {MAX_PROMPT_CHARS:,} characters.")
        machine, tag = _text(args.get("machine")), _text(args.get("tag")).lower()
        if bool(machine) == bool(tag):
            raise DelegationError("Name a machine or a tag (not both).")
        if machine and not NAME_RE.fullmatch(machine):
            raise DelegationError(f"'{machine}' isn't a machine name list_machines shows.")
        if tag and not TAG_RE.fullmatch(tag):
            raise DelegationError(f"'{tag}' isn't a tag list_machines shows.")
        # 3. What it may do.
        autonomy = _text(args.get("autonomy")) or "readonly"
        if autonomy not in AUTONOMY_RANK:
            raise DelegationError("autonomy must be one of: " + ", ".join(AUTONOMY_RANK) + ".")
        mine = parent.get("autonomy") if parent.get("autonomy") in AUTONOMY_RANK else "readonly"
        if AUTONOMY_RANK[autonomy] > AUTONOMY_RANK[mine]:
            raise DelegationError(f"A subtask can't do more than this job ({mine}).")
        if AUTONOMY_RANK[autonomy] > AUTONOMY_RANK[self.max_autonomy]:
            raise DelegationError(f"Subtasks on this hub may only {AUTONOMY_WORDS[self.max_autonomy]}; the owner "
                                  "sets that with HUB_DELEGATE_MAX_AUTONOMY.")
        # 4. How many.
        scope = self.scope(parent)
        children = scope["children"]
        if len(children) >= self.max_children:
            raise DelegationError(f"This job has already handed out {len(children)} subtasks (the most allowed).")
        active = sum(1 for c in children if c["status"] in ACTIVE)
        if active >= self.max_active:
            raise DelegationError(f"{active} subtasks are still running; wait for one to finish.")
        # 5. The owner's scope: where, and in which project.
        if scope["to"]:
            allowed = machine and self._in_to(scope, self.db.runner(machine) or {"name": machine}) \
                or (tag and f"tag:{tag}" in scope["to"])
            if not allowed:
                where = ", ".join(_target_words(e) for e in scope["to"])
                raise DelegationError(f"This task may hand subtasks only to {where}. The owner sets that on New task.")
        project = _text(args.get("project")) or parent.get("project") or "chat"
        if not NAME_RE.fullmatch(project):
            raise DelegationError(f"'{project}' isn't a project name list_machines shows.")
        if not self._project_ok(scope, project):
            raise DelegationError(f"This task may hand out subtasks only in the project '{scope['project']}' "
                                  "(and chat). The owner can allow any project on New task.")
        # 6. The target can take it.
        needs = ["levels"] + (["approvals"] if autonomy == "ask" else [])
        if machine:
            self._call(self.check_target, machine, agent, project)
            row = self.db.runner(machine) or {}
            if not own_token(row):
                raise DelegationError(f"{machine} still uses the shared runner token, and subtasks only go to machines "
                                      "with their own token.")
            if not takes_subtasks(row):
                raise DelegationError(f"{machine} doesn't take subtasks from other agents.")
            features = agent_features(row, agent)
            flags = permission_flags(row, agent)
            if "levels" not in features and flags:
                raise DelegationError(
                    f"{agent} on {machine} can't take subtasks: its runner.json gives it permission flags outside its "
                    f"levels ({', '.join(flags)}), which would win over the subtask's level.")
            if "levels" not in features:
                raise DelegationError(
                    f"{agent} on {machine} can't take subtasks: the hub can't limit what it may do (its runner.json "
                    "overrides the autonomy levels, or it isn't a Claude, Gemini or self-test agent).")
            if autonomy == "ask" and "approvals" not in features:
                raise DelegationError(f"{agent} on {machine} can't ask the owner before commands, so its subtask "
                                      "can't use 'ask'.")
            key = self.account_key(row, agent)
            asked = _text(args.get("account"))
            if asked and asked != key:
                raise DelegationError(f"'{agent}' on {machine} is the account '{key}', not '{asked}'.")
            where = machine
        else:
            key = self._call(self.check_tag_target, tag, agent, project, _text(args.get("account")) or None,
                             needs=needs, delegated=True)
            where = f"machines tagged '{tag}'"
        # 7. Which agents and accounts the owner allows, and no switch after a usage limit.
        if not self._agent_ok(scope, agent):
            raise DelegationError(f"Subtasks of this task may use only the agents {', '.join(scope['agents'])}. "
                                  "The owner can allow more on New task.")
        if scope["accounts"] is not None and key not in scope["accounts"]:
            raise DelegationError(f"Subtasks of this task may use only {', '.join(scope['accounts'])}. '{agent}' on "
                                  f"{where} is the account '{key}'. The owner can allow more accounts on New task.")
        if scope["limit"] and key not in scope["limit"]:
            first = scope["limit"][0]
            hit = (f"This task's own conversation hit a usage limit on {first}." if first == scope["own"]
                   and (parent.get("error_kind") in LIMIT_KINDS or self._thread_hit_limit(parent))
                   else f"A subtask on {first} hit a usage limit.")
            raise DelegationError(f"{hit} {OWN_ACCOUNT_LIMIT}")
        # 8. A subtask that could only wait for the slot its own parent holds.
        if machine:
            candidates = [machine]
        else:
            candidates = [r["name"] for r in self.db.runners()
                          if tag_job_fits(r, tag, agent, project, key, needs, delegated=True)]
        home = parent.get("runner")
        if candidates and all(c == home for c in candidates):
            row = self.db.runner(home) or {}
            if self._busy(home) >= int(row.get("max_parallel") or 1):
                raise DelegationError(f"{home} is busy with this job; hand the subtask to another machine.")
        # 9. Model and time: a subtask's time ends with its parent's.
        model = _text(args.get("model")) or None
        if model is not None:
            if self.check_model is not None:
                model = self._call(self.check_model, model)
            elif not MODEL_RE.fullmatch(model):
                raise DelegationError(f"'{model}' doesn't look like a model name.")
        now = time.time()
        deadline = (parent.get("started_at") or now) + self._timeout_min(parent) * 60
        left = math.ceil((deadline - now) / 60)
        if left < 1:
            raise DelegationError("Your own time limit is nearly up; finish instead.")
        asked_min = args.get("timeout_min")
        if asked_min is None:
            timeout_min = min(left, DEFAULT_TIMEOUT_MIN)
        else:
            whole = _whole(asked_min)
            if whole is None or not 1 <= whole <= 1440:
                raise DelegationError("timeout_min must be a whole number of minutes from 1 to 1440.")
            timeout_min = min(whole, left)
        # 10. Create it.
        title = _text(args.get("title")) or prompt.strip().splitlines()[0]
        intro = (f"This task comes from another AI agent (job #{parent['id']}, {parent.get('agent')} on "
                 f"{parent.get('runner')}), not directly from the user. Do only what it asks, within your own "
                 "permissions. Your final answer goes back to that agent.\n\n")
        options = {"timeout_min": timeout_min, "not_after": deadline, "capture_diff": autonomy != "readonly",
                   "delegated": True, "account": key, "needs": needs}
        child_id = self.db.create_job(
            kind="task", title=f"Subtask of #{parent['id']}: " + title[:60], runner=machine or None,
            target_tag=tag or None, agent=agent, project=project, autonomy=autonomy, model=model,
            delegated_by=parent["id"], prompt=intro + prompt, options=options)
        # 11. Say so in the parent's log.
        target = machine or f"any machine tagged {tag}"
        self.add_log(parent["id"], f"↳ Handed subtask #{child_id} to {agent} on {target}")
        log.info("job %s handed subtask %s to %s on %s", parent["id"], child_id, agent, target)
        return {"ok": True, "job_id": child_id, "status": "queued", "target": target}

    @staticmethod
    def _timeout_min(job: dict) -> float:
        try:
            value = float(_opts(job).get("timeout_min") or DEFAULT_TIMEOUT_MIN)
        except (TypeError, ValueError):
            value = float(DEFAULT_TIMEOUT_MIN)
        return value if value > 0 else float(DEFAULT_TIMEOUT_MIN)

    @staticmethod
    def _call(check: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run one of app.py's checks; its refusal (an HTTPException's detail, or a ValueError)
        becomes the agent's message."""
        try:
            return check(*args, **kwargs)
        except DelegationError:
            raise
        except Exception as err:  # noqa: BLE001 - only refusals are turned into messages
            detail = getattr(err, "detail", None)
            if detail is None and not isinstance(err, ValueError):
                raise
            raise DelegationError(str(detail if detail is not None else err)) from None

    def _child(self, parent: dict, job_id: Any) -> dict:
        whole = _whole(job_id)
        child = self.db.job(whole) if whole is not None else None
        if child is None or child.get("delegated_by") != parent["id"]:
            raise DelegationError(NOT_YOURS.format(id=job_id))
        return child

    def status(self, parent: dict, job_id: Any) -> dict:
        """task_status (one look; JobMcp does the waiting): where a subtask is, and its answer once
        it has finished."""
        child = self._child(parent, job_id)
        out: dict[str, Any] = {
            "ok": True, "job_id": child["id"], "status": child["status"], "machine": child.get("runner"),
            "agent": child.get("agent"), "title": child.get("title"), "started_at": child.get("started_at"),
            "finished_at": child.get("finished_at"),
            "waiting_for_approval": self.db.query_one(
                "SELECT 1 AS x FROM approvals WHERE job_id=? AND status='pending' LIMIT 1", (child["id"],)) is not None,
            "error_kind": child.get("error_kind"),
        }
        if child["status"] == "done":
            result = child.get("result") or ""
            out["result"] = result[:MAX_RESULT_CHARS] + ("…" if len(result) > MAX_RESULT_CHARS else "")
        if child["status"] in ("error", "cancelled"):
            out["error"] = child.get("error")
        return out

    def cancel(self, parent: dict, job_id: Any) -> dict:
        """cancel_task: cancel one of this job's subtasks, as the dashboard's Cancel does."""
        child = self._child(parent, job_id)
        if child["status"] in ACTIVE:
            self.cancel_one(child)
            self.add_log(parent["id"], f"↳ Cancelled subtask #{child['id']}")
        now = self.db.job(child["id"]) or child
        out = {"ok": True, "job_id": child["id"], "status": now["status"]}
        if now["status"] == "running" and now.get("cancel_requested"):
            out["note"] = "Its machine has been asked to stop it."
        return out
