import subprocess

from xdraft.collectors import git_commits


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_commits_grouped_per_day(cfg, since, tmp_path):
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "a.py").write_text("print(1)\nprint(2)\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "Add parser", "-m", "Uses token=abcdef123456 for tests")
    (repo / "a.py").write_text("print(1)\n")
    _git(repo, "commit", "-q", "-am", "Trim parser")

    cfg.git.repos = [str(repo), str(tmp_path / "missing")]
    res = git_commits.collect(cfg, since)
    assert len(res.errors) == 1 and "Not a git repo" in res.errors[0]
    assert len(res.items) == 1
    item = res.items[0]
    assert item.source_id.startswith("demo@")
    assert [c["subject"] for c in item.meta["commits"]] == ["Add parser", "Trim parser"]
    assert item.meta["insertions"] == 2 and item.meta["deletions"] == 1
    assert "Trim parser" in item.title and "+1 more" in item.title
    assert "abcdef123456" not in item.body
