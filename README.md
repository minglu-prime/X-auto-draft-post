# xdraft — X Auto Draft Post

Turns **your own AI usage** (Claude Code sessions, git commits, quick notes), **your takes on new models**, and **AI articles from the web** into bilingual (中文 / English) X drafts. You review, edit and approve them in a local dashboard before anything is posted.

See [docs/PLAN.md](docs/PLAN.md) for the design and the survey of similar projects.

## The pipeline

| Step | What happens | Where |
|---|---|---|
| 0. Collect | Your Claude Code sessions, notes, commits; AI blogs, arXiv; **trends**: HN front page, fast-rising GitHub repos, Product Hunt, 36Kr / 少数派 | `xdraft collect`, or daily automatically |
| 1. Topics (选题) | Trend signals (terms spreading across sources, weighted by HN points / stars) + a summary of your own AI usage → 5–10 topic suggestions, each with *why now*, *your experience*, an angle, format and score. **You pick.** | **Topics** page, `xdraft suggest` |
| 2. Write (撰写) | A thread of 3–8 tweets (or one tweet if you choose), in Chinese and English, built on **your take** if you typed one, with **your real writing** as examples and the full text of the articles | "Write this" / `xdraft write ID --take "…"` |
| 3. Polish (润色) | A de-AI pass: compares each sentence with your real writing and rewrites what doesn't sound like you; gives 2–3 alternative openings | automatic |
| 4. Optimize (优化) | Fix-only: runs only if checks find problems (too long, unsourced numbers, unknown links, **AI tells** like "不是…而是…" or "isn't X — it's Y") and fixes just those | automatic |
| 5. Review | Edit, swap in an alternative opening, AI rewrite, see earlier versions | **Drafts** page |
| 6. Publish (发布) | Approve → next free posting slot (default 09:00 and 20:00 local time) → posted to X (dry run until enabled); engagement is fed back into step 1 | automatic |

With `xdraft serve` running, steps 0–1 run every day at `pipeline.auto_suggest_at` (default 08:30 local time) and approved posts publish on their own. If you don't keep the dashboard open, `uv run xdraft schedule install` sets up Windows Task Scheduler to do the same.

## Status

| Milestone | State |
|---|---|
| M1 collectors (`xdraft collect`) | ✅ |
| M2 draft generation with DeepSeek / OpenAI | ✅ |
| M3 local review dashboard (`xdraft serve`) | ✅ |
| M4 publishing to X: OAuth 2.0, threads, scheduling, dry run by default | ✅ |
| M5 style import from your X archive + engagement feedback | ✅ |
| Content pipeline: trend-based topic suggestions → write → polish → optimize → slot publishing | ✅ |

## Setup

All commands below are written for **Windows PowerShell** and run as-is there. Run them one at a time.

