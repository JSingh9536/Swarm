from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

import swarm.workspace as ws_mod
from swarm.workspace import GitResult, Workspace, git, slugify

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# --------------------------------------------------------------------------- slugify / git()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Add a CLI", "add-a-cli"),
        ("  Hello, World!  ", "hello-world"),
        ("fix: bug #42 (urgent)", "fix-bug-42-urgent"),
        ("Café déjà vu", "caf-d-j-vu"),
        ("", "task"),
        ("!!!", "task"),
        ("日本語", "task"),
    ],
)
def test_slugify(text: str, expected: str) -> None:
    assert slugify(text) == expected


def test_slugify_truncates_without_trailing_dash() -> None:
    assert slugify("aaaa bbbb", max_len=5) == "aaaa"
    assert len(slugify("word " * 50)) <= 40


def test_git_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*a: object, **k: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(ws_mod.subprocess, "run", boom)
    assert git(["status"], tmp_path) == GitResult(127, "git is not installed")
    assert not git(["status"], tmp_path).ok


def test_git_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def slow(*a: object, **k: object) -> None:
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(ws_mod.subprocess, "run", slow)
    assert git(["status"], tmp_path).code == 124


def test_without_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws_mod, "git", lambda args, cwd, timeout=60: GitResult(127, "git is not installed"))
    ws = Workspace(tmp_path / "proj", run_id="r1")
    ws.prepare("do it")
    assert not ws.has_git and ws.base is None
    assert ws.diff_command() == "git diff"
    assert ws.changed_files() == []
    assert ws.diff_stat() == ""
    assert "not a git repository" in ws.commit("m", "b")


# --------------------------------------------------------------------------- artifacts


def test_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(ws_mod, "git", lambda args, cwd, timeout=60: GitResult(127, ""))
    ws = Workspace(tmp_path / "proj", run_id="r1")
    assert ws.run_dir == ws.project_dir / ".swarm" / "runs" / "r1"
    ws.prepare("  Build a thing  \n")
    assert ws.read("task.md") == "Build a thing\n"
    assert (ws.run_dir / "transcripts").is_dir()
    assert ws.read("missing.md") is None
    ws.write("sub/plan.md", "# plan")
    assert ws.read("sub/plan.md") == "# plan"
    ws.log("second")
    lines = ws.read("progress.md").splitlines()
    assert len(lines) == 2
    assert all(re.match(r"^- \d\d:\d\d:\d\d ", ln) for ln in lines)
    assert "run r1 started; git=no" in lines[0]
    assert lines[1].endswith("second")


def test_default_run_id() -> None:
    assert re.fullmatch(r"\d{8}-\d{6}", Workspace(Path(".")).run_id)


# --------------------------------------------------------------------------- real git


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate from the user's git config (identity, hooks, templates)."""
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_DIR"):
        monkeypatch.delenv(var, raising=False)


