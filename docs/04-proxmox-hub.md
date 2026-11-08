# 4 · Proxmox: the hub container

**Goal:** a small, always-on container on your Proxmox VE host that runs the Agent Bullpen hub and, if you
want, the Cloudflare Tunnel connector. One command sets it up.

> **Status:** the installer has been tested on Linux with stand-in commands for Proxmox
> (`tests/proxmox`), not yet on a real Proxmox VE host. Read what each step prints. If something goes
> wrong, see [troubleshooting](#troubleshooting) or open an issue on GitHub.

The installer looks and works like the community-scripts
[Proxmox VE Helper-Scripts](https://github.com/community-scripts/ProxmoxVE), so it may feel familiar. It is
our own code. Agent Bullpen is not affiliated with community-scripts or with Proxmox Server Solutions GmbH,
and the installer sends no telemetry. It only talks to GitHub (to download Agent Bullpen) and to the servers
Proxmox and Debian normally use.

Not on Proxmox? Skip to [Any Debian or Ubuntu machine](#any-debian-or-ubuntu-machine).

## What you need

- Proxmox VE 8 or 9 on an amd64 machine. (Proxmox doesn't support ARM officially. The installer runs on
  arm64 hosts but warns you, and it needs an arm64 Debian template.)
- Internet access from the host and from the new container: GitHub, the Debian mirrors and the Python
  package index (pypi.org).
- A few minutes. Most of the time goes to updating Debian inside the container.

### How big to make it

The hub itself uses about 100 MB of RAM, even with a couple of hundred machines checking in. The rest is
room for updates, logs and MCP servers that run through the hub.

| Your setup | CPU | RAM | Disk |
|---|---|---|---|
| Most setups (the default) | 1 core | 1024 MB | 6 GB |
| Dozens of machines or more, or several MCP servers through the hub | 2 cores | 2048 MB | 10 GB |
| A host that is short on memory | 1 core | 512 MB | 4 GB |

You can change CPU and RAM later in the Proxmox web interface (**container → Resources**), without
reinstalling.

**Short on memory?** On the host's **Shell** (web interface → your node → Shell), run `free -m` and look at
the `available` column. If you use ZFS, part of the "used" memory is its read cache (the ARC), which it
gives back when something needs it. Proxmox explains how to cap it under
[ZFS: Limit memory usage](https://pve.proxmox.com/wiki/ZFS_on_Linux#sysadmin_zfs_limit_memory_usage).
Only do that if you really are short on memory.

## Step 1: Run the installer

1. In the Proxmox web interface, open your node, then **Shell**. (An SSH session to the host as root works
   too.) Keep it open until the installer finishes.
2. Paste this and press Enter:

   ```bash
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/rob-paprocki/agent-bullpen/main/ct/bullpen.sh)"
   ```

3. Choose an option from the menu:

| Option | What it does |
|---|---|
| **Default Install** | Creates the container with the settings below. Asks nothing, except which storage to use when you have more than one. |
| **Advanced Install** | Asks every question, one screen at a time, with a **Back** button. |
| **My Defaults** | Uses the settings you saved after an earlier Advanced Install. |
| **Settings** | Shows, edits or deletes My Defaults. |

### Default Install

| Setting | Value |
|---|---|
| Container | unprivileged, Debian 13, 1 CPU core, 1024 MB RAM, 512 MB swap, 6 GB disk, starts on boot |
| ID and name | the next free container ID; hostname `bullpen` |
| Network | bridge `vmbr0`, address from DHCP, DNS from the host |
| Root login | no password: the Proxmox console logs you in automatically; SSH is off |
| Dashboard password | a new random one, shown once at the end |
| Port | 8080 |
| Address for machines | `http://<container IP>:8080` |
| Tunnel | none |
| Version | the newest release, checked against its published checksums |

### Advanced Install

Each screen has **Next** and **Back**. Leave a box blank to keep the default.

| Step | Question | Default | What it means |
|---|---|---|---|
| 1 | Container type | Unprivileged | Unprivileged is safer: root inside the container is not root on the host. |
| 2 | Root password | blank | Blank logs you in on the Proxmox console without a password. Needed for SSH with a password. |
| 3 | Container ID | next free | Any free number from 100 up. |
| 4 | Hostname | `bullpen` | The container's name on your network. Lowercase letters, digits and hyphens. |
| 5 | Disk size | 6 GB | See [How big to make it](#how-big-to-make-it). |
| 6 | CPU cores | 1 | |
| 7 | RAM | 1024 MiB | |
| 8 | Network bridge | `vmbr0` | The bridges on your host are listed. |
| 9 | IPv4 address | DHCP | Or a fixed address like `192.168.100.20/24`, then its gateway (usually your router). |
| 10 | VLAN tag | none | Only if your network uses VLANs. |
| 11 | DNS server | the host's | One or more addresses, separated by spaces. |
| 12 | SSH access | No | Yes asks for a public SSH key. Without a key, SSH needs the root password from step 2. |
| 13 | Verbose mode | No | Yes shows every command's output, which helps when something fails. |
| 14 | Dashboard password | generate one | You type it twice. If you leave it blank, a new one is shown at the end. |
| 15 | Hub port | 8080 | The port the dashboard listens on. |
| 16 | Address for machines | `http://<container IP>:<port>` | The address your other machines use to reach the hub. Usually the LAN address. If you use a fixed address or a DNS name, put it here. |
| 17 | Public address | none | Only if you open the dashboard through a tunnel or reverse proxy, e.g. `https://hub.example.com`. MCP servers with an OAuth sign-in send your browser back to it ([guide 10](10-shared-memory-and-mcp.md#signing-in-once-with-oauth)). |
| 18 | Cloudflare Tunnel | none | A tunnel token, to reach the hub from outside your network. See [guide 3, step 3](03-cloudflare.md#step-3-create-the-tunnel). You can add one later. |
| 19 | Release | newest | A release tag like `v1.0.0`, or `main` for the newest code. `main` is not a release, so it can't be checked against a checksum. |
| 20 | Confirm | | A summary of everything. **Create** starts; **Back** goes back. |

After the summary, the installer asks whether to save these settings as **My Defaults**. Passwords and
tokens are never saved.

## Step 2: What happens next

The installer prints one line per step. It:

1. checks for the newest release of Agent Bullpen;
2. downloads the Debian template if you don't have it;
3. creates and starts the container, and waits for its network;
4. inside the container: updates Debian, downloads the release, checks it against the release's
   `SHA256SUMS` file, and installs the hub (a Python environment, the settings file and a system service);
5. adds a login message, the `update` command and a note in the container's **Notes** panel.

At the end it shows:

```text
Completed successfully!
Agent Bullpen setup has been successfully initialized!
Access it using the following URL:
  http://192.168.100.20:8080
Dashboard password: (only when one was generated)
Add machines: open the dashboard, then Add a machine
```

The generated password is shown only this once. Write it down, or read it again later with
`pct exec <container-id> -- grep HUB_UI_PASSWORD /etc/bullpen/hub.env`.

If a step fails, the installer shows the command that failed and the end of its output, then offers to
remove the half-made container: `Remove broken container 105? (Y/n) [auto-remove in 60s]`. Answer `n` to
keep it and look inside with `pct enter 105`.

## Step 3: Check it

1. Open the address from the last step in a browser on your network, and log in with the dashboard password.
2. The Overview page says "No machine has checked in yet". That's expected.
3. Give the container a fixed address, or a DHCP reservation in your router, so your machines always find
   it.
4. To add your first machine, open the dashboard, then **Add a machine**
   ([guide 5](05-linux-and-macos-machines.md) for Linux and macOS, [guide 6](06-windows-machines.md) for Windows).

## Unattended installs

Every question has a setting, so the installer can run without anyone answering. Put `mode=default` in
front of the command and the settings in a file that only root can read. (Typed on the command line, a
password ends up in your shell history.)

1. Create the file:

   ```bash
   nano /root/bullpen.env
   ```

   For example:

   ```bash
   var_ctid=120
   var_hostname=bullpen
   var_net=192.168.100.20/24
   var_gateway=192.168.100.1
   var_container_storage=local-lvm
   var_template_storage=local
   var_hub_password='a long password'
   var_hub_runner_url=http://192.168.100.20:8080
   ```

2. Make it readable only by root:

   ```bash
   chmod 600 /root/bullpen.env
   ```

3. Run the installer with it:

   ```bash
   set -a; source /root/bullpen.env; set +a
   mode=default bash -c "$(curl -fsSL https://raw.githubusercontent.com/rob-paprocki/agent-bullpen/main/ct/bullpen.sh)"
   ```

`mode=mydefaults` works the same way, starting from My Defaults. A setting that isn't valid stops the run
before anything is created.

**All settings**

| Setting | Default | Meaning |
|---|---|---|
| `var_ctid` | next free ID | container ID |
| `var_hostname` | `bullpen` | hostname |
| `var_unprivileged` | `1` | `1` unprivileged, `0` privileged |
| `var_cpu` / `var_ram` / `var_disk` | `1` / `1024` / `6` | CPU cores, RAM in MiB, disk in GB |
| `var_os` / `var_version` | `debian` / `13` | the template (Debian 12 works too) |
| `var_brg` | `vmbr0` | network bridge |
| `var_net` / `var_gateway` | `dhcp` / none | fixed address with prefix, and its gateway |
| `var_vlan` | none | VLAN tag |
| `var_ns` / `var_searchdomain` | the host's | DNS servers (space separated) and search domain |
| `var_pw` | none | root password (blank: console autologin) |
| `var_ssh` / `var_ssh_authorized_key` | `no` / none | root SSH, and a public key for it |
| `var_verbose` | `no` | show every command's output |
| `var_tags` | `bullpen` | Proxmox tags, separated by `;` (`bullpen` is always added) |
| `var_container_storage` / `var_template_storage` | the only one; with several, the menu asks, and unattended runs take the one with the most free space (with a warning). Set both when your host has more than one. | storage for the disk, and for templates |
| `var_hub_password` | generate one | dashboard password (8 characters or more, no `'`) |
| `var_hub_port` | `8080` | hub port, 1024 to 65535 (for 80/443 put a reverse proxy in front) |
| `var_hub_runner_url` | `http://<container IP>:<port>` | address your machines use to reach the hub |
| `var_hub_public_url` | none | https address you open the dashboard at, if any |
| `var_cf_tunnel_token` | none | Cloudflare Tunnel token |
| `var_ref` | newest release | release tag, or a branch such as `main` (unverified) |
| `mode` | show the menu | `default`, `advanced`, `mydefaults` or `settings` |

With no terminal and no `mode=`, the menu can't be shown, so the installer stops and says so.

## My Defaults

My Defaults live on the Proxmox host in `/usr/local/bullpen/default.vars`, readable only by root. The
installer also remembers your storage choice there, so it asks only once.

- Save them at the end of an Advanced Install, or write them with **Settings → Edit My Defaults**. The file
  takes the same `var_name=value` lines as the table above.
- Settings you give on the command line win over My Defaults.
- CPU, RAM and disk from My Defaults never go below what the app itself needs.
- Passwords and tokens are never stored there, even if you add them.

## Updating

Inside the container, run `update`:

1. On the Proxmox host, run `pct enter <container-id>` (or open the container's **Console**).
2. Type `update` and press Enter.
3. If it asks, choose **Yes (quiet)**.

It asks GitHub which release is newest. It reports nothing anywhere. If there is no newer one, it says
`No update available: Agent Bullpen (1.0.0)`. If there is one, it downloads it, checks it against
`SHA256SUMS`, installs it, restarts the hub and says `Updated to 1.0.1`.

New releases come out when there is something worth releasing. Urgent fixes go out as soon as they
are ready. The [releases page](https://github.com/rob-paprocki/agent-bullpen/releases) lists what each one
changed.

- Your settings (`/etc/bullpen/hub.env`) and data (`/var/lib/bullpen`) are kept. Settings that a newer
  version added are filled in with their defaults.
- If the new version doesn't start, the previous one is put back and keeps running.
- Pasting the one-line command inside the container does the same as `update`.
- To install a particular version: `BULLPEN_REF=v1.0.0 update`. To follow the newest code on `main`
  (not verified): `BULLPEN_REF=main update`. Later updates then follow `main` too. To go back to
  releases, run `BULLPEN_REF=v1.0.0 update` (or any newer release tag).
- From your own copy of the code (for development, or without GitHub): on the Proxmox host, in the
  repository folder, run `bash scripts/proxmox/upgrade-hub-lxc.sh <container-id>`.

## Running it

| Task | Command (inside the container: `pct enter <id>`) |
|---|---|
| Status | `systemctl status bullpen` |
| Live log | `journalctl -u bullpen -f` |
| Change settings | `nano /etc/bullpen/hub.env`, then `systemctl restart bullpen` |
| Tunnel status | `systemctl status cloudflared` |
| Connect a tunnel later | `bash /opt/bullpen/src/scripts/linux/install-hub.sh` (it asks for the token) |

**Settings in `/etc/bullpen/hub.env`**

| Setting | What it does |
|---|---|
| `HUB_UI_PASSWORD` | the dashboard password. Jobs that ask you before commands, and tasks that hand out subtasks, need one: without it, any program on your network could answer for you |
| `HUB_PORT` | the port the hub listens on |
| `HUB_RUNNER_URL` | the address your machines use to reach the hub; **Add a machine** puts it in the commands it shows |
| `HUB_RUNNER_TOKEN` | a shared token for machines you set up by hand; machines added with **Add a machine** get their own |
| `HUB_PUBLIC_URL` | the https address you open the hub at, e.g. `https://hub.yourdomain.com`. OAuth sign-ins for MCP servers return there ([guide 10](10-shared-memory-and-mcp.md#signing-in-once-with-oauth)), and notifications link there. When blank, the hub uses the address the dashboard is open at, and notifications carry no link |
| `HUB_COOKIE_SECURE` | `1` once you only use the https address ([guide 3](03-cloudflare.md#step-5-protect-it-with-access)) |
| `HUB_ANTHROPIC_ADMIN_KEYS` | `Work=sk-ant-admin01-…;Personal=sk-ant-admin01-…` for the Usage page ([guide 7](07-api-gateway.md#usage-panel-api-spend-per-organization)) |
| `HUB_ANTHROPIC_COST_URL` | where the hub asks for the cost report, for a proxy in front of Anthropic's API. Leave it out: the default is Anthropic's own address, and the Admin keys are sent to this address |
| `HUB_LITELLM_UI_URL` | link to your LiteLLM dashboard on the Usage page |
| `HUB_LINKS` | extra links for the Usage page: `Name=https://…;Other=https://…` |
| `HUB_MCP_PROXY_TIMEOUT` | seconds one tool call through the hub may take (default 120) |
| `HUB_APPROVAL_TIMEOUT_MIN` | minutes a job's question waits for **Allow** or **Deny** before it counts as no (default 10) |
| `HUB_DELEGATE_MAX_AUTONOMY` | the most any subtask may do, whatever its task may do: `readonly` (the default), `edits`, `ask` or `full`. Raise it only on purpose ([guide 2](02-architecture.md#agents-handing-work-to-other-machines-and-machine-tags)) |
| `HUB_DELEGATE_MAX_CHILDREN` | subtasks one task may hand out in all (default 5, 1-20) |
| `HUB_DELEGATE_MAX_ACTIVE` | subtasks of one task queued or running at once (default 3, 1-10) |
| `HUB_NOTIFY_URL` | where to send notifications: an ntfy topic such as `https://ntfy.sh/<topic>`, or any address that takes a JSON POST. Off when blank ([guide 8](08-daily-use.md#notifications-on-your-phone)) |
| `HUB_NOTIFY_FORMAT` | `ntfy` or `json` (default: `ntfy` when the address's host name contains "ntfy", otherwise `json`) |
| `HUB_NOTIFY_EVENTS` | which events to send: `done`, `error`, `approval` (default all three, comma-separated) |
| `HUB_NOTIFY_HEADERS` | extra headers, `Name=value;Name2=value2`, for example `Authorization=Bearer tk_…` for a protected ntfy topic. With a plain `http://` address the hub turns notifications off instead, since headers usually carry a token |
| `HUB_NOTIFY_TITLES` | `0` never sends a job's title, not even one you typed (default `1`) |

**What is where**

| Where | What |
|---|---|
| `/etc/bullpen/hub.env` | the settings above |
| `/var/lib/bullpen/hub.db` | jobs, notes, machines: everything the hub remembers |
| `/opt/bullpen/app`, `/opt/bullpen/venv` | the hub's code and its Python packages |
| `/opt/bullpen/src` | a full copy of the installed version, used by `update` |
| `/opt/bullpen/release.env` | which version is installed, and where it came from |
| `/etc/cloudflared/bullpen-tunnel.env` | the tunnel token, readable only by root |
| `/usr/bin/update` | the update command |
| `/usr/local/bullpen/default.vars` (on the host) | My Defaults |

**Backups:** add the container to a Proxmox backup job (**Datacenter → Backup**). It's small. Or copy
`/etc/bullpen/hub.env` and `/var/lib/bullpen` somewhere safe.

## Uninstall

1. In the dashboard, **Forget** each machine, so their tokens stop working.
2. On each machine, remove the runner ([guide 5](05-linux-and-macos-machines.md), [guide 6](06-windows-machines.md)).
3. On the Proxmox host, stop and remove the container. In the web interface: **container → More → Remove**.
   Or in the host's shell:

   ```bash
   pct stop <container-id>
   ```

   ```bash
   pct destroy <container-id>
   ```

4. Optional: remove My Defaults from the host with `rm -rf /usr/local/bullpen`.
5. If you used a tunnel, delete it and its Access application in Cloudflare.

## Troubleshooting

| What you see | What to do |
|---|---|
| `This version of Proxmox VE (…) is not supported` | The installer supports Proxmox VE 8 and 9. Upgrade Proxmox first. |
| `Please run this script as root` | Use the web interface's Shell on the node, which logs you in as root. |
| `No active storage can hold the container disk` | Under **Datacenter → Storage**, edit a storage and tick **Container** in its content list. Do the same with **Container template** if the message is about templates. |
| `Storage … has only … free` | Pick another storage, or a smaller disk (`var_disk`). |
| `No debian 13 template found` | The host can't reach the template server. Check its internet access and run `pveam update`. Or use Debian 12: `var_version=12`. |
| `The container got no IPv4 address` | Check the bridge and VLAN. If your network has no DHCP, use Advanced and a fixed address. |
| `DNS does not work in the container` | Set a DNS server in Advanced (step 11), or `var_ns=1.1.1.1`. |
| `Could not ask GitHub for the newest release` | The host can't reach github.com. Check its internet access, or name a release: `var_ref=v1.0.0`. |
| `The checksum of … does not match SHA256SUMS` | The download was damaged or changed. Run the installer again. If it keeps happening, open an issue; don't switch to `main` to get around it. |
| `Installation failed in container …` | The lines above it show the failing command. The full log path is printed too. Keep the container (`n`), look inside with `pct enter <id>`, or run Advanced again with verbose mode on. |
| `The hub did not start` | Inside the container: `journalctl -u bullpen -n 50`. |
| `The upgraded hub did not start, so the previous version is running again` | Same: the journal shows why. Your settings and data are unchanged. |
| Forgot the dashboard password | `pct exec <id> -- grep HUB_UI_PASSWORD /etc/bullpen/hub.env` |
| The container's address changed | Reserve the address in your router. Then update `HUB_RUNNER_URL` in `hub.env` and restart the hub, so new machines get the right address. See [guide 9](09-troubleshooting.md) for machines added earlier. |

## Any Debian or Ubuntu machine

`scripts/linux/install-hub.sh` installs the hub on any machine or VM running Debian 12 or newer, or Ubuntu
24.04 or newer. The hub needs Python 3.11 or newer, which those systems have. Older ones (such as Ubuntu
22.04 with Python 3.10) are refused with a clear message.

1. Download the newest release and its checksums. Replace `1.0.0` with the newest version on the
   [releases page](https://github.com/rob-paprocki/agent-bullpen/releases):

   ```bash
   curl -fsSLO https://github.com/rob-paprocki/agent-bullpen/releases/download/v1.0.0/agent-bullpen-1.0.0.tar.gz
   ```

   ```bash
   curl -fsSLO https://github.com/rob-paprocki/agent-bullpen/releases/download/v1.0.0/SHA256SUMS
   ```

2. Check the download. This must print `agent-bullpen-1.0.0.tar.gz: OK`:

   ```bash
   sha256sum --check --ignore-missing SHA256SUMS
   ```

3. Unpack it and run the installer:

   ```bash
   tar -xzf agent-bullpen-1.0.0.tar.gz
   ```

   ```bash
   sudo bash agent-bullpen-1.0.0/scripts/linux/install-hub.sh
   ```

   (From a git clone of the repository, run `sudo bash scripts/linux/install-hub.sh` in its folder instead.)

It asks for the port, the dashboard password, the address for machines, an optional public address and
the optional Usage page settings, then whether to connect a Cloudflare Tunnel. Every answer has a default.

**Without questions:** put the answers in a root-only file and give it as the first argument. The file is
deleted after reading:

```bash
sudo bash agent-bullpen-1.0.0/scripts/linux/install-hub.sh /root/hub-answers.env
```

The file uses the names `HUB_UI_PASSWORD`, `HUB_PORT`, `HUB_RUNNER_URL`, `HUB_PUBLIC_URL` and
`CF_TUNNEL_TOKEN`, one `NAME='value'` per line. Add `BULLPEN_NONINTERACTIVE=1` to it to skip the remaining
questions.

**Updating** works the same as in the container, without the `update` shortcut:

```bash
sudo bash /opt/bullpen/src/ct/bullpen.sh
```

**Uninstall:**

```bash
sudo systemctl disable --now bullpen
```

```bash
sudo rm -rf /opt/bullpen /etc/bullpen /var/lib/bullpen /etc/systemd/system/bullpen.service
```

```bash
sudo userdel bullpen
```

If you connected a tunnel: `sudo systemctl disable --now cloudflared`, then remove
`/etc/systemd/system/cloudflared.service`, `/etc/cloudflared/bullpen-tunnel.env` and the `cloudflared`
package.

## How the installer is built

For contributors, and anyone who wants to check what it does before running it.

| File | Runs on | Job |
|---|---|---|
| `ct/bullpen.sh` | the Proxmox host (and inside the container for updates) | the app's defaults, its update steps and the closing message |
| `misc/build.func` | the host | checks, menus, the Advanced wizard, storage, `pct create`, Notes, the update steps |
| `misc/install.func` | the container | OS update, login message, autologin, clean-up |
| `misc/core.func` | both | messages, the error report, the download and checksum check |
| `install/bullpen-install.sh` | the container | installs the hub, using `scripts/linux/install-hub.sh` |
| `scripts/proxmox/create-hub-lxc.sh`, `upgrade-hub-lxc.sh` | the host | the same installer, with the code in your checkout instead of a download |
| `tests/proxmox/` | any Linux machine | runs all of the above with stand-in commands; changes nothing on the machine |

- Settings travel into the container as environment variables, never as command-line arguments, so
  passwords and tokens don't show up in process lists or logs.
- `BULLPEN_LOCAL_SRC=/path/to/agent-bullpen` installs from a folder (a checkout or an unpacked release)
  instead of downloading. It is not checked against a checksum.
- A fork can set `BULLPEN_REPO=owner/repo` (and `BULLPEN_REF`) to install from itself.
- Run the tests with `sudo python3 tests/proxmox/test_installer.py` (about 30 seconds). They don't
  replace a run on a real Proxmox VE host, which hasn't happened yet.
