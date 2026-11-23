from datetime import timedelta

from conftest import NOW

from xdraft.models import Item
from xdraft.pipeline.trends import format_trends, select_material, trend_terms, usage_summary


def _item(i, source, title, meta=None):
    return Item(id=i, source=source, source_id=str(i), ts=NOW - timedelta(hours=i), title=title, body="",
                meta=meta or {}, content_hash="", collected_at=NOW, updated_at=NOW)


ITEMS = [
    _item(1, "articles", "Anthropic releases Claude Code 3 with background agents", {"feed": "HN", "points": 900}),
    _item(2, "articles", "Hands-on: Claude Code background agents are wild", {"feed": "Simon Willison"}),
    _item(3, "articles", "OpenAI 发布 GPT-6 预览版", {"feed": "36Kr"}),
    _item(4, "articles", "Show HN: GPT-6 wrapper for spreadsheets", {"feed": "HN", "points": 120}),
    _item(5, "articles", "How I bake sourdough bread", {"feed": "HN", "points": 50}),
    _item(6, "claude_code", "Refactor parser", {"models": {"claude-opus-5-5": 12}, "tools": {"Edit": 5},
                                                "project": "parser", "n_user_prompts": 3, "duration_min": 40}),
    _item(7, "notes", "Background agents need better review UX"),
    _item(8, "git", "repo: fix", {"repo": "xdraft", "commits": [{"sha": "a"}, {"sha": "b"}]}),
]


def test_trend_terms_rank_cross_source_names():
    terms = trend_terms(ITEMS)
    names = [t.term for t in terms]
    assert names[0] == "Claude Code"          # 2 sources + heat beats the rest
    assert "GPT-6" in names                    # English + Chinese headline
    assert "Claude" not in names               # folded into "Claude Code"
    assert not any(n in names for n in ("How", "Show", "HN", "Hands-on"))
    assert "sourdough" not in " ".join(names).lower()
    assert "2 sources" in format_trends(terms)


def test_usage_summary_and_material_balance():
    usage = usage_summary(ITEMS)
    assert "1 sessions" in usage and "claude-opus-5-5" in usage and "xdraft (2)" in usage
    assert "Background agents need better review UX" in usage
    material = select_material(ITEMS, trend_terms(ITEMS), personal_max=5, total_max=5)
    assert {it.id for it in material[:3]} == {6, 7, 8}     # own items always included
    assert material[3].id == 1                              # then the hottest trend article


def test_title_openers_vs_leading_names():
    from xdraft.pipeline.trends import _candidates

    assert "Claude Code" in _candidates("Claude Code background agents: first impressions")
    assert "DeepSeek V4" in _candidates("DeepSeek V4 发布，推理成本再降一半")
    assert _candidates("Hands-on: Claude Code agents") == {"Claude", "Code", "Claude Code"}
    assert _candidates("How I bake sourdough bread") == set()
    assert _candidates("Show HN: GPT-6 wrapper") == {"GPT-6"}
