from datetime import timedelta
from email.utils import format_datetime

import httpx
from conftest import NOW

from xdraft.collectors import articles
from xdraft.config import FeedConfig


def _rss(entries):
    items = "".join(
        f"<item><title>{t}</title><link>{l}</link><guid>{l}</guid>"
        f"<pubDate>{format_datetime(d)}</pubDate><description>&lt;p&gt;{s}&lt;/p&gt;</description></item>"
        for t, l, d, s in entries
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{items}</channel></rss>'


def _client(routes):
    def handler(request: httpx.Request):
        key = f"{request.url.scheme}://{request.url.host}{request.url.path}"
        if key not in routes:
            return httpx.Response(404)
        body = routes[key]
        if isinstance(body, dict):
            return httpx.Response(200, json=body)
        return httpx.Response(200, text=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_feeds_and_hackernews(cfg, since):
    blog = _rss([
        ("New LLM released", "https://b.example/1", NOW - timedelta(hours=5), "A <b>reasoning</b> model"),
        ("Gardening tips", "https://b.example/2", NOW - timedelta(hours=5), "Tomatoes"),
        ("Old Claude post", "https://b.example/3", NOW - timedelta(days=9), "old"),
    ])
    ai_only = _rss([("Anything goes", "https://ai.example/1", NOW - timedelta(hours=1), "x")])
    hn = {"hits": [
        {"objectID": "1", "title": "Claude does X", "url": "https://x.example", "points": 300,
         "num_comments": 80, "created_at_i": int((NOW - timedelta(hours=3)).timestamp())},
        {"objectID": "2", "title": "Rust 2.0 released", "url": None, "points": 500,
         "num_comments": 10, "created_at_i": int((NOW - timedelta(hours=3)).timestamp())},
    ]}
    client = _client({
        "https://b.example/feed": blog,
        "https://ai.example/feed": ai_only,
        "https://hn.algolia.com/api/v1/search_by_date": hn,
    })
    cfg.articles.feeds = [
        FeedConfig(name="Blog", url="https://b.example/feed"),
        FeedConfig(name="AI only", url="https://ai.example/feed", filter=False),
        FeedConfig(name="Broken", url="https://broken.example/feed"),
    ]
    cfg.articles.hackernews.enabled = True
    res = articles.collect(cfg, since, client=client)

    titles = {i.title for i in res.items}
    assert titles == {"New LLM released", "Anything goes", "Claude does X"}
    assert len(res.errors) == 1 and "Broken" in res.errors[0]
    llm = next(i for i in res.items if i.title == "New LLM released")
    assert llm.body == "A reasoning model" and llm.meta["feed"] == "Blog"
    hn_item = next(i for i in res.items if i.source_id == "hn:1")
    assert hn_item.meta["points"] == 300


def test_keyword_matcher_word_boundaries():
    m = articles.keyword_matcher(["AI", "LLM"])
    assert m("New AI chips") and m("Two LLMs compared")
    assert not m("Airbnb raises prices") and not m("said hello")
