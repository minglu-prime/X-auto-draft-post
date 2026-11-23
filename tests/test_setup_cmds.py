import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xdraft import cli, setup_cmds
from xdraft.config import Config

REPO = Path(__file__).resolve().parent.parent
runner = CliRunner()


@pytest.fixture
def repo_copy(tmp_path, monkeypatch):
    for name in ("config.example.yaml", ".env.example", "style.example.md"):
        shutil.copyfile(REPO / name, tmp_path / name)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_creates_then_keeps_then_forces(repo_copy):
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 0, r.output
    for name in ("config.yaml", ".env", "style.md"):
        assert (repo_copy / name).is_file()
    assert (repo_copy / "notes" / "inbox").is_dir() and (repo_copy / "data").is_dir()
    assert "notepad .env" in r.output and "&&" not in r.output

    (repo_copy / ".env").write_text("DEEPSEEK_API_KEY=mine\n")
    r = runner.invoke(cli.app, ["init"])
    assert "kept (already exists)" in r.output
    assert (repo_copy / ".env").read_text() == "DEEPSEEK_API_KEY=mine\n"  # never clobbers keys

    runner.invoke(cli.app, ["init", "--force"])
    assert "DEEPSEEK_API_KEY=mine" not in (repo_copy / ".env").read_text()


def test_init_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 1 and "X-auto-draft-post folder" in r.output


def test_schtasks_arguments(monkeypatch):
    cfg = Config()
    cfg.pipeline.auto_suggest_at = "7:05"
    daily, publish = setup_cmds.jobs(cfg)
    cmd = setup_cmds.task_command(Path("C:/Users/me/X-auto-draft-post/config.yaml"),
                                  Path("C:/Users/me/X-auto-draft-post/data/scheduled.log"), daily,
                                  r"C:\Users\me\X auto\.venv\Scripts\pythonw.exe", windows=True)
    assert cmd.startswith(r'"C:\Users\me\X auto\.venv\Scripts\pythonw.exe" -m xdraft.cli --log-file ')
    assert cmd.endswith("config.yaml") and " run -c " in cmd
    assert setup_cmds.schtasks_create_args(daily, cmd) == [
        "schtasks", "/Create", "/F", "/TN", "xdraft-daily", "/SC", "DAILY", "/ST", "07:05", "/TR", cmd]
    assert publish.windows_schedule == ["/SC", "MINUTE", "/MO", "5"] and publish.args == ["publish-due"]
    assert setup_cmds.schtasks_delete_args(publish) == ["schtasks", "/Delete", "/F", "/TN", "xdraft-publish"]


def test_schedule_install_on_windows_calls_schtasks(repo_copy, monkeypatch):
    runner.invoke(cli.app, ["init"])
    calls = []
    monkeypatch.setattr(setup_cmds, "is_windows", lambda: True)
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""))
    r = runner.invoke(cli.app, ["schedule", "install"])
    assert r.exit_code == 0, r.output
    assert [c[:5] for c in calls] == [["schtasks", "/Create", "/F", "/TN", "xdraft-daily"],
                                      ["schtasks", "/Create", "/F", "/TN", "xdraft-publish"]]
    assert "install xdraft-daily: ok" in r.output and "schedule remove" in r.output


def test_schedule_show_elsewhere_prints_cron(repo_copy):
    runner.invoke(cli.app, ["init"])
    r = runner.invoke(cli.app, ["schedule", "show"])
    assert r.exit_code == 0, r.output
    assert "30 8 * * * " in r.output and "*/5 * * * * " in r.output
    assert "xdraft.cli --log-file" in r.output and "publish-due -c" in r.output


def test_schedule_needs_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(cli.app, ["schedule", "show"])
    assert r.exit_code == 1 and "xdraft init" in r.output


def test_log_file_option(repo_copy):
    runner.invoke(cli.app, ["init"])
    log = repo_copy / "data" / "scheduled.log"
    r = runner.invoke(cli.app, ["--log-file", str(log), "drafts", "--config", str(repo_copy / "config.yaml")])
    assert r.exit_code == 0, r.output
    assert "xdraft drafts" in log.read_text(encoding="utf-8")


def test_long_task_command_uses_wrapper(tmp_path):
    job = setup_cmds.jobs(Config())[0]
    short = "pythonw.exe -m xdraft.cli run"
    assert setup_cmds.fit_task_command(short, job, tmp_path) == short
    long_cmd = "C:\\" + "very-long-folder\\" * 20 + "pythonw.exe -m xdraft.cli run"
    tr = setup_cmds.fit_task_command(long_cmd, job, tmp_path)
    wrapper = tmp_path / "xdraft-daily.cmd"
    assert len(tr) <= setup_cmds.SCHTASKS_TR_MAX and str(wrapper) in tr
    assert long_cmd in wrapper.read_text(encoding="utf-8")
