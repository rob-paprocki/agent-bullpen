# 6 · Windows machines

**Goal:**

- The PC runs Claude Code, signed in to one or more Claude accounts, and optionally Gemini CLI.
- A **runner** connects it to the hub with its own token, and starts when you sign in to Windows.
- The PC shows up as "online" on the dashboard, so the hub can send it jobs in the folders where your
  Windows work lives.

This works on Windows 10 and 11. Repeat it on each PC you want to use; there is no limit on how many.

You need:

- the hub, installed and running ([guide 4](04-proxmox-hub.md)), with its dashboard open in your browser;
- the logins for the Claude account or accounts you want to use on this PC;
- optionally, a **Gemini API key** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
  ([guide 1](01-accounts-and-rules.md) explains why it's a key);
- **winget**, which comes with Windows 11 and current Windows 10. If `winget` isn't found, install
  **App Installer** from the Microsoft Store.

Use a normal PowerShell window, **not** "Run as administrator". The runner, the Claude logins and the
scheduled task belong to the user who runs the setup. Windows asks for admin approval by itself when an
installer needs it.

## The quick way: Add a machine

1. On the dashboard, open **Overview** and click **Add a machine**.
2. Type a name for the PC, for example `build-box`, or leave it blank to use its computer name.
3. The hub shows a one-time code and two commands. The code works once, and only for one hour.
4. On the PC, open **PowerShell**: Start, type "PowerShell", and open it normally.
5. Paste the **Windows** command. It looks like this, with your hub's address and your code:

   ```powershell
   irm 'http://192.168.100.20:8080/enroll/K7QM-2XTA-9RFD-WP3H/install.ps1' | iex
   ```

6. Answer the setup's questions, and approve the admin prompts from the installers.
   [What the setup does](#what-the-setup-does) explains each step.
7. When it finishes, the PC shows as online on the dashboard's **Overview**, with its agents as chips.

**What the command does.** `irm` downloads a short script from your hub and `iex` runs it. That script
downloads the runner and the setup script, also from your hub, checks each file's checksum, and runs the
setup. The setup trades the code for the PC's own runner token. Nothing comes from anywhere else, except the
tools it installs (with winget, Anthropic's installer and npm).

**Scripts are blocked on this PC?** That's Windows' default. The command still works, and you don't need to
change the setting. `iex` runs the downloaded text directly. The setup script runs in a PowerShell of its own,
with scripts allowed for that one process only.

**To read it first**, run only the first half. It prints the script, and reading it doesn't use up the code:

```powershell
irm 'http://192.168.100.20:8080/enroll/K7QM-2XTA-9RFD-WP3H/install.ps1'
```

**To choose the name on the PC instead**, run this first in the same window:

```powershell
$env:BULLPEN_NAME = 'build-box'
```

**If the code expired or was already used**, the command says so and changes nothing. Click **Add a machine**
again for a new code.

**The address in the command** is the hub's LAN address (`HUB_RUNNER_URL`, [guide 4](04-proxmox-hub.md)).
It is the direct address on your network that the PC will use every few seconds.

## The manual way

Use this when you'd rather work from a copy of the repository.

1. Install Git, then close and reopen PowerShell so `git` is on your PATH:

   ```powershell
   winget install --id Git.Git -e --accept-source-agreements --accept-package-agreements
   ```

2. Get the repository with the commands below. Or download the ZIP from GitHub (**Code → Download ZIP**)
   and extract it to `C:\Users\<you>\agent-bullpen`.

   ```powershell
   git clone https://github.com/rob-paprocki/agent-bullpen.git $HOME\agent-bullpen
   ```

   ```powershell
   cd $HOME\agent-bullpen
   ```

3. Allow scripts in **this** PowerShell window only (until you close it), and run the setup:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   ```

   ```powershell
   .\scripts\windows\Setup-Workstation.ps1
   ```

4. When it asks how to connect to the hub, give the hub's LAN address. Then give either a **one-time code**
   from **Add a machine** or the hub's **shared runner token** (`HUB_RUNNER_TOKEN` in `/etc/bullpen/hub.env`
   on the hub). The code is better, because the PC gets its own token.

You can also give these as options, so the script doesn't ask: `-Hub`, `-Code`, `-Token` and `-Name`. For
example:

```powershell
.\scripts\windows\Setup-Workstation.ps1 -Hub http://192.168.100.20:8080 -Code K7QM-2XTA-9RFD-WP3H
```

## What the setup does

It goes through eight steps and asks before each one. Running it again is safe: finished steps are skipped.

**1. Git, Python and Node.js** are installed with `winget` when they're missing. Python (3.9 or newer) is
installed just for your user. Node.js is only needed for Gemini CLI, which needs version 20 or newer; winget's
"Node.js LTS" is new enough.

**2. The runner and the hub.** It copies the runner to `%USERPROFILE%\.bullpen\runner\` and connects the PC to
the hub. With a code, the hub gives the PC its own runner token, saved in
`%USERPROFILE%\.bullpen\runner.json`. The hub keeps only a fingerprint of it.

**3. Claude Code** is installed with Anthropic's official installer, into `%USERPROFILE%\.local\bin`. Git for
Windows matters here: Claude Code uses Git's Bash to run commands when Git is installed, and falls back to
PowerShell otherwise.

**4. Gemini CLI** (optional) is installed with npm.

**5. The agents this PC offers.** The runner asks the same questions as on Linux
([guide 5](05-linux-and-macos-machines.md#what-the-setup-does)):

| Question | Answer |
|---|---|
| Hub address, name, code | already filled in when you used **Add a machine** |
| How many jobs at once | `1` is safe. Raise it on a PC with plenty of memory: each running job is a separate agent program. |
| Add a Claude agent? | yes, once for each Claude account you want to use here. The first is `claude`, with its login in `%USERPROFILE%\.claude`. Name the others, for example `work`, which becomes `claude-work` with its login in `%USERPROFILE%\.claude-work`. |
| Add a Gemini agent? | yes if you have a key |
| Project folders | the folders where your Windows work happens (you can add more later) |

**6. Signing in.** For each Claude agent that isn't signed in yet, it runs `claude auth login` with that
agent's settings folder, and you finish the login in your browser. As on Linux, make sure the browser is
signed in to the **right** Claude account each time. Use one browser profile per account, and copy the login
link into it. Each extra account also gets a command of its own, for example `claude-work`, which starts
Claude Code with that account's login.

**7. Starting the runner.** It offers to test each agent, then registers a Windows **scheduled task** called
"Agent Bullpen Runner". The task starts the runner (hidden, with `pythonw.exe`) when you sign in, restarts it
if it crashes, and has no time limit.

**8. Extras.**

- **Don't sleep on AC power**, so the PC stays reachable.
- **Shared context sync.** This runs by itself and asks nothing. It adds your pinned notes, the `/remember`
  and "second opinion" add-ons, and the servers from the hub's MCP page to every Claude account, Gemini CLI
  and Antigravity on this PC ([guide 10](10-shared-memory-and-mcp.md)). If you answered "no" to letting the
  hub manage agent settings in step 5, it changes nothing.

## Keeping a Windows machine available

The runner runs while you're signed in to Windows. It keeps running when the screen is locked
(Win + L). That's deliberate: your Windows programs and Claude Code's login live in your user profile, and
some programs need a desktop session.

After a Windows Update restart, someone has to sign in again. To make that automatic:

- **Built-in:** run `netplwiz` and untick "Users must enter a user name and password to use this
  computer". If that box is missing, first turn off **Settings → Accounts → Sign-in options → For improved
  security, only allow Windows Hello sign-in**.
- **Sysinternals Autologon** ([learn.microsoft.com](https://learn.microsoft.com/sysinternals/downloads/autologon))
  is free but not open source. It stores the password encrypted in the registry.

Either way, automatic sign-in means anyone with the PC in front of them gets your desktop. Decide based on
where the PC lives. You can also set **Active hours** under Settings → Windows Update, so restarts happen
when you're around.

## What agents can and can't do on Windows

Claude Code and Gemini CLI work through the command line: they read and edit files and run commands,
builds, tests and scripts. They work best with Windows software that has a command-line, scripting or
file-based interface, such as compilers, build tools, CLI exporters, or PowerShell modules for the program.
Clicking through a GUI-only program is outside what this setup does.

The hub's other ways of working with an agent work on Windows too:

- **Live sessions** (type to a Claude agent while it works) and **approvals** (Claude asks on the job's page
  before running a command) behave as on Linux and macOS ([guide 8](08-daily-use.md#a-jobs-page)).
- **Remote Control** from the dashboard runs without a window, and its servers end with the runner, no matter
  how the runner stops. **Stop** ends a server at once, because Windows has no way to ask it to stop cleanly.
  [Guide 9](09-troubleshooting.md#checks-with-the-real-claude-code) says what that leaves, and what has been
  checked on Windows so far. Remote Control is off unless the PC allows it. The settings are in
  `%USERPROFILE%\.bullpen\runner.json`, and [guide 5](05-linux-and-macos-machines.md#remote-control) explains
  them.
- **Tags and subtasks** work as on Linux and macOS: the PC reports `windows` as a tag by itself, and
  [guide 5](05-linux-and-macos-machines.md#tags-and-subtasks) explains `tags` and `accept_delegated_tasks`.

## Adding agents later

Run the runner's setup questions again, then the setup script, which signs in to new Claude accounts.
Existing agents are kept unless you say no to them.

```powershell
py -3 $HOME\.bullpen\runner\runner.py init
```

```powershell
powershell -ExecutionPolicy Bypass -File $HOME\.bullpen\setup\Setup-Workstation.ps1
```

(Use `.\scripts\windows\Setup-Workstation.ps1` in your copy of the repository if you set the PC up the
manual way.)

**A credits agent.** If the PC has a Claude agent with an API key from the Console organization that holds
your plan's monthly API credits, `runner.py init` asks whether it is the PC's credits agent (`"credits_agent"`
in `runner.json`). It works as on Linux and macOS:
[guide 5, A credits agent](05-linux-and-macos-machines.md#a-credits-agent).

## Managing the runner

| Task | How |
|---|---|
| Is it running? | Task Scheduler → Task Scheduler Library → **Agent Bullpen Runner** (or check the hub's Overview) |
| Log | `notepad $HOME\.bullpen\runner.log` |
| Restart | `Stop-ScheduledTask "Agent Bullpen Runner"; Start-ScheduledTask "Agent Bullpen Runner"` |
| Stop it for good | `Stop-ScheduledTask "Agent Bullpen Runner"; Unregister-ScheduledTask "Agent Bullpen Runner"` |
| Add a project | `py -3 $HOME\.bullpen\runner\runner.py add-project NAME C:\path\to\folder` |
| Test an agent | `py -3 $HOME\.bullpen\runner\runner.py test claude-work` |
| Show the last plan-limit readings | `py -3 $HOME\.bullpen\runner\runner.py limits` |
| Re-run the setup questions | `py -3 $HOME\.bullpen\runner\runner.py init` |
| Connect with a new code | `py -3 $HOME\.bullpen\runner\runner.py enroll --hub http://192.168.100.20:8080 --code K7QM-2XTA-9RFD-WP3H` |

Stopping the runner also stops the Remote Control servers it started.

A PC set up by hand with the hub's shared runner token works too.
[Guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token) explains how to give one its
own token. Guide 5 also covers
[two computers with the same name](05-linux-and-macos-machines.md#two-computers-with-the-same-name) and
[removing a machine](05-linux-and-macos-machines.md#removing-a-machine). On Windows, "stop the runner" means
the **Stop it for good** line above; afterwards you can delete `%USERPROFILE%\.bullpen`.

## Updating

Claude Code updates itself. For Gemini CLI, run `npm install -g @google/gemini-cli@latest`.

For the runner, use **Update now** on the machine's page in the hub (or **Update all runners** on the
Overview) after you've upgraded the hub. The hub hands out its own copy of the runner, so hub and runners stay
on the same version.
