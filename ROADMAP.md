# Roadmap

What may come next, and why. Nothing here is promised or dated: the order changes with what people need and
what the vendors change. If something you want isn't here, or you'd like to help with something that is,
open an issue ([CONTRIBUTING.md](CONTRIBUTING.md)).

**Size** is a rough guess at the work: **S** is a day or less, **M** a few days, **L** a week or more.
**Status** is one of: *in progress*, *planned* (wanted, not started), *considered* (wanted if it can be done
well), *later* (waits for something, given in the row), *waiting* (for an outside condition).

Facts about other tools were checked against their docs and source in November 2026. They change quickly.

## More agent services

Agent Bullpen runs Claude Code and Gemini CLI fully today, and any other CLI as a plain "text" agent
([guide 11](docs/11-adding-agents.md)). These are the next ones to support fully. That means a live log,
follow-ups, a readiness check, and the shared notes and MCP servers in their own settings.

| What | Why | Size | Status |
|---|---|---|---|
| **OpenAI Codex CLI** (Apache-2.0) | Native on Windows. Its JSON lines are defined in its own source, it resumes by session id, and its sandbox modes map cleanly onto read only / edit files / full auto. It reads `~/.codex/AGENTS.md` and `~/.agents/skills`. Needs a `codex-json` parser and a writer for its TOML MCP settings that keeps the hub's token in an environment variable, not on the command line. OpenAI asks for an API key for automation. | M | planned (first) |
| **OpenCode** (MIT) | Works with almost any model provider (OpenRouter, Ollama, LM Studio, ChatGPT and Copilot logins), so it is the natural "bring any model" agent. Needs a parser, runner-written agent files for read only and edit files, and a check that those files exist: if one is missing, OpenCode quietly falls back to an agent that may edit and run commands. Not for Claude subscriptions, which Anthropic doesn't allow there. | M | planned |
| **Qwen Code** (Apache-2.0) | Its JSON lines are close to Claude Code's, so the existing parser nearly fits, and its MCP settings are like Gemini CLI's. The cheapest of the three. Its own login needs a paid Qwen plan; otherwise it runs on an API key. | S | planned |
| **GitHub Copilot CLI** (closed source) | The best closed-source option: GitHub documents running it from scripts and CI, including on a subscription token. Needs a parser for its JSON events and a session id chosen by the runner (`new_session`). | M | considered |
| **Cursor CLI** (closed source) | Its JSON lines are Claude-like. Cursor's own docs disagree on whether print mode writes files without `--force`, so read only must use `--mode ask`. Cursor asks for an API key for automation. | S–M | considered |

**Later**, each for a reason:

| What | Why it waits |
|---|---|
| Goose | Its permission levels can be set only through an environment variable or its config (now possible with `autonomy_env`), and its JSON output carries no session id, so follow-ups can't work yet. |
| Mistral Vibe | Its streaming output format isn't documented. |
| Kimi Code | Read only works only through an agent file, and whether it takes the prompt on stdin is unverified. |
| Cline CLI | Whether it reads stdin, and whether its JSON carries a session id, are unverified; it has no allow-list for shell commands. |
| Crush | No JSON output, and `crush run` approves everything, so the three levels can't be kept. Its licence (FSL) isn't open source. |
| Amp | Officially needs WSL on Windows, and never asks for approval, so read only can't be enforced. |
| Aider | No MCP support and plain text output. It already works as a text agent with `prompt_via: "arg"`. |

## Model backends

| What | Why | Size | Status |
|---|---|---|---|
| **Ollama as a local model backend** | Local or private models without per-token costs. Ollama is more useful behind the agents than as an agent of its own: per Ollama's docs, Claude Code can use it through `ANTHROPIC_BASE_URL`, Codex with `--oss`, OpenCode as a provider, and Copilot through its provider settings. Needs ready-made agent settings for each, a guide, and testing. Using it takes a machine with a capable GPU, or Ollama's cloud models. | S–M | planned (after Codex and OpenCode) |
| **More providers in the API gateway** | The gateway takes Anthropic organizations and one Gemini key today. OpenAI and others would use the same named-key pattern. | S | considered |

## Platform

