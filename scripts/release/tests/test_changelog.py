"""changelog.py, changelog_check.py and release_conf.py."""
import os
import re
import sys
import unittest

sys.dont_write_bytecode = True
from kit_helpers import CHANGELOG, KIT, GitCase, git, run, write  # noqa: E402

import changelog  # noqa: E402
import changelog_check  # noqa: E402
import release_conf  # noqa: E402

RELEASED = """# Changelog

## [Unreleased]

**Upgrading:** nothing to do.

%s

## [1.4.2] - 2026-01-02

**Upgrading:** nothing to do.

### Fixed

- Old fix.
"""


def with_notes(body):
    return RELEASED % body


class PlanTests(unittest.TestCase):
    def test_first_release(self):
        self.assertEqual(changelog.plan(CHANGELOG)["version"], "0.1.0")
        self.assertEqual(changelog.plan(CHANGELOG, first="1.0.0")["version"], "1.0.0")

    def test_auto_bumps(self):
        cases = {"### Fixed\n\n- x": "1.4.3", "### Security\n\n- x": "1.4.3",
                 "### Added\n\n- x": "1.5.0", "### Deprecated\n\n- x": "1.5.0",
                 "### Removed\n\n- x": "2.0.0", "### Fixed\n\n- **Breaking** x": "2.0.0"}
        for body, want in cases.items():
            self.assertEqual(changelog.plan(with_notes(body))["version"], want, body)

    def test_forced_bump(self):
        self.assertEqual(changelog.plan(with_notes("### Fixed\n\n- x"), "major")["version"], "2.0.0")
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(with_notes("### Fixed\n\n- x"), "huge")

    def test_comments_do_not_count(self):
        body = "<!-- ### Removed\n- **Breaking** example -->\n### Fixed\n\n- x"
        self.assertEqual(changelog.plan(with_notes(body))["version"], "1.4.3")

    def test_breaking_before_1(self):
        text = (RELEASED % "### Removed\n\n- x").replace("1.4.2", "0.4.2")
        self.assertEqual(changelog.plan(text)["version"], "1.0.0")
        self.assertEqual(changelog.plan(text, breaking_before_1="minor")["version"], "0.5.0")

    def test_needs_upgrading_line_and_headings(self):
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(with_notes("### Fixed\n\n- x").replace("**Upgrading:** nothing to do.\n\n### F",
                                                                     "### F", 1))
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(CHANGELOG.replace("### Added\n\n", ""))
        empty = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n**Upgrading:** x\n\n### Added\n- a\n"
        with self.assertRaises(changelog.ChangelogError) as ctx:
            changelog.plan(empty)
        self.assertIn("nothing to release", str(ctx.exception))

    def test_tags_raise_the_base_and_block_reuse(self):
        text = with_notes("### Fixed\n\n- x")
        self.assertEqual(changelog.plan(text, tags={"v1.4.2", "v1.6.0"})["version"], "1.6.1")
        explicit = text.replace("## [Unreleased]", "## [1.6.0] - Unreleased")
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(explicit, tags={"v1.6.0"})

    def test_version_chosen_by_hand(self):
        text = with_notes("### Fixed\n\n- x").replace("## [Unreleased]", "## [3.0.0] - Unreleased")
        self.assertEqual(changelog.plan(text)["version"], "3.0.0")
        new = changelog.finalize(text, "3.0.0", "2026-02-03")
        self.assertIn("## [Unreleased]\n\n## [3.0.0] - 2026-02-03\n\n**Upgrading:**", new)
        with self.assertRaises(changelog.ChangelogError):
            changelog.finalize(text, "3.0.1", "2026-02-03")

    def test_unreleased_below_released_is_refused(self):
        text = CHANGELOG + "\n## [1.0.0] - 2026-01-01\n\n- a\n\n## [Unreleased]\n\n- b\n"
        with self.assertRaises(changelog.ChangelogError):
            changelog.plan(text)

    def test_finalize_and_notes(self):
        text = with_notes("### Added\n\n- See [the guide](docs/guide.md) and [site](https://example.com).")
        new = changelog.finalize(text, "1.5.0", "2026-03-04")
        self.assertTrue(new.startswith("# Changelog\n\n## [Unreleased]\n\n## [1.5.0] - 2026-03-04\n\n"
                                       "**Upgrading:** nothing to do."))
        self.assertEqual(changelog.plan(new.replace("## [Unreleased]\n", "## [Unreleased]\n\n**Upgrading:** "
                                                    "x\n\n### Fixed\n- y\n", 1))["version"], "1.5.1")
        notes = changelog.notes(new, "1.5.0", "example-owner/demo")
        self.assertIn("](https://github.com/example-owner/demo/blob/v1.5.0/docs/guide.md)", notes)
        self.assertIn("](https://example.com)", notes)
        with self.assertRaises(changelog.ChangelogError):
            changelog.finalize(new, "1.5.0", "2026-03-04")
        with self.assertRaises(changelog.ChangelogError):
            changelog.finalize(text, "1.5.0", "March")

    def test_changelog_links(self):
        text = with_notes("### Added\n\n- x") + "\n[Unreleased]: https://example.com/compare\n" \
            "[1.4.2]: https://github.com/example-owner/demo/releases/tag/v1.4.2\n"
        new = changelog.finalize(text, "1.5.0", "2026-03-04", "example-owner/demo")
        self.assertIn("[1.5.0]: https://github.com/example-owner/demo/releases/tag/v1.5.0\n[1.4.2]:", new)
        self.assertNotIn("[Unreleased]:", new)
        self.assertNotIn("[1.4.2]:", changelog.notes(new, "1.4.2"))

    def test_autoline(self):
        prefix = "Upstream moved to commit "
        text = "# Changelog\n\n## [Unreleased]\n\n## [1.0.0] - 2026-01-01\n\n**Upgrading:** x\n\n### Added\n\n- a\n"
        new = changelog.autoline(text, prefix + "abc123", prefix)
        self.assertEqual(changelog.plan(new, "patch")["version"], "1.0.1")
        self.assertEqual(changelog.autoline(new, prefix + "abc123", prefix), new)
        with self.assertRaises(changelog.ChangelogError):
            changelog.autoline(with_notes("### Added\n\n- by hand"), prefix + "x", prefix)
        with self.assertRaises(changelog.ChangelogError):
            changelog.autoline(text, "other text", prefix)
        with self.assertRaises(changelog.ChangelogError):
            changelog.autoline(text, prefix + "x", "")


