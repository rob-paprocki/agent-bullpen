"""build_public.py and check_patterns.py: the public copy and everything that may stop it."""
import contextlib
import hashlib
import io
import os
import subprocess
import sys
import tarfile
import unittest
import zlib

sys.dont_write_bytecode = True
from kit_helpers import (CONF, FAKE_HIT, FAKE_WORDS, KIT, GitCase, git, private_ip,  # noqa: E402
                         real_email, run, write)

import build_public  # noqa: E402
import check_patterns  # noqa: E402
import release_conf  # noqa: E402


def patterns():
    return build_public.parse_wordlist(FAKE_WORDS, "test")


def png(extra_chunk=None):
    def chunk(kind, data):
        body = kind + data
        return len(data).to_bytes(4, "big") + body + zlib.crc32(body).to_bytes(4, "big")
    ihdr = (1).to_bytes(4, "big") + (1).to_bytes(4, "big") + bytes([8, 0, 0, 0, 0])
    out = check_patterns.PNG_SIG + chunk(b"IHDR", ihdr)
    if extra_chunk:
        out += chunk(extra_chunk, b"Software\x00something")
    return out + chunk(b"IDAT", zlib.compress(b"\x00\x00")) + chunk(b"IEND", b"")


class BuildTests(GitCase):
    runs = 0

    def fresh(self):
        BuildTests.runs += 1
        return self.path("copy-%d" % BuildTests.runs)

    def build(self, ref="HEAD", conf=CONF, messages=(), **kw):
        out = self.fresh()
        files = build_public.build(ref, out, patterns(), messages, self.repo, self.conf(conf), **kw)
        return out, files

    def assertStops(self, fragment=None, conf=CONF, messages=(), **kw):
        out = self.fresh()
        with self.assertRaises(build_public.PublishError) as ctx:
            build_public.build("HEAD", out, patterns(), messages, self.repo, self.conf(conf), **kw)
        text = str(ctx.exception)
        self.assertFalse(os.path.exists(out), "a failed build must leave nothing behind")
        self.assertNotIn(FAKE_HIT, text)
        self.assertNotIn("zq-fake", text)
        if fragment:
            self.assertIn(fragment, text)
        return text

    def test_clean_copy(self):
        self.seed({"tools/run.sh": "#!/bin/sh\necho hi\n", "notes.txt": "x\n"}, exclude="notes.txt\n")
        os.chmod(os.path.join(self.repo, "tools/run.sh"), 0o755)
        self.commit("exec bit")
        out, files = self.build()
        self.assertNotIn("private/notes.md", files)
        self.assertNotIn("notes.txt", files)
        self.assertIn(".github/release-public.conf", files)
        self.assertTrue(os.stat(os.path.join(out, "tools/run.sh")).st_mode & 0o100)
        with open(os.path.join(out, ".github/release-public.conf"), encoding="utf-8") as f:
            public = f.read()
        self.assertIn("PUBLIC_REPO=example-owner/demo", public)
        self.assertIn("NAME=Demo", public)

    def test_public_conf_holds_only_public_keys(self):
        conf = CONF + "ALLOWED_EMAILS=@corp.example.com\nCHECK_SCRIPT=\nTEST_COMMAND=make test\n"
        self.seed(conf=conf)
        out, _files = self.build(conf=conf)
        with open(os.path.join(out, ".github/release-public.conf"), encoding="utf-8") as f:
            public = f.read()
        for word in ("ALLOWED_EMAILS", "CHECK_SCRIPT", "TEST_COMMAND", "corp.example.com"):
            self.assertNotIn(word, public)

    def test_committed_public_conf_is_refused(self):
        self.seed({".github/release-public.conf": "PUBLIC_REPO=x/y\n"})
        self.assertStops("must not be committed")

    def test_no_public_repo_is_refused(self):
        self.seed()
        with self.assertRaises(build_public.PublishError):
            build_public.build("HEAD", self.path("copy"), patterns(), (), self.repo, self.conf(""))

    def test_wordlist_hit_in_content(self):
        self.seed({"docs/more.md": "serial " + FAKE_HIT + "\n"})
        text = self.assertStops("docs/more.md: a word-list entry matches the content")
        self.assertNotIn(FAKE_HIT, text)

    def test_wordlist_hit_in_a_file_name_stops_before_anything_prints_it(self):
        self.seed({"docs/" + FAKE_HIT + ".md": "plain\n", "docs/bad.md": "[x](missing.md)\n"})
        text = self.assertStops("(in sorted order): a word-list entry matches the file name")
        self.assertNotIn("bad.md", text, "later checks must not run after a name hit")

    def test_pattern_hit_in_a_file_name(self):
        self.seed({"logs/" + private_ip() + ".txt": "plain\n"})
        text = self.assertStops("IP address in the file name")
        self.assertNotIn(private_ip(), text)

    def test_wordlist_hit_in_a_message(self):
        self.seed()
        self.assertStops("release message 2", messages=["Demo 1.0.0", "notes " + FAKE_HIT])

    def test_wordlist_is_case_insensitive_and_per_line(self):
        self.seed({"a.md": "first line\nxyzzy-plugh\n"})
        self.assertStops("a.md: a word-list entry matches")

    def test_builtin_patterns(self):
        self.seed({"docs/net.md": "host " + private_ip() + "\n"})
        text = self.assertStops("docs/net.md:1: IP address not allowed")
        self.assertNotIn(private_ip(), text)

    def test_allowed_emails(self):
        addr = real_email()
        self.seed({"docs/contact.md": "write to " + addr + "\n"})
        self.assertStops("email address")
        conf = CONF + "ALLOWED_EMAILS=@" + addr.split("@")[1] + "\n"
        self.build(conf=conf)

    def test_excluded_path_must_exist(self):
        self.seed(exclude="no-such-file\n")
        self.assertStops("not in the release")
        self.build(conf=CONF + "EXCLUDE_STRICT=0\n")

    def test_exclude_override_tolerates_old_tags(self):
        self.seed({"extra/a.txt": "x\n"})
        out, files = self.build(excludes=["no-such-file", "extra"])
        self.assertFalse(any(f.startswith("extra/") for f in files))

    def test_local_wordlist_is_never_published(self):
        self.seed({"wordlist.local": "XYZZY-PLUGH\n"})
        # The local copy of the list is itself a hit, but it is removed before any check runs.
        _out, files = self.build()
        self.assertNotIn("wordlist.local", files)

    def test_symlink_is_refused(self):
        self.seed()
        os.symlink("README.md", os.path.join(self.repo, "link.md"))
        self.commit("link")
        self.assertStops("symbolic link")

    def test_export_subst_is_refused(self):
        self.seed({".gitattributes": "*.txt export-subst\n", "v.txt": "x\n"})
        self.assertStops("export-subst")

    def test_link_checks(self):
        self.seed({"docs/a.md": "see [notes](../private/notes.md)\n"})
        self.assertStops("docs/a.md:1: link to a file that isn't in the public copy")

    def test_code_examples_need_not_exist_but_stay_out_of_private(self):
        self.seed({"docs/a.md": "```html\n<img src=\"docs/images/NAME.svg\">\n```\nUse `[x](y.md)`.\n"})
        self.build()
        write(self.repo, "docs/b.md", "~~~\n[secret](../private/notes.md)\n~~~\n")
        self.commit("example into private")
        self.assertStops("docs/b.md:2")

    def test_missing_link_target(self):
        self.seed({"docs/a.md": '<img src="pics/none.png">\n'})
        self.assertStops("docs/a.md:1")

    def test_backtick_paths(self):
        self.seed({"docs/a.md": "read `docs/none.md` first\n"})
        self.build()
        self.assertStops("names a file", conf=CONF + "LINK_CHECK_BACKTICKS=1\n")

    def test_binary_needs_approval(self):
        self.seed({"docs/images/shot.png": png()})
        self.assertStops("no APPROVED_BINARIES manifest lists it")

    def test_approved_binary_with_base_folder(self):
        data = png()
        manifest = "%s  shot.png\n" % hashlib.sha256(data).hexdigest()
        self.seed({"docs/images/shot.png": data, "docs/images/approved.sha256": manifest})
        conf = CONF + "APPROVED_BINARIES=docs/images/approved.sha256 docs/images\n"
        self.build(conf=conf)

    def test_changed_binary_is_refused(self):
        data = png()
        manifest = "%s  docs/images/shot.png\n" % hashlib.sha256(b"other").hexdigest()
        self.seed({"docs/images/shot.png": data, "approved.sha256": manifest})
        self.assertStops("binary changed", conf=CONF + "APPROVED_BINARIES=approved.sha256\n")

    def test_required_files(self):
        self.seed({".github/FUNDING.yml": None})
        self.assertStops("FUNDING.yml is missing", conf=CONF)
        self.build(conf=CONF + "REQUIRED_FILES=README.md\n")

    def test_png_metadata(self):
        data = png(b"tEXt")
        manifest = "%s  shot.png\n" % hashlib.sha256(data).hexdigest()
        self.seed({"shot.png": data, "approved.sha256": manifest})
        conf = CONF + "APPROVED_BINARIES=approved.sha256\n"
        self.assertStops("metadata chunk", conf=conf)
        self.build(conf=conf + "PNG_STRICT=0\n")

    def test_pe_strings(self):
        data = b"MZ\x90\x00" + b"\x00" * 60 + b"C:" + b"\\" + b"Users" + b"\\" + b"someone\\x.pdb\x00"
        manifest = "%s  lib/a.dll\n" % hashlib.sha256(data).hexdigest()
        self.seed({"lib/a.dll": data, "approved.sha256": manifest})
        conf = CONF + "APPROVED_BINARIES=approved.sha256\n"
        self.assertStops("user folder", conf=conf)
        self.build(conf=conf + "PE_STRICT=0\n")

    def test_media_and_raw_bytes(self):
        clean = b"\xff\xfb\x90\x00 audio frames"
        self.seed({"sounds/a.mp3": clean})
        conf = CONF + "MEDIA_EXTENSIONS=.mp3 .caf\n"
        self.build(conf=conf)
        write(self.repo, "sounds/b.caf", b"\xff\xfe tag " + FAKE_HIT.encode() + b"\xff")
        self.commit("tagged")
        self.assertStops("raw bytes", conf=conf)

    def test_manifest_problems(self):
        self.seed({"approved.sha256": "not a line\n"})
        self.assertStops("needs a SHA-256", conf=CONF + "APPROVED_BINARIES=approved.sha256\n")

    def test_fixups(self):
        self.seed({"docs/old.md": "host " + private_ip() + "\n"})
        fix = [("docs/old.md", build_public.re.compile(r"10\.\d+\.\d+\.\d+"), "192.168.100.20")]
        out, _files = self.build(fixups=fix)
        with open(os.path.join(out, "docs/old.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "host 192.168.100.20\n")

    def test_fixup_patterns(self):
        self.seed({"docs/a.md": "id OLDID here\n", "src/b.swift": "let x = \"OLDID\"\n",
                   "bin/blob.dat": None})
        with open(os.path.join(self.repo, "bin.dat"), "wb") as f:
            f.write(b"\xff\xfe OLDID")
        self.commit("binary")
        fix = [("*", build_public.re.compile("OLDID"), "NEWID")]
        conf = CONF + "APPROVED_BINARIES=approved.sha256\n"
        import hashlib
        with open(os.path.join(self.repo, "bin.dat"), "rb") as f:
            digest = hashlib.sha256(f.read()).hexdigest()
        write(self.repo, "approved.sha256", digest + "  bin.dat\n")
        self.commit("approve")
        out, _files = self.build(conf=conf, fixups=fix)
        for rel in ("docs/a.md", "src/b.swift"):
            with open(os.path.join(out, rel), encoding="utf-8") as f:
                self.assertNotIn("OLDID", f.read())
        with open(os.path.join(out, "bin.dat"), "rb") as f:
            self.assertEqual(f.read(), b"\xff\xfe OLDID")   # a pattern skips binaries
        out, _files = self.build(conf=conf, fixups=[("src/*.swift", build_public.re.compile("OLDID"), "N")])
        with open(os.path.join(out, "docs/a.md"), encoding="utf-8") as f:
            self.assertIn("OLDID", f.read())
        with open(os.path.join(out, "src/b.swift"), encoding="utf-8") as f:
            self.assertIn('"N"', f.read())

    def test_public_fixups_run_on_every_build_before_the_checks(self):
        self.seed({"docs/old.md": "team " + FAKE_HIT + "\n",
                   "private/public-fixups.txt": "*\t" + FAKE_HIT + "\tTEAMID\n"})
        self.assertStops()                                    # without the key: the word list stops it
        out, files = self.build(conf=CONF + "PUBLIC_FIXUPS=private/public-fixups.txt\n")
        self.assertNotIn("private/public-fixups.txt", files)
        with open(os.path.join(out, "docs/old.md"), encoding="utf-8") as f:
            self.assertEqual(f.read(), "team TEAMID\n")
        self.assertStops("PUBLIC_FIXUPS names a file that doesn't exist",
                         conf=CONF + "PUBLIC_FIXUPS=private/missing.txt\n")

    def test_read_fixups(self):
        path = write(self.tmp, "fix.txt", "# c\ndocs/a.md\t[0-9]+\tN\n")
        self.assertEqual(len(build_public.read_fixups(path)), 1)
        write(self.tmp, "bad.txt", "docs/a.md only-two\n")
        with self.assertRaises(build_public.PublishError):
            build_public.read_fixups(os.path.join(self.tmp, "bad.txt"))

    def test_check_script(self):
        script = ("import os, sys\n"
                  "bad = os.path.exists(os.path.join(sys.argv[1], 'docs', 'forbidden.md'))\n"
                  "print('custom: problem' if bad else 'custom: OK')\nsys.exit(1 if bad else 0)\n")
        self.seed({"tests/check_repo.py": script})
        conf = CONF + "CHECK_SCRIPT=tests/check_repo.py\n"
        self.build(conf=conf)
        write(self.repo, "docs/forbidden.md", "x\n")
        self.commit("forbidden")
        self.assertStops("custom: problem", conf=conf)

    def test_check_script_replaces_builtin_patterns_unless_asked(self):
        ok = "import sys\nsys.exit(0)\n"
        self.seed({"tests/check_repo.py": ok, "docs/net.md": private_ip() + "\n"})
        conf = CONF + "CHECK_SCRIPT=tests/check_repo.py\n"
        self.build(conf=conf)
        self.assertStops("IP address", conf=conf + "BUILTIN_PATTERNS=1\n")

    def test_not_empty_output(self):
        self.seed()
        os.makedirs(self.path("copy"))
        write(self.path("copy"), "keep.txt", "mine\n")
        with self.assertRaises(build_public.PublishError):
            build_public.build("HEAD", self.path("copy"), patterns(), (), self.repo, self.conf())
        self.assertTrue(os.path.exists(self.path("copy", "keep.txt")))

    def test_without_tarfile_data_filter(self):
        self.seed({"tools/x.sh": "#!/bin/sh\n"})
        os.chmod(os.path.join(self.repo, "tools/x.sh"), 0o755)
        self.commit("x")
        saved = getattr(tarfile, "data_filter", None)
        if saved is not None:
            del tarfile.data_filter
        try:
            out, files = self.build()
        finally:
            if saved is not None:
                tarfile.data_filter = saved
        self.assertIn("tools/x.sh", files)
        self.assertTrue(os.stat(os.path.join(out, "tools/x.sh")).st_mode & 0o100)

    def test_extract_refuses_unsafe_members(self):
        for name, kind in (("../evil", tarfile.REGTYPE), ("/abs", tarfile.REGTYPE),
                           ("link", tarfile.SYMTYPE)):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as t:
                info = tarfile.TarInfo(name)
                info.type = kind
                if kind == tarfile.SYMTYPE:
                    info.linkname = "/etc/passwd"
                t.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
            with self.assertRaises(build_public.PublishError):
                build_public.extract(buf.getvalue(), self.path("x-" + kind.decode()))

    def test_wordlist_sources(self):
        with self.assertRaises(build_public.PublishError):
            build_public.load_wordlist(None, None, None)
        with self.assertRaises(build_public.PublishError):
            build_public.load_wordlist(None, "# only a comment\n", None)
        with self.assertRaises(build_public.PublishError):
            build_public.load_wordlist(self.path("missing"), None, None)
        with self.assertRaises(build_public.PublishError) as ctx:
            build_public.load_wordlist(None, "ok\n(unclosed\n", None)
        self.assertIn("line 2", str(ctx.exception))
        path = write(self.tmp, "wl.txt", "﻿XYZZY-PLUGH\n")
        local = write(self.tmp, "local.txt", "zq-fake-[0-9]{4}\n")
        pats = build_public.load_wordlist(path, "another-fake\n", local)
        self.assertEqual(len(pats), 3)
        self.assertTrue(pats[0].search("xyzzy-plugh"))

    def test_cli_stops_closed_and_withholds(self):
        self.seed({"docs/more.md": FAKE_HIT + "\n"})
        env = {"PRIVATE_WORDLIST": FAKE_WORDS}
        p = run(sys.executable, "-I", os.path.join(KIT, "build_public.py"), "HEAD", self.path("copy"),
                "--repo", self.repo, cwd=self.tmp, check=False, env=env)
        self.assertEqual(p.returncode, 1)
        self.assertNotIn(FAKE_HIT, p.stdout + p.stderr)
        self.assertIn("STOPPED", p.stderr)
        p = run(sys.executable, "-I", os.path.join(KIT, "build_public.py"), "HEAD", self.path("copy"),
                "--repo", self.repo, cwd=self.tmp, check=False, env={"PRIVATE_WORDLIST": ""})
        self.assertEqual(p.returncode, 1)
        self.assertIn("no word list", p.stderr)

    def test_explain(self):
        self.seed({"docs/more.md": "a\n" + FAKE_HIT + "\n"})
        wl = write(self.tmp, "wl.txt", FAKE_WORDS)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build_public.explain(self.repo, wl)
        self.assertEqual(code, 1)
        self.assertIn("word-list line 2 matches docs/more.md:2", out.getvalue())
        self.assertNotIn("zq-fake", out.getvalue())
        self.assertNotIn("private/", out.getvalue())


class PatternTests(GitCase):
    def test_scanner(self):
        at = "@"
        s = check_patterns.Scanner([at + "corp.example.org", "me" + at + "site.test"])
        self.assertEqual(list(s.scan_text("a@example.com b@x.example.com c@d.invalid")), [])
        self.assertEqual(list(s.scan_text(" ".join(("me" + at + "site.test", "x" + at + "corp.example.org",
                                                    "y" + at + "a.corp.example.org")))), [])
        self.assertEqual([k for _n, k in s.scan_text(real_email())], ["email address"])
        self.assertEqual(list(s.scan_text("192.168.100.1 127.0.0.1 203.0.113.9 2001:db8::1 1.1.1.1")), [])
        self.assertEqual([k for _n, k in s.scan_text("x " + private_ip())], ["IP address"])
        self.assertEqual([k for _n, k in s.scan_text("100." + "64.1.2")], ["IP address"])
        self.assertEqual(list(s.scan_text("00:00:5E:00:53:01")), [])
        self.assertEqual([k for _n, k in s.scan_text("3c:22:" + "fb:01:02:03")], ["MAC address"])
        self.assertEqual([k for _n, k in s.scan_text("fd00" + ":1234::5")], ["IPv6 address"])

    def test_checkout_mode_skips_private_and_excluded(self):
        self.seed({"vendor/x.md": private_ip() + "\n", "private/hosts.md": private_ip() + "\n"},
                  exclude="vendor\n")
        conf = release_conf.Conf({})
        self.assertEqual(check_patterns.check(self.repo, conf), [])
        write(self.repo, "docs/new.md", "ip " + private_ip() + "\n")
        self.commit("leak")
        self.assertEqual(len(check_patterns.check(self.repo, conf)), 1)

    def test_cli(self):
        self.seed()
        p = run(sys.executable, "-I", os.path.join(KIT, "check_patterns.py"), self.repo, cwd=self.tmp,
                check=False)
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertIn("OK", p.stdout)

    def test_the_kit_itself_passes(self):
        """The kit's own files are published with every project: they must pass."""
        problems = check_patterns.check(KIT, release_conf.Conf({}), strict=True)
        problems = [p for p in problems if "__pycache__" not in p]
        self.assertEqual(problems, [])


class SyntaxTests(unittest.TestCase):
    def test_python39(self):
        import ast
        for d in (KIT, os.path.join(KIT, "tests")):
            for name in sorted(os.listdir(d)):
                if name.endswith(".py"):
                    with open(os.path.join(d, name), encoding="utf-8") as f:
                        ast.parse(f.read(), name, feature_version=(3, 9))

    def test_shebangs(self):
        for name in ("build_public.py", "publish_public.py", "changelog.py", "changelog_check.py",
                     "check_patterns.py", "release_conf.py", "reset_public.py", "release_assets.py"):
            with open(os.path.join(KIT, name), encoding="utf-8") as f:
                self.assertEqual(f.readline().strip(), "#!/usr/bin/python3 -I", name)

    def test_workflows_name_no_project(self):
        root = os.path.dirname(os.path.dirname(KIT))
        for name in ("cut-release.yml", "release.yml", "release-kit.yml"):
            path = os.path.join(root, ".github", "workflows", name)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("_WORDLIST:", text.replace("PRIVATE_WORDLIST:", ""), name)
            self.assertNotRegex(text, r"github\.repository == '[^']+'", name)


if __name__ == "__main__":
    unittest.main()
