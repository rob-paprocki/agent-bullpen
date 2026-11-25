#!/usr/bin/python3 -I
"""Build a release's download files and notes, in the public repository.

  release_assets.py TAG DIST NOTES [--conf FILE] [--repo DIR]
      (default --conf: .github/release-public.conf, which the release build writes)

  * DIST/<repo>-X.Y.Z.tar.gz: git archive of TAG in a folder <repo>-X.Y.Z/, when
    RELEASE_TARBALL=1 (<repo> is the name part of PUBLIC_REPO);
  * DIST/<name> for each RELEASE_ASSET "SOURCE [NAME [subst]]", copied from the
    checkout; with "subst", @VERSION@, @TAG@ and @REPO@ in it are replaced;
  * DIST/SHA256SUMS over those files, when there are any;
  * NOTES: the tag's CHANGELOG.md section (relative links pointing at the files at
    that tag), then the RELEASE_NOTES_FOOTER lines, with the same placeholders.

DIST must not exist yet. The public release.yml runs this; nothing here is pushed.
"""
import hashlib
import os
import re
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True      # a run must leave no bytecode behind

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import changelog  # noqa: E402
import release_conf  # noqa: E402


class AssetError(Exception):
    pass


def substitute(text, version, tag, repo):
    return text.replace("@VERSION@", version).replace("@TAG@", tag).replace("@REPO@", repo)


def build(tag, dist, notes_path, conf, repo_dir="."):
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise AssetError("the tag must look like vX.Y.Z")
    if os.path.lexists(dist):
        raise AssetError("%s already exists" % dist)
    repo = conf.public_repo
    if not repo:
        raise AssetError("the settings name no PUBLIC_REPO")
    version = tag[1:]
    os.makedirs(dist)
    made = []
    if conf.flag("RELEASE_TARBALL"):
        base = "%s-%s" % (repo.split("/", 1)[1], version)
        out = os.path.join(dist, base + ".tar.gz")
        subprocess.run(["git", "-C", repo_dir, "archive", "--format=tar.gz", "--prefix=%s/" % base,
                        "-o", os.path.abspath(out), tag], check=True)
        made.append(base + ".tar.gz")
    for src, name, subst in conf.release_assets():
        full = os.path.join(repo_dir, src)
        if os.path.islink(full) or not os.path.isfile(full):
            raise AssetError("RELEASE_ASSET %s is not a regular file in the checkout" % src)
        if name in made or name == "SHA256SUMS":
            raise AssetError("two release files would be called %s" % name)
        dest = os.path.join(dist, name)
        if subst:
            with open(full, encoding="utf-8", newline="") as f:
                text = f.read()
            with open(dest, "w", encoding="utf-8", newline="") as f:
                f.write(substitute(text, version, tag, repo))
            shutil.copymode(full, dest)
        else:
            shutil.copy2(full, dest)
        made.append(name)
    if made:
        lines = []
        for name in sorted(made):
            with open(os.path.join(dist, name), "rb") as f:
                lines.append("%s  %s\n" % (hashlib.sha256(f.read()).hexdigest(), name))
        with open(os.path.join(dist, "SHA256SUMS"), "w", encoding="utf-8") as f:
            f.write("".join(lines))
    with open(os.path.join(repo_dir, changelog.PATH), encoding="utf-8") as f:
        notes = changelog.notes(f.read(), version, repo)
    footer = conf.raw("RELEASE_NOTES_FOOTER")
    if footer:
        notes = notes.rstrip("\n") + "\n\n" + "\n".join(substitute(l, version, tag, repo) for l in footer)
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write(notes.rstrip("\n") + "\n")
    return made


def main(argv):
    args = argv[1:]
    try:
        conf_path = os.path.join(".github", "release-public.conf")
        repo_dir = "."
        for flag in ("--conf", "--repo"):
            if flag in args:
                i = args.index(flag)
                value = args[i + 1]
                del args[i:i + 2]
                if flag == "--conf":
                    conf_path = value
                else:
                    repo_dir = value
        if len(args) != 3:
            raise AssetError("usage: release_assets.py TAG DIST NOTES [--conf FILE] [--repo DIR]")
        if not os.path.isfile(conf_path):
            raise AssetError("%s is missing" % conf_path)
        made = build(args[0], args[1], args[2], release_conf.Conf.load(conf_path), repo_dir)
    except (AssetError, changelog.ChangelogError, release_conf.ConfError, IndexError, OSError,
            subprocess.CalledProcessError) as e:
        print("release_assets: %s" % e, file=sys.stderr)
        return 1
    print("release_assets: %d file(s)%s" % (len(made), (": " + ", ".join(made)) if made else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
