#!/usr/bin/env python3
"""Tests for the release scripts in scripts/release/: how next-version.py picks a version, the whole
chain (next-version.py, bump-version.py, release-notes.py) in a throwaway copy of the repository, and
the checks tests/check_repo.py makes before anything is published.

    python3 tests/test_release.py

Standard library only; it changes nothing in this checkout.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("next_version", ROOT / "scripts" / "release" / "next-version.py")
nv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nv)
cr_spec = importlib.util.spec_from_file_location("check_repo", ROOT / "tests" / "check_repo.py")
cr = importlib.util.module_from_spec(cr_spec)
cr_spec.loader.exec_module(cr)

TOP = "# Changelog\n\nIntro.\n\n"
OLD = "## [0.3.0] - 2026-11-06\n\n### Added\n\n- Something old.\n\n[0.3.0]: https://example.com/v0.3.0\n"


UPGRADE = "**Upgrading:** nothing to do.\n\n"


def log(unreleased: str, heading: str = "## [Unreleased]", upgrading: str = UPGRADE) -> str:
    return TOP + heading + "\n\n" + upgrading + unreleased + "\n" + OLD


class Decide(unittest.TestCase):
    def check(self, changelog, want, current="0.3.0", tags=("v0.3.0",), bump="auto"):
        got, why = nv.decide(current, changelog, set(tags), bump)
        self.assertEqual(got, want, why)
        self.assertTrue(why)

    def test_fixes_only_raise_the_last_number(self):
        self.check(log("### Fixed\n\n- A fix.\n\n### Security\n\n- Another."), "0.3.1")

    def test_features_raise_the_middle_number(self):
        self.check(log("### Added\n\n- A feature."), "0.4.0")
        self.check(log("### Fixed\n\n- A fix.\n\n### Changed\n\n- A change."), "0.4.0")
        self.check(log("### Removed\n\n- Gone."), "0.4.0")  # before 1.0

    def test_notes_without_a_heading_count_as_a_feature(self):
        self.check(log("- Something."), "0.4.0")
        self.check(log("### Docs\n\n- Unknown heading."), "0.4.0")

    def test_breaking(self):
        self.check(log("### Changed\n\n- **Breaking:** the config moved."), "0.4.0")
        self.check(log("### Fixed\n\n- **Breaking:** the old flag is gone."), "0.4.0")  # before 1.0, even under Fixed
        self.check(log("### Changed\n\n- **Breaking:** the config moved."), "2.0.0", current="1.2.3", tags=["v1.2.3"])
        self.check(log("### Removed\n\n- Gone."), "2.0.0", current="1.2.3", tags=["v1.2.3"])
        self.check(log("### Added\n\n- A feature."), "1.3.0", current="1.2.3", tags=["v1.2.3"])
        self.check(log("### Fixed\n\n- A fix."), "1.2.4", current="1.2.3", tags=["v1.2.3"])

    def test_nothing_to_release(self):
        self.check(log(""), None)
        self.check(log("### Added\n\n### Fixed\n"), None)
        self.check(log("<!-- Add notes here.\n- not a note -->"), None)
        self.check(TOP + OLD, None)  # the newest section is dated and tagged

    def test_the_run_can_choose_the_number(self):
        self.check(log("### Added\n\n- A feature."), "0.3.1", bump="patch")
        self.check(log("### Fixed\n\n- A fix."), "1.0.0", bump="major")
        self.check(log(""), None, bump="minor")

    def test_a_named_version(self):
        self.check(log("### Fixed\n\n- A fix.", "## [0.5.0] - Unreleased"), "0.5.0")
        self.check(log("### Fixed\n\n- A fix.", "## [0.5.0] - Unreleased"), "0.5.0", bump="patch")
        self.check(log("", "## [0.5.0] - Unreleased"), None)
        # The first release: VERSION already says it, and nothing is tagged yet.
        self.check(log("- Notes.", "## [0.3.0] - Unreleased"), "0.3.0", tags=())

    def test_counts_from_the_newest_tag(self):
        self.check(log("### Fixed\n\n- A fix."), "0.3.2", tags=["v0.3.0", "v0.3.1", "not-a-version"])

    def test_problems_need_a_person(self):
        for changelog, tags in [
            (TOP + OLD, ()),                                            # dated, but not tagged
            (log("- Notes.", "## [0.2.0] - Unreleased"), ("v0.3.0",)),  # older than VERSION
            (log("- Notes.", "## [0.3.0] - Unreleased"), ("v0.3.0",)),  # already tagged
            (log("- Notes.", "## [0.3.1] - Unreleased"), ("v0.3.0", "v0.4.0")),  # behind the newest tag
            ("# Changelog\n", ()),                                      # no sections
            # A second unreleased section: one release would leave the other's notes behind.
            (TOP + "## [Unreleased]\n\n### Fixed\n\n- A fix.\n\n## [0.4.0] - Unreleased\n\n- A feature.\n\n" + OLD,
             ("v0.3.0",)),
            (TOP + "## [0.3.1] - 2026-11-07\n\n- A fix.\n\n## [0.4.0] - Unreleased\n\n- A feature.\n\n" + OLD,
             ("v0.3.0", "v0.3.1")),
        ] + [
            # Headings in no form the scripts read: never taken for "already released".
            (log("### Fixed\n\n- A fix.", heading), ("v0.3.0",))
            for heading in ("## Unreleased", "## [unreleased]", "## [Unreleased] - next", "## [0.4.0]",
                            "## [0.4.0] - TBD", "## [0.4.0] \u2013 Unreleased")
        ]:
            with self.assertRaises(nv.Problem, msg=changelog):
                nv.decide("0.3.0", changelog, set(tags))

    def test_a_release_needs_an_upgrading_line(self):
        with self.assertRaisesRegex(nv.Problem, r"\*\*Upgrading:\*\*"):
            nv.decide("0.3.0", log("### Fixed\n\n- A fix.", upgrading=""), {"v0.3.0"})
        with self.assertRaisesRegex(nv.Problem, r"\*\*Upgrading:\*\*"):  # not the first line
            nv.decide("0.3.0", log("### Fixed\n\n- A fix.\n\n**Upgrading:** nothing to do.", upgrading=""),
                      {"v0.3.0"})
        with self.assertRaisesRegex(nv.Problem, r"\*\*Upgrading:\*\*"):  # a comment first: the release notes would start with it
            nv.decide("0.3.0", log("### Fixed\n\n- A fix.", upgrading="<!-- note -->\n**Upgrading:** nothing to do.\n\n"),
                      {"v0.3.0"})
        with self.assertRaisesRegex(nv.Problem, r"\*\*Upgrading:\*\*"):  # a named version too
            nv.decide("0.3.0", log("### Fixed\n\n- A fix.", "## [0.5.0] - Unreleased", upgrading=""), {"v0.3.0"})

    def test_the_upgrading_line_is_not_a_note(self):
        self.check(log("### Fixed\n\n- A fix."), "0.3.1")  # still a patch, not a minor
        self.check(log("", upgrading=UPGRADE), None)  # the line alone is nothing to release

    def test_a_bare_upgrading_marker_is_not_enough(self):
        with self.assertRaises(nv.Problem):
            nv.decide("0.3.0", log("### Fixed\n\n- A fix.", upgrading="**Upgrading:**\n\n"), {"v0.3.0"})

    def test_upgrading_steps_dont_make_a_major(self):
        steps = "**Upgrading:** restart the runner after updating.\n\n"
        self.check(log("### Added\n\n- A feature.", upgrading=steps), "1.3.0", current="1.2.3", tags=["v1.2.3"])

    def test_the_link_list_is_not_notes(self):
        self.check(TOP + "## [Unreleased]\n\n[0.3.0]: https://example.com/v0.3.0\n", None)


def workflow_step(workflow: Path, name: str) -> str:
    """The run: script of the step called `name` in a workflow file (enough YAML for this repo's files)."""
    lines = workflow.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.strip() != f"- name: {name}":
            continue
        for j in range(i + 1, len(lines)):
            text = lines[j].strip()
            if text.startswith("- name:") or text.startswith("- uses:"):
                break
            if text.startswith("run: ") and text != "run: |":
                return text[len("run: "):]
            if text == "run: |":
                indent = len(lines[j]) - len(lines[j].lstrip())
                body = []
                for following in lines[j + 1:]:
                    if following.strip() and len(following) - len(following.lstrip()) <= indent:
                        break
                    body.append(following)
                return textwrap.dedent("\n".join(body))
    raise AssertionError(f"{workflow.name}: no step '{name}' with a run: script")


def run(cmd, cwd, env=None, check=True):
    result = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if check and result.returncode:
        raise AssertionError(f"{cmd} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result


GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", "-c", "tag.gpgSign=false",
       "-c", "commit.gpgSign=false"]


def throwaway_repo(tmp: Path) -> Path:
    """A copy of this repository in tmp/repo (tracked files plus new ones not yet committed, but not ignored
    ones, so it works before a commit), as a git repository with one commit."""
    repo = tmp / "repo"
    files = run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], ROOT).stdout.split("\0")
    for rel in filter(None, files):
        if (ROOT / rel).is_file():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, repo / rel)
    run(["git", "init", "-q"], repo)
    commit_all(repo, "start")
    return repo


