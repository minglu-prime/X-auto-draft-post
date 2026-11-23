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
                my_connection="40-file refactor in Claude Code", format="thread", score=80,
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


def _topic(s, ids, **kw):
    topic = Topic(title="T", angle="A", why="W", my_connection="M", item_ids=[ids["s1"], ids["a1"]],
                  created_at=utcnow(), **kw)
    s.add(topic)
    s.commit()
    return topic


def _od(zh, en, zh_hooks=(), en_hooks=(), urls=()):
    return OptimizedDraft(zh=OptimizedVersion(tweets=zh, source_urls=list(urls), alt_hooks=list(zh_hooks)),
                          en=OptimizedVersion(tweets=en, source_urls=list(urls), alt_hooks=list(en_hooks)))


def test_write_polish_then_fix_only_when_checks_fail(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = _topic(s, ids, user_take="Plans matter more than models")
        long_en = "x" * 300
        llm = FakeLLM(
            BilingualDraft(zh=PostVersion(tweets=["初稿一", "初稿二", "初稿三"]), en=PostVersion(tweets=["one", "two", "three"])),
            # polish leaves an over-length tweet and an AI tell -> fix pass runs, twice
            _od(["润色一", "真正的瓶颈不是 agent，而是审核。", "润色三"], ["p1", long_en, "p3"], ["钩子一", "钩子二"], ["hook a"]),
            _od(["润色一", "瓶颈在审核。", "润色三"], ["p1", long_en, "p3"]),
            _od(["润色一", "瓶颈在审核。", "润色三"], ["p1", "Model Y kept the plan across 40 files.", "p3"], en_hooks=["hook b"]),
        )
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert [c[2].__name__ for c in llm.calls] == ["BilingualDraft", "OptimizedDraft", "OptimizedDraft", "OptimizedDraft"]
        write_sys, write_user, _ = llm.calls[0]
        assert "3-8 tweets" in write_sys and "Never use these AI tells" in write_sys
        assert "Plans matter more than models" in write_user           # the author's take is the backbone
        assert llm.temperatures[0] == cfg.llm.temperature_write and llm.temperatures[1] == cfg.llm.temperature_edit
        fix_user = llm.calls[2][1]
        assert 'AI-ish: "不是 agent，而是"' in fix_user and "weighted characters" in fix_user
        assert zh.stage == en.stage == "optimized"
        assert [r["stage"] for r in en.revisions] == ["written", "polished", "optimized", "optimized"]
        assert en.tweets[1] == "Model Y kept the plan across 40 files." and en.warnings == []
        assert zh.alt_hooks == ["钩子一", "钩子二"] and en.alt_hooks == ["hook b"]
        assert topic.status == TopicStatus.DRAFTED


def test_clean_polish_skips_fix_pass(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = _topic(s, ids, format="quick")
        llm = FakeLLM(
            BilingualDraft(zh=PostVersion(tweets=["初稿"]), en=PostVersion(tweets=["draft"])),
            _od(["今天把 40 个文件的重构交给了它，计划一直没跑偏。"], ["Handed it a 40-file refactor today. The plan held."]),
        )
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert len(llm.calls) == 2 and en.stage == "optimized" and en.warnings == []
        assert "one tweet" in llm.calls[0][0]


def test_voice_examples_reach_prompts(cfg, since):
    from xdraft import voice

    voice.save_archive(cfg, [{"text": "今天第三次被 agent 的自信坑了，它说测试过了，其实根本没跑。", "likes": 40, "retweets": 2},
                             {"text": "shipping small things daily beats one big launch", "likes": 3, "retweets": 0}])
    voice.save_text(cfg, "reference", "A writer I like.\n\nShort paragraphs, concrete numbers, no fluff at all.")
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = _topic(s, ids)
        llm = FakeLLM(BilingualDraft(zh=PostVersion(tweets=["a"]), en=PostVersion(tweets=["b"])),
                      _od(["今天试了一下，挺好。"], ["tried it today, fine."]))
        produce_drafts(cfg, llm, s, topic)
        write_sys, polish_sys = llm.calls[0][0], llm.calls[1][0]
        for system in (write_sys, polish_sys):
            assert "<my_writing>" in system and "被 agent 的自信坑了" in system
            assert "<admired_writers>" in system and "concrete numbers" in system


def test_polish_failure_falls_back_to_written(cfg, since):
    class FailingPolish(FakeLLM):
        def parse(self, system, user, schema, temperature=None):
            if "strict editor" in system:
                raise LLMError("boom")
            return super().parse(system, user, schema, temperature)

    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = Topic(title="T", angle="A", item_ids=[ids["s1"]], created_at=utcnow())
        s.add(topic)
        s.commit()
        llm = FailingPolish(BilingualDraft(zh=PostVersion(tweets=["初稿"]), en=PostVersion(tweets=["draft"])))
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert en.stage == "written" and en.tweets == ["draft"]
        assert any("Polish step failed" in w for w in en.warnings)


def test_fulltext_fetched_for_picked_topic_only(cfg, since):
    import httpx

    cfg.articles.fetch_fulltext = True
    page = "<html><head><title>Y</title></head><body><article>" + "<p>Model Y kept a 40-step plan in our test. </p>" * 30 + "</article></body></html>"
    fetched = []

    def handler(request):
        fetched.append(str(request.url))
        return httpx.Response(200, text=page, headers={"content-type": "text/html"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = _topic(s, ids)
        llm = FakeLLM(BilingualDraft(zh=PostVersion(tweets=["a"]), en=PostVersion(tweets=["b"])),
                      _od(["今天试了一下。"], ["tried it today."]))
        produce_drafts(cfg, llm, s, topic, http_client=client)
        assert fetched == ["https://news.example/y"]  # only the topic's article, not a2 / old
        assert "Full text: Model Y kept a 40-step plan" in llm.calls[0][1]
        item = s.get(Item, ids["a1"])
        assert "40-step plan" in item.meta["fulltext"]
        produce_drafts(cfg, FakeLLM(BilingualDraft(zh=PostVersion(tweets=["a"]), en=PostVersion(tweets=["b"])),
                                    _od(["再试一次。"], ["again."])), s, _topic(s, ids))
        assert len(fetched) == 1  # cached on the item


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


def test_numbers_from_own_experience_are_not_flagged_and_soft_warnings_fixed_once(cfg, since):
    with Session(get_engine(cfg.resolve_path(cfg.db_path))) as s:
        ids = _seed(s)
        topic = _topic(s, ids, user_take="Reviewing took me 95 minutes")
        llm = FakeLLM(
            BilingualDraft(zh=PostVersion(tweets=["a"]), en=PostVersion(tweets=["b"])),
            _od(["审核花了我 95 分钟。"], ["Review took 95 minutes, 3 bugs, 77% of the time."]),  # 77% is unsourced
            _od(["审核花了我 95 分钟。"], ["Review took 95 minutes, 3 bugs, 77% of the time."]),  # fix didn't remove it
        )
        zh, en = produce_drafts(cfg, llm, s, topic)
        assert len(llm.calls) == 3          # one fix round for a soft warning, not three
        assert zh.warnings == []            # 95 comes from the author's take
        assert any("77%" in w for w in en.warnings)