| What | Why | Size | Status |
|---|---|---|---|
| **One file per agent** | Adding an agent touches about a dozen files today. A small adapter per agent (preset, parser, readiness check, settings wiring, add-on folder) would make it one file, and the hub could build its list of MCP targets from what machines report instead of a fixed three. | L | planned (with Codex) |
| **One shared skills folder** | Codex, OpenCode, Copilot, Cursor, Qwen, Kimi, Vibe and Amp all read `~/.agents/skills/`. Installing the add-ons there covers them all at once. | S | planned (with the first new agent) |
| **macOS polish** | Macs run the runner as a launchd agent, which runs only while you're logged in, and the dashboard can't see a Mac's Claude login (it's in the Keychain). Needs testing on real Macs, both Intel and Apple silicon, and the rough edges fixed. | S–M | in progress |
| **Per-machine MCP tokens** | Today one MCP token sits in every agent's settings on every machine, so a lost machine means rotating it for everyone. A token per machine would let **Forget** cut that machine off alone. | M | planned |
| **User accounts on the dashboard** | Today there is one password, and whoever has it controls every machine. Accounts with roles (look only, read-only jobs, everything), and machines that belong to a person, would make the hub safer to share in a small team. | L | considered |
| **Signed releases and runner updates** | Installers check a release against its `SHA256SUMS`, and runners check updates against checksums from the hub. Neither is signed, so whoever controls the GitHub account or the hub controls what runs. Sign the checksums, and let runners check a key they got when they enrolled. | M | planned |
| **An uninstaller** | Removing Agent Bullpen takes the manual steps in guides 4 to 6. One command per machine, and one for the hub, should remove the services, files and users it created, and leave the agents' own logins alone. | S | planned |
| **Machine groups** | The dashboard works with many machines (a filter, a list view, a History page, seats spread over machines), and machine tags let a task, a council seat or a subtask go to "any machine tagged …". Next: a sync or an update for every machine with a tag, in one step. | M | in progress |
| **Lighter waiting for work** | Each waiting machine costs the hub two small database queries a second. Fine for dozens of machines; with hundreds, waking runners only when work arrives would be better. | M | planned |
| **More Linux distributions** | The setup is written for Debian, Ubuntu and Kali first; Fedora, Arch and openSUSE are best effort. Needs testing there, and fixes for what breaks. | S each | considered |
| **More services in the Proxmox installer** | The installer is built so another `ct/<service>.sh` can reuse it, for example the API gateway in a container of its own. | M | considered |

## Working across machines and sessions

Agent Bullpen 1.0 has subtasks on other machines, machine tags, live sessions, approvals from the dashboard,
Remote Control and plan limits per account. These would build on them; none is in 1.0.

| What | Why | Size | Status |
|---|---|---|---|
| **Planner councils ("split")** | A read-only plan step writes subtasks, with their tags and which ones depend on which. You approve the plan. The hub runs the subtasks on matching machines, each on its own git branch, and a merge step combines them. Builds on machine tags and subtasks. | L | planned |
| **A cap per account** | At most a set number of runs at once per account label, across all machines. It would only hold jobs back, never move them to another account. | S | planned |
| **Remote Control after a reboot** | Start chosen Remote Control servers again when the runner starts, not only after an update. | S | considered |
| **"Always allow this command in this job"** | One click to let the same command run again in a job without asking. | S | considered |
| **Approvals for Gemini CLI** | Gemini CLI has no hook like Claude Code's permission prompt tool, so "ask me before commands" is Claude only. | — | waiting (for Gemini CLI) |
| **Checking limits on a timer** | Today **Check limits** runs only when you press it, on purpose. A slow timer (an hour or more) may come once `/usage` in print mode and its report are no longer marked experimental. | S | considered |
| **Trusted tags from enrollment** | Let **Add a machine** set a machine's tags and labels up front, so they need no separate Accept. | S | considered |
| **Commit and push between machines** | Branches named by the hub, so a subtask's or a builder's work can move to another machine without manual steps. | M | considered |

## Listing in the community-scripts catalogue

The Proxmox installer looks like the [community-scripts](https://github.com/community-scripts/ProxmoxVE)
installers but is our own code. Being listed in their catalogue would make Agent Bullpen much easier to find.
Status: **waiting**, because a project has to meet their rules first. As read from their contribution rules in
November 2026:

1. The project is at least 6 months old and actively maintained.
2. It has 1,000 or more GitHub stars.
3. It publishes official release archives (Agent Bullpen does, from 1.0.0).
4. The install runs directly in the container (no Docker), as root, under `/opt/<app>`, with data that
   survives a clean reinstall in `/opt/<app>_data`, and Python set up with their `uv` helpers.
5. Updates use their release helpers (`check_for_gh_release`, backup and restore), not `git pull`, and the
   script has only one function, `update_script()`.
6. Every question has a `var_*` setting with a default, declared in the script's metadata.
7. The closing message is their standard three lines and at most one more. The "Add a machine" commands would
   move to the dashboard and the metadata notes.
8. A metadata file in their format, in the category "AI / Coding & Dev-Tools".
9. Tested on Proxmox VE, and ideally on Incus.
10. A new script starts in their DevScripts repository, with an issue, a pull request that follows their
    template (including a disclosure of AI assistance), and community testing.

Some of this conflicts with choices made here (a service user instead of root, data in `/var/lib`), so
listing would mean a separate, simpler script that follows their rules.