def commit_all(repo: Path, message: str = "change") -> None:
    run(GIT + ["add", "-A"], repo)
    run(GIT + ["commit", "-qm", message], repo)


WORD = "zebra" + "finch"  # a made-up private word (in two pieces so this file, copied into the throwaway repo, does not match it)
WORD_ENV = {**os.environ, "BULLPEN_DENYLIST": WORD}


class PrivateFolder(unittest.TestCase):
    """Private words belong in private/ and in the files public-exclude.txt lists, so check_repo.py's word
    check leaves those alone (the public copy is checked separately, without skips)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def check(self, repo):
        return run([sys.executable, "-I", "tests/check_repo.py"], repo, env=WORD_ENV, check=False)

    def test_private_files_are_skipped_by_the_word_check(self):
        repo = throwaway_repo(self.tmp)
        (repo / "private").mkdir(exist_ok=True)
        (repo / "private" / "public-exclude.txt").write_text(
            "# a note\n\n.github/workflows/private.yml\nsome/dir/\n")
        (repo / "private" / "notes.md").write_text(WORD + "\n")
        (repo / "some" / "dir").mkdir(parents=True)
        (repo / "some" / "dir" / "inside.txt").write_text(WORD + "\n")
        (repo / ".github" / "workflows" / "private.yml").write_text("# " + WORD + "\n")
        commit_all(repo)
        checked = self.check(repo)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        (repo / "docs" / "x.md").write_text(WORD + "\n")
        commit_all(repo)
        again = self.check(repo)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("docs/x.md", again.stdout)
        self.assertNotIn(WORD, again.stdout)

    def test_a_private_word_is_never_printed(self):
        # A hit names the file, the line and the entry's number; what the entry matches in a file name is
        # shown as <private>. An entry that isn't a valid expression is named by its number too: Python's
        # error message can quote part of it.
        repo = throwaway_repo(self.tmp)
        (repo / "docs" / "x.md").write_text("A note.\nSee " + WORD + ".\n")
        (repo / "docs" / (WORD + "-notes.md")).write_text(WORD + "\n")
        commit_all(repo)
        env = {**WORD_ENV, "BULLPEN_DENYLIST": WORD + "\n(?P<1" + WORD + ">x)\n"}
        checked = run([sys.executable, "-I", "tests/check_repo.py"], repo, env=env, check=False)
        self.assertEqual(checked.returncode, 1, checked.stdout)
        hint = "use a neutral word (it matches a word on your private list)"
        for line in ("private denylist entry #2 isn't a valid regular expression",
                     f"docs/x.md:2: personal reference (private entry #1): {hint}",
                     f"docs/<private>-notes.md:1: personal reference (private entry #1): {hint}"):
            self.assertIn(line, checked.stdout.splitlines())
        self.assertIn("3 problem(s) found.", checked.stdout)
        self.assertNotIn(WORD, checked.stdout + checked.stderr)


def joined(sep: str, *parts: str) -> str:
    """A value check_repo.py must report, built from pieces: written whole, it would be reported in this file."""
    return sep.join(parts)


class PersonalValues(unittest.TestCase):
    """check_repo.py reports email, IP and MAC addresses that aren't one of the allowed examples, by file, line
    and kind, never by value. Private addresses in examples come from 192.168.100.0/24."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def kinds(self, line):
        return [k for k, _ in cr.personal_values(line)]

    def test_allowed_emails(self):
        for line in ("hi@rpaprocki.com", "you@example.com", "a@git.example.org", "b@sub.example.net",
                     "x@host.invalid", "y@box.test", "z@site.example", "git@github.com:me/repo.git",
                     "41898282+github-actions[bot]@users.noreply.github.com",
                     "systemctl start container-getty@1.service",
                     "etc/systemd/system/container-getty@1.service.d/override.conf",
                     "pip install 'pkg@1.2.3'", "the decorator @app.get('/')"):
            self.assertEqual(self.kinds(line), [], line)

    def test_other_emails_are_reported_without_the_value(self):
        for line in ("someone" + "@" + "gmail.com", "me" + "@" + "icloud.com",
                     "first.last" + "@" + "company.co.uk", "1234567+someone" + "@" + "users.noreply.github.com",
                     "nobody" + "@" + "example.com.evil.org","getty" + "@" + "tty1.service.org"):
            found = cr.personal_values(line)
            self.assertEqual([k for k, _ in found], ["email address"], line)
            self.assertNotIn(line.split("@")[0], found[0][1])

    def test_allowed_addresses(self):
        for line in ("192.168.100.20", "192.168.100.1", "192.168.100.0/24", "http://192.168.100.20:8080",
                     "127.0.0.1", "0.0.0.0", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9",
                     "192.0.2.5", "198.51.100.7", "203.0.113.9", "255.255.255.0", "255.255.254.0",
                     "2001:db8::1", "2001:db8::/32", "[2001:db8::1]:8080", "::1", "::",
                     "version 1.2.3", "a long version 1.2.3.4.5", "v1.2.3.4", "lines[::2]", "x[1::3]", "x[::-1]",
                     "at 12:34:56", "00:00:00", "[Math]::Abs(1)", "00:00:5E:00:53:01", "00-00-5e-00-53-ff",
                     # Reverse-DNS names read backwards: these are 192.168.100.20, 192.168.100.0, 192.0.2.0
                     # and 127.0.0.1.
                     "20.100.168.192.in-addr.arpa", "100.168.192.in-addr.arpa", "2.0.192.in-addr.arpa",
                     "1.0.0.127.IN-ADDR.ARPA."):
            self.assertEqual(self.kinds(line), [], line)

    def test_reported_addresses(self):
        address = joined(".", "10", "1", "2", "3")
        for line, kind in ((joined(".", "192", "168", "1", "20"), "IP address"),
                           # Inside a longer dotted token that isn't a version: wildcard DNS, a file name.
                           (address + ".nip.io", "IP address"),
                           ("https://app." + address + ".sslip.io/", "IP address"),
                           ("fixtures/" + address + ".json", "IP address"),
                           ("host_" + address, "IP address"),
                           # Reverse DNS, read backwards: the address above, then a 192.168 address ending
                           # in .127 (loopback if read forwards), then the zone of a 192.168 network.
                           (joined(".", "3", "2", "1", "10") + ".in-addr.arpa", "IP address"),
                           (joined(".", "127", "1", "168", "192") + ".in-addr.arpa", "IP address"),
                           ("zone " + joined(".", "1", "168", "192") + ".IN-ADDR.ARPA", "IP address"),
                           (joined(".", "10", "0", "0", "50"), "IP address"),
                           (joined(".", "172", "16", "5", "4"), "IP address"),
                           (joined(".", "100", "101", "102", "103"), "IP address"),
                           (joined(".", "169", "254", "1", "1"), "IP address"),
                           (joined(".", "203", "0", "114", "9"), "IP address"),
                           (joined(".", "10", "0", "0", "0") + "/8", "IP address"),
                           ("http://" + joined(".", "10", "1", "2", "3") + ":8080/", "IP address"),
                           ("ends a sentence: " + joined(".", "10", "1", "2", "3") + ".", "IP address"),
                           (joined(":", "2001", "470", "", "1"), "IP address"),
                           (joined(":", "fd12", "3456", "", "1"), "IP address"),
                           (joined(":", "fe80", "", "1"), "IP address"),
                           ("[" + joined(":", "fe80", "", "1") + "]:8080", "IP address"),
                           (joined(":", "a4", "83", "e7", "12", "34", "56"), "MAC address"),
                           (joined("-", "A4", "83", "E7", "12", "34", "56"), "MAC address")):
            self.assertEqual(self.kinds(line), [kind], line)

    def test_values_come_in_the_order_of_the_line_with_their_hints(self):
        line = " ".join([joined(":", "a4", "83", "e7", "12", "34", "56"), "me" + "@" + "gmail.com",
                         joined(".", "10", "0", "0", "50")])
        self.assertEqual(cr.personal_values(line), [
            ("MAC address", "use 00:00:5E:00:53:xx"),
            ("email address", "use hi@rpaprocki.com or an example.com address"),
            ("IP address", "use an address in 192.168.100.0/24 (private) or 192.0.2.0/24 (public)")])

    def test_a_long_run_of_letters_is_read_once(self):
        # A minified line can hold a long run of letters with no @; a search that starts again from every
        # position takes minutes on this one.
        line = "a" * 200000 + " " + "me" + "@" + "gmail.com " + "b" * 200000
        self.assertEqual(self.kinds(line), ["email address"])

    def test_mask_replaces_each_value_with_its_kind(self):
        address = joined(".", "10", "1", "2", "3")
        self.assertEqual(cr.mask("fixtures/" + address + ".json:3: x"), "fixtures/<IP address>.json:3: x")
        self.assertEqual(cr.mask("to " + "a" + "@" + address + ".nip.io now"), "to <email address> now")
        self.assertEqual(cr.mask(joined(".", "3", "2", "1", "10") + ".in-addr.arpa"), "<IP address>.in-addr.arpa")
        allowed = "use hi@rpaprocki.com or 192.168.100.20, not 00:00:5E:00:53:01"
        self.assertEqual(cr.mask(allowed), allowed)

    def check(self, repo, env=None):
        return run([sys.executable, "-I", "tests/check_repo.py"], repo, env=env, check=False)

    def test_file_names_are_checked_and_printed_masked(self):
        address = joined(".", "10", "1", "2", "3")
        email = "me" + "@" + "gmail.com"
        mac = joined("-", "A4", "83", "E7", "12", "34", "56")
        repo = throwaway_repo(self.tmp)
        (repo / "fixtures").mkdir()
        (repo / "fixtures" / (address + ".json")).write_text('{"owner": "' + email + '"}\n')
        (repo / "notes").mkdir()
        (repo / "notes" / (mac + ".txt")).write_text("Nothing to see.\n")
        (repo / "private").mkdir(exist_ok=True)
        (repo / "private" / (address + ".txt")).write_text("Private names are not published.\n")
        commit_all(repo)
        checked = self.check(repo)
        self.assertEqual(checked.returncode, 1, checked.stdout)
        for line in ("fixtures/<IP address>.json: file name holds an IP address that isn't an allowed example: "
                     "use an address in 192.168.100.0/24 (private) or 192.0.2.0/24 (public)",
                     "fixtures/<IP address>.json:1: email address that isn't an allowed example: "
                     "use hi@rpaprocki.com or an example.com address",
                     "notes/<MAC address>.txt: file name holds a MAC address that isn't an allowed example: "
                     "use 00:00:5E:00:53:xx"):
            self.assertIn(line, checked.stdout.splitlines())
        self.assertIn("3 problem(s) found.", checked.stdout)
        for value in (address, email, mac):
            self.assertNotIn(value, checked.stdout)

    def test_files_that_are_not_utf8_text_are_reported_by_path(self):
        # The word and address checks read UTF-8 text only, so any other file fails closed, named by its path.
        email = "me" + "@" + "gmail.com"
        repo = throwaway_repo(self.tmp)
        png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        (repo / "docs" / "picture.png").write_bytes(png)  # NUL bytes
        # Windows PowerShell 5.1 writes UTF-16 with a byte-order mark, and CRLF line ends.
        (repo / "scripts" / "windows" / "out.txt").write_bytes(("A note.\r\n" + email + "\r\n").encode("utf-16"))
        (repo / "docs" / "latin1.txt").write_bytes("Café au lait.\n".encode("latin-1"))
        (repo / "docs" / "plain.txt").write_bytes("Café au lait.\n".encode("utf-8"))
        (repo / "private").mkdir(exist_ok=True)
        (repo / "private" / "picture.png").write_bytes(png)
        commit_all(repo)
        checked = self.check(repo, env=WORD_ENV)
        self.assertEqual(checked.returncode, 1, checked.stdout)
        lines = checked.stdout.splitlines()
        for rel in ("docs/picture.png", "scripts/windows/out.txt", "docs/latin1.txt"):
            self.assertIn(f"{rel}: not UTF-8 text, so the word and personal-value checks can't read it: keep "
                          "binary and UTF-16 files out of the repository", lines)
            self.assertNotIn(f"warning: {rel}", checked.stdout)
        self.assertIn("3 problem(s) found.", checked.stdout)
        for absent in ("docs/plain.txt", "private/", email):
            self.assertNotIn(absent, checked.stdout)

    def test_the_check_skips_private_and_reports_file_and_line(self):
        address = "me" + "@" + "gmail.com"
        repo = throwaway_repo(self.tmp)
        (repo / "private").mkdir(exist_ok=True)
        (repo / "private" / "notes.md").write_text(address + "\n")
        commit_all(repo)
        checked = run([sys.executable, "-I", "tests/check_repo.py"], repo, check=False)
        self.assertEqual(checked.returncode, 0, checked.stdout)
        (repo / "docs" / "x.md").write_text("A note.\n" + address + "\n")
        commit_all(repo)
        again = run([sys.executable, "-I", "tests/check_repo.py"], repo, check=False)
        self.assertEqual(again.returncode, 1, again.stdout)
        self.assertIn("docs/x.md:2: email address that isn't an allowed example: use hi@rpaprocki.com or an "
                      "example.com address", again.stdout)
        self.assertNotIn(address, again.stdout)
        self.assertNotIn("private/notes.md", again.stdout)


