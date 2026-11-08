#!/usr/bin/env python3
"""Checks that keep the repository ready to publish. Standard library only.

    python3 tests/check_repo.py               run every check
    python3 tests/check_repo.py --tag v1.0.0  also check that a release tag matches (used by
                                              .github/workflows/release.yml)

It checks that:

1. the version numbers agree: VERSION, hub/app.py, runner/runner.py, and that CHANGELOG.md has
   a section for that version (with --tag, a dated one, and the tag must match too);
2. no personal references are left (DENYLIST below, plus tests/denylist.local and
   $BULLPEN_DENYLIST if there are any). A private entry is named by its number, and anything it
   matches, in any message, is printed as <private>;
3. every email, IP and MAC address, in a file or in its name, is one of the allowed examples:
   hi@rpaprocki.com or an example.com address; private addresses only from 192.168.100.0/24, public
   ones only from the documentation ranges (192.0.2.0/24, 2001:db8::/32 and the like) or a few
   well-known resolvers; MAC addresses only from 00:00:5E:00:53:xx. The message names the file, line
   and kind, never the value (a file name that holds one is printed with the value masked);
4. every relative link in a Markdown file points to a file that exists, and every #anchor to a
   heading that exists;
5. the hub masks secrets with the same patterns as the runner (hub/redact.py and runner/runner.py
   each keep a copy).

Checks 2 and 3 leave private/ alone, and the paths private/public-exclude.txt lists. They read
UTF-8 text only, so any other file (binary, UTF-16, latin-1) is a problem, named by its path.

Problems are printed as "file:line: message" and make the exit code 1. Warnings don't.
"""

from __future__ import annotations

import ast
import functools
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_SLUG = "rob-paprocki/agent-bullpen"  # scripts/release/set-repo.sh rewrites this line too

# Personal references that must not appear. Each entry is (regular expression, what to write
# instead). Matching is case-sensitive unless the expression says otherwise with (?i). The owner's
# name is public anyway (LICENSE, the repository's address); keep anything more private out of this
# file and put it in one of these, which are read too and never printed:
#   tests/denylist.local   one regular expression per line, # for comments (git ignores this file)
#   BULLPEN_DENYLIST         the same, in an environment variable (CI reads it from a repository secret)
DENYLIST: list[tuple[str, str]] = [
    (r"rob-paprocki", f"the repository slug only, as {REPO_SLUG}"),
    (r"\bRob\b", "a neutral word such as 'the user'"),
    (r"Paprocki", "a neutral word"),
]
LOCAL_DENYLIST = ROOT / "tests" / "denylist.local"
PRIVATE_HINT = "a neutral word (it matches a word on your private list)"
# Files the denylist doesn't apply to: the licence (the copyright holder's name), FUNDING.yml (it
# names the owner's public funding accounts on purpose, as LICENSE names the copyright holder) and
# the changelog (history), this file (it holds the list), and set-repo.sh (it may name old owners).
# The personal-value check (emails, IP and MAC addresses) still covers them all.
DENYLIST_SKIP = {"LICENSE", ".github/FUNDING.yml", "CHANGELOG.md", "tests/check_repo.py",
                 "scripts/release/set-repo.sh", "tests/denylist.local"}

# Files other parts of the project add. A link to one of these that is missing is a warning,
# not an error, so the docs can be written before the file lands.
PENDING_FILES = {"ct/bullpen.sh", "docs/05-linux-and-macos-machines.md", "tests/proxmox/run.sh"}

SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}

errors: list[str] = []
warnings: list[str] = []


def error(msg: str) -> None:
    errors.append(hide_private(mask(msg)))


def warn(msg: str) -> None:
    warnings.append(hide_private(mask(msg)))


def tracked_files() -> list[str]:
    """Repository files, relative, with / separators: git's list, or a walk without git."""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
        files = [f for f in out.stdout.decode("utf-8", "replace").split("\0") if f]
        if files:
            return sorted(f for f in files if (ROOT / f).is_file())
    except (OSError, subprocess.CalledProcessError):
        pass
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            found.append((Path(dirpath) / name).relative_to(ROOT).as_posix())
    return sorted(found)


