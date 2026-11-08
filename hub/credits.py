"""Platform credits: the monthly API credits some Claude plans include.

Claude Max, Ultra and Team plans include API credits in the Console organization linked to the plan.
They arrive with each billing cycle, expire at its end, don't roll over, and are spent before
purchased credits; when they run out, the API refuses requests until the next cycle (unless purchased
credits are left). No documented API returns the balance, so the hub estimates it: the amount the
owner enters, less the organization's spend this cycle from the Admin API cost report (hub/usage.py).
The cost report includes all spend in the organization, so every figure here is an estimate.

What uses it:
  * the Usage page and the machine page show each organization's state (see org_view);
  * "Use platform credits first for background work" (off by default): council steps and subtasks
    named for a Claude agent that signs in with a subscription run on the same machine's credits
    agent instead (claim_route), while its organization is "available";
  * a task the owner started that stopped at a subscription usage limit is retried once on the
    machine's credits agent (retry_target).

The only automatic move is from a subscription agent to the credits agent its own machine's runner.json
names (credits_agent), which must sign in with an API key, and back to the agent that was named when
credits are unavailable or run out. Work never moves from one subscription account to another, and
nothing here reads a subscription's remaining quota (guide 1).

This module imports nothing from app.py and nothing from the `mcp` package.
"""

from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Any

from common import ACCOUNT_RE, account_key, agent_entry, agent_features

# The states of an organization's credits.
AVAILABLE, RESERVE, EXHAUSTED, UNKNOWN = "available", "reserve", "exhausted", "unknown"
STATES = (AVAILABLE, RESERVE, EXHAUSTED, UNKNOWN)
# From this share used, no work is moved to the credits agent automatically (jobs sent to it by hand
# still run).
RESERVE_AT = 0.9
# Average daily spend over this many days (or since the cycle started, if that is shorter).
BURN_DAYS = 7
# How long "the API refused for lack of credit" counts without a reset day.
EXHAUSTED_NO_RESET_S = 24 * 3600
# What the owner may enter.
MAX_AMOUNT = 1_000_000.0
# Claude agents: the runner's presets whose parser reads Claude Code's output ("selftest" is the fake).
CLAUDE_PRESETS = ("claude", "selftest")
# How a Claude agent signs in (runner.py claude_auth): a subscription, or an API key.
SUBSCRIPTION_AUTH = ("login", "token")
API_KEY_AUTH = "api_key"


# ------------------------------------------------------------- settings
def clean_org_setting(raw: Any) -> dict:
    """One organization's settings, checked: {"amount": dollars or None, "reset_day": 1-31 or None}."""
    raw = raw if isinstance(raw, dict) else {}
    amount = raw.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) \
            or not 0 < amount <= MAX_AMOUNT:
        amount = None
    day = raw.get("reset_day")
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
        day = None
    return {"amount": float(amount) if amount is not None else None, "reset_day": day}


def clean_settings(raw: Any) -> dict:
    """The stored credit settings: {"first": bool, "orgs": {name: {"amount", "reset_day"}}}."""
    raw = raw if isinstance(raw, dict) else {}
    orgs = raw.get("orgs") if isinstance(raw.get("orgs"), dict) else {}
    return {"first": raw.get("first") is True,
            "orgs": {k: clean_org_setting(v) for k, v in orgs.items() if isinstance(k, str)}}


def find_org(names: list[str], label: Any) -> str | None:
    """The organization (by its name in HUB_ANTHROPIC_ADMIN_KEYS) a label names, ignoring case."""
    if not isinstance(label, str) or not label.strip():
        return None
    wanted = label.strip().lower()
    return next((n for n in names if n.strip().lower() == wanted), None)


def org_setting(settings: dict, org: str) -> dict:
    orgs = settings.get("orgs") if isinstance(settings.get("orgs"), dict) else {}
    for name, value in orgs.items():
        if isinstance(name, str) and name.lower() == org.lower():
            return clean_org_setting(value)
    return {"amount": None, "reset_day": None}


# ---------------------------------------------------------- cycle maths
def _clamped(year: int, month: int, day: int) -> dt.date:
    """That day of the month, or the month's last day when it is shorter (31 -> 30 April, 28 February)."""
    return dt.date(year, month, min(day, calendar.monthrange(year, month)[1]))


