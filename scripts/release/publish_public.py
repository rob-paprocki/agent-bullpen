#!/usr/bin/python3 -I
"""Prepare and push the single public commit of a release.

  publish_public.py prepare PUBLIC_CLONE COPY VERSION [--first-base SHA] [--conf FILE]
  publish_public.py push PUBLIC_CLONE VERSION [--conf FILE]
  publish_public.py docs PUBLIC_CLONE COPY [--push] [--conf FILE]

prepare makes the commit "NAME X.Y.Z" and the annotated tag vX.Y.Z in PUBLIC_CLONE,
both authored and committed by the bot (PUBLIC_BOT_NAME with the GitHub Actions bot
address, never the maintainer's), dated in UTC, on top of the previous release. The
whole tree is replaced by COPY, so files deleted since the last release are deleted
publicly too. Nothing leaves the machine.

push pushes PUBLIC_BRANCH and the tag together (--atomic). It does nothing if the
remote already holds exactly that commit and tag, so a re-run after a half-done
publish finishes the public side once, and a second run is a no-op.

docs publishes documentation between releases: only DOCS_PATHS (README.md and docs/
by default) are taken from the checked COPY, committed by the bot as "NAME docs:
update", with no tag and no release. Without --push it only prepares the commit.

--first-base: only for the FIRST release, when the public repository already has
commits (for example a README that GitHub made): publish on top of exactly that
commit. An empty public repository needs none.

Checks before anything is committed:
  * the public repository hasn't moved: its branch sits on the commit its newest vX.Y.Z
    tag points at, or on top of it with nothing but the bot's docs commits, and VERSION
    is newer than that tag. For the very first release (no tags yet), the repository
    must be empty (no commits and no refs), or --first-base must name its HEAD;
  * if vVERSION already exists there with the same content, there is nothing to
    prepare; with different content, the run stops;
  * the staged tree is exactly COPY: the same paths, modes and content. No ignore rule
    (the maintainer's, the clone's or the copy's .gitignore) drops a file;
  * with ALLOW_EMPTY_RELEASE=0, a release that changes no file is refused.

Every git command runs without the maintainer's git config, hooks or signing.
Python 3.9 compatible (the first publish runs on Apple's /usr/bin/python3).
"""
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import release_conf  # noqa: E402

BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class PublishError(Exception):
    pass


class Settings:
    """What publishing reads from release.conf."""

    def __init__(self, conf):
        self.name = conf.get("NAME") or "release"
        self.bot = conf.get("PUBLIC_BOT_NAME")
        self.branch = conf.get("PUBLIC_BRANCH")
        self.docs_paths = tuple(p.rstrip("/") for p in conf.items("DOCS_PATHS"))
        self.docs_subjects = (self.name + " docs: update",) + tuple(conf.raw("DOCS_SUBJECT_ALIASES"))
        self.allow_empty = conf.flag("ALLOW_EMPTY_RELEASE")

    def bot_env(self):
        return {"GIT_AUTHOR_NAME": self.bot, "GIT_COMMITTER_NAME": self.bot,
                "GIT_AUTHOR_EMAIL": BOT_EMAIL, "GIT_COMMITTER_EMAIL": BOT_EMAIL,
                "TZ": "UTC"}              # the maintainer's time zone stays off the public record

    def is_docs_path(self, path):
        return any(path == p or path.startswith(p + "/") for p in self.docs_paths)


