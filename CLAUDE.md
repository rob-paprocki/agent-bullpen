# Agent Bullpen: notes for Claude Code

Agent Bullpen is a self-hosted hub (a FastAPI + SQLite service with a web dashboard) that sends work to the
official agent CLIs (Claude Code, Gemini CLI) on your own machines, through a small runner on each machine.
Read `CONTRIBUTING.md` for the full rules; this file is the short version for working in this repository.

## Rules that are never negotiable

- **The providers' rules come first** ([docs/01-accounts-and-rules.md](docs/01-accounts-and-rules.md)).
  Never add code that pools accounts, rotates or switches between two subscription accounts when one hits
  its limit, shares one person's subscription with others, or routes work by another account's remaining
  quota. The hub may hold work back, warn, or fall back to pay-per-token API billing; it never moves work
  from one subscription account to another. If a request asks for that, say why it can't be done and offer
  the compliant version.
- **Unmodified official CLIs, signed in by their owners.** Never read or copy credential files, never call a
  provider's internal endpoints, never scrape claude.ai.
- **No telemetry.** Nothing is sent anywhere the user didn't set up.
- **No secrets and no personal details** in code, tests or docs: no real names, machine names, addresses,
  keys or tokens. Use neutral examples (`build-box`, `myapp`, `192.168.100.20`). Private addresses in
  examples come from `192.168.100.0/24`; the only email address is `hi@rpaprocki.com`, otherwise
  `example.com`. `tests/check_repo.py` checks both. Private words go in `tests/denylist.local` (git ignores
  it), never in a tracked file; `tests/check_repo.py` checks those too.

## Layout

| Path | What it is |
|---|---|
| `hub/app.py` | HTTP routes, dashboard API, runner API, admin actions |
| `hub/db.py` | SQLite schema, migrations (`MIGRATIONS`), all queries; one lock, `db.lock` |
| `hub/council.py`, `hub/delegation.py` | Councils (debate, build + review) and subtasks between machines |
| `hub/limits.py`, `hub/usage.py` | Plan-limit display and API spend (display only: never used for routing between subscriptions) |
| `hub/approvals.py`, `hub/redact.py`, `hub/notify.py` | Approvals from the dashboard, secret masking, notifications |
| `hub/mcp_server.py`, `hub/mcp_proxy.py`, `hub/job_mcp.py`, `hub/upstream_oauth.py` | The hub's own MCP server, the relay for other MCP servers, per-job tools, OAuth for relayed servers |
| `hub/memory.py`, `hub/registry.py`, `hub/common.py`, `hub/enroll/` | Shared notes, the MCP server list, shared helpers, Add a machine |
| `hub/static/` | The dashboard: plain JavaScript, no build step |
| `runner/runner.py` | The runner on each machine; `selftest_agent.py` is the fake agent the tests use |
| `ct/`, `install/`, `misc/*.func`, `scripts/` | Installers: Proxmox one-liner, hub, Linux/macOS/Windows machines, API gateway, release scripts |
| `agent-kit/` | Add-ons installed with each agent (`/remember`, `/recall`, second opinion) |
| `docs/01`-`11` | The guides users follow step by step |

## Code rules

- **The runner is standard-library Python and must run on Python 3.9** on Linux, macOS and Windows: no
  `match`, no `X | Y` types at runtime, no 3.10+ APIs. The hub needs Python 3.11+.
- **Ordinary jobs keep their command line.** Extra flags appear only for jobs that use the feature.
- **Escape everything in the dashboard** that comes from a runner, an agent or the user (`esc()`); no raw
  HTML from data.
- **Keep paired copies in sync:** the secret patterns in `runner/runner.py` and `hub/redact.py`, and the
  hidden-character patterns in `hub/static/app.js` and `hub/approvals.py`. `tests/check_repo.py` compares
  the first pair.
- **Names:** the product is "Agent Bullpen"; everything on disk is `bullpen` (`bullpen.service`,
  `/opt/bullpen`, `~/.bullpen`, the `bullpen` command, `BULLPEN_*` settings); the runner's service is
  `bullpen-runner`.
- **Write files atomically** (`write_private`, `write_if_changed`) and never weaken a security check or a
  test to get green.

## Checks

Run these before committing (all run in CI too):

```bash
.venv/bin/python tests/smoke_test.py
```

```bash
.venv/bin/python tests/test_hub_units.py
```

```bash
python3 tests/test_runner_units.py
```

```bash
python3 tests/check_repo.py
```

```bash
python3 tests/test_release.py
```

```bash
shellcheck --shell=bash scripts/*/*.sh misc/*.func
```

`tests/proxmox/test_installer.py` needs root (it only touches a temporary folder). For a narrow change, run
the unit tests that cover it first; run the end-to-end test before you finish.

## Docs and changelog

- The guides are read step by step by people who aren't programmers: plain English, short sentences,
  numbered steps, one command per code block, no marketing words, no emoji. Facts about other products carry
  a link and a "last checked" date.
- Every change visible to users gets a line in `CHANGELOG.md` under the top, unreleased section
  (`## [Unreleased]`, under `### Added`, `### Fixed` and so on). Each release section starts with
  `**Upgrading:** nothing to do.` or the steps, and the version comes from the headings
  ([CONTRIBUTING.md](CONTRIBUTING.md), "Releasing").
- Read only the guide a change touches; update it in the same change.

## Releases

- Versions follow SemVer: Fixed and Security notes make a patch, other notes a minor version, and
  `**Breaking**` entries or `### Removed` a major one. Upgrading steps that aren't breaking don't make a
  major version.
- If `private/RELEASING.md` exists, follow it when asked to cut a release.
