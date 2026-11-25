#!/usr/bin/python3 -I
"""Build and check the public copy of a release. Fails closed: nothing in OUTDIR
is usable unless this exits 0, and a failed build removes OUTDIR.

  build_public.py REF OUTDIR [--wordlist FILE] [--message TEXT ...] [--repo DIR] [--conf FILE]
  build_public.py --explain WORDLIST_FILE [DIR]

The word list is every regular expression (one per line, # comments, matched in any
case, ^ and $ per line) from --wordlist FILE, the PRIVATE_WORDLIST environment
variable (the list's lines) and LOCAL_WORDLIST from release.conf when that file
exists. A missing or empty list stops the build. --message adds a text the list must
not match: the commit and tag messages and the release notes.

Steps:
  1. refuse a tree that turns on export-subst (git archive would write private
     commit data into the copy, after every check below could see it)
  2. git archive REF -> OUTDIR: committed files only, with no git config of the
     maintainer's applied; nothing absolute, no '..', no links
  3. remove private/, every path in private/public-exclude.txt (a listed path
     that isn't in the release is an error unless EXCLUDE_STRICT=0) and the
     local word list
  4. apply PUBLIC_FIXUPS (every build), then history fixups (reset_public.py only)
  5. write .github/release-public.conf, the public part of release.conf, for
     the public release.yml
  6. refuse symbolic links
  7. the PATH of every file against the word list and the built-in patterns
     (emails, IPs, MACs). A hit is reported as "file #N (in sorted order)", never
     by name or word, and the build stops there: any later check could print it
  8. every file must be UTF-8 text, or a binary that an APPROVED_BINARIES manifest
     pins by SHA-256 (PNG and PE files get extra checks), or a MEDIA_EXTENSIONS
     file. The word list runs over binaries' raw bytes too
  9. the word list against the CONTENT of every file and against each --message
 10. the built-in pattern checks with nothing skipped, and CHECK_SCRIPT if set
 11. links in Markdown files (inline, reference, <...>, href, src, srcset, and
     with LINK_CHECK_BACKTICKS a `folder/file.md` in backticks) must resolve
     inside the copy, and none may point into private/

No message prints a word-list entry or a matched value.
"""
import fnmatch
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
from urllib.parse import unquote

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import check_patterns  # noqa: E402
import release_conf  # noqa: E402

WORDLIST_ENV = "PRIVATE_WORDLIST"


class PublishError(Exception):
    pass


def parse_wordlist(text, where):
    patterns = []
    for i, line in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.I | re.M))
        except re.error:
            raise PublishError("%s line %d is not a valid regular expression" % (where, i)) from None
    return patterns


def load_wordlist(path=None, env_text=None, local_path=None):
    """Every pattern from the sources given. Raises if none gives one."""
    patterns, given = [], False
    if path:
        if not os.path.exists(path):         # a pipe (process substitution) is fine
            raise PublishError("word list file is missing")
        with open(path, encoding="utf-8-sig") as f:
            patterns += parse_wordlist(f.read(), "word list")
        given = True
    if env_text:
        patterns += parse_wordlist(env_text, WORDLIST_ENV)
        given = True
    if local_path and os.path.isfile(local_path):
        with open(local_path, encoding="utf-8-sig") as f:
            patterns += parse_wordlist(f.read(), "local word list")
        given = True
    if not given:
        raise PublishError("no word list given (--wordlist FILE or %s)" % WORDLIST_ENV)
    if not patterns:
        raise PublishError("the word list is empty")
    return patterns


def isolated_env():
    # The maintainer's git config (core.autocrlf, attributes, ...) must not change the copy.
    return dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                GIT_CONFIG_NOSYSTEM="1")


def export_subst(repo, ref):
    """True if REF's .gitattributes files, or the repository's info/attributes, turn on
    export-subst: git archive would then write commit data into the copy."""
    env = isolated_env()
    names = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "-z", "--name-only", ref],
                           capture_output=True, check=True, env=env).stdout.decode("utf-8", "replace")
    texts = []
    for name in names.split("\0"):
        if name == ".gitattributes" or name.endswith("/.gitattributes"):
            texts.append(subprocess.run(["git", "-C", repo, "show", "%s:%s" % (ref, name)],
                                        capture_output=True, check=True, env=env).stdout)
    info = subprocess.run(["git", "-C", repo, "rev-parse", "--git-path", "info/attributes"],
                          capture_output=True, text=True, check=True, env=env).stdout.strip()
    info = info if os.path.isabs(info) else os.path.join(repo, info)
    if os.path.isfile(info):
        with open(info, "rb") as f:
            texts.append(f.read())
    on = re.compile(rb"(?:^|\s)export-subst(?:\s|=|$)", re.M)
    return any(on.search(t) for t in texts)


