#!/usr/bin/env python3
"""Work out the next release from CHANGELOG.md.

    python3 scripts/release/next-version.py [--bump auto|patch|minor|major]

Prints the version to release, like 1.1.0, or nothing when there is nothing new to release (the reason
goes to stderr). In GitHub Actions it also writes version=... to $GITHUB_OUTPUT and a short summary to
$GITHUB_STEP_SUMMARY. It changes no files.

The newest section of CHANGELOG.md decides:

  "## [Unreleased]" with notes    the next version after VERSION (or after the newest tag, if higher):
                                  notes under ### Removed, or an entry starting "**Breaking**" under any
                                  heading, raise the first number (1.4.2 to 2.0.0); notes only under
                                  ### Fixed or ### Security the last one (1.4.2 to 1.4.3); anything else
                                  the middle one (1.4.2 to 1.5.0). --bump patch|minor|major overrides this.
  "## [X.Y.Z] - Unreleased"       X.Y.Z, as the person writing the notes chose it (--bump is ignored)
  "## [X.Y.Z] - YYYY-MM-DD"       nothing: it is already released

Only the newest section may be unreleased, and its heading must be one of those three. A version that
is already tagged is never chosen again. Exit status 1 means CHANGELOG.md and the tags disagree in a way
a person has to sort out; tests/check_repo.py checks the same rules on every pull request.

A section to release starts with its "**Upgrading:** ..." line: "nothing to do." or the steps. Without
it, the exit status is 1 too.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATCH_ONLY = {"fixed", "security"}
LINK_LINE = re.compile(r"^\[[^\]]+\]:\s+\S")
UPGRADING = re.compile(r"^\*\*Upgrading:\*\*\s+\S")
DATED = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - \d{4}-\d{2}-\d{2}(?:\s|$)")
FORMS = "'## [Unreleased]', '## [X.Y.Z] - Unreleased' or '## [X.Y.Z] - YYYY-MM-DD'"


class Problem(Exception):
    """CHANGELOG.md, VERSION and the tags disagree; a person has to fix it."""


def parse(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def newest_section(changelog: str) -> tuple[str, list[str]] | None:
    """The first "## " heading and its lines, up to the next "## " heading or the link list."""
    lines = changelog.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("## "):
            body = []
            for following in lines[i + 1:]:
                if following.startswith("## ") or LINK_LINE.match(following):
                    break
                body.append(following)
            return line.rstrip(), body
    return None


def notes_in(body: list[str]) -> tuple[set[str], bool, bool]:
    """(the ### headings that have notes under them, notes outside any heading?, a **Breaking** entry?)"""
    headings: set[str] = set()
    loose = breaking = False
    current = None
    in_comment = False
    for line in body:
        text = line.strip()
        if in_comment:
            in_comment = "-->" not in text
            continue
        if text.startswith("<!--"):
            in_comment = "-->" not in text
            continue
        if not text:
            continue
        if UPGRADING.match(text):
            continue  # the Upgrading line is neither a heading nor a note
        m = re.match(r"^###\s+(.+?)\s*$", text)
        if m:
            current = m.group(1).lower()
            continue
        if current is None:
            loose = True
        else:
            headings.add(current)
        if re.match(r"^[-*]\s+\*\*Breaking", text, re.I):
            breaking = True
    return headings, loose, breaking


def first_line(body: list[str]) -> str:
    """The first non-blank line of a section's body, or an empty string."""
    return next((t for t in (line.strip() for line in body) if t), "")


def need_upgrading(body: list[str]) -> None:
    if not UPGRADING.match(first_line(body)):
        raise Problem("the newest section needs '**Upgrading:** …' as its first line: 'nothing to do.' or "
                      "the steps (CONTRIBUTING.md, 'Releasing')")


def pick_bump(base: tuple[int, int, int], body: list[str]) -> tuple[str, str] | None:
    """Which number to raise for these notes, and why; None when there are no notes."""
    headings, loose, breaking = notes_in(body)
    if not headings and not loose:
        return None
    others = sorted(h for h in headings if h not in PATCH_ONLY) + (["no heading"] if loose else [])
    if base[0] >= 1 and (breaking or "removed" in headings):
        return "major", "notes under ### Removed or marked **Breaking** raise the first number"
    if breaking:
        return "minor", "before 1.0, a breaking change raises the middle number"
    if others:
        shown = ", ".join(h if h == "no heading" else f"### {h.title()}" for h in others)
        return "minor", f"notes under {shown} raise the middle number"
    return "patch", "only fixes (### Fixed, ### Security), so the last number goes up"


def raise_number(base: tuple[int, int, int], bump: str) -> str:
    major, minor, patch = base
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def decide(current: str, changelog: str, tags: set[str], bump: str = "auto") -> tuple[str | None, str]:
    """(version to release or None, the reason in a sentence). Raises Problem."""
    released = sorted((parse(t[1:]) for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)), reverse=True)
    section = newest_section(changelog)
    if section is None:
        raise Problem("CHANGELOG.md has no '## ' sections")
    heading, body = section
    for other in [ln.rstrip() for ln in changelog.splitlines() if ln.startswith("## ")][1:]:
        if not DATED.match(other):
            raise Problem(f"'{other}' is below the newest section and isn't released: move its notes into "
                          f"the top section ('{heading}'), so one release carries them all")

    if re.fullmatch(r"## \[Unreleased\]", heading):
        base = max([parse(current)] + released[:1])
        if bump == "auto":
            picked = pick_bump(base, body)
            if picked is None:
                return None, "there are no notes under '## [Unreleased]' yet"
            bump, why = picked
        else:
            if pick_bump(base, body) is None:
                return None, "there are no notes under '## [Unreleased]' yet"
            why = f"the run asked for a {bump} release"
        need_upgrading(body)
        version = raise_number(base, bump)
    else:
        m = re.fullmatch(r"## \[(\d+\.\d+\.\d+)\] - Unreleased", heading)
        if m:
            version = m.group(1)
            if pick_bump(parse(version), body) is None:
                return None, f"'{heading}' has no notes yet"
            need_upgrading(body)
            why = f"CHANGELOG.md names it: '{heading}'"
            if bump != "auto":
                why += f" (that heading decides, so the {bump} choice is ignored)"
            if parse(version) < parse(current):
                raise Problem(f"'{heading}' is older than VERSION ({current})")
        else:
            m = DATED.match(heading)
            if not m:
                raise Problem(f"the newest section's heading, '{heading}', isn't {FORMS}")
            if f"v{m.group(1)}" not in tags:
                raise Problem(
                    f"the newest section, '{heading}', is dated but v{m.group(1)} isn't tagged. Tag it by hand "
                    "(CONTRIBUTING.md, 'Releasing'), or start a '## [Unreleased]' section above it")
            return None, f"the newest section, '{heading}', is already released; nothing new under '## [Unreleased]'"

    if f"v{version}" in tags:
        raise Problem(f"v{version} is already tagged; check VERSION and CHANGELOG.md")
    if released and parse(version) <= released[0]:
        newest = ".".join(map(str, released[0]))
        raise Problem(f"{version} isn't newer than the newest tag, v{newest}")
    return version, why


