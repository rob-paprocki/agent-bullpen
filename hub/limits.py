"""Plan limits and tokens per job, as the Usage page and the machine page show them.

Display only. The runners send each agent's newest plan-limit figures with their check-ins, and
each job's tokens when it finishes; this module turns both into what the dashboard shows. No hub
code may use these figures to choose, hold or move work: the hub never switches accounts because
one is near its limit (guide 1).

This module imports nothing from app.py and nothing from the `mcp` package.
"""

from __future__ import annotations

import math
import re
from typing import Any

from common import ACCOUNT_RE, ONLINE_WINDOW, vtuple

# What the dashboard calls each window (runner/runner.py has the same table). Other windows
# named seven_day_<x> are shown as "Week (<X>)".
WINDOW_LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "Week (all models)",
    "seven_day_sonnet": "Week (Sonnet)",
    "seven_day_opus": "Week (Opus)",
    "seven_day_overage_included": "Week (Fable)",
    "overage": "Extra usage",
    "current": "Current limit",
}
# Where a window goes on the page: five_hour, seven_day, the other seven_day_* by name, current, overage.
WINDOW_RANK = {"five_hour": 0, "seven_day": 1, "current": 3, "overage": 4}
# The statuses Claude Code reports, for the account and for each window.
LIMIT_STATUSES = {"allowed", "allowed_warning", "rejected"}
# Where a figure came from: a job's own events, or "Check limits" (/usage).
LIMIT_SOURCES = {"job", "check"}
WINDOW_ID_RE = re.compile(r"[a-z0-9_]{1,40}")
DISABLED_REASON_RE = re.compile(r"[a-z_]{1,60}")
# The figures of a window, merged one by one: each keeps its own time (<field>_at).
LIMIT_FIELDS = ("used", "status", "resets_at", "threshold", "resets_text")
# A machine's clock this many seconds off the hub's or less is left alone.
SKEW_TOLERANCE = 30
# Windows kept per agent, and the longest reset text kept (as /usage printed it).
MAX_WINDOWS = 16
MAX_TEXT = 80

# Tokens per job.
USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
USAGE_SOURCES = {"claude", "gemini"}
MAX_USAGE_MODELS = 20
MAX_MODEL_NAME = 100
# Larger counts from a runner are cut to this (no agent reports anything near it).
MAX_COUNT = 10 ** 15
MAX_COST = 10.0 ** 9
# From this Claude Code version on, modelUsage includes the totals of the session a job resumes,
# so a follow-up's share is the difference from the job it continues.
CUMULATIVE_FROM = (2, 1, 277)
VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.+-]{0,39}")


# ------------------------------------------------------------------ helpers
def _num(v: Any) -> float | None:
    """A finite number from an int, a float or a numeric string; None for anything else
    (True and False too)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = float(v)
    elif isinstance(v, str):
        try:
            n = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    return n if math.isfinite(n) else None


def window_label(wid: str) -> str:
    """The dashboard's name for a window: '5-hour session', 'Week (Sonnet)', 'Week (Haiku)', ..."""
    if wid in WINDOW_LABELS:
        return WINDOW_LABELS[wid]
    if wid.startswith("seven_day_"):
        return "Week (" + wid[len("seven_day_"):].replace("_", " ").title() + ")"
    return wid.replace("_", " ").capitalize()


def window_order(wid: str) -> tuple:
    """Sort key for windows: five_hour, seven_day, other seven_day_* by name, current, overage,
    then anything else by name."""
    return (WINDOW_RANK.get(wid, 2 if wid.startswith("seven_day_") else 5), wid)


def _find_agent(runner: dict | None, agent: str) -> dict | None:
    for a in (runner or {}).get("agents") or []:
        if isinstance(a, dict) and a.get("name") == agent:
            return a
    return None


def reported_label(agent: dict | None) -> str | None:
    """The account label an agent reports (runner.json "account"), when it is a valid one."""
    label = (agent or {}).get("account")
    if isinstance(label, str) and ACCOUNT_RE.fullmatch(label):
        return label
    return None


