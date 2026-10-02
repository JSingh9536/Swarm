"""Command-line interface for swarm: run, review, audit, company, research, roles, doctor, install-agents, demo."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import platform
import shutil
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from swarm import __version__
from swarm.claude_cli import LOGIN_HELP, METERED_HELP, auth_info, billing_kind, cli_version, find_cli
from swarm.config import Config, ConfigError, load_config
from swarm.roles import Role, RoleError, load_roles, repo_root
from swarm.workspace import slugify

EXIT_OK, EXIT_ATTENTION, EXIT_SETUP, EXIT_BUDGET, EXIT_INTERRUPTED = 0, 1, 2, 3, 130
STATUS_EXIT = {
    "success": EXIT_OK,
    "needs_attention": EXIT_ATTENTION,
    "failed": EXIT_SETUP,
    "budget_exhausted": EXIT_BUDGET,
    "plan_limit": EXIT_BUDGET,
    "aborted": EXIT_INTERRUPTED,
}


def _console():
    from rich.console import Console

    return Console(highlight=False, soft_wrap=True)


def _fix_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


# ---------------------------------------------------------------------------------- shared setup


def _configure(args: argparse.Namespace, project: Path | None) -> Config:
    cfg = load_config(project, args.config)
    if getattr(args, "budget", None) is not None:
        if args.budget <= 0:
            raise ConfigError("--budget must be positive")
        cfg.budget_usd = args.budget
    if getattr(args, "model", None):
        cfg.force_model = args.model
    if getattr(args, "rounds", None) is not None:
        cfg.max_fix_rounds = max(0, args.rounds)
    if getattr(args, "no_research", False):
        cfg.research = False
    if getattr(args, "commit", False):
        cfg.commit = True
    if getattr(args, "allow_api_billing", False):
        cfg.allow_api_billing = True
    return cfg


def _load_roles(cfg: Config) -> dict[str, Role]:
    return load_roles(cfg.roles_dir)


def _pick_project(cfg: Config, task: str, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    base = cfg.workspace_dir
    slug, path, n = slugify(task), None, 1
    while path is None or (path.exists() and any(path.iterdir())):
        path = base / (slug if n == 1 else f"{slug}-{n}")
        n += 1
    return path


def _read_task(args: argparse.Namespace) -> str:
    if args.task_file:
        return Path(args.task_file).read_text(encoding="utf-8").strip()
    return " ".join(args.task).strip()


def _preflight(cfg: Config, console, *, need_auth: bool = True) -> str | None:
    """Return the Claude CLI path (or None to let the SDK find its own); raises SystemExit on a blocker."""
    cli = find_cli(cfg.cli_path)
    if cli is None:
        console.print(
            "[red]Claude Code was not found.[/red] Install it, or set cli_path in swarm.toml / SWARM_CLAUDE_CLI."
        )
        raise SystemExit(EXIT_SETUP)
    if need_auth:
        auth = auth_info(cli)
        if not auth.ok:
            console.print(f"[red]Not logged in.[/red] {auth.detail}\n{LOGIN_HELP}\nThen check with:  swarm doctor")
            raise SystemExit(EXIT_SETUP)
        kind = billing_kind(auth)
        if kind == "metered" and not cfg.allow_api_billing:
            console.print(METERED_HELP.format(method=auth.method), markup=False)
            raise SystemExit(EXIT_SETUP)
        if kind == "unknown":
            console.print(
                f"warning: could not confirm that this login ({auth.method}) counts against your Claude plan.",
                markup=False,
            )
    return str(cli)


def _confirm(console, args: argparse.Namespace, lines: list[str]) -> None:
    for line in lines:
        console.print(line, markup=False)
    if args.yes or getattr(args, "dry_run", False):
        return
    if not sys.stdin.isatty():
        console.print("Not an interactive terminal: re-run with --yes to proceed.", markup=False)
        raise SystemExit(EXIT_SETUP)
    if input("Proceed? [y/N] ").strip().lower() not in ("y", "yes"):
        console.print("Cancelled.")
        raise SystemExit(EXIT_OK)


def _model_line(cfg: Config, roles: dict[str, Role], names: Sequence[str]) -> str:
    return ", ".join(f"{n}={cfg.model_for(roles[n]) or 'default'}" for n in names if n in roles)


def _print_result(console, summary) -> None:
    console.print()
    console.print(f"Status:   {summary.status.upper()}", markup=False, style="bold")
    console.print(f"Project:  {summary.project_dir}", markup=False)
    console.print(f"Report:   {summary.run_dir / 'report.md'}", markup=False)
    console.print(f"Cost:     ~${summary.cost_usd:.2f} (estimated)   Time: {summary.seconds:.0f}s", markup=False)
    if summary.commit_note:
        console.print(f"Git:      {summary.commit_note}", markup=False)
    if summary.changed_files:
        shown = ", ".join(summary.changed_files[:8]) + (" ..." if len(summary.changed_files) > 8 else "")
        console.print(f"Files:    {shown}", markup=False)
    for f in summary.open_findings[:6]:
        console.print(f"  open [{f.severity}] {f.location or '-'}: {f.problem.splitlines()[0][:140]}", markup=False)
    for note in summary.notes[:8]:
        console.print(f"  note: {note}", markup=False)


def _local_backend(args: argparse.Namespace, console):
    """Build the Ollama LocalBackend, or exit with guidance if Ollama is not running."""
    from swarm.local import DEFAULT_MODEL, LocalBackend, OllamaClient, model_license

    client = OllamaClient()
    model = getattr(args, "local_model", None) or getattr(args, "model", None) or DEFAULT_MODEL
    if not client.is_up():
        console.print(
            f"Local mode needs Ollama. It is not reachable at {client.base_url}.\n"
            f"  1. install + start it:  ollama serve\n  2. pull a permissive model:  ollama pull {model}",
            markup=False,
        )
        raise SystemExit(EXIT_SETUP)
    console.print(
        f"Local mode: Ollama model '{model}' ({model_license(model)}) at {client.base_url} — 0 Claude-plan usage.",
        markup=False,
    )
    return LocalBackend(model=model)


def _apply_nice(args: argparse.Namespace, console) -> None:
    """Lower priority so the user can keep working while swarm runs in the background."""
    if getattr(args, "nice", False):
        from swarm.priority import lower_priority

        console.print(f"Background mode: {lower_priority(idle=getattr(args, 'idle', False))}", markup=False)


def _run_async(console, coro):
    try:
        return asyncio.run(coro)
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\nInterrupted. A partial report was written to the run folder under .swarm/runs/.", markup=False)
        raise SystemExit(EXIT_INTERRUPTED) from None


# ---------------------------------------------------------------------------------- commands


def cmd_run(args: argparse.Namespace) -> int:
    from swarm.backend import ClaudeBackend
    from swarm.pipeline import Pipeline
    from swarm.report import ConsoleReporter

    console = _console()
    task = _read_task(args)
    if not task:
        console.print('Give the team a task:  swarm run "build a CLI that ..."  (or --task-file spec.md)', markup=False)
        return EXIT_SETUP
    explicit = args.project
    project = _pick_project(load_config(None, args.config), task, explicit)
    cfg = _configure(args, project)
    roles = _load_roles(cfg)
    if not explicit:
        project = _pick_project(cfg, task, None)

    lines = [
        "The engineering team will run unattended:",
        f"  task:      {task[:200]}",
        f"  project:   {project}" + ("" if project.exists() else "  (new folder)"),
        f"  budget:    up to ${cfg.budget_usd:.2f}, max {cfg.max_fix_rounds} fix round(s), "
        f"research {'on' if cfg.research else 'off'}",
        "  models:    "
        + _model_line(cfg, roles, ["researcher", "architect", "developer", "tester", "reviewer", "security-auditor"]),
        "  guarded:   no git push / outbound shell network / secret access; writes stay inside the project folder",
    ]
    if args.dry_run:
        _confirm(console, args, lines)
        console.print("Dry run: nothing was started.", markup=False)
        return EXIT_OK
    if getattr(args, "local", False):
        backend, cli = _local_backend(args, console), None
    else:
        cli = _preflight(cfg, console)
        backend = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    _confirm(console, args, lines)
    _apply_nice(args, console)

    pipeline = Pipeline(cfg, roles, backend, ConsoleReporter(console, args.verbose), cli_path=cli)
    summary = _run_async(console, pipeline.run(task, project))
    _print_result(console, summary)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_review(args: argparse.Namespace) -> int:
    from swarm.backend import ClaudeBackend
    from swarm.pipeline import Pipeline
    from swarm.report import ConsoleReporter

    console = _console()
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        console.print(f"Project folder not found: {project}", markup=False)
        return EXIT_SETUP
    cfg = _configure(args, project)
    roles = _load_roles(cfg)
    lines = [
        f"Review of {project}" + (f" against {args.base}" if args.base else " (uncommitted changes)"),
        "  reviewer + security-auditor run in parallel (read-only); "
        + (
            f"blocking findings will be fixed (up to {cfg.max_fix_rounds} rounds)."
            if args.fix
            else "no code is changed."
        ),
        f"  budget: up to ${cfg.budget_usd:.2f}",
    ]
    cli = _preflight(cfg, console)
    _confirm(console, args, lines)
    _apply_nice(args, console)
    backend = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    pipeline = Pipeline(cfg, roles, backend, ConsoleReporter(console, args.verbose), cli_path=cli)
    summary = _run_async(console, pipeline.review(project, base=args.base, fix=args.fix))
    _print_result(console, summary)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_audit(args: argparse.Namespace) -> int:
    from swarm.backend import ClaudeBackend
    from swarm.pipeline import Pipeline
    from swarm.report import ConsoleReporter

    console = _console()
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        console.print(f"Project folder not found: {project}", markup=False)
        return EXIT_SETUP
    cfg = _configure(args, project)
    roles = _load_roles(cfg)
    focus = " ".join(args.focus).strip()
    lines = [
        f"Security audit of the whole codebase: {project}",
        f"  focus:  {focus[:200] or '(everything)'}",
        "  the security-auditor reads the code (read-only); "
        + (
            f"blocking findings will be fixed (up to {cfg.max_fix_rounds} rounds)."
            if args.fix
            else "no code is changed."
        ),
        f"  budget: up to ${cfg.budget_usd:.2f}; findings of every severity go to findings.md in the run folder",
    ]
    if args.dry_run:
        _confirm(console, args, lines)
        console.print("Dry run: nothing was started.", markup=False)
        return EXIT_OK
    cli = _preflight(cfg, console)
    _confirm(console, args, lines)
    _apply_nice(args, console)
    backend = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    pipeline = Pipeline(cfg, roles, backend, ConsoleReporter(console, args.verbose), cli_path=cli)
    summary = _run_async(console, pipeline.audit(project, focus, fix=args.fix))
    _print_result(console, summary)
    findings = summary.run_dir / "findings.md"
    if findings.exists():
        console.print(f"Findings: {findings}", markup=False)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_company(args: argparse.Namespace) -> int:
    from swarm.backend import ClaudeBackend
    from swarm.pipeline import Pipeline

    console = _console()
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        console.print(f"Project folder not found: {project}", markup=False)
        return EXIT_SETUP
    roles_dir = Path(args.roles_dir).expanduser().resolve() if args.roles_dir else project / "company" / "agents"
    if not roles_dir.is_dir():
        console.print(
            f"No company role-set at {roles_dir}. Create company/agents/*.md, or pass --roles-dir.", markup=False
        )
        return EXIT_SETUP
    cfg = _configure(args, project)
    try:
        roles = load_roles(roles_dir)
    except RoleError as exc:
        console.print(f"error: {exc}", markup=False)
        return EXIT_SETUP
    focus = " ".join(args.focus).strip()
    lines = [
        f"Company cycle for {project}",
        f"  focus:  {focus[:200] or '(advance the product, protect data + correctness)'}",
        f"  team:   {', '.join(sorted(roles))} (all read-only; memos are drafts, nothing ships)",
        f"  output: company/*.md and company/board.md   budget: up to ${cfg.budget_usd:.2f}",
    ]
    if args.dry_run:
        _confirm(console, args, lines)
        console.print("Dry run: nothing was started.", markup=False)
        return EXIT_OK
    from swarm.report import ConsoleReporter

    if getattr(args, "local", False):
        backend, cli = _local_backend(args, console), None
    else:
        cli = _preflight(cfg, console)
        backend = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    _confirm(console, args, lines)
    _apply_nice(args, console)
    pipeline = Pipeline(cfg, roles, backend, ConsoleReporter(console, args.verbose), cli_path=cli)
    summary = _run_async(console, pipeline.company(project, focus))
    _print_result(console, summary)
    board = summary.project_dir / "company" / "board.md"
    if board.exists():
        console.print(f"Board:    {board}", markup=False)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_research(args: argparse.Namespace) -> int:
    from swarm.backend import ClaudeBackend
    from swarm.pipeline import Pipeline
    from swarm.report import ConsoleReporter

    console = _console()
    question = " ".join(args.question).strip()
    if not question:
        console.print('Ask a question:  swarm research "best way to rate-limit an async API in Python"', markup=False)
        return EXIT_SETUP
    project = Path(args.project).expanduser().resolve() if args.project else None
    cfg = _configure(args, project)
    roles = _load_roles(cfg)
    servers = ", ".join(cfg.mcp) or "none"
    cli = _preflight(cfg, console)
    _confirm(console, args, [
        f"Research: {question[:200]}",
        f"  online sources: web search/fetch + MCP ({servers}); queries leave this machine, so never include secrets.",
        f"  budget: up to ${cfg.budget_usd:.2f}",
    ])  # fmt: skip
    _apply_nice(args, console)
    scratch = cfg.workspace_dir / "_research"
    backend = ClaudeBackend(plan_stop_at=cfg.plan_stop_at)
    pipeline = Pipeline(cfg, roles, backend, ConsoleReporter(console, args.verbose), cli_path=cli)
    summary = _run_async(console, pipeline.research(question, scratch, project))
    if summary.research:
        console.print("\n" + summary.research, markup=False)
        if args.save:
            Path(args.save).write_text(summary.research, encoding="utf-8")
            console.print(f"\nSaved to {args.save}", markup=False)
        else:
            console.print(f"\n(also saved in {summary.run_dir / 'research.md'})", markup=False)
    else:
        _print_result(console, summary)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_roles(args: argparse.Namespace) -> int:
    console = _console()
    cfg = _configure(args, None)
    roles = _load_roles(cfg)
    if args.json:
        data = [
            {
                "name": r.name, "model": cfg.model_for(r), "max_turns": cfg.turns_for(r), "tools": list(r.tools),
                "writes_files": r.can_write, "description": r.description,
            }
            for r in roles.values()
        ]  # fmt: skip
        # plain stdout: rich styles JSON whenever colour is forced (FORCE_COLOR), which breaks whatever parses it
        print(json.dumps(data, indent=2))
        return EXIT_OK
    from rich.table import Table

    table = Table(title=f"Team roles ({len(roles)}) from {next(iter(roles.values())).path.parent}")
    for col in ("Role", "Model", "Turns", "Writes", "Tools"):
        table.add_column(col, overflow="fold")
    for r in roles.values():
        table.add_row(
            r.name,
            cfg.model_for(r) or "default",
            str(cfg.turns_for(r) or "-"),
            "yes" if r.can_write else "no",
            ", ".join(r.tools) or "-",
        )
    console.print(table)
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from swarm.pipeline import Pipeline
    from swarm.report import ConsoleReporter
    from swarm.testing import demo_backend

    console = _console()
    cfg = _configure(args, None)
    cfg.research = True
    roles = _load_roles(cfg)
    project = cfg.workspace_dir / f"demo-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    console.print(
        "DEMO: a scripted team runs the real pipeline (real git, real pytest) - no model calls, no cost, no login.",
        markup=False,
    )
    _apply_nice(args, console)
    pipeline = Pipeline(cfg, roles, demo_backend(project), ConsoleReporter(console, args.verbose))
    summary = _run_async(console, pipeline.run("Demo: a tested slugify helper", project))
    _print_result(console, summary)
    return STATUS_EXIT.get(summary.status, EXIT_SETUP)


def cmd_install(args: argparse.Namespace) -> int:
    console = _console()
    source = repo_root() / ".claude"
    if args.project:
        target = Path(args.project).expanduser().resolve() / ".claude"
        scope = f"project {Path(args.project).expanduser().resolve()}"
    else:
        target = Path.home() / ".claude"
        scope = "your user profile (all projects)"
    plan: list[tuple[Path, Path]] = []
    for path in sorted((source / "agents").glob("*.md")):
        plan.append((path, target / "agents" / path.name))
    for skill in sorted((source / "skills").glob("team-*/SKILL.md")):
        plan.append((skill, target / "skills" / skill.parent.name / "SKILL.md"))
    console.print(f"Installing the team into {scope}: {target}", markup=False)
    changed = 0
    for src, dst in plan:
        rel = dst.relative_to(target)
        if dst.exists() and dst.read_bytes() == src.read_bytes():
            console.print(f"  unchanged  {rel}", markup=False)
        elif dst.exists() and not args.force:
            console.print(f"  SKIP       {rel}  (exists and differs; use --force to overwrite)", markup=False)
        else:
            console.print(f"  {'would copy' if args.dry_run else 'copy      '} {rel}", markup=False)
            if not args.dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            changed += 1
    verb = "would be written" if args.dry_run else "written"
    console.print(f"{changed} file(s) {verb}. Start a new Claude Code session to load them.", markup=False)
    console.print(
        "\nOnline code search (MCP servers) is configured separately. To enable it for every project run:\n"
        "  claude mcp add --transport http --scope user grep https://mcp.grep.app\n"
        "  claude mcp add --transport http --scope user deepwiki https://mcp.deepwiki.com/mcp\n"
        "  claude mcp add --transport http --scope user context7 https://mcp.context7.com/mcp",
        markup=False,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    from swarm import mcp
    from swarm.guard import Policy

    console = _console()
    failures = 0

    def show(level: str, text: str, hint: str = "") -> None:
        nonlocal failures
        failures += level == "FAIL"
        color = {"OK": "green", "WARN": "yellow", "FAIL": "red", "SKIP": "dim"}[level]
        console.print(f"  [{color}]{level.center(4)}[/{color}] {text}")
        if hint:
            for line in hint.splitlines():
                console.print(f"         {line}", markup=False)

    console.print(f"swarm {__version__} doctor", markup=False)
    py = sys.version_info
    show(
        "OK" if py >= (3, 11) else "FAIL",
        f"Python {platform.python_version()}",
        "" if py >= (3, 11) else "Python 3.11+ is required",
    )

    try:
        show("OK", f"claude-agent-sdk {importlib.metadata.version('claude-agent-sdk')}")
    except importlib.metadata.PackageNotFoundError:
        show("FAIL", "claude-agent-sdk is not installed", "run:  pip install -e .   inside the swarm folder")

    try:
        cfg = _configure(args, None)
        show("OK", "configuration is valid")
    except ConfigError as exc:
        show("FAIL", f"configuration: {exc}")
        cfg = Config()

    try:
        roles = _load_roles(cfg)
        show("OK", f"{len(roles)} roles loaded from {next(iter(roles.values())).path.parent}")
    except RoleError as exc:
        show("FAIL", f"roles: {exc}")
        roles = {}

    cli = find_cli(cfg.cli_path)
    if cli is None:
        show(
            "FAIL",
            "Claude Code executable not found",
            "install Claude Code, or set cli_path in swarm.toml / SWARM_CLAUDE_CLI",
        )
    else:
        show("OK", f"Claude Code executable: {cli} ({cli_version(cli) or 'version unknown'})")
    auth = auth_info(cli)
    show("OK" if auth.ok else "FAIL", f"login: {auth.method} - {auth.detail}", "" if auth.ok else LOGIN_HELP)
    kind = billing_kind(auth)
    if kind == "plan":
        show("OK", "billing: usage counts against your Claude plan (no per-token charges)")
    elif kind == "metered":
        allowed = cfg.allow_api_billing
        show(
            "WARN" if allowed else "FAIL",
            f"billing: {auth.method} is billed per token, outside your Claude plan"
            + (" (allowed by allow_api_billing)" if allowed else ""),
            "" if allowed else METERED_HELP.format(method=auth.method),
        )
    elif auth.ok:
        show("WARN", f"billing: cannot confirm that '{auth.method}' counts against your plan")

    git_path = shutil.which("git")
    show(
        "OK" if git_path else "FAIL",
        f"git: {git_path or 'not found'}",
        "" if git_path else "git is needed for diffs and reviews",
    )
    show(
        "OK" if shutil.which("node") else "WARN",
        "node: " + (shutil.which("node") or "not found (only needed for local MCP servers)"),
    )

    enabled = [n for n in cfg.mcp if n in mcp.CATALOG]
    with ThreadPoolExecutor(max_workers=max(1, len(enabled))) as pool:
        for name, (reachable, detail) in zip(
            enabled, pool.map(lambda n: mcp.probe(mcp.CATALOG[n].url, 12), enabled), strict=True
        ):
            show(
                "OK" if reachable else "WARN",
                f"online source {name}: {detail if reachable else 'unreachable - ' + detail}",
            )
    if not enabled:
        show("SKIP", "online sources are disabled ([mcp] enabled = [])")

    probe_dir = Path.cwd()
    policy = Policy(probe_dir)
    self_test = (
        not policy.check_bash("git push origin main").allowed
        and not policy.check_bash("cat ~/.ssh/id_rsa").allowed
        and policy.check_bash("python -m pytest -q").allowed
        and not policy.check("WebFetch", {"url": "http://169.254.169.254/"}).allowed
    )
    show("OK" if self_test else "FAIL", "guard self-test (blocks git push / secrets / metadata URLs; allows pytest)")

    if args.live:
        failures += _doctor_live(console, cfg, cli, show)
    else:
        show("SKIP", "live check (asks the model to say OK, costs about a cent): run  swarm doctor --live")

    console.print()
    if failures:
        console.print(f"{failures} problem(s) found - fix the FAIL lines above.", markup=False, style="bold red")
        return EXIT_SETUP
    console.print('All good. Try:  swarm demo   (free)   or   swarm run "..."', markup=False, style="bold green")
    return EXIT_OK


def _doctor_live(console, cfg: Config, cli: Path | None, show) -> int:
    from swarm.backend import AgentRequest, ClaudeBackend

    role = Role(
        "doctor", "doctor probe", "You are a health probe. Follow instructions exactly.\n", (), None, 1, Path(".")
    )
    request = AgentRequest(
        role=role, label="live check", prompt="Reply with exactly: OK", cwd=Path.cwd(), model="haiku", tools=[],
        allowed_tools=[], mcp_servers={}, read_only=True, max_turns=1, max_budget_usd=0.10, timeout_s=120,
        system_append="", cli_path=str(cli) if cli else None,
    )  # fmt: skip
    if billing_kind(auth_info(cli)) == "metered" and not cfg.allow_api_billing:
        show("SKIP", "live check skipped: this login is billed per token (see the billing line above)")
        return 0
    result = asyncio.run(ClaudeBackend(plan_stop_at=cfg.plan_stop_at).run(request))
    if result.ok:
        answer = result.text.strip()[:20]
        show("OK", f"live check: the engine answered {answer!r} (~${result.cost_usd:.3f}, {result.seconds:.1f}s)")
        return 0
    show("FAIL", f"live check failed: {result.error[:300]}", LOGIN_HELP if result.fatal else "")
    return 1


# ---------------------------------------------------------------------------------- parser


def _latest_run(project: Path) -> Path | None:
    runs = project / ".swarm" / "runs"
    if not runs.is_dir():
        return None
    dirs = [d for d in runs.iterdir() if d.is_dir() and (d / "summary.json").is_file()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def cmd_flow(args: argparse.Namespace) -> int:
    from swarm import flow

    console = _console()
    if args.run:
        run_dir: Path | None = Path(args.run).expanduser().resolve()
    else:
        run_dir = _latest_run(Path(args.project).expanduser().resolve())
    if run_dir is None or not (run_dir / "summary.json").is_file():
        console.print(
            "No run found. Pass --run <.swarm/runs/ID> (it must contain summary.json), or --project with past runs.",
            markup=False,
        )
        return EXIT_SETUP
    out = run_dir / "flow.html"
    out.write_text(flow.flow_html_for_run(run_dir), encoding="utf-8")
    console.print(f"Agent flow-chart: {out}", markup=False)
    console.print("Open it in a browser.", markup=False)
    return EXIT_OK


def cmd_load(args: argparse.Namespace) -> int:
    """Show whether the PC is in use and how local runs (and Claude routing) will behave because of it."""
    from swarm import load

    console = _console()
    for line in load.render(load.current(max_age_s=0.0)):
        console.print(line, markup=False)
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Usage and machine snapshot: Claude-plan tokens, local-model work, power, disk and GPU health."""
    import json as _json

    from swarm import usage

    snap = usage.snapshot(since_days=args.days)
    console = _console()
    if args.json:
        text = _json.dumps(snap, indent=1)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            console.print(f"wrote {args.out}", markup=False)
        else:
            print(text)
        return EXIT_OK
    for line in snap["load"]["lines"]:
        console.print(line, markup=False)
    local, claude = snap["local"], snap["claude"]["total"]
    console.print(
        f"local model: {local['calls']} agent calls, {local['tokens_in'] + local['tokens_out']:,} tokens, "
        f"{local['kwh']} kWh (~${local['electricity_usd']})",
        markup=False,
    )
    console.print(
        f"Claude plan, last {args.days} days: {claude.get('output', 0):,} output + {claude.get('input', 0):,} input "
        f"tokens ({claude.get('subagent_output', 0):,} output from sub-agents)",
        markup=False,
    )
    for disk in snap["machine"]["disks"]:
        console.print(
            f"disk {disk.get('name')}: {disk.get('health')} wear={disk.get('wear_pct')} temp={disk.get('temp_c')}",
            markup=False,
        )
    return EXIT_OK


