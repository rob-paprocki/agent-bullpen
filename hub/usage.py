"""The 'Usage' tab: what each paid account has spent, plus handy links.

Subscription limits (Claude plans, Google AI plans, ...) have no public API, so
the dashboard links to each provider's own usage pages instead. API spend does
have an API for Anthropic: its Admin API reports cost per Console organization,
and the hub shows the last 30 days for each organization you give it a key for.

Settings (environment variables, in /etc/bullpen/hub.env):
  HUB_ANTHROPIC_ADMIN_KEYS  one Admin key per Console organization, any number:
                            "Work=sk-ant-admin01-...;Personal=sk-ant-admin01-..."
  HUB_USAGE_PROVIDERS       whose links the page shows, comma separated, in order
                            (default "anthropic,google"; "none" shows only HUB_LINKS)
  HUB_LINKS                 extra links, shown first: "Name=https://...;Other=https://..."
  HUB_ANTHROPIC_COST_URL    where to ask for the cost report (default Anthropic's own address; for a
                            proxy in front of it, or the end-to-end test's stand-in)

Each organization's daily buckets cover the last 32 days, so a whole billing cycle of platform credits
(hub/credits.py) is always in them; "total" is the last 30 days.

The figures are fetched again every 15 minutes, or when Refresh on the Usage page asks for it
(summary(force=True)), at most once a minute: Anthropic suggests polling the cost API about once
a minute at most.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request

COST_URL = "https://api.anthropic.com/v1/organizations/cost_report"
# Days of buckets fetched: a billing cycle is at most 31 days, plus today.
FETCH_DAYS = 32
TOTAL_DAYS = 30
CACHE_SECONDS = 15 * 60
# Seconds between two refreshes asked for with Refresh (summary(force=True)).
REFRESH_SECONDS = 60
_cache: dict = {"at": 0.0, "data": None}
_last_forced = 0.0
_lock = threading.Lock()


class RefreshTooSoon(Exception):
    """Refresh was pressed again within REFRESH_SECONDS; `seconds_left` until it may be."""

    def __init__(self, seconds_left: int):
        super().__init__(f"Refreshed a moment ago. Try again in {seconds_left} seconds.")
        self.seconds_left = seconds_left

# Links to each provider's own pages, by provider. Add a provider here (and to
# DEFAULT_PROVIDERS if it should show without a setting) when the hub learns a new service.
PROVIDERS: dict[str, dict] = {
    "anthropic": {"name": "Anthropic", "links": [
        ("Claude plan usage (open it signed in to the right account)", "https://claude.ai/settings/usage"),
        ("Claude Code sessions (Remote Control)", "https://claude.ai/code"),
        ("Claude Console (API keys, billing)", "https://platform.claude.com"),
    ]},
    "google": {"name": "Google", "links": [
        ("Antigravity (Google AI plans)", "https://antigravity.google.com"),
        ("Gemini app", "https://gemini.google.com"),
        ("Gemini API rate limits (Google AI Studio)", "https://aistudio.google.com/rate-limit"),
    ]},
}
DEFAULT_PROVIDERS = "anthropic,google"


def providers() -> list[str]:
    """The providers whose links the Usage page shows, from HUB_USAGE_PROVIDERS."""
    raw = os.environ.get("HUB_USAGE_PROVIDERS", DEFAULT_PROVIDERS)
    out: list[str] = []
    for part in raw.replace(";", ",").split(","):
        key = part.strip().lower()
        if key in PROVIDERS and key not in out:
            out.append(key)
    return out


def _pairs(raw: str) -> list[tuple[str, str]]:
    out = []
    for part in raw.split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip() and value.strip():
                out.append((name.strip(), value.strip()))
    return out


def org_names() -> list[str]:
    """The Console organizations in HUB_ANTHROPIC_ADMIN_KEYS, in order (never their keys)."""
    names: list[str] = []
    for name, _ in _pairs(os.environ.get("HUB_ANTHROPIC_ADMIN_KEYS", "")):
        if name not in names:
            names.append(name)
    return names


def links() -> list[dict]:
    out = [{"name": n, "url": u, "provider": ""} for n, u in _pairs(os.environ.get("HUB_LINKS", ""))]
    for key in providers():
        out += [{"name": n, "url": u, "provider": key} for n, u in PROVIDERS[key]["links"]]
    return out


def _get(url: str, key: str) -> dict:
    req = urllib.request.Request(url, headers={
        "x-api-key": key,
        "anthropic-version": os.environ.get("HUB_ANTHROPIC_VERSION", "2023-06-01"),
        "User-Agent": "bullpen",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _org_cost(name: str, key: str) -> dict:
    """The daily cost buckets for the last FETCH_DAYS days, and their sum over the last TOTAL_DAYS."""
    now = dt.datetime.now(dt.timezone.utc)
    start = (now - dt.timedelta(days=FETCH_DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
    since = (now - dt.timedelta(days=TOTAL_DAYS)).strftime("%Y-%m-%d")
    base = os.environ.get("HUB_ANTHROPIC_COST_URL", "").strip() or COST_URL
    query = f"starting_at={start.strftime('%Y-%m-%dT%H:%M:%SZ')}&bucket_width=1d&limit={FETCH_DAYS + 1}"
    url = f"{base}?{query}"
    total = 0.0
    currency = "USD"
    days = []
    pages = 0
    try:
        while url and pages < 5:
            data = _get(url, key)
            pages += 1
            for bucket in data.get("data", []):
                day_total = 0.0
                for item in bucket.get("results", []):
                    # The Admin API reports cost as a decimal string in cents.
                    day_total += float(item.get("amount") or 0) / 100.0
                    currency = item.get("currency") or currency
                day = (bucket.get("starting_at") or "")[:10]
                if day >= since:
                    total += day_total
                days.append({"day": day, "cost": round(day_total, 4)})
            if data.get("has_more") and data.get("next_page"):
                url = f"{base}?{query}&page={data['next_page']}"
            else:
                url = None
        return {"name": name, "ok": True, "total": round(total, 2), "currency": currency, "days": days}
    except urllib.error.HTTPError as err:
        detail = err.read()[:200].decode(errors="replace")
        return {"name": name, "ok": False, "error": f"HTTP {err.code}: {detail}"}
    except (OSError, ValueError) as err:
        return {"name": name, "ok": False, "error": str(err)}


def cached() -> dict | None:
    """The last figures fetched, without fetching (None before the first fetch). For decisions
    made while a machine waits for work, which must never wait on the network."""
    with _lock:
        return _cache["data"]


def summary(force: bool = False) -> dict:
    """The Usage page's API spend and links: fetched again every CACHE_SECONDS, or now with
    force=True (Refresh), which raises RefreshTooSoon within REFRESH_SECONDS of the last one."""
    global _last_forced
    with _lock:
        now = time.time()
        if force:
            wait = REFRESH_SECONDS - (now - _last_forced)
            if wait > 0:
                raise RefreshTooSoon(max(1, math.ceil(wait)))
            _last_forced = now
        elif _cache["data"] and now - _cache["at"] < CACHE_SECONDS:
            return _cache["data"]
        orgs = [_org_cost(n, k) for n, k in _pairs(os.environ.get("HUB_ANTHROPIC_ADMIN_KEYS", ""))]
        data = {"orgs": orgs, "checked_at": time.time(), "links": links(),
                "providers": [{"id": k, "name": PROVIDERS[k]["name"]} for k in providers()],
                "litellm_url": os.environ.get("HUB_LITELLM_UI_URL", "")}
        _cache.update(at=time.time(), data=data)
        return data
