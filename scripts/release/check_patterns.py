#!/usr/bin/python3 -I
"""Fail if a file that would be published holds a personal-looking value.

  check_patterns.py [DIR] [--conf FILE]     (default DIR: the checkout this file is in)

It checks every git-tracked file (or every file under DIR when DIR isn't a git
checkout), except private/, .git/, node_modules/ and the paths that
private/public-exclude.txt lists. build_public.py runs the same
checks on the public copy with nothing skipped.

  * email addresses: only placeholders (example.com and its subdomains, .invalid),
    git@github.com, noreply@github.com, the GitHub Actions bot, and what
    ALLOWED_EMAILS in private/release.conf adds ("user@host" for one address,
    "@domain" for a domain and its subdomains);
  * IPv4/IPv6 addresses: only the example network 192.168.100.0/24, loopback,
    0.0.0.0 and 255.255.255.255, the documentation ranges and a few well-known
    public resolvers;
  * MAC addresses: only the documentation range 00:00:5E:00:53:xx;
  * files that aren't UTF-8 text: only binaries that a manifest named in
    APPROVED_BINARIES pins by SHA-256 (PNG and PE files get extra checks), and
    files with an extension in MEDIA_EXTENSIONS.

This is an allow-list on purpose: it catches real values without them ever being
written down here (this file is published). Messages name the file, the line and
the KIND of value, never the value. A file whose NAME is flagged is called
"file #N (in sorted order)".
"""
import hashlib
import ipaddress
import os
import re
import subprocess
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import release_conf  # noqa: E402

EMAIL = re.compile(r"[A-Za-z0-9._%+\[\]-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]*\d)")
IPV6 = re.compile(r"(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])[0-9A-Fa-f]{2}([:-])[0-9A-Fa-f]{2}(?:\1[0-9A-Fa-f]{2}){4}"
                 r"(?![0-9A-Fa-f:-])")

BASE_EMAILS = {"git@github.com", "noreply@github.com",
               "41898282+github-actions[bot]@users.noreply.github.com"}
EXAMPLE_NET = ipaddress.ip_network("192.168.100.0/24")
ALLOWED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]
ALLOWED_IPS = {ipaddress.ip_address(a) for a in (
    "0.0.0.0", "255.255.255.255", "1.1.1.1", "1.0.0.1", "8.8.8.8", "8.8.4.4", "9.9.9.9",
    "::", "::1")}

PNG_SIG = b"\x89PNG\r\n\x1a\n"
PNG_CHUNKS_OK = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"sRGB", b"gAMA", b"cHRM", b"pHYs",
                 b"tRNS", b"bKGD"}
PNG_MAX = 2 * 1024 * 1024
PE_MAX = 1024 * 1024
BS = chr(92)
# A user folder or an email address inside a PE file's strings.
PE_BAD = re.compile("[A-Za-z]:" + re.escape(BS) + "|" + re.escape(BS * 2) + r"[A-Za-z0-9_.-]+"
                    + re.escape(BS) + "|/home/|/Users/|/root/|/mnt/[a-z]/|" + EMAIL.pattern)
MANIFEST_LINE = re.compile(r"([0-9a-fA-F]{64})\s+(\S.*)")


class Scanner:
    """The pattern checks, with the project's extra allowed email addresses."""

    def __init__(self, allowed_emails=()):
        self.emails = set(BASE_EMAILS)
        self.domains = set()
        for item in allowed_emails:
            item = item.strip().lower()
            if item.startswith("@"):
                self.domains.add(item[1:])
            elif item:
                self.emails.add(item)

    def email_ok(self, addr):
        a = addr.lower()
        domain = a.split("@", 1)[1]
        if a in self.emails or domain.endswith(".invalid"):
            return True
        return any(domain == d or domain.endswith("." + d) for d in self.domains | {"example.com"})

    @staticmethod
    def ip_ok(ip):
        if ip.version == 4 and ip in EXAMPLE_NET:
            return True
        return ip in ALLOWED_IPS or any(ip in n for n in ALLOWED_NETS if n.version == ip.version)

    def scan_text(self, text):
        """Yield (line_number, kind) for every value that isn't allowed."""
        for n, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                if not self.email_ok(m.group(0)):
                    yield n, "email address"
            for m in IPV4.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:          # leading zeros and the like: not a value we can vouch for
                    yield n, "IP address"
                    continue
                if not self.ip_ok(ip):
                    yield n, "IP address"
            for m in IPV6.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                if not self.ip_ok(ip):
                    yield n, "IPv6 address"
            for m in MAC.finditer(line):
                if not m.group(0).upper().replace("-", ":").startswith("00:00:5E:00:53:"):
                    yield n, "MAC address"


