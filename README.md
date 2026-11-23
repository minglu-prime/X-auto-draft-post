# xdraft — X Auto Draft Post

Turns **your own AI usage** (Claude Code sessions, git commits, quick notes), **your takes on new models**, and **AI articles from the web** into bilingual (中文 / English) X drafts that you review and approve in a local dashboard before anything is posted.

See [docs/PLAN.md](docs/PLAN.md) for the full plan and the survey of similar projects.

## Status

| Milestone | State |
|---|---|
| M1 collectors + `xdraft collect` | ✅ done |
| M2 draft generation | ⏳ next |
| M3 web review dashboard | — |
| M4 publishing to X (dry-run by default) | — |
| M5 style import + engagement feedback | — |

## Quick start

```bash
uv sync
cp config.example.yaml config.yaml   # then edit repos / feeds
uv run xdraft collect                # gather the last 3 days
uv run xdraft items                  # list what was collected
uv run xdraft items -s claude_code --full
```

`xdraft collect -s notes -s git -d 7` runs only some sources with a 7-day window.

## Sources (M1)

| Source | Reads | One item per |
|---|---|---|
| `claude_code` | `~/.claude/projects/*/*.jsonl` | session: project, models, tool counts, your prompts |
| `notes` | `notes/inbox/*.md` (optional YAML frontmatter: `title`, `date`, `tags`, `url`) | note |
| `git` | `git log` of repos in `config.yaml` | repo × day |
| `articles` | RSS/Atom feeds (blogs, arXiv) + Hacker News stories above `min_points`, keyword-filtered | article |

Example note (`notes/inbox/2026-11-23-model-take.md`):

```markdown
---
tags: [claude, coding]
url: https://example.com/release-notes
---
# The new model is much better at long refactors
It kept the whole plan in mind across 40 files...
```

## Privacy

Everything stays in a local SQLite file (`data/xdraft.db`). Before anything is stored, text from Claude Code, git and notes passes through `xdraft/redact.py`, which strips API keys/tokens, private keys, JWTs, `KEY=value` secrets, URL credentials, email addresses and home-directory paths. Add your own regexes under `redaction.extra_patterns`, and hide whole projects with `claude_code.exclude_projects`.

## Development

```bash
uv run pytest
```
