"""publish_public.py, reset_public.py and release_assets.py, against local bare repositories."""
import hashlib
import os
import sys
import tarfile

sys.dont_write_bytecode = True
from kit_helpers import CONF, FAKE_WORDS, KIT, GitCase, git, private_ip, run, write  # noqa: E402

import build_public  # noqa: E402
import publish_public  # noqa: E402
import release_assets  # noqa: E402
import release_conf  # noqa: E402
import reset_public  # noqa: E402

BOT = publish_public.BOT_EMAIL


class PublishCase(GitCase):
    def setUp(self):
        super().setUp()
        self.remote = self.path("public.git")
        git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        self.n = 0

    def settings(self, extra=""):
        return publish_public.Settings(self.conf(CONF + extra))

    def clone(self):
        self.n += 1
        dest = self.path("clone-%d" % self.n)
        git(self.tmp, "clone", "-q", self.remote, dest)
        return dest

    def copy(self, files):
        self.n += 1
        root = self.path("copy-%d" % self.n)
        for rel, content in files.items():
            write(root, rel, content)
        return root

    def release(self, version, files, s=None, first_base=None):
        s = s or self.settings()
        clone = self.clone()
        result = publish_public.prepare(clone, self.copy(files), version, s, first_base)
        if result == "prepared":
            publish_public.push(clone, version, s)
        return clone, result

    def test_first_and_second_release(self):
        clone, result = self.release("1.0.0", {"README.md": "one\n", "old.txt": "gone soon\n",
                                               ".gitignore": "*.log\n", "keep.log": "kept\n"})
        self.assertEqual(result, "prepared")
        log = git(clone, "log", "--format=%an|%ae|%cn|%ce|%s|%ad", "--date=iso")
        name, ae, cn, ce, subject, date = log.split("|")
        self.assertEqual((name, ae, cn, ce, subject), ("Demo release", BOT, "Demo release", BOT, "Demo 1.0.0"))
        self.assertTrue(date.endswith("+0000"))
        self.assertIn("keep.log", git(clone, "ls-files"), "no ignore rule may drop a file of the copy")
        clone, _ = self.release("1.1.0", {"README.md": "two\n"})
        self.assertEqual(git(clone, "ls-files"), "README.md")
        self.assertEqual(git(clone, "log", "--format=%s"), "Demo 1.1.0\nDemo 1.0.0")
        self.assertEqual(git(clone, "cat-file", "-t", "v1.1.0"), "tag")

    def test_crlf_files_from_git_archive(self):
        files = {".gitattributes": "* text=auto\n*.bat text eol=crlf\n",
                 "run.bat": "@echo off\r\necho hi\r\n", "README.md": "one\n"}
        clone, result = self.release("1.0.0", files)
        self.assertEqual(result, "prepared")
        self.assertEqual(git(clone, "cat-file", "blob", "HEAD:run.bat"), "@echo off\necho hi")
        files["run.bat"] = "@echo off\r\necho changed\r\n"
        clone = self.clone()
        copy = self.copy(files)
        publish_public.clear_tree(clone)
        for name in os.listdir(copy):
            publish_public.copy_into(os.path.join(copy, name), os.path.join(clone, name))
        git(clone, "add", "-A", "--force")
        with open(os.path.join(copy, "run.bat"), "w", newline="") as f:
            f.write("@echo off\r\necho other\r\n")       # different text, not just endings
        with self.assertRaises(publish_public.PublishError):
            publish_public.check_staged(clone, copy)

    def test_rerun_after_half_done_publish(self):
        self.release("1.0.0", {"README.md": "one\n"})
        clone = self.clone()
        git(clone, "fetch", "-q", "--tags")
        s = self.settings()
        self.assertEqual(publish_public.prepare(clone, self.copy({"README.md": "one\n"}), "1.0.0", s), "exists")
        self.assertEqual(publish_public.push(clone, "1.0.0", s), "unchanged")
        with self.assertRaises(publish_public.PublishError):
            publish_public.prepare(self.clone(), self.copy({"README.md": "different\n"}), "1.0.0", s)

    def test_moved_public_repository(self):
        self.release("1.0.0", {"README.md": "one\n"})
        clone = self.clone()
        write(clone, "README.md", "edited on github.com\n")
        git(clone, "commit", "-qam", "edit")
        git(clone, "push", "-q", "origin", "HEAD:main")
        with self.assertRaises(publish_public.PublishError) as ctx:
            self.release("1.1.0", {"README.md": "two\n"})
        self.assertIn("someone committed there", str(ctx.exception))

    def test_older_version_refused(self):
        self.release("1.2.0", {"README.md": "one\n"})
        with self.assertRaises(publish_public.PublishError):
            self.release("1.1.0", {"README.md": "two\n"})

    def test_docs_commits_are_tolerated(self):
        s = self.settings("DOCS_SUBJECT_ALIASES=old-name docs: update\n")
        self.release("1.0.0", {"README.md": "one\n", "docs/a.md": "a\n", "src/x": "x\n"}, s)
        clone = self.clone()
        copy = self.copy({"README.md": "better\n", "docs/a.md": "b\n", "src/x": "CHANGED\n"})
        self.assertEqual(publish_public.prepare_docs(clone, copy, s), "prepared")
        self.assertEqual(git(clone, "show", "HEAD:src/x"), "x", "a docs publish touches docs only")
        git(clone, "push", "-q", "origin", "HEAD:main")
        clone = self.clone()        # an older-named docs commit counts too
        write(clone, "docs/a.md", "c\n")
        git(clone, "add", "-A")
        git(clone, "commit", "-qm", "old-name docs: update", env={
            "GIT_AUTHOR_EMAIL": BOT, "GIT_COMMITTER_EMAIL": BOT})
        git(clone, "push", "-q", "origin", "HEAD:main")
        clone, result = self.release("1.0.1", {"README.md": "better\n"}, s)
        self.assertEqual(result, "prepared")

    def test_first_release_on_existing_commits(self):
        seed = self.clone()
        write(seed, "LICENSE", "text\n")
        git(seed, "add", "-A")
        git(seed, "commit", "-qm", "Initial commit")
        git(seed, "push", "-q", "origin", "HEAD:main")
        head = git(seed, "rev-parse", "HEAD")
        with self.assertRaises(publish_public.PublishError):
            self.release("0.1.0", {"README.md": "one\n"})
        with self.assertRaises(publish_public.PublishError):
            self.release("0.1.0", {"README.md": "one\n"}, first_base="0000000")
        clone, _ = self.release("0.1.0", {"README.md": "one\n"}, first_base=head[:12])
        self.assertEqual(git(clone, "rev-parse", "HEAD~1"), head)

    def test_empty_release(self):
        self.release("1.0.0", {"README.md": "one\n"})
        self.release("1.0.1", {"README.md": "one\n"})
        with self.assertRaises(publish_public.PublishError):
            self.release("1.0.2", {"README.md": "one\n"}, self.settings("ALLOW_EMPTY_RELEASE=0\n"))

    def test_other_branch(self):
        s = self.settings("PUBLIC_BRANCH=stable\n")
        self.release("1.0.0", {"README.md": "one\n"}, s)
        self.assertTrue(git(self.tmp, "--git-dir", self.remote, "rev-parse", "refs/heads/stable"))

    def test_copy_with_git_entry_refused(self):
        with self.assertRaises(publish_public.PublishError):
            publish_public.prepare(self.clone(), self.copy({".git/config": "x"}), "1.0.0", self.settings())

    def test_cli_needs_settings(self):
        p = run(sys.executable, "-I", os.path.join(KIT, "publish_public.py"), "push", self.clone(), "1.0.0",
                "--conf", os.path.join(self.tmp, "none.conf"), check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("PUBLIC_REPO", p.stderr)


class ResetTests(GitCase):
    def test_rebuild_history(self):
        self.seed({"docs/old.md": "host " + private_ip() + "\n"},
                  conf=CONF + "HISTORY_FIXUPS=private/history-fixups.txt\n")
        git(self.repo, "tag", "-a", "v1.0.0", "-m", "Demo 1.0.0")
        write(self.repo, "notes.txt", "now private\n")
        write(self.repo, "private/public-exclude.txt", "notes.txt\n")
        write(self.repo, "private/history-fixups.txt",
              "docs/old.md\t10\\.\\d+\\.\\d+\\.\\d+\t192.168.100.20\n")
        self.commit("exclude notes")
        git(self.repo, "tag", "-a", "v1.1.0", "-m", "Demo 1.1.0")
        out = self.path("history")
        pats = build_public.parse_wordlist(FAKE_WORDS, "test")
        tags = reset_public.reset(out, pats, self.repo)
        self.assertEqual(tags, ["v1.0.0", "v1.1.0"])
        self.assertEqual(git(out, "log", "--format=%s|%ae"), "Demo 1.1.0|%s\nDemo 1.0.0|%s" % (BOT, BOT))
        self.assertNotIn("notes.txt", git(out, "ls-files"))
        self.assertEqual(git(out, "show", "v1.0.0:docs/old.md"), "host 192.168.100.20")

    def test_a_failing_tag_leaves_nothing(self):
        self.seed({"docs/old.md": "host " + private_ip() + "\n"})
        git(self.repo, "tag", "-a", "v1.0.0", "-m", "Demo 1.0.0")
        out = self.path("history")
        with self.assertRaises(build_public.PublishError) as ctx:
            reset_public.reset(out, build_public.parse_wordlist(FAKE_WORDS, "t"), self.repo)
        self.assertIn("v1.0.0", str(ctx.exception))
        self.assertFalse(os.path.exists(out))


class AssetTests(GitCase):
    def test_release_files_and_notes(self):
        conf = ("PUBLIC_REPO=example-owner/demo\nNAME=Demo App\nRELEASE_ASSET=get.sh get.sh subst\n"
                "RELEASE_ASSET=platforms/a/install.sh install-a.sh\nRELEASE_NOTES_FOOTER=---\n"
                "RELEASE_NOTES_FOOTER=Install @TAG@ from https://github.com/@REPO@\n")
        changelog = ("# Changelog\n\n## [Unreleased]\n\n## [1.2.0] - 2026-05-06\n\n**Upgrading:** none.\n\n"
                     "### Added\n\n- See [guide](docs/guide.md).\n")
        self.seed({"get.sh": "#!/bin/sh\nV=@VERSION@\n", "platforms/a/install.sh": "#!/bin/sh\n",
                   "CHANGELOG.md": changelog})
        os.chmod(os.path.join(self.repo, "get.sh"), 0o755)
        self.commit("assets")
        git(self.repo, "tag", "-a", "v1.2.0", "-m", "Demo App 1.2.0")
        public = self.path("public.conf")
        write(self.tmp, "public.conf", self.conf(conf).public_text())
        dist, notes = self.path("dist"), self.path("notes.md")
        made = release_assets.build("v1.2.0", dist, notes, release_conf.Conf.load(public), self.repo)
        self.assertEqual(sorted(made), ["demo-1.2.0.tar.gz", "get.sh", "install-a.sh"])
        with open(os.path.join(dist, "get.sh"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "#!/bin/sh\nV=1.2.0\n")
        self.assertTrue(os.stat(os.path.join(dist, "get.sh")).st_mode & 0o100)
        with tarfile.open(os.path.join(dist, "demo-1.2.0.tar.gz")) as t:
            self.assertIn("demo-1.2.0/README.md", t.getnames())
        with open(os.path.join(dist, "SHA256SUMS"), encoding="utf-8") as f:
            sums = f.read().splitlines()
        with open(os.path.join(dist, "get.sh"), "rb") as f:
            self.assertIn("%s  get.sh" % hashlib.sha256(f.read()).hexdigest(), sums)
        with open(notes, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("(https://github.com/example-owner/demo/blob/v1.2.0/docs/guide.md)", text)
        self.assertTrue(text.endswith("---\nInstall v1.2.0 from https://github.com/example-owner/demo\n"))

    def test_no_files(self):
        conf = "PUBLIC_REPO=example-owner/demo\nRELEASE_TARBALL=0\n"
        changelog = "# Changelog\n\n## [1.0.0] - 2026-05-06\n\n**Upgrading:** none.\n\n### Added\n\n- x\n"
        self.seed({"CHANGELOG.md": changelog})
        git(self.repo, "tag", "-a", "v1.0.0", "-m", "x")
        made = release_assets.build("v1.0.0", self.path("dist"), self.path("n.md"), self.conf(conf), self.repo)
        self.assertEqual(made, [])
        self.assertEqual(os.listdir(self.path("dist")), [])
