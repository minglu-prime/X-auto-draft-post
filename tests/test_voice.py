import json

import httpx
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from xdraft import cli, voice
from xdraft.web.app import create_app


def test_chunking_and_language():
    text = "第一条：今天试了 agent。\n---\nSecond piece, in English, long enough.\n---\nok"
    assert voice.chunk_text(text, 600) == ["第一条：今天试了 agent。", "Second piece, in English, long enough."]
    long = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(6))
    pieces = voice.chunk_text(long, 600)
    assert len(pieces) > 1 and all(len(p) <= 600 for p in pieces)
    assert voice.detect_lang("今天用 Claude Code 重构") == "zh" and voice.detect_lang("refactored today") == "en"


def test_pick_examples_prefers_language_engagement_and_topic(cfg):
    samples = [voice.Sample("agent 写测试比写实现难", "zh", 50), voice.Sample("周末去爬山", "zh", 5),
               voice.Sample("agent 审核才是瓶颈", "zh", 1), voice.Sample("english post about agents", "en", 999)]
    picked = voice.pick_examples(samples, "agent 审核", "zh", 2)
    assert [s.text for s in picked] == ["agent 写测试比写实现难", "agent 审核才是瓶颈"]


def test_sources_load_from_folders(cfg):
    voice.save_archive(cfg, [{"text": "my archived post about agents", "likes": 3, "retweets": 1}])
    voice.save_text(cfg, "mine", "pasted one\n---\npasted two, a bit longer")
    voice.save_text(cfg, "reference", "someone else's paragraph, quite good")
    own, refs = voice.load_own(cfg), voice.load_references(cfg)
    assert {s.text for s in own} == {"my archived post about agents", "pasted one", "pasted two, a bit longer"}
    assert next(s for s in own if "archived" in s.text).likes == 6
    assert [s.text for s in refs] == ["someone else's paragraph, quite good"]
    assert voice.summary(cfg)["own"] == 3


def test_save_url_extracts_article(cfg):
    page = ("<html><head><title>On writing</title></head><body><nav>menu</nav><article>"
            + "<p>Write the specific thing you saw, then stop. Readers can tell.</p>" * 20 + "</article></body></html>")
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    path = voice.save_url(cfg, "https://blog.example/writing", client)
    text = path.read_text(encoding="utf-8")
    assert "Write the specific thing you saw" in text and "menu" not in text
    assert voice.load_references(cfg) and "On writing" not in voice.load_references(cfg)[0].text  # header stripped


def test_style_import_saves_posts_even_with_existing_style(tmp_path, monkeypatch):
    for name in ("config.example.yaml", ".env.example", "style.example.md"):
        (tmp_path / name).write_text((cli.Path(__file__).resolve().parent.parent / name).read_text(encoding="utf-8"),
                                     encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    runner.invoke(cli.app, ["init"])
    (tmp_path / "style.md").write_text("my own hand-written style\n", encoding="utf-8")
    archive = tmp_path / "archive" / "data"
    archive.mkdir(parents=True)
    entries = [{"tweet": {"full_text": "agents still need a human reviewer, learned it the hard way", "favorite_count": "9"}}]
    (archive / "tweets.js").write_text("window.YTD.tweets.part0 = " + json.dumps(entries), encoding="utf-8")
    r = runner.invoke(cli.app, ["style-import", str(tmp_path / "archive"), "--no-llm"])
    assert r.exit_code == 0, r.output
    assert "Saved 1 of your posts" in r.output and "Kept your existing style.md" in r.output
    assert (tmp_path / "style.md").read_text(encoding="utf-8") == "my own hand-written style\n"
    assert json.loads((tmp_path / "voice" / "mine" / "x-archive.json").read_text(encoding="utf-8"))[0]["likes"] == 9


def test_inbox_voice_form(cfg):
    client = TestClient(create_app(cfg, start_scheduler=False, allowed_hosts=("testserver",)))
    assert "文风" in client.get("/inbox").text
    r = client.post("/voice", data={"kind": "mine", "text": "a post I wrote\n---\nanother one I wrote"},
                    headers={"X-Xdraft": "1"})
    assert "现在有 2 篇你自己的文字" in r.text
    r = client.post("/voice", data={"kind": "mine", "text": ""}, headers={"X-Xdraft": "1"})
    assert "请粘贴文字" in r.text
