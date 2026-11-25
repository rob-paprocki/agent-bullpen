#!/usr/bin/python3 -I
"""Flag commits that change user-visible files without touching CHANGELOG.md.

  changelog_check.py [BASE] [--head REF] [--conf FILE]

It checks the commits after BASE up to --head (default HEAD), merges left out. Without
BASE, or when BASE is all zeros or isn't an ancestor of the head (a new branch, a force
push), it starts after the newest vX.Y.Z tag in the head's history, or after the commit
that added CHANGELOG.md. CHANGELOG_SINCE in private/release.conf sets a floor: "kit"
(the default) never checks commits from before this script was added, a SHA never
checks that commit or anything older, and an empty value sets no floor.

A file is visible when it starts with an entry of CHANGELOG_VISIBLE (all files when
that is empty) and isn't hidden: private/, the paths in private/public-exclude.txt
and the CHANGELOG_HIDDEN entries are hidden (an entry that starts with "*/" matches
that folder anywhere).

A commit can opt out with "[no changelog]" in its message. Two private lists skip
commits that went in without either and were reviewed:
  private/changelog-skip.txt    a full 40-character SHA, spaces, the reason. A bad line,
                                a repeat, or a SHA not in the head's history: exit 2
  private/changelog-ignore.txt  a SHA prefix, then anything (older format)

CHANGELOG_RANGE=range is more lenient: if any commit in the range changes CHANGELOG.md,
nothing is flagged. The default, commit, checks each commit on its own.

Exit 1 if any commit is flagged, 2 if a list or the settings can't be used.
"""
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import release_conf  # noqa: E402

THIS = "scripts/release/changelog_check.py"
SKIP_LIST = "private/changelog-skip.txt"
IGNORE_LIST = "private/changelog-ignore.txt"
SKIP_LINE = re.compile(r"([0-9a-f]{40})[ \t]+([^#\s].*)")


class ListError(Exception):
    """A list or setting can't be used. The check fails rather than guess."""


def git(*args, repo="."):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True, text=True,
                          check=True).stdout


def ok(*args, repo="."):
    return subprocess.run(["git", "-C", repo] + list(args), capture_output=True).returncode == 0


def in_history(sha, head="HEAD", repo="."):
    """True if SHA is a commit that HEAD's history contains."""
    found = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "--quiet", sha + "^{commit}"],
                           capture_output=True, text=True)
    if found.returncode != 0 or found.stdout.strip() != sha:
        return False
    return ok("merge-base", "--is-ancestor", sha, head, repo=repo)


def skip_list(head="HEAD", repo="."):
    """{sha: reason} from the skip list ({} when there is none)."""
    path = os.path.join(repo, SKIP_LIST)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError) as error:
        raise ListError("%s can't be read (%s)" % (SKIP_LIST, error.__class__.__name__)) from None
    listed = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = "%s line %d" % (SKIP_LIST, number)
        match = SKIP_LINE.fullmatch(line)
        if not match:
            raise ListError("%s: needs a full 40-character commit SHA in lower case, then a space "
                            "and the reason" % where)
        sha, reason = match.groups()
        if sha in listed:
            raise ListError("%s: lists commit %s a second time" % (where, sha[:10]))
        if not in_history(sha, head, repo):
            raise ListError("%s: commit %s isn't in the history of the head" % (where, sha[:10]))
        listed[sha] = reason
    return listed


