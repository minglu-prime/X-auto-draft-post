# X Auto Draft Post: Plan

## Context
Goal: automatically turn **my recent AI usage** (Claude Code logs, notes, git commits), **my take on new models**, and **articles from the web** into bilingual (Chinese/English) X drafts. Drafts go to a local web dashboard for review, editing and approval, and approved posts are published to X. The repo `minglu-prime/X-auto-draft-post` is currently empty (no commits).

### Similar projects on GitHub (searched 2026-11)
| Project | What it does | Gap compared with this plan |
|---|---|---|
| Prem95/socialautonomies (25★) | X agent: posting, scheduling, auto-replies | Generic posting bot; doesn't read personal AI usage |
| Ask149/iodevz-news-bot | HN/Reddit/GitHub → AI news → X | News feed only, no personal angle |
| winchxyz/x-ghostwriter, emremy/tweet-dna | Ghostwrites in your voice (style profiling) | No automatic material collection |
| O-Midey/beacon, cynthialmy/build-in-public-automate, rcmiskin10/ship-log | git commits → build-in-public drafts | Only uses git |
| Aditya6688/X_Post_Automation, tomoto0/ai-news-curator-bot | RSS/arXiv/HN → summary → posted to X | No personal usage, no review flow |

**Conclusion**: each piece exists somewhere (news aggregation, style imitation, git→posts), but nothing **combines "my own Claude Code usage + my take on models + web articles" into bilingual drafts with a review dashboard**. That combination is what sets this project apart. We can borrow ideas from them (beacon's "local, never auto-posted" principle, tweet-dna's style profile) without depending on their code.

## Architecture
```
collectors ──► SQLite (items) ──► pipeline ──► drafts ──► Web dashboard (review/edit) ──► publisher (X API)
```

**Stack** (as built): Python ≥3.11, uv, FastAPI + Jinja2 + a ~60-line fetch helper instead of HTMX (no CDN, works offline), SQLModel/SQLite, APScheduler, the `openai` SDK against DeepSeek or OpenAI (JSON mode + pydantic validation), httpx for the X API v2 (instead of tweepy), feedparser, optional trafilatura, and `git` via subprocess.

### 1. Collectors (`xdraft/collectors/`): one module per source, all producing a common `Item` (source, ts, title, body, url, meta, hash for dedup)
- `claude_code.py`: reads `~/.claude/projects/*/*.jsonl` and summarizes recent sessions by project: models used (`message.model`), user prompts, tool usage counts, session length. **Secrets and absolute paths are redacted locally first** (regexes for API keys/tokens/emails, plus a project allowlist/denylist).
- `notes.py`: watches `notes/inbox/*.md` (one note per file; frontmatter can carry tags/URLs). Also supports quick entry from the dashboard.
- `git_commits.py`: commit messages + diffstat from configured local repos for the last N days.
- `articles.py`: RSS (official blogs from Anthropic/OpenAI/Google, Simon Willison, etc.), HN Algolia API (AI keyword filter), arXiv cs.CL/cs.AI; trafilatura extracts the full text.
- Configuration lives in `config.yaml` (sources, repo paths, project denylist, posting time windows).

### 2. Pipeline (`xdraft/pipeline/`)
1. **Score/cluster** (`select.py`): an LLM scores recent items for relevance to "new model / AI workflow" topics and clusters them into 3–5 topics per day (e.g. "used new model X with Claude Code to refactor Y + model X release article").
2. **Draft** (`draft.py`): for each topic, generates in one pass: a Chinese post, an English post, an optional thread (≤5 tweets) and a cited source URL. The prompt includes the **style profile** (`style.md`, built from my past posts and hand-edited) and the opinions from my notes, so the post reads as "me" rather than a news summary. Uses any OpenAI-compatible model, DeepSeek `deepseek-chat` by default, configured in `config.yaml` under `llm:`.
3. **Check** (`check.py`): character count (X's weighted count, CJK characters count double), duplicate check against recently published posts, a check that facts trace back to source items (flags unsourced claims), and a sensitive-content check.

### 3. Web dashboard (`xdraft/web/`), bound to 127.0.0.1 only
- `/` today's draft queue: Chinese/English side by side, inline editing, "regenerate / shorten / change tone" buttons, a view of the source items, then **Approve → choose post now / scheduled time**, or Reject.
- `/inbox` quick note entry; `/sources` collection status and manual re-runs; `/history` published posts.
- Approved posts are marked `scheduled` on the draft itself (`scheduled_at`), and APScheduler publishes them when due. `xdraft publish-due` does the same for cron.

### 4. Publisher (`xdraft/publish/x.py`)
- X API v2 `POST /2/tweets` (OAuth 2.0 user context with PKCE, token stored locally), with thread support via reply_to. Check X's current API pricing/tier and rate limits during implementation.
- `--dry-run` / `PUBLISH_ENABLED=false` is the default; posting only works once explicitly enabled. Nothing is ever posted without approval.

## Repo layout
```
pyproject.toml  config.example.yaml  .env.example  README.md
xdraft/
  cli.py            # xdraft collect | draft | serve | publish-due
  models.py db.py   # SQLModel: Item, Topic, Draft, ScheduledPost
  collectors/{claude_code,notes,git_commits,articles}.py
  pipeline/{select,draft,check,prompts}.py
  publish/x.py
  web/{app.py, templates/*.html}
notes/inbox/  style.md
tests/  (fixtures: sample Claude Code jsonl, RSS xml)
```

## Milestones (all done, see README for usage)
1. **M1 skeleton + collectors**: pyproject, DB models, the 4 collectors, `xdraft collect` (verify with fixtures).
2. **M2 draft generation**: select + draft + check, `xdraft draft` prints bilingual drafts in the terminal.
3. **M3 web dashboard**: review/edit/approve/regenerate.
4. **M4 publishing**: X OAuth, scheduled posting, threads, history; dry-run by default.
5. **M5 polish**: style-profile import (from my past posts on X), a performance-data dashboard (likes/reposts fed back into topic scoring).

## Verification
- `pytest`: each collector parses its fixtures; redaction tests (sample jsonl containing a fake key must have it removed); character-count/thread-splitting tests; dry-run publish tests (tweepy mocked).
- End to end: `xdraft collect && xdraft draft` against real `~/.claude/projects` → `xdraft serve` → open 127.0.0.1:8000 in a browser to check the draft queue → approve one → it shows up in the log as a dry-run post → after enabling `PUBLISH_ENABLED` and posting one test post to a test account, check it on X.
- Note: this cloud session can't access the Claude Code logs on your machine, so M1 is developed against fixtures here; real-data checks run on your machine.
