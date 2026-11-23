"""Deterministic checks on drafts: X length rules, links, numbers, duplicates, leaks."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from xdraft.redact import redact

MAX_WEIGHTED = 280
URL_WEIGHT = 23
_URL_RE = re.compile(r"https?://[^\s<>\"'）)]+")
_NUM_RE = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?:\s?%|x|×|k|K|M|B)?")
# twitter-text v3: these code point ranges weigh 1, everything else (CJK, emoji, ...) weighs 2.
_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def _char_weight(ch: str) -> int:
    cp = ord(ch)
    return 1 if any(lo <= cp <= hi for lo, hi in _LIGHT_RANGES) else 2


def weighted_length(text: str) -> int:
    """Length as X counts it (URLs = 23, CJK and emoji = 2)."""
    total, pos = 0, 0
    for m in _URL_RE.finditer(text):
        total += sum(_char_weight(c) for c in text[pos : m.start()]) + URL_WEIGHT
        pos = m.end()
    return total + sum(_char_weight(c) for c in text[pos:])


def _norm_num(s: str) -> str:
    return re.sub(r"[\s,%x×kKMB]", "", s)


def check_post(
    tweets: list[str],
    source_urls: Iterable[str],
    material_text: str,
    material_urls: Iterable[str],
    recent_texts: Iterable[str],
    thread_max: int = 5,
    duplicate_threshold: float = 0.8,
) -> list[str]:
    warnings: list[str] = []
    if not tweets or not any(t.strip() for t in tweets):
        return ["Draft is empty"]
    if len(tweets) > thread_max:
        warnings.append(f"Thread has {len(tweets)} tweets (max {thread_max})")
    for i, t in enumerate(tweets, 1):
        n = weighted_length(t)
        if n > MAX_WEIGHTED:
            warnings.append(f"Tweet {i} is {n}/{MAX_WEIGHTED} weighted characters")
        if not t.strip():
            warnings.append(f"Tweet {i} is empty")

    text = "\n".join(tweets)
    known_urls = {u.rstrip("/") for u in [*material_urls, *source_urls] if u}
    for url in _URL_RE.findall(text):
        if url.rstrip("/.,") not in known_urls and url.rstrip("/") not in known_urls:
            warnings.append(f"Link not found in the source material: {url}")

    material_nums = {_norm_num(n) for n in _NUM_RE.findall(material_text)}
    text_wo_urls = _URL_RE.sub(" ", text)
    for num in dict.fromkeys(_NUM_RE.findall(text_wo_urls)):
        key = _norm_num(num)
        if len(key.replace(".", "")) >= 2 and key not in material_nums:
            warnings.append(f"Number “{num.strip()}” does not appear in the sources — verify it")

    best = max((difflib.SequenceMatcher(None, text, r).ratio() for r in recent_texts if r), default=0.0)
    if best >= duplicate_threshold:
        warnings.append(f"Very similar to a recent post ({best:.0%} match)")

    if redact(text) != text:
        warnings.append("Possible secret, email or personal path in the text")
    return warnings