def load_manifests(root, conf):
    """({path: sha256}, problems) from the APPROVED_BINARIES manifests found under ROOT. A bad
    line, a path listed twice or a path that isn't a regular file is a problem; no message
    prints a listed path."""
    approved, problems = {}, []
    for manifest, base in conf.approved_manifests():
        full = os.path.join(root, manifest)
        if not os.path.lexists(full):
            continue                     # left out of this tree (an old tag, or not made yet)
        try:
            with open(full, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError):
            problems.append("%s: can't be read" % manifest)
            continue
        for number, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            where = "%s line %d" % (manifest, number)
            m = MANIFEST_LINE.fullmatch(line)
            if not m:
                problems.append("%s: needs a SHA-256, spaces and a path" % where)
                continue
            sha, rel = m.group(1).lower(), m.group(2).strip()
            rel = "%s/%s" % (base, rel) if base else rel
            target = os.path.join(root, rel)
            if rel in approved:
                problems.append("%s: lists a path a second time" % where)
            elif (rel.startswith("/") or ".." in rel.split("/") or os.path.islink(target)
                  or not os.path.isfile(target)):
                problems.append("%s: the path isn't in the tree as a regular file" % where)
            else:
                approved[rel] = sha
    return approved, problems


def png_problem(data):
    if len(data) > PNG_MAX:
        return "PNG is larger than 2 MB"
    pos, ended = len(PNG_SIG), False
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        if kind not in PNG_CHUNKS_OK:
            return "PNG has a metadata chunk (not allowed)"
        pos += 12 + length
        if kind == b"IEND":
            ended = True
            break
    if not ended or pos != len(data):
        return "PNG is malformed or has data after its end"
    return None


def pe_strings(data):
    for m in re.finditer(rb"[\x20-\x7e]{5,}", data):
        yield m.group().decode("ascii")
    for m in re.finditer(rb"(?:[\x20-\x7e]\x00){5,}", data):
        yield m.group().decode("utf-16le")


def pe_problem(data):
    if len(data) > PE_MAX:
        return "PE file is larger than 1 MB"
    if any(PE_BAD.search(s) for s in pe_strings(data)):
        return "an embedded string looks like a user folder or an email address (withheld)"
    return None


def binary_problem(rel, data, conf, approved, patterns=()):
    """None if the non-UTF-8 file REL (content DATA) may be published; otherwise why not.
    PATTERNS (the word list) also runs over the raw bytes when WORDLIST_ON_BINARIES is on."""
    media = any(rel.lower().endswith(ext.lower()) for ext in conf.items("MEDIA_EXTENSIONS"))
    if not media:
        if rel not in approved:
            return "not UTF-8 text, and no APPROVED_BINARIES manifest lists it"
        if approved[rel] != hashlib.sha256(data).hexdigest():
            return "binary changed since it was approved; look at it, then update its SHA-256"
        if data.startswith(PNG_SIG) and conf.flag("PNG_STRICT"):
            bad = png_problem(data)
            if bad:
                return bad
        if data.startswith(b"MZ") and conf.flag("PE_STRICT"):
            bad = pe_problem(data)
            if bad:
                return bad
    if patterns and conf.flag("WORDLIST_ON_BINARIES"):
        blob = data.decode("latin-1")
        if any(p.search(blob) for p in patterns):
            return "a word-list entry matches the raw bytes"
    return None


