# 1 · Your accounts, what each one pays for, and the rules

Read this before installing anything. It explains why the design looks the way it does, and it keeps your
accounts safe. These rules change often, so check the linked pages again now and then.

> **Last checked:** 5 November 2026, against the providers' own pages (platform credits: November 2026).

## What you might have

Agent Bullpen works with any mix of these. You need at least one of the first two to run Claude Code, or a
Gemini API key to run Gemini CLI.

| Account | What it is | What it pays for | In the hub |
|---|---|---|---|
| **A Claude subscription** (Pro, Max, Ultra, Team or Enterprise) | a monthly plan, one per person | claude.ai (web, desktop, mobile) **and** Claude Code, under the plan's usage limits | a Claude agent: `claude` for the first account on a machine, `claude-<name>` for each other one |
| **An Anthropic Console organization** | the Anthropic API, pay per token | anything that uses an API key: Claude Code with a key, LiteLLM, LibreChat | a Claude agent with an API key, or the gateway ([guide 7](07-api-gateway.md)) |
| **A Gemini API key** (Google AI Studio, paid tier) | the Gemini API, pay per token | Gemini CLI, LiteLLM | a Gemini agent: `gemini`, `gemini-<name>` |
| **A Google AI plan** (for example Google AI Pro) | Google One's AI plan | the Gemini app, **Antigravity** (desktop app, `agy` CLI, IDE extensions), Jules | not driven by the hub (see below); Antigravity gets the shared notes and MCP servers |

You can have any number of each: several Claude accounts on one machine, several Console organizations in
the gateway, and so on.