class VersionFileTests(GitCase):
    def test_finalize_writes_every_version_file(self):
        conf = ("PUBLIC_REPO=example-owner/demo\nVERSION_FILE=VERSION\nVERSION_FILE=app/__init__.py py\n"
                "VERSION_FILE=web/version.ts ts\nVERSION_FILE=hub.py assign\n"
                "VERSION_FILE=Info.plist plist\n"
                'VERSION_FILE=pkg.json "version": "(\\d+\\.\\d+\\.\\d+)"\n')
        self.seed({"VERSION": "0.0.9\n", "app/__init__.py": '__version__ = "0.0.9"\n',
                   "web/version.ts": 'export const VERSION = "0.0.9";\n', "hub.py": 'VERSION = "0.0.9"\n',
                   "Info.plist": "<key>CFBundleShortVersionString</key>\n<string>0.0.9</string>\n",
                   "pkg.json": '{"version": "0.0.9"}\n'}, conf=conf)
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "finalize", "0.1.0", "2026-04-05",
                cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "verify-version", "0.1.0",
                cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.repo, "pkg.json"), encoding="utf-8") as f:
            self.assertEqual(f.read(), '{"version": "0.1.0"}\n')
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "verify-version", "0.2.0",
                cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 1)

    def test_a_bad_version_file_changes_nothing(self):
        conf = "PUBLIC_REPO=example-owner/demo\nVERSION_FILE=VERSION\nVERSION_FILE=hub.py assign\n"
        self.seed({"VERSION": "0.0.9\n", "hub.py": "no version line\n"}, conf=conf)
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "finalize", "0.1.0", "2026-04-05",
                cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_plan_cli_reads_first_version_and_tags(self):
        self.seed(conf="PUBLIC_REPO=example-owner/demo\nFIRST_VERSION=2.2.0\n")
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "plan", cwd=self.repo)
        self.assertIn('"version": "2.2.0"', p.stdout)
        git(self.repo, "tag", "v2.2.0")
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog.py"), "plan", cwd=self.repo)
        self.assertIn('"version": "2.3.0"', p.stdout)


