"""LLM access through any OpenAI-compatible chat API (DeepSeek, OpenAI, ...).

Uses JSON mode (supported by both DeepSeek and OpenAI) and validates every reply
against a pydantic model, retrying once with the validation error if it does not fit.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from xdraft.config import LLMConfig

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def parse(self, system: str, user: str, schema: type[T]) -> T: ...


def schema_instructions(schema: type[BaseModel]) -> str:
    return (
        "Reply with a single JSON object (no markdown fences, no prose) that matches this JSON Schema:\n"
        + json.dumps(schema.model_json_schema(), ensure_ascii=False)
    )


def extract_json(text: str) -> str:
    """Tolerate code fences or stray prose around the JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class OpenAICompatibleLLM:
    def __init__(self, cfg: LLMConfig, client=None):
        self.cfg = cfg
        if client is None:
            import openai

            key = os.environ.get(cfg.api_key_env)
            if not key:
                raise LLMError(
                    f"Environment variable {cfg.api_key_env} is not set "
                    f"(put it in .env; provider={cfg.provider})."
                )
            client = openai.OpenAI(
                api_key=key, base_url=cfg.base_url or None,
                timeout=cfg.timeout_seconds, max_retries=cfg.max_retries,
            )
        self.client = client

    def _complete(self, messages: list[dict]) -> str:
        import openai

        try:
            resp = self.client.chat.completions.create(
                model=self.cfg.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=self.cfg.temperature,
                max_tokens=self.cfg.max_tokens,
            )
        except openai.AuthenticationError as e:
            raise LLMError(f"{self.cfg.provider}: authentication failed — check {self.cfg.api_key_env}") from e
        except openai.RateLimitError as e:
            raise LLMError(f"{self.cfg.provider}: rate limited or out of credit: {e}") from e
        except openai.APIStatusError as e:
            raise LLMError(f"{self.cfg.provider}: API error {e.status_code}: {e.message}") from e
        except openai.APIConnectionError as e:
            raise LLMError(f"{self.cfg.provider}: cannot reach {self.cfg.base_url or 'api.openai.com'}") from e
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise LLMError("LLM reply was cut off (max_tokens too small)")
        return choice.message.content or ""

    def parse(self, system: str, user: str, schema: type[T]) -> T:
        messages = [
            {"role": "system", "content": system + "\n\n" + schema_instructions(schema)},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None
        for _attempt in range(2):
            raw = self._complete(messages)
            try:
                return schema.model_validate_json(extract_json(raw))
            except ValidationError as e:
                last_error = e
                log.warning("LLM reply did not match schema, retrying: %s", e)
                messages += [
                    {"role": "assistant", "content": raw},
                    {"role": "user", "content": f"That JSON was invalid: {e}. Reply again with corrected JSON only."},
                ]
        raise LLMError(f"LLM reply did not match the expected JSON shape: {last_error}")


def make_llm(cfg: LLMConfig) -> LLM:
    return OpenAICompatibleLLM(cfg)
