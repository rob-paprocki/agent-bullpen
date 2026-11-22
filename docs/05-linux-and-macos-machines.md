# 5 · Linux and macOS machines

**Goal:**

- The machine runs Claude Code, signed in to one or more Claude accounts, and optionally Gemini CLI.
- A **runner** connects it to the hub with its own token, and starts by itself.
- The machine shows up as "online" on the dashboard.

This guide works for any Linux desktop or server and for a Mac. The setup is written first for Debian,
Ubuntu and Kali. Other Linux distributions (Fedora, Arch, openSUSE and so on) work on a best-effort basis.
Repeat it on each machine you want to use; there is no limit on how many.

You need:

- the hub, installed and running ([guide 4](04-proxmox-hub.md)), with its dashboard open in your browser;
- the logins for the Claude account or accounts you want to use on this machine;
- optionally, a **Gemini API key** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
  [Guide 1](01-accounts-and-rules.md) explains why it's a key and not a Google AI Pro login.

> Run all of this as your **normal user**, not root. Claude Code limits what it does as root, and the logins
> belong in your home folder. The setup uses `sudo` only to install packages. If a Linux machine only has
> the `root` user, create one first with `adduser <name>` and `usermod -aG sudo <name>`, then log in as that
> user.

## The quick way: Add a machine

1. On the dashboard, open **Overview** and click **Add a machine**.
2. Type a name for the machine, for example `build-box`, or leave it blank to use the machine's host name.
3. The hub shows a one-time code and two commands. The code works once, and only for one hour.
4. On the new machine, open a terminal as your normal user and paste the **Linux and macOS** command. It
   looks like this, with your hub's address and your code:

   ```bash
   curl -fsSL 'http://192.168.100.20:8080/enroll/K7QM-2XTA-9RFD-WP3H/install.sh' | bash
   ```

5. Answer the setup's questions. [What the setup does](#what-the-setup-does) explains each one.
6. When it finishes, open the dashboard's **Overview**. The machine is online, with its agents shown as
   chips. A chip with a warning sign means that agent isn't ready yet; hover over it to see why.

**What the command does.** It downloads a short script from your hub. That script downloads the runner and
the setup script, also from your hub, checks each file's checksum, and runs the setup. The setup trades the
code for the machine's own runner token. Nothing comes from anywhere else, except the tools the setup
installs (Claude Code from Anthropic, Gemini CLI from npm, packages from your distribution).

**To read it first**, run the same address without `| bash`. Reading it doesn't use up the code:

```bash
curl -fsSL 'http://192.168.100.20:8080/enroll/K7QM-2XTA-9RFD-WP3H/install.sh' | less
```

**To choose the name on the machine instead**, add it at the end of the command:

```bash
curl -fsSL 'http://192.168.100.20:8080/enroll/K7QM-2XTA-9RFD-WP3H/install.sh' | bash -s -- --name build-box
```

**If the code expired or was already used**, the command says so and changes nothing. Click **Add a machine**
again for a new code. A code you don't use expires on its own.

**The address in the command** is the hub's LAN address, `HUB_RUNNER_URL` in the hub's settings
([guide 4](04-proxmox-hub.md)). Machines use it every few seconds, so it should be the direct address on
your network, not one behind Cloudflare Access.

## The manual way

Use this when you'd rather work from a copy of the repository.

1. Get the repository. On a Mac without git, run `xcode-select --install` first; it installs Apple's
   Command Line Tools, which include git and Python.

   ```bash
   git clone https://github.com/rob-paprocki/agent-bullpen.git ~/agent-bullpen
   ```

   ```bash
   cd ~/agent-bullpen
   ```

2. Run the setup script:

   ```bash
   bash scripts/linux/setup-workstation.sh
   ```

3. When it asks how to connect to the hub, give the hub's LAN address and then either:
   - a **one-time code** from **Add a machine** on the dashboard (better: the machine gets its own token,
     which you can revoke separately), or
   - the hub's **shared runner token** (`HUB_RUNNER_TOKEN` in `/etc/bullpen/hub.env` on the hub).

You can also give these on the command line, so the script doesn't ask:

| Option | What it is |
|---|---|
| `--hub URL` | the hub's address, for example `http://192.168.100.20:8080` |
| `--code CODE` | a one-time code from **Add a machine** |
| `--token TOKEN` | a runner token, instead of a code |
| `--name NAME` | the machine's name on the dashboard |

## What the setup does

It goes through eight steps and asks before each one. Running it again is safe: finished steps are skipped.

**1. Basic tools.** It installs git, curl and Python 3.9 or newer if any are missing. On Linux it uses `apt`
(Debian, Ubuntu, Kali), or `dnf`, `pacman` or `zypper` on other distributions. On a Mac it uses Apple's
Command Line Tools; if they are missing, it opens their installer and asks you to run the setup again
afterwards. It uses Homebrew if you have it. It also adds `~/.local/bin` to your PATH in `~/.bashrc` and
`~/.zshrc`.

**2. The runner and the hub.** It copies the runner to `~/.bullpen/runner/` and connects the machine to the
hub. With a code, the hub gives the machine its own runner token. It is saved in `~/.bullpen/runner.json`,
which only your user can read. The hub keeps only a fingerprint of it.

**3. Claude Code.** It installs Claude Code with Anthropic's official installer.

**4. Gemini CLI (optional).** Gemini CLI needs Node.js 20 or newer. Debian 12 and Ubuntu 24.04 come with
Node.js 18, so on those the script offers two ways to get a newer one:

