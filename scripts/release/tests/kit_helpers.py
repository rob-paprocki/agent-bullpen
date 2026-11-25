"""Shared fixtures for the release kit's tests: throwaway git repositories, fake settings.

Nothing here is a real private value. The word-list patterns are obviously fake, and any
value a pattern check would flag is assembled at run time, so this file passes the checks.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KIT not in sys.path:
    sys.path.insert(0, KIT)

import release_conf  # noqa: E402

FAKE_WORDS = "# fake entries for tests only\nzq-fake-[0-9]{4}\nXYZZY-PLUGH\n"
FAKE_HIT = "zq-fake-" + "4821"          # what the first fake entry matches
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test Person", "GIT_AUTHOR_EMAIL": "person@example.com",
    "GIT_COMMITTER_NAME": "Test Person", "GIT_COMMITTER_EMAIL": "person@example.com",
}


def private_ip():
    return "10." + "20.30.40"


def real_email():
    return "someone" + "@" + "mail-provider.test.org"


def run(*cmd, cwd=None, check=True, env=None):
    e = dict(os.environ)
    e.update(GIT_ENV)
    e.update(env or {})
    return subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, check=check, env=e)


def git(repo, *args, env=None):
    return run("git", "-C", repo, *args, env=env).stdout.strip()


def write(root, rel, content, mode=None):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    flag = "wb" if isinstance(content, bytes) else "w"
    with open(path, flag, **({} if isinstance(content, bytes) else {"encoding": "utf-8", "newline": ""})) as f:
        f.write(content)
    if mode:
        os.chmod(path, mode)
    return path


CHANGELOG = """# Changelog

## [Unreleased]

**Upgrading:** nothing to do.

### Added

- A feature.
"""

CONF = "PUBLIC_REPO=example-owner/demo\nNAME=Demo\n"


class GitCase(unittest.TestCase):
    """A temporary folder with a private repository in it (self.repo)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kit-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "private-repo")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        self.old_env = {k: os.environ.get(k) for k in GIT_ENV}
        os.environ.update(GIT_ENV)
        self.addCleanup(self.restore_env)

    def restore_env(self):
        for k, v in self.old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def seed(self, files=None, conf=CONF, exclude="", message="start"):
        """A first commit with a README, a changelog, the exclude list and the settings."""
        base = {"README.md": "# Demo\n\nSee [the docs](docs/guide.md).\n",
                "docs/guide.md": "Example network: 192.168.100.20\n",
                "CHANGELOG.md": CHANGELOG,
                ".github/FUNDING.yml": "github: [example-owner]\n",
                "private/public-exclude.txt": "# excluded\n" + exclude,
                "private/notes.md": "private notes " + FAKE_HIT + "\n"}
        if conf is not None:
            base["private/release.conf"] = conf
        base.update(files or {})
        for rel, content in base.items():
            if content is not None:  # None leaves a base file out
                write(self.repo, rel, content)
        return self.commit(message)

    def commit(self, message, *paths):
        git(self.repo, "add", "-A", *paths)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def conf(self, text=CONF):
        return release_conf.Conf(release_conf.parse(text))