def cmd_vault(args: argparse.Namespace) -> int:
    """Mirror the swarm's docs and a status note into an Obsidian vault (one-way, its own folder only)."""
    from swarm import usage, vault

    console = _console()
    target = Path(args.vault) if args.vault else vault.find_vault()
    if target is None or not target.is_dir():
        console.print("No Obsidian vault found. Pass --vault PATH or set SWARM_OBSIDIAN_VAULT.", markup=False)
        return EXIT_SETUP
    docs = Path(__file__).resolve().parents[2] / "docs"
    sources = sorted(docs.glob("*.md")) + [Path(p) for p in args.add or []]
    result = vault.sync(target, sources, None if args.no_status else usage.snapshot())
    console.print(f"{result['folder']}: wrote {len(result['written'])}, removed {len(result['removed'])}", markup=False)
    for name in result["skipped"]:
        console.print(f"left alone (your own note, same name): {name}", markup=False)
    return EXIT_OK


def cmd_portfolio(args: argparse.Namespace) -> int:
    """Token use per project (Claude transcripts) and the local model's totals; `--add` records a run."""
    from rich.table import Table

    from swarm import usage

    console = _console()
    if args.add:
        from swarm import portfolio

        summary = Path(args.add).expanduser().resolve()
        if not summary.is_file():
            console.print(f"No summary.json at {summary}", markup=False)
            return EXIT_SETUP
        target = Path(args.file).expanduser().resolve() if args.file else repo_root() / "docs" / "PORTFOLIO.md"
        console.print(f"{target}: {portfolio.append_row(summary, target)}", markup=False)
        return EXIT_OK
    snap = usage.snapshot()

    # Projects summary from Claude transcripts
    claude = snap["claude"]
    projects = claude.get("by_project", [])

    if not projects:
        console.print("No projects found in Claude transcripts.", markup=False)
        return EXIT_OK

    table = Table(title="Swarm Project Portfolio")
    table.add_column("Project", style="cyan")
    table.add_column("Output Tokens", justify="right")
    table.add_column("Input Tokens", justify="right")
    table.add_column("Total", justify="right")

    for p in projects:
        out = p.get("output", 0)
        inp = p.get("input", 0)
        table.add_row(p["project"], f"{out:,}", f"{inp:,}", f"{out + inp:,}")

    console.print(table)

    # Local model summary
    local = snap["local"]
    console.print(
        f"\nLocal Model Totals:\n  Calls: {local['calls']}\n"
        f"  Tokens: {local['tokens_in'] + local['tokens_out']:,}\n  Cost: ~${local['electricity_usd']}",
        markup=False,
    )

    return EXIT_OK


