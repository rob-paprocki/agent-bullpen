# Changelog

All notable changes to Agent Bullpen are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The hub and the runner always share one
version number, because the hub hands its own copy of the runner to every machine.

## [1.0.1] - 2026-11-15

**Upgrading:** nothing to do.

### Fixed

- The runner's plan-limit tests no longer start failing eight days after the date their test data uses.

## [1.0.0] - 2026-11-15

**Upgrading:** nothing to do. This is the first release.

The first public release. Agent Bullpen runs the official Claude Code and Gemini CLIs on your own machines,
from one self-hosted dashboard, with any number of machines, accounts per machine and API organizations.

### Added

- **The hub**: a FastAPI and SQLite dashboard, in a Proxmox container or on any Debian or Ubuntu machine, with
  an Overview of every machine, tasks with a live log, follow-ups, a **History** page with filters and paging,
  and a Usage page. Online machines come first, with a filter box, a Cards / List switch and phone layouts
  that fit.
- **The runner**: a standard-library Python program (3.9 or newer) for Linux, macOS and Windows that runs
  each agent's official CLI headlessly, streams its log, and supports cancel, time limits and several logins
  per machine. It runs as a systemd user service, a launchd agent or a scheduled task.
- **Proxmox one-liner.**
  `bash -c "$(curl -fsSL https://raw.githubusercontent.com/rob-paprocki/agent-bullpen/main/ct/bullpen.sh)"`
  on a Proxmox VE host creates the hub container, in the style of the community-scripts installers: a Default
  and an Advanced install, saved defaults, unattended installs and an `update` command inside the container.
  It downloads a release and checks it against the release's `SHA256SUMS`.
- **Machine enrollment.** **Add a machine** on the dashboard makes a one-time code (valid for one hour, usable
  once) and shows one command for Linux and macOS and one for Windows. Each machine then gets its own runner
  token, stored hashed on the hub, and **Forget** revokes it. `HUB_RUNNER_URL` sets the address machines use
  to reach the hub. A shared runner token (`HUB_RUNNER_TOKEN`) is there for machines set up by hand.
- **Any number of Claude accounts per machine.** The first uses `~/.claude`; each other one is `claude-<name>`
  with its own settings folder `~/.claude-<name>` and a command of the same name. Any number of Gemini agents
  too. Two machines that report under one name are flagged, with the other computer's name.
- **Councils**: a debate with up to 12 participants, or build and review with a git diff, a fix round and up
  to 10 reviewers. Seats are spread over machines.
- A model box on tasks and council seats, with suggestions per agent. For Gemini, the models the agent's
  API key can use are listed.
- **A page per machine**: clone, create, pull and remove projects, update the runner, and accept its tags
  and account labels. **Update all runners** and **Sync all machines** on the Overview say which machines
  they reached and why others were skipped.
- **Shared memory**: notes with scopes (`global`, `agent:<name>`, `project:<name>`), tags, pinning and
  full-text search, a Memory page, and the hub's own MCP server (`/mcp`, named `bullpen`) with `remember`,
  `recall` and `list_notes`.
- **A central MCP list** on the MCP page, which runners write into every agent's own settings: Claude Code
  (each account), Gemini CLI and Antigravity, per platform (Linux, macOS, Windows).
- **MCP relay.** A remote server on the central list can be served by the hub at `/mcp/s/<name>`, which
  is the default. Agents connect there with the hub's token, and the hub calls the server with the real key
  or sign-in, which never leaves the hub. You sign in to an OAuth server once, on the dashboard, with
  automatic client registration or your own OAuth app, and the hub renews the tokens for every agent.
- **Live sessions.** "Keep the session open for messages" on New task (Claude agents): type to the agent on
  the job's page while it works, then **End session**, or let it end after a quiet spell (15 minutes by
  default) or at the time limit.
- **Approvals from the dashboard.** The level "Edit files, ask me before commands": Claude asks on the job's
  page before running a command, and you press Allow or Deny, with a reason if you like. No answer in 10
  minutes means no (`HUB_APPROVAL_TIMEOUT_MIN`).
- **Remote Control from the dashboard.** On machines that allow it (`allow_remote_control`, off by
  default), **Start Remote Control** on a machine's page runs Anthropic's own `remote-control` server for a
  Claude agent with a full subscription login and a project. **Stop** ends it. It also ends after 8 hours
  (`remote_control_max_hours`) and when the runner stops. When Claude Code refuses to start it, the machine's
  page says why and what to do.