def extract(data, out):
    """Unpack the tar bytes DATA into OUT. Only regular files and directories, nothing
    absolute, nothing with a '..' part. Uses tarfile's own "data" filter where this
    Python has it; otherwise every member is checked here first."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            members = t.getmembers()
            if not members:
                raise PublishError("the archive is empty")
            for m in members:
                parts = m.name.replace("\\", "/").split("/")
                if m.issym() or m.islnk():
                    raise PublishError("the release contains a symbolic link or hard link; "
                                       "refused, path withheld")
                if not (m.isreg() or m.isdir()) or m.name.startswith("/") or ".." in parts:
                    raise PublishError("the release contains an unsafe entry (special file or "
                                       "path outside the copy); refused, path withheld")
            if hasattr(tarfile, "data_filter"):
                t.extractall(out, filter="data")
                return
            root = os.path.abspath(out)
            for m in members:             # no extractall: no modes, owners or links applied
                dest = os.path.join(root, *[x for x in m.name.split("/") if x not in ("", ".")])
                if m.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                src = t.extractfile(m)
                with open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
                os.chmod(dest, 0o755 if m.mode & 0o100 else 0o644)     # git keeps only this bit
    except tarfile.ReadError:
        raise PublishError("the archive is empty or unreadable") from None
    except tarfile.TarError:
        raise PublishError("the release contains a symbolic link or a path pointing outside "
                           "the copy; refused, path withheld") from None


def archive(ref, out, repo="."):
    os.makedirs(out, exist_ok=True)
    data = subprocess.run(["git", "-C", repo, "archive", "--format=tar", ref],
                          capture_output=True, check=True, env=isolated_env()).stdout
    extract(data, out)


def read_excludes(path):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip() and not l.startswith("#")]


def remove_private(out, excludes=None, local_wordlist=None, strict=True):
    """Remove private/, the excluded paths and the local word list. With the release's own
    private/public-exclude.txt and STRICT, a listed path that isn't in the release is an
    error, so the list can't go stale. With EXCLUDES (a list, used to rebuild old tags that
    predate an entry) a listed path may be absent."""
    strict = strict and excludes is None
    if excludes is None:
        excl = os.path.join(out, "private", "public-exclude.txt")
        if not os.path.isfile(excl):
            raise PublishError("private/public-exclude.txt is missing from the release")
        paths = read_excludes(excl)
    else:
        paths = list(excludes)
    for p in paths + ["private"]:
        p = p.rstrip("/")
        if not p or p.startswith("/") or ".." in p.split("/"):
            raise PublishError("bad path in public-exclude.txt")
        full = os.path.join(out, p)
        if not os.path.lexists(full):
            if strict and p != "private":
                raise PublishError("a path listed in public-exclude.txt is not in the release")
            continue
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.unlink(full)
    if local_wordlist:
        full = os.path.join(out, local_wordlist)
        if os.path.lexists(full) and not os.path.isdir(full):
            os.unlink(full)               # should it ever be tracked: never published
    return paths


def read_fixups(path):
    """Lines of TAB-separated PATH, REGEX, REPLACEMENT (blank lines and # skipped)."""
    fixups = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.rstrip("\n")
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) != 3:
                raise PublishError("fixups line %d needs PATH, REGEX and REPLACEMENT separated by "
                                   "tabs" % n)
            try:
                fixups.append((parts[0], re.compile(parts[1]), parts[2]))
            except re.error:
                raise PublishError("fixups line %d is not a valid regular expression" % n) from None
    return fixups


def public_fixups(repo, conf):
    """The PUBLIC_FIXUPS file from the working tree (it lives in private/, so the copy never has
    it), or no fixups when the key is empty. A named file that is missing stops the build."""
    rel = conf.get("PUBLIC_FIXUPS")
    if not rel:
        return []
    path = os.path.join(repo, rel)
    if not os.path.isfile(path):
        raise PublishError("PUBLIC_FIXUPS names a file that doesn't exist (%s)" % rel)
    return read_fixups(path)


