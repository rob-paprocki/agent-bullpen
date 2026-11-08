#!/usr/bin/env python3
"""Tests for the Proxmox installer (ct/bullpen.sh, misc/*.func,
install/bullpen-install.sh and scripts/linux/install-hub.sh), without Proxmox.

They run the real one-line command with stub commands first on PATH (pct,
pvesm, pveam, pvesh, pveversion, whiptail, lxc-attach, curl, apt-get,
systemctl, ...; see stubs/). curl serves GitHub's addresses from release
tarballs built from this checkout. The container side writes into a fake root
folder (BULLPEN_TEST_ROOT), so nothing on this machine is installed or changed.

    python3 tests/proxmox/test_installer.py          (about 30 seconds)

shellcheck runs too when it is found (PATH, or SHELLCHECK=/path/to/shellcheck).
None of this replaces a run on a real Proxmox VE host.
"""
import atexit
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
STUBS = Path(__file__).resolve().parent / "stubs"
SLUG = "rob-paprocki/agent-bullpen"
ONE_LINER = f'bash -c "$(curl -fsSL https://raw.githubusercontent.com/{SLUG}/main/ct/bullpen.sh)"'
REAL_PYTHON = shutil.which("python3")
SYS_PATH = os.environ.get("PATH", "/usr/bin:/bin")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ALLOWED_URL = re.compile(
    rf"^(https://raw\.githubusercontent\.com/{SLUG}/|https://github\.com/{SLUG}/releases/"
    rf"|https://codeload\.github\.com/{SLUG}/tar\.gz/|http://127\.0\.0\.1:\d+/healthz$"
    r"|https://pkg\.cloudflare\.com/cloudflare-main\.gpg$)")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}
SHELL_FILES = ["ct/bullpen.sh", "misc/build.func", "misc/core.func", "misc/install.func",
               "install/bullpen-install.sh", "scripts/linux/install-hub.sh",
               "scripts/proxmox/create-hub-lxc.sh", "scripts/proxmox/upgrade-hub-lxc.sh"]


def source_files():
    for path in sorted(REPO.rglob("*")):
        rel = path.relative_to(REPO)
        if path.is_file() and not SKIP_DIRS.intersection(rel.parts) and rel.parts[:2] != ("hub", "data"):
            yield path, rel


def build_release(releases, version):
    """A release like the GitHub workflow makes: agent-bullpen-X.Y.Z.tar.gz and SHA256SUMS."""
    tag_dir = releases / f"v{version}"
    tag_dir.mkdir(parents=True)
    name = f"agent-bullpen-{version}.tar.gz"
    top = f"agent-bullpen-{version}"
    with tarfile.open(tag_dir / name, "w:gz") as tar:
        for path, rel in source_files():
            if rel.as_posix() != "VERSION":
                tar.add(path, f"{top}/{rel.as_posix()}")
        version_file = tag_dir / "VERSION"
        version_file.write_text(version + "\n")
        tar.add(version_file, f"{top}/VERSION")
    digest = hashlib.sha256((tag_dir / name).read_bytes()).hexdigest()
    (tag_dir / "SHA256SUMS").write_text(f"{digest}  {name}\n")


def build_codeload(releases, ref):
    out = releases / "codeload"
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out / f"{ref}.tar.gz", "w:gz") as tar:
        for path, rel in source_files():
            tar.add(path, f"agent-bullpen-{ref}/{rel.as_posix()}")


class Releases:
    """Built once for all tests: v1.0.0, v1.0.1, v1.0.2 and a 'main' branch download."""
    _dir = None

    @classmethod
    def path(cls):
        if cls._dir is None:
            cls._dir = Path(tempfile.mkdtemp(prefix="bullpen-releases-"))
            atexit.register(shutil.rmtree, cls._dir, True)
            for version in ("1.0.0", "1.0.1", "1.0.2"):
                build_release(cls._dir, version)
            build_codeload(cls._dir, "main")
        return cls._dir


class Run:
    def __init__(self, proc, output):
        self.rc = proc.returncode
        self.raw = output
        self.out = ANSI.sub("", output)


class InstallerTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bullpen-pve-test-"))
        self.state = self.tmp / "state"
        self.host_root = self.tmp / "host"
        for d in (self.state, self.host_root, self.tmp / "tmp", self.tmp / "home"):
            d.mkdir()
        self.answers = self.tmp / "answers.txt"
        self.secrets = []

    def tearDown(self):
        if os.environ.get("KEEP_TEST_DIRS"):
            print(f"\n  kept {self.tmp}")
        else:
            shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ helpers ---
    def env(self, container=None, **extra):
        root = self.rootfs(container) if container else self.host_root
        path = f"{STUBS / 'common'}:{root / 'usr/bin'}:{SYS_PATH}" if container else \
            f"{STUBS / 'host'}:{STUBS / 'common'}:{SYS_PATH}"
        env = {
            "PATH": path, "HOME": str(self.tmp / "home"), "TERM": "xterm", "LANG": "C.UTF-8",
            "TMPDIR": str(self.tmp / "tmp"), "BULLPEN_TEST_ROOT": str(root),
            "BULLPEN_TEST_STATE": str(self.state), "BULLPEN_TEST_STUBS": str(STUBS / "common"),
            "BULLPEN_TEST_SYS_PATH": SYS_PATH, "BULLPEN_TEST_REAL_PYTHON": REAL_PYTHON,
            "BULLPEN_TEST_RAW_DIR": str(REPO), "BULLPEN_TEST_RELEASES": str(Releases.path()),
            "BULLPEN_TEST_LATEST": "v1.0.0", "BULLPEN_TEST_WT_ANSWERS": str(self.answers),
            "BULLPEN_PROMPT_TIMEOUT": "2",
        }
        env.update({k: v for k, v in extra.items() if v is not None})
        return env

    def run_cmd(self, command, env, stdin="", timeout=180):
        proc = subprocess.run(["bash", "-c", command], env=env, input=stdin.encode(), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=timeout, cwd=self.tmp)
        run = Run(proc, proc.stdout.decode(errors="replace"))
        if os.environ.get("SHOW_OUTPUT"):
            print(run.raw)
        return run

    def answer(self, *lines):
        self.answers.write_text("\n".join(lines) + "\n")

    def rootfs(self, ctid="100"):
        return self.state / "ct" / str(ctid) / "rootfs"

    def pct_calls(self, sub=None):
        path = self.state / "pct.log"
        calls = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
        return [c for c in calls if sub is None or (c and c[0] == sub)]

    def create_args(self, ctid="100"):
        return json.loads((self.state / "ct" / str(ctid) / "create.json").read_text())

    def opt(self, args, name):
        return args[args.index(name) + 1] if name in args else None

    def attach_env(self, ctid="100"):
        raw = (self.state / "ct" / str(ctid) / "attach-env").read_bytes().split(b"\0")
        return dict(item.decode().split("=", 1) for item in raw if b"=" in item)

    def read(self, path):
        return Path(path).read_text() if Path(path).exists() else ""

    def whiptail_log(self):
        return self.read(self.state / "whiptail.log")

    def assert_ok(self, run):
        self.assertEqual(run.rc, 0, run.out[-4000:])

    def assert_clean_logs(self, run):
        """No surprise network calls, no unanswered dialogs, and secrets never in a
        command line or a log."""
        for url in self.read(self.state / "curl.log").splitlines():
            self.assertRegex(url, ALLOWED_URL, "unexpected network address")
            self.assertNotIn("UNEXPECTED", url)
        self.assertNotIn("NO ANSWER", self.whiptail_log())
        self.assertNotIn("UNEXPECTED DIALOG", self.whiptail_log())
        logs = [self.state / n for n in ("pct.log", "lxc-attach.log", "calls.log", "curl.log", "whiptail.log")]
        logs += list((self.tmp / "tmp").glob("*.log"))
        logs += list(self.state.glob("ct/*/rootfs/root/.install-*.log"))
        for secret in self.secrets:
            for log in logs:
                self.assertNotIn(secret, self.read(log), f"a secret is in {log}")

    def assert_closing(self, run, url, password=None):
        self.assertIn("Completed successfully!", run.out)
        self.assertIn("Agent Bullpen setup has been successfully initialized!", run.out)
        self.assertIn(url, run.out)
        self.assertIn("Add machines: open the dashboard, then Add a machine", run.out)
        if password:
            self.assertIn(f"Dashboard password: {password}", run.out)
        else:
            self.assertNotIn("Dashboard password:", run.out)

    def hub_env(self, ctid="100"):
        text = self.read(self.rootfs(ctid) / "etc/bullpen/hub.env")
        return dict(re.findall(r"^([A-Z_]+)='(.*)'$", text, re.M))

    def install_default(self):
        """A Default install run straight from the command line (used by the update tests)."""
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env())
        self.assert_ok(run)
        return run

    # -------------------------------------------------------------- tests ---
    def test_pve9_template_list_gets_the_hosts_architecture(self):
        # Proxmox VE 9 lists amd64 and arm64 templates side by side (this is its real list, from
        # aplinfo-pve-9.dat); "arm64" sorts after "amd64", so a name-only match picked the wrong one.
        listing = (Path(__file__).resolve().parent / "data" / "pve9-available.txt").read_text()
        templates = " ".join(line.split()[1] for line in listing.splitlines() if line.strip())
        self.assertIn("debian-13-standard_13.6-1_arm64.tar.zst", templates)
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_TEMPLATES=templates))
        self.assert_ok(run)
        self.assertEqual(self.create_args()[2], "local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst")

    def test_an_id_taken_meanwhile_is_never_offered_for_removal(self):
        # CT 100 is free when it is chosen, then someone creates it while the template downloads.
        env = self.env(BULLPEN_PROMPT_TIMEOUT="1", BULLPEN_TEST_REAL_STUBS=str(STUBS / "host"))
        env["PATH"] = f"{STUBS / 'race'}:{env['PATH']}"
        run = self.run_cmd(f"mode=default {ONE_LINER}", env)
        self.assertNotEqual(run.rc, 0)
        self.assertIn("was taken while the template downloaded", run.out)
        self.assertNotIn("Remove broken container", run.out)
        self.assertTrue((self.state / "ct/100/OTHER-CONTAINER").exists(), "someone else's container was touched")

    def test_a_hub_installed_from_main_moves_to_the_first_release(self):
        # Before the first release the installer falls back to main (unverified), with that version's
        # number. Once the release with the same number exists, "update" must move it there.
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_LATEST=""))
        self.assert_ok(run)
        root = self.rootfs()
        self.assertIn("BULLPEN_SOURCE_KIND='branch'", self.read(root / "opt/bullpen/release.env"))
        version = self.read(REPO / "VERSION").strip()
        run = self.run_cmd(f"bash {root}/usr/bin/update", self.env(container="100", BULLPEN_TEST_LATEST=f"v{version}"))
        self.assert_ok(run)
        self.assertIn("to the verified release", run.out)
        self.assertIn("BULLPEN_SOURCE_KIND='release'", self.read(root / "opt/bullpen/release.env"))

    def test_update_follows_a_renamed_repository_to_its_release(self):
        self.install_default()
        root = self.rootfs()
        release = root / "opt/bullpen/release.env"
        # The hub was installed from a repository that has since been renamed.
        release.write_text(self.read(release).replace(f"BULLPEN_REPO='{SLUG}'", "BULLPEN_REPO='old-owner/agent-bullpen'"))
        run = self.run_cmd(f"bash {root}/usr/bin/update", self.env(
            container="100", BULLPEN_TEST_LATEST="v1.0.1", BULLPEN_TEST_OLD_REPO="old-owner/agent-bullpen",
            BULLPEN_REPO="old-owner/agent-bullpen"))
        self.assertNotIn("not verified", run.out)
        self.assertIn("BULLPEN_SOURCE_KIND='release'", self.read(release))

    def test_default_install_from_menu(self):
        self.answer("Agent Bullpen => 1", "STORAGE POOLS => local-zfs")
        run = self.run_cmd(ONE_LINER, self.env(
            BULLPEN_TEST_STORAGES_ROOTDIR="local-lvm:lvmthin:300000000:20000000;local-zfs:zfspool:500000000:10000000"))
        self.assert_ok(run)

        args = self.create_args()
        self.assertEqual(args[:3], ["create", "100", "local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst"])
        expected = {"--hostname": "bullpen", "--ostype": "debian", "--cores": "1", "--memory": "1024",
                    "--swap": "512", "--rootfs": "local-zfs:6", "--net0": "name=eth0,bridge=vmbr0,ip=dhcp",
                    "--unprivileged": "1", "--features": "nesting=1", "--onboot": "1", "--tags": "bullpen",
                    "--arch": "amd64"}
        for name, value in expected.items():
            self.assertEqual(self.opt(args, name), value, name)
        self.assertNotIn("--password", args)

        env = self.attach_env()
        password = env["HUB_UI_PASSWORD"]
        self.secrets.append(password)
        self.assertRegex(password, r"^[A-Za-z0-9]{24}$")
        self.assertEqual(env["HUB_PORT"], "8080")
        self.assertEqual(env["HUB_RUNNER_URL"], "http://192.168.100.50:8080")
        self.assertEqual(env["HUB_PUBLIC_URL"], "")
        self.assertEqual(env["CF_TUNNEL_TOKEN"], "")
        self.assertEqual(env["PASSWORD"], "")
        self.assertEqual((env["BULLPEN_REF"], env["BULLPEN_SOURCE_KIND"]), ("v1.0.0", "release"))
        self.assertIn("setting_up_container()", env["FUNCTIONS_FILE_PATH"])
        # The installer's own colour and icon variables stay out of the container.
        self.assertNotIn("CM", env)
        self.assertNotIn("🏠", env.get("HOSTNAME", ""))
        self.assertIn("--keep-env", self.read(self.state / "lxc-attach.log"))

        root = self.rootfs()
        hub = self.hub_env()
        self.assertEqual(hub["HUB_UI_PASSWORD"], password)
        self.assertEqual(hub["HUB_RUNNER_URL"], "http://192.168.100.50:8080")
        self.assertEqual(hub["HUB_PORT"], "8080")
        self.assertTrue(hub["HUB_RUNNER_TOKEN"] and hub["HUB_SECRET"])
        self.secrets += [hub["HUB_RUNNER_TOKEN"], hub["HUB_SECRET"]]
        for rel in ("opt/bullpen/app/app.py", "opt/bullpen/app/runner_dist/runner.py",
                    "opt/bullpen/app/agent_kit", "opt/bullpen/app/setup_dist/linux/setup-workstation.sh",
                    "opt/bullpen/app/setup_dist/windows/Setup-Workstation.ps1",
                    "opt/bullpen/src/ct/bullpen.sh", "opt/bullpen/src/misc/build.func",
                    "opt/bullpen/venv/bin/python", "etc/systemd/system/bullpen.service",
                    "etc/profile.d/00_bullpen-details.sh", "root/.hushlogin",
                    "etc/systemd/system/container-getty@1.service.d/override.conf"):
            self.assertTrue((root / rel).exists(), rel)
        self.assertFalse((root / "opt/bullpen/app/data").exists())
        self.assertIn("BULLPEN_VERSION='1.0.0'", self.read(root / "opt/bullpen/release.env"))
        self.assertIn("BULLPEN_SOURCE_KIND='release'", self.read(root / "opt/bullpen/release.env"))
        update = root / "usr/bin/update"
        self.assertTrue(os.access(update, os.X_OK))
        self.assertIn("/opt/bullpen/src/ct/bullpen.sh", self.read(update))
        self.assertEqual(stat.S_IMODE((root / "etc/bullpen/hub.env").stat().st_mode), 0o640)
        self.assertFalse(os.access(root / "etc/update-motd.d/10-uname", os.X_OK))
        self.assertEqual(self.read(root / "etc/motd"), "Agent Bullpen LXC Container\n")
        self.assertFalse((root / "etc/systemd/system/cloudflared.service").exists())
        self.assertIn("--autologin root", self.read(root / "etc/systemd/system/container-getty@1.service.d/override.conf"))
        calls = self.read(self.state / "calls.log")
        self.assertIn("systemctl [" + str(root) + "] restart bullpen", calls)
        self.assertIn("pip install -q -r " + str(root) + "/opt/bullpen/app.new/requirements.txt", calls)
        self.assertNotIn("chpasswd", calls)

        notes = self.read(self.state / "ct/100/description.html")
        for text in ("Agent Bullpen LXC", "http://192.168.100.50:8080", f"https://github.com/{SLUG}", "Version 1.0.0",
                     "not affiliated"):
            self.assertIn(text, notes)
        for text in ("donat", "community-scripts.org", "<img"):
            self.assertNotIn(text, notes.lower())

        defaults = self.host_root / "usr/local/bullpen/default.vars"
        self.assertIn("var_container_storage=local-zfs", self.read(defaults))
        self.assertEqual(stat.S_IMODE(defaults.stat().st_mode), 0o600)
        self.assertIn("Downloaded and verified agent-bullpen-1.0.0.tar.gz", run.out)
        self.assertIn("Using Default Settings on node test-node", run.out)
        # The generated password is shown once on screen on purpose, but never logged.
        self.assert_closing(run, "http://192.168.100.50:8080", password)
        self.assert_clean_logs(run)

    def test_advanced_wizard_with_back_and_validation(self):
        root_pw, hub_pw = "rootpass123", "correct horse battery"
        token = "eyJhIjoiMTIzNDU2Nzg5MCIsInQiOiJ0ZXN0LXR1bm5lbCJ9"
        key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOnlyATestKey test@example"
        self.secrets += [root_pw, hub_pw, token]
        self.answer(
            "Agent Bullpen => 2",
            "CONTAINER TYPE => 0",
            f"ROOT PASSWORD => {root_pw}", f"ROOT PASSWORD => {root_pw}",
            "CONTAINER ID => abc",           # not a number: refused, asked again
            "CONTAINER ID => 120",
            "HOSTNAME => <back>",            # Back to the ID step
            "CONTAINER ID => 120",
            "HOSTNAME => My-Hub",            # capitals: refused
            "HOSTNAME => my-hub",
            "DISK SIZE => 8", "CPU CORES => 2", "RAM SIZE => 2048",
            "NETWORK BRIDGE => vmbr1",
            "IPv4 ADDRESS => static", "STATIC IPv4 ADDRESS => 192.168.100.20/24", "GATEWAY IP => 192.168.100.1",
            "VLAN TAG => 20", "DNS SERVER => 1.1.1.1",
            "SSH ACCESS => yes", f"SSH KEY => {key}",
            "VERBOSE MODE => no",
            f"DASHBOARD PASSWORD => {hub_pw}", f"DASHBOARD PASSWORD => {hub_pw}",
            "HUB PORT => 9090",
            "ADDRESS FOR MACHINES => ",
            "PUBLIC ADDRESS => https://hub.example.com",
            f"CLOUDFLARE TUNNEL => {token}",
            "RELEASE => ",
            "CONFIRM SETTINGS => <yes>",
            "MY DEFAULTS => <yes>",
        )
        run = self.run_cmd(ONE_LINER, self.env(BULLPEN_TEST_IP="192.168.100.20"))
        self.assert_ok(run)

        args = self.create_args(120)
        self.assertEqual(args[1], "120")
        expected = {"--hostname": "my-hub", "--cores": "2", "--memory": "2048", "--rootfs": "local-lvm:8",
                    "--unprivileged": "0", "--nameserver": "1.1.1.1",
                    "--net0": "name=eth0,bridge=vmbr1,ip=192.168.100.20/24,gw=192.168.100.1,tag=20"}
        for name, value in expected.items():
            self.assertEqual(self.opt(args, name), value, name)

        wt = self.whiptail_log()
        self.assertIn("msgbox | CONTAINER ID", wt)
        self.assertIn("msgbox | HOSTNAME", wt)
        self.assertIn("Agent Bullpen [Step 3/20]", wt)
        self.assertIn("yesno | CONFIRM SETTINGS | Agent Bullpen [Step 20/20]", wt)
        self.assertIn("Cloudflare Tunnel", wt)  # the confirm screen's summary

        env = self.attach_env(120)
        self.assertEqual((env["PASSWORD"], env["SSH_ROOT"], env["SSH_AUTHORIZED_KEY"]), (root_pw, "yes", key))
        self.assertEqual((env["HUB_UI_PASSWORD"], env["HUB_PORT"]), (hub_pw, "9090"))
        self.assertEqual(env["HUB_RUNNER_URL"], "http://192.168.100.20:9090")
        self.assertEqual(env["HUB_PUBLIC_URL"], "https://hub.example.com")
        self.assertEqual(env["CF_TUNNEL_TOKEN"], token)

        root = self.rootfs(120)
        self.assertEqual(self.hub_env(120)["HUB_UI_PASSWORD"], hub_pw)
        self.assertEqual(self.hub_env(120)["HUB_PUBLIC_URL"], "https://hub.example.com")
        tunnel_env = root / "etc/cloudflared/bullpen-tunnel.env"
        self.assertEqual(self.read(tunnel_env), f"TUNNEL_TOKEN='{token}'\n")
        self.assertEqual(stat.S_IMODE(tunnel_env.stat().st_mode), 0o600)
        unit = self.read(root / "etc/systemd/system/cloudflared.service")
        self.assertIn("EnvironmentFile=/etc/cloudflared/bullpen-tunnel.env", unit)
        self.assertNotIn(token, unit)
        calls = self.read(self.state / "calls.log")
        self.assertIn("chpasswd sha256=" + hashlib.sha256(f"root:{root_pw}\n".encode()).hexdigest(), calls)
        self.assertIn(key, self.read(root / "root/.ssh/authorized_keys"))
        self.assertEqual(self.read(root / "etc/ssh/sshd_config.d/10-bullpen.conf"), "PermitRootLogin yes\n")
        self.assertFalse((root / "etc/systemd/system/container-getty@1.service.d/override.conf").exists())

        defaults = self.read(self.host_root / "usr/local/bullpen/default.vars")
        for line in ("var_unprivileged=0", "var_cpu=2", "var_ram=2048", "var_net=192.168.100.20/24",
                     "var_gateway=192.168.100.1", "var_vlan=20", "var_hub_port=9090",
                     "var_hub_public_url=https://hub.example.com", "var_hostname=my-hub"):
            self.assertIn(line, defaults.splitlines())
        self.assertIn("Using Advanced Settings on node", run.out)
        self.assertIn("Connected the Cloudflare Tunnel", run.out)
        self.assert_closing(run, "http://192.168.100.20:9090")
        for secret in self.secrets:
            self.assertNotIn(secret, run.out)
            self.assertNotIn(secret, defaults)
        self.assert_clean_logs(run)

    def test_unattended_with_env_file(self):
        hub_pw = "a long test password"
        self.secrets.append(hub_pw)
        env_file = self.tmp / "hub-install.env"
        env_file.write_text(
            "var_ctid=150\nvar_hostname=hub-test\nvar_cpu=2\nvar_ram=2048\nvar_disk=10\n"
            "var_container_storage=local-zfs\nvar_template_storage=local\n"
            f"var_hub_password='{hub_pw}'\nvar_hub_port=8181\nvar_hub_runner_url=http://hub.lan:8181\n"
            "var_ref=v1.0.0\nvar_verbose=yes\n")
        env_file.chmod(0o600)
        run = self.run_cmd(f"set -a; source {env_file}; set +a; mode=default {ONE_LINER}", self.env(
            BULLPEN_TEST_LATEST="v1.0.1",
            BULLPEN_TEST_STORAGES_ROOTDIR="local-lvm:lvmthin:300000000:20000000;local-zfs:zfspool:500000000:10000000"))
        self.assert_ok(run)
        self.assertFalse((self.state / "whiptail.log").exists(), "an unattended run showed a dialog")
        args = self.create_args(150)
        for name, value in {"--hostname": "hub-test", "--cores": "2", "--memory": "2048",
                            "--rootfs": "local-zfs:10"}.items():
            self.assertEqual(self.opt(args, name), value, name)
        env = self.attach_env(150)
        self.assertEqual((env["HUB_UI_PASSWORD"], env["HUB_PORT"]), (hub_pw, "8181"))
        self.assertEqual(env["HUB_RUNNER_URL"], "http://hub.lan:8181")
        self.assertEqual(env["BULLPEN_REF"], "v1.0.0")
        self.assertIn(f"https://github.com/{SLUG}/releases/download/v1.0.0/agent-bullpen-1.0.0.tar.gz",
                      self.read(self.state / "curl.log"))
        self.assertFalse((self.host_root / "usr/local/bullpen/default.vars").exists())
        # Verbose mode: every command's output is on screen as well as in the log.
        self.assertIn("Follow the install from another shell with: pct exec 150 -- tail -f /root/.install-", run.out)
        self.assertIn("1/6  Installing system packages", run.out)
        self.assertIn("1/6  Installing system packages", self.read(next(self.rootfs(150).glob("root/.install-*.log"))))
        self.assert_closing(run, "http://192.168.100.50:8181")
        self.assertNotIn(hub_pw, run.out)
        self.assert_clean_logs(run)

    def test_unattended_rejects_a_bad_setting_before_creating_anything(self):
        run = self.run_cmd(f"var_hub_port=99999 mode=default {ONE_LINER}", self.env())
        self.assertNotEqual(run.rc, 0)
        self.assertIn("var_hub_port: Use a port from 1024 to 65535 (put a reverse proxy in front for 80 or 443).", run.out)
        self.assertEqual(self.pct_calls("create"), [])

    def test_unattended_taken_container_id(self):
        run = self.run_cmd(f"var_ctid=105 mode=default {ONE_LINER}", self.env(BULLPEN_TEST_USED_IDS="105"))
        self.assertNotEqual(run.rc, 0)
        self.assertIn("var_ctid: Container/VM ID 105 is already in use.", run.out)
        self.assertEqual(self.pct_calls("create"), [])

    def test_my_defaults(self):
        defaults = self.host_root / "usr/local/bullpen/default.vars"
        defaults.parent.mkdir(parents=True)
        defaults.write_text("# saved\nvar_cpu=4\nvar_ram=512\nvar_hostname=saved-hub\nvar_pw=never\n")
        run = self.run_cmd(f"var_cpu=3 mode=mydefaults {ONE_LINER}", self.env())
        self.assert_ok(run)
        args = self.create_args()
        # The command line wins; RAM never goes below the app's own 1024.
        self.assertEqual((self.opt(args, "--cores"), self.opt(args, "--memory"), self.opt(args, "--hostname")),
                         ("3", "1024", "saved-hub"))
        self.assertIn("My Defaults: ignoring var_pw", run.out)
        self.assertIn("Using My Defaults on node", run.out)

    def test_settings_menu_then_exit(self):
        self.answer("Agent Bullpen => 4", "SETTINGS => 2", "SETTINGS => 1", "SETTINGS => <back>", "Agent Bullpen => <back>")
        run = self.run_cmd(ONE_LINER, self.env())
        self.assert_ok(run)
        self.assertIn("User exited script", run.out)
        self.assertIn("msgbox | MY DEFAULTS", self.whiptail_log())
        defaults = self.host_root / "usr/local/bullpen/default.vars"
        self.assertIn("nano " + str(defaults), self.read(self.state / "calls.log"))
        self.assertIn("#var_container_storage=", self.read(defaults))
        self.assertEqual(stat.S_IMODE(defaults.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(defaults.parent.stat().st_mode), 0o700)
        self.assertEqual(self.pct_calls("create"), [])

    def test_failure_in_container_offers_removal_yes(self):
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_FAIL_APT="python3-venv"), stdin="y\n")
        self.assertEqual(run.rc, 100, run.out[-3000:])
        self.assertIn("while executing command apt-get install -y curl ca-certificates tar python3 python3-venv",
                      run.out)
        self.assertIn("--- Output of the failing command ---", run.out)
        self.assertIn("E: Unable to locate package python3-venv", run.out)
        self.assertIn("Installation failed in container 100 (exit code: 100)", run.out)
        self.assertIn("Remove broken container 100? (Y/n) [auto-remove in 2s]", run.out)
        self.assertIn("Removed container 100", run.out)
        self.assertTrue(self.pct_calls("destroy"))
        self.assertFalse((self.state / "ct/100").exists())
        logs = list((self.tmp / "tmp").glob("bullpen-100-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("=== Container log", self.read(logs[0]))
        self.assertEqual(stat.S_IMODE(logs[0].stat().st_mode), 0o600)

    def test_failure_in_container_keep_on_no(self):
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_PY_VERSION="3.10"), stdin="n\n")
        self.assertNotEqual(run.rc, 0)
        self.assertIn("The hub needs Python 3.11 or newer, and this system has 3.10", run.out)
        self.assertIn("Container 100 kept. Look inside with: pct enter 100", run.out)
        self.assertEqual(self.pct_calls("destroy"), [])
        self.assertTrue((self.state / "ct/100").exists())

    def test_failure_on_host_removes_after_timeout(self):
        # stdin stays open but nobody answers: the container is removed after the timeout.
        run = self.run_cmd(f"/bin/sleep 4 | (mode=default {ONE_LINER})",
                           self.env(BULLPEN_TEST_FAIL_PCT="start", BULLPEN_PROMPT_TIMEOUT="1"))
        self.assertNotEqual(run.rc, 0)
        self.assertIn("while executing command pct start 100", run.out)
        self.assertIn("stub pct: start failed (injected)", run.out)
        self.assertIn("[auto-remove in 1s]", run.out)
        self.assertIn("Removed container 100", run.out)
        self.assertTrue(self.pct_calls("destroy"))

    def test_no_network_in_container(self):
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_NO_IP="1", BULLPEN_NET_WAIT="3"),
                           stdin="y\n")
        self.assertNotEqual(run.rc, 0)
        self.assertIn("The container got no IPv4 address in 3 seconds", run.out)
        self.assertIn("Removed container 100", run.out)

    def test_debian_12_when_13_is_not_offered(self):
        old_list = "debian-12-standard_12.7-1_amd64.tar.zst debian-12-standard_12.12-1_amd64.tar.zst"
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_PVE="8.2.4", BULLPEN_TEST_TEMPLATES=old_list))
        self.assert_ok(run)
        self.assertIn("offers no Debian 13 template, so the container uses Debian 12", run.out)
        self.assertEqual(self.create_args()[2], "local:vztmpl/debian-12-standard_12.12-1_amd64.tar.zst")
        # Asked for by name, Debian 13 is not swapped for 12.
        run = self.run_cmd(f"var_version=13 mode=default {ONE_LINER}",
                           self.env(BULLPEN_TEST_TEMPLATES=old_list, BULLPEN_TEST_USED_IDS="100"))
        self.assertEqual(run.rc, 1)
        self.assertIn("No debian 13 template found", run.out)

    def test_not_proxmox_and_old_proxmox(self):
        env = self.env()
        env["PATH"] = f"{STUBS / 'common'}:{SYS_PATH}"  # no pveversion: not a Proxmox host
        run = self.run_cmd(ONE_LINER, env)
        self.assertEqual(run.rc, 1)
        self.assertIn("No Agent Bullpen installation found on this machine.", run.out)
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_PVE="7.4-3"))
        self.assertEqual(run.rc, 1)
        self.assertIn("This version of Proxmox VE (7.4-3) is not supported.", run.out)

    def test_install_without_a_release_uses_main_unverified(self):
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_LATEST=""))
        self.assert_ok(run)
        self.assertIn("Installing 'main': not a release, so the download can't be verified.", run.out)
        self.assertIn(f"https://codeload.github.com/{SLUG}/tar.gz/main", self.read(self.state / "curl.log"))
        release = self.read(self.rootfs() / "opt/bullpen/release.env")
        self.assertIn("BULLPEN_SOURCE_KIND='branch'", release)
        # Nothing was asked for, so later updates move to the first real release.
        self.assertIn("BULLPEN_TRACK=''", release)

    def test_tampered_release_is_refused(self):
        releases = self.tmp / "releases"
        shutil.copytree(Releases.path(), releases)
        (releases / "v1.0.0/SHA256SUMS").write_text("0" * 64 + "  agent-bullpen-1.0.0.tar.gz\n")
        run = self.run_cmd(f"mode=default {ONE_LINER}", self.env(BULLPEN_TEST_RELEASES=str(releases)), stdin="y\n")
        self.assertEqual(run.rc, 116, run.out[-2000:])
        self.assertIn("The checksum of agent-bullpen-1.0.0.tar.gz does not match SHA256SUMS", run.out)
        self.assertFalse((self.rootfs() / "opt/bullpen/app").exists())

    def test_local_source_wrapper(self):
        run = self.run_cmd(f"mode=default bash {REPO}/scripts/proxmox/create-hub-lxc.sh", self.env())
        self.assert_ok(run)
        urls = self.read(self.state / "curl.log").splitlines()
        self.assertTrue(urls and all(u.endswith("/healthz") for u in urls), urls)
        self.assertTrue(any(c[:2] == ["push", "100"] and c[3] == "/root/.bullpen-src.tar.gz"
                            for c in self.pct_calls()))
        self.assertIn("BULLPEN_SOURCE_KIND='local'", self.read(self.rootfs() / "opt/bullpen/release.env"))
        self.assertFalse((self.rootfs() / "root/.bullpen-src.tar.gz").exists())

    def test_update_path(self):
        self.install_default()
        root = self.rootfs()
        data = root / "var/lib/bullpen/hub.db"
        data.write_text("my job history")
        env_path = root / "etc/bullpen/hub.env"
        before = self.hub_env()
        # Pretend this hub came from an older version without HUB_RUNNER_URL.
        env_path.write_text("".join(l for l in self.read(env_path).splitlines(True)
                                    if not l.startswith("HUB_RUNNER_URL=")))
        update = f"bash {root}/usr/bin/update"

        run = self.run_cmd(update, self.env(container="100", BULLPEN_TEST_LATEST="v1.0.1"), stdin="")
        self.assert_ok(run)
        self.assertIn("Update available: Agent Bullpen 1.0.0 → 1.0.1", run.out)
        self.assertIn("Downloaded and verified agent-bullpen-1.0.1.tar.gz", run.out)
        self.assertIn("Updated to 1.0.1", run.out)
        self.assertIn("BULLPEN_VERSION='1.0.1'", self.read(root / "opt/bullpen/release.env"))
        self.assertEqual(self.read(root / "opt/bullpen/src/VERSION").strip(), "1.0.1")
        self.assertEqual(self.read(data), "my job history")
        after = self.hub_env()
        for key in ("HUB_UI_PASSWORD", "HUB_RUNNER_TOKEN", "HUB_SECRET", "HUB_PORT"):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(after["HUB_RUNNER_URL"], "http://192.168.100.50:8080")
        self.assertTrue((root / "opt/bullpen/app.old").is_dir())

        run = self.run_cmd(update, self.env(container="100", BULLPEN_TEST_LATEST="v1.0.1"))
        self.assert_ok(run)
        self.assertIn("No update available: Agent Bullpen (1.0.1)", run.out)
        self.assertNotIn("Downloading", run.out)

        # The one-liner, run inside the container, takes the same update path.
        run = self.run_cmd(ONE_LINER, self.env(container="100", BULLPEN_TEST_LATEST="v1.0.1"))
        self.assert_ok(run)
        self.assertIn("No update available: Agent Bullpen (1.0.1)", run.out)

        # The packages of a newer release can't be downloaded: the running version is left alone.
        run = self.run_cmd(update, self.env(container="100", BULLPEN_TEST_LATEST="v1.0.2", BULLPEN_TEST_FAIL_PIP="1"))
        self.assertNotEqual(run.rc, 0)
        self.assertIn("ERROR: Could not find a version (test stub)", run.out)
        self.assertEqual(self.read(root / "opt/bullpen/app/VERSION").strip(), "1.0.1")
        self.assertEqual(self.read(root / "opt/bullpen/src/VERSION").strip(), "1.0.1")
        self.assertIn("BULLPEN_VERSION='1.0.1'", self.read(root / "opt/bullpen/release.env"))

        # A newer release that does not start: the previous version comes back.
        run = self.run_cmd(update, self.env(container="100", BULLPEN_TEST_LATEST="v1.0.2", BULLPEN_TEST_HEALTH_FAIL="1.0.2"))
        self.assertNotEqual(run.rc, 0)
        self.assertIn("the previous version is running again", run.out)
        self.assertIn("BULLPEN_VERSION='1.0.1'", self.read(root / "opt/bullpen/release.env"))
        self.assertEqual(self.read(root / "opt/bullpen/src/VERSION").strip(), "1.0.1")
        self.assertEqual(self.read(data), "my job history")

        # A local copy (what upgrade-hub-lxc.sh sends) always installs.
        shutil.copy(Releases.path() / "v1.0.2/agent-bullpen-1.0.2.tar.gz", root / "root/src.tar.gz")
        run = self.run_cmd(update, self.env(container="100", BULLPEN_LOCAL_SRC="/root/src.tar.gz"))
        self.assert_ok(run)
        self.assertIn("Updated to 1.0.2", run.out)
        self.assertIn("BULLPEN_SOURCE_KIND='local'", self.read(root / "opt/bullpen/release.env"))
        self.assert_clean_logs(run)

    def test_install_hub_by_hand_interactive(self):
        import pty
        import select
        import time
        root = self.tmp / "debian"
        for d in ("etc/systemd/system", "usr/bin", "etc/apt/sources.list.d"):
            (root / d).mkdir(parents=True)
        env = self.env(BULLPEN_TEST_ROOT=str(root))
        env["PATH"] = f"{STUBS / 'common'}:{root / 'usr/bin'}:{SYS_PATH}"
        answers = [("Port for the hub (1024-65535) [8080]: ", "9000"),
                   ("Password for the dashboard (leave blank to generate one): ", "my dashboard pw"),
                   ("Address for machines [http://192.168.100.50:9000]: ", ""),
                   ("Public address (blank to skip, you can add it later): ", "https://hub.example.org"),
                   ("Admin keys (blank to skip, you can add them later): ", ""),
                   ("LiteLLM dashboard address if you set one up (blank to skip): ", ""),
                   ("Install cloudflared and connect a tunnel now? (y/N): ", "")]
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(self.tmp)
            os.execve("/bin/bash", ["bash", str(REPO / "scripts/linux/install-hub.sh")], env)
        output, pos, deadline = "", 0, time.time() + 60
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk.decode(errors="replace")
            if answers and answers[0][0] in output[pos:]:
                pos = output.index(answers[0][0], pos) + len(answers[0][0])
                os.write(fd, (answers.pop(0)[1] + "\n").encode())
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output[-3000:])
        self.assertEqual(answers, [], "not every question was asked")
        text = self.read(root / "etc/bullpen/hub.env")
        hub = dict(re.findall(r"^([A-Z_]+)='(.*)'$", text, re.M))
        self.assertEqual((hub["HUB_PORT"], hub["HUB_UI_PASSWORD"]), ("9000", "my dashboard pw"))
        self.assertEqual(hub["HUB_RUNNER_URL"], "http://192.168.100.50:9000")
        self.assertEqual(hub["HUB_PUBLIC_URL"], "https://hub.example.org")
        self.assertNotIn("my dashboard pw", output)  # typed without echo
        self.assertIn("Add machines: open the dashboard, then Add a machine.", output)
        self.assertIn(hub["HUB_RUNNER_TOKEN"], output)
        self.assertIn("BULLPEN_SOURCE_KIND='local'", self.read(root / "opt/bullpen/release.env"))
        self.assertFalse((root / "usr/bin/update").exists())

    def test_connect_a_tunnel_later(self):
        self.install_default()
        root = self.rootfs()
        token = "eyJhIjoiYWJjZGVmZ2hpamtsbW5vcCIsInQiOiJsYXRlciJ9"
        self.secrets.append(token)
        script = root / "opt/bullpen/src/scripts/linux/install-hub.sh"
        run = self.run_cmd(f"bash {script}", self.env(container="100", BULLPEN_NONINTERACTIVE="1",
                                                       CF_TUNNEL_TOKEN=token))
        self.assert_ok(run)
        self.assertIn(f"TUNNEL_TOKEN='{token}'", self.read(root / "etc/cloudflared/bullpen-tunnel.env"))
        release = self.read(root / "opt/bullpen/release.env")
        self.assertIn("BULLPEN_SOURCE_KIND='release'", release)
        self.assertIn("BULLPEN_SOURCE_REF='v1.0.0'", release)
        self.assertNotIn(token, run.out)
        self.assert_clean_logs(run)

    def test_update_follows_a_branch(self):
        self.install_default()
        run = self.run_cmd(f"bash {self.rootfs()}/usr/bin/update", self.env(container="100", BULLPEN_REF="main"))
        self.assert_ok(run)
        self.assertIn("Updating to the newest 'main' (not a release, so not verified)", run.out)
        self.assertIn("BULLPEN_TRACK='main'", self.read(self.rootfs() / "opt/bullpen/release.env"))

    def test_shellcheck(self):
        shellcheck = os.environ.get("SHELLCHECK") or shutil.which("shellcheck")
        if not shellcheck:
            self.skipTest("shellcheck not found (set SHELLCHECK=/path/to/shellcheck)")
        files = SHELL_FILES + [str(p.relative_to(REPO)) for p in sorted(STUBS.glob("*/*"))
                               if p.read_text().startswith("#!/usr/bin/env bash")]
        proc = subprocess.run([shellcheck, "-x", "-S", "style", *files], cwd=REPO, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    if os.geteuid() != 0:
        # The installer refuses to run without root, as it must on a Proxmox host. The tests keep
        # everything in a fake root and stand-in commands, so running them as root changes nothing.
        raise SystemExit("Run these tests as root, for example: sudo python3 tests/proxmox/test_installer.py")
    unittest.main(verbosity=2)
