# 10 · Shared memory, /remember and one central MCP list

You get two things from this guide:

1. **One memory for every agent.** Notes that every Claude account, Gemini CLI and Antigravity can read,
   on every machine. You add notes with `/remember`, from any of those tools or from the dashboard.
2. **One place to set up MCP servers.** You add a server once on the hub's **MCP** page, and every
   machine puts it into every agent's own settings.

Both work in your normal terminal sessions too, not only in hub jobs. The end-to-end test covers the
wiring.

> **Last checked:** November 2026, against each tool's documentation.

## How it works

The hub stores the notes and the MCP list. Each runner turns them into each tool's own settings files on
its machine, and redoes that within about 15 seconds of any change:

| What | Claude Code (every account) | Gemini CLI | Antigravity |
|---|---|---|---|
| **Pinned notes**, always in context | `@` import in each account's `CLAUDE.md` (`~/.claude/CLAUDE.md`, `~/.claude-<name>/CLAUDE.md`) | `@./bullpen-shared.md` import in `~/.gemini/GEMINI.md` | the same `GEMINI.md`, via Antigravity's `@[…](…)` include |
| **`remember` / `recall` tools** | MCP server `bullpen`, added with `claude mcp add-json -s user` per account | `bullpen` in `~/.gemini/settings.json` | `bullpen` in `~/.gemini/config/mcp_config.json` |
| **`/remember` command** | skill in `<config dir>/skills/remember/` | `/remember` and `/recall` in `~/.gemini/commands/` | skill in `~/.gemini/config/skills/remember/` |
| **Your central MCP servers** (through the hub: `/mcp/s/<name>`) | `claude mcp add-json -s user`, per account | `mcpServers` in `settings.json` | `mcpServers` in `mcp_config.json` |
| Fallback in any terminal | `bullpen remember …` / `bullpen recall …` | same | same |

**What the runner never does:**

- It never touches a server, skill or file you created yourself. It only changes things it marked as its
  own, and the machine's page in the dashboard lists anything it skipped.
- In `CLAUDE.md` and `GEMINI.md` it only writes between its `<!-- bullpen:begin -->` and
  `<!-- bullpen:end -->` lines.

To keep the hub out of a machine's agent settings, answer "no" to the question in `runner.py init`
(`manage_agent_settings`).