def read_text(rel: str) -> str | None:
    """The file's text, or None when it isn't UTF-8 text (a NUL byte, as in binary and UTF-16 files, or
    bytes UTF-8 can't decode). check_personal_values reports those outside private/."""
    data = (ROOT / rel).read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ------------------------------------------------------------------ versions
def code_version(rel: str) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    m = re.search(r'^VERSION = "([^"]+)"', text, re.M)
    if not m:
        error(f"{rel}: no line like VERSION = \"X.Y.Z\"")
        return ""
    return m.group(1)


def check_versions(tag: str | None) -> None:
    path = ROOT / "VERSION"
    if not path.exists():
        error("VERSION: file missing")
        return
    version = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        error(f"VERSION: '{version}' isn't X.Y.Z")
    for rel in ("hub/app.py", "runner/runner.py"):
        found = code_version(rel)
        if found and found != version:
            error(f"{rel}: VERSION is {found}, but the VERSION file says {version}")
    if tag is not None and tag != f"v{version}":
        error(f"the tag is {tag}, but the VERSION file says {version} (expected v{version})")
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.exists():
        error("CHANGELOG.md: file missing")
        return
    heading = re.search(rf"^## \[{re.escape(version)}\](.*)$", changelog.read_text(encoding="utf-8"), re.M)
    if not heading:
        error(f"CHANGELOG.md: no section '## [{version}]'")
    elif tag is not None and not re.match(r"^ - \d{4}-\d{2}-\d{2}\s*$", heading.group(1)):
        error(f"CHANGELOG.md: the [{version}] heading needs its release date: '## [{version}] - YYYY-MM-DD'")
    check_changelog_sections(changelog.read_text(encoding="utf-8"), tag)


DATED_HEADING = re.compile(r"^## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}(?:\s|$)")
PENDING_HEADING = re.compile(r"^## (?:\[Unreleased\]|\[\d+\.\d+\.\d+\] - Unreleased)\s*$")


def check_changelog_sections(text: str, tag: str | None) -> None:
    """Only the newest section may be unreleased, in one of the forms the release scripts read
    (scripts/release/next-version.py); with --tag, none may be, or code would ship without its notes."""
    headings = [(n, line.rstrip()) for n, line in enumerate(text.splitlines(), 1) if line.startswith("## ")]
    for i, (n, heading) in enumerate(headings):
        if DATED_HEADING.match(heading):
            continue
        if i > 0:
            error(f"CHANGELOG.md:{n}: '{heading}' is below the newest section and isn't released: "
                  "move its notes into the top section")
        elif not PENDING_HEADING.match(heading):
            error(f"CHANGELOG.md:{n}: '{heading}' should be '## [Unreleased]', '## [X.Y.Z] - Unreleased' "
                  "or '## [X.Y.Z] - YYYY-MM-DD'")
        elif tag is not None:
            error(f"CHANGELOG.md:{n}: '{heading}' has notes that aren't in this release: release them "
                  "together (date this section as the release) or move them out first")


# ------------------------------------------------------------- personal refs
def private_patterns() -> list[str]:
    """The extra expressions from tests/denylist.local and $BULLPEN_DENYLIST (blank lines and # skipped)."""
    lines = os.environ.get("BULLPEN_DENYLIST", "").splitlines()
    if LOCAL_DENYLIST.is_file():
        lines += LOCAL_DENYLIST.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


@functools.cache
def private_entries() -> tuple[tuple[int, re.Pattern | None], ...]:
    """(entry number, its expression, or None when it isn't a valid regular expression) for each private
    entry, read once."""
    entries = []
    for n, rx in enumerate(private_patterns(), 1):
        try:
            entries.append((n, re.compile(rx)))
        except re.error:
            entries.append((n, None))
    return tuple(entries)