def limit_account(runner: dict | None, agent: str) -> tuple[str, bool]:
    """Which account an agent's plan limits belong to on the Usage page, and whether that is a
    label: the label it reports (accepted or not: this is display, and for "Check limits" the
    stricter reading), else its own key "<machine>/<agent>". Never used for routing (that is
    common.account_key, with accepted labels only)."""
    label = reported_label(_find_agent(runner, agent))
    if label:
        return label, True
    return f"{(runner or {}).get('name') or ''}/{agent}", False


# --------------------------------------------------------------- plan limits
def _stamp(v: Any, skew: float) -> float | None:
    n = _num(v)
    return n + skew if n is not None else None


def _clean_window(raw: dict, skew: float) -> dict | None:
    """One window from a heartbeat, checked and moved to the hub's clock. `skew` is how many
    seconds the hub's clock is ahead of the machine's (0 when it is close enough)."""
    observed = _stamp(raw.get("observed_at"), skew)
    used = _num(raw.get("used"))
    status = raw.get("status") if raw.get("status") in LIMIT_STATUSES else None
    source = raw.get("source") if raw.get("source") in LIMIT_SOURCES else None
    resets_at = _num(raw.get("resets_at"))
    if resets_at is not None and source == "check":
        # Check limits works the reset time out on the machine; a job's reset times come from
        # Claude's own service and don't depend on the machine's clock.
        resets_at += skew
    threshold = _num(raw.get("threshold"))
    text = raw.get("resets_text")
    text = (" ".join(text.split())[:MAX_TEXT] or None) if isinstance(text, str) else None
    out: dict[str, Any] = {
        "used": min(max(used, 0.0), 5.0) if used is not None else None,
        "status": status,
        "resets_at": resets_at,
        "threshold": min(max(threshold, 0.0), 5.0) if threshold is not None else None,
        "resets_text": text,
    }
    for field in LIMIT_FIELDS:
        at = _stamp(raw.get(field + "_at"), skew)
        out[field + "_at"] = (at if at is not None else observed) if out[field] is not None else None
    stamps = [out[f + "_at"] for f in LIMIT_FIELDS if out[f + "_at"] is not None]
    out["observed_at"] = max(stamps) if stamps else observed
    out["source"] = source
    if all(out[f] is None for f in LIMIT_FIELDS) or out["observed_at"] is None:
        return None
    return out


def _clean_overage(raw: Any, skew: float) -> dict | None:
    if not isinstance(raw, dict):
        return None
    observed = _stamp(raw.get("observed_at"), skew)
    if observed is None:
        return None
    reason = raw.get("disabled_reason")
    return {
        "using": raw.get("using") is True,
        "status": raw.get("status") if raw.get("status") in LIMIT_STATUSES else None,
        "resets_at": _num(raw.get("resets_at")),  # Claude's own time: no clock correction
        "disabled_reason": reason if isinstance(reason, str) and DISABLED_REASON_RE.fullmatch(reason) else None,
        "observed_at": observed,
    }


def _clean_status(raw: Any, skew: float) -> dict | None:
    if not isinstance(raw, dict) or raw.get("value") not in LIMIT_STATUSES:
        return None
    observed = _stamp(raw.get("observed_at"), skew)
    return {"value": raw["value"], "observed_at": observed} if observed is not None else None


def agent_limits(raw: Any, clock_skew: Any = None) -> dict:
    """An agent's limits from its machine's heartbeat ({"windows", "overage", "status"}), checked
    and with its times moved to the hub's clock when the machine's clock is more than
    SKEW_TOLERANCE seconds off. Used for the machine page and the Usage page."""
    skew = _num(clock_skew) or 0.0
    if abs(skew) <= SKEW_TOLERANCE:
        skew = 0.0
    raw = raw if isinstance(raw, dict) else {}
    windows: dict[str, dict] = {}
    raw_windows = raw.get("windows") if isinstance(raw.get("windows"), dict) else {}
    for wid, w in raw_windows.items():
        if len(windows) >= MAX_WINDOWS:
            break
        if isinstance(wid, str) and WINDOW_ID_RE.fullmatch(wid) and isinstance(w, dict):
            clean = _clean_window(w, skew)
            if clean is not None:
                windows[wid] = clean
    return {"windows": {wid: windows[wid] for wid in sorted(windows, key=window_order)},
            "overage": _clean_overage(raw.get("overage"), skew),
            "status": _clean_status(raw.get("status"), skew)}


