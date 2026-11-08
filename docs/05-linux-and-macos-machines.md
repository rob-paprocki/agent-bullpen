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
CLI and Antigravity on this machine. After that the runner keeps them up to date. If you answered "no" to
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
  hours still count from the first start. After a restart of the computer they stay off until you press
  **Start** again.
- The first time on each machine, Claude Code asks two questions that need a terminal. **Start** then says
  so and shows the commands to run once ([guide 9](09-troubleshooting.md#claude-code)).

## Keeping a Mac available

A launchd agent runs while you're logged in to the Mac, including when the screen is locked. That's
deliberate: Claude Code keeps its login in your Keychain, which needs your session.

- **Local network permission (macOS 15 and later).** The first time the runner starts, macOS asks whether
  Python may find devices on your local network. Click **Allow**, or the runner can't reach a hub on your
  network. If you set the Mac up over SSH, the question appears on the Mac's screen; you can also allow it
  later in **System Settings → Privacy & Security → Local Network**.

- **After a restart**, someone has to log in again. To make that automatic, turn on **System Settings →
  Users & Groups → Automatically log in as**. macOS doesn't offer this while FileVault is on, and anyone
  with the Mac in front of them then gets your desktop, so decide based on where the Mac lives.
- **Sleep.** A sleeping Mac can't take jobs. On a desktop Mac, turn on **System Settings → Energy →
  Prevent automatic sleeping when the display is off**. On a laptop, keep it on power and use the
  **Battery → Options** settings, or run `sudo pmset -c sleep 0`.
- **The dashboard can't see a Mac's Claude login**, because it is in the Keychain. Hovering over the agent's
  chip says so, and `python3 ~/.bullpen/runner/runner.py test claude` checks the login for real.

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
| Restart | `launchctl kickstart -k gui/$(id -u)/bullpen.runner` |
| Stop it (until you log in again) | `launchctl bootout gui/$(id -u)/bullpen.runner` |
| Stop it for good | the line above, then `rm ~/Library/LaunchAgents/bullpen.runner.plist` |

**Both:**

| Task | Command |
|---|---|
| Re-run the setup questions | `python3 ~/.bullpen/runner/runner.py init` |
| Test an agent without the hub | `python3 ~/.bullpen/runner/runner.py test claude-work` |
| Show the last plan-limit readings | `python3 ~/.bullpen/runner/runner.py limits` |
| Connect with a new code | `python3 ~/.bullpen/runner/runner.py enroll --hub http://192.168.100.20:8080 --code K7QM-2XTA-9RFD-WP3H` |

Stopping or restarting the runner also stops the [Remote Control](#remote-control) servers it started.

## Machines set up with the shared token

A machine you set up by hand can connect with the hub's shared runner token, `HUB_RUNNER_TOKEN` in the hub's
settings. It works for jobs aimed at it by name. Tasks for a tag, tasks that hand out subtasks, and subtasks
go only to machines with their own token. To give one its own token, click **Add a machine** with that
machine's name, or **Make a setup command** on its page, and run the command on that machine. The hub
recognizes it by its host name and switches it over. After that, the shared token no longer works for that
machine. Once every machine has its own token, you can empty `HUB_RUNNER_TOKEN` in the hub's settings, so the
shared token stops working altogether.

## Two computers with the same name

Each machine needs its own name. If two computers check in under one name, they take each other's jobs. The
hub notices and flags that machine, naming the other computer. To fix it, give one of them a new name with
**Add a machine** (type the new name) and run the command on that computer; then **Forget** the old entry if
it is left over. A machine with its own token can't take a name that belongs to another computer: enrolling
refuses it unless the code was made for exactly that machine (**Make a setup command** on its page).

## Removing a machine

1. On the machine, stop the runner and stop it from starting by itself (see
   [Managing the runner](#managing-the-runner)).
2. On the dashboard, wait until the machine shows as offline, then click **Forget this machine** on its card.
   This also cancels its runner token, so it can't connect again with it. To add it back later, use **Add a
   machine** again.
3. If you like, delete `~/.bullpen` on the machine. Your agents' own logins and settings (`~/.claude`,
   `~/.claude-<name>`, `~/.gemini`) are left alone.

If the machine is lost or stolen:

1. **Forget** it (step 2). With its own token, that shuts its runner out of the hub. A machine on the shared
   token can only be shut out by changing `HUB_RUNNER_TOKEN` in the hub's settings, which every other machine
   on the shared token then needs too.
2. On the **MCP** page, press **Rotate** next to the hub's token. The machine's agents have a copy of it in
   their settings, and it gives access to the shared memory and to every MCP server routed through the hub.
   Your other machines pick up the new token on their next sync.
3. Sign its agents' accounts out on the providers' websites: the machine still has their logins.

## Updating

Claude Code updates itself. For Gemini CLI, run:

```bash
npm install -g @google/gemini-cli@latest
```

For the runner, use **Update now** on the machine's page in the hub (or **Update all runners** on the
Overview) after you've upgraded the hub. The hub hands out its own copy of the runner, so hub and runners stay
on the same version.
