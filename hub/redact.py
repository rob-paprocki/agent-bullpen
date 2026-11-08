"""Masking secrets in text the hub shows or keeps: the approval lines in a job's log, and an
approval's stored input once its job has ended.

The patterns are copies of the runner's (runner/runner.py: SECRET_VALUE_RES, AUTH_WORD_RE,
URL_USERINFO_RE, NAMED_SECRET_RE), so the hub hides what the runner hides. tests/check_repo.py
fails when the two copies differ: change both together.

Standard library only; imports nothing from app.py and nothing from the `mcp` package.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Well-known key and token formats.
SECRET_VALUE_RES = [
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}"),                          # Anthropic, OpenAI and similar API keys
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),                         # Google API keys
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),  # GitHub tokens
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                   # Slack tokens
    # Agent Bullpen machine and job tokens: "bp_" or "bpj_" and secrets.token_urlsafe(32), which is always
    # 43 URL-safe characters. Only that exact shape, so names like bp_admin_routes are left alone.
    re.compile(r"\bbpj?_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    # JWTs. Tried once per run of key characters (the run must end in ".xxxxxxxx."), so a long run can't
    # make it backtrack; a word joined to the token by a hyphen is masked with it.
    re.compile(r"(?<![A-Za-z0-9_-])(?=[A-Za-z0-9_-]*\.[A-Za-z0-9_-]{8,}\.)[A-Za-z0-9_-]*?\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*"),
]
# "Bearer <value>", "token=<value>", ...
AUTH_WORD_RE = re.compile(r"(?i)\b(bearer|basic|token)(\s+|=)[A-Za-z0-9._~+/=-]{8,}")
# Passwords inside URLs: https://user:password@host
# This pattern and the next start only where a run of name characters starts, and check the end of
# the run once before looking inside it, so they take time in proportion to the text: an agent
# chooses some of what is masked (an approval's input), and patterns that backtrack took hours on
# 20,000 characters. The parts kept (the scheme, the setting's name) are kept whole, as before.
URL_USERINFO_RE = re.compile(r"(?i)(?<![a-z0-9+.-])(?=[a-z0-9+.-]*://[^/@\s]+@)([a-z0-9+.-]*?\b[a-z][a-z0-9+.-]*://)[^/@\s]+@")
# "X-Api-Key: value", "api_key=value" (also in URLs), "password: value", ...
NAMED_SECRET_RE = re.compile(r"(?i)(?<![\w-])(?=[\w-]+\s*[:=]\s*[^\s&,;\"']{6})([\w-]*?(?:token|key|secret|passw|credential)[\w-]*)(\s*[:=]\s*)([^\s&,;\"']{6,})")

MASK = "***"
# JSON keys whose string values are secrets as a whole (hub only: the runner masks text, not JSON).
SECRET_KEY_RE = re.compile(r"(?i)(token|key|secret|passw|credential)")


def redact(text: str, secrets_: list[str] | tuple = ()) -> str:
    """`text` with secrets replaced by ***: the exact values in `secrets_` first, then passwords
    in URLs, values after Bearer/Basic/token, values of key-, token- and password-named settings,
    and well-known key formats. The same steps, in the same order, as the runner's redact()."""
    for secret in sorted(secrets_, key=len, reverse=True):
        text = text.replace(secret, MASK)
    text = URL_USERINFO_RE.sub(r"\1***@", text)
    text = AUTH_WORD_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    text = NAMED_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + MASK, text)
    for rx in SECRET_VALUE_RES:
        text = rx.sub(MASK, text)
    return text


def redact_value(value: Any, secret: bool = False) -> Any:
    """A JSON value with every string in it (keys too) passed through redact(). A string under a
    key named like a secret ("api_key", "password", ...) is replaced as a whole, since nothing in
    the string itself says it is one."""
    if isinstance(value, str):
        return MASK if secret and len(value) >= 6 else redact(value)
    if isinstance(value, list):
        return [redact_value(v, secret) for v in value]
    if isinstance(value, dict):
        return {redact(str(k)): redact_value(v, secret or bool(SECRET_KEY_RE.search(str(k))))
                for k, v in value.items()}
    return value


def redact_json(text: str) -> str:
    """JSON text with its strings masked one by one, so the result is still JSON (masking the
    text as a whole could cut an escape such as \\" in half). Text that isn't JSON is masked as a
    whole."""
    try:
        value = json.loads(text)
    except ValueError:
        return redact(text)
    return json.dumps(redact_value(value), ensure_ascii=False, sort_keys=True)
