#!/usr/bin/env python3
"""Tests for tests/check_repo.py: the private/ folder and the excluded paths, and the personal-value
checks (emails, IPs, MACs). The release tooling's own tests are in scripts/release/tests (the
.default-settings release kit).

    python3 tests/test_check_repo.py

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
cr_spec = importlib.util.spec_from_file_location("check_repo", ROOT / "tests" / "check_repo.py")
cr = importlib.util.module_from_spec(cr_spec)
cr_spec.loader.exec_module(cr)

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
WORD_ENV = {**os.environ, "PRIVATE_WORDLIST": WORD}


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
        env = {**WORD_ENV, "PRIVATE_WORDLIST": WORD + "\n(?P<1" + WORD + ">x)\n"}
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


class Workflows(unittest.TestCase):
    def test_there_is_no_schedule(self):
        self.assertFalse((ROOT / ".github/workflows/scheduled-release.yml").exists())
        for wf in (ROOT / ".github/workflows").glob("*.yml"):
            self.assertNotIn("schedule:", wf.read_text(), wf.name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