def apply_fixups(out, fixups):
    """Each fixup replaces matches of REGEX in the files PATH names. PATH is a path in the copy, or
    a pattern with * ? [ ] (fnmatch, where * also crosses folders, so a lone * is every file); a
    pattern skips files that aren't UTF-8 text. A path that isn't in the copy is skipped."""
    files = None
    for path, rx, repl in fixups:
        if any(c in path for c in "*?["):
            if files is None:
                files = sorted(all_files(out))
            targets = [rel for rel in files if fnmatch.fnmatchcase(rel, path)]
        else:
            targets = [path]
        for rel in targets:
            full = os.path.join(out, rel)
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            try:
                with open(full, encoding="utf-8", newline="") as f:
                    text = f.read()
            except UnicodeDecodeError:
                if targets == [path]:
                    raise PublishError("a fixup names a file that isn't UTF-8 text") from None
                continue
            new = rx.sub(repl, text)
            if new != text:
                with open(full, "w", encoding="utf-8", newline="") as f:
                    f.write(new)


def write_public_conf(out, conf):
    path = os.path.join(out, release_conf.PUBLIC_NAME)
    if os.path.lexists(path):
        raise PublishError("%s is written by the build; it must not be committed"
                           % release_conf.PUBLIC_NAME.replace(os.sep, "/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(conf.public_text())


def all_files(out):
    for d, _dirs, names in os.walk(out):
        for n in names:
            yield os.path.relpath(os.path.join(d, n), out).replace(os.sep, "/")


def symlinks(out):
    """Number of symbolic links anywhere in the copy (files or directories)."""
    found = 0
    for d, dirs, names in os.walk(out):
        for n in dirs + names:
            if os.path.islink(os.path.join(d, n)):
                found += 1
    return found


def name_problems(out, patterns, scanner):
    """Flagged file names, each only as its position in sorted order."""
    problems = []
    for index, rel in enumerate(sorted(all_files(out)), 1):
        position = "file #%d (in sorted order)" % index
        if any(p.search(rel) for p in patterns):
            problems.append("%s: a word-list entry matches the file name" % position)
        for _line, kind in scanner.scan_text(rel):
            problems.append("%s: %s in the file name" % (position, kind))
    return problems


def content_problems(out, patterns, messages, conf):
    """Word-list hits in content and messages, and every file no check can read."""
    approved, problems = check_patterns.load_manifests(out, conf)
    for rel in sorted(all_files(out)):
        try:
            with open(os.path.join(out, rel), "rb") as f:
                data = f.read()
        except OSError:
            problems.append("%s: could not be read" % rel)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            bad = check_patterns.binary_problem(rel, data, conf, approved, patterns)
            if bad:
                problems.append("%s: %s" % (rel, bad))
            continue
        if any(p.search(text) for p in patterns):
            problems.append("%s: a word-list entry matches the content" % rel)
    for i, msg in enumerate(messages, 1):
        if any(p.search(msg) for p in patterns):
            problems.append("release message %d: a word-list entry matches" % i)
    return problems


def script_problems(out, conf, repo):
    """Run the project's own CHECK_SCRIPT on the copy (it takes the folder to check)."""
    script = conf.get("CHECK_SCRIPT")
    if not script:
        return []
    path = os.path.abspath(os.path.join(repo, script))
    if not os.path.isfile(path):
        return ["CHECK_SCRIPT %s is missing" % script]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, "-I", path, os.path.abspath(out)], capture_output=True,
                       text=True, cwd=os.path.abspath(out), env=env)
    if p.returncode == 0:
        return []
    lines = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()][-40:]
    return ["CHECK_SCRIPT failed:"] + ["  " + l for l in lines]


# The ways a Markdown file links: [x](path "title") and [x](<path>), reference definitions
# [x]: path (not footnotes, [^1]: text), HTML href=, src= and srcset=, and a bare <path>.
INLINE = re.compile(r"\]\(\s*(?:<([^<>\n]*)>|([^)\s<>]+))")
REFDEF = re.compile(r"^[\s>]*\[(?!\^)[^\]\n]+\]:\s*(?:<([^<>\n]*)>|(\S+))")
HTML_LINK = re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
SRCSET = re.compile(r"""\bsrcset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))""", re.I)
ANGLE = re.compile(r"<([^<>\s]+)>")
EXTERNAL = re.compile(r"([a-z][a-z0-9+.-]*:|#)", re.I)
# A repo path in backticks names a file as surely as a link does: only a path with a folder
# in it and a .md file at its end (a bare `README.md` is whichever folder's).
TICKED = re.compile(r"`([\w.-]+(?:/[\w.-]+)+\.md)(?:#[^`]*)?`")
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
CODE_SPAN = re.compile(r"(`+)(?!`).*?(?<!`)\1(?!`)")


