"""Clone URLs used by both the hub tests and the runner tests, so the two copies of
url_has_userinfo (hub/redact.py and runner/runner.py) are held to the same cases."""

OK = ["https://github.com/a/b.git", "git@example.com:a/b.git", "ssh://git@example.com/a/b.git",
      "https://example.com/a/b%40c.git"]
BAD = ["https://u:p@example.com/a/b.git", "https://p@example.com/a/b.git", "ssh://u:pw@build-box/a/b.git",
       # A password holding a raw ? or #: the '@' is looked for up to the first '/' after the slashes.
       "https://u:p?x@host/r", "https://u:p#x@host/r"]
# Extra slashes before the real host: git and curl read the "u:p" as credentials. url_has_userinfo says so,
# and the clone is refused (the address check refuses an empty host first).
NO_HOST = ["https:///u:p@example.com/a/b.git", "https:////u:p@example.com/a/b.git",
           "https:///example.com/a/b.git", "https://", "ssh:///a/b.git"]
NO_HOST_USERINFO = NO_HOST[:2]
