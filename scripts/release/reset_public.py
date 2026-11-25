#!/usr/bin/python3 -I
"""Rebuild a public history of ONE scrubbed commit per released version, locally.

  reset_public.py OUTDIR [--wordlist FILE] [--through vX.Y.Z] [--repo DIR] [--conf FILE]
      (or the PRIVATE_WORDLIST environment variable with the list's lines)

For every vX.Y.Z tag in the private repository up to --through (default: all), in
SemVer order, this builds the public copy of that tag as build_public.py does, with
two differences for old tags: the CURRENT private/public-exclude.txt is used (a listed
path an old tag lacks is fine), and the CURRENT HISTORY_FIXUPS file (TAB-separated
PATH, REGEX, REPLACEMENT lines) rewrites old releases that predate the checks. Each
copy is committed to a brand-new repository in OUTDIR as "NAME X.Y.Z" by the bot,
tagged vX.Y.Z, dated like the original tagged commit. The first version is the root
commit.

It never pushes and never touches the public repository. Any failed check stops it
and leaves nothing usable. Pushing the result over the public repository's history is
a separate, deliberate step the owner takes by hand.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import build_public  # noqa: E402
import publish_public  # noqa: E402
import release_conf  # noqa: E402


def version_tags(repo, through=None):
    tags = publish_public.release_tags(repo)
    if through:
        if through not in tags:
            raise build_public.PublishError("%s is not a tag of the private repository" % through)
        tags = [t for t in tags if publish_public.vkey(t) <= publish_public.vkey(through)]
    if not tags:
        raise build_public.PublishError("no vX.Y.Z tags to rebuild")
    return tags


def current_file(repo, path, tmp, name):
    """HEAD's version of PATH copied to TMP/NAME, or None if HEAD has no such file."""
    p = subprocess.run(["git", "-C", repo, "show", "HEAD:" + path], capture_output=True)
    if p.returncode != 0:
        return None
    dest = os.path.join(tmp, name)
    with open(dest, "wb") as f:
        f.write(p.stdout)
    return dest


def reset(out, patterns, repo=".", through=None, conf=None):
    if os.path.exists(out) and os.listdir(out):
        raise build_public.PublishError("output directory %s is not empty" % out)
    conf = conf if conf is not None else release_conf.Conf.load(
        os.path.join(repo, release_conf.DEFAULT_PATH))
    s = publish_public.Settings(conf)
    tags = version_tags(repo, through)
    tmp = tempfile.mkdtemp()
    try:
        excl = current_file(repo, "private/public-exclude.txt", tmp, "excl.txt")
        excludes = build_public.read_excludes(excl) if excl else []
        fix = current_file(repo, conf.get("HISTORY_FIXUPS"), tmp, "fixups.txt")
        fixups = build_public.read_fixups(fix) if fix else []
        os.makedirs(out, exist_ok=True)
        publish_public.git(out, "init", "-q")
        publish_public.git(out, "symbolic-ref", "HEAD", "refs/heads/" + s.branch)
        for tag in tags:
            version = tag[1:]
            msg = "%s %s" % (s.name, version)
            copy = os.path.join(tmp, "copy-" + version)
            try:
                build_public.build(tag, copy, patterns, [msg], repo, conf, excludes, fixups)
            except build_public.PublishError as e:
                raise build_public.PublishError("%s: %s" % (tag, e)) from None
            when = publish_public.git(repo, "log", "-1", "--format=%aI", tag + "^{commit}")
            env = dict(s.bot_env(), GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
            publish_public.replace_tree(out, copy)
            publish_public.git(out, "commit", "-q", "--allow-empty", "--no-verify", "-m", msg, env=env)
            publish_public.git(out, "tag", "-a", tag, "-m", msg, env=env)
            shutil.rmtree(copy)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return tags


def main(argv):
    args = argv[1:]
    try:
        if not args or args[0].startswith("--"):
            raise build_public.PublishError("usage: reset_public.py OUTDIR [--wordlist FILE] "
                                            "[--through vX.Y.Z] [--repo DIR] [--conf FILE]")
        out = args[0]
        repo = build_public.option(args, "--repo") or "."
        conf = release_conf.Conf.load(build_public.option(args, "--conf")
                                      or os.path.join(repo, release_conf.DEFAULT_PATH))
        local = conf.get("LOCAL_WORDLIST")
        patterns = build_public.load_wordlist(build_public.option(args, "--wordlist"),
                                              os.environ.get(build_public.WORDLIST_ENV),
                                              os.path.join(repo, local) if local else None)
        tags = reset(out, patterns, repo, build_public.option(args, "--through"), conf)
    except (build_public.PublishError, publish_public.PublishError, release_conf.ConfError,
            subprocess.CalledProcessError) as e:
        print("reset_public: STOPPED, nothing is published.\n%s" % e, file=sys.stderr)
        return 1
    print("reset_public: OK, %d commits (%s) in %s; nothing pushed" % (len(tags), ", ".join(tags), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