def git_tags() -> set[str]:
    try:
        out = subprocess.run(["git", "-C", str(ROOT), "tag", "--list", "v*"],
                             capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {t.strip() for t in out.splitlines() if t.strip()}


def report(path_var: str, text: str) -> None:
    path = os.environ.get(path_var)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


def main(argv: list[str]) -> int:
    bump = "auto"
    if argv[:1] == ["--bump"] and len(argv) == 2 and argv[1] in ("auto", "patch", "minor", "major"):
        bump = argv[1]
    elif argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    current = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    try:
        version, why = decide(current, changelog, git_tags(), bump)
    except Problem as e:
        print(f"::error::{e}" if os.environ.get("GITHUB_ACTIONS") else f"Problem: {e}", file=sys.stderr)
        report("GITHUB_STEP_SUMMARY", f"### No release\n\nA person has to look at this first: {e}\n")
        return 1
    if version is None:
        print(f"Nothing to release: {why}.", file=sys.stderr)
        report("GITHUB_OUTPUT", "version=\n")
        report("GITHUB_STEP_SUMMARY", f"### Nothing to release\n\n{why[0].upper() + why[1:]}.\n")
        return 0
    print(version)
    print(f"Release {version}: {why}.", file=sys.stderr)
    report("GITHUB_OUTPUT", f"version={version}\n")
    report("GITHUB_STEP_SUMMARY", f"### Releasing {version}\n\nVERSION is {current}; {why}.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
