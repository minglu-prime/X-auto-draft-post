"""Collector registry: name -> collect(cfg, since) -> CollectResult."""

from xdraft.collectors import articles, claude_code, git_commits, notes

COLLECTORS = {
    claude_code.SOURCE: claude_code.collect,
    notes.SOURCE: notes.collect,
    git_commits.SOURCE: git_commits.collect,
    articles.SOURCE: articles.collect,
}

__all__ = ["COLLECTORS"]