def merge_window(current: dict | None, obs: dict, runner: str, agent: str) -> dict:
    """A window with another agent's reading of it (from agent_limits) merged in, field by field:
    a newer value replaces an older one, and a field the reading leaves out keeps its value.
    `runner`, `agent` and `source` name where the newest `used` figure came from (the newest
    reading, while there is no such figure)."""
    out = dict(current or {})
    for field in LIMIT_FIELDS:
        value, at = obs.get(field), obs.get(field + "_at")
        if value is None or at is None:
            continue
        if out.get(field + "_at") is None or out[field + "_at"] <= at:
            out[field], out[field + "_at"] = value, at
            if field == "used":
                out.update(source=obs.get("source"), runner=runner, agent=agent)
    for field in LIMIT_FIELDS:
        out.setdefault(field, None)
        out.setdefault(field + "_at", None)
    if out["used"] is None and (out.get("observed_at") is None or out["observed_at"] <= obs["observed_at"]):
        out.update(source=obs.get("source"), runner=runner, agent=agent)
    stamps = [out[f + "_at"] for f in LIMIT_FIELDS if out[f + "_at"] is not None]
    out["observed_at"] = max(stamps) if stamps else obs.get("observed_at")
    return out


def _has_limits(agent: Any) -> bool:
    return isinstance(agent, dict) and isinstance(agent.get("name"), str) \
        and isinstance(agent.get("features"), list) and "limits" in agent["features"]


def account_view(runners: list[dict], now: float) -> dict:
    """GET /api/usage/limits: every agent that reports plan limits, grouped by the account label
    it reports (or "<machine>/<agent>" without one), with the figures of all its machines merged
    field by field. `machines` lists every machine using a label, so a label given to two
    different accounts shows. `waiting` lists the agents whose account has no reading yet, with
    `can_check`: whether Check limits can ask it (else only its jobs bring figures). An agent with
    no reading of its own whose label has one (from another agent) isn't waiting: Check limits
    reads each account once, and the card shows that account's figures."""
    accounts: dict[str, dict] = {}
    waiting: list[dict] = []
    for runner in sorted((r for r in runners if isinstance(r, dict)), key=lambda r: str(r.get("name") or "")):
        name = str(runner.get("name") or "")
        online = now - (_num(runner.get("last_seen")) or 0) < ONLINE_WINDOW
        for agent in runner.get("agents") or []:
            if not _has_limits(agent):
                continue
            key, labelled = limit_account(runner, agent["name"])
            acc = accounts.setdefault(key, {"account": key, "labelled": labelled, "agents": [], "machines": [],
                                            "status": None, "windows": {}, "overage": None})
            acc["agents"].append({"runner": name, "agent": agent["name"], "online": online})
            if name not in acc["machines"]:
                acc["machines"].append(name)
            lim = agent_limits(agent.get("limits"), runner.get("clock_skew"))
            if not lim["windows"] and lim["overage"] is None and lim["status"] is None:
                waiting.append({"runner": name, "agent": agent["name"], "account": key,
                                "can_check": "usage_check" in agent["features"]})
                continue
            for wid, w in lim["windows"].items():
                acc["windows"][wid] = merge_window(acc["windows"].get(wid), w, name, agent["name"])
            ov = lim["overage"]
            if ov is not None and (acc["overage"] is None or acc["overage"]["observed_at"] <= ov["observed_at"]):
                acc["overage"] = {**ov, "runner": name, "agent": agent["name"]}
            st = lim["status"]
            if st is not None and (acc["status"] is None or acc["status"]["observed_at"] <= st["observed_at"]):
                acc["status"] = st
    out = []
    for acc in accounts.values():
        if not acc["windows"] and acc["overage"] is None and acc["status"] is None:
            continue  # only agents without a reading: they are in `waiting`
        windows = []
        for wid in sorted(acc["windows"], key=window_order):
            w = acc["windows"][wid]
            windows.append({"id": wid, "label": window_label(wid), **w,
                            "expired": w["resets_at"] is not None and w["resets_at"] < now})
        out.append({**acc, "windows": windows})
    out.sort(key=lambda a: (not a["labelled"], a["account"].lower(), a["account"]))
    read = {acc["account"] for acc in out}
    return {"now": now, "accounts": out, "waiting": [w for w in waiting if w["account"] not in read]}