def hide_private(text: str) -> str:
    """The text with everything a private entry matches replaced by <private>, as
    private/release/public_copy.py shows it. Every message goes through this."""
    spans = sorted(m.span() for _, rx in private_entries() if rx is not None
                   for m in rx.finditer(text) if m.end() > m.start())
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, pos = [], 0
    for start, end in merged:
        out += [text[pos:start], "<private>"]
        pos = end
    return "".join(out) + text[pos:]


PRIVATE_EXCLUDE = ROOT / "private" / "public-exclude.txt"


def private_paths() -> list[str]:
    """Paths the word check leaves alone: private/ and what private/public-exclude.txt lists (one
    repository-relative path per line, # comments and blank lines ignored, a trailing / means a folder)."""
    paths = ["private/"]
    if PRIVATE_EXCLUDE.is_file():
        for line in PRIVATE_EXCLUDE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                paths.append(line)
    return paths


def is_private(rel: str, paths: list[str]) -> bool:
    return any(rel == p or (p.endswith("/") and rel.startswith(p)) for p in paths)


def check_denylist(files: list[str]) -> None:
    private = private_paths()
    patterns = [(re.compile(rx), None, hint) for rx, hint in DENYLIST]
    for n, rx in private_entries():
        if rx is None:  # Python's reason isn't shown: it can quote part of the entry
            error(f"private denylist entry #{n} isn't a valid regular expression")
        else:
            patterns.append((rx, n, PRIVATE_HINT))
    for rel in files:
        if rel in DENYLIST_SKIP or is_private(rel, private):
            continue
        text = read_text(rel)
        if text is None:
            continue  # not UTF-8 text: check_personal_values reports it
        for lineno, line in enumerate(text.splitlines(), 1):
            plain = line.replace(REPO_SLUG, "")
            for rx, n, hint in patterns:
                m = rx.search(plain)
                if m:
                    # A private entry is named by its number, never by what it matched.
                    what = f"'{m.group(0)}'" if n is None else f"(private entry #{n})"
                    error(f"{rel}:{lineno}: personal reference {what}: use {hint}")


# ---------------------------------------------------------- personal values
# Email, IP and MAC addresses are caught by pattern, so the private word list only needs what no
# pattern can catch. Only the examples below are allowed. Private addresses are allowed in one example
# network only, so a real one is caught without being named here (this file is public).
# An email address is [A-Za-z0-9._%+-]+@ and a dotted domain ending in letters. email_matches finds them
# from each @, since a regular expression would search a long run of letters again from every position.
EMAIL_LOCAL_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._%+-")
EMAIL_DOMAIN_RE = re.compile(r"(?:[A-Za-z0-9-]+\.)+[A-Za-z]+")
ALLOWED_EMAILS = {"hi@rpaprocki.com", "git@github.com",
                  # The [bot] part keeps the pattern from matching this one at all; listed in case it changes.
                  "41898282+github-actions[bot]@users.noreply.github.com"}
PLACEHOLDER_DOMAINS = ("example.com", "example.org", "example.net")  # and their subdomains
PLACEHOLDER_TLDS = (".invalid", ".test", ".example")
# Not an email: a systemd instance name such as container-getty@1.service, or its drop-in folder (.d).
SYSTEMD_UNIT_RE = re.compile(r"[0-9]+\.(?:service|socket|timer|target|mount|path)(?:\.d)?")

