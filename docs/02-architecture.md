# 2 · How it fits together

## The pieces

**Agent Bullpen** (`hub/`) is a small web app: Python with FastAPI, a single SQLite file, and a plain
JavaScript page, with no build step. It keeps the list of machines, the queue of jobs and their results,
and it runs the multi-agent "councils". It does no AI work itself. It runs in a small Proxmox container
([guide 4](04-proxmox-hub.md)) or on any Debian or Ubuntu machine.

**Runner** (`runner/runner.py`) is one Python file that runs on every machine that does work: Linux, macOS
or Windows, as many as you like. It uses only Python's standard library, so it needs nothing beyond Python
3.9 or newer. Every 15 seconds it tells the hub "I'm here, these are my agents and project folders". That
check-in includes the machine's tags, each agent's account label and the newest plan-limit figures Claude
Code reported. The runner also keeps asking "anything for me?" When a job arrives it:

1. starts the agent's official program (for example `claude` or `gemini`) in the chosen project folder,
   with flags that match the autonomy you picked;
2. passes your prompt in on stdin, which avoids Windows' command-length limits and escaping problems;
3. reads the program's live JSON output, turns it into readable log lines (for example "Read src/app.py")
   and sends them to the hub every second;
4. sends back the final answer, the session id (so you can follow up), the tokens the agent reported and,
   for build jobs, a `git diff`.

**Each machine has its own runner token.** You add a machine with **Add a machine** on the dashboard, which
makes a one-time code. The setup on the machine trades the code for a token of its own
([guide 5](05-linux-and-macos-machines.md), [guide 6](06-windows-machines.md)). The hub keeps only a
fingerprint of each token, and **Forget** on the dashboard revokes it.

**Agents** are entries in each machine's `~/.bullpen/runner.json`. The setup creates them; you choose which
and how many:

| Agent | Command it runs | Login it uses |
|---|---|---|
| `claude` | `claude -p --output-format stream-json …` | Claude Code's normal login (`~/.claude`) |
| `claude-<name>`, e.g. `claude-work` | the same, with `CLAUDE_CONFIG_DIR=~/.claude-<name>` | a separate login, for another Claude account |
| `claude-<name>` with an API key | the same, with `ANTHROPIC_API_KEY` set (and its own settings folder) | a Console API key or a LiteLLM key, billed per token |
| `gemini`, `gemini-<name>` | `gemini --output-format stream-json …` | the AI Studio key in `~/.gemini/.env`, or one of its own |
| `selftest` | a fake agent | none (for testing) |
| any other name, preset `text` | any CLI that takes a prompt and prints an answer | whatever that CLI uses ([guide 11](11-adding-agents.md)) |

`CLAUDE_CONFIG_DIR` is Anthropic's documented way to keep several logins side by side. A machine can offer
any number of Claude accounts, and each extra account also gets a command of the same name (for example
`claude-work`) that works in your own terminal.

### Autonomy levels

You choose one per job. It maps to each CLI's own permission system:

