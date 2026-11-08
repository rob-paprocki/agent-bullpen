# 8 · Using it day to day

You'll use four places, roughly from most used to least used:

| Place | Use it for |
|---|---|
| **The hub's dashboard** (its LAN address, or the https address from [guide 3](03-cloudflare.md)) | Sending work to any machine and account, councils, history, API spend |
| **claude.ai/code** and the Claude app (Remote Control) | Live, back-and-forth Claude Code sessions on a machine, from your phone or browser; start them from a machine's page on the dashboard |
| **Antigravity** (app, `agy`, antigravity.google.com) | Work on your Google AI plan |
| **claude.ai**, **gemini.google.com**, LibreChat | Plain chat on each subscription, or on the API keys (LibreChat, [guide 7](07-api-gateway.md)) |

## The hub

### Overview

This page shows every machine, online ones first. Offline machines are folded away until you open them.
With many machines, type in the **filter** box (a name, host, system, agent or tag), or switch from **Cards**
to **List**. Each machine shows:

- a green dot when it's online;
- its agents as chips (a warning sign means not ready; hover to see why);
- how many jobs it's running;
- **own token** or **shared token**: how it signs in to the hub;
- its tags, if it has any; dimmed ones count only once you accept them on its page
  ([guide 5](05-linux-and-macos-machines.md#tags-and-subtasks));
- an **update** badge when the machine's runner is older than the hub's;
- a warning when two computers check in under the same name, naming the other computer.

**Add a machine** makes a one-time code and shows the command to paste on the new machine, one for Linux and
macOS and one for Windows ([guide 5](05-linux-and-macos-machines.md), [guide 6](06-windows-machines.md)).
Codes you didn't use are listed there too, with **Revoke**. **Forget this machine** (on an offline
machine's card, or at the bottom of its page) removes it and revokes its own token.

Below the machines is **Recent work**: the tasks and councils you sent. Machine chores (syncs, updates, git
actions) have their own folded list, so they don't push real work out of view. Click any row to open it. A
job that stopped at a usage limit shows **Claude usage limit** or **Gemini quota hit** next to its status.
When any machine is out of date, an **Update all runners** button appears. Both it and **Sync all** say which
machines they reached and which they skipped, and why.

These notices show at the top of the page when they apply:

- **New tags or account labels** names the machines that report tags or labels you haven't accepted yet.
  They count for tasks aimed at a tag and for subtasks once you accept them on the machine's page.
- **Waiting for your approval** lists the jobs that are waiting for you to allow or deny a command, with the
  tool each one wants to use. Click one to answer on its page. Their rows in **Recent work** show **waiting
  for you**.
- **Remote Control running** lists every Remote Control server the machines report: the agent, the project
  and the machine, when it started and when it stops by itself. **Stop** ends it.

### History

**History** lists every job, newest first, a page at a time. Filter by machine, agent, kind, status or words
in the title. The filters stay in the page's address, so you can bookmark a filtered list.

In the **Where** column, a task for a tag that no machine has taken yet reads "any machine tagged …". A
subtask starts with "↳ #N", a link to the task that handed it out, and a task that handed out subtasks says
how many.

### A machine's own page

Click a machine's name to manage it without going to it:

- **Runner:** its version, and an **Update now** button. The runner downloads the new files from the hub,
  checks them, waits until it isn't running anything, then restarts itself.
- **How it signs in:** with its own token, or with the shared one. For a machine on the shared token, **Make a
  setup command** gives it its own (run the command on that machine; it keeps its name and projects).
  Tasks for a tag and subtasks only go to machines with their own token.
- **Tags** show in the page's header: dimmed ones are reported but not accepted. A **takes subtasks** chip
  means the machine's `runner.json` lets agents on your other machines hand it subtasks.
- **Tags and account labels:** what the machine reports about itself counts for tasks aimed at a tag and for
  subtasks only once you accept it here.
  1. Under **To accept**, look at the tags and labels listed. Untick anything you don't recognise, and any
     label that isn't that very account.
  2. Click **Accept**.

  **Accepted** lists what counts now. Click **×** next to one to withdraw it: tasks for that tag stop coming
  here, and an agent whose label you withdraw counts as an account of its own again. The operating-system
  tag (`linux`, `macos`, `windows`) counts without accepting.
- **Agents:** whether each one is ready, and its default model and model suggestions. For Claude agents
  there are two more columns:
  - **Account** is the agent's account label, or "(no label)" ([guide 5](05-linux-and-macos-machines.md#account-labels)).
  - **Limits** is how much of each plan window is used, for example "5h 31% · week 47%", and how old the
    reading is. It turns amber when a window is close to its limit and red at the limit. "No reading yet"
    means no job or check has reported figures for that agent; run a job, or press **Check limits**. "No
    reading yet: run a job" means Check limits can't ask that agent (see [Usage](#usage)), so only a job
    brings its figures.
  - The machine's **credits agent**, if it has one ([guide 5](05-linux-and-macos-machines.md#a-credits-agent)),
    has a **credits agent** chip under its name, with its organization and the state of its platform credits
    (available, reserve, used up, or not set up), as on the [Usage](#platform-credits) page. When it can't be
    used, the chip says why, for example that its account label isn't accepted yet.
- **Check limits** (next to the agents) asks each Claude agent on the machine for its plan limits, with Claude
  Code's own `/usage` command. It opens the job, which lists what it read. The job starts with a "Not
  checked" line, with the reason, for each agent with a long-lived `setup-token` login: it can't ask those.
  Agents that use an API key have no plan limits, so it leaves them out. See [Usage](#usage) for what it can
  and can't do.
- **Remote Control** lists the Remote Control servers running on the machine, with **Open** (their
  claude.ai address) and **Stop**, and has the form to start one ([below](#live-interactive-sessions-remote-control)).
  While a server runs, anyone signed in to that Claude account can drive Claude in that project folder and
  approve its commands, and none of that shows in the hub. A server stops by itself after 8 hours. Claude
  agents that can't run it (an API key, a `setup-token` login) are listed with the reason.
- **Projects:** every folder it offers, with **Pull** (`git pull --ff-only`) and **Remove**. Remove only stops
  offering the folder; nothing is deleted.
- **Clone a git repository** or create a **new empty project**. New projects always go inside that machine's
  projects folder (`~/projects`, or `C:\Users\<you>\projects` on Windows). Project folders elsewhere on the
  disk can only be added on the machine itself. (That guards against mistakes. It won't stop a determined
  intruder: anyone with your dashboard login can also run "Full auto" jobs and push MCP servers to every
  machine, so protect that login as you would the machines themselves.)

These actions show up among the machine chores like any job, with a log, so you can see what `git` said.
The page also lists the machine's recent work, with a link to all of it in **History**.

If you don't want a machine to be managed remotely, answer "no" to those two questions in
`runner.py init`. That sets `allow_remote_projects` / `allow_remote_update` to `false` in its `runner.json`.

### New task: one prompt, one agent

1. Pick **machine → agent → project → model**. `chat` is a scratch folder on every machine, for questions
   that aren't about a codebase. Leave the model blank for the agent's default, or pick or type one. Each
   agent suggests its own models (Claude Code's aliases, the Gemini models your key can use); you can type
   any model id the CLI accepts. A follow-up keeps the same model unless you choose another. Council seats
   have the same model box.

   Instead of one machine you can pick **Any machine tagged …** at the bottom of the machine list. The task
   then runs on the first free machine with that accepted tag that has the agent and the project, and stays
   there: a follow-up goes to the same machine. The agent list shows each agent's account. When the agent is
   a different account on different machines with that tag, an **Account** box appears: choose one. The
   task waits for a machine with that account rather than run under another one. Only machines with their
   own runner token take tasks for a tag.
2. Pick what the agent may do:
   - **Read only** is the default. Use it for questions, explanations and reviews.
   - **Edit files** lets it change files in the project. Tick "show me a diff" to see the changes afterwards.
   - **Edit files, ask me before commands** (Claude agents) lets it change files, and asks you on the job's
     page before it runs a command ([below](#a-jobs-page)). File commands inside the project (`mkdir`, `mv`,
     `cp`, `rm`, `sed`) and read-only commands (`ls`, `cat`, `grep`, read-only `git`) run without asking.
     Claude Code always asks before writing its own settings, the project's MCP list or git's folder
     (`.claude`, `.mcp.json`, `.git`). The choice is greyed out, with a line saying why, when the agent's
     runner is too old, the hub has no MCP support, or the hub has no dashboard password.
   - **Full auto** also lets it run commands (builds, tests). Claude's safety classifier still checks each
     command.
3. Optionally tick **Keep the session open for messages** (Claude agents). You can then type to the agent on
   the job's page while it works ([below](#a-jobs-page)). The session ends when you press **End session**,
   after the number of minutes without a message you set (15 by default), or at the job's time limit.
4. Optionally tick **May hand subtasks to other machines** ([below](#letting-an-agent-hand-out-subtasks)).
5. Optionally give the task a **Title**. Lists show it instead of the prompt's first line, and so do
   [notifications](#notifications-on-your-phone).
6. Write the prompt and press **Send**. The job page shows the live log as it works, then the answer.
7. **Follow up** continues the same conversation, on the same machine and agent ([below](#a-jobs-page)).
   **Run again…** copies the prompt into a new task, so you can send it elsewhere.

Example uses:

- `build-box · claude-work · myapp · Full auto`: "Run the test suite and fix whatever makes it fail."
- `desktop · gemini · chat · Read only`: "Compare WireGuard and Tailscale for remote access to a Proxmox node."
- `Any machine tagged gpu · claude · myapp · Edit files`: "Profile the training script and make it faster."

### Letting an agent hand out subtasks

With **May hand subtasks to other machines** ticked, the agent can list your other machines, start a
subtask on one, wait for its answer, and cancel it. For example, a task on `build-box` can ask `gemini` on
another machine for a second review, or run the tests on any machine tagged `windows`. Each subtask is an
ordinary job you can open, titled "Subtask of #N".

The box shows only for agents that can use the hub's job tools, on a hub with MCP support and a dashboard
password. Claude agents can. Gemini agents can, but only on a runner that runs as root
([guide 9](09-troubleshooting.md#gemini-cli)), though any Gemini agent can take subtasks. It is greyed out on a machine that still uses the shared runner token.

When you tick it, you set the scope:

- **Subtasks may go to:** the machines and tags subtasks may use. Nothing ticked means every machine that
  takes subtasks. A machine takes them only when its `runner.json` says so (`accept_delegated_tasks`,
  [guide 5](05-linux-and-macos-machines.md#tags-and-subtasks)), and only with its own runner token.
- **Projects:** by default only a project named like this task's, and `chat`. Choose **any project** to
  allow more.
- **Accounts** and **Agents:** by default every one of your accounts and agents on those machines is ticked,
  your other Claude account and Gemini included. Untick the ones subtasks must not use.

The hub keeps these rules whatever the agent asks:

- A subtask may only read, unless your hub allows more (`HUB_DELEGATE_MAX_AUTONOMY`,
  [guide 4](04-proxmox-hub.md#running-it)). It never gets more than the task itself.
- A subtask can't hand out subtasks of its own.
- A task can hand out at most 5 subtasks, 3 at a time.
- A subtask's time limit ends with the task's.
- **No overflow to another account.** Handing work to your other Claude account because this one is close to
  its limit is the overflow pattern [guide 1](01-accounts-and-rules.md#more-than-one-claude-account) warns
  against. Untick that account if the task has no other reason to use it. The hub blocks it once a limit is
  hit: after a usage-limit error in the task or one of its subtasks, new subtasks may use only the account
  that hit the limit. That holds for its follow-ups too, for a week. Usage limits aren't a reason to switch
  accounts; wait for the reset.

Only allow subtasks for tasks whose input you trust: a web page or a file the agent reads could try to talk
it into starting work elsewhere, and a subtask can read any file in its project. While such a task runs,
don't run Full-auto tasks on untrusted content on the same machine: they could read the task's job token
and use its subtask powers.

### A job's page

Click any job to open its page. While the job runs, the page shows its live log and refreshes every 2
seconds. A refresh never wipes what you are typing.

**A question from the agent.** In a job set to ask you before commands, a card appears when the agent wants
to run something: "claude-work wants to use Bash", then exactly what it wants to run, in full.

1. Read the whole input, down to the buttons. The card shows all of it, however long, and nothing in it
   scrolls on its own. Long commands wrap, and each real line starts on a line of its own; a field with
   more than one line says how many ("command · 41 lines"). Characters that don't show on screen (control
   codes, right-to-left marks, zero-width spaces) are written as highlighted codes, and the card then says
   **hidden characters**: be suspicious of those. Two or more blank lines in a row are drawn as one
   highlighted line ("39 blank lines"), and the card then says **blank lines**: read what comes after.
2. The agent's own description, if any, comes after the input, as "The agent says: …". It is the agent's
   claim, not a check.
3. Press **Allow** to let it run exactly what the card shows. Or type a **Reason** and press **Deny**; the
   agent gets your reason and can try another way.

The agent edits files in the project without asking. So a command that runs the project's own code (`npm
test`, `make`, `pytest`) runs whatever the agent wrote there: allow it only for a task whose input you trust.

If you don't answer by the time the card shows (10 minutes by default), the hub denies it. Several cards can
appear at once when the agent runs tools in parallel. Answered questions are listed under **Approvals**. A
question from a council step says that its prompt may include text other agents wrote.

**Talking to a live session.** For a task with "Keep the session open for messages", the page shows the
conversation as it goes: your prompt, each answer, and the messages you sent.

1. Type in **Message** and press **Send** (or Ctrl+Enter). The agent reads it when it has finished its
   current answer; until then it shows "waiting for the agent".
2. Send more whenever you like. Each message is one turn, answered in order.
3. Press **End session** (next to the title) when you're done. The agent first answers the messages it
   already has, then the job finishes with its last answer.

Messages starting with `/` can't be sent: Claude Code would run them as its own commands. If the session
ends (at its time limit, for example) before the agent read a message, the message is marked "not delivered:
the session had ended". Once a session has ended, **Follow up** continues the conversation.

**A task waiting for a tag.** Until a machine takes it, a task for a tag says what it waits for, for example
"Waiting for a free machine tagged gpu with claude (personal) and myapp. 2 machines match; 1 is online."

**Subtasks.** A task that handed out subtasks lists them in a **Subtasks** card: number, title, machine,
agent, account and status, each linked to its own page. The page keeps refreshing while any of them runs. A
subtask's own page says "subtask of #N", with a link back. **Cancel** on the task cancels its queued
subtasks and stops the running ones. When the task ends any other way, its queued subtasks are cancelled and
the running ones finish by the task's deadline.

Once the job has finished, the page shows the answer (or the error, and the diff if you asked for one), and
below it:

- **Continue in a terminal.** The command that opens the job's session in your own terminal on that machine,
  with a **Copy** button. It moves to the project folder, then resumes the session, for example:

  ```bash
  cd '/home/you/projects/myapp' && claude-work --resume 4f2a9c1e-5b7d-4e8f-9a0b-1c2d3e4f5a6b
  ```

  It is shown for Claude and Gemini agents that use the same sign-in as your own terminal. An agent whose
  API key or sign-in token is in `runner.json` gets no command, because your terminal wouldn't use it. Other
  agents get one when their entry has a `terminal` setting ([guide 11](11-adding-agents.md#every-setting-an-agent-entry-can-have)).
- **Follow up.** It continues the same conversation, on the same machine and agent. It works after an
  error, a cancel or a time-out too, as long as the agent had started its session, and on a council's steps.
  You choose again what the agent may do. Leave the model box blank to keep the job's model, or pick
  another.
- **Show as a conversation.** Once a job has follow-ups, or is one, this link shows the whole chain on one
  page: each prompt and each answer, with a link to each job.
- **Tokens.** When the job has finished, a line shows the tokens it used: tokens in (with how many were read
  from the cache) and tokens out. Hover over the figures to see each part. For Claude agents the line
  adds an **API-equivalent estimate**: what the job would have cost at API list prices, as Claude Code itself
  works it out. A subscription isn't billed per token, so the estimate only shows how heavy the job was. A
  follow-up counts only what it added to the conversation. When the hub can't tell that apart (for example
  when the job it continues was stopped before Claude Code saved its totals), the line says the figure
  includes earlier turns or is approximate.
- **Usage limits.** A job that stopped at a limit says so in its error. **Claude usage limit** names the
  window and when it resets, and links to the [Usage](#usage) page. **Gemini quota hit** links to Google's
  rate-limit page ([guide 9](09-troubleshooting.md#gemini-cli)). **Platform credits used up** means the API
  refused an agent with an API key because its Console organization has no credit left
  ([Platform credits](#platform-credits)).
- **On credits.** A job that ran on its machine's credits agent instead of the agent its council seat or
  subtask named says "on credits (agent claude-api) instead of claude-work", here, on its council's page
  and in History. A retry says "retried on credits" with a link to the job that stopped, and that job links
  to its retry. A task that stopped at a usage limit, and wasn't retried by itself because it may have
  changed files, shows a **Retry on credits** button when its machine has a credits agent with credit left.

### Council: agents working together and against each other

**Debate.** Use it for decisions and questions where being wrong is expensive.

1. Pick 2–12 **participants** and a **judge**. Mix models: `claude` and `gemini` disagree more usefully
   than two copies of Claude. Pick a project if the question is about code on that machine.
2. Optionally tick **revise round**, so each participant fixes its own answer after the critiques.
3. What happens:
   - each participant answers on its own;
   - each one attacks the others' answers, which are anonymised as "Response A/B";
   - the judge writes a ruling: verdict, where they agree, where they disagree and who's right, and a
     final answer.

   The result page shows who was who at the bottom.

**Build + review.** Use it for code changes you want checked before you trust them.

1. Pick a **builder** (machine, agent and project) and up to 10 **reviewers**. Choose what the builder may
   do: edit files, edit files and run the commands you approve (Claude agents; the questions appear on the
   build step's page), or edit and run commands.
2. A reviewer on the **same machine** can open the real files. Reviewers elsewhere see the diff.
3. What happens:
   - the builder does the work;
   - the runner records a `git diff`;
   - reviewers hunt for bugs, security problems and missed requirements;
   - if **fix** is ticked, the builder checks each finding against the code, fixes the valid ones and
     explains which it rejected.

Any seat can be **Any machine tagged …** instead of one machine, as on New task. A builder aimed at a tag
builds on the first free machine with that tag. A reviewer on that same machine opens the real files, and
the fix step goes back to the machine that built, where its session is. The result's "who was who" names the
machine each seat ran on.

Suggested line-ups:

| Situation | Participants / builder | Reviewers / judge |
|---|---|---|
| Architecture or tooling decision | `claude`, `gemini` | judge: `claude`, revise on |
| Change on a Windows project | builder `claude` on the Windows machine (Edit files) | `gemini` on the same machine (sees the files) |
| Security-sensitive change | builder `claude` | `gemini`, plus a Claude agent on an API key with a different model |

**Keep an eye on quota.** A three-way debate with a judge is seven agent runs, and a revise round makes it
ten. Councils are for things worth that. Several runs at once on one subscription use its limits quickly
([guide 1](01-accounts-and-rules.md#anthropic-what-is-allowed)).

### Memory and MCP

- **Memory** shows every shared note. You can search, add, edit, pin and delete them there. Agents add
  notes with `/remember`.
- **MCP** is the central list of MCP servers every agent gets, plus the hub's own `bullpen` memory server.

Both are explained in [guide 10](10-shared-memory-and-mcp.md).

### Usage

The Usage page has six parts.

**Claude plan limits.** One card per Claude account, with a bar for each limit window the plan has: the
5-hour session, the week for all models, and weekly limits for single models (for example "Week (Fable)").
Each bar shows how much is used and when it resets, in your own time zone. A bar turns amber when the window is
close to its limit, and red at the limit or past it.

- The figures come from Claude Code itself. Every Claude job reports them as it runs, so they are as fresh as
  the last job on that account. The card says which machine and agent the newest reading came from, and how
  old it is.
- **Check limits now** asks Claude Code's own `/usage` command for fresh figures: once for each account, on
  one online machine signed in to it. A machine's page has the same button for its own agents. Claude Code
  answers `/usage` itself, without a model turn (seen with Claude Code 2.1.319), so a check costs nothing.
  The hub checks each agent and each account at most once every 5 minutes. Press it again sooner and the
  Usage page lists the accounts it skipped, with how long ago each was checked; a machine's page says when
  you can try again.
- Agents with the same account label share one card ([guide 5](05-linux-and-macos-machines.md#account-labels)).
  When a label is used on more than one machine, the card lists them, so a label given to two different
  accounts by mistake shows. An agent without a label has a card of its own.
- Only the windows Claude Code reports for that account appear, and each one can be missing on its own
  ([status line docs](https://code.claude.com/docs/en/statusline), last checked 6 November 2026). A window
  whose reset time has passed shows "Reset at …; no reading since." until a new reading comes in.
- Agents that report limits but whose account has no reading yet are listed under the cards, with "No
  reading yet". An agent that shares its label with one that has a reading isn't listed: its account's
  card is there, and Check limits reads each account once.
- Check limits needs a full claude.ai login. It can't read plan limits for an agent with an API key (there
  are none) or one signed in with a long-lived `claude setup-token` token. Such a token's jobs still report
  figures, so the agent is listed with "No reading yet … run a job" until one runs. While it is listed,
  **Check limits now** names it under "Skipped" with the reason ([guide 9](09-troubleshooting.md#claude-code)),
  unless an agent with a full login and the same account label can be checked instead.

These figures help you plan. The hub never picks, holds back or switches accounts because of them, and a
limit isn't a reason to switch accounts: wait for the reset
([guide 1](01-accounts-and-rules.md#more-than-one-claude-account)).

**Agent use, last 30 days.** A table of the tasks that finished in the last 30 days, per machine and agent:
the number of jobs, tokens in and out, and the API-equivalent estimate. The estimate is Claude Code's own, at
API list prices; Gemini CLI reports tokens only.

**Gemini.** Gemini API keys have no readout of the quota left, so the page shows none. Limits apply per
Google Cloud project, and the link opens Google AI Studio's rate-limit page for your projects. The page counts
the jobs that hit the Gemini quota in the last 30 days.

**Platform credits.** One card per Console organization in `HUB_ANTHROPIC_ADMIN_KEYS`; see
[Platform credits](#platform-credits) below.

**API spend.** Spend per Console organization once you've added Admin keys
([guide 7](07-api-gateway.md#usage-panel-api-spend-per-organization)). The hub fetches it every 15 minutes;
**Refresh** fetches it now, at most once a minute.

**Subscriptions and accounts.** Links to each provider's own usage pages. Keep one browser profile per
Claude account, so each link opens signed in to the right one.

### Platform credits

Claude Max, Ultra and Team plans include monthly API credits in the Console organization linked to the plan
([guide 1](01-accounts-and-rules.md#platform-credits-the-api-credit-in-your-plan); Anthropic's
[API credits for subscribers](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers),
last checked November 2026). No API reports how much of them is left, so the hub estimates it. It needs the
organization's Admin key in `HUB_ANTHROPIC_ADMIN_KEYS` ([guide 7](07-api-gateway.md#usage-panel-api-spend-per-organization)).

**Setting it up.**

1. On the Usage page, under **Platform credits**, find the organization's card.
2. Type the monthly credit in **Monthly credits (USD)**, for example `200`.
3. Type the day of the month your plan renews in **Reset day of the month** (1 to 31). A day the month
   doesn't have counts as its last day, so 31 means 30 April and 28 February.
4. Click **Save**. Clear both boxes and save to remove them.

**What each card shows.** Every figure is an estimate: the spend is everything in the organization from
Anthropic's cost report, which doesn't say what the credits paid for, and credits you bought are spent after
the plan's credits.

- **Spent this billing cycle** (this calendar month without a reset day), and the average spend a day over
  the last 7 days, or since the cycle started when that is shorter.
- With an amount and a reset day: what is **left**, a bar with how much is used, the **next reset** in your
  own time, and when the credits **run out** at the current rate, or "Lasts until the reset".
- Whether the API refused a job for lack of credit this cycle.
- The **state**: **available** (under 90% used), **reserve** (90% or more), **used up** (100% by the estimate,
  or the API refused a job for lack of credit), or **not set up** (no amount yet). After a refusal, the
  organization counts as used up until its next reset, or for 24 hours without a reset day.
- Which machines name a credits agent for it ([guide 5](05-linux-and-macos-machines.md#a-credits-agent)).

**Use platform credits first for background work.** Off by default. Tick it at the top of **Platform credits**
to let the hub use the credits for work you don't start by hand:

- **Council steps and subtasks.** When a step or subtask names a Claude agent that signs in with a
  subscription, and the machine that takes it has a credits agent whose organization is **available**, it
  runs on the credits agent instead. In **reserve** or **used up**, or on a machine without a credits agent,
  it runs on the agent named. A step that continues a session (the builder's fixes) stays with the agent
  that started it, and a live session is never moved. A subtask moves only if the agents and accounts you
  allowed for subtasks on New task include the credits agent and its account.
- **Credits that run out.** If a step or subtask on the credits agent finds the credits used up, it runs once
  more on the agent it named, and the organization counts as used up.
- **Tasks you start** run on the agent you pick. If one stops at a subscription usage limit and its machine
  has a credits agent with credit available, the hub runs the same prompt once more on the credits agent, as a
  new job ("retried on credits"). Only once, never for a live session or a follow-up, and only for a task
  that can't have changed files: one that was read only, or one whose captured diff was empty. Otherwise its
  page offers **Retry on credits**, so you can check the project first. A job you send to the credits agent
  yourself always runs, even in reserve.

The hub never moves work from one subscription account to another, never picks an agent you didn't
configure, and never looks at a subscription's remaining usage to decide
([guide 1](01-accounts-and-rules.md#platform-credits-the-api-credit-in-your-plan)).

## Live interactive sessions: Remote Control

For a short back-and-forth with an agent, a task with "Keep the session open for messages" is enough
([A job's page](#a-jobs-page)). For a longer session from your phone ("try this… no, that…"), use
Anthropic's own **Remote Control**. Claude runs on your machine, and you drive it from
[claude.ai/code](https://claude.ai/code) or the Claude app.

**From the dashboard.** The machine must allow it first: `allow_remote_control` in its `runner.json`, off by
default ([guide 5](05-linux-and-macos-machines.md#remote-control)).

1. Open the machine's page.
2. Under **Remote Control**, pick the agent and the project, and press **Start Remote Control**.
3. When it is running, the page shows the server with an **Open** link. Open claude.ai/code or the Claude
   app, signed in to that agent's Claude account, and start a session on that server.
4. Press **Stop** when you're done. Otherwise it stops by itself after 8 hours, and when the runner stops.

In a git project, one session works in the project folder itself and each new session gets its own git
worktree; elsewhere every session works in the folder. Remote Control
needs a full claude.ai login: not an API key, and not a long-lived `claude setup-token` token. The first time
on each machine, Claude Code asks two questions that need a terminal; **Start** then says so and shows the
commands to run once, after which you press **Start** again ([guide 9](09-troubleshooting.md#claude-code)).

While it runs, anyone signed in to that Claude account can drive Claude in that folder and approve its
commands, and the hub doesn't see any of it. Stop servers you aren't using.

**By hand.** On a machine, in the project folder you want to work in, run:

```bash
claude remote-control --name "build-box myapp" --spawn worktree
```

For one of the other Claude accounts on that machine, use that account's own command instead, for example:

```bash
claude-work remote-control --name "build-box myapp (work)" --spawn worktree
```

Then:

- The first run asks you to trust the folder and enable Remote Control (answer `y`). It needs a real
  terminal for that.
- Leave it running. On Linux and macOS, run it inside `tmux` so it survives closing the window. On Windows,
  keep that terminal window open.
- Each server can serve up to 32 sessions. `--spawn worktree` gives each new session its own git worktree,
  so parallel sessions don't trip over each other.
- Sessions appear under the account that started them, in that account's Claude app. That's why one
  browser profile per account helps.

To make every interactive session reachable automatically, run `/config` inside Claude Code and turn on
**Enable Remote Control for all sessions**. Details:
[Remote Control docs](https://code.claude.com/docs/en/remote-control).

## Notifications on your phone

The hub can tell you when a task finishes or fails, and when a job waits for your approval. It sends a short
message to an address you choose: an [ntfy](https://ntfy.sh) topic, which the ntfy app shows on your phone,
or any address that takes a JSON POST.

**With ntfy:**

1. Pick a topic name that is hard to guess, for example `bullpen-` followed by 20 random letters. Anyone who
   knows the name can read your notifications.
2. Install the ntfy app on your phone (Android or iOS) and subscribe to that topic.
3. On the hub (inside its container: `pct enter <id>`), open its settings:

   ```bash
   nano /etc/bullpen/hub.env
   ```

4. Add a line with your topic's address, then save:

   ```
   HUB_NOTIFY_URL='https://ntfy.sh/bullpen-<your topic>'
   ```

5. Restart the hub:

   ```bash
   systemctl restart bullpen
   ```

With your own ntfy server, use its address instead. For a topic protected with an access token, also add
`HUB_NOTIFY_HEADERS='Authorization=Bearer tk_…'`. Headers need an `https://` address: with plain `http://` the
hub turns notifications off rather than send a token unencrypted.

**With a JSON webhook:** set `HUB_NOTIFY_URL` to the address and `HUB_NOTIFY_FORMAT='json'`. Each
notification is a POST like this:

```json
{"event": "done", "job_id": 12, "title": "#12 Nightly check", "status": "done", "url": "https://hub.example.com/#/job/12"}
```

**What a notification says.** The event (finished, failed, needs your approval), the job's number, a title
only when you typed one on New task (otherwise "Job #12"), the status, and a link to the job when
`HUB_PUBLIC_URL` is set. A council step or a subtask is named only by its number and its parent's, because an
agent may have written its title. Notifications never contain a prompt, an answer, a log line, or the
command a job wants to run.

- Finished and failed tasks and councils send one each; a council's steps don't. Cancelled jobs send none.
- A job that waits for your approval sends at most one notification every 5 minutes, however many questions
  it asks.
- `HUB_NOTIFY_EVENTS='approval'` sends only approvals; `HUB_NOTIFY_TITLES=0` never sends a title.
  [Guide 4](04-proxmox-hub.md#running-it) lists every setting.

## Your Google AI plan

Use it directly, in Google's own tools:

- the **Antigravity** desktop app, or `agy` in a terminal, on any of your machines. Install it from
  [antigravity.google/download](https://antigravity.google/download);
- **Antigravity Remote Control** at [antigravity.google.com](https://antigravity.google.com), to drive
  those sessions from a browser;
- the **Gemini app** for chat and research.

Antigravity on a machine still gets the shared notes, `/remember` and your MCP servers
([guide 10](10-shared-memory-and-mcp.md)). To bring Google into a debate the safe way, copy a council's result
into Antigravity or the Gemini app and ask it to find the holes.
[Guide 1](01-accounts-and-rules.md#google-why-a-google-ai-plan-cant-drive-the-agents) explains why the hub
doesn't automate this.

## Second opinions inside an interactive session

If you installed the add-on (the setup does by default), interactive sessions can ask the other model
directly:

- **In Claude Code:** "get a second opinion from Gemini on this diff" (or on this plan). Claude writes a
  request, runs `gemini` read only, then tells you which of Gemini's points it agrees with and which it
  rejects, and why. Ask for one of your other Claude accounts by name if you'd rather use that.
- **In Gemini CLI:** `/second-opinion the changes in src/importer`. Gemini asks Claude Code (the machine's
  `claude` account, read only) and weighs the reply.

## Optional: Happier, one app for every machine's sessions

[Happier](https://github.com/happier-dev/happier) (MIT, actively developed) shows live Claude Code
sessions from all your machines in one app (Android, desktop and web), and can start new sessions on
any machine. It runs a small daemon on each machine. You can use its hosted relay (end-to-end encrypted)
or self-host one (`happier relay host install`).

If you try it, keep to the rules from [guide 1](01-accounts-and-rules.md):

- **Sign in on each machine** with Claude Code's own login. Don't use Happier's "connected services",
  which store login tokens on the relay.
- **Leave account pools and "automatic switching" off.**
- **Don't connect its Antigravity integration** to your Google account.

To install, run this on Linux:

```bash
curl -fsSL https://happier.dev/install | bash
```

On Windows:

```powershell
iwr https://happier.dev/install.ps1 -useb | iex
```

Read the script first if you like; that's good practice for any `curl | bash` install. See
[docs.happier.dev](https://docs.happier.dev).

## Chat

| You want | Use | Paid by |
|---|---|---|
| Long conversations, projects, artifacts | claude.ai (one browser profile per account) | that account's Claude plan |
| Gemini chat, Deep Research | gemini.google.com | your Google AI plan |
| One chat app across all your API organizations and Gemini | LibreChat ([guide 7](07-api-gateway.md)) | per token, by organization |
| A quick question to any agent on any machine | hub → New task → project `chat` | that agent's account |