def _shift(year: int, month: int, by: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + by
    return index // 12, index % 12 + 1


def _ts(day: dt.date) -> float:
    return dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc).timestamp()


def cycle_bounds(reset_day: int | None, now: float) -> tuple[float, float]:
    """The current billing cycle as (start, next reset), at midnight UTC: the most recent reset day
    on or before today and the next one, each clamped to its month's last day. Without a reset day,
    the calendar month."""
    day = reset_day if isinstance(reset_day, int) and 1 <= reset_day <= 31 else 1
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    this = _clamped(today.year, today.month, day)
    if today >= this:
        start, nxt = this, _clamped(*_shift(today.year, today.month, 1), day)
    else:
        start, nxt = _clamped(*_shift(today.year, today.month, -1), day), this
    return _ts(start), _ts(nxt)


def _day_ts(text: Any) -> float | None:
    """A cost bucket's day ("2026-11-08") as midnight UTC."""
    try:
        return _ts(dt.date.fromisoformat(str(text)[:10]))
    except ValueError:
        return None


def spend_since(days: Any, since: float) -> float:
    """The spend in the daily buckets that start at `since` or later."""
    total = 0.0
    for d in days if isinstance(days, list) else []:
        at = _day_ts(d.get("day")) if isinstance(d, dict) else None
        cost = d.get("cost") if isinstance(d, dict) else None
        if at is not None and at >= since and isinstance(cost, (int, float)) and not isinstance(cost, bool) \
                and math.isfinite(cost):
            total += float(cost)
    return total


def burn_start(cycle_start: float, now: float) -> float:
    """Where the average daily spend starts: BURN_DAYS days ago (whole days, today included), or the
    cycle's start when that is later."""
    today = dt.datetime.fromtimestamp(now, dt.timezone.utc).date()
    return max(_ts(today - dt.timedelta(days=BURN_DAYS - 1)), cycle_start)


def burn_rate(days: Any, cycle_start: float, now: float) -> float:
    """Average daily spend since burn_start, by the time that has passed (at least a day)."""
    first = burn_start(cycle_start, now)
    elapsed = max(1.0, (now - first) / 86400)
    return spend_since(days, first) / elapsed


# --------------------------------------------------------------- states
def exhausted_until(reset_day: int | None, now: float) -> float:
    """How long "the API refused for lack of credit" counts: until the next reset, or a day."""
    if reset_day:
        return cycle_bounds(reset_day, now)[1]
    return now + EXHAUSTED_NO_RESET_S


def org_view(name: str, cost: dict | None, setting: dict, refused: dict | None, now: float) -> dict:
    """What the Usage page shows for one organization, and its state:
      available  under 90% used by the estimate
      reserve    90% or more: nothing is moved to credits automatically
      exhausted  100% by the estimate, or the API refused a job for lack of credit this cycle
      unknown    no amount set, or no spend figures"""
    setting = clean_org_setting(setting)
    start, nxt = cycle_bounds(setting["reset_day"], now)
    ok = isinstance(cost, dict) and cost.get("ok") is True
    days = cost.get("days") if ok else None
    out: dict[str, Any] = {
        "name": name, "amount": setting["amount"], "reset_day": setting["reset_day"],
        "cycle_start": start, "next_reset": nxt if setting["reset_day"] else None,
        "month_end": None if setting["reset_day"] else nxt,
        "spend_ok": ok, "spend": round(spend_since(days, start), 2) if ok else None,
        "burn": round(burn_rate(days, start, now), 2) if ok else None,
        "burn_days": math.floor((now - burn_start(start, now)) / 86400) + 1,
        "refused": False, "refused_at": None, "refused_until": None,
        "left": None, "used": None, "runs_out": None, "lasts": None,
    }
    if isinstance(refused, dict) and isinstance(refused.get("until"), (int, float)) and refused["until"] > now:
        out.update(refused=True, refused_at=refused.get("at"), refused_until=refused["until"])
    if ok and setting["amount"]:
        amount = setting["amount"]
        left = max(0.0, amount - out["spend"])
        out["left"] = round(left, 2)
        out["used"] = min(1.0, out["spend"] / amount) if amount else None
        if left <= 0:
            out["lasts"] = False
        elif out["burn"] and out["burn"] > 0:
            runs_out = now + left / out["burn"] * 86400
            out["runs_out"] = runs_out if runs_out < nxt else None
            out["lasts"] = runs_out >= nxt
        else:
            out["lasts"] = True
    if out["refused"] or (out["used"] is not None and out["used"] >= 1.0):
        out["state"] = EXHAUSTED
    elif out["used"] is None:
        out["state"] = UNKNOWN
    elif out["used"] >= RESERVE_AT:
        out["state"] = RESERVE
    else:
        out["state"] = AVAILABLE
    return out