def cmd_local(args: argparse.Namespace) -> int:
    from swarm.local import DEFAULT_MODEL, OllamaClient, model_license

    console = _console()
    client = OllamaClient()
    if not client.is_up():
        console.print(f"Ollama is not reachable at {client.base_url}.", markup=False)
        console.print("Install it from https://ollama.com, then:  ollama serve", markup=False)
        console.print(f"and pull a permissive model:  ollama pull {DEFAULT_MODEL}", markup=False)
        return EXIT_SETUP
    console.print(f"Ollama is up at {client.base_url}", markup=False)
    models = client.models()
    if models:
        console.print("Installed models (license):", markup=False)
        for m in models:
            console.print(f"  {m}  [{model_license(m)}]", markup=False)
    else:
        console.print(f"No models pulled yet. Try:  ollama pull {DEFAULT_MODEL}", markup=False)
    console.print('\nUse it:  swarm run "<task>" --local    (runs on your GPU, 0 Claude-plan usage)', markup=False)
    return EXIT_OK


def cmd_hardware(args: argparse.Namespace) -> int:
    import time as _time

    from swarm import hardware

    console = _console()
    if args.reset_gpu_power:
        console.print(hardware.reset_gpu_power_limit(), markup=False)
    elif args.gpu_power_limit:
        console.print(
            "The GPU power limit is a machine-wide change (every app, until reboot) and needs admin.",
            markup=False,
        )
        if not args.yes:
            if sys.stdin.isatty():
                if input(f"Set GPU power limit to {args.gpu_power_limit}W? [y/N] ").strip().lower() not in ("y", "yes"):
                    console.print("Cancelled.", markup=False)
                    return EXIT_OK
            else:
                console.print("Re-run with --yes to apply this non-interactively.", markup=False)
                return EXIT_SETUP
        console.print(hardware.set_gpu_power_limit(args.gpu_power_limit), markup=False)
    priority = "normal"
    if args.nice:
        from swarm.priority import lower_priority

        note = lower_priority(idle=args.idle)
        priority = "idle" if args.idle else "below-normal"
        console.print(f"priority: {note}", markup=False)

    def show() -> None:
        r = hardware.read(priority=priority)
        for line in hardware.render(r):
            console.print(line, markup=False)
        for n in r.notes:
            console.print(f"  note: {n}", markup=False)

    if args.watch:
        try:
            while True:
                console.print(f"\n--- {_time.strftime('%H:%M:%S')} ---", markup=False)
                show()
                _time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            return EXIT_OK
    show()
    return EXIT_OK


