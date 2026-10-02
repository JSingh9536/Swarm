"""Per-run working area (`.swarm/`) and the git plumbing around it."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

LOCAL_EXCLUDES = (
    ".swarm/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".mypy_cache/",
    "*.pyc",
)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "task"


@dataclass(frozen=True)
class GitResult:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


def git(args: list[str], cwd: Path, timeout: float = 60.0) -> GitResult:
    """Run git; never raises (a missing git is reported as exit code 127)."""
    try:
        proc = subprocess.run(  # noqa: S603, S607
            ["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, stdin=subprocess.DEVNULL,
        )  # fmt: skip
    except FileNotFoundError:
        return GitResult(127, "git is not installed")
    except subprocess.TimeoutExpired:
        return GitResult(124, "git timed out")
    # rstrip only: leading whitespace is significant in `git status --porcelain` output
    return GitResult(proc.returncode, (proc.stdout + proc.stderr).rstrip())


class Workspace:
    """The project directory plus this run's artifact folder under `.swarm/runs/<id>/`."""

    def __init__(self, project_dir: Path, run_id: str | None = None) -> None:
        self.project_dir = project_dir.resolve()
        self.run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self.swarm_dir = self.project_dir / ".swarm"
        self.run_dir = self.swarm_dir / "runs" / self.run_id
        self.has_git = False
        self.base: str | None = None
        self.dirty_at_start = False

    # ---- setup

    def prepare(self, task: str, use_git: bool = True) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "transcripts").mkdir(parents=True, exist_ok=True)
        self.write("task.md", task.strip() + "\n")
        if use_git:
            self._setup_git()
        self.log(f"run {self.run_id} started; git={'yes' if self.has_git else 'no'}; base={self.base or 'none'}")

    def _setup_git(self) -> None:
        top = git(["rev-parse", "--show-toplevel"], self.project_dir)
        owns_repo = top.ok and os.path.normcase(os.path.abspath(top.out)) == os.path.normcase(str(self.project_dir))
        # not a repo, or only a subdirectory of some parent repo: give the project its own
        if not owns_repo and not git(["init", "-q"], self.project_dir).ok:
            return
        self.has_git = True
        self._exclude_swarm_dir()
        head = git(["rev-parse", "--verify", "-q", "HEAD"], self.project_dir)
        self.base = head.out if head.ok else None
        self.dirty_at_start = bool(git(["status", "--porcelain"], self.project_dir).out)

    def _exclude_swarm_dir(self) -> None:
        """Keep run artifacts and dependency/cache folders out of `git status` and `git diff` (local-only ignore)."""
        info = git(["rev-parse", "--git-path", "info/exclude"], self.project_dir)
        if not info.ok:
            return
        path = Path(info.out)
        path = path if path.is_absolute() else self.project_dir / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text(encoding="utf-8") if path.exists() else ""
            present = set(existing.splitlines())
            missing = [entry for entry in LOCAL_EXCLUDES if entry not in present]
            if missing:
                path.write_text(
                    existing.rstrip("\n") + ("\n" if existing else "") + "\n".join(missing) + "\n", encoding="utf-8"
                )
        except OSError:
            pass

    # ---- artifacts

    def path(self, name: str) -> Path:
        return self.run_dir / name

    def write(self, name: str, text: str) -> Path:
        target = self.path(name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return target

    def read(self, name: str) -> str | None:
        target = self.path(name)
        return target.read_text(encoding="utf-8") if target.exists() else None

    def log(self, message: str) -> None:
        line = f"- {datetime.now().strftime('%H:%M:%S')} {message}\n"
        with self.path("progress.md").open("a", encoding="utf-8") as fh:
            fh.write(line)

    def event(self, kind: str, **fields: object) -> None:
        """Append one live-activity record to `events.jsonl` (read by the dashboard); never raises."""
        record = {"t": round(datetime.now().timestamp(), 3), "kind": kind, **fields}
        try:
            with self.path("events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, ValueError):
            pass

    # ---- git views

    def intent_to_add(self) -> None:
        """Mark untracked files so that `git diff` shows them as additions."""
        if self.has_git:
            git(["add", "-N", "."], self.project_dir)

    def diff_command(self) -> str:
        return f"git diff {self.base}" if self.base else "git diff"

    def changed_files(self) -> list[str]:
        if not self.has_git:
            return []
        # -z: NUL-separated, never quoted or escaped; renames/copies are `XY new\0old\0`
        out = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], self.project_dir).out
        fields = out.split("\0")
        files: list[str] = []
        i = 0
        while i < len(fields):
            entry = fields[i]
            i += 1
            if len(entry) < 4:
                continue
            status, path = entry[:2], entry[3:]
            if "R" in status or "C" in status:
                i += 1  # skip the original path
            if path and not path.startswith(".swarm/"):
                files.append(path)
        return files

    def diff_stat(self) -> str:
        if not self.has_git:
            return ""
        self.intent_to_add()
        return git(["diff", "--stat", *([self.base] if self.base else [])], self.project_dir).out

    def commit(self, message: str, branch: str) -> str:
        """Commit everything on a new branch; returns a human-readable outcome."""
        if not self.has_git:
            return "not a git repository; nothing committed"
        created = git(["checkout", "-b", branch], self.project_dir)
        if not created.ok:
            return f"could not create branch {branch}: {created.out}"
        git(["add", "-A"], self.project_dir)
        identity: list[str] = []
        if not git(["config", "user.email"], self.project_dir).out:
            identity = ["-c", "user.name=swarm", "-c", "user.email=swarm@localhost"]
        result = git([*identity, "commit", "-q", "-m", message], self.project_dir)
        if not result.ok:
            return f"commit failed on {branch}: {result.out}"
        return f"committed on branch {branch}"