- **Subtasks on other machines.** With "May hand subtasks to other machines" ticked on New task, an agent can
  list the machines you allow, start subtasks on them and read their answers. Subtasks are read only (unless
  your hub allows more) and never get more than the task itself. They run in a project named like the task's
  (or `chat`) unless you allow any, and with any of your agents unless you untick some. They go one level
  deep, at most 5 per task, and only to machines that take subtasks (`accept_delegated_tasks`). History and
  the job page show the tree; cancelling a task cancels its subtasks. The job tools live at `/mcp/job`
  (server `bullpenjob`).
- **Machine tags** (up to 15 in `runner.json`, plus the operating system). A task, a council seat or a
  subtask can go to "any machine tagged …".
- **Plan limits per Claude account** on the Usage page and each machine's page: the 5-hour, weekly and
  per-model windows, read from Claude Code's own output when jobs run, or with **Check limits now**. An
  optional `account` label per agent joins the readings from every machine. `runner.py limits` prints them
  on a machine.
- **Platform credits first.** The Usage page tracks the monthly API credits some Claude plans include, per
  Console organization. It shows spend this billing cycle and average daily spend. Once you enter the amount
  and the reset day, it also shows what is left, the next reset and when the credits run out at this rate.
  All of them are labelled as estimates. A machine can name one Claude agent with an API key as its
  `credits_agent` (asked in `runner.py init`, shown in `runner.py list` and on the machine's page). With
  **Use platform credits first for background work** on (off by default), council steps and subtasks named
  for a subscription agent run on that machine's credits agent while its organization has credit available,
  a task stopped by a usage limit is retried there once (or offers **Retry on credits**), and a job that
  finds the credits used up says **Platform credits used up** and runs again on the agent it named. Work
  never moves from one subscription account to another.
- **Tokens per job** and an API-equivalent estimate, with a 30-day table per agent on the Usage page, API
  spend per Console organization (`HUB_ANTHROPIC_ADMIN_KEYS`), and `HUB_USAGE_PROVIDERS` to choose which
  providers' links it shows. A job that hits a usage or quota limit says which one and when it resets.
- **Git accounts.** Name your GitHub, GitLab or other git logins in a machine's `runner.json` (an SSH key, an
  HTTPS token, a commit name and email), or with `runner.py init`. Then pick one per task, follow-up, council,
  clone or pull with the **Git account** box, or set a default for the machine or for one project. A job uses
  only that account for its host. For a `github` or `gitlab` account, `gh` and `glab` get its token, or are
  signed out when it has none, so they don't fall back to your own login. The kind comes from the host for
  `github.com` and `gitlab.com`; set `kind` for a GitHub or GitLab server of your own. Switching accounts in
  a follow-up doesn't change which machine, agent or AI account runs the job. Tasks for a tag and subtasks
  can't pick one. Every git account needs git 2.31 or later on the machine. `runner.py list` and the
  machine's page show the accounts and what is wrong with them, an older git included, never a key or token.
  [Guide 5](docs/05-linux-and-macos-machines.md#git-accounts) has the settings and what they don't cover.
- **MCP servers per task.** Mark a server **Only for tasks that pick it** on the MCP page, and no agent has it
  until a task adds it. New task, follow-up and council get an **MCP servers** box for Claude Code agents:
  **Add for this task**, and **Only these servers** to limit the agent to the ones you tick. The tools of
  picked servers are allowed even in Read only, and Claude Code, not the hub, enforces the limit. Subtasks
  never get a task's picks. [Guide 10](docs/10-shared-memory-and-mcp.md#servers-for-one-task-only) explains it.
- **Job summaries in shared memory.** Two switches on the Memory page, both off at first. **Summarize jobs**
  has the agent write a short summary when a task is done, as one extra short turn on the same AI account,
  and the hub keeps it as an unpinned note tagged `job-summary`. **Start new tasks from the project's recent
  summaries** puts the newest three in front of a new task's prompt, as context. New task and council get a
  box for each. A follow-up gets only the summary box, because it continues its session. Subtasks never
  summarize, and a summary is never pinned automatically.
  [Guide 10](docs/10-shared-memory-and-mcp.md#job-summaries) explains it.
- **Notifications** when a task finishes or fails, or a job needs your approval (`HUB_NOTIFY_URL`, ntfy or a
  JSON webhook). They carry the job number, a title you typed (if any), the status and a link, never prompts
  or answers.
- **Follow up** on any finished job, also after an error, a cancel or a time-out, with another autonomy
  level or model; a chain reads as one conversation. **Continue in a terminal** shows the command that
  resumes the session on that machine.
- **The agent kit**: the `/remember`, `/recall` and "second opinion" add-ons for Claude Code, Gemini CLI and
  Antigravity, and the `bullpen` command on every machine.
- Runner options for other agent CLIs: `prompt_via` ("stdin" or "arg"), `autonomy_env`, `new_session` and
  the `{workdir}` template. [Guide 11](docs/11-adding-agents.md) explains how to add a CLI.
- **The API gateway**: LiteLLM and LibreChat, set up by one script, for any number of named Console
  organizations (`work`, `personal`, ...).
- Step-by-step guides for accounts, the hub, Cloudflare (optional; also LAN only, Tailscale or WireGuard,
  and Caddy with TLS), Linux, macOS and Windows machines, daily use and troubleshooting.
- Tests and CI: an end-to-end test with a fake agent on Python 3.11 and 3.13, unit tests for the hub and
  the runner, the runner on Windows and macOS, shellcheck, the Proxmox installer tests and
  `tests/check_repo.py`.
- Releases: a `vX.Y.Z` tag builds `agent-bullpen-X.Y.Z.tar.gz` and `SHA256SUMS` and publishes them with this
  changelog's notes. `scripts/release/set-repo.sh` moves every link to a new repository name.

### Security

- A clone URL with a user name or password in it is refused; put credentials in a git account instead.
  GitLab tokens are masked in logs.
- A job, clone or pull with a git account is refused when something else would sign in to the account's host
  first. That covers a `~/.netrc` entry for the host or a `default` entry (on Windows `_netrc` too). For a
  task it also covers a remote, or a `url.<base>.insteadOf` setting, with a user name or password for the
  host (over HTTPS or HTTP), and a setting that adds them to every address. In such a job git has no terminal or other program to ask for a password, so an account without a
  token fails over HTTPS.
- A job with a git account hides that account's token in the job's live log, answer and diff (SECURITY.md
  lists what this doesn't cover). The secrets in the settings file of a job with picked MCP servers are masked the same way: every
  value of 6 characters or more, and a shorter one when its name or place marks it as a secret, such as a
  header value or `API_KEY=12345`. A short secret under an ordinary name (such as `CODE=1234`) isn't masked,
  so give it a name with `key`, `token` or `pin`, or use a longer one.
  [Guide 10](docs/10-shared-memory-and-mcp.md#secrets-in-the-jobs-file) has the rules.
- The dashboard needs `HUB_UI_PASSWORD`, refuses writes from another origin, and can't be shown inside
  another site's frame. Cloudflare Access tokens are checked against the team's keys, and an Access login
  reaches the shared memory only, not the relayed servers.
- Each machine has its own runner token, kept on the hub only as a hash. Tasks for a tag, tasks that hand out
  subtasks, and subtasks go only to machines with their own token, and tags and account labels a machine
  reports count only after you accept them.
- Job tools never use the shared MCP token: only jobs that ask before commands or hand out subtasks get a
  token of their own, kept only as a fingerprint and revoked when the job is cancelled or ends.
- Approvals can only be answered on the dashboard; an unanswered question is denied, and a request too long
  to show (an input over 20,000 characters, or a tool name over 200) is denied unseen. An approval card shows
  the whole input, however long, and draws runs of blank lines as one highlighted line that counts them;
  the job log counts hidden or blank characters instead of printing them.
- Subtasks are off unless you allow them per task, within the machines, projects and accounts you choose;
  machines take them only when `accept_delegated_tasks` is on, and `HUB_DELEGATE_MAX_AUTONOMY` (read only by
  default) caps what any subtask may do. After a usage-limit error, new subtasks of that task, and of its
  follow-ups for a week, may use only the account that hit it.
- A credits agent must sign in with an API key and have an accepted account label that names an organization
  in `HUB_ANTHROPIC_ADMIN_KEYS`; the hub never moves work to a subscription agent, and decides only by the
  credits' estimated balance, never by a subscription's remaining usage. A subtask moves to credits only
  within the agents and accounts its task allows for subtasks.
- An agent with a permission flag such as `--permission-mode` or `--yolo` in its `args` or `extra_args`
  takes no subtasks and can't use **Edit files, ask me before commands**, and the dashboard names the flag.
- Secrets in log lines and error messages are masked, with patterns whose time grows only with the line's
  length. Keys, tokens and URLs with credentials stay out of error messages.
- Only the dashboard can pin notes, pinned notes can't import files, and read-only jobs can look notes up
  but not save them.
- The installers download a release and check it against its `SHA256SUMS` before installing it.

[1.0.1]: https://github.com/rob-paprocki/agent-bullpen/releases/tag/v1.0.1
[1.0.0]: https://github.com/rob-paprocki/agent-bullpen/releases/tag/v1.0.0
