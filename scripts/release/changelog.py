#!/usr/bin/python3 -I
"""CHANGELOG.md helper (Keep a Changelog 1.1.0 + SemVer 2.0.0).

  changelog.py plan [--bump auto|patch|minor|major] [--first X.Y.Z]
      -> prints JSON {"version", "previous", "notes"} for the section to release
  changelog.py finalize VERSION DATE
      -> dates that section "## [VERSION] - DATE", starts a fresh "## [Unreleased]"
         above it, and writes VERSION into every VERSION_FILE
  changelog.py notes VERSION [--repo OWNER/NAME]
      -> prints that version's section (the release page). With --repo, relative
         links become links to the files at that tag
  changelog.py autoline TEXT
      -> adds "- TEXT" under "### Changed" for an automatic patch release (TEXT
         must start with AUTO_PREFIX); refuses when a person wrote notes there
  changelog.py verify-version VERSION
      -> exit 1 unless every VERSION_FILE holds VERSION
  (each takes --conf FILE; the default is private/release.conf)

The section to release is the newest one, headed "## [Unreleased]", or
"## [X.Y.Z] - Unreleased" to choose the number by hand. Only the newest section may
be unreleased. It must start with an "**Upgrading:**" line and have ### headings.

"auto" bump: **Breaking** anywhere, or a "### Removed" heading -> major (minor before
1.0.0 when BREAKING_BEFORE_1=minor); Added/Changed/Deprecated -> minor; only
Fixed/Security -> patch. The number goes up from the newer of the last released
section and the newest vX.Y.Z tag, and a version that is already tagged is refused.
"""
import json
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import release_conf  # noqa: E402

PATH = "CHANGELOG.md"
HEAD = re.compile(r"^## \[([^\]]+)\](?:[ \t]*-[ \t]*(\S+))?[ \t]*$", re.M)
LINK_LINE = re.compile(r"^\[[^\]]+\]:\s+\S", re.M)
VERSION = re.compile(r"\d+\.\d+\.\d+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
BUMPS = ("auto", "patch", "minor", "major")


class ChangelogError(Exception):
    pass


def sections(text):
    """[(name, date, body, start, end)] in file order. A body ends at the next "## "
    heading or at the link list at the bottom."""
    found = list(HEAD.finditer(text))
    out = []
    for i, m in enumerate(found):
        end = found[i + 1].start() if i + 1 < len(found) else len(text)
        body = text[m.end():end]
        link = LINK_LINE.search(body)
        if link:
            body = body[:link.start()]
        out.append((m.group(1), m.group(2), body.strip("\n"), m.start(), end))
    return out


def parse_version(v):
    if not VERSION.fullmatch(v or ""):
        raise ChangelogError("not a version: %r" % v)
    return tuple(int(x) for x in v.split("."))


def is_unreleased(name, date):
    return name.lower() == "unreleased" or (VERSION.fullmatch(name) and (date or "").lower() == "unreleased")


def pending(text):
    """(name, date, body, start, end) of the section to release. Raises if there is none,
    or if an unreleased section sits below a released one."""
    secs = sections(text)
    if not secs:
        raise ChangelogError("CHANGELOG.md has no ## sections")
    first = secs[0]
    if not is_unreleased(first[0], first[1]):
        raise ChangelogError("CHANGELOG.md has no [Unreleased] section at the top")
    for name, date, *_ in secs[1:]:
        if not (VERSION.fullmatch(name) and date and DATE.fullmatch(date)):
            raise ChangelogError("'## [%s]' is below the newest section and isn't released: move its "
                                 "notes into the top section" % name)
    return first


def previous_version(text):
    for name, date, *_ in sections(text):
        if VERSION.fullmatch(name) and date and DATE.fullmatch(date):
            return name
    return None


def strip_comments(body):
    return re.sub(r"<!--.*?-->", "", body, flags=re.S)


def check_body(body):
    visible = strip_comments(body)
    lines = [l for l in visible.splitlines() if l.strip()]
    if not lines:
        raise ChangelogError("nothing to release: the newest section is empty")
    if not lines[0].startswith("**Upgrading:**"):
        raise ChangelogError("the release notes must start with an '**Upgrading:**' line "
                             "(for example '**Upgrading:** nothing to do.')")
    if not re.search(r"^### ", visible, re.M):
        raise ChangelogError("the newest section has no ### Added/Changed/Fixed/... entries")


def bump_kind(body, base=(1, 0, 0), breaking_before_1="major"):
    visible = strip_comments(body)
    if "**Breaking**" in visible or re.search(r"^### Removed\b", visible, re.M):
        return "minor" if base[0] == 0 and breaking_before_1 == "minor" else "major"
    if re.search(r"^### (Added|Changed|Deprecated)\b", visible, re.M):
        return "minor"
    if re.search(r"^### (Fixed|Security)\b", visible, re.M):
        return "patch"
    raise ChangelogError("can't tell the version bump from the ### headings in the newest section")


def raise_number(base, kind):
    major, minor, patch = base
    return {"major": "%d.0.0" % (major + 1), "minor": "%d.%d.0" % (major, minor + 1),
            "patch": "%d.%d.%d" % (major, minor, patch + 1)}[kind]


def plan(text, bump="auto", first="0.1.0", tags=(), breaking_before_1="major"):
    if bump not in BUMPS:
        raise ChangelogError("--bump must be one of %s" % ", ".join(BUMPS))
    name, _date, body, _start, _end = pending(text)
    check_body(body)
    prev = previous_version(text)
    released = sorted(parse_version(t[1:]) for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t))
    if VERSION.fullmatch(name):                  # "## [X.Y.Z] - Unreleased": chosen by hand
        version = name
    elif prev is None and not released:
        version = first
        parse_version(version)
    else:
        base = max(([parse_version(prev)] if prev else []) + released[-1:])
        kind = bump_kind(body, base, breaking_before_1) if bump == "auto" else bump
        version = raise_number(base, kind)
    if "v" + version in tags:
        raise ChangelogError("v%s is already tagged" % version)
    if released and parse_version(version) <= released[-1]:
        raise ChangelogError("%s isn't newer than the newest tag, v%s"
                             % (version, ".".join(map(str, released[-1]))))
    if prev and parse_version(version) <= parse_version(prev):
        raise ChangelogError("%s isn't newer than the last released section, %s" % (version, prev))
    return {"version": version, "previous": prev, "notes": body}


