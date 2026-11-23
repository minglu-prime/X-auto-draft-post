"""Fake LLM for tests: returns queued replies (pydantic objects or callables)."""

from __future__ import annotations


class FakeLLM:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str, type]] = []
        self.temperatures: list[float | None] = []

    def parse(self, system, user, schema, temperature=None):
        self.calls.append((system, user, schema))
        self.temperatures.append(temperature)
        if not self.replies:
            raise AssertionError(f"unexpected LLM call for {schema.__name__}")
        reply = self.replies.pop(0)
        if callable(reply) and not isinstance(reply, schema):
            reply = reply(system, user)
        assert isinstance(reply, schema), f"expected {schema.__name__}, got {type(reply).__name__}"
        return reply
