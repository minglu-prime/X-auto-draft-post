"""Shapes the LLM must return (validated with pydantic)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class TopicProposal(BaseModel):
    title: str = Field(description="Short internal title of the post idea")
    angle: str = Field(description="The author's own take / hook: what makes this worth posting")
    why: str = Field(description="One sentence on why this is timely or interesting")
    score: int = Field(ge=0, le=100, description="Expected interest for an AI-practitioner audience")
    item_ids: list[int] = Field(description="ids of the material items this topic is based on")


class TopicPlan(BaseModel):
    topics: list[TopicProposal]


class PostVersion(BaseModel):
    tweets: list[str] = Field(min_length=1, description="One string per tweet; a single tweet unless a thread is needed")
    source_urls: list[str] = Field(default_factory=list, description="URLs from the material that the post relies on")


class BilingualDraft(BaseModel):
    zh: PostVersion = Field(description="Chinese version, written natively (not a literal translation)")
    en: PostVersion = Field(description="English version, written natively (not a literal translation)")


class StyleProfile(BaseModel):
    style_markdown: str = Field(description="A style guide in Markdown describing how the author writes on X")