def run_git(cwd: Path, *args: str) -> str:
    ident = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
    proc = subprocess.run(["git", *ident, *args], cwd=cwd, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def committed_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(path, "init", "-q")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", "init")
    return path


def exclude_lines(project: Path) -> list[str]:
    return (project / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()


@needs_git
@pytest.mark.usefixtures("git_env")
class TestGit:
    def test_fresh_dir_is_initialised(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path / "new" / "proj", run_id="r1")
        ws.prepare("task")
        assert ws.has_git
        assert (ws.project_dir / ".git").is_dir()
        assert ws.base is None
        assert ws.dirty_at_start is False, ".swarm/ must be excluded before the dirty check"
        assert ws.diff_command() == "git diff"
        assert exclude_lines(ws.project_dir).count(".swarm/") == 1

    def test_exclude_written_once(self, tmp_path: Path) -> None:
        project = tmp_path / "proj"
        Workspace(project, run_id="r1").prepare("t")
        Workspace(project, run_id="r2").prepare("t")
        assert exclude_lines(project).count(".swarm/") == 1

    def test_exclude_appends_to_existing(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        exclude = project / ".git" / "info" / "exclude"
        exclude.write_text("*.log", encoding="utf-8")  # no trailing newline
        Workspace(project, run_id="r1").prepare("t")
        assert exclude_lines(project) == ["*.log", *ws_mod.LOCAL_EXCLUDES]
        Workspace(project, run_id="r2").prepare("t")
        assert exclude_lines(project) == ["*.log", *ws_mod.LOCAL_EXCLUDES]

    def test_exclude_does_not_duplicate_existing_entries(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        exclude = project / ".git" / "info" / "exclude"
        exclude.write_text(".venv/\n# comment\n*.pyc\n", encoding="utf-8")
        Workspace(project, run_id="r1").prepare("t")
        lines = exclude_lines(project)
        assert lines[:3] == [".venv/", "# comment", "*.pyc"]
        for entry in ws_mod.LOCAL_EXCLUDES:
            assert lines.count(entry) == 1, entry

    def test_venv_is_not_in_review_diff(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        (ws.project_dir / ".venv" / "Lib").mkdir(parents=True)
        (ws.project_dir / ".venv" / "Lib" / "big.py").write_text("x\n", encoding="utf-8")
        (ws.project_dir / "__pycache__").mkdir()
        (ws.project_dir / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0")
        (ws.project_dir / "real.py").write_text("x\n", encoding="utf-8")
        assert ws.changed_files() == ["real.py"]
        assert ".venv" not in ws.diff_stat()

    def test_base_after_commit_and_clean(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        ws = Workspace(project, run_id="r1")
        ws.prepare("t")
        assert ws.base == run_git(project, "rev-parse", "HEAD")
        assert re.fullmatch(r"[0-9a-f]{40,64}", ws.base)
        assert ws.diff_command() == f"git diff {ws.base}"
        assert ws.dirty_at_start is False

    def test_dirty_at_start(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        (project / "a.txt").write_text("changed\n", encoding="utf-8")
        ws = Workspace(project, run_id="r1")
        ws.prepare("t")
        assert ws.dirty_at_start is True

    def test_nested_in_parent_repo_gets_own_repo(self, tmp_path: Path) -> None:
        parent = committed_repo(tmp_path / "parent")
        project = parent / "sub" / "proj"
        ws = Workspace(project, run_id="r1")
        ws.prepare("t")
        assert (project / ".git").is_dir()
        top = run_git(project, "rev-parse", "--show-toplevel")
        assert Path(top).resolve() == project.resolve()
        assert ws.base is None

    def test_changed_files(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        (project / "b.txt").write_text("b\n", encoding="utf-8")
        run_git(project, "add", "b.txt")
        run_git(project, "commit", "-q", "-m", "b")
        ws = Workspace(project, run_id="r1")
        ws.prepare("t")

        (project / "a.txt").write_text("modified\n", encoding="utf-8")
        (project / "pkg" / "deep").mkdir(parents=True)
        (project / "pkg" / "deep" / "new.py").write_text("x = 1\n", encoding="utf-8")
        run_git(project, "mv", "b.txt", "c.txt")
        ws.write("notes.md", "ignored")

        assert sorted(ws.changed_files()) == ["a.txt", "c.txt", "pkg/deep/new.py"]

    def test_changed_files_with_spaces(self, tmp_path: Path) -> None:
        project = committed_repo(tmp_path / "proj")
        (project / "old name.txt").write_text("o\n", encoding="utf-8")
        run_git(project, "add", "-A")
        run_git(project, "commit", "-q", "-m", "o")
        ws = Workspace(project, run_id="r1")
        ws.prepare("t")

        (project / "my file.txt").write_text("x\n", encoding="utf-8")
        run_git(project, "mv", "old name.txt", "new name.txt")
        assert sorted(ws.changed_files()) == ["my file.txt", "new name.txt"]

    def test_changed_files_non_ascii(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        (ws.project_dir / "café.txt").write_text("x\n", encoding="utf-8")
        assert ws.changed_files() == ["café.txt"]

    def test_diff_stat_shows_new_file(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        (ws.project_dir / "brand_new.py").write_text("print('hi')\n", encoding="utf-8")
        stat = ws.diff_stat()
        assert "brand_new.py" in stat
        assert ".swarm" not in stat

    def test_diff_stat_without_base(self, tmp_path: Path) -> None:
        ws = Workspace(tmp_path / "proj", run_id="r1")
        ws.prepare("t")
        (ws.project_dir / "first.py").write_text("x = 1\n", encoding="utf-8")
        assert "first.py" in ws.diff_stat()

    def test_commit_on_new_branch_with_fallback_identity(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        (ws.project_dir / "feature.py").write_text("x = 1\n", encoding="utf-8")

        assert ws.commit("swarm: add feature", "swarm/feature") == "committed on branch swarm/feature"
        assert run_git(ws.project_dir, "branch", "--show-current") == "swarm/feature"
        assert run_git(ws.project_dir, "log", "-1", "--format=%s|%ae") == "swarm: add feature|swarm@localhost"
        assert "feature.py" in run_git(ws.project_dir, "show", "--name-only", "--format=", "HEAD")
        assert ".swarm" not in run_git(ws.project_dir, "ls-files")

    def test_commit_uses_configured_identity(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        run_git(ws.project_dir, "config", "user.name", "Dev")
        run_git(ws.project_dir, "config", "user.email", "dev@example.com")
        (ws.project_dir / "f.py").write_text("x\n", encoding="utf-8")
        ws.commit("m", "b1")
        assert run_git(ws.project_dir, "log", "-1", "--format=%ae") == "dev@example.com"

    def test_commit_existing_branch_fails_cleanly(self, tmp_path: Path) -> None:
        ws = Workspace(committed_repo(tmp_path / "proj"), run_id="r1")
        ws.prepare("t")
        run_git(ws.project_dir, "branch", "taken")
        assert ws.commit("m", "taken").startswith("could not create branch taken")