- **NodeSource's packages.** The script adds `deb.nodesource.com` to apt and installs the current long-term
  release. Updates then come with your normal system updates.
- **nvm**, which installs Node.js in your home folder without sudo. Follow
  [nvm's install steps](https://github.com/nvm-sh/nvm#installing-and-updating), run `nvm install --lts`,
  then run the setup again. The runner's service is given that Node.js version's folder; if you later switch
  versions with nvm, run the setup again so the service follows.

On a Mac it uses Homebrew (`brew install node`), or asks you to install the LTS from
[nodejs.org](https://nodejs.org). Gemini CLI itself goes into `~/.npm-global`, so no root is involved.

**5. The agents this machine offers.** The runner asks its setup questions:

| Question | Answer |
|---|---|
| Hub address, name, code | already filled in when you used **Add a machine** |
| How many jobs at once | `1` is safe. Raise it on a machine with plenty of memory: each running job is a separate agent program. |
| Tags for this machine | blank, or a few words such as `gpu, security-tools` ([Tags and subtasks](#tags-and-subtasks)) |
| Projects folder, and letting the dashboard manage projects and updates | the defaults (`~/projects`, yes, yes) |
| Let the hub add shared notes, `/remember` and your MCP servers to the agents' settings? | yes ([guide 10](10-shared-memory-and-mcp.md)) |
| Let agents on your other machines hand subtasks to this machine? | no (the default), unless you want tasks on other machines to start work here ([Tags and subtasks](#tags-and-subtasks)) |
| Let the dashboard start Remote Control on this machine? | no (the default), unless you want to start it from the dashboard ([Remote Control](#remote-control)) |
| Add a Claude agent? | yes, once for each Claude account you want to use here (see below) |
| Add a Gemini agent? | yes if you have a key. The first Gemini agent saves its key in `~/.gemini/.env`, which Gemini CLI reads by itself. |
| Account label (the last question for each agent) | blank, or a word for the account the agent is signed in to ([Account labels](#account-labels)) |
| Self-test agent? | yes; it checks the plumbing without using any quota |
| Add a project folder? | add the folders you want agents to work in (you can add more later) |

**Claude agents.** Each Claude agent is one Claude login with its own settings folder:

- The first is called `claude`. It uses Claude Code's normal login, in `~/.claude`.
- Give each of the others a short name. `work` becomes the agent `claude-work`, with its login in
  `~/.claude-work`. There is no limit on how many.
- For an agent billed per token instead of a subscription, choose **API key** when it asks how the agent
  signs in ([guide 7](07-api-gateway.md) explains when you'd want that).

[Guide 1](01-accounts-and-rules.md) covers what the providers' rules say about using more than one account.

**6. Signing in.** For each Claude agent that isn't signed in yet, it runs `claude auth login` with that
agent's settings folder, and you finish the login in your browser. Each extra account also gets a command of
its own, for example `claude-work`, which starts Claude Code with that account's login.

> **Don't mix up the accounts.** The login opens your default browser, and that browser decides which Claude
> account you sign in to. Before each login, make sure the browser is signed in to the right account. The
> easy way is one browser profile per Claude account: when the login page opens in the wrong profile, copy
> its address into the right one. Choose the option to sign in with your **Claude subscription**. To check
> which account a command uses, run it (for example `claude-work`) and type `/status`.

**7. Starting the runner.** It offers to send a one-word test to each agent, then sets the runner up to
start by itself:

- **Linux:** a systemd user service. It also turns on "linger", which keeps user services running when
  you're logged out and starts them at boot.
- **macOS:** a launchd agent (`~/Library/LaunchAgents/bullpen.runner.plist`). It starts when you log in
  and runs while you stay logged in. See [Keeping a Mac available](#keeping-a-mac-available).
- **Linux without systemd** (some containers, WSL without systemd): it says so and shows the command to start
  the runner yourself.

**8. Shared context sync.** This runs by itself and asks nothing. It adds your pinned notes, the `/remember`
command, the "second opinion" add-on and the servers from the hub's MCP page to every Claude account, Gemini
CLI and Antigravity on this machine. The `remember` skill also goes to `~/.agents/skills`, for other agent
CLIs such as Codex and OpenCode. After that the runner keeps them up to date. If you answered "no" to
letting the hub manage agent settings in step 5, it changes nothing. See
[guide 10](10-shared-memory-and-mcp.md) and
[second opinions](08-daily-use.md#second-opinions-inside-an-interactive-session).

## Adding agents later

To add another Claude account or Gemini agent, run the runner's setup questions again, then the setup script,
which signs in to the new accounts. Existing agents are kept unless you say no to them.

```bash
python3 ~/.bullpen/runner/runner.py init
```

```bash
bash ~/.bullpen/setup/setup-workstation.sh
```

(Use `scripts/linux/setup-workstation.sh` in your copy of the repository if you set the machine up the manual
way.) A running runner picks up the new agents within 15 seconds.

## Adding and removing project folders

The easiest way is the hub: open the machine's page to clone a repository or create a project in its
projects folder ([guide 8](08-daily-use.md#a-machines-own-page)). To offer a folder that lives
elsewhere on the disk, add it on the machine itself. The runner only works in folders you've listed,
both for safety and so the dashboard can offer them in a list:

```bash
python3 ~/.bullpen/runner/runner.py add-project myapp ~/code/myapp
```

```bash
python3 ~/.bullpen/runner/runner.py remove-project myapp
```

```bash
python3 ~/.bullpen/runner/runner.py list
```

A running runner picks up changes within 15 seconds; no restart is needed. Only add folders you trust. The
reason is in [guide 2](02-architecture.md#network-and-security).

## Choosing models per agent

You pick the model per task in the dashboard. Two optional settings per agent in `~/.bullpen/runner.json`
change what you see there:

- `"model_name"` is the model the agent uses when you leave the box blank;
- `"models"` is the list of suggestions the dashboard offers.

For example:

```json
"gemini": {
  "preset": "gemini",
  "label": "Gemini",
  "command": "/home/you/.npm-global/bin/gemini",
  "model_name": "auto",
  "models": ["auto", "gemini-3-pro-preview", "gemini-3-flash-preview"]
}
```

Claude Code accepts aliases such as `opus`, `sonnet` and `haiku`, or full model ids. For Gemini, use the ids
from [Google's model list](https://ai.google.dev/gemini-api/docs/models).

**Letting read-only agents search the web.** In read-only mode Claude Code refuses anything that would
normally ask for permission, and that includes web search and fetching pages. To allow those for an agent,
add an `autonomy` override to its entry:

```json
"autonomy": { "readonly": ["--permission-mode", "dontAsk", "--allowedTools",
                              "WebSearch,WebFetch,mcp__bullpen__recall,mcp__bullpen__list_notes"] }
```

The override replaces the runner's own read-only list, so keep `mcp__bullpen__recall` and
`mcp__bullpen__list_notes` in it, or read-only jobs can no longer search the shared memory.

The same `"autonomy"`, `"args"` and `"extra_args"` keys let you adjust any agent's flags if a CLI
changes, without editing the runner. Put permission flags such as `--permission-mode` only in `"autonomy"`:
in `"args"` or `"extra_args"` they would win over every level's, so the agent no longer takes subtasks
or asks you before commands ([guide 11](11-adding-agents.md#every-setting-an-agent-entry-can-have)).

## Account labels

The dashboard's **Usage** page shows each Claude account's plan limits ([guide 8](08-daily-use.md#usage)).
On its own it can't tell that two agents, on one machine or on two, are signed in to the same account. An
**account label** tells it, so their figures show on one card.

`runner.py init` asks for a label as each agent's last question. You can also set it in
`~/.bullpen/runner.json`, as `"account"` in the agent's entry:

```json
"claude-work": {
  "preset": "claude",
  "env": {"CLAUDE_CONFIG_DIR": "/home/you/.claude-work"},
  "account": "work"
}
```

- Use one label only for the very same account, and the same label on every machine signed in to it. Two
  different accounts under one label would look like one account on the Usage page.
- What the label stands for: the claude.ai account for a Claude login, the Console organization for a Claude
  agent with an API key, and the Google Cloud project behind the key for a Gemini agent.
- A label is up to 40 letters, digits, spaces, dots, dashes or underscores, and starts with a letter or digit.
- An agent without a label is an account of its own on the dashboard, shown as its machine and agent name.
- The Usage page shows a label as soon as the machine reports it. For tasks aimed at a tag and for subtasks,
  a label counts only after you accept it on the machine's page ([Tags and subtasks](#tags-and-subtasks)).
- The hub never picks, holds back or moves work because of a label's limits
  ([guide 1](01-accounts-and-rules.md#more-than-one-claude-account)).

`runner.py list` shows each agent's label, and a running runner picks up a change within 15 seconds.

To see the plan-limit figures this machine last read, without the dashboard:

```bash
python3 ~/.bullpen/runner/runner.py limits
```

Add an agent's name (for example `limits claude-work`) for that agent only. The figures come from the
agents' own jobs and from **Check limits**, so an agent that hasn't run yet has none.

## A credits agent

Some Claude plans include monthly API credits in a Console organization
([guide 1](01-accounts-and-rules.md#platform-credits-the-api-credit-in-your-plan)). A **credits agent** is a
Claude agent on this machine that signs in with an API key from that organization. Once you turn on
**Use platform credits first for background work** on the Usage page
([guide 8](08-daily-use.md#platform-credits)), the hub can run council steps and subtasks meant for this
machine's subscription agents on the credits agent instead.

`runner.py init` asks for it after the agents, when the machine has a Claude agent with an API key. The
answer is none unless you name one. You can also set it in `~/.bullpen/runner.json`, and a running runner
picks up the change within 15 seconds:

```json
"credits_agent": "claude-api"
```

For it to count:

1. It must be a Claude agent with an API key (`ANTHROPIC_API_KEY` in its `env`). The runner ignores any
   other agent here, and says so in its log and in `runner.py list`.
2. Give it an account label equal to the organization's name in `HUB_ANTHROPIC_ADMIN_KEYS` on the hub
   (case doesn't matter), for example `"account": "Work"` ([Account labels](#account-labels)).
3. Accept that label on the machine's page.

`runner.py list` shows the credits agent, and the machine's page on the dashboard shows it with its
organization's state.

## Git accounts

A **git account** is a named git login on this machine: a commit name and email, an SSH key, an HTTPS token,
or a mix. You pick one for a task, a follow-up, a council, a clone or a pull
([guide 8](08-daily-use.md#picking-a-git-account-and-mcp-servers)). Without accounts, jobs use the machine
user's own git setup, as before.

A git account only picks credentials on this machine. Which machine, agent or AI account runs a job stays
as it was. Check your git host's rules before you set up a second account on it
([guide 1](01-accounts-and-rules.md#git-hosts-github-gitlab-and-others)).

### Setting them up

Run the setup questions and answer the **Git accounts** step. It asks for one account at a time:

```bash
python3 ~/.bullpen/runner/runner.py init
```

Or edit `~/.bullpen/runner.json` yourself. A running runner picks up the change within 15 seconds. This
example has a personal account that uses an SSH key, a GitHub machine account that uses a token, and a
self-hosted GitLab:

```json
"git_accounts": {
  "personal": {"host": "github.com", "name": "Your Name", "email": "you@example.com",
               "ssh_key": "~/.ssh/id_ed25519"},
  "bot":      {"host": "github.com", "token": "github_pat_EXAMPLE-TOKEN-0123456789",
               "name": "build-box bot", "email": "bot@example.com"},
  "lab":      {"host": "gitlab.example.com", "kind": "gitlab", "token": "glpat-EXAMPLE-TOKEN-0123456789",
               "name": "Your Name", "email": "you@example.com"}
},
"default_git_account": "personal",
"project_git_accounts": {"homelab": "lab"}
```

The fields of an account:

| Field | Meaning |
|---|---|
| name of the account | 1 to 32 lowercase letters, digits or dashes, starting with a letter or digit. This is what you pick on the dashboard. |
| `host` | Required. The host name only, such as `github.com` or `gitlab.example.com`: no `https://`, no path, no `user@`. A port after a colon is the host's **SSH** port. |
| `kind` | `github`, `gitlab` or `other`. Left out, it is `github` for `github.com`, `gitlab` for `gitlab.com` (with or without a port) and `other` for anything else. Set it for a GitHub or GitLab server of your own. |
| `name`, `email` | The commit author and committer for the job. |
| `ssh_key` | The path of a private key file on this machine. |
| `token` | An HTTPS access token, at least 8 characters. |
| `user` | The user name sent with the token. The default is `x-access-token`. Some hosts want the real user name. |

An account needs an `ssh_key`, a `token`, or a `name` or `email`. An entry that doesn't pass these checks is
left out. `runner.py list` shows it with the reason, and so does the machine's page, so a typo is visible
before it stops a job.

Two more settings choose the default:

- `default_git_account` is the account for every project, `chat` included.
- `project_git_accounts` gives one project its own default.

A task that picks an account always uses it. A default applies only when the task picks nothing. It is
skipped for an agent whose own `env` in `runner.json` sets `GIT_SSH_COMMAND`, `GIT_AUTHOR_*`,
`GIT_COMMITTER_*`, `GIT_CONFIG_COUNT`, `GH_TOKEN`, `GH_ENTERPRISE_TOKEN` or `GITLAB_TOKEN`. That agent
keeps its own git setup, and the job's log says the default was skipped. This means the older way of
working, one agent entry per git login, keeps working. A default that names an account that doesn't exist
or is invalid stops the jobs it would apply to. Pick an account or **None** for the task, or fix
`runner.json`.

To see what the runner understood, without any key or token shown:

```bash
python3 ~/.bullpen/runner/runner.py list
```

Its **Git accounts** section lists each account with its host, kind and what credentials it has, then the
problems, then the defaults. A default for a project that isn't on this machine yet says "applies when that
project is cloned".

### An SSH key or a token

| | SSH key | Token |
|---|---|---|
| Works for | `git@host:…` and `ssh://` addresses | `https://` addresses |
| Where it lives | the key file stays where it is; `runner.json` holds its path | in `runner.json`, which only your user can read, like an API key in an agent's `env` |
| Needs | the key file to exist, with no passphrase prompt (the job has no terminal) | nothing more |

Every git account needs git 2.31 or later on this machine, whatever it holds. With an older git, a job that
uses an account stops before it starts, and the machine's page says so.

An account may have both. Then both kinds of address work for its host.

### What a job with an account gets

A job, clone or pull with an account uses that account alone for its host:

- **Commit name and email.** `GIT_AUTHOR_NAME`, `GIT_COMMITTER_NAME`, `GIT_AUTHOR_EMAIL` and
  `GIT_COMMITTER_EMAIL`.
- **SSH.** A settings file in the job's private folder gives the account's key to its host only
  (`IdentitiesOnly yes`), and git uses it through `GIT_SSH_COMMAND`. Your own `~/.ssh/config` still applies to
  every other host. An account without a key can't sign in to its host over SSH at all, so an `ssh` or
  `git@` address fails rather than use your own key.
- **HTTPS.** The machine's credential helpers for the host are switched off for the job. If the account has
  a token, git gets a helper that reads it from the environment (`BULLPEN_GIT_TOKEN`), never from a command
  line or a file. An account without a token makes HTTPS to its host fail. That also holds for an account
  with only a name and email.
- **The `gh` and `glab` programs.** For a `github` or `gitlab` account, they get an empty settings folder
  of their own, so they never fall back to your own login. They get the account's token if it has one;
  without one they are signed out for the job, and the log says so.
- **The log.** One line, `Git account: <name> (<host>)`, and the job page shows the account name.

The variables set for `gh` and `glab` are the ones their documentation names (last checked 2026-11-08):

| Kind | Variables | Source |
|---|---|---|
| `github`, host `github.com` | `GH_TOKEN`, `GH_CONFIG_DIR` | [gh environment variables](https://cli.github.com/manual/gh_help_environment) |
| `github`, another host | `GH_ENTERPRISE_TOKEN`, `GH_HOST`, `GH_CONFIG_DIR` (a `ghe.com` host uses `GH_TOKEN`) | the same page |
| `gitlab` | `GITLAB_TOKEN`, `GITLAB_HOST`, `GLAB_CONFIG_DIR` | [glab configuration](https://docs.gitlab.com/cli/configuration/) |

### Clone and pull

A clone or pull with an account refuses an address on another host than the account's, an address that
carries a user name or password, and an `ssh` address when the account has no key. The runner checks the
address git would really use, after your `url.<base>.insteadOf` settings. Pick **None** to clone from
another host with your own git setup.

### What this does not cover

Read these before you rely on an account to keep two identities apart:

- **A token is a secret the job can use.** The agent runs as your user, with the token in its environment. Use
  a token with only the access the work needs, and one you can revoke.
- **Masking.** The runner hides the token, and the encoded form git sends, in the job's log, answer and diff.
  A command an "ask me before commands" job shows you on the dashboard goes to the hub as the agent wrote
  it. If it holds the token in plain text, the hub masks it only when the token has a known prefix such as
  `ghp_`, `github_pat_` or `glpat-` ([SECURITY.md](../SECURITY.md)).
- **SSH aliases.** A host alias in your own `~/.ssh/config` that points to the account's host (a
  `Host work` with `HostName github.com`) still gets your setup when a remote uses the alias.
- **Another ssh program.** Jobs with an account ignore `core.sshCommand` and `GIT_SSH` and use the `ssh` on
  the `PATH`. The log warns when either is set. A key that only an ssh agent holds on your behalf can't be
  used, because the job has no terminal.
- **Extra HTTP headers.** For the account's host, the job clears the extra headers set for the whole host
  (the form `actions/checkout` writes), not only `Authorization`. A header set for one longer address, such
  as a single repository, still applies. If a self-hosted git server sits behind a proxy that needs a
  header, test a clone with the account first.
- **Settings given with `git -c`.** `GIT_CONFIG_PARAMETERS` in the environment is dropped for the job, and
  the log says so, because git reads it after the account's settings.
- **A `~/.netrc` file.** Git's HTTPS goes through curl, and curl signs in with a `~/.netrc` entry for the
  host, or a `default` entry, before git asks for the account's token. While such an entry is there, a job,
  clone or pull with the account is refused. On Windows, `%USERPROFILE%\_netrc` counts too. Remove the entry,
  or pick **None**.
- **A user name or password in an address.** Git uses those before the account's token. A task with an
  account is refused when a remote of its project, or a `url.<base>.insteadOf` or `pushInsteadOf` setting,
  gives an address on the account's host with a user name or password, over `https://` or `http://`. A
  setting that adds them to every `https://` address counts too. A clone or pull checks its own address
  ([Clone and pull](#clone-and-pull)).
- **Password prompts.** In a job with an account, git can't ask a terminal or another program
  (`core.askPass`, `SSH_ASKPASS`) for a password. An account without a token fails over HTTPS.
- **Subtasks and Remote Control.** Subtasks never get an account, a default included. They use the machine
  user's own git setup. Remote Control gets none either.
- **Tasks aimed at a tag.** Each machine names its own accounts, so only a task for a named machine can pick
  one.

## Tags and subtasks

**Tags** are a few words about a machine, for example `gpu` or `security-tools`. On New task and in a
council you can then pick "Any machine tagged …" instead of one machine, and the first free machine with
that tag runs the task ([guide 8](08-daily-use.md#new-task-one-prompt-one-agent)). The runner also reports
the machine's operating system as a tag: `linux`, `macos` or `windows`.

**Subtasks** are jobs that an agent on another machine hands to this one, in a task where you allowed it
([guide 2](02-architecture.md#agents-handing-work-to-other-machines-and-machine-tags)).

`runner.py init` asks for both. You can also set them in `~/.bullpen/runner.json`:

| Setting | Default | What it does |
|---|---|---|
| `tags` | none | words for this machine, as a list. Lowercase letters, digits, `.`, `_` and `-`, up to 32 characters each, at most 15 (the operating-system tag makes 16). `runner.py list` names any it left out |
| `accept_delegated_tasks` | `false` | `true` lets agents on your other machines hand subtasks to this machine, in tasks where you allow it |

For example:

```json
"tags": ["gpu", "security-tools"],
"accept_delegated_tasks": true
```

A running runner picks up a change to `runner.json` within 15 seconds, and `runner.py list` shows the
machine's tags and whether it takes subtasks.

**Accept new tags and labels on the machine's page.** A machine reports its own tags and account labels,
so the hub doesn't trust them on its own. They count for tasks aimed at a tag and for subtasks only after
you accept them:

1. Open the machine's page on the dashboard.
2. Under **Tags and account labels**, look at the list in **To accept**. Untick anything you don't
   recognise, and any label that isn't that very account.
3. Click **Accept**.

The Overview says when a machine has something new to accept. The operating-system tag counts without
accepting. To withdraw a tag or a label, click its **×** under **Accepted**. A tag the machine stops
reporting stops counting at once; if it reports it again later, it counts again without a new Accept.

For a new machine you can skip the Accept: give its tags and labels in **Add a machine** when you make its
setup command ([guide 8](08-daily-use.md#overview)).

**The machine needs its own runner token.** Tasks for a tag, tasks that hand out subtasks, and subtasks
themselves only go to machines that check in with their own token, never with the shared
`HUB_RUNNER_TOKEN`. A machine set up by hand with the shared one needs its own first
([Machines set up with the shared token](#machines-set-up-with-the-shared-token)).

## Remote Control

**Start Remote Control** on a machine's page runs Anthropic's own `claude remote-control` server in one of
its project folders, so you can drive Claude Code there from claude.ai/code or the Claude app
([guide 8](08-daily-use.md#live-interactive-sessions-remote-control)). It is off unless the machine allows
it. Three settings in `~/.bullpen/runner.json` control it:

| Setting | Default | What it does |
|---|---|---|
| `allow_remote_control` | `false` | `true` lets the dashboard start Remote Control on this machine. `runner.py init` asks; pressing Enter keeps No |
| `remote_control_max_hours` | `8` | hours after which each server stops by itself (1 to 72) |
| `max_remote_control` | `4` | how many servers may run at once on this machine (1 to 16) |

For example, to allow it with servers that stop after 4 hours:

```json
"allow_remote_control": true,
"remote_control_max_hours": 4
```

- While a server runs, anyone signed in to that Claude account (on claude.ai or in the Claude app) can drive
  Claude in that project folder and approve its commands. None of that shows in the hub.
- It needs a full claude.ai login. It doesn't work for an agent with an API key, one signed in with a
  long-lived `claude setup-token` token, or one that uses Bedrock, Vertex or another API address; the
  machine's page says why.
- Setting `allow_remote_control` back to `false` stops the running servers within about 15 seconds. **Stop**
  on the machine's page always works, whatever the setting says.
- The servers stop with the runner. After **Update now**, the new runner starts them again, and their 8
  hours still count from the first start. After a restart of the computer, or of the runner, they stay off
  unless you ticked **Start again when the runner starts** when you started them. If you did, the runner
  starts them again once it has checked in with the hub. Their hours still count from the first start, so
  a server whose time is up stays off. The runner doesn't start them again if `allow_remote_control` is now
  `false`, or if the machine is paused on the dashboard; the runner's log says why.
- The first time on each machine, Claude Code asks two questions that need a terminal. **Start** then says
  so and shows the commands to run once ([guide 9](09-troubleshooting.md#claude-code)).

## Signing agents out from the dashboard

**Sign out…** on a machine's page signs one of its Claude agents out, with that agent's own
`claude auth logout` ([guide 8](08-daily-use.md#a-machines-own-page)). It is off unless the machine
allows it, in `~/.bullpen/runner.json`:

| Setting | Default | What it does |
|---|---|---|
| `allow_remote_sign_out` | `false` | `true` lets the dashboard sign this machine's Claude agents out |

To allow it, add this line to `runner.json`:

```json
"allow_remote_sign_out": true
```

- The runner picks the change up within 15 seconds.
- The hub needs a dashboard password (`HUB_UI_PASSWORD`) for it, like approvals.
- Signing in again always happens on the machine itself: run the command the machine's page shows, such as
  `claude auth login`, at the machine or over SSH. On a Mac, sign in at the Mac itself: Claude Code keeps
  the login in the Keychain, which is locked in an SSH session. Claude Code signs in through Anthropic's
  own page, and nothing about it goes through the hub.

## Keeping a Mac available

On a Mac the runner is a launchd agent, and it runs only while you're logged in. When you log out, macOS
stops it. When you log in again, it starts. This is on purpose. Claude Code keeps its claude.ai login in your
login Keychain, which belongs to your login session. A LaunchDaemon would run with nobody logged in, but
outside any user's session, where that login isn't available, so subscription agents wouldn't work in it
([Apple's TN2083 on daemons and agents](https://developer.apple.com/library/archive/technotes/tn2083/_index.html),
last checked 17 November 2026). The setup doesn't offer one.

What keeps it running, and what doesn't, as seen on an Apple silicon Mac with macOS 27 on 20 November
2026:

| Situation | The runner |
|---|---|
| Logged in, screen in use | runs |
| Screen locked, or the screen saver on | keeps running. Claude jobs still work: Claude Code reads its login from the Keychain while the screen is locked. |
| Another user logged in with fast user switching, your session still open | keeps running, and Claude jobs still work |
| You logged out | stops, and starts again when you log in |
| The Mac restarted | starts when someone logs in, or by itself with automatic login (see **After a restart** below) |
| The Mac is asleep, or a MacBook's lid is closed | paused; the dashboard shows the Mac offline until it wakes, and it comes back by itself (see **Sleep** below) |

- **Background items.** In **System Settings → General → Login Items & Extensions**, under **App
  Background Activity**, the runner may be listed, probably as `python3`. On older macOS versions this is
  **System Settings → General → Login Items**, under **Allow in the Background**. If you switch it off
  there, the runner stops starting. The name it shows needs checking on a real Mac
  ([Apple's Login Items settings](https://support.apple.com/guide/mac-help/change-login-items-extensions-settings-mtusr003/mac),
  last checked 17 November 2026).
- **Python from Homebrew.** The launchd agent starts the exact Python the setup found. If you upgrade or
  remove that Python with Homebrew and the runner stops starting, run the setup again.
- **Apple's own Python.** This Python lives inside Xcode or the Command Line Tools, and that path breaks
  when Xcode is moved or switched. So for this Python the setup writes `/usr/bin/python3` into the
  launchd agent. It runs the Python of whichever Xcode or Command Line Tools `xcode-select` points to
  ([Apple's steps](https://developer.apple.com/documentation/xcode/configuring-command-line-tools-settings),
  last checked 17 November 2026).
- **Apple silicon and Rosetta.** Run the setup in a Terminal that doesn't use Rosetta, so Python and
  Claude Code are installed as Apple silicon programs. The setup warns you when it is running under Rosetta.
- **Local network permission (macOS 15 and later).** The first time the runner tries to reach a hub on your
  network, macOS asks whether Python may find devices on your local network. Click **Allow**. Until you
  do, the Mac shows offline on the dashboard, and `runner.log` says `cannot reach the hub` with `No route
  to host` every minute. If you set the Mac up over SSH, the question appears on the Mac's screen. You can
  also allow it later: in **System Settings → Privacy & Security → Local Network**, turn on **Python**.

- **After a restart**, the runner starts only when someone logs in. With FileVault on, unlocking the disk
  doesn't count as logging in. On the Mac checked for the table above, FileVault could be unlocked over SSH
  at startup. The Mac then waited at the login window, and the runner stayed offline until someone logged
  in, at the keyboard or with Screen Sharing. Programs installed as system LaunchDaemons start without a
  login, but the runner is a launchd agent, so it waits.

  To have the Mac log in by itself, turn FileVault off, then turn on **System Settings → Users & Groups →
  Automatically log in as**. macOS doesn't offer automatic login while FileVault is on
  ([Apple's steps](https://support.apple.com/en-us/102316), last checked 20 November 2026). With it on, the
  runner was running about 20 seconds after a restart, and a Claude job worked with nobody at the Mac. Know
  what you give up:
  - With FileVault off, anyone who takes the Mac, or its disk, can read what is on it.
  - Automatic login logs in as you, which also unlocks your login Keychain and the Claude login in it.
    Anyone at the Mac gets your desktop. Apple's own words: "Automatic login allows anyone to access your Mac
    simply by restarting it." Apple also advises against it for an administrator account
    ([Users & Groups settings](https://support.apple.com/guide/mac-help/change-users-groups-settings-mtusr001/mac),
    last checked 20 November 2026).

  Use automatic login only for a Mac in a place you trust, and only for a standard account, not an
  administrator.
- **Sleep.** A sleeping Mac can't take jobs, and after about 45 seconds the dashboard shows it offline. A
  job that was running pauses with the Mac and carries on when it wakes. The runner's time limit for the job
  doesn't count the time asleep, but the hub's does: once the time limit plus 5 minutes has passed by the
  clock, the job can no longer use the hub's tools, such as approvals and subtasks. In the check for the
  table above, the Mac slept for three and a half minutes in the middle of a job. About 25 seconds after it
  woke, the same runner was back online, and the job finished normally. Right after waking, `runner.log` may
  show one `Network is unreachable` warning; that is harmless. To keep the Mac awake
  ([Apple's sleep settings](https://support.apple.com/guide/mac-help/set-sleep-and-wake-settings-mchle41a6ccd/mac),
  last checked 17 November 2026):
  - On a desktop Mac, turn on **System Settings → Energy → Prevent automatic sleeping when the display is
    off**.
  - On a MacBook, keep it on power and turn on **System Settings → Battery → Options → Prevent automatic
    sleeping on power adapter when the display is off**. Closing the lid still puts it to sleep, unless it
    has an external display, keyboard and power connected.
  - To keep it awake only for a while, without changing any setting, leave this running in a Terminal
    window. Ctrl+C ends it ([caffeinate](https://ss64.com/mac/caffeinate.html), last checked 17 November
    2026):

    ```bash
    caffeinate -i
    ```

- **The Claude login is in the Keychain.** The runner never reads the Keychain. It asks Claude Code itself
  (`claude auth status`), so the machine's page shows whether each agent is signed in. With Claude Code
  older than 2.1.317 the page says "Unknown (CLI too old)"; update Claude Code, or run
  `python3 ~/.bullpen/runner/runner.py test claude` to check the login for real.
- **Check the login at the Mac, not over SSH.** Run `claude auth status` and `runner.py test` in Terminal
  on the Mac itself. Over SSH the login Keychain is locked, so Claude Code says it is signed out
  (`loggedIn` false) even when it isn't. The machine's page comes from the runner, which runs in your
  login session, so it shows the real state.

## Managing the runner

**Linux:**

| Task | Command |
|---|---|
| Status | `systemctl --user status bullpen-runner` |
| Live log | `journalctl --user -u bullpen-runner -f`, or `tail -f ~/.bullpen/runner.log` |
| Restart | `systemctl --user restart bullpen-runner` |
| Stop it from starting at boot | `systemctl --user disable --now bullpen-runner` |

**macOS:**

| Task | Command |
|---|---|
| Status | `launchctl print gui/$(id -u)/bullpen.runner` |
| Live log | `tail -f ~/.bullpen/runner.log` |
| Errors from before the log opens (a wrong Python path, a crash at start) | `cat ~/.bullpen/launchd.log` |
| Restart | `launchctl kickstart -k gui/$(id -u)/bullpen.runner` |
| Stop it (until you log in again) | `launchctl bootout gui/$(id -u)/bullpen.runner` |
| Start it again after stopping it | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/bullpen.runner.plist` |
| Stop it for good | the line above, then `rm ~/Library/LaunchAgents/bullpen.runner.plist` (or remove the runner altogether: [Removing a machine](#removing-a-machine)) |

**Both:**

| Task | Command |
|---|---|
| Re-run the setup questions | `python3 ~/.bullpen/runner/runner.py init` |
| Test an agent without the hub (on a Mac, in Terminal on the Mac itself) | `python3 ~/.bullpen/runner/runner.py test claude-work` |
| Show the last plan-limit readings | `python3 ~/.bullpen/runner/runner.py limits` |
| Connect with a new code | `python3 ~/.bullpen/runner/runner.py enroll --hub http://192.168.100.20:8080 --code K7QM-2XTA-9RFD-WP3H` |

Stopping or restarting the runner also stops the [Remote Control](#remote-control) servers it started. The
ones started with **Start again when the runner starts** come back when the runner starts again.

## Machines set up with the shared token

A machine you set up by hand can connect with the hub's shared runner token, `HUB_RUNNER_TOKEN` in the hub's
settings. It works for jobs aimed at it by name. Tasks for a tag, tasks that hand out subtasks, and subtasks
go only to machines with their own token. To give one its own token, click **Add a machine** with that
machine's name, or **Make a setup command** on its page, and run the command on that machine. The hub
recognizes the machine by its install id (an older runner by its host name) and switches it over. After
that, the shared token no longer works for that machine. Once every machine has its own token, you can empty
`HUB_RUNNER_TOKEN` in the hub's settings, so the shared token stops working altogether.

## Two computers with the same name

Each machine needs its own name. If two computers check in under one name, they take each other's jobs. The
hub notices and flags that machine, naming the other computer. To fix it, give one of them a new name with
**Add a machine** (type the new name) and run the command on that computer; then **Forget** the old entry if
it is left over. A machine with its own token can't take a name that belongs to another computer: enrolling
refuses it unless the code was made for exactly that machine (**Make a setup command** on its page).

The hub tells computers apart by an install id that the runner makes once and keeps in
`~/.bullpen/install-id`. Host names don't work for this, because some computers, Macs among them, change
theirs by themselves. If you copy `~/.bullpen` from one computer to another, delete `install-id` on the copy
before its runner starts.

## Removing a machine

The runner can remove itself and what the setup and the hub added to the machine.

1. See what it would remove. This changes nothing:

   ```bash
   python3 ~/.bullpen/runner/runner.py uninstall --dry-run
   ```

2. Remove it. It shows the same list and asks before it changes anything:

   ```bash
   python3 ~/.bullpen/runner/runner.py uninstall
   ```

3. On the dashboard, wait until the machine shows as offline, then click **Forget this machine** on its card.
   This also cancels its runner token, so it can't connect again with it. To add it back later, use **Add a
   machine** again.

**What goes:**

- the service that starts the runner (on Linux, the systemd user service `bullpen-runner`; on a Mac, the
  launchd agent `bullpen.runner`). It is stopped first.
- `~/.bullpen`: the runner, its settings and token, its logs and the chat workspace.
- the `bullpen` command in `~/.local/bin`, and the commands the setup made for extra Claude accounts, such as
  `claude-work`.
- what the hub added to your agents' own settings: the Agent Bullpen block in each `CLAUDE.md` and in
  `GEMINI.md`, the `/remember` and "second opinion" add-ons, and the hub's MCP servers.

Only files and settings marked as Agent Bullpen's are removed. Files you made yourself stay, even with the same
name, and so do the MCP servers you added. A service that starts a runner from a different folder stays too.

**What stays:**

- your agents' logins, sessions and settings: `~/.claude`, each `~/.claude-<name>` (such as `~/.claude-work`)
  and `~/.gemini`. The Gemini API key in `~/.gemini/.env` stays too.
- your projects, and Claude Code, Gemini CLI, Python and Git.
- the PATH line the setup added to `~/.bashrc` and `~/.zshrc`, and "linger" on Linux. Other programs may use
  them. To turn linger off: `loginctl disable-linger`.

Add `--yes` to skip the question, for example in a script. If something can't be removed, the command says
what and why. Run it again once that's fixed: it only removes what is left.

**By hand**, if the runner's files are already gone:

1. Stop the runner and stop it from starting by itself (see [Managing the runner](#managing-the-runner)).
2. Delete `~/.bullpen`, and `~/.local/bin/bullpen`.
3. In `~/.claude/CLAUDE.md`, in each `~/.claude-<name>/CLAUDE.md` and in `~/.gemini/GEMINI.md`, delete the
   lines from `<!-- bullpen:begin` to `<!-- bullpen:end -->`.
4. Remove the hub's MCP server from each Claude account. For the first account:

   ```bash
   claude mcp remove -s user bullpen
   ```

   For an extra account, use its own command, for example `claude-work mcp remove -s user bullpen`.

If the machine is lost or stolen:

1. **Forget** it (step 2). With its own token, that shuts its runner out of the hub and stops its own MCP
   token. A machine on the shared token can only be shut out by changing `HUB_RUNNER_TOKEN` in the hub's
   settings, which every other machine on the shared token then needs too.
2. On the **MCP** page, press **Rotate** next to the hub's shared token. The machine's agents may have a copy
   of it: they used it if the machine was on the shared runner token, or before its runner got an MCP token
   of its own. It gives access to the shared memory and to every MCP server routed through the hub. Your other
   machines pick up the new token on their next sync. If you turned the shared token off, skip this step.
3. Sign its agents' accounts out on the providers' websites: the machine still has their logins.

## Updating

Claude Code updates itself. For Gemini CLI, run:

```bash
npm install -g @google/gemini-cli@latest
```

For the runner, use **Update now** on the machine's page in the hub (or **Update all runners** on the
Overview, for every machine or only those with a tag) after you've upgraded the hub. The hub hands out its own copy of the runner, so hub and runners stay
on the same version.
