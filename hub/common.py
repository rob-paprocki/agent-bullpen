"""Small things several hub modules share.

This module imports nothing from app.py and nothing from the `mcp` package, so every other
module (and the hub's unit tests) can use it without pulling in the web app.
"""

from __future__ import annotations

import re
from typing import Any, Literal

# What a job may do, from least to most: read only, edit files, edit files and ask the owner
# before commands, or do anything.
Autonomy = Literal["readonly", "edits", "ask", "full"]
AUTONOMY_RANK = {"readonly": 0, "edits": 1, "ask": 2, "full": 3}

# Seconds since its last heartbeat for a runner to count as online.
ONLINE_WINDOW = 45
# Seconds before a job the runner stopped reporting is marked lost.
LOST_JOB_GRACE = 60

# Machine, agent and project names.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# Model names a job may ask for.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]@-]{0,99}$")
# Machine tags: short lowercase words such as gpu or build-box.
TAG_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,31}")
# Account labels from runner.json. Shown on the dashboard, never put into SQL text.
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,39}")
# An agent's own account key when it has no accepted label: "<machine>/<agent>".
MACHINE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}/[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
# Tags a runner adds for its operating system. They count without being accepted.
PLATFORM_TAGS = {"linux", "macos", "windows"}
# Tags kept per machine.
MAX_TAGS = 16

# The tools at /mcp/job that let a job hand work to other machines.
DELEGATION_TOOLS = ["list_machines", "delegate_task", "task_status", "cancel_task"]
# Job fields the dashboard and the agents never see.
HIDDEN_JOB_FIELDS = ("token_hash",)


def vtuple(version: str | None) -> tuple:
    """'1.2.0' -> (1, 2, 0); anything unparsable sorts lowest."""
    try:
        return tuple(int(x) for x in (version or "").split(".")[:3])
    except ValueError:
        return (0,)


def clean_tags(raw: Any) -> list[str]:
    """The tags a machine reports, cleaned: lowercase words matching TAG_RE, without repeats,
    sorted, at most MAX_TAGS. Takes a list, or one string with commas or spaces between tags."""
    if isinstance(raw, str):
        raw = re.split(r"[\s,]+", raw)
    if not isinstance(raw, (list, tuple, set)):
        return []
    words = {t.strip().lower() for t in raw if isinstance(t, str)}
    return sorted(t for t in words if TAG_RE.fullmatch(t))[:MAX_TAGS]


def accepted_of(runner: dict | None) -> dict:
    """What the owner accepted for a machine: {"tags": [...], "accounts": {agent: label}}."""
    acc = (runner or {}).get("accepted")
    acc = acc if isinstance(acc, dict) else {}
    tags, accounts = acc.get("tags"), acc.get("accounts")
    return {
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        "accounts": ({k: v for k, v in accounts.items() if isinstance(k, str) and isinstance(v, str)}
                     if isinstance(accounts, dict) else {}),
    }


def reported_tags(runner: dict | None) -> list[str]:
    """The tags a machine reported at its last check-in (a database row's machine_tags)."""
    tags = (runner or {}).get("machine_tags")
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def effective_tags(runner: dict | None) -> list[str]:
    """The tags that count for routing: the platform tag the machine reports, and every other
    tag it reports that the owner has accepted. A tag the machine stops reporting stops counting."""
    accepted = set(accepted_of(runner)["tags"])
    return sorted({t for t in reported_tags(runner) if t in PLATFORM_TAGS or t in accepted})


def to_accept(runner: dict | None) -> dict:
    """What a machine reports that the owner hasn't accepted yet: {"tags": [...], "accounts":
    {agent: label}}, or {} when nothing waits. Platform tags count without being accepted."""
    accepted = accepted_of(runner)
    tags = [t for t in reported_tags(runner) if t not in PLATFORM_TAGS and t not in accepted["tags"]]
    accounts = {}
    for a in (runner or {}).get("agents") or []:
        if not isinstance(a, dict) or not isinstance(a.get("name"), str):
            continue
        label = a.get("account")
        if isinstance(label, str) and ACCOUNT_RE.fullmatch(label) and accepted["accounts"].get(a["name"]) != label:
            accounts[a["name"]] = label
    return {"tags": tags, "accounts": accounts} if tags or accounts else {}


def account_key(runner: dict | None, agent: str) -> str:
    """Which account an agent counts as: its label when the owner has accepted exactly that
    label, otherwise its own key "<machine>/<agent>" (no label, or a label not accepted yet)."""
    runner = runner or {}
    label = None
    for a in runner.get("agents") or []:
        if isinstance(a, dict) and a.get("name") == agent:
            label = a.get("account")
            break
    accepted = accepted_of(runner)["accounts"].get(agent)
    if isinstance(label, str) and label and ACCOUNT_RE.fullmatch(label) and label == accepted:
        return label
    return f"{runner.get('name') or ''}/{agent}"


def valid_account_key(key: Any) -> bool:
    """An account key: an account label, or "<machine>/<agent>" for an agent without one."""
    return isinstance(key, str) and bool(ACCOUNT_RE.fullmatch(key) or MACHINE_KEY_RE.fullmatch(key))


def agent_entry(runner: dict | None, agent: str) -> dict | None:
    """The agent's entry in the machine's last check-in, if it has one by that name."""
    for a in (runner or {}).get("agents") or []:
        if isinstance(a, dict) and a.get("name") == agent:
            return a
    return None


def agent_features(runner: dict | None, agent: str) -> list[str]:
    """What the agent reports it can do (section 4.1 of the runner's heartbeat); [] when nothing."""
    features = (agent_entry(runner, agent) or {}).get("features")
    return [f for f in features if isinstance(f, str)] if isinstance(features, list) else []


FLAG_RE = re.compile(r"-{1,2}[A-Za-z][A-Za-z0-9-]{0,39}")


def permission_flags(runner: dict | None, agent: str) -> list[str]:
    """The permission flags outside its levels that the agent's runner.json gives it (--permission-mode,
    --yolo...), as the runner reports them: why it takes no subtasks and can't ask before commands."""
    flags = (agent_entry(runner, agent) or {}).get("permission_flags")
    if not isinstance(flags, list):
        return []
    return [f for f in flags if isinstance(f, str) and FLAG_RE.fullmatch(f)][:10]


def own_token(runner: dict | None) -> bool:
    """Did the machine's last check-in use its own runner token (not the shared one)?"""
    return (runner or {}).get("auth") == "machine"


def takes_subtasks(runner: dict | None) -> bool:
    """Does the machine take subtasks from agents on other machines (accept_delegated_tasks)?"""
    info = (runner or {}).get("info")
    return isinstance(info, dict) and info.get("accept_delegated_tasks") is True


def runner_projects(runner: dict | None) -> list[str]:
    projects = (runner or {}).get("projects")
    return [p for p in projects if isinstance(p, str)] if isinstance(projects, list) else []


# ------------------------------------------------------------- tag targets
# How the owner reads each feature a job may need (options.needs).
FEATURE_WORDS = {
    "approvals": "asking you before commands",
    "live": "a session kept open for messages",
    "job_mcp": "the hub's job tools",
    "levels": "autonomy levels the hub can check",
}


class TargetError(ValueError):
    """A job can't go where it was aimed. The message says why, in plain words."""


def tag_job_fits(runner: dict | None, tag: str, agent: str, project: str | None, account: str | None,
                 needs: Any = (), delegated: bool = False) -> bool:
    """Could this machine (a database row) take a queued job for `tag`, judging by its last
    check-in? The same rule
    the claim uses (Database.claim_next_job): its own runner token, the accepted tag, the agent (with
    the job's account key, when one is given), the project, every needed feature and, for a subtask,
    accept_delegated_tasks."""
    if not own_token(runner) or tag not in effective_tags(runner) or agent_entry(runner, agent) is None:
        return False
    if project and project not in runner_projects(runner):
        return False
    if account is not None and account_key(runner, agent) != account:
        return False
    if not set(needs or ()) <= set(agent_features(runner, agent)):
        return False
    return not delegated or takes_subtasks(runner)


def _names(names: list[str]) -> str:
    """'alpha', 'alpha and beta', 'alpha, beta and gamma'."""
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def tag_target(runners: list[dict], tag: str, agent: str, project: str | None, account: str | None = None,
               needs: Any = (), delegated: bool = False) -> tuple[str, list[dict]]:
    """Check a job aimed at "any machine tagged `tag`" against the machines (database rows): which
    account key it runs under, and the machines that could take it. Raises TargetError with what
    the owner (or the agent) should do. A machine key ("alpha/claude") matches only its own
    machine, so a tag job without accepted labels can't spread over machines that may be signed
    in to different accounts."""
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise TargetError(f"'{tag}' isn't a tag: tags are lowercase letters, digits, '.', '_' and '-'.")
    needs = [n for n in (needs or ()) if isinstance(n, str)]
    has = [r for r in runners if agent_entry(r, agent) is not None and (not project or project in runner_projects(r))]
    found = [r for r in has if own_token(r) and tag in effective_tags(r) and (not delegated or takes_subtasks(r))]
    if not found:
        what = f"an agent called '{agent}'" + (f" and a project called '{project}'" if project else "")
        message = f"No machine with the accepted tag '{tag}' has {what}."
        shared = [r["name"] for r in has if not own_token(r) and tag in reported_tags(r)]
        waiting = [r["name"] for r in has if own_token(r) and tag in reported_tags(r) and tag not in effective_tags(r)]
        closed = [r["name"] for r in has if delegated and own_token(r) and tag in effective_tags(r)
                  and not takes_subtasks(r)]
        if waiting:
            message += (f" {_names(waiting)} {'reports' if len(waiting) == 1 else 'report'} this tag, but you haven't "
                        f"accepted it yet (on {'its page' if len(waiting) == 1 else 'their pages'}).")
        if shared:
            message += (f" {_names(shared)} still {'uses' if len(shared) == 1 else 'use'} the shared runner token; "
                        "tasks for a tag only go to machines with their own token.")
        if closed:
            message += (f" {_names(closed)} {'doesn' if len(closed) == 1 else 'don'}'t take subtasks from other "
                        "agents (accept_delegated_tasks in runner.json).")
        raise TargetError(message)
    if needs:
        found = [r for r in found if set(needs) <= set(agent_features(r, agent))]
        if not found:
            words = ", ".join(FEATURE_WORDS.get(n, n) for n in needs)
            raise TargetError(f"No machine tagged '{tag}' has '{agent}' with what this task needs ({words}); "
                              "update the runner there.")
    keys: dict[str, list[str]] = {}
    for r in found:
        keys.setdefault(account_key(r, agent), []).append(r["name"])
    if account:
        if account not in keys:
            raise TargetError(f"No machine tagged '{tag}' has '{agent}' on the account '{account}'.")
        key = account
    elif len(keys) == 1:
        key = next(iter(keys))
    else:
        unlabelled = [r["name"] for r in found if account_key(r, agent) == f"{r['name']}/{agent}"]
        if unlabelled:
            raise TargetError(
                f"'{agent}' has no accepted account label on {', '.join(unlabelled)}, so the hub can't tell whether "
                f"it is the same account there. Give it the same label in each machine's runner.json "
                f"(agents.{agent}.account) and accept it on each machine's page, or choose one: "
                f"{', '.join(sorted(keys))}.")
        where = ", ".join(f"{k} on {', '.join(machines)}" for k, machines in sorted(keys.items()))
        raise TargetError(f"'{agent}' is a different account on different machines tagged '{tag}' ({where}). "
                          "Choose the account.")
    return key, [r for r in found if account_key(r, agent) == key]


def job_tools_for(job: dict) -> list[str]:
    """The tools at /mcp/job this job may use. Every job may ask who it is (job_info); an 'ask'
    job also has approve, and a top-level task allowed to hand out subtasks the delegation tools.
    Only a job with more than job_info gets a job token."""
    tools = ["job_info"]
    if job.get("autonomy") == "ask":
        tools.append("approve")
    opts = job.get("options") if isinstance(job.get("options"), dict) else {}
    if opts.get("delegate") and not job.get("delegated_by"):
        tools += DELEGATION_TOOLS
    return tools


def public_job(row: dict | None) -> dict | None:
    """A job row as the API shows it: without the job token's fingerprint."""
    if row is None:
        return None
    return {k: v for k, v in row.items() if k not in HIDDEN_JOB_FIELDS}
