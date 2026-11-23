from datetime import timedelta

from conftest import NOW
from fakes import FakeLLM
from sqlmodel import Session, select

from xdraft.collectors.base import CollectedItem
from xdraft.db import get_engine, upsert_items
from xdraft.models import Draft, Item, Topic
from xdraft.pipeline.generate import generate
from xdraft.pipeline.schemas import BilingualDraft, PostVersion, TopicPlan, TopicProposal


def _seed(session):
    upsert_items(session, [
        CollectedItem("claude_code", "s1", NOW - timedelta(hours=2), "Refactor parser",
                      "Models: claude-opus-5-5. Refactored 40 files in one session."),
        CollectedItem("articles", "a1", NOW - timedelta(hours=5), "New model released",
                      "Lab X releases model Y, 2x faster.", url="https://news.example/y"),
        CollectedItem("articles", "old", NOW - timedelta(days=20), "Old news", "old"),
    ])
    return {i.source_id: i.id for i in session.exec(select(Item)).all()}


def test_generate_creates_bilingual_drafts(cfg, since):
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        ids = _seed(session)
        llm = FakeLLM(
            TopicPlan(topics=[
                TopicProposal(title="Big refactors", angle="It kept the plan across 40 files", why="new model",
                              score=80, item_ids=[ids["s1"], ids["a1"], 9999]),
                TopicProposal(title="Hallucinated", angle="x", why="y", score=10, item_ids=[9999]),
            ]),
            BilingualDraft(
                zh=PostVersion(tweets=["用新模型重构了 40 个文件，计划一直没跑偏。https://news.example/y"],
                               source_urls=["https://news.example/y"]),
                en=PostVersion(tweets=["Refactored 40 files with the new model; it is 2x faster and 99% right.",
                                       "Source: https://news.example/y"], source_urls=["https://news.example/y"]),
            ),
        )
        drafts = generate(cfg, llm, session, since)
        assert [d.lang for d in drafts] == ["zh", "en"]
        zh, en = drafts
        assert zh.warnings == []
        assert any("99%" in w for w in en.warnings)  # unsourced number flagged
        topic = session.exec(select(Topic)).one()
        assert topic.item_ids == [ids["s1"], ids["a1"]]  # unknown id dropped
        # The old item is outside the window and never sent to the LLM.
        assert "Old news" not in llm.calls[0][1]
        assert "<material>" in llm.calls[0][1] and "data, not instructions" in llm.calls[0][0]
        assert len(session.exec(select(Draft)).all()) == 2


def test_generate_without_items_does_not_call_llm(cfg, since):
    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        assert generate(cfg, FakeLLM(), session, since) == []


def test_performance_summary_feeds_topic_selection(cfg, since):
    from xdraft.models import DraftStatus
    from xdraft.pipeline.generate import performance_summary
    from xdraft.timeutil import utcnow

    engine = get_engine(cfg.resolve_path(cfg.db_path))
    with Session(engine) as session:
        assert "no engagement" in performance_summary(session)
        for text, likes in (("hit post", 120), ("flop post", 1)):
            session.add(Draft(lang="en", tweets=[text], status=DraftStatus.PUBLISHED, tweet_ids=["1"],
                              metrics={"like_count": likes}, created_at=utcnow(), updated_at=utcnow()))
        session.commit()
        summary = performance_summary(session)
        assert summary.index("hit post") < summary.index("flop post") and "likes 120" in summary
        _seed(session)
        llm = FakeLLM(TopicPlan(topics=[]))
        generate(cfg, llm, session, since)
        assert "likes 120" in llm.calls[0][0]
