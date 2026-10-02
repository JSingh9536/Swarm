"""Render a swarm run as a visual flow-chart webpage: every agent, in order, grouped by phase.

Reads a run's `summary.json` (written by the pipeline) and produces a single self-contained HTML file —
no server, no libraries — showing the pipeline flow top to bottom, each agent call as a node coloured by
outcome, with its model, turns, cost, time, and any guard blocks. `swarm flow` writes it into the run dir.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

# The canonical pipeline order, so phases read top-to-bottom even if calls interleave.
PHASE_ORDER = [
    "start", "research", "plan", "implement", "verify", "review", "fix", "docs", "final", "ceo", "functions",
]  # fmt: skip


def _phase_rank(name: str) -> int:
    base = name.split(" ")[0].lower()
    return PHASE_ORDER.index(base) if base in PHASE_ORDER else len(PHASE_ORDER)


def _esc(v: Any) -> str:
    return html.escape(str(v))


def _node(call: dict[str, Any]) -> str:
    ok = call.get("ok")
    denied = call.get("denied") or 0
    badge = "done" if ok else f"failed: {_esc(call.get('error', ''))[:80]}"
    cls = "ok" if ok else "bad"
    if ok and denied:
        cls = "warn"
    model = _esc(call.get("model") or "default")
    cost = call.get("cost_usd", 0) or 0
    secs = call.get("seconds", 0) or 0
    stats = f"{_esc(call.get('turns', 0))} turns · ${cost:.2f} · {secs:.0f}s"
    deny = f'<span class="deny">{denied} blocked</span>' if denied else ""
    return (
        f'<div class="node {cls}">'
        f'<div class="role">{_esc(call.get("role", "?"))}<span class="model">{model}</span></div>'
        f'<div class="label">{_esc(call.get("label", ""))}</div>'
        f'<div class="meta">{stats}{deny}</div>'
        f'<div class="state {cls}">{badge}</div>'
        f"</div>"
    )


def build_flow_html(summary: dict[str, Any]) -> str:
    calls: list[dict[str, Any]] = summary.get("calls", [])
    status = str(summary.get("status", "unknown"))
    task = _esc(summary.get("task", ""))[:200]
    total_cost = sum((c.get("cost_usd") or 0) for c in calls)
    total_s = summary.get("seconds", 0) or 0

    # group consecutive calls by phase, phases sorted by the canonical order
    groups: list[tuple[str, list[dict[str, Any]]]] = []
    for c in sorted(calls, key=lambda c: (_phase_rank(str(c.get("phase", ""))),)):
        phase = str(c.get("phase", "run"))
        if groups and groups[-1][0] == phase:
            groups[-1][1].append(c)
        else:
            groups.append((phase, [c]))

    sections: list[str] = []
    for i, (phase, members) in enumerate(groups):
        nodes = "".join(_node(c) for c in members)
        arrow = '<div class="arrow">▼</div>' if i < len(groups) - 1 else ""
        sections.append(
            f'<section class="phase"><h2>{_esc(phase).upper()}</h2>'
            f'<div class="row">{nodes}</div></section>{arrow}'
        )
    flow = "\n".join(sections) or '<p class="empty">No agent activity recorded for this run.</p>'

    status_cls = {"success": "ok", "needs_attention": "warn", "plan_limit": "warn"}.get(status, "bad")
    gate = summary.get("gate") or []
    gate_rows = "".join(
        f'<li class="{"ok" if g.get("ok") else ("warn" if g.get("skipped") else "bad")}">'
        f'<code>{_esc(g.get("command", ""))}</code> → '
        f'{"skipped" if g.get("skipped") else ("pass" if g.get("ok") else "fail")}</li>'
        for g in gate
    )
    gate_block = f'<h2>Verification gate</h2><ul class="gate">{gate_rows}</ul>' if gate else ""

    return _TEMPLATE.format(
        task=task,
        status=_esc(status),
        status_cls=status_cls,
        calls=len(calls),
        cost=f"{total_cost:.2f}",
        secs=f"{total_s:.0f}",
        flow=flow,
        gate_block=gate_block,
    )


def flow_html_for_run(run_dir: Path) -> str:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    return build_flow_html(summary)


_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Swarm run — agent flow</title>
<style>
  :root{{ color-scheme: dark; --bg:#0c0e13; --panel:#141821; --line:#232a37; --ink:#e9edf4; --dim:#95a0b2;
    --ok:#54d093; --warn:#e2a24a; --bad:#ff6b83; --accent:#8b83ff; }}
  *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
    font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;padding:0 16px}}
  .wrap{{max-width:900px;margin:0 auto;padding:28px 0 48px}}
  h1{{font-size:1.5rem;margin:0 0 4px}} .task{{color:var(--dim);margin:0 0 18px;font-size:.95rem}}
  .tiles{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:26px}}
  .tile{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 16px;min-width:110px}}
  .tile .n{{font-size:1.3rem;font-weight:700}}
  .tile .l{{color:var(--dim);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em}}
  .pill{{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;font-size:.85rem}}
  .pill.ok{{background:#12281d;color:var(--ok)}} .pill.warn{{background:#2a2012;color:var(--warn)}}
  .pill.bad{{background:#2a1620;color:var(--bad)}}
  h2{{font-size:.78rem;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);margin:0 0 10px}}
  .phase{{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px}}
  .row{{display:flex;gap:10px;flex-wrap:wrap}}
  .arrow{{text-align:center;color:var(--dim);font-size:1.1rem;margin:6px 0}}
  .node{{flex:1 1 220px;border:1px solid var(--line);border-left:3px solid var(--line);border-radius:10px;
    padding:10px 12px;background:#10141d}}
  .node.ok{{border-left-color:var(--ok)}} .node.warn{{border-left-color:var(--warn)}}
  .node.bad{{border-left-color:var(--bad)}}
  .role{{font-weight:700;display:flex;justify-content:space-between;gap:8px;align-items:baseline}}
  .model{{font-weight:400;color:var(--dim);font-size:.78rem}}
  .label{{color:var(--dim);font-size:.85rem;margin:2px 0}}
  .meta{{font-size:.78rem;color:var(--dim);font-variant-numeric:tabular-nums}}
  .deny{{color:var(--bad);margin-left:8px}}
  .state{{margin-top:6px;font-size:.78rem;font-weight:600}}
  .state.ok{{color:var(--ok)}} .state.warn{{color:var(--warn)}} .state.bad{{color:var(--bad)}}
  .gate{{list-style:none;padding:0;margin:0}}
  .gate li{{padding:4px 0;border-bottom:1px solid var(--line);font-size:.9rem}}
  .gate li.ok code{{color:var(--ok)}} .gate li.bad code{{color:var(--bad)}}
  .gate li.warn code{{color:var(--warn)}}
  code{{font-family:ui-monospace,monospace;font-size:.85em}}
  .empty{{color:var(--dim)}}
  section.phase+.arrow+section.phase{{margin-top:0}}
</style></head><body><div class="wrap">
  <h1>Agent flow</h1>
  <p class="task">{task}</p>
  <div class="tiles">
    <div class="tile"><div class="n"><span class="pill {status_cls}">{status}</span></div>
      <div class="l">status</div></div>
    <div class="tile"><div class="n">{calls}</div><div class="l">agent calls</div></div>
    <div class="tile"><div class="n">${cost}</div><div class="l">est. cost</div></div>
    <div class="tile"><div class="n">{secs}s</div><div class="l">wall time</div></div>
  </div>
  {flow}
  {gate_block}
</div></body></html>
"""
