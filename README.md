# xdraft — X Auto Draft Post

Turns **your own AI usage** (Claude Code sessions, git commits, quick notes), **your takes on new models**, and **AI articles from the web** into bilingual (中文 / English) X drafts. You review, edit and approve them in a local dashboard before anything is posted.

See [docs/PLAN.md](docs/PLAN.md) for the design and the survey of similar projects.

## The pipeline

| Step | What happens | Where |
|---|---|---|
| 0. Collect | Your Claude Code sessions, notes, commits; AI blogs, arXiv; **trends**: HN front page, fast-rising GitHub repos, Reddit, Product Hunt, 36Kr / 少数派 | `xdraft collect`, or daily automatically |
| 1. Topics (选题) | Trend signals (terms spreading across sources, weighted by HN points / stars) + a summary of your own AI usage → 5–10 topic suggestions, each with *why now*, *your experience*, an angle, format and score. **You pick.** | **Topics** page, `xdraft suggest` |
| 2. Write (撰写) | Chinese + English first drafts in your style | "Write this" / `xdraft write ID` |
| 3. Polish (润色) | Voice and natural language: no 翻译腔, no hype words, facts unchanged | automatic |
| 4. Optimize (优化) | Stronger hook, skimmable structure, fixes for every check warning, 2–3 alternative openings; re-runs while length checks still fail | automatic |
| 5. Review | Edit, swap in an alternative opening, AI rewrite, see earlier versions | **Drafts** page |
| 6. Publish (发布) | Approve → next free posting slot (default 09:00 and 20:00 local time) → posted to X (dry run until enabled); engagement is fed back into step 1 | automatic |

With `xdraft serve` running, steps 0–1 run every day at `pipeline.auto_suggest_at` (default 08:30 local time) and approved posts publish on their own. Without the dashboard, put `xdraft run` (collect + suggest + publish due) in cron.

## Status

| Milestone | State |
|---|---|
| M1 collectors (`xdraft collect`) | ✅ |
| M2 draft generation with DeepSeek / OpenAI (`xdraft draft`) | ✅ |
| M3 local review dashboard (`xdraft serve`) | ✅ |
| M4 publishing to X: OAuth 2.0, threads, scheduling, dry run by default | ✅ |
| M5 style import from your X archive + engagement feedback | ✅ |
| Content pipeline: trend-based topic suggestions → write → polish → optimize → slot publishing | ✅ |

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

In the dashboard:

1. **Topics**: see what is trending and your recent AI usage, click **Suggest topics** (or wait for the daily run), then **Write this** on the ones you like and **Dismiss** the rest.
2. **Drafts**: each picked topic has a Chinese and an English card that went through write → polish → optimize. Edit inline (a line of `---` separates thread tweets), click an alternative opening to swap it in, or use **Rewrite with AI**. Then **Approve → next slot**, **Post now**, a custom **Schedule**, or **Reject**.
3. **Inbox** saves quick notes, which are the strongest signal for topic choice. **History** shows what was posted, with likes, reposts, replies and views.

The same flow from the terminal:

```bash
uv run xdraft collect                 # gather the last 3 days
uv run xdraft suggest                 # trends + usage → topic suggestions (add --collect to do both)
uv run xdraft topics                  # list suggestions
uv run xdraft write 12 15             # write → polish → optimize the picked topics
uv run xdraft drafts                  # list pending drafts
uv run xdraft run                     # cron: collect + suggest + publish due posts
uv run xdraft publish-due             # post approved drafts that are due
uv run xdraft metrics                 # refresh engagement numbers
```

A picked topic costs 3–5 LLM calls (write, polish, 1–3 optimize passes); a suggestion run costs 1. Each draft is checked against X's weighted length (CJK counts double, URLs count 23), links that are not in the sources, numbers that are not in the sources, near-duplicates of recent posts, and leaked secrets or emails. Drafts over 280 cannot be approved.

## Posting to X

Publishing is **off by default**. Approving a draft then only writes it to `data/publish.log` and marks it *dry run*.

1. Create an app at developer.x.com with OAuth 2.0 enabled, type *Web App / native*, permissions **Read and write**, and callback URL `http://127.0.0.1:8000/x/callback`. Check X's current API plan and pricing for posting limits.
2. Put `X_CLIENT_ID` (and `X_CLIENT_SECRET` for a confidential client) in `.env`.
3. Run `xdraft serve`, click **Connect X**, and authorize. The token is stored in `data/x_token.json` with mode 600 and refreshed automatically.
4. Set `publish.enabled: true` in `config.yaml` and restart. The header shows **LIVE posting**.

Approved drafts go into the next free slot from `publish.slots` (in `timezone`, at most `max_per_slot` per slot). While `xdraft serve` runs, due drafts are published every minute and metrics refresh every `publish.metrics_every_minutes`. Without the dashboard running, use cron: `*/5 * * * * cd /path/to/repo && uv run xdraft publish-due`, plus a daily `uv run xdraft run`. Threads are posted as replies; if one fails half-way, approving it again resumes where it stopped instead of posting duplicates. On a rate limit (429) the post is rescheduled automatically.

## Sources

| Source | Reads | One item per |
|---|---|---|
| `claude_code` | `~/.claude/projects/*/*.jsonl` | session: project, models, tool counts, your prompts |
| `notes` | `notes/inbox/*.md` (optional YAML frontmatter: `title`, `date`, `tags`, `url`) | note |
| `git` | `git log` of repos in `config.yaml` | repo × day |
| `articles` | RSS/Atom feeds (AI blogs, arXiv, Reddit, Product Hunt, 36Kr, 少数派), Hacker News stories above `min_points` (all tech by default), new GitHub repos gaining stars fastest | article / repo |

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
