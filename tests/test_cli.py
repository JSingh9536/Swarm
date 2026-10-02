"""CLI behaviour: argument handling, safety preflight, install-agents, demo. Never starts a real model call."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import swarm.cli as cli
import swarm.config as config_mod
import swarm.pipeline as pipeline_mod
from swarm.claude_cli import AuthInfo
from swarm.report import RunSummary
from swarm.roles import repo_root

PLAN_LOGIN = AuthInfo(True, "claude.ai", "logged in")
API_KEY = AuthInfo(True, "ANTHROPIC_API_KEY", "key")


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """No real Claude CLI, no real login check, no real pipeline, workspace under tmp."""
    state: dict[str, Any] = {"auth": PLAN_LOGIN, "pipelines": []}
    fake_cli = tmp_path / "claude.exe"
    fake_cli.write_text("", encoding="utf-8")
    monkeypatch.setattr(cli, "find_cli", lambda explicit=None: fake_cli)
    monkeypatch.setattr(cli, "auth_info", lambda c: state["auth"])
    monkeypatch.setattr(cli, "cli_version", lambda c: "9.9.9")
    monkeypatch.setattr(config_mod, "repo_root", lambda: tmp_path / "checkout")
    monkeypatch.delenv("SWARM_ROLES_DIR", raising=False)

    class FakePipeline:
        def __init__(self, cfg: Any, roles: Any, backend: Any, reporter: Any = None, **kw: Any) -> None:
            state["pipelines"].append({"cfg": cfg, "backend": backend, "kw": kw})

        async def run(self, task: str, project: Path, run_id: str | None = None) -> RunSummary:
            state["task"] = task
            return RunSummary("success", task, project, project / ".swarm" / "runs" / "x")

        async def review(self, project: Path, *, base: str | None = None, fix: bool = False) -> RunSummary:
            state["review"] = (project, base, fix)
            return RunSummary("needs_attention", "review", project, project)

        async def research(self, question: str, scratch: Path, project: Path | None = None) -> RunSummary:
            s = RunSummary("success", question, scratch, scratch)
            s.research = "BRIEF TEXT"
            return s

    state["FakePipeline"] = FakePipeline
    return state


@pytest.fixture
def fake_pipeline(offline: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(pipeline_mod, "Pipeline", offline["FakePipeline"])
    return offline


def main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str]:
    try:
        code = cli.main(list(argv))
    except SystemExit as exc:  # preflight/confirm exit this way; the process exit code is what users see
        code = int(exc.code or 0)
    out = capsys.readouterr()
    return code, out.out + out.err


# --------------------------------------------------------------------------- basics


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "swarm " in capsys.readouterr().out


def test_command_required() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_status_exit_codes() -> None:
    assert cli.STATUS_EXIT == {
        "success": 0, "needs_attention": 1, "failed": 2, "budget_exhausted": 3, "plan_limit": 3, "aborted": 130,
    }  # fmt: skip


def test_roles_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "roles", "--json")
    assert code == 0
    data = json.loads(out)
    names = {r["name"] for r in data}
    assert {"architect", "developer", "reviewer", "researcher"} <= names
    reviewer = next(r for r in data if r["name"] == "reviewer")
    assert reviewer["writes_files"] is False and "model" in reviewer
    by_name = {r["name"]: r for r in data}
    # an unpinned role (model: inherit) resolves to the safe default, never the account default
    assert by_name["tech-lead"]["model"] == "sonnet"
    assert by_name["docs-writer"]["model"] == "haiku"


def test_roles_table(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "roles")
    assert code == 0 and "developer" in out


def test_bad_config_is_a_setup_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[run]\nbudget_usd = -1\n", encoding="utf-8")
    code, out = main(capsys, "roles", "--config", str(bad))
    assert code == 2 and "error:" in out and "budget_usd" in out


# --------------------------------------------------------------------------- run


def test_run_without_task(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "run")
    assert code == 2 and "Give the team a task" in out


def test_run_dry_run_starts_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    monkeypatch.setattr(cli, "find_cli", lambda explicit=None: pytest.fail("dry run must not look for Claude"))
    project = tmp_path / "proj"
    code, out = main(capsys, "run", "build", "a", "thing", "--project", str(project), "--dry-run", "--budget", "1.5")
    assert code == 0
    assert "Dry run: nothing was started." in out
    assert "task:      build a thing" in out and "up to $1.50" in out and "(new folder)" in out
    assert fake_pipeline["pipelines"] == []
    assert not project.exists()


def test_run_dry_run_default_project_under_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_pipeline: dict
) -> None:
    code, out = main(capsys, "run", "Make a Todo App!", "--dry-run")
    assert code == 0
    assert str(tmp_path / "checkout" / "workspace" / "make-a-todo-app") in out


def test_run_metered_login_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict, fake_pipeline: dict
) -> None:
    offline["auth"] = API_KEY
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes")
    assert code == 2
    assert "billed per token" in out and "ANTHROPIC_API_KEY" in out
    assert fake_pipeline["pipelines"] == [], "nothing may start on a metered login"


def test_run_metered_login_allowed_explicitly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict, fake_pipeline: dict
) -> None:
    offline["auth"] = API_KEY
    code, _ = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes", "--allow-api-billing")
    assert code == 0 and len(fake_pipeline["pipelines"]) == 1


def test_run_unknown_billing_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict, fake_pipeline: dict
) -> None:
    offline["auth"] = AuthInfo(True, "mystery", "")
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes")
    assert code == 0 and "could not confirm" in out


def test_run_not_logged_in(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict, fake_pipeline: dict
) -> None:
    offline["auth"] = AuthInfo(False, "none", "not logged in")
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes")
    assert code == 2 and "Not logged in" in out and "claude auth login" in out
    assert fake_pipeline["pipelines"] == []


def test_run_cli_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    monkeypatch.setattr(cli, "find_cli", lambda explicit=None: None)
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes")
    assert code == 2 and "Claude Code was not found" in out


def test_run_needs_yes_when_not_interactive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"))
    assert code == 2 and "--yes" in out
    assert fake_pipeline["pipelines"] == []


def test_run_passes_options(tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_pipeline: dict) -> None:
    code, _ = main(
        capsys, "run", "do", "it", "--project", str(tmp_path / "p"), "--yes", "--budget", "2", "--model", "haiku",
        "--rounds", "1", "--no-research", "--commit",
    )  # fmt: skip
    assert code == 0
    cfg = fake_pipeline["pipelines"][0]["cfg"]
    got = (cfg.budget_usd, cfg.force_model, cfg.max_fix_rounds, cfg.research, cfg.commit)
    assert got == (2, "haiku", 1, False, True)
    assert fake_pipeline["task"] == "do it"
    assert fake_pipeline["pipelines"][0]["kw"]["cli_path"].endswith("claude.exe")


def test_run_nice_lowers_priority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    called: list[bool] = []
    monkeypatch.setattr("swarm.priority.lower_priority", lambda idle=False: called.append(idle) or "test-priority")
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes", "--nice")
    assert code == 0 and "Background mode: test-priority" in out
    assert called == [False]


def test_run_idle_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    seen: list[bool] = []
    monkeypatch.setattr("swarm.priority.lower_priority", lambda idle=False: seen.append(idle) or "idle")
    main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes", "--nice", "--idle")
    assert seen == [True]


def test_run_without_nice_keeps_normal_priority(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, fake_pipeline: dict
) -> None:
    monkeypatch.setattr("swarm.priority.lower_priority", lambda idle=False: pytest.fail("must not lower priority"))
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--yes")
    assert code == 0 and "Background mode" not in out


def test_run_task_file(tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_pipeline: dict) -> None:
    spec = tmp_path / "spec.md"
    spec.write_text("  Build from spec\n", encoding="utf-8")
    code, _ = main(capsys, "run", "--task-file", str(spec), "--project", str(tmp_path / "p"), "--yes")
    assert code == 0 and fake_pipeline["task"] == "Build from spec"


def test_run_bad_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "run", "x", "--project", str(tmp_path / "p"), "--budget", "0", "--dry-run")
    assert code == 2 and "--budget must be positive" in out


# --------------------------------------------------------------------------- review / research


def test_review_missing_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "review", "--project", str(tmp_path / "nope"), "--yes")
    assert code == 2 and "not found" in out


def test_review_exit_code_and_args(tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_pipeline: dict) -> None:
    code, _ = main(capsys, "review", "--project", str(tmp_path), "--base", "main", "--fix", "--yes")
    assert code == 1
    assert fake_pipeline["review"] == (tmp_path.resolve(), "main", True)


def test_review_metered_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict) -> None:
    offline["auth"] = API_KEY
    assert main(capsys, "review", "--project", str(tmp_path), "--yes")[0] == 2


def test_audit_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch, fake_pipeline: dict) -> None:
    monkeypatch.setattr(cli, "find_cli", lambda explicit=None: pytest.fail("dry run must not look for Claude"))
    code, out = main(capsys, "audit", "RLS", "policies", "--project", str(tmp_path), "--dry-run")
    assert code == 0 and "focus:  RLS policies" in out and "no code is changed" in out
    assert fake_pipeline["pipelines"] == []


def test_audit_metered_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str], offline: dict) -> None:
    offline["auth"] = API_KEY
    assert main(capsys, "audit", "--project", str(tmp_path), "--yes")[0] == 2


def test_audit_missing_folder(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(capsys, "audit", "--project", str(tmp_path / "nope"), "--yes")[0] == 2


def test_research_prints_and_saves(tmp_path: Path, capsys: pytest.CaptureFixture[str], fake_pipeline: dict) -> None:
    save = tmp_path / "brief.md"
    code, out = main(capsys, "research", "best", "http", "client", "--yes", "--save", str(save))
    assert code == 0 and "BRIEF TEXT" in out
    assert save.read_text(encoding="utf-8") == "BRIEF TEXT"


def test_research_needs_question(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(capsys, "research")[0] == 2


def test_research_metered_refused(capsys: pytest.CaptureFixture[str], offline: dict) -> None:
    offline["auth"] = API_KEY
    assert main(capsys, "research", "q", "--yes")[0] == 2


# --------------------------------------------------------------------------- install-agents


def expected_files() -> list[str]:
    source = repo_root() / ".claude"
    agents = [f"agents/{p.name}" for p in (source / "agents").glob("*.md")]
    skills = [f"skills/{p.parent.name}/SKILL.md" for p in (source / "skills").glob("team-*/SKILL.md")]
    return sorted(agents + skills)


def installed(target: Path) -> list[str]:
    return sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())


def test_install_dry_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "install-agents", "--project", str(tmp_path), "--dry-run")
    assert code == 0 and "would copy" in out and "would be written" in out
    assert not (tmp_path / ".claude").exists()


def test_install_copy_then_unchanged_skip_force(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target = tmp_path / ".claude"
    code, out = main(capsys, "install-agents", "--project", str(tmp_path))
    assert code == 0 and installed(target) == expected_files()
    assert f"{len(expected_files())} file(s) written" in out

    _, out = main(capsys, "install-agents", "--project", str(tmp_path))
    assert "0 file(s) written" in out and "unchanged" in out

    edited = target / "agents" / "developer.md"
    edited.write_text("my local edit\n", encoding="utf-8")
    _, out = main(capsys, "install-agents", "--project", str(tmp_path))
    assert "SKIP" in out and edited.read_text(encoding="utf-8") == "my local edit\n"

    _, out = main(capsys, "install-agents", "--project", str(tmp_path), "--force")
    assert "1 file(s) written" in out
    assert edited.read_bytes() == (repo_root() / ".claude" / "agents" / "developer.md").read_bytes()


def test_install_never_copies_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(capsys, "install-agents", "--project", str(tmp_path))
    files = installed(tmp_path / ".claude")
    assert not any("settings" in f for f in files)


def test_install_user_scope_uses_home(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))
    code, _ = main(capsys, "install-agents", "--dry-run")
    assert code == 0 and not home.exists()


# --------------------------------------------------------------------------- doctor


@pytest.fixture
def doctor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import swarm.mcp as mcp

    monkeypatch.setattr(mcp, "probe", lambda url, timeout=15: (True, "srv 1.0"))
    monkeypatch.setattr(cli, "_doctor_live", lambda *a: pytest.fail("the live check must not run in tests"))


@pytest.mark.usefixtures("doctor_env")
def test_doctor_plan_login(capsys: pytest.CaptureFixture[str]) -> None:
    code, out = main(capsys, "doctor")
    assert "billing: usage counts against your Claude plan" in out
    assert "guard self-test" in out and "live check" in out
    if shutil.which("git"):
        assert code == 0, out


@pytest.mark.usefixtures("doctor_env")
def test_doctor_metered_login_fails(capsys: pytest.CaptureFixture[str], offline: dict) -> None:
    offline["auth"] = API_KEY
    code, out = main(capsys, "doctor")
    assert code == 2 and "billed per token" in out


def test_doctor_live_skipped_on_metered(capsys: pytest.CaptureFixture[str], offline: dict, monkeypatch) -> None:
    import swarm.backend as backend_mod
    import swarm.mcp as mcp

    monkeypatch.setattr(mcp, "probe", lambda url, timeout=15: (True, "srv"))
    monkeypatch.setattr(backend_mod.ClaudeBackend, "run", lambda *a, **k: pytest.fail("no model call on metered"))
    offline["auth"] = API_KEY
    _, out = main(capsys, "doctor", "--live")
    assert "live check skipped" in out


# --------------------------------------------------------------------------- demo (real pipeline, scripted team)


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_demo_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    empty = tmp_path / "gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    code, out = main(capsys, "demo")
    assert code == 0, out
    assert "Status:   SUCCESS" in out
    demos = list((tmp_path / "checkout" / "workspace").glob("demo-*"))
    assert len(demos) == 1 and (demos[0] / "slugify.py").is_file()


def test_company_accepts_local_flags() -> None:
    args = cli.build_parser().parse_args(["company", "--local", "--local-model", "qwen2.5-coder", "-p", "."])
    assert args.local is True and args.local_model == "qwen2.5-coder"
    assert cli.build_parser().parse_args(["company"]).local is False