def link_targets(line):
    """(target, must_exist) for every link on LINE. A bare <...> may be an HTML tag or a
    placeholder such as <OWNER>, so it only must not point into private/ or outside."""
    def first(m):
        return next(g for g in m.groups() if g is not None)
    for m in INLINE.finditer(line):
        yield first(m), True
    m = REFDEF.match(line)
    if m:
        yield first(m), True
    for m in HTML_LINK.finditer(line):
        yield first(m), True
    for m in SRCSET.finditer(line):
        for candidate in first(m).split(","):
            if candidate.split():
                yield candidate.split()[0], True
    for m in ANGLE.finditer(line):
        yield m.group(1), False


def bad_link(target, rel, root, must_exist):
    target = target.strip()
    if not target or EXTERNAL.match(target):
        return False
    path = unquote(re.split(r"[?#]", target, maxsplit=1)[0])
    if not path:
        return False
    if must_exist:
        full = os.path.normpath(os.path.join(root, os.path.dirname(rel), path))
    else:                                     # "/x" read as from the top of the repository
        base = root if path.startswith("/") else os.path.join(root, os.path.dirname(rel))
        full = os.path.normpath(os.path.join(base, path.lstrip("/")))
    if not (full == root or full.startswith(root + os.sep)):
        return True
    if os.path.relpath(full, root).split(os.sep)[0].lower() == "private":
        return True
    return must_exist and not os.path.exists(full)


def link_check(out, backticks=False):
    problems = []
    root = os.path.abspath(out)
    for rel in sorted(all_files(out)):
        if not rel.lower().endswith(".md"):
            continue
        try:
            with open(os.path.join(out, rel), encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        fence = None
        for n, line in enumerate(lines, 1):
            m = FENCE.match(line)
            if m and (fence is None or m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence)):
                fence = None if fence else m.group(1)
                continue
            # In a code block or a code span a "link" is an example: it needn't exist, but it
            # still may not point into private/ or out of the copy.
            text = line if fence else CODE_SPAN.sub("", line)
            targets = [(t, must and not fence) for t, must in link_targets(text)]
            if any(bad_link(t, rel, root, must) for t, must in targets):
                problems.append("%s:%d: link to a file that isn't in the public copy" % (rel, n))
            if backticks:
                for target in TICKED.findall(line):
                    # From the repo root, as docs usually write them, or from the file's folder.
                    if not any(os.path.exists(os.path.join(out, base, target))
                               for base in ("", os.path.dirname(rel))):
                        problems.append("%s:%d: names a file that isn't in the public copy"
                                        % (rel, n))
    return problems


def build(ref, out, patterns, messages=(), repo=".", conf=None, excludes=None, fixups=()):
    if os.path.exists(out) and (os.path.islink(out) or not os.path.isdir(out) or os.listdir(out)):
        raise PublishError("output directory is not empty")      # never delete what isn't ours
    conf = conf if conf is not None else release_conf.Conf.load(
        os.path.join(repo, release_conf.DEFAULT_PATH))
    if not conf.public_repo:
        raise PublishError("private/release.conf names no PUBLIC_REPO; there is nothing to build for")
    try:
        return _build(ref, out, patterns, list(messages), repo, conf, excludes, fixups)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)     # a failed build leaves nothing usable
        raise


