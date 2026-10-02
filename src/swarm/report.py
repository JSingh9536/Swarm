"""Run summary, Markdown report, and console progress output."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from swarm.gates import GateResult
from swarm.models import Finding, Plan


@dataclass
class CallRecord:
    role: str
    label: str
    phase: str
    model: str | None
    ok: bool
    cost_usd: float
    turns: int
    seconds: float
    error: str = ""
    denied: int = 0


STATUS_TEXT = {
    "success": "SUCCESS - implemented, tests green, review clean",
    "needs_attention": "NEEDS ATTENTION - work exists but something below still needs a human",
    "failed": "FAILED - the run could not complete",
    "budget_exhausted": "STOPPED - the cost budget ran out",
    "plan_limit": "STOPPED - your Claude plan limit is (nearly) reached; swarm stops rather than use paid usage",
    "aborted": "ABORTED - interrupted before completion",
}


@dataclass
class RunSummary:
    status: str
    task: str
    project_dir: Path
    run_dir: Path
    plan: Plan | None = None
    calls: list[CallRecord] = field(default_factory=list)
    open_findings: list[Finding] = field(default_factory=list)
    gate: GateResult | None = None
    changed_files: list[str] = field(default_factory=list)
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)
    commit_note: str = ""
    research: str | None = None
    plan_resets_at: float | None = None  # status plan_limit only: Unix time the plan window resets (None = unknown)

    @property
    def cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)


def _fmt_time(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_report(s: RunSummary) -> str:
    title = s.plan.title if s.plan else s.task.strip().splitlines()[0][:70]
    lines = [
        f"# Run report: {title}",
        "",
        f"**Status:** {STATUS_TEXT.get(s.status, s.status)}",
        f"**Task:** {s.task.strip()}",
        f"**Project:** `{s.project_dir}`",
        f"**Time:** {_fmt_time(s.seconds)} | **Estimated cost:** ${s.cost_usd:.2f} | **Agent calls:** {len(s.calls)}",
    ]
    if s.plan:
        lines += ["", "## What was planned", s.plan.goal, ""]
        lines += [f"- {ac.id}: {ac.text}" for ac in s.plan.acceptance_criteria]
        if s.plan.run_instructions:
            lines += ["", "## How to run", s.plan.run_instructions]
    if s.changed_files:
        lines += ["", "## Files changed", *[f"- `{f}`" for f in s.changed_files[:80]]]
        if len(s.changed_files) > 80:
            lines.append(f"- ... and {len(s.changed_files) - 80} more")
    lines += ["", "## Verification (final gate, run by the pipeline itself)"]
    if s.gate is None or not s.gate.results:
        lines.append("- No automated test or lint command could be found or run.")
    else:
        for r in s.gate.results:
            state = f"skipped ({r.skipped})" if r.skipped else ("PASS" if r.ok else "FAIL")
            lines.append(f"- `{r.command}` -> {state}")
    if s.open_findings:
        lines += ["", "## Open issues (blocking)"]
        for f in s.open_findings:
            lines.append(f"- [{f.severity}] {f.location or '-'}: {f.problem}" + (f" - fix: {f.fix}" if f.fix else ""))
    if s.notes:
        lines += ["", "## Notes", *[f"- {n}" for n in s.notes]]
    if s.commit_note:
        lines += ["", f"**Git:** {s.commit_note}"]
    if s.calls:
        lines += [
            "",
            "## Team activity",
            "",
            "| # | Phase | Role | Model | Turns | Cost | Time | Result |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for i, c in enumerate(s.calls, 1):
            result = "ok" if c.ok else _cell(f"failed: {c.error}")[:80]
            if c.denied:
                result += f" ({c.denied} blocked by guard)"
            lines.append(
                f"| {i} | {c.phase} | {c.role} | {c.model or 'default'} | {c.turns} | ${c.cost_usd:.2f} "
                f"| {_fmt_time(c.seconds)} | {result} |"
            )
    lines += ["", "_Cost is the engine's own estimate; on a subscription login it is notional. Transcripts are in "
              f"`{s.run_dir / 'transcripts'}`._", ""]  # fmt: skip
    return "\n".join(lines)


def summary_dict(s: RunSummary) -> dict[str, Any]:
    return {
        "status": s.status,
        "task": s.task,
        "project_dir": str(s.project_dir),
        "run_dir": str(s.run_dir),
        "seconds": round(s.seconds, 1),
        "cost_usd": round(s.cost_usd, 4),
        "changed_files": s.changed_files,
        "notes": s.notes,
        "commit": s.commit_note,
        "plan_resets_at": s.plan_resets_at,
        "open_findings": [f.model_dump() for f in s.open_findings],
        "gate": [
            {"command": r.command, "ok": r.ok, "skipped": r.skipped, "exit_code": r.exit_code}
            for r in (s.gate.results if s.gate else [])
        ],
        "calls": [c.__dict__ for c in s.calls],
    }


# ---------------------------------------------------------------------------------- progress output


class Reporter(Protocol):
    def phase(self, name: str, detail: str = "") -> None: ...
    def agent_start(self, role: str, label: str, model: str | None) -> None: ...
    def agent_event(self, role: str, kind: str, text: str) -> None: ...
    def agent_end(self, record: CallRecord) -> None: ...
    def note(self, text: str) -> None: ...


class NullReporter:
    def phase(self, name: str, detail: str = "") -> None: ...
    def agent_start(self, role: str, label: str, model: str | None) -> None: ...
    def agent_event(self, role: str, kind: str, text: str) -> None: ...
    def agent_end(self, record: CallRecord) -> None: ...
    def note(self, text: str) -> None: ...


class RecordingReporter(NullReporter):
    """Keeps everything it is told - handy for tests."""

    def __init__(self) -> None:
        self.phases: list[str] = []
        self.starts: list[tuple[str, str, str | None]] = []
        self.events: list[tuple[str, str, str]] = []
        self.ends: list[CallRecord] = []
        self.notes: list[str] = []

    def phase(self, name: str, detail: str = "") -> None:
        self.phases.append(name)

    def agent_start(self, role: str, label: str, model: str | None) -> None:
        self.starts.append((role, label, model))

    def agent_event(self, role: str, kind: str, text: str) -> None:
        self.events.append((role, kind, text))

    def agent_end(self, record: CallRecord) -> None:
        self.ends.append(record)

    def note(self, text: str) -> None:
        self.notes.append(text)


class ConsoleReporter:
    """Compact, ASCII-only progress lines (safe on any Windows code page)."""

    def __init__(self, console: Any | None = None, verbose: bool = False) -> None:
        from rich.console import Console

        self.console = console or Console(highlight=False, soft_wrap=True)
        self.verbose = verbose
        self.t0 = time.monotonic()
        self._tools: dict[str, int] = {}

    def _line(self, text: str, style: str = "") -> None:
        from rich.text import Text

        stamp = _fmt_time(time.monotonic() - self.t0).rjust(6)
        self.console.print(Text.assemble((stamp + "  ", "dim"), (text, style)))

    def phase(self, name: str, detail: str = "") -> None:
        self._line(f"== {name.upper()} {detail}".rstrip(), "bold cyan")

    def agent_start(self, role: str, label: str, model: str | None) -> None:
        self._tools[role] = 0
        self._line(f"  > {role} ({model or 'default'}): {label}", "bold")

    def agent_event(self, role: str, kind: str, text: str) -> None:
        if kind == "tool":
            n = self._tools[role] = self._tools.get(role, 0) + 1
            blocked = text.startswith("BLOCKED")
            if blocked or self.verbose or n <= 4 or n % 10 == 0:
                self._line(
                    f"      {text[:110]}" if not blocked else f"      ! {text[:150]}", "red" if blocked else "dim"
                )
        elif kind == "limit":
            self._line(f"      ! {text[:150]}", "yellow")
        elif kind == "text" and self.verbose:
            first = text.strip().splitlines()[0][:110] if text.strip() else ""
            self._line(f"      | {first}", "dim")

    def agent_end(self, record: CallRecord) -> None:
        state = "done" if record.ok else f"FAILED: {record.error[:100]}"
        extra = f" ({record.denied} blocked)" if record.denied else ""
        stats = f"turns={record.turns} ${record.cost_usd:.2f} {_fmt_time(record.seconds)}"
        self._line(f"  < {record.role} {state}  {stats}{extra}", "green" if record.ok else "bold red")

    def note(self, text: str) -> None:
        self._line(f"  * {text}", "yellow")
