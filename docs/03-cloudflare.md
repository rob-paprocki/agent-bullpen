# 3 · Reaching the hub from outside (optional): VPN, TLS or Cloudflare

At home, your browser and your machines reach the hub directly on your network, at an address like
`http://192.168.100.20:8080`. That's all most setups need. This guide is for when you also want one of these:

- the dashboard from outside your home, for example on your phone;
- https on your own network, because you don't trust every device on it;
- https for MCP servers with an OAuth sign-in, because many providers refuse plain `http://` addresses
  other than `localhost` ([guide 10](10-shared-memory-and-mcp.md#signing-in-once-with-oauth));
- a machine that isn't on your home network, such as a laptop on the road.

Never open a port on your router to the hub. Use one of the options below instead.

## What plain HTTP on your network means

The hub serves plain HTTP. Without one of the options below, these cross your network **unencrypted**:

- the dashboard password when you log in, and the session cookie after that;
- each machine's runner token, sent with every check-in (every few seconds);
- the hub's MCP token, sent by every agent that uses the shared memory or a server through the hub;
- every prompt, answer and log line.

Anyone who can watch your network traffic can read them, and with the password or the cookie they can control
every machine. That includes a guest on your Wi-Fi or a compromised device such as a cheap camera. On a home
network with only your own devices, many people accept that trade-off. On a shared or office network, don't:
use a VPN or TLS.

## Choose an option

| Option | What you get | Needs | Good when |
|---|---|---|---|
| **LAN only** (nothing to do) | the dashboard and the machines on your own network, over plain HTTP | nothing | everything is at home, on a network you trust |
| **[A VPN](#option-a-a-vpn-tailscale-or-wireguard)**: Tailscale or WireGuard | the hub from anywhere, as if you were at home, encrypted between your devices | a free Tailscale account, or a router with a WireGuard server | you want it on your phone or laptop without a public address |
| **[Caddy with TLS](#option-b-https-on-your-network-with-caddy)** | https on your own network | 15 minutes, and trusting one certificate on each device | you want https at home, but not a VPN |
| **[Cloudflare Tunnel and Access](#option-c-cloudflare-tunnel-and-access)** | `https://hub.example.com` from anywhere, behind a login page, with no open ports | a domain on Cloudflare (the free plan is enough) | you want a normal https address, for example for OAuth sign-ins |

You can combine them. For example, keep the machines on the LAN or a VPN, and open the dashboard through
Cloudflare.

**Which address the machines use.** `HUB_RUNNER_URL` in `/etc/bullpen/hub.env` is the address **Add a
machine** puts in its commands ([guide 4](04-proxmox-hub.md)). Machines check in every few seconds, so it
should be a direct address they can always reach: the LAN address, or the hub's VPN address if every machine
is on the VPN. Don't use an address behind Cloudflare Access.

## Option A: a VPN (Tailscale or WireGuard)

A VPN puts your phone, laptop and other machines on a private network with the hub, wherever they are. The
VPN encrypts everything between them, so plain `http://` inside it is fine.

### Tailscale

[Tailscale](https://tailscale.com) is the least work. It has a free plan for personal use. Most of its
client software is open source; its coordination service is not ([Headscale](https://github.com/juanfont/headscale)
is an open-source replacement for that part).

1. **Let the container use a VPN device.** On the Proxmox host, open the container's settings file (replace
   `<container-id>`):

   ```bash
   nano /etc/pve/lxc/<container-id>.conf
   ```

   Add these two lines at the end, save, then restart the container:

   ```
   lxc.cgroup2.devices.allow: c 10:200 rwm
   lxc.mount.entry: /dev/net/tun dev/net/tun none bind,create=file
   ```

   ```bash
   pct reboot <container-id>
   ```

   Skip this step if the hub isn't in a Proxmox container.
2. **Install Tailscale on the hub.** Inside the container (`pct enter <container-id>`), run:

   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   ```

   ```bash
   tailscale up
   ```

   It prints a link. Open it and sign in to your Tailscale account.
3. **Install Tailscale on your phone, laptop and any machine** that should reach the hub from outside, and
   sign in with the same account.
4. **Open the hub** at its Tailscale address: `http://<hub's Tailscale IP>:8080`. The Tailscale app shows
   the IP (it starts with `100.`).
5. **Optional: a real https address.** In the Tailscale admin console, under **DNS**, turn on **MagicDNS** and
   **HTTPS Certificates**. Then, in the container:

   ```bash
   tailscale serve --bg 8080
   ```

   The hub is now also at `https://<container-name>.<your-tailnet>.ts.net`, with a certificate every browser
   trusts. Set `HUB_PUBLIC_URL` to that address in `/etc/bullpen/hub.env` if you want OAuth sign-ins to
   use it, then run `systemctl restart bullpen`.

A machine that is sometimes away from home can use the hub's Tailscale address. Set
`"hub_url"` in its `~/.bullpen/runner.json` to `http://<hub's Tailscale IP>:8080`, then restart its runner.

### WireGuard on your router

Many routers and firewalls (OPNsense, pfSense, OpenWrt, UniFi, FRITZ!Box and others) have a built-in
WireGuard VPN server. Turn it on in the router's settings, and add your phone and laptop as clients with the
official WireGuard app. Once they're connected, the hub's normal LAN address works from anywhere. The steps depend on
the router, so follow its manual.

## Option B: https on your network with Caddy

[Caddy](https://caddyserver.com) (Apache-2.0) is a web server that handles certificates by itself. Here it
runs next to the hub and gives it an https address on your network, signed by Caddy's own small certificate
authority. Each device then has to trust that authority once.

1. Inside the hub's container (or on the hub machine), install Caddy. Debian 12 and newer have it:

   ```bash
   apt install -y caddy
   ```

2. Replace `/etc/caddy/Caddyfile` with this, using the hub's own IP address:

   ```
   https://192.168.100.20 {
       tls internal
       reverse_proxy 127.0.0.1:8080
   }
   ```

3. Reload Caddy:

   ```bash
   systemctl reload caddy
   ```

4. Copy Caddy's root certificate to each device you browse from, and mark it as trusted. On Windows, open the
   file, then choose **Install Certificate**, **Local Machine** and **Trusted Root Certification Authorities**.
   On macOS, open it in **Keychain Access** and set it to **Always Trust**. On Android and iOS, install it as
   a CA certificate in the security settings. The file is:

   ```
   /var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
   ```

5. Open `https://192.168.100.20`. Once that works, tell the hub its https address. In
   `/etc/bullpen/hub.env` set:

   ```
   HUB_PUBLIC_URL='https://192.168.100.20'
   HUB_COOKIE_SECURE='1'
   ```

   then run `systemctl restart bullpen`. With `HUB_COOKIE_SECURE='1'`, logging in over plain http stops
   working, so only set it once you always use the https address.

Port 8080 still answers over plain http, and the machines keep using it. To encrypt their traffic too, either
use a VPN for them, or trust the root certificate on each machine as well and change `"hub_url"` in its
`runner.json` to the https address. On Linux and Windows the runner uses the system's certificate store. On
macOS, how Python finds certificates depends on how it was installed, so the VPN is the easier route there.

**Already have a reverse proxy** (Caddy, Traefik, nginx Proxy Manager) with real certificates? Point it at
`http://<hub-ip>:8080`, then set `HUB_PUBLIC_URL` and `HUB_COOKIE_SECURE` as in step 5.

## Option C: Cloudflare Tunnel and Access

When you finish, `https://hub.example.com` works from anywhere, behind a login only you can pass, and your
home network keeps every port closed.

- **Cloudflare Tunnel** is a small program (`cloudflared`) inside the hub container. It connects *out* to
  Cloudflare, and Cloudflare sends visitors for your hostnames back down that connection.
- **Cloudflare Access** puts a login page in front of those hostnames. Only the e-mail addresses you list
  get through.

Both are included in the free plan, and you need a domain whose DNS is on Cloudflare. Cloudflare's services
are not open source, and your traffic passes through Cloudflare's servers, which decrypt it before sending it
on. Dashboard paths below are as of November 2026; Cloudflare renames things occasionally.

> Replace `example.com` with your domain throughout.

### Step 1: Turn on Zero Trust (one time)

1. Log in at [dash.cloudflare.com](https://dash.cloudflare.com) and open **Zero Trust** in the left menu.
2. If it's your first time, pick a **team name** (for example `my-home`) and the **Free** plan. Your login
   page will live at `https://<team-name>.cloudflareaccess.com`.

### Step 2: Choose how you log in

Go to **Zero Trust → Integrations → Identity providers → Add new identity provider** and pick one.

- **One-time PIN** is the quickest. Cloudflare e-mails you a code each time you log in.
- **Google** lets you log in with your Google account. It needs a free OAuth client in Google Cloud:
  1. In [console.cloud.google.com](https://console.cloud.google.com), create a project (any name).
  2. Go to **APIs & Services → OAuth consent screen**, set the audience to **External**, and add yourself
     as a test user.
  3. Go to **Credentials → Create credentials → OAuth client ID → Web application**, and set:
     - Authorized JavaScript origin: `https://<team-name>.cloudflareaccess.com`
     - Authorized redirect URI: `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/callback`
  4. Copy the Client ID and Client secret into Cloudflare's **Google** identity provider form, then select
     **Test**.
- **Cloudflare** lets you log in with your Cloudflare account. Turn on **Restrict to account members**.

You can enable more than one: the login page then offers a choice.

### Step 3: Create the tunnel

If you gave the hub installer a tunnel token, the tunnel already exists and is connected. Skip to step 4.

Otherwise:

1. Go to **Networking → Tunnels → Create a tunnel**. Name it `home` and choose **Cloudflared**.
2. Choose **Debian**. The page shows an install command that ends in a long token starting with `ey…`.
   Copy only the token.
3. On the hub, run the hub installer again. It keeps your settings, offers to install cloudflared, and asks
   for the token. In a Proxmox container, enter it first with `pct enter <container-id>`; on any other
   machine, put `sudo` in front:

   ```bash
   bash /opt/bullpen/src/scripts/linux/install-hub.sh
   ```

4. Back in the Cloudflare dashboard, wait for the tunnel to show **Healthy**, then select **Continue**.

### Step 4: Publish the hub (and later the API apps)

Open your tunnel, go to the **Routes** tab, select **Add route → Published application**, and add:

| Subdomain | Domain | Service URL | When |
|---|---|---|---|
| `hub` | example.com | `http://localhost:8080` | now |
| `llm` | example.com | `http://<gateway-ip>:4000` | after [guide 7](07-api-gateway.md) (LiteLLM) |
| `chat` | example.com | `http://<gateway-ip>:3080` | after guide 7 (LibreChat) |

`localhost` works for the hub because cloudflared runs on the same machine. The gateway entries reach across
your LAN to the machine that runs the gateway, so give that machine a fixed IP or a DHCP reservation.

> **Do step 5 right away.** Until an Access application covers a hostname, anyone who guesses it reaches
> your hub's password page.

### Step 5: Protect it with Access

1. Go to **Zero Trust → Access controls → Applications → Add an application → Self-hosted**.
2. Name it `Agent Bullpen`.
3. Under **Public hostnames**, add `hub.example.com`, and later `llm.example.com` and `chat.example.com`.
   One application can cover several hostnames.
4. Set **Session duration** to something comfortable, such as 24 hours or 1 week.
5. Add a policy:

   | Action | Rule type | Selector | Value |
   |---|---|---|---|
   | Allow | Include | Emails | your e-mail address |

6. Under login methods, tick the identity provider(s) you set up in step 2, then save.

To test it, open `https://hub.example.com` in a private window. You should see Cloudflare's login first,
then the hub's own password box.

Then tell the hub its public address, so OAuth sign-ins for MCP servers return there. Inside the container,
set this in `/etc/bullpen/hub.env`:

```
HUB_PUBLIC_URL='https://hub.example.com'
```

Once you always open the dashboard at the https address, you can also make the hub's login cookie
https-only with `HUB_COOKIE_SECURE='1'` in the same file. With `1`, logging in over plain http stops
working. Leave it at `0` if you also open the hub over plain `http://<ip>:8080` at home. After either
change, run:

```bash
systemctl restart bullpen
```

### Step 6 (optional): Machines that aren't on your home network

Machines at home talk to the hub over the LAN (`HUB_RUNNER_URL`) and never touch Cloudflare. A machine
elsewhere, such as a laptop on the road, can't reach that address. The VPN ([option A](#option-a-a-vpn-tailscale-or-wireguard))
is the simpler fix. To use Cloudflare instead, give the machine a **service token** so it can get through Access
without a browser:

1. Add the machine while it's at home, with **Add a machine** as usual. (The one-line command can't get
   through Access.)
2. Go to **Zero Trust → Access controls → Service credentials → Service Tokens → Create Service Token**,
   and copy the Client ID and Client Secret.
3. Edit the `Agent Bullpen` application and add a second policy. Set **Action** to **Service Auth**,
   **Include** to **Service Token**, and pick that token.
4. In that machine's `~/.bullpen/runner.json`, set:

   ```json
   "hub_url": "https://hub.example.com",
   "cf_access_client_id": "<Client ID>",
   "cf_access_client_secret": "<Client Secret>"
   ```

5. Restart its runner.

### What about Workers?

Nothing in this setup needs Workers. The reasons are in [guide 2](02-architecture.md#why-not-cloudflare-workers-for-the-hub).
