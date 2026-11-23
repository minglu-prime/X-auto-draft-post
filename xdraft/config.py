"""Configuration loaded from config.yaml (see config.example.yaml)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, PrivateAttr

DEFAULT_KEYWORDS = [
    "AI", "LLM", "GPT", "Claude", "Anthropic", "OpenAI", "Gemini", "DeepMind",
    "Llama", "Mistral", "Qwen", "DeepSeek", "agent", "model", "transformer",
    "reasoning", "benchmark", "fine-tuning", "RAG", "inference",
]


class ClaudeCodeConfig(BaseModel):
    enabled: bool = True
    projects_dir: str = "~/.claude/projects"
    # fnmatch globs on the project folder name (basename of the session cwd).
    include_projects: list[str] = Field(default_factory=list)
    exclude_projects: list[str] = Field(default_factory=list)
    max_prompts_per_session: int = 15
    max_prompt_chars: int = 400
    min_user_prompts: int = 1


class NotesConfig(BaseModel):
    enabled: bool = True
    inbox_dir: str = "notes/inbox"


class GitConfig(BaseModel):
    enabled: bool = True
    repos: list[str] = Field(default_factory=list)
    # Passed to `git log --author` (OR-ed). Empty = all authors.
    authors: list[str] = Field(default_factory=list)
    max_body_chars: int = 300


class FeedConfig(BaseModel):
    name: str
    url: str
    # Apply the keyword filter to this feed (turn off for AI-only feeds).
    filter: bool = True
    max_items: int = 30


class HackerNewsConfig(BaseModel):
    enabled: bool = True
    min_points: int = 100


class ArticlesConfig(BaseModel):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    feeds: list[FeedConfig] = Field(default_factory=list)
    hackernews: HackerNewsConfig = Field(default_factory=HackerNewsConfig)
    fetch_fulltext: bool = False
    fulltext_max_chars: int = 6000
    timeout_seconds: float = 20.0


class RedactionConfig(BaseModel):
    # Extra regexes whose matches are replaced with [REDACTED].
    extra_patterns: list[str] = Field(default_factory=list)


class Config(BaseModel):
    db_path: str = "data/xdraft.db"
    lookback_days: int = 3
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    articles: ArticlesConfig = Field(default_factory=ArticlesConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)

    _base_dir: Path = PrivateAttr(default_factory=Path.cwd)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def resolve_path(self, p: str | Path) -> Path:
        """Expand ~ and resolve relative paths against the config file's folder."""
        path = Path(p).expanduser()
        if not path.is_absolute():
            path = self._base_dir / path
        return path


def load_config(path: str | Path | None = None) -> Config:
    """Load config from `path`, or ./config.yaml if present, else defaults."""
    candidate = Path(path) if path else Path("config.yaml")
    if candidate.exists():
        data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
        cfg = Config.model_validate(data)
        cfg._base_dir = candidate.resolve().parent
    elif path:
        raise FileNotFoundError(f"Config file not found: {candidate}")
    else:
        cfg = Config()
        cfg._base_dir = Path.cwd()
    return cfg