def files_in(root, strict=False):
    """STRICT (the public copy, published as it is): every file under ROOT. Otherwise the
    git-tracked files of a checkout, or every file under ROOT."""
    if not strict and (os.path.isdir(os.path.join(root, ".git"))
                       or os.path.isfile(os.path.join(root, ".git"))):
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], capture_output=True,
                             check=True).stdout.decode("utf-8", "surrogateescape")
        return [f for f in out.split("\0") if f]
    found = []
    for d, dirs, names in os.walk(root):
        if not strict:
            dirs[:] = [x for x in dirs if x not in (".git", "node_modules")]
        for name in names:
            if not strict and os.path.basename(d) == "__pycache__" and name.endswith(".pyc"):
                continue
            found.append(os.path.relpath(os.path.join(d, name), root).replace(os.sep, "/"))
    return found


def excluded_paths(root):
    """The paths private/public-exclude.txt lists: they never reach the public copy."""
    try:
        with open(os.path.join(root, "private", "public-exclude.txt"), encoding="utf-8") as f:
            return [l.strip().rstrip("/") for l in f if l.strip() and not l.startswith("#")]
    except OSError:
        return []


def skipped(rel, excluded=()):
    """private/ at the top, .git/ or node_modules/ anywhere, and the excluded paths."""
    parts = rel.split("/")
    if parts[0] == "private" or ".git" in parts or "node_modules" in parts:
        return True
    return any(rel == p or rel.startswith(p + "/") for p in excluded)


def check(root, conf, strict=False, counted=None, binaries=True):
    """Problems under ROOT. A finding about a file name never prints the name. BINARIES False
    leaves files that aren't UTF-8 text to the caller (build_public.py checks them itself)."""
    scanner = Scanner(conf.items("ALLOWED_EMAILS"))
    approved, problems = load_manifests(root, conf) if binaries else ({}, [])
    excluded = () if strict else excluded_paths(root)
    scanned = 0
    for index, rel in enumerate(sorted(files_in(root, strict)), 1):
        if not strict and skipped(rel, excluded):
            continue
        path = os.path.join(root, rel)
        if not os.path.lexists(path):                     # tracked, deleted, not yet committed
            continue
        name_kinds = [kind for _line, kind in scanner.scan_text(rel)]
        label = "file #%d (in sorted order)" % index if name_kinds else rel
        scanned += 1
        for kind in name_kinds:
            problems.append("%s: %s in the file name" % (label, kind))
        if os.path.islink(path) or not os.path.isfile(path):
            problems.append("%s: not a regular file" % label)
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            problems.append("%s: can't be read" % label)
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            bad = binary_problem(rel, data, conf, approved) if binaries else None
            if bad:
                problems.append("%s: %s" % (label, bad))
            continue
        for line, kind in scanner.scan_text(text):
            problems.append("%s:%d: %s not allowed in public files" % (label, line, kind))
    if counted is not None:
        counted.append(scanned)
    return problems


def main(argv):
    args = argv[1:]
    conf_path = None
    if "--conf" in args:
        i = args.index("--conf")
        conf_path = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    root = args[0] if args else os.path.dirname(os.path.dirname(HERE))
    if not os.path.isdir(root):
        print("check_patterns: the directory to check does not exist")
        return 1
    try:
        conf = release_conf.Conf.load(conf_path or os.path.join(root, release_conf.DEFAULT_PATH))
    except (release_conf.ConfError, OSError, UnicodeDecodeError) as e:
        print("check_patterns: %s" % e)
        return 1
    counted = []
    problems = check(root, conf, counted=counted)
    if not problems and not counted[0]:
        problems = ["no files were checked"]
    for p in problems:
        print(p)
    print("check_patterns: %s" % ("%d problem(s)" % len(problems) if problems else "OK"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
