from types import SimpleNamespace

import pytest

from xdraft.config import LLMConfig
from xdraft.llm import LLMError, OpenAICompatibleLLM, extract_json
from xdraft.pipeline.schemas import PostVersion


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.kwargs = []

    def create(self, **kwargs):
        self.kwargs.append(kwargs)
        content, finish = self.replies.pop(0)
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content))])


def _llm(replies):
    comp = FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return OpenAICompatibleLLM(LLMConfig(), client=client), comp


def test_parse_uses_json_mode_and_validates():
    llm, comp = _llm([('```json\n{"tweets": ["hi"], "source_urls": []}\n```', "stop")])
    out = llm.parse("sys", "user", PostVersion)
    assert out.tweets == ["hi"]
    kw = comp.kwargs[0]
    assert kw["response_format"] == {"type": "json_object"} and kw["model"] == "deepseek-chat"
    assert "JSON Schema" in kw["messages"][0]["content"]


def test_parse_retries_once_on_invalid_json():
    llm, comp = _llm([('{"tweets": []}', "stop"), ('{"tweets": ["ok"]}', "stop")])
    assert llm.parse("s", "u", PostVersion).tweets == ["ok"]
    assert "invalid" in comp.kwargs[1]["messages"][-1]["content"]


def test_truncated_reply_raises():
    llm, _ = _llm([('{"tweets": ["a', "length")])
    with pytest.raises(LLMError):
        llm.parse("s", "u", PostVersion)


def test_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    with pytest.raises(LLMError, match="DEEPSEEK_API_KEY"):
        OpenAICompatibleLLM(LLMConfig())


def test_extract_json():
    assert extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