def cmd_autopilot(args: argparse.Namespace) -> int:
    """Work queue.json unattended; see swarm/autopilot.py for the policy."""
    import time as _time

    from swarm import load as load_mod
    from swarm import workqueue
    from swarm.autopilot import State, dry_run_lines, loop, real_runner, step

    console = _console()
    queue_path = Path(args.queue).expanduser().resolve() if args.queue else repo_root() / "queue.json"
    state_path = queue_path.parent / "autopilot-state.json"

    if args.dry_run:
        items = workqueue.load(queue_path)
        state = State.load(state_path)
        for line in dry_run_lines(items, state, load_mod.current(), _time.time(), local_only=args.local_only):
            console.print(line, markup=False)
        return EXIT_OK

    if args.once:
        result = step(
            queue_path, state_path, real_runner, _time.time(), load_mod.current(), local_only=args.local_only
        )
        console.print(str(result), markup=False)
        return EXIT_OK

    results = loop(
        queue_path, state_path, real_runner,
        max_items=args.max_items, max_hours=args.max_hours, local_only=args.local_only,
    )  # fmt: skip
    console.print(f"autopilot: ran {len(results)} item(s)", markup=False)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swarm", description="An AI software engineering team (Claude agents + test gates)."
    )
    parser.add_argument("--version", action="version", version=f"swarm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")

    def common(p: argparse.ArgumentParser, *, run_like: bool = True) -> None:
        p.add_argument(
            "--config", type=Path, help="path to a swarm.toml (default: project folder, then the swarm checkout)"
        )
        p.add_argument("--verbose", "-v", action="store_true", help="show every tool call")
        p.add_argument(
            "--nice",
            action="store_true",
            help="run in the background at low CPU priority so you can keep using your PC",
        )
        p.add_argument(
            "--idle", action="store_true", help="with --nice, use only spare CPU cycles (lowest priority)"
        )
        if run_like:
            p.add_argument("--budget", type=float, help="cost cap in USD for the whole run")
            p.add_argument("--model", help="run every role on this model (sonnet, haiku, opus, ...)")
            p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
            p.add_argument(
                "--allow-api-billing",
                action="store_true",
                help="also accept logins that bill per token (API key, Bedrock, ...); off = stay on your plan",
            )

    p = sub.add_parser("run", help="build or change something with the full team")
    p.add_argument("task", nargs="*", help="what to build or change")
    p.add_argument("--task-file", help="read the task from a file (long specs)")
    p.add_argument("--project", "-p", help="project folder (default: a new folder under workspace/)")
    p.add_argument("--rounds", type=int, help="maximum fix rounds (default from swarm.toml: 3)")
    p.add_argument("--no-research", action="store_true", help="skip the online research step")
    p.add_argument("--commit", action="store_true", help="commit the result to a new branch (never pushes)")
    p.add_argument("--dry-run", action="store_true", help="show what would run, without starting anything")
    p.add_argument("--local", action="store_true", help="run on a local Ollama model instead of the Claude plan")
    p.add_argument("--local-model", help="Ollama model for --local (default: qwen2.5-coder)")
    common(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("review", help="independent code + security review of a project's changes")
    p.add_argument("--project", "-p", default=".", help="project folder (default: current folder)")
    p.add_argument("--base", help="git ref to diff against (default: uncommitted changes vs HEAD)")
    p.add_argument("--fix", action="store_true", help="let the developer fix blocking findings")
    p.add_argument("--rounds", type=int, help="maximum fix rounds")
    common(p)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("audit", help="security audit of a whole codebase (not just the changes); --fix to repair")
    p.add_argument("focus", nargs="*", help="what to examine most closely (default: everything)")
    p.add_argument("--project", "-p", default=".", help="project folder (default: current folder)")
    p.add_argument("--fix", action="store_true", help="let the developer fix blocking findings")
    p.add_argument("--rounds", type=int, help="maximum fix rounds")
    p.add_argument("--commit", action="store_true", help="commit the fixes to a new branch when clean (never pushes)")
    p.add_argument("--dry-run", action="store_true", help="show what would run, without starting anything")
    common(p)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("company", help="run one AI-company cycle (CEO, product, security, legal, finance, ...)")
    p.add_argument("focus", nargs="*", help="what this cycle should focus on")
    p.add_argument("--project", "-p", default=".", help="project folder (default: current folder)")
    p.add_argument("--roles-dir", help="company role-set (default: <project>/company/agents)")
    p.add_argument("--dry-run", action="store_true", help="show what would run, without starting anything")
    p.add_argument("--local", action="store_true", help="run on a local Ollama model instead of the Claude plan")
    p.add_argument("--local-model", help="Ollama model for --local (default: qwen2.5-coder)")
    common(p)
    p.set_defaults(func=cmd_company)

    p = sub.add_parser("research", help="look up libraries, docs and real code examples online")
    p.add_argument("question", nargs="*")
    p.add_argument("--project", "-p", help="let the researcher read this project's manifests for context")
    p.add_argument("--save", help="write the brief to this file")
    common(p)
    p.set_defaults(func=cmd_research)

    p = sub.add_parser("roles", help="list the team's roles, models and tools")
    p.add_argument("--json", action="store_true")
    common(p, run_like=False)
    p.set_defaults(func=cmd_roles)

    p = sub.add_parser("doctor", help="check the setup (login, tools, online sources, guard)")
    p.add_argument("--live", action="store_true", help="also make one tiny real model call")
    common(p, run_like=False)
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("demo", help="run a scripted team through the real pipeline: free, offline, no login")
    common(p, run_like=False)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("hardware", help="live CPU / GPU / power readout and power controls for local runs")
    p.add_argument("--watch", action="store_true", help="refresh continuously until Ctrl-C")
    p.add_argument("--interval", type=float, default=2.0, help="seconds between refreshes with --watch")
    p.add_argument("--gpu-power-limit", type=int, metavar="W", help="set the NVIDIA GPU power cap, watts (needs admin)")
    p.add_argument("--reset-gpu-power", action="store_true", help="restore the GPU's factory-default power limit")
    p.add_argument("--nice", action="store_true", help="also lower this process's CPU priority")
    p.add_argument("--idle", action="store_true", help="with --nice, use the lowest priority")
    p.add_argument("--yes", "-y", action="store_true", help="apply a machine-wide power change without prompting")
    p.set_defaults(func=cmd_hardware)

    p = sub.add_parser("load", help="is the PC in use (game, call, busy GPU) and how local runs will adapt")
    p.set_defaults(func=cmd_load)

    p = sub.add_parser("status", help="usage snapshot: Claude tokens, local-model work, power, GPU and disk health")
    p.add_argument("--json", action="store_true", help="print the full snapshot as JSON (for the dashboard)")
    p.add_argument("--out", help="with --json, write to this file instead of printing")
    p.add_argument("--days", type=int, default=14, help="how many days of Claude usage to include (default 14)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("vault", help="mirror the swarm's docs and status into an Obsidian vault (its own folder only)")
    p.add_argument("--vault", help="vault folder (default: the vault Obsidian has open)")
    p.add_argument("--add", action="append", metavar="FILE", help="extra Markdown file to mirror (repeatable)")
    p.add_argument("--no-status", action="store_true", help="skip the live status note")
    p.set_defaults(func=cmd_vault)

    p = sub.add_parser("portfolio", help="summarize projects and costs across the swarm; --add records a run")
    p.add_argument("--add", metavar="SUMMARY_JSON", help="append this run's row to docs/PORTFOLIO.md")
    p.add_argument("--file", help="portfolio file for --add (default: docs/PORTFOLIO.md in the swarm checkout)")
    p.set_defaults(func=cmd_portfolio)


    p = sub.add_parser("local", help="check the local Ollama backend used by `swarm run --local`")
    common(p, run_like=False)
    p.set_defaults(func=cmd_local)

    p = sub.add_parser("flow", help="generate a visual agent flow-chart webpage from a run")
    p.add_argument("--run", help="a .swarm/runs/<id> folder (default: the latest run under --project)")
    p.add_argument("--project", "-p", default=".", help="where to find the latest run (default: current folder)")
    p.set_defaults(func=cmd_flow)

    p = sub.add_parser(
        "autopilot",
        help="work queue.json unattended: local model for small jobs, Claude for the rest, "
        "paused and resumed around the Claude plan limit",
    )
    p.add_argument("--queue", help="queue.json path (default: queue.json at the repo root)")
    p.add_argument("--once", action="store_true", help="run a single queue item instead of looping")
    p.add_argument("--max-items", type=int, help="stop after running this many items")
    p.add_argument("--max-hours", type=float, help="stop after this many hours")
    p.add_argument(
        "--local-only", action="store_true",
        help="spend no Claude-plan tokens: run what the local model can do, leave the rest queued, then stop",
    )  # fmt: skip
    p.add_argument(
        "--dry-run", action="store_true",
        help="show which backend each queued item would use right now, and why; start nothing",
    )  # fmt: skip
    p.set_defaults(func=cmd_autopilot)

    p = sub.add_parser("install-agents", help="copy the team into Claude Code (~/.claude or a project's .claude)")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--user", action="store_true", help="install for every project (default)")
    scope.add_argument("--project", help="install into this project's .claude folder")
    p.add_argument("--force", action="store_true", help="overwrite files that differ")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _fix_stdout()
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, RoleError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_SETUP
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