class ChangelogCheckTests(GitCase):
    def setUp(self):
        super().setUp()
        self.seed(conf="PUBLIC_REPO=example-owner/demo\nCHANGELOG_SINCE=\n", exclude="vendor\n")
        git(self.repo, "tag", "-a", "v0.1.0", "-m", "Demo 0.1.0")
        self.conf_ = release_conf.Conf.load(os.path.join(self.repo, "private/release.conf"))

    def change(self, rel, message, content="x\n"):
        write(self.repo, rel, content)
        return self.commit(message)

    def check(self, conf=None, base=None):
        conf = conf or self.conf_
        base = base or changelog_check.default_base(repo=self.repo)
        return changelog_check.flagged(base, conf, repo=self.repo,
                                       listed=changelog_check.skip_list(repo=self.repo))

    def test_flags_visible_changes_only(self):
        self.change("src/a.py", "feature")
        self.change("tests/test_a.py", "tests")
        self.change("vendor/x", "excluded")
        self.change("private/plan.md", "private")
        self.change("docs/b.md", "typo [no changelog]")
        bad, _ = self.check()
        self.assertEqual([b[1] for b in bad], ["feature"])
        write(self.repo, "CHANGELOG.md", CHANGELOG + "- more\n")
        self.change("src/c.py", "with its line")
        bad, _ = self.check()
        self.assertEqual([b[1] for b in bad], ["feature"])

    def test_allow_list_and_anywhere_hidden(self):
        conf = self.conf(self.conf_text("CHANGELOG_VISIBLE=src/ README.md\nCHANGELOG_VISIBLE=Spoken%20Content/\n"
                                        "CHANGELOG_HIDDEN=*/tests/\n"))
        self.change("Spoken Content/a.txt", "spaced")
        self.change("other/x.txt", "not listed")
        self.change("src/tests/t.py", "nested tests")
        self.change("src/m.py", "module")
        bad, _ = self.check(conf)
        self.assertEqual([b[1] for b in bad], ["module", "spaced"])

    def conf_text(self, extra):
        return "PUBLIC_REPO=example-owner/demo\nCHANGELOG_SINCE=\n" + extra

    def test_skip_and_ignore_lists(self):
        a = self.change("src/a.py", "a")
        b = self.change("src/b.py", "b")
        write(self.repo, "private/changelog-skip.txt", "# reviewed\n%s  went in by mistake\n" % a)
        write(self.repo, "private/changelog-ignore.txt", "%s line came later\n" % b[:10])
        bad, skipped = self.check()
        self.assertEqual((bad, skipped), ([], 2))
        write(self.repo, "private/changelog-skip.txt", "%s\n" % a)
        with self.assertRaises(changelog_check.ListError):
            changelog_check.skip_list(repo=self.repo)
        write(self.repo, "private/changelog-skip.txt", "%s  unknown\n" % ("0" * 40))
        with self.assertRaises(changelog_check.ListError):
            changelog_check.skip_list(repo=self.repo)

    def test_range_mode(self):
        self.change("src/a.py", "a")
        write(self.repo, "CHANGELOG.md", CHANGELOG + "- a\n")
        self.commit("line for a")
        self.assertEqual(len(self.check()[0]), 1)
        self.assertEqual(self.check(self.conf(self.conf_text("CHANGELOG_RANGE=range\n")))[0], [])

    def test_floor(self):
        old = self.change("src/old.py", "before the kit")
        write(self.repo, changelog_check.THIS, "# the check\n")
        self.commit("add the check [no changelog]")
        self.change("src/new.py", "after the kit")
        self.assertEqual(len(self.check()[0]), 2)
        bad, _ = self.check(self.conf(self.conf_text("").replace("CHANGELOG_SINCE=\n", "")))
        self.assertEqual([b[1] for b in bad], ["after the kit"])
        bad, _ = self.check(self.conf(self.conf_text("").replace("CHANGELOG_SINCE=", "CHANGELOG_SINCE=" + old)))
        self.assertEqual(len(bad), 1)

    def test_cli(self):
        self.change("src/a.py", "feature")
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog_check.py"), cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("feature", p.stdout)
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog_check.py"), "0" * 40, cwd=self.repo,
                check=False)
        self.assertEqual(p.returncode, 1, "an all-zero base falls back to the newest tag")
        write(self.repo, "private/changelog-skip.txt", "garbage\n")
        p = run(sys.executable, "-I", os.path.join(KIT, "changelog_check.py"), cwd=self.repo, check=False)
        self.assertEqual(p.returncode, 2)


