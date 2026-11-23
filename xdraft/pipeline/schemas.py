"""Shapes the LLM must return (validated with pydantic)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PostVersion(BaseModel):
    tweets: list[str] = Field(min_length=1, description="One string per tweet; a single tweet unless a thread is needed")
    source_urls: list[str] = Field(default_factory=list, description="URLs from the material that the post relies on")


class BilingualDraft(BaseModel):
    zh: PostVersion = Field(description="Chinese version, written natively (not a literal translation)")
    en: PostVersion = Field(description="English version, written natively (not a literal translation)")


class StyleProfile(BaseModel):
    style_markdown: str = Field(description="A style guide in Markdown describing how the author writes on X")


class TopicSuggestion(BaseModel):
    title: str = Field(description="Short working title of the post idea")
    angle: str = Field(description="The specific take or hook the post will make")
    why_now: str = Field(description="The trend evidence: what is happening right now that makes this timely")
    my_connection: str = Field(description="The author's own usage/experience that relates to it; empty if none")
    format: Literal["single", "thread"] = Field(description="single tweet or a short thread")
    score: int = Field(ge=0, le=100, description="Expected interest x fit with the author's voice")
    item_ids: list[int] = Field(description="ids of the material items supporting this topic")
    trend_terms: list[str] = Field(default_factory=list, description="Trend signals this topic rides on")


class SuggestionPlan(BaseModel):
    topics: list[TopicSuggestion]


class OptimizedVersion(PostVersion):
    alt_hooks: list[str] = Field(default_factory=list, description="2-3 alternative opening lines for tweet 1")
    notes: str = Field(default="", description="One line: what was changed and why")


class OptimizedDraft(BaseModel):
    zh: OptimizedVersion
    en: OptimizedVersion