You need [uv](https://docs.astral.sh/uv/) and git. To install uv on Windows:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```powershell
git clone https://github.com/minglu-prime/X-auto-draft-post.git
```

```powershell
cd X-auto-draft-post
```

```powershell
uv sync
```

```powershell
uv run xdraft init
```

`init` creates `config.yaml`, `.env` and `style.md` from the examples (it never overwrites existing ones), plus the `notes\inbox` and `data` folders. Then put your key in `.env`:

```powershell
notepad .env
```

and set `git.repos` (your local repo paths, e.g. `C:\Users\you\code\my-project`) in `config.yaml`:

```powershell
notepad config.yaml
```

**LLM**: any OpenAI-compatible API. The default is DeepSeek (`deepseek-chat`, key in `DEEPSEEK_API_KEY`). To use OpenAI, set `llm.provider: openai`, `llm.base_url: null`, `llm.model: <model>` and `llm.api_key_env: OPENAI_API_KEY` in `config.yaml`. JSON mode is used and every reply is validated.

**Claude Code logs** are read from `~/.claude/projects`, which on Windows is `%USERPROFILE%\.claude\projects`. Nothing to configure.

### Make drafts sound like you

Drafts sound like AI when the writer has no idea how *you* write. Give it real examples:

1. **Your X archive** (Settings → Your account → Download an archive, then unzip it). This saves your posts as examples in `voice\mine` and writes a style guide to `style.md`:

```powershell
uv run xdraft style-import "$HOME\Downloads\twitter-archive"
```

2. **Things you wrote elsewhere** (Jike, 小红书, newsletters…): paste them in the dashboard **Inbox → Voice** ("My own writing"), separating pieces with a line of `---`. Or save them in a text file and add it:

```powershell
uv run xdraft voice add-file "$HOME\Desktop\my-posts.txt"
```

3. **Writers you like**: paste their text in **Inbox → Voice** ("A writer I like"), or save an article by link (blogs, Substack, Medium; x.com needs login and won't work):

```powershell
uv run xdraft voice add-url "https://example.com/some-article"
```

Check what you have; 10–20 pieces of your own writing make the biggest difference:

```powershell
uv run xdraft voice show
```

The other big lever is **your take**: when you pick a topic, type two or three rough sentences of what *you* think in the box above "Write this". The post is then built on your opinion instead of one the AI made up.

## Daily use

```powershell
uv run xdraft serve
```

Then open http://127.0.0.1:8000 in your browser. Stop the dashboard with `Ctrl+C`.

In the dashboard:

1. **Topics**: see what is trending and your recent AI usage, click **Suggest topics** (or wait for the daily run), then **Write this** on the ones you like and **Dismiss** the rest.
2. **Drafts**: each picked topic has a Chinese and an English card that went through write → polish → optimize. Edit inline (a line of `---` separates thread tweets), click an alternative opening to swap it in, or use **Rewrite with AI**. Then **Approve → next slot**, **Post now**, a custom **Schedule**, or **Reject**.
3. **Inbox** saves quick notes, which are the strongest signal for topic choice. **History** shows what was posted, with likes, reposts, replies and views.

The same flow from the terminal, one command at a time:

| Command | What it does |
|---|---|
| `uv run xdraft collect` | gather the last 3 days of material |
| `uv run xdraft suggest` | trends + your usage → topic suggestions (`--collect` collects first) |
| `uv run xdraft topics` | list suggestions with their ids |
| `uv run xdraft write 12 --take "审核比写代码累"` | write → polish → fix topic 12, built on your take |
| `uv run xdraft drafts` | list pending drafts |
| `uv run xdraft run` | collect + suggest + publish due posts (what the daily task runs) |
| `uv run xdraft publish-due` | post approved drafts that are due |
| `uv run xdraft metrics` | refresh engagement numbers |

A picked topic costs 2–5 LLM calls (write, polish, and 0–3 fix passes only when checks find problems); a suggestion run costs 1. Each draft is checked against X's weighted length (CJK counts double, URLs count 23), links that are not in the sources, numbers that are not in the sources, near-duplicates of recent posts, and leaked secrets or emails. Drafts over 280 cannot be approved.

### Running without the dashboard open

`xdraft serve` already runs everything on schedule while it is open. To have it run without the dashboard, register two Windows scheduled tasks: a daily collect + suggest at `pipeline.auto_suggest_at`, and publishing every 5 minutes. They run windowless and log to `data\scheduled.log`.

```powershell
uv run xdraft schedule install
```

Check what is registered (or see the exact commands first):

```powershell
uv run xdraft schedule show
```

Remove both tasks:

```powershell
uv run xdraft schedule remove
```

Task times use your PC's clock, so keep `timezone` in `config.yaml` the same as your Windows time zone. On macOS or Linux, `uv run xdraft schedule show` prints the matching crontab lines instead.

## Posting to X

Publishing is **off by default**. Approving a draft then only writes it to `data\publish.log` and marks it *dry run*.

1. Create an app at developer.x.com with OAuth 2.0 enabled, type *Web App / native*, permissions **Read and write**, and callback URL `http://127.0.0.1:8000/x/callback`. Check X's current API plan and pricing for posting limits.
2. Put `X_CLIENT_ID` (and `X_CLIENT_SECRET` for a confidential client) in `.env`.
3. Run `uv run xdraft serve`, click **Connect X**, and authorize. The token is stored in `data\x_token.json` inside your project folder (readable only by you on macOS/Linux) and refreshed automatically.
4. Set `publish.enabled: true` in `config.yaml` and restart the dashboard. The header shows **LIVE posting**.

Approved drafts go into the next free slot from `publish.slots` (in `timezone`, at most `max_per_slot` per slot), and are published within a minute of their slot while the dashboard runs, or within 5 minutes by the scheduled task. Threads are posted as replies; if one fails half-way, approving it again resumes where it stopped instead of posting duplicates. On a rate limit (429) the post is rescheduled automatically.

## Sources

| Source | Reads | One item per |
|---|---|---|
| `claude_code` | `~/.claude/projects/*/*.jsonl` | session: project, models, tool counts, your prompts |
| `notes` | `notes/inbox/*.md` (optional YAML frontmatter: `title`, `date`, `tags`, `url`) | note |
| `git` | `git log` of repos in `config.yaml` | repo × day |
| `articles` | RSS/Atom feeds (AI blogs, arXiv, Product Hunt, 36Kr, 少数派), Hacker News stories above `min_points` (all tech by default), new GitHub repos gaining stars fastest | article / repo |

## Privacy and safety

- Everything stays local: `data/xdraft.db`, `notes/inbox/`, `style.md`, `.env` and `config.yaml` are git-ignored.
- Before storage, text from Claude Code, git and notes goes through `xdraft/redact.py`. It strips API keys and tokens, private keys, JWTs, `KEY=value` secrets, URL credentials, emails and home-directory paths. Add regexes in `redaction.extra_patterns`, and hide projects with `claude_code.exclude_projects`.
- Only the redacted material for the current window is sent to your LLM provider. Web content is marked as data, not instructions, in every prompt.
- The dashboard listens on 127.0.0.1 only. It rejects other `Host` headers (DNS rebinding) and any write request that lacks its own header (CSRF from other sites).
- Nothing is posted without your explicit approval, and nothing at all while `publish.enabled` is false.

## Development

```powershell
uv run pytest
```
