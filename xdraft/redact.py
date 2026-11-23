"""Local redaction of secrets and personal paths before anything leaves the machine."""

from __future__ import annotations

import re
from collections.abc import Iterable

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
     "[REDACTED_PRIVATE_KEY]"),
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED_KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{35}"), "[REDACTED_KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-~+/]{16,}=*"), "Bearer [REDACTED]"),
    # KEY=value / "token": "value" style assignments.
    (re.compile(
        r"(?i)\b([A-Za-z0-9_\-]*(?:api[_\-]?key|secret|token|passw(?:or)?d)[A-Za-z0-9_\-]*)"
        r"(\"?\s*[:=]\s*)([\"']?)([^\s\"']{6,})\3"),
     r"\1\2\3[REDACTED]\3"),
    # Credentials embedded in URLs.
    (re.compile(r"(://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    # Home directories reveal usernames.
    (re.compile(r"/(?:home|Users)/[^/\s\"']+"), "~"),
    (re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s\"']+"), "~"),
]


def redact(text: str, extra_patterns: Iterable[str] = ()) -> str:
    if not text:
        return text
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    for extra in extra_patterns:
        text = re.sub(extra, "[REDACTED]", text)
    return text