# Four numbers with no letter or digit right next to them and no further number in the same dotted run, so
# 1.2.3.4.5 and v1.2.3.4 are versions, while the address in <addr>.nip.io, app.<addr>.sslip.io,
# fixtures/<addr>.json, host_<addr> or at the end of a sentence is still found.
IPV4_RE = re.compile(r"(?<![A-Za-z0-9])(?<![0-9]\.)[0-9]{1,3}(?:\.[0-9]{1,3}){3}(?![A-Za-z0-9]|\.[0-9])")
# A reverse-DNS name lists the numbers backwards: 9.2.0.192.in-addr.arpa is 192.0.2.9, and a zone such as
# 2.0.192.in-addr.arpa (one to three numbers) names the network 192.0.2.0.
REVERSE_DNS_RE = re.compile(r"(?<![A-Za-z0-9])((?:[0-9]{1,3}\.){1,4})in-addr\.arpa", re.I)
IN_ADDR_ARPA_RE = re.compile(r"\.in-addr\.arpa", re.I)
# Hex groups and colons, two colons at least; ipaddress decides whether it is an address.
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,8}[0-9A-Fa-f]{0,4}(?![\w:])")
ALLOWED_NETWORKS = [ipaddress.ip_network(net) for net in (
    "192.168.100.0/24",                                   # the example network
    "127.0.0.0/8",                                        # loopback
    "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",  # documentation (RFC 5737)
    "2001:db8::/32",                                      # documentation (RFC 3849)
)]
ALLOWED_ADDRESSES = {ipaddress.ip_address(a) for a in (
    "0.0.0.0", "::", "::1", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9")}

MAC_RE = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
DOC_MAC_PREFIX = "00:00:5E:00:53:"  # the documentation range (RFC 7042)

EMAIL_HINT = "use hi@rpaprocki.com or an example.com address"
IP_HINT = "use an address in 192.168.100.0/24 (private) or 192.0.2.0/24 (public)"
MAC_HINT = "use 00:00:5E:00:53:xx"


def email_allowed(address: str, domain: str) -> bool:
    domain = domain.lower()
    return (address.lower() in ALLOWED_EMAILS
            or any(domain == d or domain.endswith("." + d) for d in PLACEHOLDER_DOMAINS)
            or domain.endswith(PLACEHOLDER_TLDS)
            or SYSTEMD_UNIT_RE.fullmatch(domain) is not None)


def ip_reported(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False  # not an address: a time (12:34:56), a version, a number over 255
    if address in ALLOWED_ADDRESSES or any(address in net for net in ALLOWED_NETWORKS):
        return False
    if address.is_reserved or address.is_multicast:
        # Netmasks and broadcast (240.0.0.0/4), multicast groups, and IPv6's unassigned blocks, which is
        # where Python slices such as lines[::2] land.
        return False
    # What is left can point at a real network: private, shared (CGNAT, Tailscale), link-local,
    # unique-local or global.
    return True


def email_matches(text: str):
    """(start, end, domain) of each email address, as re.finditer would give them, in linear time."""
    end = 0
    at = text.find("@")
    while at != -1:
        start = at
        while start > end and text[start - 1] in EMAIL_LOCAL_CHARS:
            start -= 1
        domain = EMAIL_DOMAIN_RE.match(text, at + 1) if start < at else None
        if domain:
            yield start, domain.end(), domain.group(0)
            end = domain.end()
            at = text.find("@", end)
        else:
            at = text.find("@", at + 1)


def personal_value_spans(text: str) -> list[tuple[int, int, str, str]]:
    """The (start, end, kind, hint) of each email, IP or MAC address in the text that isn't an allowed
    example, in the order they appear."""
    found: list[tuple[int, int, str, str]] = []
    for start, end, domain in email_matches(text):
        if not email_allowed(text[start:end], domain):
            found.append((start, end, "email address", EMAIL_HINT))
    for m in IPV4_RE.finditer(text):
        if not IN_ADDR_ARPA_RE.match(text, m.end()) and ip_reported(m.group(0)):
            found.append((m.start(), m.end(), "IP address", IP_HINT))
    for m in REVERSE_DNS_RE.finditer(text):
        labels = m.group(1).rstrip(".").split(".")
        if ip_reported(".".join(labels[::-1] + ["0"] * (4 - len(labels)))):
            found.append((m.start(1), m.end(1) - 1, "IP address", IP_HINT))
    for m in IPV6_RE.finditer(text):
        if ip_reported(m.group(0)):
            found.append((m.start(), m.end(), "IP address", IP_HINT))
    for m in MAC_RE.finditer(text):
        if not m.group(0).upper().replace("-", ":").startswith(DOC_MAC_PREFIX):
            found.append((m.start(), m.end(), "MAC address", MAC_HINT))
    return sorted(found)


def personal_values(line: str) -> list[tuple[str, str]]:
    """The (kind, hint) of each email, IP or MAC address in the line that isn't an allowed example, in
    the order they appear. The value itself is never returned."""
    return [(kind, hint) for _, _, kind, hint in personal_value_spans(line)]


def mask(text: str) -> str:
    """The text with each email, IP or MAC address that isn't an allowed example replaced by its kind, such
    as <IP address>. Every message goes through this, so a file name never prints the value it holds."""
    out, pos = [], 0
    for start, end, kind, _ in personal_value_spans(text):
        if start < pos:  # inside the value just masked (an address in an email's domain)
            pos = max(pos, end)
            continue
        out += [text[pos:start], f"<{kind}>"]
        pos = end
    return "".join(out) + text[pos:]


def a_kind(kind: str) -> str:
    return ("a " if kind.startswith("MAC") else "an ") + kind


def check_personal_values(files: list[str]) -> None:
    """Every file outside private/ and the excluded paths: its name, then each line. A file that isn't UTF-8
    text is a problem of its own, since neither this check nor the word check can read it. Messages name the
    file (masked by error()), the line and the kind, never the value."""
    private = private_paths()
    for rel in files:
        if is_private(rel, private):
            continue
        for kind, hint in personal_values(rel):
            error(f"{rel}: file name holds {a_kind(kind)} that isn't an allowed example: {hint}")
        text = read_text(rel)
        if text is None:
            error(f"{rel}: not UTF-8 text, so the word and personal-value checks can't read it: keep binary and "
                  "UTF-16 files out of the repository")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for kind, hint in personal_values(line):
                error(f"{rel}:{lineno}: {kind} that isn't an allowed example: {hint}")


# --------------------------------------------------------------------- links
FENCE_RE = re.compile(r"^\s*(```|~~~)")
INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
LINK_RE = re.compile(r"!?\[(?:[^\]\[]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFDEF_RE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
HTML_LINK_RE = re.compile(r"""<(?:a|img)\s[^>]*(?:href|src)=["']([^"']+)["']""", re.I)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lowercase, drop punctuation, spaces become hyphens."""
    text = re.sub(r"<[^>]+>", "", heading)              # inline HTML
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links keep their text
    text = text.replace("`", "").lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(rel: str, cache: dict[str, set[str]]) -> set[str]:
    if rel not in cache:
        found: set[str] = set()
        counts: dict[str, int] = {}
        text = read_text(rel) or ""
        in_fence = False
        for line in text.splitlines():
            if FENCE_RE.match(line):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = HEADING_RE.match(line)
            if m:
                base = slug(m.group(2))
                n = counts.get(base, 0)
                counts[base] = n + 1
                found.add(base if n == 0 else f"{base}-{n}")
            for name in re.findall(r"""<a\s+(?:name|id)=["']([^"']+)["']""", line):
                found.add(name)
        cache[rel] = found
    return cache[rel]


def link_targets(text: str):
    in_fence = False
    for lineno, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = INLINE_CODE_RE.sub("", line)
        for m in LINK_RE.finditer(stripped):
            yield lineno, m.group(1)
        m = REFDEF_RE.match(stripped)
        if m:
            yield lineno, m.group(1)
        for m in HTML_LINK_RE.finditer(stripped):
            yield lineno, m.group(1)


def check_links(files: list[str]) -> None:
    present = set(files)
    cache: dict[str, set[str]] = {}
    for rel in files:
        if not rel.endswith(".md"):
            continue
        text = read_text(rel)
        if text is None:
            continue
        here = Path(rel).parent
        for lineno, target in link_targets(text):
            if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("//"):
                continue  # https:, mailto: and the like
            path_part, _, anchor = target.partition("#")
            path_part = path_part.split("?", 1)[0]
            if path_part:
                dest = os.path.normpath((here / path_part).as_posix()).replace(os.sep, "/")
                if dest.startswith(".."):
                    error(f"{rel}:{lineno}: link '{target}' leaves the repository")
                    continue
                exists = dest in present or (ROOT / dest).is_dir() or (ROOT / dest).is_file()
                if not exists:
                    if dest in PENDING_FILES:
                        warn(f"{rel}:{lineno}: link to '{dest}', which another change adds (not there yet)")
                    else:
                        error(f"{rel}:{lineno}: link '{target}': {dest} doesn't exist")
                    continue
            else:
                dest = rel
            if anchor and dest.endswith(".md") and (ROOT / dest).is_file():
                if anchor.lower() not in anchors(dest, cache):
                    error(f"{rel}:{lineno}: link '{target}': no heading in {dest} makes the anchor #{anchor}")


# -------------------------------------------------------- redaction patterns
# The hub (approval lines, stored approval inputs) masks secrets with copies of the runner's
# patterns. Both files must define these names with the same re.compile(...) calls.
REDACTION_FILES = ("runner/runner.py", "hub/redact.py")
REDACTION_NAMES = ("SECRET_VALUE_RES", "AUTH_WORD_RE", "URL_USERINFO_RE", "NAMED_SECRET_RE")


def compiled_patterns(value: ast.expr) -> list | None:
    """The arguments of re.compile(...), or of each one in a list, as comparable values; None if
    the value is something else."""
    calls = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
    out = []
    for call in calls:
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "compile"
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "re"):
            return None
        try:
            args = [ast.literal_eval(a) for a in call.args]
        except ValueError:
            args = [ast.dump(a) for a in call.args]
        out.append((args, sorted(ast.dump(k) for k in call.keywords)))
    return out


def redaction_patterns(rel: str) -> dict[str, list | None]:
    tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
    found: dict[str, list | None] = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and target.id in REDACTION_NAMES and node.value is not None:
                found[target.id] = compiled_patterns(node.value)
    return found


def check_redaction_patterns() -> None:
    runner_rel, hub_rel = REDACTION_FILES
    if not all((ROOT / rel).exists() for rel in REDACTION_FILES):
        error(f"{hub_rel} or {runner_rel} is missing")
        return
    runner, hub = redaction_patterns(runner_rel), redaction_patterns(hub_rel)
    for name in REDACTION_NAMES:
        for rel, found in ((runner_rel, runner), (hub_rel, hub)):
            if found.get(name) is None:
                error(f"{rel}: no {name} = re.compile(...) at the top level")
        if runner.get(name) is not None and hub.get(name) is not None and runner[name] != hub[name]:
            error(f"{hub_rel}: {name} differs from {runner_rel}'s; the hub must mask secrets with the same "
                  "patterns as the runner (change both)")


# --------------------------------------------------------------- reminders
def check_reminders() -> None:
    coc = ROOT / "CODE_OF_CONDUCT.md"
    if coc.exists() and "[INSERT CONTACT METHOD]" in coc.read_text(encoding="utf-8"):
        warn("CODE_OF_CONDUCT.md: the contact for reports is still '[INSERT CONTACT METHOD]'")


def main(argv: list[str]) -> int:
    tag = None
    if argv[:1] == ["--tag"] and len(argv) == 2:
        tag = argv[1]
    elif argv:
        print(__doc__.strip())
        return 2
    files = tracked_files()
    check_versions(tag)
    check_denylist(files)
    check_personal_values(files)
    check_links(files)
    check_redaction_patterns()
    check_reminders()
    for msg in warnings:
        print(f"warning: {msg}")
    for msg in errors:
        print(msg)
    if errors:
        print(f"\n{len(errors)} problem(s) found.")
        return 1
    print(f"Repository checks passed ({len(files)} files, {len(warnings)} warning(s)).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
