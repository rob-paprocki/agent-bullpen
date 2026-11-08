# 7 · The API side: LiteLLM, LibreChat and the Usage page (optional)

Your Anthropic Console organizations are the **pay-per-token** side. This guide puts any number of them,
plus an optional Gemini API key, behind one gateway:

- **LiteLLM**: one address that speaks the OpenAI and Anthropic APIs, holds each organization's key, and
  tracks spend per key in its dashboard.
- **LibreChat** (MIT): a chat app (conversations, file uploads, presets) that goes through LiteLLM.
- **The hub's Usage page**: last-30-days spend per Console organization, from Anthropic's Admin API.

**Cost:** these are API charges, billed to the organization whose key a request uses. Some Claude plans
include a monthly API credit for an organization you link to the plan, and this is where that credit gets
used ([guide 1](01-accounts-and-rules.md#what-you-might-have)). Set budgets in LiteLLM if you want a hard
stop.

**Where it runs:** in Docker, on any Linux machine that is on most of the time and has the memory to spare:
about 1–2 GB for LiteLLM, and about 3–4 GB with LibreChat. The hub's own container is too small for it. On
Debian, Ubuntu and Kali the script installs Docker for you; elsewhere, install Docker Engine and its Compose
plugin first.

## Step 1: Create the keys

1. In [platform.claude.com](https://platform.claude.com), switch to the first organization, open
   **API keys → Create key**, and copy it. Do the same in each other organization you want in the gateway.
2. Think of a short name for each organization, such as `work`, `personal` or `client-eu`. It becomes the
   start of its model names. Use lowercase letters, digits and hyphens. Names starting with `claude`, and
   `gemini`, are taken.
3. Optional: create a Gemini API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## Step 2: Run the script

On the machine that will run the gateway, as your normal user, get a copy of the repository and run the
script:

```bash
git clone https://github.com/rob-paprocki/agent-bullpen.git ~/agent-bullpen
```

```bash
cd ~/agent-bullpen
```

```bash
bash scripts/linux/setup-api-gateway.sh
```

It does three things:

1. **Docker.** Installs `docker.io` and Compose from your distribution if they're missing.
2. **LiteLLM.** Asks for a dashboard username and password, then for each organization: its short name and
   its API key. If you add more than one, it asks which one pays for plain model names (see below). Then it
   offers to add a Gemini key. It generates the internal secrets, writes `~/bullpen-stack/litellm/.env`
   (readable only by you) and `config.yaml`, and starts LiteLLM and Postgres.
3. **LibreChat** (optional). Downloads the latest LibreChat release, generates its secrets, creates a
   dedicated LiteLLM key for it (so its spend shows separately), writes its model list from what the gateway
   offers, and starts it.

Then:

- **LiteLLM dashboard:** `http://<gateway-ip>:4000/ui`. Here you can create keys for other tools, set monthly
  budgets and see spend per key and per model.
- **LibreChat:** `http://<gateway-ip>:3080`. Register an account; the **first account becomes the admin**.
  Then close sign-ups, as the script tells you. (The commands here say `sudo docker compose`; on Debian 12 the
  setup uses the older `sudo docker-compose` instead, and its last lines tell you which one your machine has.)

  ```bash
  cd ~/bullpen-stack/LibreChat && sed -i 's/^ALLOW_REGISTRATION=true/ALLOW_REGISTRATION=false/' .env && sudo docker compose up -d
  ```

## Model names: choosing which organization pays

Every model name says who pays for it:

| Model name | Paid by |
|---|---|
| `<organization>/<model-id>`, for example `work/<model-id>` | that organization's key |
| plain Anthropic model ids, starting with `claude-` | the **default organization** (`DEFAULT_ORG` in `.env`); these exist for Claude Code through the gateway (below) |
| `gemini/<model-id>` | your Gemini API key |

The model ids are Anthropic's own, from its [models page](https://platform.claude.com/docs/en/about-claude/models).
The routes are patterns, so a new model works as soon as Anthropic releases it, without editing anything.
LibreChat asks the gateway for the current list each time it starts.

## Adding or changing an organization later

Run the script again:

```bash
bash ~/agent-bullpen/scripts/linux/setup-api-gateway.sh
```

It shows the organizations it already has, offers to add another, asks which one pays for plain model names,
and restarts LiteLLM if anything changed. Your other settings are kept.

- **To replace a key**, add the organization again with the same name; the script asks before replacing it.
- **To remove an organization**, delete its `ANTHROPIC_KEY_<NAME>=` line from `~/bullpen-stack/litellm/.env`,
  then run the script again.

**Your own changes.** The script writes `config.yaml` and LibreChat's `librechat.yaml`, and their first two
lines say so. To change one by hand (for example to add another provider), delete those two lines. From
then on the script leaves that file alone, and says so each time it runs.

**Pinning LiteLLM's version.** The gateway uses LiteLLM's `main-stable` image. To stay on one version, add
`LITELLM_TAG=<tag>` to `~/bullpen-stack/litellm/.env`, with a tag from LiteLLM's releases page, and run the
script again.

## Reaching them from elsewhere

To use the gateway and LibreChat from outside your network, use a VPN, or publish them through Cloudflare:
add `llm.example.com → http://<gateway-ip>:4000` and `chat.example.com → http://<gateway-ip>:3080` as routes
on your tunnel, and add both hostnames to your Access application
([guide 3, steps 4–5](03-cloudflare.md#step-4-publish-the-hub-and-later-the-api-apps)). If LibreChat should
use that address, set `DOMAIN_CLIENT` and `DOMAIN_SERVER` in `~/bullpen-stack/LibreChat/.env` to
`https://chat.example.com`, then run:

```bash
cd ~/bullpen-stack/LibreChat && sudo docker compose up -d
```

## Using the API from the hub: a Claude agent with an API key

Sometimes you'll want a job billed to an organization rather than to a subscription, for example when the
job belongs to that organization's work anyway. On any machine, run the runner's setup questions:

```bash
python3 ~/.bullpen/runner/runner.py init
```

On Windows, use `py -3` instead of `python3`, and `$HOME\.bullpen\runner\runner.py`.

Answer **yes** to "Add a Claude agent?", give it a name (for example `api`, which makes the agent
`claude-api`), choose **API key** as the way it signs in, then give:

- **Gateway URL:** `http://<gateway-ip>:4000`, or leave it blank to call Anthropic directly;
- **API key:** a LiteLLM key (create one in its dashboard, so the spend is tracked), or a Console key.

Through the gateway, Claude Code asks for plain model ids, so the default organization pays. For an agent
that bills a different organization, leave the gateway URL blank and give it that organization's own Console
key.

That agent keeps its own settings folder (`~/.claude-<name>`), so it never touches your subscription logins.

## Usage panel: API spend per organization

The hub's **Usage** page can show each organization's spend over the last 30 days:

1. In each Console organization, an admin creates an **Admin API key** at
   [platform.claude.com/settings/admin-keys](https://platform.claude.com/settings/admin-keys). It starts
   with `sk-ant-admin01-`. Admin keys only exist for organizations, not individual accounts.
2. On the hub, edit `/etc/bullpen/hub.env`. List one `Name=key` pair per organization, as many as you
   have, separated by `;`. The names are what the Usage page shows:

   ```
   HUB_ANTHROPIC_ADMIN_KEYS='Work=sk-ant-admin01-…;Personal=sk-ant-admin01-…'
   HUB_LITELLM_UI_URL='http://<gateway-ip>:4000/ui'
   ```

   Two more optional settings change the page's links:

   ```
   HUB_USAGE_PROVIDERS='anthropic,google'
   HUB_LINKS='Team wiki=https://wiki.example.com;Billing=https://example.com/billing'
   ```

   `HUB_USAGE_PROVIDERS` chooses whose usage pages are linked, in that order (`anthropic`, `google`; `none` for
   neither). `HUB_LINKS` adds your own links at the top.

   If an organization holds your plan's monthly API credits, its name here is also what a machine's credits
   agent's account label must say ([guide 5](05-linux-and-macos-machines.md#a-credits-agent)), and the Usage
   page can track the credits ([guide 8](08-daily-use.md#platform-credits)).
3. Restart the hub:

   ```bash
   systemctl restart bullpen
   ```

Admin keys can read (and manage) the whole organization, so keep them only in that file.

The hub fetches the figures again every 15 minutes. **Refresh** under the spend cards fetches them now, at
most once a minute, which is how often Anthropic says its cost API can be polled for sustained use
([Usage and Cost API](https://platform.claude.com/docs/en/manage-claude/usage-cost-api), last checked
5 November 2026).

Your Claude plans' limits aren't API spend, so this panel doesn't show them. The same Usage page shows them
in its own section, read from Claude Code itself ([guide 8](08-daily-use.md#usage)). Google AI plans have no
readout the hub can use; the page links to Google's own pages instead.
