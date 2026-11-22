# Security

## Reporting a problem

Please **don't open a public issue** for a security problem, because that tells everyone before there is a
fix. Report it privately instead:

1. Open the repository's **Security** tab on GitHub (called **Security and quality** on newer pages).
2. Choose **Report a vulnerability** (GitHub's private vulnerability reporting), or go straight to
   [github.com/rob-paprocki/agent-bullpen/security/advisories/new](https://github.com/rob-paprocki/agent-bullpen/security/advisories/new).
3. Describe what you found, how to reproduce it, and what someone could do with it.

The maintainers answer there, in private. Once the problem is fixed, the release notes credit you, unless
you'd rather they didn't.

## Supported versions

Agent Bullpen is young, so only the newest release gets security fixes. Update with `update` inside the hub's
container (or the way [guide 4](docs/04-proxmox-hub.md#updating) describes). Then press
**Update all runners** on the dashboard.

| Version | Security fixes |
|---|---|
| 1.0.x (newest) | yes |

1.0.0 is the first public release.

## What Agent Bullpen protects, and what it doesn't

These are the things that matter, and where the line is.

### The dashboard is the key to every machine

Anyone who can log in to the dashboard can:

- run commands on every machine, as the user the runner runs as (a "Full auto" task);
- start a program on every machine (an MCP server of the "local program" kind);
- replace the runner on every machine (**Update now**, **Update all runners**);
- answer an agent's request to run a command (**Allow** on a job's page);
- type into a running session;
- start Remote Control on the machines that allow it;
- read every prompt, answer and shared note.

So the dashboard password protects all your machines at once. Use a long, unique one. Keep the dashboard on
your own network, a VPN, or behind a login wall such as Cloudflare Access, and never open its port on your
router ([guide 3](docs/03-cloudflare.md)). There is one password and no user accounts yet, so everyone who
can log in can do everything.

### Tokens

| Token | Where it is | What someone holding it can do |
|---|---|---|
| A machine's **runner token** | that machine's `~/.bullpen/runner.json`, readable only by its user; the hub keeps only a fingerprint | act as that machine: receive the jobs sent to it (prompts included), report results, and fetch that machine's own MCP token (a new one, which stops the one the machine holds) and the keys of MCP servers added **directly**; and, while a task that may hand out subtasks runs there, that task's subtask powers |
| The **shared runner token** (`HUB_RUNNER_TOKEN`, for machines set up by hand) | `/etc/bullpen/hub.env`, and the `runner.json` of each machine that uses it | the same, for any machine name, except tasks that hand out subtasks and tasks for a tag, which only go to machines with their own token. It fetches the shared MCP token, never a machine's own |
| A machine's own **MCP token** (for machines with their own runner token) | that machine's `~/.bullpen/mcp-token.json`, readable only by its user, and its agents' settings; the hub keeps only a fingerprint | read and write the shared notes, and call every MCP server that goes through the hub, with your keys and sign-ins, until you **Forget** the machine or press **New MCP token** on its page |
| The hub's **shared MCP token** | the agents' settings on machines that use the shared runner token or a runner from before per-machine MCP tokens, and any client you set it up in | the same, until you press **Rotate** on the MCP page. `HUB_MCP_SHARED_TOKEN=0` turns it off |
| A **job token** | a file in `~/.bullpen/jobs/<job>/` on the machine running that job, only for jobs that ask before commands or hand out subtasks; removed when the job ends; the hub keeps only a fingerprint. Any program running as the runner's user on that machine can read it while the job runs, other jobs included | while that one job runs: read the job's own details; in jobs set to "ask me before commands", ask you a question on the dashboard (not answer it); in jobs allowed to hand out subtasks, list the machines you allowed and start up to 5 subtasks within that job's scope and limits, and read their answers |
| A **git account's token** (and the path of its SSH key) | that machine's `~/.bullpen/runner.json`, readable only by its user. It never reaches the hub: the hub learns only the account's name, host and kind | sign in to that git host as the account, with whatever access the token has. While a job with that account runs, its token is in the agent's environment, so the agent can use it too |
| **Your Claude account's sign-in** (claude.ai, the Claude app) | wherever you are signed in | while Remote Control runs on a machine: drive Claude in that project folder there, and approve its commands |
| A one-time **enrollment code** | the command **Add a machine** shows | add a machine and get a runner token: so the same as a runner token, until it is used or its hour is up |

A job token stops working the moment you press **Cancel**, when its job ends, 5 minutes after the job's time
limit, and when its machine stops checking in. It doesn't open the shared notes (`/mcp`) or the servers
routed through the hub (`/mcp/s/<name>`).

Approvals are answered only on the dashboard. An agent's question waits on the job's page. Only someone
logged in to the dashboard can press **Allow** or **Deny**; a job token, a runner token or an MCP token never
can. The hub refuses jobs that ask before commands while `HUB_UI_PASSWORD` is empty, because without a
password any program on your network could answer. A question nobody answers within 10 minutes
(`HUB_APPROVAL_TIMEOUT_MIN`) is denied. A request over 20,000 characters is denied without being shown,
since nobody can check that much on a card. On **Allow**, the agent gets exactly the input the card showed.
If an agent tries to approve the hub's own tools, the hub denies it without asking. **Allow for this job**
also lets that same request through again without a card, until the job ends. It matches only the same tool
with exactly the same input (a Bash command's description aside), never another job, and the hub refuses
it for a request with hidden characters or blank lines.

**Forget** on the dashboard revokes a machine's own runner token and its own MCP token at once, and no other
machine is affected. Once a machine has had an MCP token of its own, its runner token never fetches the shared
one again. Every machine held the shared MCP token before its runner got its own, though. If a machine is lost,
follow the steps in [guide 10, "Security notes"](docs/10-shared-memory-and-mcp.md#security-notes): forget it,
rotate the shared MCP token (unless it is off), change the keys of servers added directly, and sign its
agents' accounts out.

The hub compares the shared MCP token in constant time and finds a machine's own by its fingerprint, as it
does runner tokens. Neither the hub nor the runner writes an MCP token to its log. The dashboard shows the
shared one only under **Show**, and never a machine's own. Anything shaped like a machine's MCP token (`bpm_`
and 43 characters) is masked in job logs, answers and diffs.

### Plain HTTP on your network

By default the hub serves plain HTTP. On your network, the dashboard password, the session cookie, every
token above, every prompt, and the questions and answers of approvals travel unencrypted. (Job tokens work
only while their job runs, and questions can be answered only then.) Anyone who can watch your network
traffic can read them. On a home network with only your own devices, that is a common trade-off. Anywhere
else it is a bad one. Use a VPN or TLS for the dashboard and, where you can, for the machines too
([guide 3](docs/03-cloudflare.md)).
Once you always use https, set `HUB_COOKIE_SECURE='1'`.

### What the hub stores

On the hub, readable only by root and the hub's service user:

- `/etc/bullpen/hub.env`: the dashboard password, the shared runner token (if any), Anthropic Admin
  keys for the Usage page (if any), and the notification address and headers (if any; an ntfy topic's name
  works like a password);
- `/etc/cloudflared/bullpen-tunnel.env`: the Cloudflare Tunnel token, if you connected a tunnel;
- `/var/lib/bullpen/hub.db`:
  - jobs with their prompts, logs, answers and token counts (subtasks included, with the job that handed
    each one out);
  - the messages you typed into live sessions and the agent's answers;
  - the questions of jobs that ask before commands (the tool's full input while the job runs, masked with
    the runner's secret patterns once it ends) and your answers;
  - shared notes;
  - the list of machines, the fingerprints of their tokens, the tags, plan-limit readings, account labels
    and Remote Control servers they report, and the tags and labels you accepted;
  - for each task, the name of the git account and the MCP servers you picked (names only), and for each
    machine the git accounts it reports: name, host and kind, and which are its defaults. Never a token, a
    key path, a commit name or an email;
  - the shared MCP token, and a fingerprint of each machine's own MCP token;
  - the MCP servers with their keys and headers, OAuth sign-ins (access and refresh tokens) and your own
    OAuth apps' client secrets;
  - the platform-credit settings you make on the Usage page (each organization's monthly amount and reset
    day, and whether background work may use them) and when the API last refused an organization for lack
    of credit. Those hold no keys. The Admin keys stay in `hub.env`, and are sent only to Anthropic's cost
    report (or to `HUB_ANTHROPIC_COST_URL`, if you set it).

Back the database up like a password store, and keep the backups as private as the hub.

A Claude Code session on its way to another machine (**Move the exact session**, below) is kept only in the
hub's memory, never in the database or a log.

The hub never sees your agents' logins. Claude Code, Gemini CLI and the other agents keep their own logins
and API keys on each machine, in their own folders. The hub only starts them through the runner.

### What runs on the machines

- **Agents act as you.** An agent with "Edit files" changes files in the project; with "Full auto" it runs
  commands. Claude Code's safety classifier checks each command in "Full auto", but that is a safeguard, not
  a sandbox. "Read only" is the default.
- **Ask me before commands.** Claude asks you on the job's page before running a command, except file
  commands inside the project (mkdir, mv, cp, rm, sed) and read-only commands. It always asks before writing
  its own settings, the project's MCP list or git hooks (`.claude`, `.mcp.json`, `.git`). Read the whole card
  before you press **Allow**: a command can do more than the agent's description says. The card shows all
  of the input, with **Allow** after it. Characters that don't show on screen are written as highlighted
  codes, and blank lines in a row as one highlighted line that counts them. Claude edits project files
  without asking, so a command that runs the project's own code (`npm test`, `make`) runs what it wrote.
  An agent with a permission flag such as `--permission-mode` in its `args` or `extra_args` can't use this
  level, because the flag would win over asking.
- **Subtasks.** A task you allow to hand out subtasks can start jobs on the machines, projects and accounts
  you chose. A subtask can read any file in its project, `.env` files included. Only allow it for tasks whose
  input you trust: a web page or a file the agent reads could try to talk it into starting work elsewhere.
  Don't run Full-auto tasks on untrusted content on the same machine at the same time: they could read that
  task's job token. The hub keeps these limits whatever the agent asks:
  - machines take subtasks only when their `runner.json` allows it (`accept_delegated_tasks`, off by
    default), and only with their own runner token;
  - a subtask may only read, unless the hub's `HUB_DELEGATE_MAX_AUTONOMY` allows more, and never does more
    than its task;
  - it goes only to agents whose levels the hub can check (not one with levels of its own, or with a
    permission flag such as `--permission-mode` in its `args` or `extra_args`), one level deep, at most 5
    per task and 3 at once;
  - its time ends with its task's, and cancelling the task cancels it;
  - after a usage-limit error, new subtasks of that task, and of its follow-ups for a week, may use only
    the account that hit it.

  Each subtask is a job on the dashboard, titled "Subtask of #N", and its prompt says it comes from another
  agent.
- **Git accounts.** A task, follow-up, council, clone or pull can use a named git account from the machine's
  `runner.json` ([guide 5](docs/05-linux-and-macos-machines.md#git-accounts)). The runner gives that
  account alone to the job for its host: a per-job SSH settings file, the machine's credential helpers for the
  host switched off, and the token in an environment variable, never in a command line or a file. For a
  `github` or `gitlab` account, `gh` and `glab` get its token, or are signed out when it has none; they never
  use your own login. The kind comes from the host for `github.com` and `gitlab.com`, so set `kind` for a
  GitHub or GitLab server of your own. Git can't ask a terminal or another program for a password in the
  job, so an account without a token fails over HTTPS. The job doesn't start when something else would sign
  in to the host first: a `~/.netrc` entry for the host or a `default` one (on Windows `_netrc` too), or, for a
  task, a remote or a `url.<base>.insteadOf` setting with a user name or password for the host. The job's
  log, answer and diff are masked with the token. What this does not do:
  - The agent runs as you, with the token in its environment, so it can use it. Give the token only the
    access the work needs.
  - A command that an "ask me before commands" job shows you on the dashboard goes to the hub as the agent
    wrote it. If it holds the account's token in plain text, the hub masks it only when the token has a known
    prefix such as `ghp_`, `github_pat_` or `glpat-`.
  - An SSH host alias in your own `~/.ssh/config` that points to the account's host still gets your own
    setup. `core.sshCommand` and `GIT_SSH` are ignored in account jobs, and the log warns when they are set.
    A path-specific `http.<url>.extraHeader` for one longer address still applies.
  - Subtasks and Remote Control get no git account.
  - A clone address with a user name or password in it is refused.
  - The GitHub account rules are in [guide 1](docs/01-accounts-and-rules.md#git-hosts-github-gitlab-and-others).
- **MCP servers picked for a task.** A task can add servers marked "on demand" or limit the agent to the
  servers it lists ([guide 10](docs/10-shared-memory-and-mcp.md#servers-for-one-task-only)). Three things to
  know:
  - **The tools of picked servers are allowed at every level, Read only included.** The dashboard says so
    where you pick: "Tools of the picked servers are allowed even in Read only." A job with picks is
    never retried on credits by itself.
  - **The agent program enforces the limit, not the hub.** The hub's relay at `/mcp/s/<name>` accepts any
    valid MCP token (the shared one or a machine's own) from any agent, so a program holding one can call any
    server routed through the hub, whatever a task picked. Narrowing is Claude Code's `--strict-mcp-config`.
  - **Subtasks never get a task's git account or MCP choice.** This stops an agent from giving itself tools
    beyond the subtask limits.

  The settings file written for a job holds the machine's MCP token and any key of a server added directly. It is
  private to the runner's user and deleted when the job ends. Its secrets are masked in the job's log, answer
  and diff: the job token and anything shaped like one, every value of 6 characters or more, and a shorter
  one when its name or place marks it as a secret (a header value, a URL's password, `API_KEY=12345`,
  `PIN=1234`). A short secret under an ordinary name (such as `CODE=1234`) isn't masked. Give a short secret
  a name with `key`, `token`, `pin` and so on, or use a longer one
  ([guide 10](docs/10-shared-memory-and-mcp.md#secrets-in-the-jobs-file)).
- **Moving a session to another machine.** Only the dashboard can start one, and only you pick where it goes.
  **Carry the conversation** puts the conversation so far into the new task's prompt, with anything that
  looks like a secret masked. **Move the exact session** copies one Claude Code session to a machine signed in
  to the same account:
  - The first machine's runner reads only that session's transcript, by its exact path in the agent's settings
    folder. It never reads the login (`.credentials.json`), the settings or another session, and never
    follows a link.
  - The transcript holds everything the session saw, file contents and command output included. It goes to
    the hub as it is: masking would break it.
  - The hub keeps it in memory and hands it once to the runner of the job it was made for. Then it is gone.
    It is also gone after 10 minutes, when that job ends, or when the hub restarts.
  - The other machine writes it into its own agent's settings folder, readable only by the runner's user. An
    older copy of the same session there is set aside, not overwritten.
- **Tags and account labels are claims a machine makes.** A machine reports its own tags and labels, so they
  count for tasks aimed at a tag and for subtasks only after you accept them on its page. The operating-system
  tag counts at once. A task for a tag runs under the one account it was made for, and only on a machine with
  its own runner token.
- **Remote Control.** While it runs, anyone signed in to that Claude account can drive Claude in that
  folder and approve its commands. The hub doesn't see it. It's off unless the machine allows it
  (`allow_remote_control`). It stops after 8 hours, on **Stop**, when the setting is turned off, and when
  the runner stops. Its error messages reach the hub with at most its last 5 lines, masked, and with e-mail
  addresses hidden.
- **Sign-in status and Sign out.** To show whether a Claude agent is signed in, the runner runs that agent's
  own `claude auth status --json` and keeps only whether it is signed in, the sign-in method, the API
  provider, the plan and when the sign-in ends. The account's email and organization stay on the machine. The runner never reads a
  login's files or the macOS Keychain. Signing in is never relayed: no sign-in link, code or token goes
  through the hub, and the page only shows the command to run on the machine itself. **Sign out…** runs the
  agent's own `claude auth logout` as a machine action. It's off unless the machine allows it
  (`allow_remote_sign_out`), needs the dashboard password, and is refused while that agent runs a job or
  Remote Control.
- **Projects can carry instructions.** In headless mode Claude Code runs a project's own hooks and MCP servers
  without asking. Only add project folders you trust.
- **Notes are instructions too.** Pinned notes go into every agent's context. Only the dashboard can pin, and
  the hub neutralises `@` imports in them. Don't put secrets in notes: every agent can read them, including
  anything an agent fetched from the web.
- **Runner updates are checked, not signed.** A runner checks each update against checksums from the hub, so
  whoever controls the hub controls the runners. Release downloads are checked against the release's
  `SHA256SUMS`, which comes from the same GitHub release. Signing both is on the [roadmap](ROADMAP.md).

### No telemetry

Agent Bullpen sends nothing to this project or to anyone else. The installers download from GitHub (Agent
Bullpen itself), your systems' package mirrors, and the official sources of the tools you choose to install
(Claude Code from claude.ai, Gemini CLI from npm, Node.js from NodeSource or Homebrew, cloudflared from
Cloudflare's package repository). The hub talks only to the services you set up: the MCP servers on your list,
Anthropic's Admin API if you give it keys, Cloudflare if you connect a tunnel, and the notification address
you set in `HUB_NOTIFY_URL`, if any. A notification carries the event, the job's number, a title (only if
you typed one), its status and a link. It never carries a prompt, an answer or a command. The agents talk
to their own providers, as they would without Agent Bullpen. Plan limits come from Claude Code itself: from
its output during jobs, and from its own `/usage` command when you press **Check limits**. The hub and the
runner never call Anthropic for them.

## Out of scope

- Problems in the agents themselves (Claude Code, Gemini CLI and others), in Proxmox, or in the MCP servers you
  add: please report those to their makers.
- Anything that needs the dashboard password or a machine's runner token to begin with, unless it lets
  someone do more than the table above says.
