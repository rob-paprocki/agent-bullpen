# Contributing to Agent Bullpen

Thanks for wanting to help. Agent Bullpen is a small project, and the most useful help is often not code.

## What helps most

1. **Running it on real hardware and saying what happened.** The installers have been tested with stand-in
   commands, not on every system they support. A report from a real Proxmox VE host, a Mac, a Fedora or Arch
   machine, or Windows 10 is worth a lot, even when everything worked.
2. **Clear bug reports.** Use the issue form. Include the version, what you did, and the log lines. Remove
   passwords, tokens, API keys and enrollment codes first.
3. **Fixing the guides.** If a step confused you, it will confuse the next person. Small fixes are welcome
   without an issue.
4. **Support for another agent CLI.** Open an issue first ("Support another agent CLI"), so we can agree on the
   approach. [Guide 11](docs/11-adding-agents.md) and the [roadmap](ROADMAP.md) explain what is involved.

For a security problem, don't open an issue: follow [SECURITY.md](SECURITY.md).

## Ground rules

- **Keep changes small and focused.** One pull request per fix or feature is easier to review and to undo.
- **No telemetry, ever.** Agent Bullpen sends nothing to this project or anyone else. Installers download only
  from GitHub, the system's package mirrors and the official source of each tool the user chooses to install.
  Anything new that goes over the network must be something the user set up.
- **No secrets and no personal details** in code, tests or docs: no real names, machine names, addresses,
  keys or tokens. Use neutral examples such as `build-box`, `claude-work`, `myapp` and `192.168.100.20`.
  Private addresses in examples come from `192.168.100.0/24`, and the only email address is
  `hi@rpaprocki.com`; otherwise use an `example.com` address. `tests/check_repo.py` checks both, and MAC
  addresses too. It also catches some of the rest. To have it look for words of your own (a machine or
  project name, say) without publishing them, put one regular expression per line in
  `tests/denylist.local`, which git ignores. CI reads the same list from a repository secret called
  `BULLPEN_DENYLIST`, if there is one.
- **The runner stays standard-library Python**, working on Python 3.9 and newer, on Linux, macOS and Windows.
  It is copied to every machine, so it can't depend on packages.
- **The hub supports Python 3.11 and newer.** New Python packages for the hub need a good reason.
- **The dashboard escapes what it didn't write.** Every string an agent, a runner or a CLI supplies (tool
  names and inputs, titles, error texts, commands, tags, account labels) goes through `esc()` in
  `hub/static/app.js`. `md()` is only for answer bodies: the agent's results and turns.
- **Shell scripts pass shellcheck** and stay readable for someone who isn't a programmer: they say what they
  do, ask before they change anything, and can be run again safely.
- **The providers' rules come first.** Agent Bullpen runs the vendors' own, unmodified programs, signed in by
  their owners. Features that pool accounts, switch accounts when one hits its limit, or pass one person's
  subscription to others won't be accepted ([guide 1](docs/01-accounts-and-rules.md)). Usage figures (plan
  limits, tokens per job) are for display only: no code may choose, hold back or move work because of them.

## How the docs are written

The guides are read by people who are technical but not programmers, often step by step while they set
things up. Keep the existing voice:

- plain English and short sentences;
- numbered steps for anything you do, one action per step, and one command per code block;
- the reason in a short clause ("so the machine stays reachable"), not a paragraph;
- no marketing words and no emoji;
- every new setting or command is explained where it is first used;
- facts about other products (flags, plans, rules) carry a link, and dated pages say when they were last
  checked.

## Setting up a development copy

You need Git, Python 3.11 or newer, bash and curl, on Linux (or WSL on Windows). The end-to-end test runs
the Linux setup script and the Proxmox installer's tests use bash stand-ins for Proxmox's tools, so they are
Linux-only. CI also checks that the runner starts on Windows and macOS.

1. Get the code:

   ```bash
   git clone https://github.com/rob-paprocki/agent-bullpen.git
   ```

   ```bash
   cd agent-bullpen
   ```

2. Make a private Python environment with the hub's packages:

   ```bash
   python3 -m venv .venv
   ```

   ```bash
   .venv/bin/pip install -r hub/requirements.txt
   ```