def finalize(text, version, date, links_repo=None):
    parse_version(version)
    if not DATE.fullmatch(date):
        raise ChangelogError("the date must look like YYYY-MM-DD")
    name, _d, body, start, _end = pending(text)
    check_body(body)
    if VERSION.fullmatch(name) and name != version:
        raise ChangelogError("the newest section names %s, not %s" % (name, version))
    if any(n == version and d and DATE.fullmatch(d) for n, d, *_ in sections(text)):
        raise ChangelogError("CHANGELOG.md already has a [%s] section" % version)
    line_end = text.find("\n", start)
    line_end = len(text) if line_end < 0 else line_end
    text = text[:start] + "## [Unreleased]\n\n## [%s] - %s" % (version, date) + text[line_end:]
    if links_repo:
        link = "[%s]: https://github.com/%s/releases/tag/v%s" % (version, links_repo, version)
        text = re.sub(r"^\[Unreleased\]:.*\n?", "", text, flags=re.M | re.I)
        if not re.search(r"^\[%s\]:" % re.escape(version), text, re.M):
            m = LINK_LINE.search(text)
            text = (text[:m.start()] + link + "\n" + text[m.start():]) if m else \
                (text.rstrip("\n") + "\n\n" + link + "\n")
    return text


def notes(text, version, repo=None):
    for name, date, body, *_ in sections(text):
        if name == version and not is_unreleased(name, date):
            if repo:
                body = re.sub(r"\]\((?![a-z][a-z0-9+.-]*:|#|/)([^)\s]+)\)",
                              lambda m: "](https://github.com/%s/blob/v%s/%s)" % (repo, version, m.group(1)),
                              body, flags=re.I)
            return body
    raise ChangelogError("no released [%s] section" % version)


