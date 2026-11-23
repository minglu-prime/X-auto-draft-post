"""Configuration loaded from config.yaml (see config.example.yaml)."""

from __future__ import annotations

import os
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
    last_reply_chars: int = 600  # the session's last assistant message: a concrete "what got done"


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
    # Apply the keyword filter to this feed (turn off for AI-only and trend feeds).
    filter: bool = True
    max_items: int = 30
    # ai | tech | community | zh | product — shown to the LLM as context for trends.
    category: str = "ai"


class HackerNewsConfig(BaseModel):
    enabled: bool = True
    min_points: int = 100
    # False = every front-page-level story (broad tech trends); True = AI keywords only.
    keyword_filter: bool = False


class GitHubTrendingConfig(BaseModel):
    """Fast-rising new repositories via the GitHub search API (no scraping)."""

    enabled: bool = True
    created_within_days: int = 7
    min_stars: int = 100
    max_items: int = 15
    token_env: str = "GITHUB_TOKEN"  # optional; raises the rate limit


class ArticlesConfig(BaseModel):
    enabled: bool = True
    keywords: list[str] = Field(default_factory=lambda: list(DEFAULT_KEYWORDS))
    feeds: list[FeedConfig] = Field(default_factory=list)
    hackernews: HackerNewsConfig = Field(default_factory=HackerNewsConfig)
    github: GitHubTrendingConfig = Field(default_factory=GitHubTrendingConfig)
    fetch_fulltext: bool = True  # fetch article text when a topic is written (only for that topic's articles)
    fulltext_max_chars: int = 6000
    timeout_seconds: float = 20.0


class RedactionConfig(BaseModel):
    # Extra regexes whose matches are replaced with [REDACTED].
    extra_patterns: list[str] = Field(default_factory=list)


class LLMConfig(BaseModel):
    """Any OpenAI-compatible chat API. DeepSeek: base_url https://api.deepseek.com."""

    provider: str = "deepseek"  # deepseek | openai | other (label only)
    base_url: str | None = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    # Name of the environment variable holding the key (never put the key itself here).
    api_key_env: str = "DEEPSEEK_API_KEY"
    temperature: float = 0.7        # topic suggestions
    temperature_write: float = 1.0  # first drafts: higher = less generic (DeepSeek suggests ~1.0-1.5 for writing)
    temperature_edit: float = 0.6   # polish / fixes: keep close to the draft
    max_tokens: int = 6000
    timeout_seconds: float = 120.0
    max_retries: int = 2


class DraftConfig(BaseModel):
    max_items: int = 60  # newest items sent to topic selection
    item_body_chars: int = 1200
    fulltext_chars: int = 4000  # full article text used when a picked topic is written
    thread_min_tweets: int = 3
    thread_max_tweets: int = 8
    style_path: str = "style.md"
    duplicate_threshold: float = 0.8  # difflib ratio vs. recent posts


class VoiceConfig(BaseModel):
    dir: str = "voice"           # voice/mine (your writing) and voice/references (writers you like)
    own_examples: int = 10       # your real posts shown to the writer per draft
    reference_excerpts: int = 3
    excerpt_chars: int = 600


class PipelineConfig(BaseModel):
    suggest_count: int = 8          # topic suggestions per run
    personal_items: int = 30        # max own items (sessions, notes, commits) sent to the LLM
    trend_terms: int = 15           # trend signals shown to the LLM
    optimize_rounds: int = 2        # extra optimize passes while hard checks still fail
    # Daily automatic collect + suggest while `xdraft serve` runs, local time "HH:MM"; null = off.
    auto_suggest_at: str | None = "08:30"


class PublishConfig(BaseModel):
    # Off by default: approved posts are only logged as dry runs.
    enabled: bool = False
    client_id_env: str = "X_CLIENT_ID"
    client_secret_env: str = "X_CLIENT_SECRET"  # optional (confidential clients)
    redirect_uri: str = "http://127.0.0.1:8000/x/callback"
    token_path: str = "data/x_token.json"
    api_base: str = "https://api.x.com"
    authorize_url: str = "https://x.com/i/oauth2/authorize"
    retry_minutes_on_rate_limit: int = 15
    # Approved posts go into the next free slot (local time, see `timezone`).
    slots: list[str] = Field(default_factory=lambda: ["09:00", "20:00"])
    max_per_slot: int = 1
    metrics_every_minutes: int = 120


class Config(BaseModel):
    db_path: str = "data/xdraft.db"
    lookback_days: int = 3
    timezone: str = "Asia/Shanghai"  # IANA name; used for slots and the daily job
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)
    notes: NotesConfig = Field(default_factory=NotesConfig)
    git: GitConfig = Field(default_factory=GitConfig)
    articles: ArticlesConfig = Field(default_factory=ArticlesConfig)
    redaction: RedactionConfig = Field(default_factory=RedactionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    draft: DraftConfig = Field(default_factory=DraftConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    voice: VoiceConfig = Field(default_factory=VoiceConfig)
    publish: PublishConfig = Field(default_factory=PublishConfig)

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


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=value lines; never overrides variables already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


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
    load_dotenv(cfg.base_dir / ".env")
    return cfg
