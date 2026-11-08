#!/usr/bin/env bash
# =============================================================================
#  set-repo.sh: point every link and installer at a new GitHub repository.
#
#      bash scripts/release/set-repo.sh NEW_OWNER/NEW_REPO            change the files
#      bash scripts/release/set-repo.sh --dry-run NEW_OWNER/NEW_REPO  only list what would change
#
#  The installers, the docs and the release workflow name the repository as
#  "owner/repo" (for example in raw.githubusercontent.com/<owner>/<repo>/main/...).
#  This replaces that text in every file git tracks, this script included, so
#  running it again later starts from the new name. Review the result with
#  `git diff`, then commit it.
#
#  --from OLD_OWNER/OLD_REPO replaces a different name than the current one.
# =============================================================================
set -euo pipefail

# Everything below is one block that bash reads in full before running it: this script rewrites
# its own CURRENT line, and bash would otherwise go on reading the changed file mid-way.
{
CURRENT="rob-paprocki/agent-bullpen"  # the repository this copy points at; this script rewrites the line too

usage() { sed -n '3,14p' "$0" | sed -E 's/^# ? ?//'; exit 2; }
DRY=0
FROM="$CURRENT"
NEW=""
while (( $# )); do
  case "$1" in
    --dry-run|-n) DRY=1 ;;
    --from) shift; FROM=${1:-} ;;
    -h|--help) usage ;;
    -*) echo "Unknown option: $1" >&2; usage ;;
    *) [[ -z "$NEW" ]] || usage; NEW=$1 ;;
  esac
  shift
done
[[ -n "$NEW" ]] || usage
SLUG_RE='^[A-Za-z0-9]([A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$'
[[ "$NEW" =~ $SLUG_RE ]] || { echo "'$NEW' doesn't look like OWNER/REPO" >&2; exit 2; }
[[ "$FROM" =~ $SLUG_RE ]] || { echo "'$FROM' doesn't look like OWNER/REPO" >&2; exit 2; }
if [[ "$NEW" == "$FROM" ]]; then echo "The files already point at $NEW."; exit 0; fi
command -v git >/dev/null || { echo "git is needed" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is needed" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Run this inside a git checkout of the project." >&2; exit 1; }

# Each tracked text file that mentions the old name, then a literal (not regex) replacement.
git ls-files -z | FROM="$FROM" NEW="$NEW" DRY="$DRY" python3 -c '
import os, sys
old, new, dry = os.environ["FROM"], os.environ["NEW"], os.environ["DRY"] == "1"
changed = total = 0
for path in sys.stdin.buffer.read().decode("utf-8", "replace").split("\0"):
    if not path or not os.path.isfile(path) or os.path.islink(path):
        continue
    with open(path, "rb") as fh:
        data = fh.read()
    if b"\0" in data:
        continue  # binary
    n = data.count(old.encode())
    if not n:
        continue
    changed += 1
    total += n
    print(f"  {path}: {n}")
    if not dry:
        with open(path, "wb") as fh:
            fh.write(data.replace(old.encode(), new.encode()))
verb = "Would change" if dry else "Changed"
print(f"{verb} {total} mention(s) in {changed} file(s): {old} -> {new}")
'
if (( ! DRY )); then
  echo "Next: check with  git diff  and  python3 tests/check_repo.py , then commit."
fi
exit 0
}