class ConfTests(unittest.TestCase):
    def test_defaults_are_inert(self):
        conf = release_conf.Conf({})
        self.assertEqual(conf.public_repo, "")
        self.assertEqual(conf.get("NAME"), "")
        self.assertEqual(conf.get("PRIVATE_REPO"), "")
        self.assertEqual(conf.get("BUILTIN_PATTERNS"), "1")

    def test_derived_values(self):
        conf = release_conf.Conf(release_conf.parse("PUBLIC_REPO=example-owner/demo-app\n"))
        self.assertEqual(conf.get("NAME"), "demo-app")
        self.assertEqual(conf.get("PRIVATE_REPO"), "example-owner/demo-app-private")
        self.assertEqual(conf.get("PUBLIC_BOT_NAME"), "demo-app release")
        conf = release_conf.Conf(release_conf.parse("PUBLIC_REPO=a/b\nCHECK_SCRIPT=tests/check_repo.py\n"))
        self.assertEqual(conf.get("BUILTIN_PATTERNS"), "0")

    def test_parse_errors(self):
        bad = ["NOPE=1\n", "PUBLIC_REPO=a/b\nPUBLIC_REPO=c/d\n", "just words\n", "PUBLIC_REPO=not a slug\n",
               "PNG_STRICT=yes\n", "VERSION_FILE=x.py ^no group$\n", "VERSION_FILE=../x\n",
               "RELEASE_ASSET=a b/c\n", "BREAKING_BEFORE_1=patch\n", "FIRST_VERSION=1.0\n",
               "TEST_RUNNER=ubuntu latest; rm\n", "PRIVATE_REPO=a/b\n"]
        for text in bad:
            with self.assertRaises(release_conf.ConfError, msg=text):
                release_conf.Conf(release_conf.parse(text))

    def test_lists_and_comments(self):
        values = release_conf.parse("# c\n\nTEST_COMMAND=make test\nTEST_COMMAND=bash -n x.sh\n"
                                    "ALLOWED_EMAILS=@a.example.com b@example.com\nRELEASE_NOTES_FOOTER=\n")
        conf = release_conf.Conf(values)
        self.assertEqual(conf.get("TEST_COMMAND"), ["make test", "bash -n x.sh"])
        self.assertEqual(conf.items("ALLOWED_EMAILS"), ["@a.example.com", "b@example.com"])
        self.assertEqual(conf.get("RELEASE_NOTES_FOOTER"), [""])

    def test_public_text_round_trip(self):
        conf = release_conf.Conf(release_conf.parse(
            "PUBLIC_REPO=a/b\nVERSION_FILE=VERSION\nRELEASE_ASSET=x.sh\nCHECK_SCRIPT=t.py\n"))
        again = release_conf.Conf(release_conf.parse(conf.public_text()))
        self.assertEqual(again.public_repo, "a/b")
        self.assertEqual(again.raw("VERSION_FILE"), ["VERSION"])
        self.assertEqual(again.get("CHECK_SCRIPT"), "")

    def test_example_file_parses(self):
        example = os.path.join(os.path.dirname(os.path.dirname(KIT)), "private", "release.conf.example")
        if not os.path.exists(example):
            self.skipTest("no private/ here (a published copy)")
        conf = release_conf.Conf.load(example)
        self.assertEqual(conf.public_repo, "")
        with open(example, encoding="utf-8") as f:
            documented = set(re.findall(r"^#([A-Z][A-Z0-9_]*)=", f.read(), re.M))
        self.assertEqual(documented, set(release_conf.KEYS))

    def test_cli(self):
        p = run(sys.executable, "-I", os.path.join(KIT, "release_conf.py"), "get", "PUBLIC_BRANCH",
                "--conf", os.devnull + "-missing", check=False)
        self.assertEqual(p.stdout.strip(), "main")


if __name__ == "__main__":
    unittest.main()