Some Claude plans also come with a **monthly API credit**, paid into a Console organization you link to the
plan (you claim it on claude.ai). Requests with that organization's API key use the credit first. The credit
**can't** be used inside Claude Code's subscription mode or in claude.ai. Source:
[Monthly API credits](https://support.claude.com/en/articles/17154008). The hub can use this credit for
background jobs; see [Platform credits](#platform-credits-the-api-credit-in-your-plan) below.

## Anthropic: what is allowed

From Anthropic's [Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance):

> OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Ultra, Team, and
> Enterprise subscription plans and is designed to support ordinary use of Claude Code and other native
> Anthropic applications.

> Anthropic does not permit third-party developers to offer Claude.ai login into their own applications,
> or to route requests through Free, Pro, Max, or Ultra plan credentials on behalf of their users. Moreover,
> developers may not collect, store, or intermediate Claude.ai credentials or session tokens.

The same page says this does not prevent "an end user from signing in to the **unmodified Claude Code
binary** with their own Claude subscription". It also says: "Advertised usage limits for Pro, Max, and Ultra
plans assume ordinary, individual usage of Claude Code and the Agent SDK." Anthropic's Help Center
([Agent SDK article](https://support.claude.com/en/articles/15036540), update of 7 Oct 2026) confirms that
"when you're signed in with your Claude plan, the Claude Agent SDK and `claude -p` still draw from your
subscription limits."

**What that means for this setup**

- **Allowed:** the runner starts the official `claude` program on your own machine, signed in through
  Anthropic's own login, exactly as you would in a terminal. The hub never sees or stores your login.
- **Allowed:** the hub uses `claude -p` (headless mode) for jobs. That's documented and draws on your plan.
- **Councils count as usage like any other.** A three-way debate with a judge is seven agent runs, and a
  revise round makes it ten. Use them for questions that are worth it, and watch your limits.
- **Not allowed:** "subscription to API" proxies (CLIProxyAPI, claude-code-proxy and similar) that take your
  claude.ai login and expose it as an API. That is exactly what the rules forbid. Agent Bullpen doesn't do
  this and has no option for it.
- **Not allowed:** running the hub so that other people use your subscription. Each person signs in to
  their own accounts, on their own machines.
- **Avoid:** third-party services that store your Claude login tokens. If you use Happier (an optional
  extra, [guide 8](08-daily-use.md)), keep its "connected services" feature off and sign in on each machine
  instead.
- **Allowed:** **Check limits** on the dashboard ([guide 8](08-daily-use.md#usage)) runs Claude Code's own
  `/usage` command in print mode on the machine, with that agent's own login, as you would in a terminal.
  `/usage` is a documented command ([Commands](https://code.claude.com/docs/en/commands)), and the
  structured copy of its report that the hub reads first is documented as experimental
  ([Agent SDK reference, `SDKUsageReport`](https://code.claude.com/docs/en/agent-sdk/typescript#sdkusagereport);
  last checked 6 November 2026). Neither the hub nor the runner reads your login, calls Anthropic's internal
  endpoints or opens claude.ai. Each agent and each account is checked at most once every 5 minutes.
- **Allowed:** live sessions (typing to a running job) and Remote Control are your own interactive use of
  Claude Code, and count against your plan's limits like any session. Remote Control is Anthropic's own
  feature and works only with a full claude.ai subscription login
  ([Remote Control docs](https://code.claude.com/docs/en/remote-control), last checked 6 November 2026). While
  it runs on a machine, anyone signed in to that Claude account can drive Claude in that folder, so keep the
  account to yourself.

### More than one Claude account

Anthropic's [Consumer Terms](https://www.anthropic.com/legal/consumer-terms) have no rule against one
person paying for more than one subscription, but they do forbid sharing an account with anyone else. The
[Usage Policy](https://www.anthropic.com/legal/aup), in the version taking effect 12 Nov 2026, forbids
creating multiple accounts "to avoid detection, circumvent product guardrails…" and "intentionally bypass
or interfere with … restrictions … established within our products". Usage limits are a product restriction.

This guide's reading, which isn't legal advice: accounts that each have their own purpose are fine, for
example a work account your employer pays for next to a personal one. **Using a second account only as
overflow when the first hits its limit is the pattern most likely to be read as circumventing.** So:

- give each account its own lane (for example `claude-work` for your employer's projects and `claude` for
  your own), and pick the account per job in the dashboard;
- the hub **does not** switch accounts by itself, and you should keep any tool's "account pool" or
  "auto-switch" feature turned off;
- if you're unsure about your case, ask Anthropic support. They're the only ones who can say for certain.

**Account labels and the Usage page.** You can give an agent an account label in `runner.json`
([guide 5](05-linux-and-macos-machines.md#account-labels)). The Usage page then joins the plan limits of
every agent with that label, on every machine, into one account:

- give an agent a label only when it is that very account, and the same label on every machine signed in to
  it. Two different accounts with one label would show as one;
- a label counts for tasks aimed at a tag and for subtasks only after you accept it on the machine's page
  ([guide 8](08-daily-use.md#a-machines-own-page)). Until then, and for an agent without a label, the hub
  treats the agent as an account of its own on that one machine;
- the Usage page shows each account's limits to help you plan your own work. The hub never chooses, holds
  back or moves work because of them;
- when an account reaches a limit, its jobs fail with a message that names the window and when it resets.
  Send the work again after the reset.

**Tasks for a tag and subtasks.** A task for "any machine tagged …" runs under one account, fixed when you
create it: the hub hands it only to a machine where that agent is the same account, and it waits rather than
run under another one. A task you allow to hand out subtasks
([guide 8](08-daily-use.md#new-task-one-prompt-one-agent)) may by default use any of your agents on the
machines you allow, your other Claude account and Gemini included. You can untick accounts and agents on
New task.

That default is for work that belongs to each account anyway, for example a review by Gemini, or a step in a
project that lives with your work account. Sending work to your other Claude account because the first one
is near its limit is the overflow pattern the rules above warn against. Don't ask an agent to do that, and
don't let a prompt talk it into it. The hub blocks it once a limit is hit: after a usage-limit error in a
task or in one of its subtasks, new subtasks of that task may use only the account that hit the limit. A
follow-up continues the task's conversation, so the rule carries over to it for a week, the longest plan
window.

**Usage limits aren't a reason to switch accounts; wait for the reset.**

### Platform credits: the API credit in your plan

Claude Max, Ultra and Team plans include monthly API credits in the Console organization linked to the plan
(Ultra: $200 a month per plan). What Anthropic says about them
([API credits for subscribers](https://platform.claude.com/docs/en/about-claude/api-credits-for-subscribers),
[Monthly API credits](https://support.claude.com/en/articles/17154008); last checked November 2026):

- they arrive at the start of each billing cycle and expire at its end; they don't roll over;
- they are spent before any credits you bought;
- when they run out, the API refuses requests until the next cycle, unless you have bought credits;
- `claude -p` uses them only when it runs with an API key from that organization.

The hub calls them **platform credits**. On each machine, you can name one agent that signs in with such an
API key as that machine's **credits agent** ([guide 5](05-linux-and-macos-machines.md#a-credits-agent)). You
can also turn on **Use platform credits first for background work** on the Usage page
([guide 8](08-daily-use.md#platform-credits)). With both set:

- council steps and subtasks named for a Claude agent that signs in with a subscription run on the same
  machine's credits agent while its credits last;
- a task you start that stops at a usage limit is run again once on the credits agent.

**Why this is allowed, and switching accounts isn't.** The credits agent is not a second subscription. It is
an API key, billed by the API (here, paid by the credit your plan includes), which is the documented way to
use Claude Code beyond a plan. Moving work from one subscription account to another to get around a limit is
the overflow pattern described above, and the hub never does it:

- the only automatic move is from a subscription agent to the API-key agent that machine's own
  `runner.json` names, and back to the agent that was named when the credits are short or used up;
- the hub refuses a credits agent that signs in with a subscription;
- the hub decides by the credits' estimated balance, never by a subscription's remaining usage;
- nothing moves until you turn the setting on, and the job page always says when a job ran on credits.

### One hub, several people

The dashboard has one password and no user accounts yet ([roadmap](../ROADMAP.md)). Everyone who can log in
can send jobs to every machine and every agent. So a hub is for one person, or for a team whose members each
sign in to **their own** accounts on **their own** machines and trust each other with all of them.

## Google: why a Google AI plan can't drive the agents

On 18 June 2026 Google moved consumer Gemini developer tools to Antigravity
([announcement](https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli)):

> On June 18, 2026, Gemini CLI and Gemini Code Assist IDE extensions will stop serving requests for Google
> AI Pro and Ultra, as well as those using it free of charge… Gemini CLI will remain accessible via paid
> Gemini and Gemini Enterprise Agent Platform API keys.

The replacement is **Antigravity CLI (`agy`)**. Its [FAQ](https://antigravity.google/docs/faq/) is
explicit about automation on consumer plans:

> Antigravity CLI headless mode (agy -p) is intended for direct scripting and CI workflows. Wrapping agy
> inside third-party coding agents or applications, or automating account rotation, is not supported under
> consumer plans.

> Using third-party software, tools, or services to access Antigravity is a violation of our Terms of
> Service … Such actions can result in suspension or termination of your account. To use a third-party
> coding agent with Gemini, we recommend using a Gemini Enterprise or Google AI Studio API key.

Google suspended accounts over this in early 2026, so treat it as enforced.

**What that means for this setup**

- **Gemini inside the hub** (councils, reviews, "ask Gemini" from Claude) uses **Gemini CLI with an AI
  Studio API key**. That's the route Google itself recommends. It's billed per token. Get a key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey), and link billing so you're on the paid
  tier: on the free tier Google uses your prompts to improve its products, and the free models are limited.
- **Your Google AI plan** stays yours to use directly:
  - the **Antigravity desktop app** and **`agy` in your own terminal**, on any of your machines;
  - **Antigravity Remote Control** at [antigravity.google.com](https://antigravity.google.com), Google's own
    "drive my sessions from a browser";
  - the **Gemini app** for chat.

  Antigravity's [models page](https://antigravity.google/docs/models/) also lists Claude models on paid
  Google AI plans, inside Google's own tool.
- **Agent Bullpen does not run `agy` for you**, and this guide advises against tools that do (several
  dashboards offer it). The runner can run any command-line tool, but this one puts your Google account at
  risk. The runner only adds the shared notes and MCP servers to Antigravity's settings, which Antigravity
  reads when you use it yourself.
- You can still bring Google into a debate by hand: paste a council's result into Antigravity or the Gemini
  app and ask it to find holes.

## Other agent programs

Agent Bullpen can run other coding agents as plain "text" agents ([guide 11](11-adding-agents.md)), and the
[roadmap](../ROADMAP.md) plans full support for Codex CLI, OpenCode and Qwen Code. The same rule applies to
each: the vendor's own program, signed in on your machine, used the way the vendor allows. Read the vendor's
automation rules before you rely on a subscription:

- **OpenAI (Codex CLI):** "The right way to authenticate automation is with an API key."
  ([Codex auth in CI/CD](https://developers.openai.com/codex/auth/ci-cd-auth))
- **Cursor:** "For automation, scripts, or CI environments, use API key authentication."
  ([Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication.md))
- **OpenCode:** don't use it with a Claude subscription. Anthropic prohibits plugins that do.

## At a glance: what can the hub use?

| Surface | Paid by | In the hub? | How you reach it otherwise |
|---|---|---|---|
| Claude Code on a subscription | that account's plan | yes: `claude`, `claude-<name>` | terminal, IDE, claude.ai/code (Remote Control) |
| Claude Code on an API key | a Console organization (per token, or your plan's monthly API credits) | yes: a Claude agent with a key, or through LiteLLM; one per machine can be its credits agent | any API tool, LibreChat |
| claude.ai chat | that account's plan | no (no API) | browser: one browser profile per Claude account |
| Gemini CLI with an AI Studio key | Google Cloud billing (per token) | yes: `gemini`, `gemini-<name>` | terminal |
| Antigravity / `agy` | your Google AI plan | no (see above); it gets the shared notes and MCP servers | Antigravity app, `agy`, antigravity.google.com |
| Gemini app | your Google AI plan | no | gemini.google.com, the mobile app |

## Further reading

- Anthropic: [Use Claude Code with your Pro, Max or Ultra plan](https://support.claude.com/en/articles/11145838),
  [Authentication](https://code.claude.com/docs/en/authentication) (several logins side by side with
  `CLAUDE_CONFIG_DIR`), [Headless mode](https://code.claude.com/docs/en/headless)
- Google: [Antigravity plans](https://antigravity.google/docs/plans/), [Gemini CLI authentication](https://geminicli.com/docs/get-started/authentication),
  [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
