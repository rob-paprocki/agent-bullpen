# 9 · Troubleshooting

Start with the logs:

| Where | Log |
|---|---|
| Hub | on the hub (in a Proxmox container, after `pct enter <id>`): `journalctl -u bullpen -n 100` |
| Runner on Linux | `journalctl --user -u bullpen-runner -n 100`, or `~/.bullpen/runner.log` |
| Runner on macOS | `~/.bullpen/runner.log` |
| Runner on Windows | `%USERPROFILE%\.bullpen\runner.log` |
| A single job | the **Live log** section on its page in the dashboard |

If none of this helps, [open an issue](https://github.com/rob-paprocki/agent-bullpen/issues) with the log
lines. Remove passwords, tokens, API keys and enrollment codes from them first.

## The dashboard

| Symptom | Fix |
|---|---|
| Can't open `http://<hub-ip>:8080` | On the hub, check `systemctl status bullpen`. On Proxmox, check that the container has the IP you think it has: `pct exec <id> -- hostname -I`. |
| `hub.example.com` shows a Cloudflare error 1033 or 502 | The tunnel is down or the route is wrong. On the hub, run `systemctl status cloudflared`. In Cloudflare, check that the route's Service URL is `http://localhost:8080`. |
| Logged in, but get sent back to the login | You set `HUB_COOKIE_SECURE='1'` but opened the hub over plain http. Use the https address or set it back to `0`. |
| Forgot the dashboard password | On the hub, run `grep PASSWORD /etc/bullpen/hub.env` (or change it there), then `systemctl restart bullpen`. |
| Notifications don't arrive | Look at the hub's log (`journalctl -u bullpen -n 200`). At start the hub logs "notifications go to <host>". If it says "notifications are off", the line says why (the address isn't `http://` or `https://`, or `HUB_NOTIFY_HEADERS` is set with a plain `http://` address). "refused: HTTP 403" usually means a protected ntfy topic without its token. After changing `hub.env`, restart the hub. Check that the phone is subscribed to exactly the same topic. |
| The hub's address changed | Reserve the address in your router so it doesn't happen again. Set `HUB_RUNNER_URL` in `/etc/bullpen/hub.env` to the new address and restart the hub, so **Add a machine** shows it. On each machine added earlier, change `"hub_url"` in `~/.bullpen/runner.json` (`%USERPROFILE%\.bullpen\runner.json` on Windows), then restart its runner. |

## Adding a machine

| Symptom | Fix |
|---|---|
| "This code has expired" or "This code was already used" | Codes work once, for one hour. Click **Add a machine** again for a new one. |
| `curl` or `irm` can't connect | The address in the command is `HUB_RUNNER_URL`. Check that this machine can open it in a browser. If it can't, the address is wrong (fix it in `/etc/bullpen/hub.env` and restart the hub), or a firewall is in the way. |
| The machine shows a "same name" warning | Two computers check in under one name and take each other's jobs. Rename one ([guide 5](05-linux-and-macos-machines.md#two-computers-with-the-same-name)). |
| "running scripts is disabled on this system" (Windows) | Run `Set-ExecutionPolicy -Scope Process Bypass -Force` in the same window first. |

## Machines and runners

| Symptom | Fix |
|---|---|
| Machine never appears | Check the runner log. "cannot reach the hub" means the `hub_url` in `runner.json` is wrong, or a firewall is in the way: try opening the hub address in that machine's browser. |
| "the hub rejected this runner's token" | The machine was **forgotten** on the dashboard, or its token doesn't match. Click **Add a machine** with the same name and run the command on that machine again. |
| Machine shows offline after a while | **Linux:** is linger on? Check with `loginctl show-user $USER -p Linger`. **macOS:** the runner only runs while you're logged in, and the Mac may have gone to sleep ([guide 5](05-linux-and-macos-machines.md#keeping-a-mac-available)). **Windows:** the runner only runs while you're signed in, and the PC may have gone to sleep ([guide 6](06-windows-machines.md#keeping-a-windows-machine-available)). |
| Agent chip shows a warning, "no login found" | Run that account's command (`claude`, or `claude-<name>` for another account) in a terminal on that machine, type `/login`, then `/exit`. On a Mac the dashboard can't see the login (it's in the Keychain). To check that it works, run `python3 ~/.bullpen/runner/runner.py test <agent>`. |
| Agent chip shows a warning, "not found on this machine" | The `command` path in `runner.json` is wrong (often after reinstalling). Re-run `runner.py init` to detect it again. |
| Job stuck at "queued" | The target machine is offline, or busy with as many jobs as its "jobs at once" setting allows. |
| Job ends with "Lost: the runner stopped reporting this job" | The runner restarted mid-job (reboot, crash or update). Run it again. |
| "doesn't allow managing projects from the dashboard" | That machine's `runner.json` has `allow_remote_projects` (or `allow_remote_update`) set to `false`. Re-run `runner.py init` there and answer yes, or edit the file. |
| Cloning a private repository fails | The machine needs its own git credentials, and the clone can't ask for a password. On Windows, run `git clone <url>` once in a terminal. Git Credential Manager then remembers the login. On Linux and macOS, use an SSH key added to your git host (`git@github.com:…` addresses) or `gh auth login`. |
| A machine stays on the old version after **Update now** | It restarts only when no job is running, so wait for its current job. The job's log shows what was updated. |
| A job fails with "<agent> has no '<level>' level in its settings." | That agent's entry in `runner.json` has `autonomy` flags of its own, and the level the job asked for is missing or `null`. Add the level's flags ([guide 11](11-adding-agents.md#every-setting-an-agent-entry-can-have)), or send the job with a level the agent has. |
| No **Follow up** on a finished job | The agent never started a session (the job failed before the agent ran), so there is nothing to continue. Use **Run again…** instead. |
| A task for "any machine tagged …" stays queued | Its page says what it waits for and how many machines match. With 0 matches: accept the tag (and the agent's account label, if it has one) on the machine's page; check that the machine uses its own runner token, not the shared one ([guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token)); and check that it has the agent and the project. A task that asks you before commands, or keeps its session open, also needs an agent that can (update the runner there). When machines match, they are offline or busy: it starts when one has a free slot. It never runs under another account than the one it was made for, so a withdrawn label holds it too. |
| Accept says "<machine> reports something else now" | The machine checked in with other tags or labels after the page was drawn. Reload the page and check again. |
| **May hand subtasks to other machines** is missing or greyed out on New task | The box shows only for agents that can use the hub's job tools, on a hub with MCP support and a dashboard password (`HUB_UI_PASSWORD`). Update the runner if the agent should have them. For a Gemini agent, see [Gemini CLI](#gemini-cli) below. Greyed out with "still uses the shared runner token": give that machine its own token ([guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token)). |
| An agent says a subtask is outside its scope ("may hand subtasks only to …", "only in the project …", "may use only …") | The agent asked for a machine, tag, project, account or agent you didn't allow on New task. Use **Run again…** with a wider scope if the task needs it. |
| An agent says "<machine> doesn't take subtasks from other agents" | Set `accept_delegated_tasks` to `true` in that machine's `runner.json` ([guide 5](05-linux-and-macos-machines.md#tags-and-subtasks)). The runner picks it up within 15 seconds. |
| An agent says "<machine> still uses the shared runner token" | Subtasks go only to machines with their own token. Give that machine one ([guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token)). |
| An agent says "<agent> on <machine> can't take subtasks" | The hub can't tell what that agent's levels allow: its entry in `runner.json` has `autonomy` flags of its own, has a flag that changes permissions in `args` or `extra_args` (such as `--permission-mode` or `--yolo`, which would win over the level's), or it isn't a Claude, Gemini or self-test agent ([guide 11](11-adding-agents.md#every-setting-an-agent-entry-can-have)). Pick another agent, or move the flag into the agent's `autonomy` levels. |
| A subtask fails with "<agent> can't take subtasks here: its entry in runner.json has …" | That agent's entry changed after the hub last heard from the machine, as in the row above. The message names the flags. |
| An agent says "A subtask can't do more than this job" or "Subtasks on this hub may only read" | A subtask never gets a higher level than its task, nor more than `HUB_DELEGATE_MAX_AUTONOMY` (read only by default, [guide 4](04-proxmox-hub.md#running-it)). |
| An agent says the task "has already handed out" its subtasks, or that some "are still running" | A task can hand out 5 subtasks in all, 3 at a time (`HUB_DELEGATE_MAX_CHILDREN`, `HUB_DELEGATE_MAX_ACTIVE`). |
| An agent says "Usage limits aren't a reason to switch accounts" | An account hit a usage limit in this task, so new subtasks may use only that account. Wait for the reset ([guide 1](01-accounts-and-rules.md#more-than-one-claude-account)). |
| A subtask fails with "This machine doesn't take subtasks from other agents" | `accept_delegated_tasks` was turned off in that machine's `runner.json` after the subtask was handed out. Turn it back on, or let the task's agent pick another machine. |
| A subtask was cancelled with "Its parent's time limit ran out before it started" | A subtask's time ends with its task's. It was still waiting for a machine when the task's time ran out. Give the task a longer time limit, or check why no machine took the subtask. |
| A cancelled job keeps running on Windows | The runner stops the whole process tree with `taskkill /T`. If something survives, end `claude.exe` or `node.exe` in Task Manager, and open an issue saying what was left. |

## Claude Code

| Symptom | Fix |
|---|---|
| Job is billed to an API key instead of the subscription | An `ANTHROPIC_API_KEY` variable is set for that user, and it takes priority over the subscription login. Remove it from your shell profile (Linux, macOS) or your Environment Variables (Windows), then restart the runner. |
| Jobs fail with `authentication_failed` after a Claude Code update | Run the agent's command in a terminal and `/login` again. Anthropic also says `--bare` "will become the default for `-p` in a future release", and bare mode doesn't read subscription logins. If that lands, check the [headless docs](https://code.claude.com/docs/en/headless) for the opt-out flag and add it to the agent's `"extra_args"` in `runner.json`. |
| "cannot be launched inside another Claude Code session" | The runner was started from inside a Claude Code session. It already clears `CLAUDECODE` for its jobs. If you see the message in your own terminal, run `unset CLAUDECODE`. |
| Edits are refused in a job | The job was **Read only**. Use **Edit files** or **Full auto**. In Full auto, Claude's safety classifier can still refuse a risky command. The log says why. |
| A job fails with "Claude usage limit reached" | That account reached one of its plan's limits. The message names the window (for example the 5-hour session) and when it resets, and the **Usage** page shows every window. Wait for the reset, then send the job again. Sending it to another Claude account because this one is at its limit is the overflow pattern [guide 1](01-accounts-and-rules.md#more-than-one-claude-account) warns about. |
| A Claude agent's **Limits** column is blank on its machine's page, the Usage page has no card for it, and **Check limits** leaves it out without a word | The agent signs in with an API key. An API key is billed per token and has no plan limits to read. Its spend shows under **API spend** once you've added an Admin key ([guide 7](07-api-gateway.md#usage-panel-api-spend-per-organization)). |
| **Check limits** skips an agent, or its job says "Not checked": "signs in with a long-lived token (claude setup-token)" | Such tokens can't read plan usage, though the agent's jobs still report its figures. For a full login, remove `CLAUDE_CODE_OAUTH_TOKEN` from the agent's `env` in `runner.json`, then run the command the message shows (for example `claude-work auth login`) on that machine. |
| **Check limits** says "Checked … minutes ago" | Each agent, and each account on any machine, can be checked once every 5 minutes. Jobs keep the figures fresh in between. |
| **Check limits** says the login "can't read plan usage" | Claude Code returned no plan figures for that login. It gives them only to a claude.ai login on a plan with limits, not to an account billed by usage or a token with a narrow scope ([Agent SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage), last checked 6 November 2026). Sign the agent in again with its own command, for example `claude-work auth login`. |
| **Check limits** says Claude Code "printed nothing the hub could read as plan usage" | Claude Code couldn't fetch the figures, most often because its usage service limits how often they can be read. "Rate limited until …" means only the figures can't be fetched until then; your work isn't limited ([Claude Code costs](https://code.claude.com/docs/en/costs), last checked 6 November 2026). Try again after that time. Until then the bars keep the figures from the last job. |
| A job set to **Edit files, ask me before commands** fails with "The agent can't reach the hub's job tools" | Claude Code asks you through the hub's job tools at `<hub address>/mcp/job`, and this machine couldn't reach them. The message says why. Check that the machine can open the hub address in its `runner.json` (`hub_url`). Behind Cloudflare Access, the machine also needs its service token there. |
| **Edit files, ask me before commands** is greyed out on New task | The line under it says why. The agent isn't a Claude agent, or its runner is too old (press **Update now** on the machine's page). Or its entry in `runner.json` has a flag that changes permissions in `args` or `extra_args`, such as `--permission-mode`, which would win over asking: move it into the agent's `autonomy` levels ([guide 11](11-adding-agents.md#every-setting-an-agent-entry-can-have)). Or the hub has no MCP support (re-run the hub installer, which adds the `mcp` package). Or the hub has no dashboard password: set `HUB_UI_PASSWORD` in `/etc/bullpen/hub.env` and restart the hub, since without one any program on your network could answer for you. |
| A question was denied with "No answer from the owner within 10 minutes" | Nobody pressed **Allow** or **Deny** in time (`HUB_APPROVAL_TIMEOUT_MIN`, [guide 4](04-proxmox-hub.md#running-it)). The agent was told to find another way or to say what it needed. **Follow up** on the job to carry on. [Notifications](08-daily-use.md#notifications-on-your-phone) tell you when a question is waiting. |
| A question was "denied unseen" as too long to review | Its input was over 20,000 characters, too much to check on a card. The agent was asked to split the work into smaller steps. "A tool name of … characters (too long to show)" means the request named a tool with a name over 200 characters long. No real tool has one, so it was denied without asking you. |
| "The session has ended" when you send a message | The live session ended: you pressed **End session**, it had no message for its quiet time, or it reached the time limit. The log's last line starting with ⏹ says which. **Follow up** continues the conversation. |
| **Start Remote Control** says it "needs a one-time answer in a terminal" | The first time on each machine, Claude Code asks "Enable Remote Control?" and may ask to trust the folder, and both need a terminal. Run the two commands the message shows on that machine, answer `y`, stop it with Ctrl+C once it says it is ready, then press **Start Remote Control** again. |
| **Start Remote Control** says the agent "signs in with an API key" or "with a long-lived token" | Remote Control needs a full claude.ai login ([Remote Control docs](https://code.claude.com/docs/en/remote-control), last checked 6 November 2026). For a long-lived token, remove `CLAUDE_CODE_OAUTH_TOKEN` from the agent's `env` in `runner.json` and run the command the message shows (for example `claude-work auth login`) on that machine. An agent with an API key can't use Remote Control: use one with a subscription login. |
| **Start Remote Control** says the agent "has no full claude.ai login that Remote Control can use" | Claude Code gives the same answer when the agent isn't signed in at all and when it uses a long-lived `claude setup-token` token ([Remote Control docs](https://code.claude.com/docs/en/remote-control#troubleshooting), last checked 7 November 2026). Run the command the message shows (for example `claude-work auth login`) on that machine. If the `env` block of that agent's Claude Code `settings.json` (`~/.claude/settings.json`, or the one in its own settings folder such as `~/.claude-work`) sets `CLAUDE_CODE_OAUTH_TOKEN`, remove it first. |
| **Start Remote Control** says the agent "uses another service than Anthropic's own" | The agent is set up for Bedrock, Vertex or Foundry, or has `ANTHROPIC_BASE_URL` in its `env`. Remote Control works only with Anthropic's own service, so use another agent. |
| **Start Remote Control** says it "isn't enabled for the Claude account" the agent uses | Claude Code's check for that account came back off. The usual cause is a login that is out of date after a plan change ([Remote Control docs](https://code.claude.com/docs/en/remote-control#troubleshooting), last checked 7 November 2026). On that machine, run the two commands the message shows (for example `claude-work auth logout`, then `claude-work auth login`), and update Claude Code if it is old. |
| **Start Remote Control** says it "is turned off by a policy" for the agent's Claude account | On Team and Enterprise plans, Remote Control is off until an Owner turns it on in the organization's admin settings. On a Pro or Max plan, Claude Code may still be signed in under an earlier Team or Enterprise organization: run the two commands the message shows on that machine to sign in again. If the lines under the message name `disableRemoteControl`, that machine's managed settings turn it off, whatever the plan ([Remote Control docs](https://code.claude.com/docs/en/remote-control#troubleshooting), last checked 7 November 2026). |
| **Start Remote Control** says Claude Code "can't check whether Remote Control is available" | A setting turns off the feature flags Remote Control needs. The message names it: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_GROWTHBOOK` or `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`. Remove it from the agent's `env` in `runner.json`, from that user's environment, and from the `env` block of the agent's Claude Code `settings.json` (`~/.claude/settings.json`, or the one in its own settings folder such as `~/.claude-work`), then restart the runner ([Remote Control docs](https://code.claude.com/docs/en/remote-control#troubleshooting), last checked 7 November 2026). |
| **Start Remote Control** says Claude Code "couldn't reach Anthropic to check whether Remote Control is available" | The machine is offline, or a proxy or firewall blocks Claude Code's check ([Remote Control docs](https://code.claude.com/docs/en/remote-control#troubleshooting), last checked 7 November 2026). Check that machine's network, then press **Start Remote Control** again. |
| "doesn't allow Remote Control from the dashboard" | That machine's `runner.json` has `allow_remote_control` set to `false`, the default ([guide 5](05-linux-and-macos-machines.md#remote-control)). Set it to `true` there. The runner picks the change up within 15 seconds. |
| Remote Control "started, but hasn't shown its address" | The server is running but hadn't printed its claude.ai address within 15 seconds. It may still work: open claude.ai/code or the Claude app, signed in to that account, and look for the server's name (for example "build-box myapp"). If it isn't there, press **Stop** and read the lines the job shows. |
| Start Remote Control was "Not picked up within 2 minutes" | The machine went offline before it picked the request up. The hub drops it rather than start Remote Control much later. Press **Start** again once the machine is back. |
| A Remote Control server stopped by itself | It stops after `remote_control_max_hours` (8 by default), when `allow_remote_control` is turned off, when the runner stops, and when the hub no longer accepts the machine's token. The machine's page says which. |

### Checks with the real Claude Code

**The permission prompt tool.** The runner never pre-approves the hub's `approve` tool in `--allowedTools`:
Claude Code finds its permission prompt tool among the connected MCP tools, and the model can't call it on its
own. This was checked with Claude Code 2.1.319 on Linux on 7 November 2026, against a stand-in for the hub's
job tools. With `approve` left out of `--allowedTools`, Claude Code still asked the tool before a Bash command.
It ran the command after an allow (it printed `42`) and didn't run it after a deny, which it listed among the
job's permission denials. `approve` never appeared among the tools the model itself could call.

**Remote Control.** The runner starts `claude remote-control` the way it starts jobs: with no terminal, an
empty input, and its output read through a pipe (on Windows, also without a window). Anthropic describes
Remote Control for a terminal, so this needs checking on each system with a real claude.ai login. So far:

| System | Starts without a terminal and shows its address (real login) | Stops on **Stop** (stand-in) | Stops with the runner (stand-in) |
|---|---|---|---|
| Linux | not yet checked | checked, 7 November 2026 | checked, 7 November 2026 |
| macOS | not yet checked | in the automated tests | not yet checked |
| Windows | not yet checked | in the automated tests (at once with `taskkill /T /F`) | in the automated tests (the runner's Job Object) |

The stand-in is the self-test agent's pretend `remote-control`, used by the automated tests.

On Windows the runner can't ask a server to stop cleanly: it ends it at once with `taskkill /T /F`. Ctrl+Break
can't reach a server that runs without a window, and Claude Code would only be killed by it. So the server
doesn't sign off, and claude.ai may list it for a while. In a git project it may also leave the worktrees it
made for its sessions. `git worktree list` in the project folder shows them, and `git worktree remove <path>`
removes one you no longer need.

**Manual checks before relying on Remote Control.** Do these once on each system you want to use it on.
If a check fails, leave `allow_remote_control` off on that machine, start Remote Control by hand in a
terminal instead ([guide 8](08-daily-use.md#live-interactive-sessions-remote-control)), and
[open an issue](https://github.com/rob-paprocki/agent-bullpen/issues) saying what you saw.

On **Linux and macOS**:

1. In a terminal on the machine, go to a project folder and run Remote Control once by hand, so it can ask
   its one-time questions. Answer `y`, and stop it with Ctrl+C when it says it is ready:

   ```bash
   claude remote-control --name test --spawn same-dir
   ```

2. Run it again the way the runner does, without a terminal for its input. Within 15 seconds it should print
   a line with a `https://claude.ai/code/…` address:

   ```bash
   claude remote-control --name test --spawn same-dir < /dev/null 2>&1 | cat
   ```

3. Open [claude.ai/code](https://claude.ai/code), signed in to the same account, start a session on "test" and
   send a short message. Then stop the command with Ctrl+C.
4. Check that nothing is left running. This should print nothing:

   ```bash
   pgrep -fl remote-control
   ```

5. Set `allow_remote_control` to `true` in the machine's `runner.json`. On its page on the dashboard, press
   **Start Remote Control**, check that **Open** works, then press **Stop**. Run step 4's command again.
6. Press **Start Remote Control** once more, then restart the runner ([guide 5](05-linux-and-macos-machines.md#managing-the-runner)).
   Run step 4's command again: the server must have stopped with the runner.

On **Windows**, in PowerShell:

1. Go to a project folder and run Remote Control once by hand. Answer `y`, and stop it with Ctrl+C when it
   says it is ready:

   ```powershell
   claude remote-control --name test --spawn same-dir
   ```

2. Set `allow_remote_control` to `true` in `%USERPROFILE%\.bullpen\runner.json`. On the PC's page on the
   dashboard, press **Start Remote Control**. Within 15 seconds the page should show the server with an
   **Open** link, and no window should appear on the PC.
3. Open the link, signed in to the same account, and send a short message.
4. Press **Stop**. Check that nothing is left running. This should print nothing:

   ```powershell
   Get-CimInstance Win32_Process | Where-Object CommandLine -like '*remote-control*'
   ```

5. Press **Start Remote Control** once more, then restart the runner
   ([guide 6](06-windows-machines.md#managing-the-runner)). Run step 4's command again: the server must have
   stopped with the runner.

## Gemini CLI

| Symptom | Fix |
|---|---|
| "Please set an Auth method" or a login prompt | Gemini CLI is set to Google login, which no longer works with Google AI plans. Run `runner.py init` again and re-enter the key, or in `gemini` run `/auth` and choose the Gemini API key. |
| A job fails with "Gemini quota hit" | The job ended at a rate or quota limit of the Google Cloud project behind the agent's API key, after Gemini CLI had already retried by itself (up to 10 attempts, [its source](https://github.com/google-gemini/gemini-cli/blob/main/packages/core/src/utils/retry.ts)). Limits apply per project, not per API key, and requests-per-day limits reset at midnight Pacific time ([Gemini API rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), last checked 7 November 2026). See your project's limits on [AI Studio's rate-limit page](https://aistudio.google.com/rate-limit). On the free tier they are low: link billing in AI Studio to move to a paid tier. |
| `gemini: command not found` (Linux, macOS) | Open a new terminal (PATH) or run `export PATH="$HOME/.npm-global/bin:$PATH"`. |
| **May hand subtasks to other machines** doesn't show for a Gemini agent, or New task says it "can't hand out subtasks" | The runner hands Gemini CLI the job tools through a copy of the machine's `system-defaults.json` for that job only (`GEMINI_CLI_SYSTEM_DEFAULTS_PATH`). Gemini CLI 0.60 and later read that file only when root owns it and every folder above it, none writable by group or others (on Windows, an administrator or SYSTEM) ([its source](https://github.com/google-gemini/gemini-cli/blob/main/packages/cli/src/config/settings.ts), last checked 7 November 2026). The runner runs as your own user, so it can't make such a file, and it doesn't offer subtasks for Gemini agents. Gemini agents still take subtasks from other agents, which needs no job tools. Hand out subtasks from a Claude agent instead. |
| A Gemini job has no subtask tools | Only on a runner that runs as root. The job's log says "⚠ Subtask tools unavailable" and why: Gemini CLI skipped the job's settings file (its "Security Warning" line is above), or the job's folder (in `~/.bullpen/jobs`) or a folder above it isn't root's or is writable by group or others. Otherwise another settings layer wins over the job's: your own or the project's `.gemini/settings.json` has an MCP server called `bullpenjob` (remove it: the name is the hub's); `mcp.allowed`, `mcp.excluded` or `--allowed-mcp-server-names` leaves `bullpenjob` out; or an administrator's settings turn MCP servers off. |

### Manual check: subtasks with a real Gemini CLI

The way the runner hands Gemini CLI the job tools comes from Gemini CLI's source: settings merge from its
own defaults, then the system-defaults file, then your settings, then the project's, then an
administrator's, and MCP servers merge by name. Gemini CLI 0.67.0 was checked on Linux on 7 November 2026.
It read such a file only when root owned it and every folder above it, and skipped it with a "Security
Warning" otherwise. That is why only a runner that runs as root offers subtasks for Gemini agents. Handing
out a subtask from a real Gemini CLI with an API key has not been checked yet. The automated tests use a
stand-in that applies the same ownership rule: a Gemini job lists every job tool, hands out a subtask, and
leaves `~/.gemini/settings.json` as it was.

Do this once before you rely on subtasks from Gemini agents. You need a machine whose runner runs as root,
with Gemini CLI, its API key, and its own runner token.

1. On New task, pick that machine's `gemini` agent and tick **May hand subtasks to other machines**.
2. Send this prompt:

   ```text
   Call the job_info tool from the bullpenjob server and tell me what it says.
   ```

3. The answer should give the job's number and list `list_machines` and `delegate_task` among the tools. If
   Gemini says it has no such tool, look at the job's log for a line starting with "⚠".
4. In a terminal on that machine, check that your own Gemini settings still list your servers and no
   `bullpenjob`:

   ```bash
   gemini mcp list
   ```

If the check fails, don't allow subtasks for Gemini agents, and
[open an issue](https://github.com/rob-paprocki/agent-bullpen/issues) with the job's log. Claude agents and
the rest of Gemini's work are not affected.

## Windows setup script

| Symptom | Fix |
|---|---|
| "running scripts is disabled on this system" | Run `Set-ExecutionPolicy -Scope Process Bypass -Force` in the same window first. |
| `winget` not found | Install **App Installer** from the Microsoft Store. |
| Python opens the Microsoft Store | That's the Store's placeholder `python`. The script uses `py -3` instead, so finish the Python install from the script, or turn off the aliases in **Settings → Apps → Advanced app settings → App execution aliases**. |
| A command isn't found right after installing it | Close PowerShell, open a new window and run the script again. Finished steps are skipped. |

## Shared memory and MCP

| Symptom | Fix |
|---|---|
| A machine's page says **behind** | It updates within about 15 seconds of its next check-in. Use **Sync now** to force it. The job log shows each change. |
| A machine's page lists "exists and isn't ours" | You already had a file or MCP server with that name, so the runner left it alone. Rename yours, or delete it if it was an old copy, then sync again. |
| `/remember` isn't offered in Claude Code | Check that the skill exists: `ls ~/.claude/skills/remember` (or `~/.claude-<name>/skills/remember` for another account). In a running session, start a new one so skills reload. |
| "'bullpenjob' is reserved for the hub's own servers" on the MCP page | `bullpen` and `bullpenjob` are the hub's own servers (shared memory and job tools). Pick another name. |
| Claude says the `bullpen` tools aren't available | Run `claude mcp list` (or `claude-<name> mcp list`). `bullpen` should be listed and connected. If it shows failed, open `http://<hub-ip>:8080/healthz` from that machine. The hub may also need `pip install mcp` (re-run the hub installer). |
| Gemini CLI doesn't see the shared notes | `~/.gemini/GEMINI.md` must contain the `bullpen:begin` block with `@./bullpen-shared.md`. Run `/memory reload` in Gemini, or sync again. |
| Antigravity's MCP list lost your comments | The runner can't keep comments when it rewrites `mcp_config.json`. Your original is saved next to it as `mcp_config.json.bak-bullpen`. |
| An OAuth MCP server keeps asking to sign in on every machine | That's expected for servers added **directly**. Edit it on the MCP page, choose **Through the hub** and **OAuth sign-in**, then press **Sign in** once ([guide 10](10-shared-memory-and-mcp.md#signing-in-once-with-oauth)). |
| An agent says a server "is not signed in" or "needs a sign-in on the hub" | Press **Sign in** next to it on the MCP page. If the service revoked the hub's access, the page shows **needs sign-in** with the reason. |
| **Sign in** fails with "refused automatic client registration" | That service needs an OAuth app you create yourself. Create one with the callback address the form shows, enter its client ID and secret, and sign in again. |
| The provider says the callback / redirect address isn't allowed | Set `HUB_PUBLIC_URL` in `/etc/bullpen/hub.env` to your https address (for example `https://hub.example.com`), restart the hub, and sign in from that address. For an app you created yourself, its callback address must match exactly. Many providers refuse plain `http://` addresses: [guide 3](03-cloudflare.md) shows ways to get https. |
| **Sign in** says "Open the hub at https://… to sign in" | `HUB_PUBLIC_URL` is set to that address. Open the dashboard there and press **Sign in** again. |
| The sign-in tab says "Finish the sign-in in the browser where you started it" | Open the dashboard and press **Sign in** in the same browser you sign in to the provider with (the hub ties each sign-in to the browser that started it). |
| A server routed through the hub fails for every agent | Press **Test** next to it: the hub shows the server's own error (wrong key, unreachable, timed out). The key and address are only on the hub, so fix them on the MCP page; no machine needs a sync. |
| An agent doesn't see a tool the server just added | The hub refreshes each server's tool list every 5 minutes. Press **Test** to refresh it now, then start a new agent session. |

## API side

| Symptom | Fix |
|---|---|
| LiteLLM won't start | Run `cd ~/bullpen-stack/litellm && sudo docker compose logs litellm` (`sudo docker-compose` on Debian 12). A wrong `DATABASE_URL` or an empty key is the usual cause. |
| LiteLLM says there is no model by that name | A model name must start with one of your organizations' names (`work/<model-id>`), with `claude-` (the default organization), or with `gemini/`. The names are the `ANTHROPIC_KEY_<NAME>` lines in `~/bullpen-stack/litellm/.env`. |
| The script says "skipping ANTHROPIC_KEY_…" | That organization's name can't be used (see [guide 7, step 1](07-api-gateway.md#step-1-create-the-keys)). Rename the line in `.env` and run the script again. |
| The script says `config.yaml` "was edited by hand" | Its first two lines are gone, so the script leaves it alone. Delete the file and run the script again to let it write a new one. |
| LibreChat shows no models | Check that LiteLLM works at `http://<gateway-ip>:4000/ui`, and that `LITELLM_API_KEY` in `~/bullpen-stack/LibreChat/.env` is a valid LiteLLM key. |
| Usage page shows "HTTP 401" for an organization | That Admin key is wrong or was revoked. Admin keys start with `sk-ant-admin01-` and only work for organizations. |
| A job fails with **Platform credits used up** | The Anthropic API refused the agent's API key because its Console organization has no credit left ("Your credit balance is too low"). The plan's monthly credits come back at the next billing cycle ([API credits for subscribers](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers), last checked November 2026). Until then, buy credits in the Console or use another agent. The hub counts the organization as used up until its reset day (24 hours without one), so it moves nothing more to credits. A council step or subtask that had been moved to credits runs once more on the agent it named, by itself. |
| Routing didn't use credits: a council step or subtask ran on the agent named, not on the credits agent | Check each of these. **Use platform credits first for background work** is ticked on the Usage page. The organization's card says **available**: in **reserve** (90% used or more), **used up** or **not set up** (no amount entered, or no spend figures yet) nothing is moved. The machine that took the job has a credits agent: its page shows the **credits agent** chip, or says why not (no API key, label not accepted, or a label that isn't an organization in `HUB_ANTHROPIC_ADMIN_KEYS`); `runner.py list` on the machine also says. The named agent signs in with a subscription; a Gemini or API-key agent is never moved. The step doesn't continue a session (the builder's fixes stay with the agent that built) and needs nothing the credits agent lacks (asking before commands, for example). The spend figures are fetched every 15 minutes, so a change can take that long to show. |
| A task stopped at a usage limit and wasn't retried on credits | The hub retries it only when **Use platform credits first** is on, the machine's credits agent has credit **available**, and the task can't have changed files (read only, or its captured diff was empty). It never retries a live session, a follow-up or a retry. The job's page says when **Retry on credits** can still run it by hand. |

## Testing the plumbing without using any quota

Every runner can offer a **selftest** agent: a fake agent that answers instantly and costs nothing. Send it
a task, or run a whole council with selftest agents, to check that the hub, the runners and the network work.
In a copy of the repository, on Linux, you can also run the end-to-end test. It needs Python 3.11 or newer,
and the hub's packages in a private environment (current Debian, Ubuntu and Homebrew Pythons refuse a plain
`pip install` outside one):

```bash
python3 -m venv .venv
```

```bash
.venv/bin/pip install -r hub/requirements.txt
```

```bash
.venv/bin/python tests/smoke_test.py
```
