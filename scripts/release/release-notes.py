#!/usr/bin/env python3
"""Print one version's section of CHANGELOG.md, for the GitHub release notes.

    python3 scripts/release/release-notes.py 1.0.0

The section starts after its "## [1.0.0] - ..." heading and ends at the next "## [" heading
or at the link list at the bottom. Exits with 1 if there is no such section.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def section(changelog: str, version: str) -> str | None:
    lines = changelog.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^## \[{re.escape(version)}\]", line):
            start = i + 1
            break
    if start is None:
        return None
    out = []
    for line in lines[start:]:
        if line.startswith("## [") or re.match(r"^\[[^\]]+\]:\s+\S", line):
            break
        out.append(line)
    return "\n".join(out).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    version = argv[0].removeprefix("v")
    text = section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    if text is None:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    # Relative links in the changelog point into the repository; on the release page they
    # need the full address of the tagged files.
    slug = "rob-paprocki/agent-bullpen"
    text = re.sub(r"\]\((?!https?://|#)([^)]+)\)", rf"](https://github.com/{slug}/blob/v{version}/\1)", text)
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