3. Run the checks. All of them also run in CI on every push and pull request
   ([.github/workflows/ci.yml](.github/workflows/ci.yml)):

   | Check | Command | Takes |
   |---|---|---|
   | End-to-end test: a hub, two runners and a fake agent, with no AI service called | `.venv/bin/python tests/smoke_test.py` | about 2 minutes |
   | The hub's database, shared helpers and usage figures, and a few parts of the web app | `.venv/bin/python tests/test_hub_units.py` | seconds |
   | The runner on its own: jobs with the fake agent and a stand-in for the hub | `python3 tests/test_runner_units.py` | seconds |
   | Versions, personal references, links in the docs | `python3 tests/check_repo.py` | seconds |
   | The release scripts: how the next version is picked, and a release in a throwaway copy | `python3 tests/test_release.py` | seconds |
   | Shell scripts | `shellcheck --shell=bash scripts/*/*.sh` | seconds |
   | The Proxmox installer, with stand-in commands | `sudo python3 tests/proxmox/test_installer.py` (as root, like the installer itself; nothing outside a temporary folder is touched; [guide 4](docs/04-proxmox-hub.md#how-the-installer-is-built)) | under a minute |

**Trying the dashboard locally.** Start a hub with a throwaway password, token and data folder:

```bash
cd hub && HUB_UI_PASSWORD=dev HUB_RUNNER_TOKEN=dev-token HUB_DATA_DIR=/tmp/bullpen-dev ../.venv/bin/uvicorn app:app --port 8080
```

Then open a second terminal in the repository folder. Write a runner config with only the fake agent, and
start a runner that keeps its files in `/tmp` (so your own agents' settings are left alone):

```bash
mkdir -p /tmp/bullpen-dev/chat && printf '%s\n' '{"name": "dev", "hub_url": "http://127.0.0.1:8080", "token": "dev-token", "manage_agent_settings": false, "agents": {"selftest": {"preset": "selftest"}}, "projects": {"chat": "/tmp/bullpen-dev/chat"}}' > /tmp/bullpen-dev/runner.json
```

```bash
BULLPEN_HOME=/tmp/bullpen-dev/home python3 runner/runner.py --config /tmp/bullpen-dev/runner.json
```

Open `http://127.0.0.1:8080`, log in with `dev`, and send a task to `selftest`.

## Pull requests

1. Fork the repository and make a branch.
2. Make the change, with tests where it makes sense, and update the guides.
3. Add a line to the top section of `CHANGELOG.md`, the one that isn't released yet: `## [Unreleased]`,
   or `## [X.Y.Z] - Unreleased` if a maintainer has named the next version. Add `## [Unreleased]` only if
   the top section already has a date. Put the line under the heading that fits: `### Added`,
   `### Changed`, `### Deprecated`, `### Removed`, `### Fixed` or `### Security`. The release uses
   these headings to pick the next version. `tests/check_repo.py` fails if a second unreleased section
   appears.
4. Run the checks above.
5. Open the pull request. The template asks what you changed, why, and how you tested it. Say plainly what
   you could not test.

## Releasing

For maintainers. The hub and the runner always share one version number, because the hub hands its own copy
of the runner to every machine. Versions follow [Semantic Versioning](https://semver.org): a fix raises the
last number, a feature the middle one, and the first number goes up only for a change that stops something
working until people act. Steps that aren't breaking go in the **Upgrading** line of a patch or minor
release.

### How releases reach this repository

The maintainer makes releases from a separate development repository. Each one arrives here as one commit,
`Agent Bullpen X.Y.Z`, with its tag `vX.Y.Z`. The tag starts
[.github/workflows/release.yml](.github/workflows/release.yml), which publishes the release (step 6 below).
The version comes from the headings in `CHANGELOG.md`
([scripts/release/next-version.py](scripts/release/next-version.py)):

- notes only under `### Fixed` or `### Security` raise the last number (1.0.0 to 1.0.1);
- notes under `### Removed`, or an entry that starts with `**Breaking**` under any heading, raise the first
  one (1.0.0 to 2.0.0);
- any other notes raise the middle one (1.0.0 to 1.1.0);
- a heading written as `## [X.Y.Z] - Unreleased` (instead of `## [Unreleased]`) names the version itself.

Every release section starts with an **Upgrading** line, right under its heading: `**Upgrading:** nothing
to do.`, or `**Upgrading:**` followed by the steps (or a link to the guide that has them). A section
without it is not released. The line is not a note, so it never changes the number.

### By hand

1. Check that CI passes on `main`.
2. Write the release notes in `CHANGELOG.md`, under `## [Unreleased]` (or an existing
   `## [X.Y.Z] - Unreleased` heading). The section starts with the **Upgrading** line.
3. Set the new version everywhere and date the notes:

   ```bash
   python3 scripts/release/bump-version.py X.Y.Z
   ```

   It changes `VERSION`, `VERSION = "…"` in `hub/app.py` and `runner/runner.py`, and the changelog heading to
   `## [X.Y.Z] - <today>`. Then it runs `tests/check_repo.py --tag vX.Y.Z`. To do it by hand, change those
   four places yourself.
4. Run the end-to-end test once more, then commit:

   ```bash
   git commit -am "Release X.Y.Z"
   ```

5. Tag the commit and push both:

   ```bash
   git tag -a vX.Y.Z -m "Agent Bullpen X.Y.Z"
   ```

   ```bash
   git push origin main vX.Y.Z
   ```

6. The tag starts [.github/workflows/release.yml](.github/workflows/release.yml). It checks that the tag,
   `VERSION` and both code versions agree. Then it builds `agent-bullpen-X.Y.Z.tar.gz` (the repository at the
   tag, in a folder `agent-bullpen-X.Y.Z/`) and `SHA256SUMS`, and publishes them as a GitHub release with the
   changelog section as its notes.
7. Check the release page: it should have both files. The installers pick up the newest release on their
   own and check the download against `SHA256SUMS`.

If the workflow fails, fix the problem, delete the tag (`git tag -d vX.Y.Z` and
`git push origin :refs/tags/vX.Y.Z`), and tag again.

## Moving the repository

The installers, the docs and the release workflow name the repository as `rob-paprocki/agent-bullpen`. To move
the project to another owner or name:

1. See what would change:

   ```bash
   bash scripts/release/set-repo.sh --dry-run NEW_OWNER/NEW_REPO
   ```

2. Change it:

   ```bash
   bash scripts/release/set-repo.sh NEW_OWNER/NEW_REPO
   ```

3. Check the result with `git diff` and `python3 tests/check_repo.py`, then commit and push.

A fork that only wants to install from itself doesn't need this: the installers take `BULLPEN_REPO=owner/repo`
(and `BULLPEN_REF`) from the environment ([guide 4](docs/04-proxmox-hub.md)).

GitHub redirects the old address after a rename or a transfer, so existing installs keep finding updates.
This works as long as nobody creates a new repository under the old name. To point a hub at the new name, run
`BULLPEN_REPO=NEW_OWNER/NEW_REPO update` inside its container.

## Code of conduct

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).