def git(repo, *args, **kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") or k in ("GIT_SSH_COMMAND", "GIT_SSH", "GIT_ASKPASS")}
    env.update(kw.get("env") or {})
    # No config-borne leak of the maintainer (hooks, user.*, signing, templates, trailers).
    env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1"})
    pre = ["-c", "core.hooksPath=" + os.devnull, "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "core.excludesFile=" + os.devnull]
    cmd = ["git", "-C", repo] + pre + list(args)
    raw = kw.get("raw", False)
    p = subprocess.run(cmd, capture_output=True, text=not raw, env=env, input=kw.get("input"))
    if p.returncode != 0:
        err = p.stderr if not raw else p.stderr.decode("utf-8", "replace")
        raise PublishError("git %s failed: %s" % (" ".join(args[:2]), err.strip()[-300:]))
    return p.stdout if raw else p.stdout.strip()


def vkey(tag):
    return tuple(int(x) for x in tag[1:].split("."))


def release_tags(repo):
    tags = [t for t in git(repo, "tag", "--list", "v*").split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)]
    return sorted(tags, key=vkey)


def tree_of(repo, ref):
    return git(repo, "rev-parse", ref + "^{tree}")


def head_commit(repo):
    """HEAD's commit, or None in a clone of an empty repository (no commits and no refs)."""
    try:
        return git(repo, "rev-parse", "-q", "--verify", "HEAD^{commit}")
    except PublishError:
        pass
    if git(repo, "for-each-ref"):
        raise PublishError("the public clone has refs but no HEAD commit; nothing published")
    return None


def check_only_docs_since(repo, tag, s):
    """HEAD must be on TAG, or on top of it with nothing but the bot's docs commits."""
    bad = PublishError("the public %s branch is not on %s: someone committed there directly; "
                       "nothing published" % (s.branch, tag))
    try:
        git(repo, "merge-base", "--is-ancestor", tag, "HEAD")
        log = git(repo, "log", "--format=%H|%ae|%ce|%P|%s", tag + "..HEAD")
    except PublishError:
        raise bad from None
    for line in filter(None, log.splitlines()):
        _sha, ae, ce, parents, subject = line.split("|", 4)
        if ae != BOT_EMAIL or ce != BOT_EMAIL or subject not in s.docs_subjects \
                or len(parents.split()) != 1:
            raise bad
    changed = git(repo, "diff", "--name-only", "-z", tag, "HEAD").split("\0")
    if any(p and not s.is_docs_path(p) for p in changed):
        raise bad


def check_unmoved(repo, version, s, first_base=None):
    """Return "already-tagged", "first" or the previous tag; raise if the repo moved."""
    tags = release_tags(repo)
    head = head_commit(repo)
    newest = tags[-1] if tags else None
    if version is None:                       # a docs publish
        if not newest:
            raise PublishError("the public repository has no release yet; docs go out with the first one")
        check_only_docs_since(repo, newest, s)
        return newest
    if "v" + version in tags:
        return "already-tagged"
    if newest and vkey(newest) > vkey("v" + version):
        raise PublishError("%s is older than the public %s" % (version, newest))
    if not newest:
        if head is None:
            if first_base:
                raise PublishError("the public repository is empty, so there is no commit to "
                                   "acknowledge; run without --first-base")
            return "first"
        if not first_base:
            raise PublishError("the public repository has commits but no release tag. Check them; "
                               "if they may stay, run again with --first-base SHA (its HEAD)")
        # Allowed only if the public repository is EXACTLY at the commit the maintainer acknowledged.
        if len(first_base) < 7 or not head.startswith(first_base.lower()):
            raise PublishError("the public repository is not at the acknowledged commit %s"
                               % first_base[:12])
        return "first"
    check_only_docs_since(repo, newest, s)
    return newest


def clear_tree(repo):
    for name in os.listdir(repo):
        if name == ".git":
            continue
        p = os.path.join(repo, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        else:
            os.unlink(p)


def copy_into(src, dst):
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def replace_tree(repo, copy):
    clear_tree(repo)
    for name in os.listdir(copy):
        copy_into(os.path.join(copy, name), os.path.join(repo, name))
    # The copy is the vetted file set: no ignore rule may drop a file from it.
    git(repo, "add", "-A", "--force")
    check_staged(repo, copy)


def copy_entries(repo, copy):
    """{path: (mode, blob)} for every file in COPY, as git would stage it. Anything that is not
    a regular file gets an entry that never matches."""
    copy = os.path.abspath(copy)              # git -C REPO would read a relative path from REPO
    entries, regular = {}, []
    for d, dirs, names in os.walk(copy):
        for n in dirs + names:
            full = os.path.join(d, n)
            rel = os.path.relpath(full, copy).replace(os.sep, "/")
            if n in dirs and not os.path.islink(full):
                continue
            if os.path.islink(full) or not os.path.isfile(full) or "\n" in full:
                entries[rel] = ("not a regular file", None)
                continue
            entries[rel] = ("100755" if os.stat(full).st_mode & 0o100 else "100644", None)
            regular.append(rel)
    if regular:
        paths = "".join(os.path.join(copy, rel) + "\n" for rel in regular)
        blobs = git(repo, "hash-object", "--no-filters", "--stdin-paths", input=paths).split()
        for rel, blob in zip(regular, blobs):
            entries[rel] = (entries[rel][0], blob)
    return entries


def staged_entries(repo):
    entries = {}
    for item in git(repo, "ls-files", "-s", "-z").split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            mode, blob, _stage = meta.split()
            entries[path] = (mode, blob)
    return entries


def same_after_eol(repo, copy, rel, want, have):
    """True when COPY's file differs from the staged blob only as git's own line-ending rules for
    that path (.gitattributes text/eol) store it. git archive writes an eol=crlf file with CRLF,
    and git add stores it with LF: the same vetted text. Only the line endings may differ."""
    w, h = want.get(rel), have.get(rel)
    if not w or not h or w[1] is None or w[0] != h[0]:
        return False
    full = os.path.join(os.path.abspath(copy), rel)
    try:
        with open(full, "rb") as f:
            data = f.read()
        staged = git(repo, "cat-file", "blob", h[1], raw=True)
    except (OSError, PublishError):
        return False
    if data.replace(b"\r\n", b"\n") != staged.replace(b"\r\n", b"\n"):
        return False
    return git(repo, "hash-object", "--path", rel, full) == h[1]


def check_staged(repo, copy):
    """Refuse unless the staged tree holds exactly COPY's files, modes and content. Names are
    never printed (one could hold a private value): only the count and positions."""
    want, have = copy_entries(repo, copy), staged_entries(repo)
    paths = sorted(set(want) | set(have))
    differ = [str(i) for i, p in enumerate(paths, 1)
              if want.get(p) != have.get(p) and not same_after_eol(repo, copy, p, want, have)]
    if differ:
        raise PublishError("the staged public tree differs from the copy in %d file(s) (number %s "
                           "in the sorted list of both trees' paths); nothing committed"
                           % (len(differ), ", ".join(differ[:10])))


def prepare(repo, copy, version, s, first_base=None):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise PublishError("version must look like X.Y.Z")
    if os.path.lexists(os.path.join(copy, ".git")):
        raise PublishError("the copy contains a .git entry; refusing to touch the clone")
    state = check_unmoved(repo, version, s, first_base)
    tag = "v" + version
    replace_tree(repo, copy)
    if state == "already-tagged":
        # Finish a half-done publish: the tag must hold exactly this content.
        staged = git(repo, "write-tree")
        git(repo, "reset", "-q", "--hard", "HEAD")
        if staged != tree_of(repo, tag):
            raise PublishError("%s already exists in the public repository with different content" % tag)
        return "exists"
    if not s.allow_empty and state != "first" and git(repo, "write-tree") == tree_of(repo, "HEAD"):
        raise PublishError("this release changes no public file (ALLOW_EMPTY_RELEASE=0)")
    msg = "%s %s" % (s.name, version)
    git(repo, "commit", "-q", "--allow-empty", "--no-verify", "-m", msg, env=s.bot_env())
    git(repo, "tag", "-a", tag, "-m", msg, env=s.bot_env())
    return "prepared"


def prepare_docs(repo, copy, s):
    check_unmoved(repo, None, s)
    for name in s.docs_paths:
        if os.path.islink(os.path.join(copy, name)):
            raise PublishError("%s in the copy is a symlink" % name)
    for name in s.docs_paths:
        p, src = os.path.join(repo, name), os.path.join(copy, name)
        if os.path.isdir(p) and not os.path.islink(p):
            shutil.rmtree(p)
        elif os.path.lexists(p):
            os.unlink(p)
        if os.path.exists(src):
            copy_into(src, p)
    present = [n for n in s.docs_paths
               if os.path.lexists(os.path.join(repo, n)) or git(repo, "ls-files", "--", n)]
    if present:
        git(repo, "add", "-A", "--force", "--", *present)
    staged = [p for p in git(repo, "diff", "--cached", "--name-only", "-z").split("\0") if p]
    if not staged:
        return "unchanged"
    if any(not s.is_docs_path(p) for p in staged):
        raise PublishError("a docs publish would change files outside %s" % ", ".join(s.docs_paths))
    git(repo, "commit", "-q", "--no-verify", "-m", s.docs_subjects[0], env=s.bot_env())
    return "prepared"


def remote_ref(repo, ref):
    out = git(repo, "ls-remote", "origin", ref)
    return out.split()[0] if out else None


def push(repo, version, s):
    """Push the branch and the tag together; a no-op if the remote already has both."""
    tag = "v" + version
    if git(repo, "rev-parse", "HEAD") != git(repo, "rev-parse", tag + "^{commit}"):
        raise PublishError("HEAD of the public clone is not %s; nothing pushed" % tag)
    branch = "refs/heads/" + s.branch
    if (remote_ref(repo, branch) == git(repo, "rev-parse", "HEAD")
            and remote_ref(repo, "refs/tags/" + tag) == git(repo, "rev-parse", "refs/tags/" + tag)):
        return "unchanged"
    git(repo, "push", "--atomic", "origin", "HEAD:" + branch, "refs/tags/" + tag)
    return "pushed"


def main(argv):
    usage = ("usage: publish_public.py prepare PUBLIC_CLONE COPY VERSION [--first-base SHA]\n"
             "       publish_public.py push PUBLIC_CLONE VERSION\n"
             "       publish_public.py docs PUBLIC_CLONE COPY [--push]\n"
             "       (each takes --conf FILE)")
    args = list(argv[1:])
    try:
        conf_path = release_conf.DEFAULT_PATH
        if "--conf" in args:
            i = args.index("--conf")
            if i + 1 >= len(args):
                raise PublishError(usage)
            conf_path = args[i + 1]
            del args[i:i + 2]
        conf = release_conf.Conf.load(conf_path)
        if not conf.public_repo:
            raise PublishError("private/release.conf names no PUBLIC_REPO")
        s = Settings(conf)
        if len(args) >= 4 and args[0] == "prepare":
            repo, copy, version = args[1], args[2], args[3]
            rest = args[4:]
            first_base = None
            if rest:
                if len(rest) != 2 or rest[0] != "--first-base":
                    raise PublishError(usage)
                first_base = rest[1] or None
            result = prepare(repo, copy, version, s, first_base)
            if result == "exists":
                print("publish_public: v%s is already in the public clone with this content" % version)
            else:
                print("publish_public: prepared %s %s locally; nothing pushed." % (s.name, version))
                print(git(repo, "show", "--stat", "--format=%an <%ae>%n%s", "HEAD")[:3000])
        elif len(args) == 3 and args[0] == "push":
            result = push(args[1], args[2], s)
            if result == "unchanged":
                print("publish_public: v%s is already pushed; nothing to do" % args[2])
            else:
                print("publish_public: pushed %s %s (commit + tag v%s)" % (s.name, args[2], args[2]))
        elif len(args) in (3, 4) and args[0] == "docs" and args[3:] in ([], ["--push"]):
            repo = args[1]
            result = prepare_docs(repo, args[2], s)
            if result == "unchanged":
                print("publish_public: the public docs already match; nothing to do")
            elif args[3:] == ["--push"]:
                git(repo, "push", "origin", "HEAD:refs/heads/" + s.branch)
                print("publish_public: pushed a docs update (no tag, no release)")
            else:
                print("publish_public: prepared a docs commit locally; nothing pushed.")
                print(git(repo, "show", "--stat", "--format=%an <%ae>%n%s", "HEAD")[:3000])
        else:
            raise PublishError(usage)
    except (PublishError, release_conf.ConfError) as e:
        print("publish_public: STOPPED, nothing published.\n%s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