def org_views(names: list[str], cost_orgs: Any, settings: dict, refused: dict, now: float) -> list[dict]:
    """Every organization in HUB_ANTHROPIC_ADMIN_KEYS, in that order (cost_orgs: usage.summary's orgs)."""
    by_name = {o.get("name"): o for o in cost_orgs or [] if isinstance(o, dict)}
    refused = refused if isinstance(refused, dict) else {}
    return [org_view(n, by_name.get(n), org_setting(settings, n), refused.get(n), now) for n in names]


# ------------------------------------------------------ agents and machines
def is_claude(entry: dict | None) -> bool:
    return isinstance(entry, dict) and entry.get("preset") in CLAUDE_PRESETS


def is_subscription(runner: dict | None, agent: str) -> bool:
    """A Claude agent that signs in with a claude.ai subscription (a login or a setup-token token)."""
    entry = agent_entry(runner, agent)
    return is_claude(entry) and entry.get("auth") in SUBSCRIPTION_AUTH


def is_api_key(runner: dict | None, agent: str) -> bool:
    """A Claude agent that signs in with an API key (billed per token, or paid by platform credits)."""
    entry = agent_entry(runner, agent)
    return is_claude(entry) and entry.get("auth") == API_KEY_AUTH


def credits_agent(runner: dict | None, org_names: list[str]) -> tuple[str | None, str | None, str]:
    """The machine's credits agent and its organization: (agent, org, "") when it is usable, else
    (agent or None, None, why not). It must be the agent the machine reports as credits_agent, sign in
    with an API key, and have an accepted account label that names an organization in
    HUB_ANTHROPIC_ADMIN_KEYS (ignoring case)."""
    info = (runner or {}).get("info")
    name = info.get("credits_agent") if isinstance(info, dict) else None
    if not isinstance(name, str) or not name:
        return None, None, "no credits agent"
    entry = agent_entry(runner, name)
    if entry is None:
        return name, None, f"the machine has no agent called {name}"
    if not is_api_key(runner, name):
        return name, None, f"{name} doesn't sign in with an API key"
    label = entry.get("account")
    if not isinstance(label, str) or not ACCOUNT_RE.fullmatch(label):
        return name, None, f"{name} has no account label"
    if account_key(runner, name) != label:
        return name, None, f"accept {name}'s account label on the machine's page"
    org = find_org(org_names, label)
    if org is None:
        return name, None, f"its label '{label}' isn't an organization in HUB_ANTHROPIC_ADMIN_KEYS"
    return name, org, ""


def agent_org(runner: dict | None, agent: str, org_names: list[str]) -> str | None:
    """The organization an API-key Claude agent's accepted label names, if any."""
    entry = agent_entry(runner, agent)
    if not is_api_key(runner, agent) or entry is None:
        return None
    label = entry.get("account")
    if not isinstance(label, str) or account_key(runner, agent) != label:
        return None
    return find_org(org_names, label)


def plan(runner: dict | None, org_names: list[str], states: dict[str, str]) -> dict | None:
    """What a machine offers for moving background work to credits, when it may now: its credits
    agent (ready), the organization (available), the agent's features, and which of its agents sign
    in with a subscription. None otherwise."""
    agent, org, _ = credits_agent(runner, org_names)
    if not agent or not org or states.get(org) != AVAILABLE:
        return None
    entry = agent_entry(runner, agent) or {}
    if entry.get("ready") is False:
        return None
    subs = [a["name"] for a in (runner or {}).get("agents") or []
            if isinstance(a, dict) and isinstance(a.get("name"), str) and is_subscription(runner, a["name"])]
    if not subs:
        return None
    return {"agent": agent, "org": org, "account": account_key(runner, agent),
            "features": agent_features(runner, agent), "subscription_agents": subs}


