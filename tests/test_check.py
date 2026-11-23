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
