#!/usr/bin/python3 -I
"""Read the project's release settings: private/release.conf.

  release_conf.py get KEY [--conf FILE]        print one value (a list prints one item per line)
  release_conf.py check [--conf FILE]          parse the file and report problems
  release_conf.py public OUT [--conf FILE]     write the public subset (see PUBLIC_KEYS) to OUT

The file is plain KEY=VALUE lines. It is parsed, never executed: no quotes, no
variables, no escapes. A line that starts with # is a comment, blank lines are
ignored, and the space around KEY and VALUE is dropped. A list key may appear on
several lines, one item per line. Every other key may appear once. An unknown
key is an error, so a typo can't silently fall back to a default.

A missing file is not an error: every key takes its default, and PUBLIC_REPO is
empty, which means "no public twin yet" and keeps the release workflows inert.

The keys, their defaults and what reads them are listed in KEYS below and in
private/release.conf.example.
"""
import os
import re
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

DEFAULT_PATH = os.path.join("private", "release.conf")
PUBLIC_NAME = os.path.join(".github", "release-public.conf")

# key: (default, is_list). A string default of None means "derived" (see Conf.get).
KEYS = {
    # Identity
    "PUBLIC_REPO": ("", False),
    "PRIVATE_REPO": (None, False),
    "NAME": (None, False),
    "PUBLIC_BRANCH": ("main", False),
    "PUBLIC_BOT_NAME": (None, False),
    "PRIVATE_BOT_NAME": (None, False),
    "RELEASE_COMMIT_MESSAGE": ("Release {version} [no changelog]", False),
    # Versions and the changelog
    "FIRST_VERSION": ("0.1.0", False),
    "VERSION_FILE": ([], True),
    "BREAKING_BEFORE_1": ("major", False),
    "CHANGELOG_LINKS": ("0", False),
    "AUTO_PREFIX": ("", False),
    # The changelog check
    "CHANGELOG_VISIBLE": ([], True),
    "CHANGELOG_HIDDEN": (["tests/", ".github/workflows/", ".claude/", "scripts/release/",
                          "CLAUDE.md", "AGENTS.md", "GEMINI.md"], True),
    "CHANGELOG_SINCE": ("kit", False),
    "CHANGELOG_RANGE": ("commit", False),
    # Building and checking the public copy
    "CHECK_SCRIPT": ("", False),
    "BUILTIN_PATTERNS": (None, False),
    "ALLOWED_EMAILS": ([], True),
    "APPROVED_BINARIES": ([], True),
    "MEDIA_EXTENSIONS": ([], True),
    "PNG_STRICT": ("1", False),
    "PE_STRICT": ("1", False),
    "WORDLIST_ON_BINARIES": ("1", False),
    "LINK_CHECK_BACKTICKS": ("0", False),
    "LOCAL_WORDLIST": ("wordlist.local", False),
    "EXCLUDE_STRICT": ("1", False),
    "REQUIRED_FILES": ([".github/FUNDING.yml"], True),
    "HISTORY_FIXUPS": ("private/history-fixups.txt", False),
    "PUBLIC_FIXUPS": ("", False),
    # Publishing
    "DOCS_PATHS": (["README.md", "docs"], True),
    "DOCS_SUBJECT_ALIASES": ([], True),
    "ALLOW_EMPTY_RELEASE": ("1", False),
    # The cut-release workflow
    "TEST_COMMAND": ([], True),
    "TEST_RUNNER": ("ubuntu-latest", False),
    # The public release.yml (copied into the public repository, see PUBLIC_KEYS)
    "RELEASE_TARBALL": ("1", False),
    "RELEASE_ASSET": ([], True),
    "RELEASE_NOTES_FOOTER": ([], True),
    "RELEASE_TEST_COMMAND": ([], True),
    "RELEASE_REQUIRE_PUBLIC": ("1", False),
}

# What the public release.yml needs. build_public.py writes these, and nothing else, to
# .github/release-public.conf in the public copy, where they are checked like every file.
PUBLIC_KEYS = ("PUBLIC_REPO", "NAME", "PUBLIC_BRANCH", "VERSION_FILE", "RELEASE_TARBALL",
               "RELEASE_ASSET", "RELEASE_NOTES_FOOTER", "RELEASE_TEST_COMMAND",
               "RELEASE_REQUIRE_PUBLIC")

