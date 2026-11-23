from datetime import timedelta

from conftest import NOW
from fakes import FakeLLM
from sqlmodel import Session, select

from xdraft.collectors.base import CollectedItem
from xdraft.db import get_engine, upsert_items
from xdraft.llm import LLMError
from xdraft.models import Draft, DraftStatus, Item, Topic, TopicStatus
from xdraft.pipeline.generate import performance_summary, produce_drafts, suggest_topics
from xdraft.pipeline.schemas import (
    BilingualDraft,
    OptimizedDraft,
    OptimizedVersion,
    PostVersion,
    SuggestionPlan,
    TopicSuggestion,
)
from xdraft.timeutil import utcnow


def _seed(session):
    upsert_items(session, [
        CollectedItem("claude_code", "s1", NOW - timedelta(hours=2), "Refactor parser",
                      "Models: claude-opus-5-5. Refactored 40 files in one session.",
                      meta={"models": {"claude-opus-5-5": 30}, "tools": {"Edit": 12}, "project": "parser",
                            "n_user_prompts": 4, "duration_min": 50}),
        CollectedItem("articles", "a1", NOW - timedelta(hours=5), "Lab X ships Model Y with agent mode",
                      "Model Y is 2x faster.", url="https://news.example/y", meta={"feed": "HN", "points": 400}),
        CollectedItem("articles", "a2", NOW - timedelta(hours=6), "Hands-on with Model Y agents",
                      "first impressions", url="https://blog.example/y", meta={"feed": "36Kr"}),
        CollectedItem("articles", "old", NOW - timedelta(days=20), "Old news", "old"),
    ])
    return {i.source_id: i.id for i in session.exec(select(Item)).all()}


def _suggestion(ids, **kw):
    base = dict(title="Agents in big refactors", angle="It kept the plan across 40 files", why_now="Model Y agent mode",
                my_connection="40-file refactor in Claude Code", format="single", score=80,
                item_ids=ids, trend_terms=["Model Y"])
    return TopicSuggestion(**{**base, **kw})


def test_suggest_topics_uses_trends_and_usage(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        llm = FakeLLM(SuggestionPlan(topics=[
            _suggestion([ids["s1"], ids["a1"], 9999]),
            _suggestion([9999], title="Hallucinated"),
        ]))
        topics = suggest_topics(cfg, llm, s, since)
        assert [t.title for t in topics] == ["Agents in big refactors"]
        t = topics[0]
        assert t.status == TopicStatus.SUGGESTED and t.item_ids == [ids["s1"], ids["a1"]]
        assert t.my_connection and t.trend_terms == ["Model Y"]
        system, user, _ = llm.calls[0]
        assert "<trends>" in user and "Model Y" in user.split("</trends>")[0]   # trend signal detected
        assert "claude-opus-5-5" in user.split("<usage>")[1].split("</usage>")[0]  # usage summary
        assert "Old news" not in user and "data, not instructions" in system
        assert s.exec(select(Draft)).all() == []  # suggestions only, nothing drafted yet


def test_produce_drafts_runs_write_polish_optimize(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = Topic(title="T", angle="A", why="W", my_connection="M", item_ids=[ids["s1"], ids["a1"]],
                      created_at=utcnow())
        s.add(topic)
        s.commit()
        long_en = "x" * 300
        llm = FakeLLM(
            BilingualDraft(zh=PostVersion(tweets=["初稿"]), en=PostVersion(tweets=["first draft"])),
            BilingualDraft(zh=PostVersion(tweets=["润色后"]), en=PostVersion(tweets=[long_en])),
            # optimize round 1 still too long -> a second optimize round runs with the warning
            OptimizedDraft(zh=OptimizedVersion(tweets=["优化后 https://news.example/y"], source_urls=["https://news.example/y"],
                                               alt_hooks=["钩子一", "钩子二"]),
                           en=OptimizedVersion(tweets=[long_en], alt_hooks=["hook a"])),
            OptimizedDraft(zh=OptimizedVersion(tweets=["优化后 https://news.example/y"], source_urls=["https://news.example/y"]),
                           en=OptimizedVersion(tweets=["Model Y kept the plan across 40 files."], alt_hooks=["hook b"], notes="shortened")),
        )
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert [c[2].__name__ for c in llm.calls] == ["BilingualDraft", "BilingualDraft", "OptimizedDraft", "OptimizedDraft"]
        assert "weighted characters" in llm.calls[3][1]  # second optimize got the length warning
        assert zh.stage == en.stage == "optimized"
        assert [r["stage"] for r in en.revisions] == ["written", "polished", "optimized", "optimized"]
        assert en.tweets == ["Model Y kept the plan across 40 files."] and en.warnings == []
        assert zh.alt_hooks == ["钩子一", "钩子二"]  # kept from the round that produced them
        assert topic.status == TopicStatus.DRAFTED


def test_polish_failure_falls_back_to_written(cfg, since):
    class FailingPolish(FakeLLM):
        def parse(self, system, user, schema):
            if "Polish both versions" in system:
                raise LLMError("boom")
            return super().parse(system, user, schema)

    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = Topic(title="T", angle="A", item_ids=[ids["s1"]], created_at=utcnow())
        s.add(topic)
        s.commit()
        llm = FailingPolish(BilingualDraft(zh=PostVersion(tweets=["初稿"]), en=PostVersion(tweets=["draft"])))
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert en.stage == "written" and en.tweets == ["draft"]
        assert any("Polish step failed" in w for w in en.warnings)


def test_no_items_no_llm_call(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        assert suggest_topics(cfg, FakeLLM(), s, since) == []


def test_performance_summary_feeds_suggestions(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        assert "no engagement" in performance_summary(s)
        for text, likes in (("hit post", 120), ("flop post", 1)):
            s.add(Draft(lang="en", tweets=[text], status=DraftStatus.PUBLISHED, tweet_ids=["1"],
                        metrics={"like_count": likes}, created_at=utcnow(), updated_at=utcnow()))
        s.commit()
        summary = performance_summary(s)
        assert summary.index("hit post") < summary.index("flop post") and "likes 120" in summary
        _seed(s)
        llm = FakeLLM(SuggestionPlan(topics=[]))
        suggest_topics(cfg, llm, s, since)
        assert "likes 120" in llm.calls[0][0]