# ------------------------------------------------------------- tokens per job
def _count(v: Any) -> int:
    n = _num(v)
    return int(min(n, MAX_COUNT)) if n is not None and n > 0 else 0


def _cost(v: Any) -> float | None:
    n = _num(v)
    return round(min(n, MAX_COST), 6) if n is not None and n >= 0 else None


def _record(raw: Any) -> dict | None:
    """One set of figures, numbers only: the four token counts, cost_usd (None when unknown) and
    the same per model (at most MAX_USAGE_MODELS, names cut to MAX_MODEL_NAME characters)."""
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {f: _count(raw.get(f)) for f in USAGE_FIELDS}
    out["cost_usd"] = _cost(raw.get("cost_usd"))
    models: dict[str, dict] = {}
    raw_models = raw.get("models") if isinstance(raw.get("models"), dict) else {}
    for name, m in raw_models.items():
        if len(models) >= MAX_USAGE_MODELS:
            break
        if not isinstance(name, str) or not isinstance(m, dict):
            continue
        key = " ".join(name.split())[:MAX_MODEL_NAME]
        if key and key not in models:
            models[key] = {**{f: _count(m.get(f)) for f in USAGE_FIELDS}, "cost_usd": _cost(m.get("cost_usd"))}
    out["models"] = models
    if raw.get("scope") == "run":
        out["scope"] = "run"  # this run's figures only (a Claude Code without modelUsage)
    return out


def sanitize_usage(raw: Any) -> tuple[dict | None, dict | None]:
    """A runner's `usage` (what its agent reported for a job) as (reported, own), numbers only.
    `reported` is the agent's own total (for Claude, the whole conversation); `own` this job's
    share when the agent said it, else None. Both carry `source` ("claude", "gemini" or None),
    `turns` and, for Claude, `cli_version`."""
    if not isinstance(raw, dict):
        return None, None
    reported, own = _record(raw.get("reported")), _record(raw.get("own"))
    if reported is None:
        return None, None
    source = raw.get("source") if raw.get("source") in USAGE_SOURCES else None
    version = raw.get("cli_version")
    meta: dict[str, Any] = {"source": source, "turns": min(_count(raw.get("turns")), 10 ** 6)}
    if source == "claude":
        meta["cli_version"] = version if isinstance(version, str) and VERSION_RE.fullmatch(version) else None
    reported.update(meta)
    if own is not None:
        own.update(meta)
    return reported, own


def _cumulative_cli(version: Any) -> bool:
    """Does this Claude Code version count resumed totals in modelUsage? Unknown counts as yes."""
    if not isinstance(version, str) or not version:
        return True
    parsed = vtuple(version)
    return parsed == (0,) or parsed >= CUMULATIVE_FROM


def _covers(reported: dict, baseline: dict) -> bool:
    """Is every figure of `reported` at least the baseline's (totals that only grew)?"""
    if any(reported[f] < baseline[f] for f in USAGE_FIELDS):
        return False
    if reported["cost_usd"] is not None and baseline["cost_usd"] is not None \
            and reported["cost_usd"] + 1e-9 < baseline["cost_usd"]:
        return False
    for name, base in baseline["models"].items():
        mine = reported["models"].get(name)
        if mine is None or any(mine[f] < base[f] for f in USAGE_FIELDS):
            return False
    return True


