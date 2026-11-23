"""Cross-platform setup helpers: `xdraft init` and `xdraft schedule` (no shell-specific commands)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from xdraft.config import Config

TEMPLATES = (
    ("config.example.yaml", "config.yaml"),
    (".env.example", ".env"),
    ("style.example.md", "style.md"),
)
FOLDERS = ("notes/inbox", "data")
TASK_PREFIX = "xdraft"
SCHTASKS_TR_MAX = 261  # Task Scheduler's limit for the /TR command line


def init_project(repo: Path, force: bool = False) -> list[tuple[str, str]]:
    """Copy the example files into place and create folders. Returns (path, what happened) pairs."""
    report = []
    for src_name, dst_name in TEMPLATES:
        src, dst = repo / src_name, repo / dst_name
        if not src.is_file():
            report.append((dst_name, f"skipped: {src_name} not found"))
        elif dst.exists() and not force:
            report.append((dst_name, "kept (already exists)"))
        else:
            shutil.copyfile(src, dst)
            report.append((dst_name, "created" if not force else "overwritten"))
    for folder in FOLDERS:
        path = repo / folder
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        report.append((folder + "/", "kept" if existed else "created"))
    return report


@dataclass
class Job:
    name: str
    description: str
    args: list[str]          # xdraft arguments, e.g. ["run"]
    windows_schedule: list[str]  # schtasks /SC … options
    cron: str                # crontab time fields


def jobs(cfg: Config) -> list[Job]:
    hh, mm = (cfg.pipeline.auto_suggest_at or "08:30").split(":")
    return [
        Job(f"{TASK_PREFIX}-daily", "collect + suggest topics + publish due posts", ["run"],
            ["/SC", "DAILY", "/ST", f"{int(hh):02d}:{int(mm):02d}"], f"{int(mm)} {int(hh)} * * *"),
        Job(f"{TASK_PREFIX}-publish", "publish approved posts that are due", ["publish-due"],
            ["/SC", "MINUTE", "/MO", "5"], "*/5 * * * *"),
    ]


def is_windows() -> bool:
    return os.name == "nt"


def windowless_python() -> str:
    """On Windows, pythonw.exe next to the venv's python runs without flashing a console window."""
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return str(pythonw if pythonw.exists() else exe)


def task_command(cfg_path: Path, log_path: Path, job: Job, python: str, windows: bool | None = None) -> str:
    """The command line a scheduled task runs. Absolute paths: Task Scheduler and cron have no PATH or cwd."""
    parts = [python, "-m", "xdraft.cli", "--log-file", str(log_path), *job.args, "-c", str(cfg_path)]
    windows = is_windows() if windows is None else windows
    return subprocess.list2cmdline(parts) if windows else " ".join(_sh_quote(p) for p in parts)


def _sh_quote(s: str) -> str:
    import shlex

    return shlex.quote(s)


def fit_task_command(command: str, job: Job, data_dir: Path) -> str:
    """Long paths can push /TR past 261 characters; then run a small .cmd wrapper instead."""
    if len(command) <= SCHTASKS_TR_MAX:
        return command
    data_dir.mkdir(parents=True, exist_ok=True)
    wrapper = data_dir / f"{job.name}.cmd"
    wrapper.write_text(f"@echo off\r\n{command}\r\n", encoding="utf-8")
    return subprocess.list2cmdline([str(wrapper)])


def schtasks_create_args(job: Job, command: str) -> list[str]:
    return ["schtasks", "/Create", "/F", "/TN", job.name, *job.windows_schedule, "/TR", command]


def schtasks_delete_args(job: Job) -> list[str]:
    return ["schtasks", "/Delete", "/F", "/TN", job.name]


def cron_lines(cfg: Config, cfg_path: Path, log_path: Path, python: str) -> list[str]:
    return [f"{j.cron} {task_command(cfg_path, log_path, j, python, windows=False)}" for j in jobs(cfg)]
