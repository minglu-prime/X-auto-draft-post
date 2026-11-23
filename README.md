# xdraft — X Auto Draft Post

Turns **your own AI usage** (Claude Code sessions, git commits, quick notes), **your takes on new models**, and **AI articles from the web** into bilingual (中文 / English) X drafts. You review, edit and approve them in a local dashboard before anything is posted.

See [docs/PLAN.md](docs/PLAN.md) for the design and the survey of similar projects.

```
collectors ─► SQLite ─► LLM: pick topics ─► LLM: zh + en drafts ─► checks ─► dashboard (edit / rewrite / approve) ─► X (or dry run)
                                                                                         ▲
                                                          engagement metrics ────────────┘ (fed back into topic picking)
```

## Status

| Milestone | State |
|---|---|
| M1 collectors (`xdraft collect`) | ✅ |
| M2 draft generation with DeepSeek / OpenAI (`xdraft draft`) | ✅ |
| M3 local review dashboard (`xdraft serve`) | ✅ |
| M4 publishing to X: OAuth 2.0, threads, scheduling, dry run by default | ✅ |
| M5 style import from your X archive + engagement feedback | ✅ |

## Setup

```bash
uv sync
cp config.example.yaml config.yaml   # edit git repos, feeds, LLM provider
cp .env.example .env                 # put your API keys here (git-ignored)
```

**LLM**: any OpenAI-compatible API. The default is DeepSeek (`deepseek-chat`, key in `DEEPSEEK_API_KEY`). To use OpenAI, set `llm.provider: openai`, `llm.base_url: null`, `llm.model: <model>` and `llm.api_key_env: OPENAI_API_KEY` in `config.yaml`. JSON mode is used and every reply is validated.

**Style** (optional but recommended): `cp style.example.md style.md` and edit, or learn it from your X archive (Settings → Your account → Download an archive):

```bash
uv run xdraft style-import ~/Downloads/twitter-archive
```

## Daily use

```bash
uv run xdraft serve        # http://127.0.0.1:8000
```

In the dashboard: **Collect material**, then **Generate drafts**. Each topic gets a Chinese and an English card. Edit them inline (a line of `---` separates thread tweets), use **Rewrite with AI** (shorter / punchier / more personal / your own instruction), then **Approve & post now**, **Schedule**, or **Reject**. The **Inbox** page saves quick notes, which are the strongest signal for topic choice. **History** shows what was posted, with likes, reposts, replies and views.

The same flow from the terminal:

```bash
uv run xdraft collect                 # gather the last 3 days
uv run xdraft draft                   # pick topics + write drafts (add --collect to do both)
uv run xdraft drafts                  # list pending drafts
uv run xdraft publish-due             # post approved drafts that are due (for cron)
uv run xdraft metrics                 # refresh engagement numbers
```

Each draft is checked against X's weighted length (CJK counts double, URLs count 23), links that are not in the sources, numbers that are not in the sources, near-duplicates of recent posts, and leaked secrets or emails. Drafts over 280 cannot be approved.

## Posting to X

Publishing is **off by default**. Approving a draft then only writes it to `data/publish.log` and marks it *dry run*.

1. Create an app at developer.x.com with OAuth 2.0 enabled, type *Web App / native*, permissions **Read and write**, and callback URL `http://127.0.0.1:8000/x/callback`. Check X's current API plan and pricing for posting limits.
2. Put `X_CLIENT_ID` (and `X_CLIENT_SECRET` for a confidential client) in `.env`.
3. Run `xdraft serve`, click **Connect X**, and authorize. The token is stored in `data/x_token.json` with mode 600 and refreshed automatically.
4. Set `publish.enabled: true` in `config.yaml` and restart. The header shows **LIVE posting**.

While `xdraft serve` runs, scheduled drafts are published every minute and metrics refresh every `publish.metrics_every_minutes`. Without the dashboard running, use cron: `*/5 * * * * cd /path/to/repo && uv run xdraft publish-due`. Threads are posted as replies; if one fails half-way, approving it again resumes where it stopped instead of posting duplicates. On a rate limit (429) the post is rescheduled automatically.

## Sources

| Source | Reads | One item per |
|---|---|---|
| `claude_code` | `~/.claude/projects/*/*.jsonl` | session: project, models, tool counts, your prompts |
| `notes` | `notes/inbox/*.md` (optional YAML frontmatter: `title`, `date`, `tags`, `url`) | note |
| `git` | `git log` of repos in `config.yaml` | repo × day |
| `articles` | RSS/Atom feeds (blogs, arXiv) + Hacker News stories above `min_points`, keyword-filtered | article |

## Privacy and safety

- Everything stays local: `data/xdraft.db`, `notes/inbox/`, `style.md`, `.env` and `config.yaml` are git-ignored.
- Before storage, text from Claude Code, git and notes goes through `xdraft/redact.py`. It strips API keys and tokens, private keys, JWTs, `KEY=value` secrets, URL credentials, emails and home-directory paths. Add regexes in `redaction.extra_patterns`, and hide projects with `claude_code.exclude_projects`.
- Only the redacted material for the current window is sent to your LLM provider. Web content is marked as data, not instructions, in every prompt.
- The dashboard listens on 127.0.0.1 only. It rejects other `Host` headers (DNS rebinding) and any write request that lacks its own header (CSRF from other sites).
- Nothing is posted without your explicit approval, and nothing at all while `publish.enabled` is false.

## Development

```bash
uv run pytest
```