def _minus(a: dict, b: dict | None) -> dict:
    out: dict[str, Any] = {f: a[f] - (b[f] if b else 0) for f in USAGE_FIELDS}
    if a.get("cost_usd") is None:
        out["cost_usd"] = None
    elif b is None:
        out["cost_usd"] = a["cost_usd"]
    elif b.get("cost_usd") is None:
        out["cost_usd"] = None  # the earlier cost is unknown, so this job's share is too
    else:
        out["cost_usd"] = round(max(a["cost_usd"] - b["cost_usd"], 0.0), 6)
    return out


def usage_share(reported: dict | None, own: dict | None, baseline: Any, resumed: bool) -> dict | None:
    """What a job itself used, for its `usage` (both arguments from sanitize_usage; `baseline` is
    the `usage_reported` of the job it continues, or None):
      - the agent said this job's share (own): that, exactly;
      - Gemini, or a job that resumed nothing: the figures as reported;
      - a Claude follow-up without its share and nothing to compare with: the reported figures,
        marked "includes_earlier";
      - with the earlier job's figures, a Claude Code that counts resumed totals, and totals that
        only grew: the difference, per field and per model;
      - otherwise (an older Claude Code that reported this run only, or totals that went down
        after a kill): the reported figures, marked "approximate"."""
    if reported is None:
        return None
    meta = {k: reported[k] for k in ("source", "turns", "cli_version") if k in reported}
    if own is not None:
        return {**own, **meta}
    if reported.get("source") == "gemini" or not resumed:
        return dict(reported)
    base = _record(baseline)
    if base is None:
        return {**reported, "includes_earlier": True}
    if reported.get("scope") == "run" or base.get("scope") == "run" \
            or not _cumulative_cli(reported.get("cli_version")) or not _covers(reported, base):
        return {**reported, "approximate": True}
    out = _minus(reported, base)
    models = {}
    for name, m in reported["models"].items():
        diff = _minus(m, base["models"].get(name))
        if any(diff[f] for f in USAGE_FIELDS) or diff["cost_usd"]:
            models[name] = diff
    return {**out, "models": models, **meta}


def usage_totals(jobs: list[dict], runners: list[dict]) -> list[dict]:
    """GET /api/usage/jobs rows: the `usage` of finished jobs added up per machine and agent, with
    the agent's current reported label. `jobs` are rows with runner, agent and usage, oldest
    first. cost_usd stays None when no job had one (Gemini CLI reports tokens only)."""
    labels: dict[tuple[str, str], str | None] = {}
    for r in runners:
        for a in (r.get("agents") or []) if isinstance(r, dict) else []:
            if isinstance(a, dict) and isinstance(a.get("name"), str):
                labels[(str(r.get("name") or ""), a["name"])] = reported_label(a)
    groups: dict[tuple[str, str], dict] = {}
    for job in jobs:
        u = job.get("usage")
        if not isinstance(u, dict):
            continue
        key = (str(job.get("runner") or ""), str(job.get("agent") or ""))
        row = groups.get(key)
        if row is None:
            row = groups[key] = {"runner": key[0], "agent": key[1], "account": labels.get(key), "source": None,
                                 "jobs": 0, **{f: 0 for f in USAGE_FIELDS}, "cost_usd": None}
        row["jobs"] += 1
        for f in USAGE_FIELDS:
            row[f] += _count(u.get(f))
        cost = _cost(u.get("cost_usd"))
        if cost is not None:
            row["cost_usd"] = round((row["cost_usd"] or 0.0) + cost, 6)
        if u.get("source") in USAGE_SOURCES:
            row["source"] = u["source"]  # the newest job's
    return [groups[k] for k in sorted(groups)]
