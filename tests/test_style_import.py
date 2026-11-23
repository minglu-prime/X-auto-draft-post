import json

from fakes import FakeLLM

from xdraft.pipeline.schemas import StyleProfile
from xdraft.style_import import build_style, load_archive_tweets


def test_archive_parsing_and_style(tmp_path):
    entries = [
        {"tweet": {"full_text": "Tried the new model on a big refactor, it held up well https://t.co/abc", "favorite_count": "50", "retweet_count": "4"}},
        {"tweet": {"full_text": "RT @someone: not mine at all, skip this one", "favorite_count": "0"}},
        {"tweet": {"full_text": "@friend replying to you about agents", "in_reply_to_status_id_str": "1"}},
        {"tweet": {"full_text": "short"}},
        {"tweet": {"full_text": "用 agent 写测试，比写实现更难", "favorite_count": "3", "retweet_count": "0"}},
    ]
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "tweets.js").write_text("window.YTD.tweets.part0 = " + json.dumps(entries), encoding="utf-8")
    tweets = load_archive_tweets(tmp_path)
    assert [t["text"] for t in tweets] == ["Tried the new model on a big refactor, it held up well", "用 agent 写测试，比写实现更难"]
    llm = FakeLLM(StyleProfile(style_markdown="- terse"))
    assert build_style(llm, tweets) == "- terse\n"
    assert "[likes=50 reposts=4]" in llm.calls[0][1]