class WholeChain(unittest.TestCase):
    """next-version.py, bump-version.py, release-notes.py, check_repo.py and release.yml's own steps on a
    throwaway copy of this repository, whatever state its CHANGELOG.md is in."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = throwaway_repo(self.tmp)
        self.git = GIT
        self.py = [sys.executable, "-I"]
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")}
        self.version = (self.repo / "VERSION").read_text().strip()

    def script(self, name, *args, check=True, **env):
        return run(self.py + [f"scripts/release/{name}", *args], self.repo, env={**self.env, **env}, check=check)

    def changelog(self, text=None):
        path = self.repo / "CHANGELOG.md"
        if text is None:
            return path.read_text()
        path.write_text(text)

    def commit_and_tag(self, version):
        run(self.git + ["commit", "-qam", f"Release {version}"], self.repo)
        run(self.git + ["tag", "-a", f"v{version}", "-m", "release"], self.repo)

    def released_state(self):
        """VERSION released and tagged, as in the real repository; unreleased notes set aside."""
        text = self.changelog()
        if f"## [{self.version}] - Unreleased" in text:
            # Before the first release: release it here, as a maintainer would.
            self.assertEqual(self.script("next-version.py").stdout.strip(), self.version)
            self.script("bump-version.py", self.version)
            run(self.git + ["commit", "-qam", f"Release {self.version}"], self.repo)
            # Dated but not tagged: a person has to look.
            self.assertEqual(self.script("next-version.py", check=False).returncode, 1)
            run(self.git + ["tag", "-a", f"v{self.version}", "-m", "release"], self.repo)
            return
        run(self.git + ["tag", "-a", f"v{self.version}", "-m", "release"], self.repo)
        first = text.index("\n## ") + 1
        if not nv.DATED.match(text[first:].split("\n", 1)[0]):
            following = text.find("\n## ", first)
            self.changelog(text[:first] + text[following + 1:])
            run(self.git + ["commit", "-qam", "set aside the unreleased notes"], self.repo)

    def release_steps(self, version) -> str:
        """release.yml's own steps, in the shell GitHub runs them in (bash -eo pipefail), with `python`
        on the PATH as in the workflow. Returns the release notes."""
        shim = self.tmp / "bin"
        if not shim.exists():
            shim.mkdir()
            (shim / "python").symlink_to(sys.executable)
        env = {**self.env, "TAG": f"v{version}", "PATH": f"{shim}{os.pathsep}{self.env.get('PATH', '')}"}
        workflow = self.repo / ".github" / "workflows" / "release.yml"
        for name in ("Check the tag matches every version number", "Repository checks for a release",
                     "Build agent-bullpen-X.Y.Z.tar.gz and SHA256SUMS", "Release notes from CHANGELOG.md"):
            result = run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", workflow_step(workflow, name)],
                         self.repo, env=env, check=False)
            self.assertEqual(result.returncode, 0, f"release.yml step '{name}':\n{result.stdout}\n{result.stderr}")
        dist = self.repo / "dist"
        self.assertTrue((dist / f"agent-bullpen-{version}.tar.gz").is_file())
        run(["sha256sum", "-c", "SHA256SUMS"], dist)
        notes = (dist / "notes.md").read_text()
        shutil.rmtree(dist)
        return notes

    def test_a_fix_release(self):
        self.released_state()
        done = self.script("next-version.py")
        self.assertEqual(done.stdout.strip(), "", done.stderr)  # nothing new yet

        # A fix goes in under ## [Unreleased]; the next version raises the last number.
        text = self.changelog()
        first = text.index("\n## ") + 1
        self.changelog(text[:first] + "## [Unreleased]\n\n" + UPGRADE + "### Fixed\n\n- A test fix.\n\n" + text[first:])
        major, minor, patch = (int(x) for x in self.version.split("."))
        want = f"{major}.{minor}.{patch + 1}"
        out = self.tmp / "github_output"
        self.assertEqual(self.script("next-version.py", GITHUB_OUTPUT=str(out)).stdout.strip(), want)
        self.assertEqual(out.read_text(), f"version={want}\n")

        self.script("bump-version.py", want)
        self.assertEqual((self.repo / "VERSION").read_text().strip(), want)
        dated = self.changelog()
        self.assertRegex(dated, rf"(?m)^## \[{want}\] - \d{{4}}-\d{{2}}-\d{{2}}\n\n\*\*Upgrading:\*\* nothing to do\.\n\n### Fixed$")  # blank lines kept
        self.assertIn(f"/releases/tag/v{want}", dated)
        for rel in ("hub/app.py", "runner/runner.py"):
            self.assertIn(f'VERSION = "{want}"', (self.repo / rel).read_text())
        self.commit_and_tag(want)

        notes = self.release_steps(want)
        self.assertIn("A test fix.", notes)
        self.assertTrue(notes.startswith("**Upgrading:**"))
        self.assertNotIn(f"[{self.version}]", notes)
        done = self.script("next-version.py")
        self.assertEqual((done.returncode, done.stdout.strip()), (0, ""), done.stderr)

    def test_problems_stop_before_anything_changes(self):
        self.released_state()
        text = self.changelog()
        first = text.index("\n## ") + 1
        watched = ("VERSION", "hub/app.py", "runner/runner.py", "CHANGELOG.md")
        for top in ("## [Unreleased]\n\n" + UPGRADE + "### Fixed\n\n- A fix.\n\n"
                    "## [9.9.9] - Unreleased\n\n" + UPGRADE + "- A feature.\n\n",
                    "## Unreleased\n\n" + UPGRADE + "### Fixed\n\n- A fix.\n\n"):
            with self.subTest(top=top.split("\n", 1)[0]):
                self.changelog(text[:first] + top + text[first:])
                before = {rel: (self.repo / rel).read_bytes() for rel in watched}
                self.assertEqual(self.script("next-version.py", check=False).returncode, 1)
                bumped = self.script("bump-version.py", "9.9.9", check=False)
                self.assertNotEqual(bumped.returncode, 0)
                self.assertIn("Nothing was changed", bumped.stderr)
                self.assertEqual({rel: (self.repo / rel).read_bytes() for rel in watched}, before)
                checked = run(self.py + ["tests/check_repo.py"], self.repo, env=self.env, check=False)
                self.assertEqual(checked.returncode, 1, checked.stdout)
                self.assertIn("CHANGELOG.md:", checked.stdout)


class Workflows(unittest.TestCase):
    def test_release_yml_runs_only_in_the_public_repository(self):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("github.repository == 'rob-paprocki/agent-bullpen'", text)
        self.assertNotIn("workflow_call", text)

    def test_there_is_no_schedule(self):
        self.assertFalse((ROOT / ".github/workflows/scheduled-release.yml").exists())
        for wf in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("schedule:", wf.read_text(), wf.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
