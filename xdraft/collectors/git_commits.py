"""Collect commits from local repos, grouped into one Item per repo per day."""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from xdraft.collectors.base import CollectedItem, CollectResult
from xdraft.config import Config
from xdraft.redact import redact
from xdraft.timeutil import parse_iso

SOURCE = "git"
_RS, _US = "\x1e", "\x1f"
_STAT_RE = re.compile(
    r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?"
)


def read_commits(repo: Path, since: datetime, authors: list[str]) -> list[dict]:
    cmd = [
        # Force UTF-8 on both ends: the Windows locale (e.g. gbk) can't decode git's output.
        "git", "-C", str(repo), "-c", "i18n.logOutputEncoding=UTF-8",
        "log", "--no-merges", "--shortstat",
        f"--since={since.isoformat()}",
        f"--pretty=format:{_RS}%H{_US}%an{_US}%aI{_US}%s{_US}%b{_US}",
    ]
    cmd += [f"--author={a}" for a in authors]
    out = subprocess.run(cmd, capture_output=True, encoding="utf-8", check=True).stdout
    commits = []
    for chunk in out.split(_RS):
        if not chunk.strip():
            continue
        parts = chunk.split(_US)
        if len(parts) < 5:
            continue
        sha, author, date, subject, body = parts[:5]
        stat = _STAT_RE.search(parts[5] if len(parts) > 5 else "")
        commits.append({
            "sha": sha.strip(),
            "author": author,
            "date": date,
            "subject": subject.strip(),
            "body": body.strip(),
            "files": int(stat.group(1)) if stat else 0,
            "insertions": int(stat.group(2) or 0) if stat else 0,
            "deletions": int(stat.group(3) or 0) if stat else 0,
        })
    return commits


def collect(cfg: Config, since: datetime) -> CollectResult:
    gc = cfg.git
    extra = cfg.redaction.extra_patterns
    result = CollectResult()
    for repo_str in gc.repos:
        repo = cfg.resolve_path(repo_str)
        if not (repo / ".git").exists():
            result.errors.append(f"Not a git repo: {repo}")
            continue
        try:
            commits = read_commits(repo, since, gc.authors)
        except (subprocess.CalledProcessError, OSError) as e:
            result.errors.append(f"{repo.name}: git log failed: {e}")
            continue

        by_day: dict[str, list[dict]] = defaultdict(list)
        for c in reversed(commits):  # git log is newest-first
            by_day[c["date"][:10]].append(c)  # author-local calendar day

        for day, day_commits in sorted(by_day.items()):
            day_commits.sort(key=lambda c: c["date"])
            lines = [f"Repo: {repo.name} — {day} — {len(day_commits)} commits"]
            for c in day_commits:
                lines.append(
                    f"- {c['subject']} ({c['files']} files, +{c['insertions']}/-{c['deletions']})"
                )
                if c["body"]:
                    b = " ".join(c["body"].split())
                    lines.append("  " + (b[: gc.max_body_chars] + ("…" if len(b) > gc.max_body_chars else "")))
            result.items.append(CollectedItem(
                source=SOURCE,
                source_id=f"{repo.name}@{day}",
                ts=parse_iso(day_commits[-1]["date"]),
                title=redact(f"{repo.name}: {day_commits[-1]['subject']}"
                             + (f" (+{len(day_commits) - 1} more)" if len(day_commits) > 1 else ""), extra),
                body=redact("\n".join(lines), extra),
                meta={
                    "repo": repo.name,
                    "day": day,
                    "commits": [
                        {k: c[k] for k in ("sha", "subject", "files", "insertions", "deletions")}
                        for c in day_commits
                    ],
                    "insertions": sum(c["insertions"] for c in day_commits),
                    "deletions": sum(c["deletions"] for c in day_commits),
                },
            ))
    return result