BOOLS = ("CHANGELOG_LINKS", "EXCLUDE_STRICT", "BUILTIN_PATTERNS", "PNG_STRICT", "PE_STRICT", "WORDLIST_ON_BINARIES",
         "LINK_CHECK_BACKTICKS", "ALLOW_EMPTY_RELEASE", "RELEASE_TARBALL", "RELEASE_REQUIRE_PUBLIC")
KEY_LINE = re.compile(r"([A-Z][A-Z0-9_]*)\s*=(.*)")
SLUG = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
SEMVER = re.compile(r"\d+\.\d+\.\d+")
# Short names for the common version-file shapes; anything else is a regular expression
# with exactly one group around the version.
VERSION_KINDS = {
    "py": r'^__version__ = "(\d+\.\d+\.\d+)"$',
    "assign": r'^VERSION = "(\d+\.\d+\.\d+)"$',
    "ts": r'^export const VERSION = "(\d+\.\d+\.\d+)";$',
    "plist": r"<key>CFBundleShortVersionString</key>\s*<string>(\d+\.\d+\.\d+)</string>",
}


class ConfError(Exception):
    pass


def parse(text, where="release.conf"):
    """{KEY: value or [items]} for the keys the text sets. Raises ConfError."""
    found = {}
    for number, raw in enumerate(text.lstrip("﻿").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = KEY_LINE.fullmatch(line)
        if not m:
            raise ConfError("%s line %d: expected KEY=VALUE" % (where, number))
        key, value = m.group(1), m.group(2).strip()
        if key not in KEYS:
            raise ConfError("%s line %d: unknown key %s" % (where, number, key))
        if KEYS[key][1]:
            found.setdefault(key, []).append(value)
        elif key in found:
            raise ConfError("%s line %d: %s is set a second time" % (where, number, key))
        else:
            found[key] = value
    return found


class Conf:
    def __init__(self, values=None):
        self.values = dict(values or {})
        self.validate()

    @classmethod
    def load(cls, path=DEFAULT_PATH):
        if not os.path.exists(path):
            return cls({})
        with open(path, encoding="utf-8") as f:
            return cls(parse(f.read(), path))

    def raw(self, key):
        if key not in KEYS:
            raise ConfError("unknown key %s" % key)
        default, is_list = KEYS[key]
        if key in self.values:
            return self.values[key]
        return list(default) if is_list else default

    def get(self, key):
        value = self.raw(key)
        if key == "PRIVATE_REPO" and not value:
            return self.public_repo + "-private" if self.public_repo else ""
        if key == "NAME" and not value:
            return self.public_repo.split("/", 1)[1] if self.public_repo else ""
        if key in ("PUBLIC_BOT_NAME", "PRIVATE_BOT_NAME") and not value:
            return "%s release" % self.get("NAME") if self.get("NAME") else "release"
        if key == "BUILTIN_PATTERNS" and value is None:
            return "0" if self.raw("CHECK_SCRIPT") else "1"
        return value

    def items(self, key):
        """A list key, with each line also split on whitespace (for path and word lists).
        %20 in an item stands for a space (a folder called "Spoken Content", say)."""
        out = []
        for line in self.raw(key):
            out.extend(item.replace("%20", " ") for item in line.split())
        return out

    def flag(self, key):
        return self.get(key) == "1"

    @property
    def public_repo(self):
        return self.raw("PUBLIC_REPO")

    def version_files(self):
        """[(path, compiled regex or None)]: None means the whole file is the version."""
        out = []
        for line in self.raw("VERSION_FILE"):
            path, _sep, pattern = line.partition(" ")
            pattern = pattern.strip()
            if not pattern:
                out.append((path, None))
                continue
            pattern = VERSION_KINDS.get(pattern, pattern)
            rx = re.compile(pattern, re.M)
            out.append((path, rx))
        return out

    def approved_manifests(self):
        """[(manifest path, base folder)], both relative to the repository root."""
        out = []
        for line in self.raw("APPROVED_BINARIES"):
            parts = line.split()
            if not parts:
                continue
            out.append((parts[0], parts[1].rstrip("/") if len(parts) > 1 else ""))
        return out

    def release_assets(self):
        """[(source, name, substitute placeholders?)]"""
        out = []
        for line in self.raw("RELEASE_ASSET"):
            parts = line.split()
            src = parts[0]
            name = parts[1] if len(parts) > 1 else os.path.basename(src)
            out.append((src, name, len(parts) > 2 and parts[2] == "subst"))
        return out

    def validate(self):
        repo = self.public_repo
        if repo and not SLUG.fullmatch(repo):
            raise ConfError("PUBLIC_REPO must look like OWNER/NAME")
        if self.raw("PRIVATE_REPO") and not SLUG.fullmatch(self.raw("PRIVATE_REPO")):
            raise ConfError("PRIVATE_REPO must look like OWNER/NAME")
        if self.raw("PRIVATE_REPO") and not repo:
            raise ConfError("PRIVATE_REPO is set but PUBLIC_REPO isn't")
        if not SEMVER.fullmatch(self.get("FIRST_VERSION")):
            raise ConfError("FIRST_VERSION must look like X.Y.Z")
        if self.get("BREAKING_BEFORE_1") not in ("major", "minor"):
            raise ConfError("BREAKING_BEFORE_1 must be major or minor")
        if self.get("CHANGELOG_RANGE") not in ("commit", "range"):
            raise ConfError("CHANGELOG_RANGE must be commit or range")
        for key in BOOLS:
            if self.get(key) not in ("0", "1"):
                raise ConfError("%s must be 0 or 1" % key)
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", self.get("PUBLIC_BRANCH")):
            raise ConfError("PUBLIC_BRANCH is not a plain branch name")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.get("TEST_RUNNER")):
            raise ConfError("TEST_RUNNER is not a plain runner label")
        for line in self.raw("VERSION_FILE"):
            path, _sep, pattern = line.partition(" ")
            if not path or path.startswith("/") or ".." in path.split("/"):
                raise ConfError("VERSION_FILE needs a relative path inside the repository")
            pattern = VERSION_KINDS.get(pattern.strip(), pattern.strip())
            if pattern:
                try:
                    rx = re.compile(pattern, re.M)
                except re.error:
                    raise ConfError("VERSION_FILE %s: not a valid regular expression" % path) from None
                if rx.groups != 1:
                    raise ConfError("VERSION_FILE %s: the expression needs exactly one group" % path)
        for line in self.raw("RELEASE_ASSET"):
            parts = line.split()
            if not parts or len(parts) > 3 or (len(parts) == 3 and parts[2] != "subst"):
                raise ConfError("RELEASE_ASSET is SOURCE [NAME [subst]]")
            if parts[0].startswith("/") or ".." in parts[0].split("/"):
                raise ConfError("RELEASE_ASSET needs a relative source path")
            if len(parts) > 1 and "/" in parts[1]:
                raise ConfError("RELEASE_ASSET's NAME is a file name, not a path")
        for path, _base in self.approved_manifests():
            if path.startswith("/") or ".." in path.split("/"):
                raise ConfError("APPROVED_BINARIES needs a relative manifest path")
        for line in self.raw("CHANGELOG_VISIBLE") + self.raw("CHANGELOG_HIDDEN"):
            if line.startswith("/"):
                raise ConfError("changelog paths are relative to the repository root")

    def public_text(self):
        """The public subset, as release.conf text."""
        lines = ["# Written by scripts/release/build_public.py from the private release settings.",
                 "# The public release.yml reads it. Don't edit it here: it is replaced on every release."]
        for key in PUBLIC_KEYS:
            value = self.get(key) if key == "NAME" else self.raw(key)
            if isinstance(value, list):
                lines.extend("%s=%s" % (key, v) for v in value)
            else:
                lines.append("%s=%s" % (key, value))
        return "\n".join(lines) + "\n"


def main(argv):
    args = argv[1:]
    conf_path = DEFAULT_PATH
    if "--conf" in args:
        i = args.index("--conf")
        if i + 1 >= len(args):
            print("release_conf: --conf needs a file", file=sys.stderr)
            return 2
        conf_path = args[i + 1]
        del args[i:i + 2]
    try:
        conf = Conf.load(conf_path)
        if args[:1] == ["get"] and len(args) == 2:
            value = conf.get(args[1])
            if isinstance(value, list):
                for item in value:
                    print(item)
            else:
                print(value)
        elif args == ["check"]:
            print("release_conf: OK%s" % ("" if conf.public_repo else
                                         " (no PUBLIC_REPO: the release workflows stay inert)"))
        elif args[:1] == ["public"] and len(args) == 2:
            with open(args[1], "w", encoding="utf-8") as f:
                f.write(conf.public_text())
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (ConfError, OSError, UnicodeDecodeError) as e:
        print("release_conf: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