def _build(ref, out, patterns, messages, repo, conf, excludes, fixups):
    if export_subst(repo, ref):
        raise PublishError("the release turns on export-subst in a .gitattributes file; git archive "
                           "would write private commit data into the copy. Remove it")
    archive(ref, out, repo)
    remove_private(out, excludes, conf.get("LOCAL_WORDLIST"), conf.flag("EXCLUDE_STRICT"))
    apply_fixups(out, public_fixups(repo, conf))
    apply_fixups(out, fixups)
    write_public_conf(out, conf)
    # A link's target is text no check reads, and following it could read files
    # from outside the copy: refuse links outright (fail closed, path withheld).
    n = symlinks(out)
    if n:
        raise PublishError("the public copy contains %d symbolic link(s); they are not allowed" % n)
    scanner = check_patterns.Scanner(conf.items("ALLOWED_EMAILS"))
    names = name_problems(out, patterns, scanner)
    # Any other check could print a flagged file name: report only these, and stop.
    if names:
        raise PublishError("the public copy failed its checks:\n  " + "\n  ".join(names))
    problems = ["%s is missing from the public copy (REQUIRED_FILES)" % rel
                for rel in conf.items("REQUIRED_FILES") if not os.path.isfile(os.path.join(out, rel))]
    problems += content_problems(out, patterns, messages, conf)
    if conf.flag("BUILTIN_PATTERNS"):
        problems += check_patterns.check(out, conf, strict=True, binaries=False)
    problems += script_problems(out, conf, repo)
    problems += link_check(out, conf.flag("LINK_CHECK_BACKTICKS"))
    if problems:
        raise PublishError("the public copy failed its checks:\n  " + "\n  ".join(problems))
    return sorted(all_files(out))


def explain(root, wordlist_path):
    """Local diagnosis for the maintainer: which word-list LINE NUMBER matches which
    file:line. Never prints the pattern or the matched text, nor a matching file name."""
    with open(wordlist_path, encoding="utf-8-sig") as f:
        lines = [(n, l.strip()) for n, l in enumerate(f, 1)
                 if l.strip() and not l.strip().startswith("#")]
    pats = [(n, re.compile(l, re.I | re.M)) for n, l in lines]
    scanner = check_patterns.Scanner()
    excluded = check_patterns.excluded_paths(root)
    hits = 0
    for index, rel in enumerate(sorted(check_patterns.files_in(root)), 1):
        if check_patterns.skipped(rel, excluded):
            continue
        named = False
        for n, p in pats:
            if p.search(rel):
                print("word-list line %d matches the NAME of file #%d (in sorted order)" % (n, index))
                hits += 1
                named = True
        named = named or any(True for _ in scanner.scan_text(rel))     # an email, IP or MAC
        label = "file #%d (in sorted order)" % index if named else rel
        try:
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                for ln, text in enumerate(f, 1):
                    for n, p in pats:
                        if p.search(text):
                            print("word-list line %d matches %s:%d" % (n, label, ln))
                            hits += 1
        except (UnicodeDecodeError, OSError):
            continue
    print("explain: %d match(es)" % hits)
    return 1 if hits else 0


def option(args, name):
    if name not in args:
        return None
    i = args.index(name)
    if i + 1 >= len(args):
        raise PublishError("%s needs a value" % name)
    return args[i + 1]


def main(argv):
    args = argv[1:]
    if args[:1] == ["--explain"]:
        if len(args) < 2 or not os.path.isfile(args[1]):
            print("usage: build_public.py --explain WORDLIST_FILE [DIR]", file=sys.stderr)
            return 2
        return explain(args[2] if len(args) > 2 else ".", args[1])
    try:
        if len(args) < 2 or args[0].startswith("--") or args[1].startswith("--"):
            raise PublishError("usage: build_public.py REF OUTDIR [--wordlist FILE] "
                               "[--message TEXT ...] [--repo DIR] [--conf FILE]")
        ref, out = args[0], args[1]
        repo = option(args, "--repo") or "."
        conf = release_conf.Conf.load(option(args, "--conf")
                                      or os.path.join(repo, release_conf.DEFAULT_PATH))
        msgs = [args[i + 1] for i, a in enumerate(args) if a == "--message" and i + 1 < len(args)]
        local = conf.get("LOCAL_WORDLIST")
        patterns = load_wordlist(option(args, "--wordlist"), os.environ.get(WORDLIST_ENV),
                                 os.path.join(repo, local) if local else None)
        files = build(ref, out, patterns, msgs, repo, conf)
    except (PublishError, release_conf.ConfError, subprocess.CalledProcessError) as e:
        print("build_public: STOPPED, nothing is published.\n%s" % e, file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError):
        print("build_public: STOPPED, nothing is published.\n"
              "an input could not be read (details withheld)", file=sys.stderr)
        return 1
    print("build_public: OK, %d files in %s" % (len(files), out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