def _opts(job: dict) -> dict:
    opts = job.get("options")
    return opts if isinstance(opts, dict) else {}


def background(job: dict) -> bool:
    """Work the owner didn't start by hand: a council step or a subtask."""
    return job.get("kind") == "task" and bool(job.get("parent_id") or job.get("delegated_by"))


def subtask_scope_allows(parent_options: Any, agent: Any, account: Any) -> bool:
    """Would the owner's scope for a task's subtasks (New task: which agents and accounts they may
    use, delegation.py) let one run on `agent`, the account `account`? The same lists the hub checks
    when the subtask is handed out, so moving it to credits never widens them."""
    opts = parent_options if isinstance(parent_options, dict) else {}
    agents = opts.get("delegate_agents")
    if isinstance(agents, list) and agent not in agents:
        return False
    accounts = opts.get("delegate_accounts")
    if isinstance(accounts, list) and account not in accounts:
        return False
    return True


def claim_route(job: dict, offer_plan: dict | None, parent_options: Any = None) -> dict | None:
    """At claim time: should this job run on the machine's credits agent instead of the agent it
    names? Only a background job that names one of the machine's subscription agents, starts a new
    session (a session belongs to its agent), isn't a live session or a job already moved back from
    credits, and only when the credits agent has every feature the job needs. A subtask moves only
    when its parent's scope (`parent_options`, the parent task's options) allows the credits agent and
    its account. Returns the job's new fields, or None to run it as named."""
    if not offer_plan or not background(job):
        return None
    opts = _opts(job)
    if job.get("resume_session") or opts.get("live") or opts.get("no_credits") or job.get("named_agent"):
        return None
    if job.get("agent") not in offer_plan.get("subscription_agents", ()):
        return None
    target = offer_plan.get("agent")
    if not isinstance(target, str) or not target or target == job.get("agent"):
        return None
    needs = opts.get("needs") if isinstance(opts.get("needs"), list) else []
    if not set(needs) <= set(offer_plan.get("features") or ()):
        return None
    if job.get("delegated_by") and not subtask_scope_allows(parent_options, target, offer_plan.get("account")):
        return None
    return {"agent": target, "named_agent": job.get("agent"),
            "credits": {"org": offer_plan.get("org"), "how": "background", "agent": target,
                        "named_agent": job.get("agent")}}


def retry_target(job: dict, runner: dict | None, org_names: list[str], states: dict[str, str],
                 by_hand: bool = False) -> tuple[str | None, str | None, str]:
    """Where a task the owner started, which stopped at a subscription usage limit, may run again:
    (credits agent, org, "") or (None, None, why not). Once only, never for a live session, a
    follow-up (its session belongs to the first agent), or a job that may have changed files unless
    it was read only. `by_hand` (Retry on credits) skips the last rule and also runs in reserve."""
    opts = _opts(job)
    if job.get("kind") != "task" or job.get("parent_id") or job.get("delegated_by"):
        return None, None, "only tasks you started are retried"
    if job.get("error_kind") != "claude_limit":
        return None, None, "it didn't stop at a usage limit"
    if job.get("retry_of") or job.get("named_agent"):
        return None, None, "it is already a retry"
    if opts.get("live"):
        return None, None, "live sessions aren't retried"
    if job.get("resume_session"):
        return None, None, "a follow-up continues a session, which only its own agent has"
    if not is_subscription(runner, job.get("agent") or ""):
        return None, None, f"{job.get('agent')} doesn't sign in with a subscription"
    agent, org, why = credits_agent(runner, org_names)
    if not agent or not org:
        return None, None, why
    state = states.get(org, UNKNOWN)
    if state == EXHAUSTED or (state != AVAILABLE and not (by_hand and state == RESERVE)):
        return None, None, f"{org}'s platform credits are {state}"
    if (agent_entry(runner, agent) or {}).get("ready") is False:
        return None, None, f"{agent} isn't ready"
    needs = opts.get("needs") if isinstance(opts.get("needs"), list) else []
    if not set(needs) <= set(agent_features(runner, agent)):
        return None, None, f"{agent} can't do what this task needs"
    if not by_hand and job.get("autonomy") != "readonly" and (not opts.get("capture_diff") or job.get("diff")):
        return None, None, "it may have changed files"
    return agent, org, ""
