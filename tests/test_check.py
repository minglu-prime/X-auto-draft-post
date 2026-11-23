from xdraft.pipeline.check import check_post, weighted_length


def test_weighted_length():
    assert weighted_length("hello") == 5
    assert weighted_length("你好") == 4
    assert weighted_length("see https://example.com/a/very/long/path/that/is/long") == 4 + 23
    assert weighted_length("😀") == 2  # one code point outside the light ranges


def test_check_post_flags_problems():
    long_zh = "字" * 141
    w = check_post(
        tweets=[long_zh, "It is 3x faster, 42% cheaper https://evil.example/x"],
        source_urls=[],
        material_text="The model is 3x faster on our benchmark.",
        material_urls=["https://good.example/a"],
        recent_texts=[],
    )
    joined = "\n".join(w)
    assert "Tweet 1 is 282/280" in joined
    assert "evil.example" in joined
    assert "42%" in joined and "3x" not in joined


def test_duplicates_and_leaks():
    text = "Claude Code refactored my parser in one go"
    assert any("Very similar" in x for x in check_post([text + "!"], [], "", [], [text]))
    assert any("secret" in x for x in check_post(["mail me@example.com"], [], "", [], []))


def test_clean_post_has_no_warnings():
    w = check_post(["Tried the new model on a 40-file refactor. It kept the plan. https://good.example/a"],
                   ["https://good.example/a"], "40 files", [], ["something unrelated"])
    assert w == []


def test_ai_patterns_catch_tells_and_spare_normal_text():
    from xdraft.pipeline.check import ai_patterns

    bad_en = ["The bottleneck isn't the agent — it's the review.", "Not just faster, but cheaper.",
              "Here's the thing: nobody reads docs.", "A real game-changer.", "Let's dive in.",
              "We delve into agents.", "This unlocks new workflows.", "In today's fast world.",
              "Shipped it. What do you think?", "agents 🚀", "1/ a thread about agents"]
    for t in bad_en:
        assert ai_patterns(t, "en"), t
    bad_zh = ["真正的瓶颈不是模型，而是审核。", "值得注意的是，它很快。", "总的来说还不错。", "让我们一起看看。",
              "AI 赋能开发者。", "重磅发布", "干货分享", "首先装依赖，其次跑测试", "太强了！！", "你们觉得呢？"]
    for t in bad_zh:
        assert ai_patterns(t, "zh"), t
    good = ["Handed Claude Code a 40-file refactor today. The plan held; tests took 2 hours to fix.",
            "今天把 40 个文件的重构交给它，计划没跑偏，修测试花了两个小时。不确定换个项目还行不行。"]
    assert ai_patterns(good[0], "en") == [] and ai_patterns(good[1], "zh") == []
    assert ai_patterns("one — two — three", "en") == ['AI-ish: "2 em dashes"']