**The job tools at `/mcp/job`** are a second MCP server in the hub, `bullpenjob`, for running jobs only. You
don't add it anywhere, and the name `bullpenjob` (like `bullpen`) can't be used on the MCP page. The runner
hands it to each job that needs it, with that job's own short-lived token, and to no other session
([guide 2](02-architecture.md#job-tools-what-a-running-agent-can-ask-the-hub)).

## Shared memory

### Adding notes

You can add a note in any of these places:

- **Claude Code:** type `/remember The Windows machines keep their projects in C:\projects`, or just ask
  Claude to remember something for all your agents.
- **Gemini CLI:** `/remember …` (and `/recall …`).
- **Antigravity:** the `/remember` skill.
- **Any terminal:**
  `bullpen remember --tags windows "The Windows machines keep their projects in C:\projects"`.
- **The dashboard:** the **Memory** page.

Each note has a **scope**:

- `global`: for everyone (the default). This is the "shared account context": every Claude account, Gemini
  and Antigravity see it.
- `agent:<name>`: only that agent, e.g. `agent:claude-work` for notes that only matter to your work
  account. Every Claude agent has its own. Gemini agents on one machine share one settings folder
  (`~/.gemini`), so they all get the notes for `agent:gemini`, and a note for a second Gemini agent's own
  name doesn't reach it automatically (it can still find it with `recall`).
- `project:<name>`: found when an agent searches about that project.

**Pinned** notes (global or agent scope) are loaded into every session automatically. Keep them short:
every session on every account pays for them. The Memory page warns you above about 12,000 characters.
Unpinned notes cost nothing until an agent looks them up with `recall`. The agents are told to do that
before asking you something you may have told another agent.

**Only you can pin.** An agent's `/remember … and pin it` saves the note with a **pin requested** label.
You approve it with **Pin** on the Memory page. This is deliberate: a pinned note becomes standing
instructions for every agent on every machine, so a web page or repository an agent read can't sneak one
in. Project notes can't be pinned, because no agent loads them automatically. For the same reason the hub
neutralises `@` in pinned notes (a backslash before it), so a note can never make Claude or Gemini load a
file from disk.

**Don't put secrets in notes.** The skill tells agents to refuse.

### How it relates to each tool's own memory

- **Claude Code** has its own *auto memory* (per project, on that machine only). It also has no built-in
  `/remember`: the one you get comes from this setup. Saying "remember X" without the slash may still go to
  Claude's own auto memory. Use `/remember` or say "for all my agents" when you mean the shared one.
- **Gemini CLI** writes its own memories into `~/.gemini/GEMINI.md`, on that machine only. The runner leaves
  those alone.
- **Antigravity's** `/learn` writes rules on that machine. The runner leaves those alone too.
- **claude.ai** (web and mobile) has its own memory per account, with no API. It can still use *this*
  shared memory through a connector; see "claude.ai web and mobile" below.

## The central MCP list

Open the hub's **MCP** page and add a server once. You choose:

- **Remote (HTTP)**: a URL, and how agents reach it (below).
- **Local program (stdio)**: a command, arguments and environment. The program has to be installed on
  each machine that gets it. On Windows, `npx`-style commands are wrapped in `cmd /c` for Claude Code
  automatically.
- **Which agents** get it (Claude, Gemini CLI, Antigravity), and on **which platforms** (Linux, macOS,
  Windows). A machine gets exactly the servers ticked for its kind.

Machines pick it up on their next check-in. **Sync all machines now** pushes it immediately. Deleting
a server removes it everywhere on the next sync.

Names use lowercase letters, digits and `-` only. Gemini CLI breaks on underscores.

### Through the hub (recommended) or directly

For a remote server you pick one of two routes:

| | **Through the hub** (the default) | **Directly from each machine** |
|---|---|---|
| What each agent gets | `http://<hub>:8080/mcp/s/<name>` plus the hub's own MCP token | the server's real URL and headers |
| Where the server's key or sign-in lives | only on the hub | in every agent's settings file, on every machine, in plain text |
| OAuth sign-in | once, on the dashboard (next section) | in every agent on every machine |
| Changing a key | edit it on the MCP page; nothing to sync | every machine rewrites its settings on the next sync |
| Who talks to the server | the hub | each machine |

"Through the hub" is the hub acting as a relay. Agents still see the server's own tools under their own
names: Claude Code calls them `mcp__<name>__<tool>`, exactly as if it were connected directly, so
permission rules you write for a server keep working.

Use **directly** only when the server must be reached from the machine itself, for example a service that
only listens on one machine's network, or one you don't want going through the hub.

A few things to know about the hub route:

- **The Test button** asks the server for its tool list, from the hub, and shows what agents will get.
  The list shows when the hub last reached each server, and the last error if that failed.
- **New tools** a server adds appear within 5 minutes (the hub keeps the list that long), or straight
  away after you press **Test**. Agents that were already running may need a restart to see them; that's
  how they load MCP tools.
- **One tool call may take up to 2 minutes.** Raise `HUB_MCP_PROXY_TIMEOUT` in `hub.env` for slower
  servers.
- **Requests other than tool calls aren't relayed**: resources, prompts, and a server asking the agent a
  question mid-call (sampling, elicitation). Almost all MCP servers only use tools. Route a server that
  needs more than tools directly.
- The hub has to have the `mcp` Python package, which the installer adds. Without it the MCP page says
  so, and saving a server with **Through the hub** is refused: choose **Directly from each machine**.

### Signing in once with OAuth

Many hosted MCP servers (Notion, Linear, Atlassian, Sentry, …) want you to sign in with your account
rather than paste a key. For those:

1. On the MCP page, add the server with **Through the hub** and **OAuth sign-in, done once on the hub**.
   Leave the client ID empty unless step 3 applies.
2. Press **Sign in** next to it. A new tab opens on the provider's sign-in page. Sign in and approve.
   The tab ends on a hub page that says "Signed in". Close it; the MCP page now shows **signed in**.
3. Some services don't let apps register themselves (GitHub and Slack OAuth apps, for example). For
   those, the sign-in says so. Create an OAuth app in that service's developer settings, with the
   **callback address** the form shows (`https://<your hub>/oauth/callback`), and enter its client ID and
   secret in the form. **Send the secret** stays on "as HTTP Basic", which every provider must accept;
   switch it to "in the request body" only if the sign-in fails with `invalid_client` and the provider's
   docs say it wants the secret in the body. Press **Sign in** again.
4. If **Sign in** says the server "doesn't offer an OAuth sign-in", it wants a key instead: edit it and
   choose **API key or token in a header**.

From then on every agent on every machine uses that one sign-in. The hub renews the access token by
itself when it expires, once for all agents. You only sign in again if the service ends the sign-in (for
example, you removed the app in its settings, or it expires refresh tokens after some months). The MCP
page then shows **needs sign-in**, and agents get a message telling them to ask you to sign in on the
hub. If the service is only briefly unreachable or answers strangely, the hub keeps the sign-in, and
agents are told to try again later.

**Which address the sign-in returns to.** The provider sends your browser back to the hub, so this must be
an address your browser can open. Set `HUB_PUBLIC_URL` in `/etc/bullpen/hub.env` to the https address
you open the dashboard at, for example `https://hub.example.com`, then restart the hub. Without it, the
hub uses whatever address you opened the dashboard at. Many providers refuse plain `http://` addresses
other than `localhost`, so sign in from an https address. [Guide 3](03-cloudflare.md) shows three ways to get
one: Tailscale, Caddy, or Cloudflare. With `HUB_PUBLIC_URL` set, **Sign in** only
works on the dashboard opened at that address (the page tells you), because the sign-in has to finish in
the same browser and at the same address where it started.

**Changing a server's address or client ID** cancels a sign-in that's still open for it, and drops the
stored sign-in: press **Sign in** again afterwards.

**Sign out** forgets the tokens on the hub. The hub doesn't tell the provider (most MCP providers have no
way to), so if you want the provider to forget the hub too, remove the app in the provider's settings.

### How many sign-ins you need

| The server uses… | Do this | Sign-ins |
|---|---|---|
| **An API key or personal access token** (e.g. GitHub's MCP server with a PAT) | Add it **through the hub**, with the header | **Zero** after setup |
| **OAuth** (most hosted MCP servers) | Add it **through the hub** with OAuth, press **Sign in** once | **One** |
| **OAuth with an app you must create yourself** (some GitHub/Slack setups) | The same, plus the client ID and secret from your app | **One** |
| **An Anthropic-hosted connector** (Gmail, Google Calendar, …) | Add it on claude.ai once per Claude account. It then appears in Claude Code on every machine, when the account is signed in with `/login` | **One per Claude account**; Claude only |

If you add an OAuth server **directly** instead, every agent on every machine has to sign in on its own:
Claude needs `claude mcp login <name>` in each account on each machine. With ten machines and two accounts
each, that's twenty sign-ins for one server; through the hub it's one.

## Alternatives: an MCP gateway

You don't need these for the cases above. They are useful if you also want your servers in claude.ai on
the web and phone (Option A), or want a full-featured gateway with per-user access rules and its own admin
interface (Option B). Either way you add just the gateway to the MCP page, through the hub or directly.

### Option A: Cloudflare MCP server portal (no extra software, not open source)

If you already use Cloudflare ([guide 3](03-cloudflare.md#option-c-cloudflare-tunnel-and-access)), this is
the least work. A portal puts many MCP servers behind one URL. You
sign in to each upstream server once in the dashboard, and the portal shares that login with everyone you
allow. Steps, from [Cloudflare's docs](https://developers.cloudflare.com/cloudflare-one/access-controls/ai-controls/mcp-portals/):

1. Go to **Zero Trust → Access controls → MCP Portals → MCP servers → Add MCP server**. Enter the server's
   URL. Choose **Automatic** OAuth (DCR) and sign in once.
2. **Add server portal**. Pick a hostname, for example `mcp.example.com`, and select the servers.
   Turn **Require user auth** off so everyone uses your one login.
3. Create a **service token** (**Access controls → Service credentials**). Add a **Service Auth** policy
   with it to the portal, and to each server.
4. On the hub's MCP page, add one server called `portal`:
   - URL: `https://mcp.example.com/mcp`
   - headers: `CF-Access-Client-Id: …` and `CF-Access-Client-Secret: …`

Limits to know:

- tool calls time out after 60 seconds;
- only remote (HTTP) servers can be added;
- Cloudflare warns that the stored admin login can expire without notice, so sign in again in the dashboard
  when a server stops working;
- servers that need a manually created OAuth client can't share one login. Use Option B for those.

### Option B: IBM ContextForge (open source, Apache-2.0)

[ContextForge](https://github.com/IBM/mcp-context-forge) is a self-hosted MCP gateway. It signs in to
OAuth servers once (with automatic or manual clients), then stores the tokens encrypted and refreshes them.
It exposes everything as one "virtual server" URL that takes one bearer token.

It needs about 450 MB of RAM (its own sizing guide). Give it a container of its own, or raise the hub's
container to about 1.5 GB in Proxmox (**container → Resources → Memory**). Outline, from its docs:

```bash
pip install mcp-contextforge-gateway        # in a Python 3.12+ virtualenv
```

```bash
python3 -m mcpgateway.scripts.init_secrets  # prints JWT_SECRET_KEY and AUTH_ENCRYPTION_SECRET
```

```bash
DATABASE_URL=sqlite:////var/lib/contextforge/mcp.db JWT_SECRET_KEY=… AUTH_ENCRYPTION_SECRET=… \
  mcpgateway --host 0.0.0.0 --port 4444
```

Then:

1. In its admin UI (`http://<host>:4444/admin`), add each upstream server ("gateway") and complete its
   OAuth sign-in once.
2. Create a **virtual server** that bundles their tools.
3. Mint a long-lived token. `--exp` is in minutes; 525600 is a year:

   ```bash
   python3 -m mcpgateway.utils.create_jwt_token --username you@example.com --exp 525600 --secret <JWT_SECRET_KEY>
   ```

4. On the hub's MCP page, add one server called `gateway`:
   - URL: `http://<host>:4444/servers/<virtual-server-id>/mcp`
   - header: `Authorization: Bearer <that token>`

This repository doesn't install ContextForge for you. It's a separate project with its own upgrade cycle,
and its exact settings change between releases. Follow its install guide for the version you get. Check its
memory use on your container before relying on the figure above.

## claude.ai web and mobile (optional)

claude.ai's connectors are called from Anthropic's cloud, so the server has to be reachable on the public
internet with a sign-in claude.ai understands (OAuth). The cleanest route reuses Option A:

1. Make the hub's `/mcp` reachable through a Cloudflare tunnel ([guide 3](03-cloudflare.md)). With the
   tunnel from that guide it already is, at `https://hub.example.com/mcp`, behind Access.
2. Create a service token and add a **Service Auth** policy with it to the `Agent Bullpen` Access application.
3. In an MCP portal, add the hub as a server:
   - URL: `https://hub.example.com/mcp`
   - auth: **bearer**
   - headers: `Authorization: Bearer <hub MCP token>`, `CF-Access-Client-Id: …`, `CF-Access-Client-Secret: …`

   The hub MCP token is on the MCP page, under **Show**.
4. In the portal's Managed OAuth settings, add `https://claude.ai/api/mcp/auth_callback` to the allowed
   redirect URIs.
5. In claude.ai, for **each** account, go to **Customize → Connectors → Add custom connector**, enter
   `https://mcp.example.com/mcp`, and pick **Register automatically**.

The memory tools then work in claude.ai chats, the desktop app and the Android app. Claude Code sessions
signed in with `/login` would get the connector too. Because the runner already gives Claude Code the hub
directly, you'd see the tools twice there; turn the connector off for Claude Code in `/mcp` if that bothers
you.

**Alternative without a portal.**

1. Turn on **Managed OAuth** on the `Agent Bullpen` Access application, and add
   `https://claude.ai/api/mcp/auth_callback` to its allowed redirect URIs.
2. Add these three lines to `/etc/bullpen/hub.env`:

   ```
   HUB_MCP_TRUST_ACCESS='1'
   HUB_CF_ACCESS_TEAM='<team-name>.cloudflareaccess.com'
   HUB_CF_ACCESS_AUD='<the application's AUD tag>'
   ```

   The AUD tag is in the application's settings under **Additional settings** (or **Overview**).
3. Restart the hub:

   ```bash
   systemctl restart bullpen
   ```

The hub then accepts `/mcp` requests carrying a valid, signed Access login: it checks the signature,
audience, issuer and expiry against your team's public keys. Without both settings it keeps requiring its
own token. An Access login only reaches the shared memory at `/mcp`. The servers the hub relays
(`/mcp/s/<name>`) always need the hub's own token, because they act with your keys and sign-ins. Also
turn on **Protect with Access** for the hub's tunnel route, so cloudflared checks the login too.

## Security notes

- **The hub MCP token is the key to your shared memory and to every server routed through the hub.** Anyone
  holding it can call those servers' tools with your keys and sign-ins. It sits in each agent's settings on
  each machine, so keep the hub on your LAN, a VPN, or behind a login wall such as Cloudflare Access, and
  rotate the token if a machine is lost (below).
- **Keys and sign-ins for servers routed through the hub stay on the hub**, in its database
  (`/var/lib/bullpen/hub.db`). They never reach the machines. Back the database up like a password store.
- **If a machine is lost**, rotating the MCP token alone isn't enough, because a runner token can fetch the
  new one. Do all of this:
  1. **Forget** the lost machine on the Overview page. That revokes its own runner token straight away.
     If it used the shared token, also change `HUB_RUNNER_TOKEN` in
     `/etc/bullpen/hub.env`, restart the hub, and give each remaining machine its own token with **Add a
     machine** ([guide 5](05-linux-and-macos-machines.md#machines-set-up-with-the-shared-token)).
  2. Rotate the MCP token on the MCP page. The remaining machines pick up the new one by themselves.
  3. Change the keys of servers you added **directly** (those were on the machine). Servers routed through
     the hub are safe once steps 1 and 2 are done.
  4. Sign the lost machine's agent accounts out on the providers' websites: their logins were on it.
- **Keys of servers added directly reach every machine** (see above). Prefer the hub route.
- **Job tokens open the job tools of one job, and only while it runs.** Only a job that asks you before
  commands or hands out subtasks gets one. With it, an agent can read its own job's details, ask you a
  question on the dashboard (never answer one), or start subtasks within the limits you set for that task.
  It stops working when you press Cancel, when the job ends, 5 minutes after the job's time limit, or when
  the machine stops checking in. The hub keeps only a fingerprint of it. On the machine it sits in a file
  that the runner deletes when the job ends; until then, any program running as the runner's user there
  can read it, other jobs included.
- **Notes are visible to every agent**, including anything they fetch from the web. Don't store anything
  you'd mind an agent repeating.
- **The MCP page can run programs on every machine.** A "local program (stdio)" server is started by every
  agent on every machine that gets it. Treat dashboard access as the keys to every machine: keep it off the
  open internet ([guide 3](03-cloudflare.md)) and use a strong password.