| Dashboard | Claude Code | Gemini CLI | Use for |
|---|---|---|---|
| Read only | `--permission-mode dontAsk` (reads allowed; anything that would need approval is refused) | `--approval-mode default` (headless refuses tools that need approval) | questions, reviews, critiques, chat |
| Edit files | `--permission-mode acceptEdits` | `--approval-mode auto_edit` | code changes |
| Edit files, ask me before commands | `--permission-mode acceptEdits --permission-prompt-tool mcp__bullpenjob__approve` (see below) | not available | code changes where you want to see each command first |
| Full auto | `--permission-mode auto` (Claude's safety classifier reviews each command) | `--approval-mode yolo` | builds and tests that need to run commands |

Every council step except "build" and "fix" is read only.

**Edit files, ask me before commands** works with Claude agents only. Claude Code edits files in the project
on its own, and runs file commands inside the project (`mkdir`, `mv`, `cp`, `rm`, `sed`) and read-only
commands (`ls`, `cat`, `grep`, read-only `git`) without asking. Before any other command it asks you on the
job's page, and you press **Allow** or **Deny**. It also always asks before writing its protected paths
(`.git`, `.claude`, `.mcp.json`, shell start-up files), which it never writes on its own
([permission modes](https://code.claude.com/docs/en/permission-modes), last checked 6 November 2026). When
the agent runs several tools at once, several questions can arrive together. This level needs a dashboard
password (`HUB_UI_PASSWORD`): without one, any program on your network could answer for you.

A **subtask** that an agent hands to another machine is read only, unless the hub's
`HUB_DELEGATE_MAX_AUTONOMY` allows more ([guide 4](04-proxmox-hub.md)). Even then it never gets a higher
level than the task that handed it out.

## Shared context: memory, add-ons and MCP servers

The hub also keeps three things every agent should share, and the runners copy them into each agent's own
settings whenever they change ([guide 10](10-shared-memory-and-mcp.md)):

- **Notes.** Pinned ones are imported into every Claude account's `CLAUDE.md` and into `GEMINI.md`. All of
  them are searchable through the hub's own MCP server, `bullpen`, at `/mcp`, with the tools `remember`,
  `recall` and `list_notes`.
- **Add-ons.** The `/remember` skill and command, and "second opinion".
- **The central MCP list.** Servers you add once on the MCP page. By default a remote server is routed
  **through the hub**: agents connect to `/mcp/s/<name>` on the hub with the hub's token, and the hub relays
  their tool calls with the server's real key or OAuth sign-in, which never leave the hub. OAuth servers are
  signed in once, on the dashboard.

Each runner's check-in tells it whether anything changed. If something did, it fetches the new context and
updates:

- Claude Code through its own `claude mcp add-json` command, for each account;
- Gemini CLI's `settings.json`;
- Antigravity's `mcp_config.json`;
- the skill and command folders.

It only touches entries it created.

## Job tools: what a running agent can ask the hub

Some jobs need the hub while they run: a job that asks you before commands sends its questions there, and a
task you let hand out subtasks starts them there. For that the hub has a second MCP server, `bullpenjob`, at
`/mcp/job`. It is separate from the shared memory at `/mcp`, and it answers only to job tokens.

- **Only jobs that need it get a token.** When a runner picks up such a job, the hub gives that job a token of
  its own. Ordinary jobs get none, and their command line stays as it was.
- **The hub keeps only a fingerprint** of each job token. The dashboard and the API never show it.
- **A job token works only while its job runs.** It stops working:
  - the moment you press **Cancel**;
  - when the job ends, whatever way it ends;
  - 5 minutes after the job's time limit runs out;
  - when the job's machine hasn't checked in for 105 seconds.
- **How the agent gets it.** The runner writes the address and the token into a file for that job only,
  `~/.bullpen/jobs/<job number>/mcp.json`, in a folder only the runner's user can open. It hands the file to
  Claude Code with `--mcp-config`, and allows the job's tools by name with `--allowedTools`, except `approve`.
  The folder is deleted when the job ends. The token is never put on a command line, and the runner hides it
  in every log line.
- **Neither token works in the other's place.** The hub's MCP token doesn't open `/mcp/job`, and a job token
  doesn't open `/mcp` or `/mcp/s/<name>`.

| Tool | What it does | Which jobs get it |
|---|---|---|
| `job_info` | says which hub job the agent is running as: its number, machine, agent, project, autonomy and tools | every job with a token |
| `approve` | Claude Code's permission prompt tool: asks you on the job's page whether a command may run, and waits for your answer | jobs set to ask you before commands |
| `list_machines`, `delegate_task`, `task_status`, `cancel_task` | lists the machines you allowed, starts a subtask on one, waits for its answer, cancels it | tasks you let hand out subtasks |

## Live sessions, approvals and Remote Control

These let you take part while an agent works. None of them needs an open port on the machines: everything
travels over the connections the runner and the agent already make to the hub.

- **Messages to a live session.** For a task with "Keep the session open for messages", the runner starts
  Claude Code with `--input-format stream-json` and keeps its input open. Every second the runner sends the
  job's new log lines to the hub, and the hub's reply carries the messages you typed on the job's page. The
  runner writes each one to Claude Code between turns, one turn per message, and sends each answer back
  with the next upload. **End session** works the same way: the agent first answers the messages it already
  has, then its input is closed and the job finishes with the last answer. The session also ends after a
  quiet spell (15 minutes by default) and at the job's time limit.
- **Approvals.** In a job set to ask you before commands, Claude Code calls the `approve` job tool before
  each command it wants to run. The hub shows the question on the job's page, notifies you if
  [notifications](08-daily-use.md#notifications-on-your-phone) are on, and holds the call open until you
  answer. Every 30 seconds it sends a progress message so Claude Code keeps waiting. **Allow** returns
  exactly the input you were shown; **Deny** returns your reason. No answer within 10 minutes
  (`HUB_APPROVAL_TIMEOUT_MIN`) means no. Only the dashboard can answer: job tokens and the MCP token can't.
- **Remote Control.** **Start Remote Control** on a machine's page queues a machine action. The runner
  starts Anthropic's own `claude remote-control` server in the project folder, as a child process of its
  own, and reports its address back with its check-ins. You then drive Claude from claude.ai/code or the
  Claude app; that traffic goes between Claude Code and Anthropic, not through the hub. The runner stops the
  server on **Stop**, after `remote_control_max_hours` (8 by default), when you turn `allow_remote_control`
  off, and when the runner itself stops.

## Agents handing work to other machines, and machine tags

**Machine tags.** Each machine can report a few words about itself: its tags (`tags` in `runner.json`, for
example `gpu` or `security-tools`). The runner adds the operating system (`linux`, `macos` or `windows`). On New task and in a council you can then pick "Any machine tagged …" instead of one machine.

**Accepting.** A machine reports its own tags and account labels, so a machine could claim anything about
itself. A tag or a label counts only after you accept it on the machine's page. The operating-system tag
counts at once. If you withdraw an accepted tag or label, or the machine stops reporting it, it stops
counting straight away.

**How a task for a tag finds its machine.**

- It goes only to a machine that checks in with its own runner token, not the shared `HUB_RUNNER_TOKEN`.
- The machine needs the accepted tag, the agent, the project, a free slot, and everything the task needs
  (for example asking you before commands).
- The account is fixed when you create the task. If the agent has the same accepted label everywhere, that
  label is the account. If it is a different account on different machines, you choose one. An agent
  without an accepted label counts as an account of its own on its own machine, so such a task can't spread
  over machines that might be signed in to different accounts.
- The task waits rather than run under another account. Once a machine takes it, it stays there: a
  follow-up goes to the same machine, because the session lives there.
- Jobs aimed at a machine by name are found separately, so tasks for a tag can't hold them up.

**Subtasks.** On New task you can tick "May hand subtasks to other machines". The agent then gets the
`list_machines`, `delegate_task`, `task_status` and `cancel_task`
[job tools](#job-tools-what-a-running-agent-can-ask-the-hub), and can start jobs on your other machines and
read their answers. The rules:

- **You set the scope on New task:**
  - which machines and tags subtasks may go to (every machine that takes subtasks, unless you narrow it);
  - which projects (by default only projects named like the task's own, and `chat`);
  - which accounts and agents (by default any of yours; untick to narrow it).
- **Machines take subtasks only when you allow it**, with `accept_delegated_tasks` in their `runner.json`
  (off by default), and only when they check in with their own runner token. The task that hands them out
  must also run on a machine with its own token.
- **A subtask can do no more than its task, and no more than the hub allows.** That is read only unless you
  raise `HUB_DELEGATE_MAX_AUTONOMY`. The target agent must have the standard levels of a Claude, Gemini or
  self-test agent, with no permission flags of its own elsewhere in its command line. That way the hub
  knows what each level means for it.
- **One level deep, and capped.** A subtask can't hand out subtasks of its own. A task can hand out at most
  5 subtasks, at most 3 of them queued or running at once (`HUB_DELEGATE_MAX_CHILDREN`,
  `HUB_DELEGATE_MAX_ACTIVE`). All the checks and the new job happen in one step, so parallel calls can't slip
  past a limit or past your Cancel.
- **A subtask's time ends with its task's.** Its time limit is cut to what the task has left.
- **No switching accounts after a limit.** After a usage-limit error in the task or one of its subtasks, new
  subtasks may use only the account that hit it, in that task and in its follow-ups for a week
  ([guide 1](01-accounts-and-rules.md#more-than-one-claude-account)).
  The hub never picks an agent or an account the task didn't name.
- **Everything shows on the dashboard.** Each subtask is an ordinary job titled "Subtask of #N", whose prompt
  says it comes from another agent. The task's page lists its subtasks. Cancelling the task cancels its
  queued subtasks and stops its running ones. When the task ends any other way, its queued subtasks are
  cancelled and the running ones finish by its deadline.

## Councils: how agents work together

A **debate** sends the same request to 2–12 agents. Mix models: Claude and Gemini disagree more usefully than
two copies of the same model.

```
answer   → every participant answers independently
critique → every participant attacks the others' answers (labelled "Response A/B/C", so nobody knows
           which model wrote what)
revise   → (optional) each participant fixes its own answer in light of the critiques
judge    → one agent reads everything and writes: verdict, agreements, disagreements (who's right),
           final answer
```

A **build + review** has one agent do the work and up to 10 others try to break it:

```
build   → the builder works in a project folder (may edit files); the runner records a git diff
review  → reviewers get the task, the builder's summary and the diff; a reviewer on the same machine can
          also open the files
fix     → (optional) the builder checks each finding against the code, fixes the valid ones and says why
          it rejected the rest (it resumes its own earlier session)
```

If one agent fails, the council carries on with the others. It stops only if every agent in a stage fails.
The prompts are in `hub/council.py`; edit them to taste.

## Network and security

```
browser ──http (LAN), or https through a VPN or tunnel──► hub :8080 (dashboard password)
runners ──http (LAN)──► hub :8080 (each machine's own runner token)
agents  ──http (LAN)──► hub /mcp/job (that job's own token)
agents  ──https──► their providers (each machine's own logins)
```

- **Nothing has to be exposed to the internet.** Runners make outbound connections to the hub, and the hub
  never connects to them, so machines need no open ports. To use the dashboard away from home, use a VPN or
  a tunnel rather than opening a router port ([guide 3](03-cloudflare.md)).
- **Plain HTTP on the LAN by default.** The dashboard password, the session cookie, the runner tokens, the
  hub's MCP token, job tokens and every prompt cross your network unencrypted. Anyone who can watch that traffic (another
  device on the same Wi-Fi, a compromised IoT gadget) can read them. On a home network you trust, that's a
  common trade-off. Otherwise put TLS in front of the hub or reach it only through a VPN
  ([guide 3](03-cloudflare.md) shows both).
- **Treat dashboard access like shell access to all your machines.** A "Full auto" job can run commands on
  them, a "local program" MCP server is started on every machine that gets it, and **Update now** replaces
  the runner. Use a long password, and keep the dashboard off the open internet. Read only is the default
  for jobs.
- **Runner tokens.** Anyone holding a machine's token could pretend to be that machine: receive the jobs sent
  to it, and fetch the hub's MCP token and the keys of MCP servers added **directly**. It lives in that
  machine's `runner.json`, readable only by its user. **Forget** the machine on the dashboard to revoke it. A
  machine set up by hand may use the shared token `HUB_RUNNER_TOKEN` from `/etc/bullpen/hub.env` instead;
  give it its own ([guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token)).
- **Job tokens.** A job that asks you before commands or hands out subtasks gets a token of its own for the
  [job tools](#job-tools-what-a-running-agent-can-ask-the-hub). It works only while that job runs. During
  that time, any program running as the runner's user on that machine can read it, other jobs included. A job
  token can ask you a question on the dashboard, never answer one.
- **Subtasks spread a task's reach.** A task allowed to hand out subtasks can start jobs on the machines,
  projects and accounts you chose, and a read-only subtask can read any file in its project. A web page or a
  file the agent reads could try to talk it into that. Allow subtasks only for tasks whose input you trust.
  Tasks for a tag and tasks with subtasks go only to machines with their own runner token, so the shared
  token never receives them.
- **Remote Control is a second way in.** While it runs on a machine, anyone signed in to that Claude
  account can drive Claude in that project folder and approve its commands, and none of that shows in the
  hub. It is off unless the machine's `runner.json` allows it, and it stops after 8 hours.
- **Only add project folders you trust.** In headless mode Claude Code runs a project's own hooks
  (`.claude/settings.json`) and MCP servers (`.mcp.json`) without the trust prompt you'd see in a
  terminal ([details](https://code.claude.com/docs/en/headless)). The runner only works in folders
  you've listed.
- **The hub's MCP token** (memory access, and access to every server routed through the hub) is written into
  each agent's settings on each machine, as are the keys of servers you add **directly**. Keys and sign-ins
  of servers routed through the hub stay in the hub's database. If a machine is lost, forget it and rotate the
  MCP token ([guide 10](10-shared-memory-and-mcp.md#security-notes)).
- **Pinned notes are standing instructions for every agent.** Only you can pin (agents can only ask), and
  the hub neutralises `@` imports in them.
- **Secrets on disk**, each readable only by you or by the hub's service user:
  - `runner.json` on each machine;
  - `~/.gemini/.env`;
  - `/etc/bullpen/hub.env` and `/var/lib/bullpen/hub.db` on the hub;
  - LiteLLM's `.env`, if you use the gateway.

[SECURITY.md](../SECURITY.md) has the whole threat model.

## Sizing

The figures below are rough. Watch your own with `free -m` or Task Manager.

| Where | Item | RAM |
|---|---|---|
| Hub | the hub (Python + Uvicorn, including its MCP endpoint and relay) | about 100 MB; measured at 76–97 MB with 25 and with 200 simulated machines |
| | cloudflared, if you use a Cloudflare Tunnel | about 30–50 MB |
| | **container size** | 1024 MB by default; 512 MB is enough on a tight host; 2048 MB for dozens of machines or several MCP servers through the hub |
| Each machine | runner (idle) | about 30 MB |
| | each running Claude Code or Gemini CLI job | about 300–700 MB, so set "jobs at once" to what the machine can hold |
| Gateway machine (optional) | LiteLLM + Postgres | about 1–2 GB |
| | LibreChat (with MongoDB, Meilisearch, vector DB) | about 1.5–2 GB |

A small always-on box (a mini PC or a Proxmox node) is plenty for the hub. The machines that run agents need
the memory: a machine with 8 GB can usually run one or two jobs at once next to its normal work.

## Why not Cloudflare Workers for the hub?

The agents have to run where your files and programs are, and a Worker can't start programs on your
machines. A Worker *could* host the hub's queue instead of a container, with the runners polling it, but:

- the hub would become a second codebase (TypeScript + D1);
- every idle machine sends about 9,000 requests a day (a check-in every 15 seconds, a poll every 25), so the
  free plan's 100,000 requests per day would run out at about ten machines;
- the container costs only about 100 MB of RAM.

So the hub stays a small self-hosted app. If you use Cloudflare, it handles login, Tunnel and DNS
([guide 3](03-cloudflare.md)).

## Other tools considered (November 2026)

- **Happier** ([happier-dev/happier](https://github.com/happier-dev/happier), MIT) is the closest thing to
  "one app for interactive sessions on all my machines". It's listed as an optional extra in
  [guide 8](08-daily-use.md), with the settings to avoid.
- **Vibe Kanban** is shutting down; **AgentAPI** is archived; **Coder Tasks** is in extended support;
  **PAL MCP (clink)** has had no release since December 2025. None was a safe foundation.
- **CloudCLI / claudecodeui** (AGPL) gives a web UI per machine, but not one view across machines, and
  it has no Gemini.
- **Open WebUI** works well but has a branding clause, so it isn't strictly open source. **LibreChat**
  (MIT) is used instead.