def autoline(text, line, prefix):
    """Add an automatic release line to the newest section, or refuse if a person wrote
    something there. It may hold only the Upgrading line, a "### Changed" heading and
    lines that start with "- " plus PREFIX (the ones this function wrote)."""
    if not prefix:
        raise ChangelogError("AUTO_PREFIX isn't set in release.conf, so there are no automatic lines")
    if not line.startswith(prefix) or "\n" in line:
        raise ChangelogError("an automatic line must start with AUTO_PREFIX")
    name, date, body, start, end = pending(text)
    for l in body.splitlines():
        if not l.strip() or l.startswith("**Upgrading:**") or l.strip() == "### Changed":
            continue
        if l.startswith("- " + prefix):
            continue
        raise ChangelogError("the newest section has notes written by hand; release it by hand")
    if "- " + line in body.splitlines():
        return text
    new_body = body if body.strip() else "**Upgrading:** nothing to do."
    if not new_body.startswith("**Upgrading:**"):
        new_body = "**Upgrading:** nothing to do.\n\n" + new_body
    if "### Changed" not in new_body:
        new_body = new_body.rstrip("\n") + "\n\n### Changed\n"
    new_body = new_body.rstrip("\n") + "\n- " + line + "\n"
    head = re.match(r"^## \[[^\n]*\n?", text[start:]).group(0)
    rest = text[end:]
    return text[:start] + head.rstrip("\n") + "\n\n" + new_body + ("\n" if rest.strip() else "") + \
        rest.lstrip("\n")


def get_versions(conf, root="."):
    """{path: version} for every VERSION_FILE; raises unless each holds exactly one."""
    out = {}
    for path, rx in conf.version_files():
        with open(os.path.join(root, path), encoding="utf-8", newline="") as f:
            src = f.read()
        if rx is None:
            if not re.fullmatch(r"\d+\.\d+\.\d+\s*", src):
                raise ChangelogError("%s must hold exactly one version number" % path)
            out[path] = src.strip()
        else:
            found = rx.findall(src)
            if len(found) != 1:
                raise ChangelogError("%s must have exactly one version line" % path)
            out[path] = found[0]
    return out


def set_version(source, version, rx, path="the version file"):
    parse_version(version)
    if rx is None:
        if not re.fullmatch(r"\d+\.\d+\.\d+\s*", source):
            raise ChangelogError("%s must hold exactly one version number" % path)
        return version + "\n"
    found = list(rx.finditer(source))
    if len(found) != 1:
        raise ChangelogError("%s must have exactly one version line" % path)
    m = found[0]
    return source[:m.start(1)] + version + source[m.end(1):]


def git_tags():
    try:
        out = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True, text=True,
                             check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {t.strip() for t in out.split() if t.strip()}


def option(argv, name, default=None):
    if name not in argv:
        return default
    i = argv.index(name)
    if i + 1 >= len(argv):
        raise ChangelogError("%s needs a value" % name)
    value = argv[i + 1]
    del argv[i:i + 2]
    return value


def main(argv):
    argv = list(argv)
    try:
        conf = release_conf.Conf.load(option(argv, "--conf", release_conf.DEFAULT_PATH))
        if len(argv) < 2:
            raise ChangelogError("usage: see the top of scripts/release/changelog.py")
        command = argv[1]
        if command == "verify-version":
            want = argv[2]
            versions = get_versions(conf)
            for path, have in versions.items():
                if have != want:
                    raise ChangelogError("%s holds %s, not %s" % (path, have, want))
            print("changelog: every version file holds %s (%d file(s))" % (want, len(versions)))
            return 0
        with open(PATH, encoding="utf-8", newline="") as f:
            text = f.read()
        if command == "plan":
            bump = option(argv, "--bump", "auto")
            first = option(argv, "--first", conf.get("FIRST_VERSION"))
            print(json.dumps(plan(text, bump, first, git_tags(), conf.get("BREAKING_BEFORE_1"))))
        elif command == "finalize":
            version, date = argv[2], argv[3]
            links = conf.public_repo if conf.flag("CHANGELOG_LINKS") else None
            new = finalize(text, version, date, links)
            files = {}
            for path, rx in conf.version_files():          # check every file before writing any
                with open(path, encoding="utf-8", newline="") as f:
                    files[path] = set_version(f.read(), version, rx, path)
            with open(PATH, "w", encoding="utf-8", newline="") as f:
                f.write(new)
            for path, src in files.items():
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(src)
                print("%s: version = %s" % (path, version))
            print("CHANGELOG.md: newest section -> [%s] - %s" % (version, date))
        elif command == "notes":
            repo = option(argv, "--repo")
            print(notes(text, argv[2], repo))
        elif command == "autoline":
            with open(PATH, "w", encoding="utf-8", newline="") as f:
                f.write(autoline(text, argv[2], conf.get("AUTO_PREFIX")))
            print("CHANGELOG.md: added the automatic line")
        else:
            raise ChangelogError("unknown command %s" % command)
    except (ChangelogError, release_conf.ConfError, IndexError, OSError) as e:
        print("changelog: %s" % (e if not isinstance(e, IndexError) else "missing argument"),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
