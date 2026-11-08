#!/usr/bin/env python3
"""Prepare a release: set the version everywhere and date its CHANGELOG.md section.

    python3 scripts/release/bump-version.py 1.0.1

It changes four things, then runs tests/check_repo.py --tag v1.0.1:

  VERSION                          1.0.1
  hub/app.py, runner/runner.py     VERSION = "1.0.1"  (they must match: the hub hands its
                                   own copy of the runner to every machine)
  CHANGELOG.md                     the newest section's heading, "## [1.0.1] - Unreleased" or
                                   "## [Unreleased]", becomes "## [1.0.1] - <today>", and a link for
                                   the tag is added

It commits, tags and pushes nothing. CONTRIBUTING.md ("Releasing") has the steps after it.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SLUG = "rob-paprocki/agent-bullpen"


def with_code_version(rel: str, version: str) -> str:
    text = (ROOT / rel).read_text(encoding="utf-8")
    new, n = re.subn(r'^VERSION = "[^"]*"', f'VERSION = "{version}"', text, count=1, flags=re.M)
    if n != 1:
        sys.exit(f"{rel}: no line like VERSION = \"X.Y.Z\". Nothing was changed.")
    return new


def dated_changelog(text: str, version: str, today: str) -> str:
    """CHANGELOG.md with its newest section dated as this version, and the tag's link added."""
    first = re.search(r"^## .*$", text, re.M)
    heading = first.group(0).rstrip() if first else ""
    for other in re.findall(r"^## .*$", text[first.end():] if first else "", re.M):
        if not re.match(r"## \[\d+\.\d+\.\d+\] - \d{4}-\d{2}-\d{2}(?:\s|$)", other):
            sys.exit(f"CHANGELOG.md: '{other.rstrip()}' is below the newest section and isn't released. Move its "
                     "notes into the top section first. Nothing was changed.")
    if re.fullmatch(rf"## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}", heading):
        print(f"CHANGELOG.md: [{version}] already has a date; left as it is")
    elif heading in ("## [Unreleased]", f"## [{version}] - Unreleased"):
        # Only the heading line: the blank line under it stays.
        text = text[:first.start()] + f"## [{version}] - {today}" + text[first.end():]
    else:
        sys.exit(f"CHANGELOG.md: the newest section is '{heading or '(none)'}'. Put the release's notes at "
                 f"the top, under '## [Unreleased]' or '## [{version}] - Unreleased'. Nothing was changed.")
    link = f"[{version}]: https://github.com/{SLUG}/releases/tag/v{version}"
    if not re.search(rf"^\[{re.escape(version)}\]:", text, re.M):
        # The link list is at the end, newest first.
        m = re.search(r"^\[[^\]]+\]:\s+\S", text, re.M)
        text = (text[:m.start()] + link + "\n" + text[m.start():]) if m else (text.rstrip("\n") + "\n\n" + link + "\n")
    return re.sub(r"^\[Unreleased\]:.*\n", "", text, flags=re.M)


def main(argv: list[str]) -> int:
    if len(argv) != 1 or not re.fullmatch(r"v?\d+\.\d+\.\d+", argv[0]):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version = argv[0].removeprefix("v")
    today = dt.date.today().isoformat()
    # Work out every change first, so a problem leaves every file as it was.
    changelog = dated_changelog((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version, today)
    code = {rel: with_code_version(rel, version) for rel in ("hub/app.py", "runner/runner.py")}
    (ROOT / "VERSION").write_text(version + "\n", encoding="utf-8")
    for rel, text in code.items():
        (ROOT / rel).write_text(text, encoding="utf-8", newline="")
    (ROOT / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    print(f"Set the version to {version} and dated its changelog section {today}.")
    result = subprocess.run([sys.executable, str(ROOT / "tests" / "check_repo.py"), "--tag", f"v{version}"])
    if result.returncode:
        print("Fix the problems above before tagging.", file=sys.stderr)
        return result.returncode
    steps = [("git diff", "check the changes"),
             ("python3 tests/smoke_test.py", "the end-to-end test"),
             (f'git commit -am "Release {version}"', ""),
             (f'git tag -a v{version} -m "Agent Bullpen {version}"', ""),
             (f"git push origin HEAD v{version}", "the tag starts .github/workflows/release.yml")]
    width = max(len(command) for command, _ in steps) + 3  # the notes line up, whatever the version
    print("\nNext:\n" + "\n".join(f"  {command:<{width}}{note}".rstrip() for command, note in steps))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
