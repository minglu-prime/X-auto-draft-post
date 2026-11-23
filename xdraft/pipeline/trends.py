"""Deterministic trend signals and a summary of the author's own AI usage.

Trends: terms (product/model/company names, version-like tokens and their bigrams) that show
up across several articles and feeds, weighted by HN points / GitHub stars. Cheap, explainable,
and gives the LLM evidence instead of letting it guess what is "hot".
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from xdraft.models import Item

PERSONAL_SOURCES = ("claude_code", "notes", "git")

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*")
STOPWORDS = {
    w.lower() for w in """
    a an and are as at be but by can do does for from has have how i if in into is it its just
    me my new no not of on or our out show ask tell hn launch launches launched the their this
    that these those to up us we what when where which who why will with you your vs via
    about after all also any best better big first get got here more most now one only over
    some than them then there they two use used using way year years make makes made want
    week day today yesterday open free fast faster real still new latest update updates
    introducing announcing release released releases version guide build building built
    hands-on inside meet review reviewing thoughts notes lessons
    my-own own let lets don t s re ve ll d
    """.split()
}


@dataclass
class TrendTerm:
    term: str
    items: int = 0
    feeds: set = field(default_factory=set)
    points: int = 0
    item_ids: list = field(default_factory=list)
    examples: list = field(default_factory=list)

    @property
    def score(self) -> float:
        return 3 * len(self.feeds) + self.items + 0.6 * math.log1p(self.points)


def _name_like(tok: str) -> bool:
    return (any(c.isdigit() for c in tok) or tok[0].isupper() or tok.isupper()) and tok.lower() not in STOPWORDS


def _strong_name(tok: str) -> bool:
    """Clearly a name even at the start of a title: inner capitals or digits (DeepSeek, GPT-5, iOS)."""
    return any(c.isdigit() for c in tok) or any(c.isupper() for c in tok[1:])


def _candidates(title: str) -> set[str]:
    """Name-like tokens (capitalised, all-caps or containing digits) and adjacent pairs of them.

    A title's first word is capitalised anyway, so it only counts when it is clearly a name or is
    directly followed by another name ("Claude Code ..."), not for openers like "Hands-on:".
    """
    matches = list(_TOKEN_RE.finditer(title))
    keep: list[str | None] = []
    for i, m in enumerate(matches):
        tok = m.group()
        ok = len(tok) > 1 and _name_like(tok)
        if ok and i == 0 and not _strong_name(tok):
            nxt = matches[1] if len(matches) > 1 else None
            ok = bool(nxt and title[m.end():nxt.start()].strip() == "" and _name_like(nxt.group()))
        keep.append(tok if ok else None)
    terms = {t for t in keep if t}
    for (a, ma), (b, mb) in zip(zip(keep, matches), zip(keep[1:], matches[1:])):
        if a and b and title[ma.end():mb.start()].strip() == "":
            terms.add(f"{a} {b}")
    return terms


def trend_terms(items: list[Item], limit: int = 15) -> list[TrendTerm]:
    stats: dict[str, TrendTerm] = {}
    for it in items:
        if it.source in PERSONAL_SOURCES:
            continue
        meta = it.meta or {}
        feed = meta.get("feed") or it.source
        points = int(meta.get("points") or meta.get("stars") or 0)
        for term in _candidates(it.title):
            key = term.lower()
            t = stats.setdefault(key, TrendTerm(term=term))
            t.items += 1
            t.feeds.add(feed)
            t.points += points
            t.item_ids.append(it.id)
            if len(t.examples) < 3:
                t.examples.append(it.title[:120])
    ranked = [t for t in stats.values() if t.items >= 2 or t.points >= 300]
    phrases = [t for t in ranked if " " in t.term]

    def covered(t: TrendTerm) -> bool:
        # "Claude" is redundant when "Claude Code" covers the same items.
        return " " not in t.term and any(
            t.term in p.term.split() and set(t.item_ids) <= set(p.item_ids) for p in phrases
        )

    ranked = [t for t in ranked if not covered(t)]
    ranked.sort(key=lambda t: (-t.score, -t.term.count(" "), t.term.lower()))
    return ranked[:limit]


def format_trends(terms: list[TrendTerm]) -> str:
    if not terms:
        return "(no clear trend signal in this window)"
    lines = []
    for t in terms:
        lines.append(
            f"- {t.term}: {t.items} items across {len(t.feeds)} sources"
            + (f", heat {t.points}" if t.points else "")
            + " — e.g. " + " | ".join(t.examples[:2])
        )
    return "\n".join(lines)


def usage_summary(items: list[Item]) -> str:
    """What the author actually did with AI in this window (already redacted at collection)."""
    models, tools, projects = Counter(), Counter(), Counter()
    sessions = prompts = minutes = 0
    repos: dict[str, int] = defaultdict(int)
    notes = []
    for it in items:
        meta = it.meta or {}
        if it.source == "claude_code":
            sessions += 1
            prompts += int(meta.get("n_user_prompts", 0))
            minutes += int(meta.get("duration_min", 0))
            models.update(meta.get("models") or {})
            tools.update(meta.get("tools") or {})
            projects[meta.get("project") or "?"] += 1
        elif it.source == "git":
            repos[meta.get("repo") or "?"] += len(meta.get("commits") or [])
        elif it.source == "notes":
            notes.append(it.title)
    lines = []
    if sessions:
        lines.append(f"- Claude Code: {sessions} sessions, {prompts} prompts, ~{minutes} min, "
                     f"projects: {', '.join(p for p, _ in projects.most_common(5))}")
        lines.append("- Models used: " + ", ".join(f"{m} ({n} replies)" for m, n in models.most_common(5)))
        lines.append("- Most used tools: " + ", ".join(f"{t} ×{n}" for t, n in tools.most_common(6)))
    if repos:
        lines.append("- Commits: " + ", ".join(f"{r} ({n})" for r, n in sorted(repos.items(), key=lambda x: -x[1])[:5]))
    if notes:
        lines.append("- Own notes / takes: " + " | ".join(notes[:8]))
    return "\n".join(lines) or "(no personal usage recorded in this window)"


def select_material(items: list[Item], terms: list[TrendTerm], personal_max: int, total_max: int) -> list[Item]:
    """Keep the author's own items, then the hottest / most trend-related articles."""
    personal = [it for it in items if it.source in PERSONAL_SOURCES][:personal_max]
    trend_ids = Counter(i for t in terms for i in t.item_ids)
    others = [it for it in items if it.source not in PERSONAL_SOURCES]
    others.sort(key=lambda it: (trend_ids.get(it.id, 0), int((it.meta or {}).get("points") or 0)), reverse=True)
    return personal + others[: max(0, total_max - len(personal))]
