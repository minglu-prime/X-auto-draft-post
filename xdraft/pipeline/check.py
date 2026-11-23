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


_AI_EN = [
    r"\b(?:isn't|is not|wasn't|aren't|it's not)\b[^.!?\n]{1,60}?(?:—|–|--|;|,)\s*(?:it's|it is|they're|that's|it was)\b",
    r"\bnot just\b[^.!?\n]{1,60}?\bbut\b",
    r"\bhere'?s the (?:thing|kicker|catch|truth)\b",
    r"\bgame[- ]?changer\b", r"\bdelv(?:e|es|ing)\b", r"\bunlock(?:s|ed|ing)?\b", r"\bin today'?s\b",
    r"\blet'?s dive\b", r"\bdive (?:in|into|deep)\b", r"\brevolutioni[sz]\w*", r"\bseamless(?:ly)?\b",
    r"\bleverag(?:e|es|ed|ing)\b", r"\ba testament to\b", r"\bever[- ]evolving\b", r"\bthe \w+ landscape\b",
    r"🚀", r"🧵", r"(?m)^\s*\d+/\d*\s",
]
_AI_EN_END = r"(?:what do you think|thoughts|agree|what about you)\s*\?\s*$"
_AI_ZH = [
    r"不是[^。！？\n]{1,30}?[，,、]?\s*而是", r"值得注意的是", r"总的来说", r"总而言之", r"让我们", r"赋能", r"颠覆",
    r"重磅", r"干货", r"一文读懂", r"炸裂", r"划重点", r"首先[\s\S]{0,200}?其次", r"！！",
]
_AI_ZH_END = r"(?:你怎么看|你们怎么看|你们觉得呢|你觉得呢|欢迎评论|欢迎留言|评论区聊聊)[？?！!。]?\s*$"


def ai_patterns(text: str, lang: str | None = None) -> list[str]:
    """Phrases that make a post read machine-written. Returns warnings like `AI-ish: "…"`."""
    hits: list[str] = []
    check_en = lang in (None, "en")
    check_zh = lang in (None, "zh")
    patterns = (_AI_EN if check_en else []) + (_AI_ZH if check_zh else [])
    flags = re.I
    for pat in patterns:
        for m in re.finditer(pat, text, flags):
            hits.append(m.group().strip())
    ends = ([_AI_EN_END] if check_en else []) + ([_AI_ZH_END] if check_zh else [])
    for pat in ends:
        m = re.search(pat, text.strip(), re.I)
        if m:
            hits.append(m.group().strip() + " (question ending)")
    if check_en and text.count("—") > 1:
        hits.append(f"{text.count('—')} em dashes")
    out = []
    for h in dict.fromkeys(hits):
        snippet = " ".join(h.split())
        out.append(f'AI-ish: "{snippet[:40]}{"…" if len(snippet) > 40 else ""}"')
    return out


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
    lang: str | None = None,
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
    warnings += ai_patterns(text, lang)
    return warnings