def ignore_prefixes(repo="."):
    try:
        with open(os.path.join(repo, IGNORE_LIST), encoding="utf-8") as f:
            return [l.split()[0].lower() for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def excluded(repo="."):
    """Paths in private/public-exclude.txt never reach the public repository."""
    try:
        with open(os.path.join(repo, "private", "public-exclude.txt"), encoding="utf-8") as f:
            return [l.strip().rstrip("/") for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def visible_filter(conf, repo="."):
    allow = conf.items("CHANGELOG_VISIBLE")
    hidden = conf.items("CHANGELOG_HIDDEN")
    skip = excluded(repo)

    def visible(f):
        if f == "private" or f.startswith("private/"):
            return False
        if any(f == s or f.startswith(s + "/") for s in skip):
            return False
        for h in hidden:
            if h.startswith("*/"):
                if ("/" + f).find("/" + h[2:]) >= 0:
                    return False
            elif f == h.rstrip("/") or f.startswith(h):
                return False
        return not allow or any(f == v.rstrip("/") or f.startswith(v) for v in allow)
    return visible


def newest_tag(head="HEAD", repo="."):
    tags = git("tag", "--list", "v*", "--merged", head, "--sort=-v:refname", repo=repo).split()
    tags = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return tags[0] if tags else None


def default_base(head="HEAD", repo="."):
    tag = newest_tag(head, repo)
    if tag:
        return tag
    added = git("log", "--diff-filter=A", "--format=%H", head, "--", "CHANGELOG.md", repo=repo).split()
    return added[-1] if added else None


def floor(conf, head="HEAD", repo="."):
    since = conf.get("CHANGELOG_SINCE")
    if not since:
        return None
    if since == "kit":
        found = git("log", "--diff-filter=A", "--format=%H", head, "--", THIS, repo=repo).split()
        return found[-1] if found else None
    if not re.fullmatch(r"[0-9a-f]{7,40}", since) or not ok("rev-parse", "--verify", "--quiet",
                                                             since + "^{commit}", repo=repo):
        raise ListError("CHANGELOG_SINCE is not a commit of this repository")
    return since


def flagged(base, conf, head="HEAD", repo=".", listed=None):
    """([(short sha, subject, visible files)], number of listed commits skipped)."""
    listed = listed or {}
    prefixes = ignore_prefixes(repo)
    visible = visible_filter(conf, repo)
    rng = [head] + ["^" + b for b in (base, floor(conf, head, repo)) if b]
    out, skipped, touched = [], 0, False
    for sha in git("rev-list", "--no-merges", *rng, repo=repo).split():
        files = git("diff-tree", "-z", "--no-commit-id", "--name-only", "-r", "--root", sha,
                    repo=repo).split("\0")
        files = [f for f in files if f]
        if "CHANGELOG.md" in files:
            touched = True
            continue
        if sha in listed:
            skipped += 1
            continue
        if any(sha.startswith(p) for p in prefixes):
            skipped += 1
            continue
        msg = git("log", "-1", "--format=%B", sha, repo=repo)
        if "[no changelog]" in msg:
            continue
        shown = [f for f in files if visible(f)]
        if shown:
            out.append((sha[:10], msg.splitlines()[0] if msg else "", shown))
    if touched and conf.get("CHANGELOG_RANGE") == "range":
        out = []
    return out, skipped


def main(argv):
    args = argv[1:]
    try:
        conf_path = release_conf.DEFAULT_PATH
        head = "HEAD"
        if "--conf" in args:
            i = args.index("--conf")
            conf_path = args[i + 1]
            del args[i:i + 2]
        if "--head" in args:
            i = args.index("--head")
            head = args[i + 1]
            del args[i:i + 2]
        conf = release_conf.Conf.load(conf_path)
        listed = skip_list(head)
        base = args[0] if args else None
        if base and (set(base) == {"0"} or not ok("merge-base", "--is-ancestor", base, head)):
            base = None
        base = base or default_base(head)
        bad, skipped = flagged(base, conf, head, listed=listed)
    except (ListError, release_conf.ConfError, IndexError) as error:
        print("changelog check: %s" % (error if not isinstance(error, IndexError) else "missing argument"))
        return 2
    for sha, subject, files in bad:
        print("%s %s\n    changes %s but not CHANGELOG.md (add a line, or put "
              "[no changelog] in the message)" % (sha, subject, ", ".join(files[:5])))
    if skipped:
        print("changelog check: skipped %d reviewed commit(s) from the private skip lists" % skipped)
    print("changelog check: %s" % ("%d commit(s) need a changelog line" % len(bad) if bad else "OK"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
