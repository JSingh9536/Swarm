"""The desktop window: a live neuron field of the swarm, an inspector with each agent's membrane trace,
a scrubbable timeline, the activity feed, and a harness to launch demo/build/review/audit jobs.

Tk only (standard library). Everything drawn comes from `Brain`; everything played comes from `feed`.
"""

from __future__ import annotations

import math
import random
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, ttk
from typing import Any

from swarm_desk import sessions
from swarm_desk.brain import PHASES, Brain, Neuron
from swarm_desk.feed import SPEEDS, Player, RunRef, Tail, folder, is_live, list_runs, newest_active, newest_since
from swarm_ui.jobs import Job, JobError, JobManager
from swarm_ui.runs import list_projects

FPS = 40
BG, PANEL, PANEL2, LINE = "#0a0e16", "#0f1522", "#141c2c", "#22304a"
TEXT, MUTED, DIM = "#e7eef9", "#8391ab", "#4a5874"
ACCENT, OK, BAD, WARN, VIOLET = "#5fe3ff", "#47e08b", "#ff5f6e", "#ffc44d", "#a98bff"
UI_FONT, MONO = "Helvetica Neue", "Menlo"
STATUS_TONE = {"success": OK, "running": ACCENT, "failed": BAD, "interrupted": WARN, "aborted": BAD, "live": ACCENT,
               "idle": MUTED, "ended": OK}  # fmt: skip


def blend(color: str, base: str, f: float) -> str:
    """`f` of `color` over `base` (Tk has no alpha: glows are drawn as blended rings)."""
    f = max(0.0, min(1.0, f))
    c = [int(color[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(base[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(b[k] + (c[k] - b[k]) * f):02x}" for k in range(3))


def tint(n: Neuron) -> str:
    return {"active": ACCENT, "waiting": VIOLET, "done": OK, "failed": BAD}.get(n.state, MUTED)


def hhmmss(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M:%S") if t else "--:--:--"


def bezier(p: list[tuple[float, float]], steps: int = 22) -> list[float]:
    out: list[float] = []
    for i in range(steps + 1):
        t = i / steps
        u = 1 - t
        out += [
            u**3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t**3 * p[3][0],
            u**3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t**3 * p[3][1],
        ]
    return out


def at(p: list[tuple[float, float]], t: float) -> tuple[float, float]:
    u = 1 - t
    return (
        u**3 * p[0][0] + 3 * u * u * t * p[1][0] + 3 * u * t * t * p[2][0] + t**3 * p[3][0],
        u**3 * p[0][1] + 3 * u * u * t * p[1][1] + 3 * u * t * t * p[2][1] + t**3 * p[3][1],
    )


class Desk:
    def __init__(
        self, root: tk.Tk, repo: Path, workspace: Path, data_dir: Path, sessions_root: Path | None = None
    ) -> None:
        self.root, self.repo, self.workspace = root, repo, workspace
        self.roots = {"swarm": workspace, "claude": sessions_root or sessions.root()}
        self.last_follow = 0.0
        self.jobs = JobManager(repo, data_dir)
        self.job: Job | None = None
        self.job_offset = 0
        self.brain = Brain()
        self.player = Player()
        self.tail: Tail | None = None
        self.ref: RunRef | None = None
        self.mode = "idle"  # idle | live | replay
        self.selected = "developer"
        self.pinned_until = 0.0  # a click keeps its neuron in the inspector for a while
        self.pos: dict[str, tuple[float, float]] = {}
        self.pulses: list[dict[str, Any]] = []
        self.sparks: list[list[float]] = []
        self.shocks: list[list[Any]] = []
        self.fed = 0
        self.last = time.monotonic()
        self.last_poll = 0.0
        self.last_runs = 0.0
        self.W = self.H = 0

        root.title("Swarm Desk")
        root.configure(bg=BG)
        root.geometry("1440x900")
        root.minsize(1100, 700)
        self.bubble_font = tkfont.Font(root=root, family=MONO, size=11)
        self._style()
        self._build()
        self._bind_keys()
        self.refresh_runs()
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.closed = False
        self.tick = root.after(10, self.frame)

    # ------------------------------------------------------------------ layout

    def _style(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=PANEL, foreground=TEXT, fieldbackground=PANEL2, bordercolor=LINE,
                    lightcolor=LINE, darkcolor=LINE, font=(UI_FONT, 12))  # fmt: skip
        s.configure("TFrame", background=PANEL)
        s.configure("Bg.TFrame", background=BG)
        s.configure("TLabel", background=PANEL, foreground=TEXT)
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Head.TLabel", foreground=MUTED, font=(UI_FONT, 10, "bold"))
        s.configure("Big.TLabel", font=(UI_FONT, 20, "bold"))
        s.configure("TButton", background=PANEL2, foreground=TEXT, padding=(10, 5), borderwidth=1)
        s.map("TButton", background=[("active", LINE), ("disabled", PANEL)], foreground=[("disabled", DIM)])
        s.configure("Go.TButton", background="#123a2a", foreground=OK)
        s.map("Go.TButton", background=[("active", "#17503a")])
        s.configure("Stop.TButton", background="#3a141a", foreground=BAD)
        s.map("Stop.TButton", background=[("active", "#521b24")])
        s.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT, insertcolor=TEXT, padding=5)
        s.configure("TCombobox", fieldbackground=PANEL2, foreground=TEXT, background=PANEL2, arrowcolor=MUTED)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)])
        s.configure("TCheckbutton", background=PANEL, foreground=TEXT, indicatorcolor=PANEL2)
        s.map("TCheckbutton", indicatorcolor=[("selected", ACCENT)])
        s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=26,
                    borderwidth=0, font=(UI_FONT, 11))  # fmt: skip
        s.map("Treeview", background=[("selected", LINE)])
        s.configure("Treeview.Heading", background=PANEL2, foreground=MUTED, font=(UI_FONT, 10, "bold"), relief="flat")
        s.configure("TNotebook", background=PANEL, borderwidth=0)
        s.configure("TNotebook.Tab", background=PANEL2, foreground=MUTED, padding=(12, 4))
        s.map("TNotebook.Tab", background=[("selected", LINE)], foreground=[("selected", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", PANEL2)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(14, 10))
        top.pack(fill="x")
        tk.Label(top, text="◉", fg=ACCENT, bg=PANEL, font=(UI_FONT, 20)).pack(side="left")
        tk.Label(top, text=" SWARM DESK", fg=TEXT, bg=PANEL, font=(UI_FONT, 15, "bold")).pack(side="left")
        self.pill = tk.Label(top, text=" IDLE ", fg=BG, bg=MUTED, font=(MONO, 11, "bold"), padx=8, pady=2)
        self.pill.pack(side="left", padx=14)

        ttk.Button(top, text="■ Stop job", style="Stop.TButton", command=self.stop_job).pack(side="right")
        ttk.Button(top, text="Audit", command=lambda: self.launch("audit")).pack(side="right", padx=3)
        ttk.Button(top, text="Review", command=lambda: self.launch("review")).pack(side="right", padx=3)
        ttk.Button(top, text="Build", style="Go.TButton", command=lambda: self.launch("run")).pack(side="right", padx=3)
        self.project = ttk.Combobox(top, width=18, state="readonly")
        self.project.pack(side="right", padx=3)
        self.task = ttk.Entry(top, width=42)
        self.task.insert(0, "describe a change for the team…")
        self.task.bind("<FocusIn>", lambda _e: self.task.get().endswith("…") and self.task.delete(0, "end"))
        self.task.pack(side="right", padx=3)
        ttk.Button(top, text="▶ Demo (free)", style="Go.TButton", command=lambda: self.launch("demo")).pack(
            side="right", padx=10
        )

        body = ttk.Frame(self.root, style="Bg.TFrame")
        body.pack(fill="both", expand=True)

        # left: runs
        left = ttk.Frame(body, padding=(12, 8), width=300)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)
        ttk.Label(left, text="RUNS", style="Head.TLabel").pack(anchor="w")
        self.runs = ttk.Treeview(left, columns=("status", "cost"), show="tree headings", selectmode="browse")
        self.runs.heading("#0", text="project / run")
        self.runs.heading("status", text="status")
        self.runs.heading("cost", text="$")
        self.runs.column("#0", width=150)
        self.runs.column("status", width=76, anchor="w")
        self.runs.column("cost", width=44, anchor="e")
        for status, color in STATUS_TONE.items():
            self.runs.tag_configure(status, foreground=color)
        self.runs.tag_configure("group", foreground=MUTED, font=(UI_FONT, 10, "bold"))
        self.runs.pack(fill="both", expand=True, pady=(6, 6))
        self.runs.bind("<Double-1>", lambda _e: self.open_selected())
        self.runs.bind("<Return>", lambda _e: self.open_selected())
        row = ttk.Frame(left)
        row.pack(fill="x")
        ttk.Button(row, text="Open", command=self.open_selected).pack(side="left")
        ttk.Button(row, text="Refresh", command=self.refresh_runs).pack(side="left", padx=6)
        self.follow = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Follow new runs live", variable=self.follow).pack(anchor="w", pady=(8, 0))

        # right: inspector
        right = ttk.Frame(body, padding=(14, 8), width=330)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        ttk.Label(right, text="NEURON", style="Head.TLabel").pack(anchor="w")
        head = ttk.Frame(right)
        head.pack(fill="x", pady=(2, 4))
        self.i_name = ttk.Label(head, text="developer", style="Big.TLabel")
        self.i_name.pack(side="left")
        self.i_state = tk.Label(head, text="idle", fg=BG, bg=MUTED, font=(MONO, 10, "bold"), padx=6)
        self.i_state.pack(side="right")
        self.scope = tk.Canvas(right, height=120, bg=BG, highlightthickness=1, highlightbackground=LINE)
        self.scope.pack(fill="x", pady=(2, 8))
        grid = ttk.Frame(right)
        grid.pack(fill="x")
        self.i_stats: dict[str, ttk.Label] = {}
        for i, key in enumerate(("calls", "tool calls", "blocked", "failures", "turns", "cost", "time", "model")):
            ttk.Label(grid, text=key, style="Muted.TLabel").grid(
                row=i // 2, column=(i % 2) * 2, sticky="w", padx=(0, 6)
            )
            lab = ttk.Label(grid, text="–", font=(MONO, 12))
            lab.grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", padx=(0, 18))
            self.i_stats[key] = lab
        self.i_doing = ttk.Label(right, text="", wraplength=300, font=(MONO, 11), foreground=ACCENT)
        self.i_doing.pack(fill="x", pady=(10, 4))

        ttk.Separator(right).pack(fill="x", pady=10)
        ttk.Label(right, text="RUN", style="Head.TLabel").pack(anchor="w")
        tiles = ttk.Frame(right)
        tiles.pack(fill="x", pady=4)
        self.tiles: dict[str, ttk.Label] = {}
        for i, key in enumerate(("cost", "agent calls", "gates", "elapsed")):
            cell = ttk.Frame(tiles, padding=(0, 2))
            cell.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 26))
            ttk.Label(cell, text=key, style="Muted.TLabel").pack(anchor="w")
            lab = ttk.Label(cell, text="–", font=(UI_FONT, 18, "bold"))
            lab.pack(anchor="w")
            self.tiles[key] = lab
        self.r_task = ttk.Label(right, text="No run open. Press ▶ Demo, or double-click a run.", wraplength=300,
                                style="Muted.TLabel")  # fmt: skip
        self.r_task.pack(fill="x", pady=(6, 6))
        self.gatebar = tk.Canvas(right, height=26, bg=PANEL, highlightthickness=0)
        self.gatebar.pack(fill="x")

        # centre: phases, neuron field, timeline, feed
        centre = ttk.Frame(body, style="Bg.TFrame")
        centre.pack(side="left", fill="both", expand=True)
        self.ribbon = tk.Canvas(centre, height=34, bg=BG, highlightthickness=0)
        self.ribbon.pack(fill="x", padx=10, pady=(8, 0))
        self.canvas = tk.Canvas(centre, bg=BG, highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=10)
        self.canvas.bind("<Configure>", self._resize)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._hover)

        ctl = ttk.Frame(centre, padding=(10, 6))
        ctl.pack(fill="x")
        ttk.Button(ctl, text="⏮", width=3, command=lambda: self.seek(0)).pack(side="left")
        self.play_btn = ttk.Button(ctl, text="⏸", width=3, command=self.toggle_play)
        self.play_btn.pack(side="left", padx=4)
        ttk.Button(ctl, text="⏭ Live", command=self.go_live).pack(side="left")
        self.speed = ttk.Combobox(ctl, width=5, state="readonly", values=[f"{s:g}×" for s in SPEEDS])
        self.speed.set("1×")
        self.speed.bind("<<ComboboxSelected>>", lambda _e: self._set_speed())
        self.speed.pack(side="left", padx=8)
        self.clock = ttk.Label(ctl, text="", style="Muted.TLabel", font=(MONO, 11))
        self.clock.pack(side="right")
        self.timeline = tk.Canvas(centre, height=34, bg=PANEL, highlightthickness=0, cursor="hand2")
        self.timeline.pack(fill="x", padx=10)
        self.timeline.bind("<Button-1>", self._scrub)
        self.timeline.bind("<B1-Motion>", self._scrub)

        tabs = ttk.Notebook(centre, height=170)
        tabs.pack(fill="x", padx=10, pady=(8, 10))
        self.feed = self._text(tabs)
        self.log = self._text(tabs)
        tabs.add(self.feed, text="Activity")
        tabs.add(self.log, text="Harness log")
        self.tabs = tabs

    def _text(self, parent: tk.Misc) -> tk.Text:
        t = tk.Text(parent, bg=BG, fg=TEXT, insertbackground=TEXT, relief="flat", font=(MONO, 11), wrap="none",
                    padx=10, pady=6, highlightthickness=0, state="disabled")  # fmt: skip
        for tag, color in (("time", DIM), ("phase", VIOLET), ("start", ACCENT), ("ok", OK), ("bad", BAD),
                           ("warn", WARN), ("", TEXT)):  # fmt: skip
            t.tag_configure(tag or "plain", foreground=color)
        t.tag_configure("phase", font=(MONO, 11, "bold"))
        return t

    def _bind_keys(self) -> None:
        r = self.root
        r.bind("<space>", lambda e: None if isinstance(e.widget, ttk.Entry) else self.toggle_play())
        r.bind("<Left>", lambda e: None if isinstance(e.widget, ttk.Entry) else self.seek(self.player.index - 1))
        r.bind("<Right>", lambda e: None if isinstance(e.widget, ttk.Entry) else self.seek(self.player.index + 1))
        r.bind("<Home>", lambda _e: self.seek(0))
        for i, s in enumerate(SPEEDS, 1):
            r.bind(str(i), lambda e, s=s: None if isinstance(e.widget, ttk.Entry) else self._set_speed(s))

    # ------------------------------------------------------------------ runs & playback

    def refresh_runs(self) -> None:
        self.last_runs = time.monotonic()
        keep = self.runs.selection()
        self.runs.delete(*self.runs.get_children())
        for source, title in (("claude", "Claude Code sessions"), ("swarm", "swarm runs")):
            group = self.runs.insert("", "end", iid=f"group:{source}", text=title, open=True, tags=("group",))
            for info in list_runs(self.roots[source]):
                ref = RunRef(info["project"], info["run"], source)
                status = self._session_status(ref) if source == "claude" else info["status"]
                cost = "" if source == "claude" else f"{float(info['cost_usd'] or 0):.2f}"
                self.runs.insert(group, "end", iid=ref.iid, text=f"{info['project']}  {info['run'][9:]}",
                                 values=(status, cost), tags=(info["status"],))  # fmt: skip
        if keep and self.runs.exists(keep[0]):
            self.runs.selection_set(keep[0])
        projects = list_projects(self.workspace)
        self.project["values"] = ["(new project)", *projects]
        if not self.project.get():
            self.project.set("(new project)")

    def _session_status(self, ref: RunRef) -> str:
        path = folder(self.roots["claude"], ref)
        if path is None or (path / "summary.json").exists():
            return "ended"
        try:
            quiet = time.time() - (path / "events.jsonl").stat().st_mtime
        except OSError:
            return "idle"
        return "live" if quiet < 120 else "idle"

    def open_selected(self) -> None:
        sel = self.runs.selection()
        ref = RunRef.from_iid(sel[0]) if sel else None
        if ref:
            self.open(ref)

    def open(self, ref: RunRef, live: bool | None = None) -> None:
        path = folder(self.roots[ref.source], ref)
        if path is None:
            return
        self.ref, self.tail = ref, Tail(path)
        self.player = Player(self.tail.read_new(), self._speed_value())
        live = is_live(path) if live is None else live
        self._reset_view()
        if live:
            self.mode = "live"
            self._apply(self.player.catch_up(), quiet=True)
        else:
            self.mode = "replay"
        task = path / "task.md"
        text = task.read_text(encoding="utf-8", errors="replace").strip() if task.exists() else ""
        where = "Claude Code session" if ref.source == "claude" else "swarm run"
        self.r_task.configure(text=f"{where} · {ref.key}\n{text[:220]}", style="TLabel")

    def _reset_view(self) -> None:
        self.brain.reset()
        self.pulses, self.sparks, self.shocks = [], [], []
        self.fed = 0
        self.feed.configure(state="normal")
        self.feed.delete("1.0", "end")
        self.feed.configure(state="disabled")

    def _apply(self, events: list[dict[str, Any]], quiet: bool = False) -> None:
        for ev in events:
            self.brain.apply(ev)
        effects = self.brain.take_effects()
        if not quiet:
            for fx in effects:
                self._spawn(fx)
        active = self.brain.active
        if active and events and time.monotonic() > self.pinned_until:
            self.selected = active.name

    def seek(self, index: int) -> None:
        if not self.player.events:
            return
        if self.mode == "live":
            self.mode = "replay"
        self._reset_view()
        self._apply(self.player.seek(index), quiet=True)
        self.brain.tick(4.0)

    def toggle_play(self) -> None:
        if self.mode == "live":
            self.mode = "replay"
            self.player.paused = True
        elif self.player.done:
            self.seek(0)
            self.player.paused = False
        else:
            self.player.paused = not self.player.paused

    def go_live(self) -> None:
        if self.ref:
            self.player.paused = False
            self.mode = "live"
            self._apply(self.player.catch_up())

    def _speed_value(self) -> float:
        try:
            return float(self.speed.get().rstrip("×"))
        except ValueError:
            return 1.0

    def _set_speed(self, value: float | None = None) -> None:
        if value is not None:
            self.speed.set(f"{value:g}×")
        self.player.speed = self._speed_value()

    # ------------------------------------------------------------------ harness

    def launch(self, kind: str) -> None:
        text = self.task.get().strip()
        if text.endswith("…"):
            text = ""
        project = self.project.get()
        spec: dict[str, Any] = {"kind": kind}
        if kind == "run":
            spec["text"] = text
        if kind in ("run", "review", "audit") and project and not project.startswith("("):
            spec["project"] = project
        if kind != "demo":
            what = {"run": "build this with the full team", "review": "review", "audit": "security-audit"}[kind]
            target = spec.get("project", "a new project")
            question = f"Start a real run on your Claude plan?\n\n{what}: {target}\nbudget cap $3, low CPU priority"
            if not messagebox.askyesno("Swarm Desk", question):
                return
        try:
            self.job = self.jobs.start(spec)
        except JobError as exc:
            messagebox.showwarning("Swarm Desk", str(exc))
            return
        self.job_offset = 0
        self._log(f"$ {' '.join(self.job.argv[1:])}\n", "start")
        if self.follow.get():
            self.mode, self.ref, self.tail = "idle", None, None
            self._reset_view()
            self.r_task.configure(text=f"{self.job.title}\nwaiting for the run folder…", style="TLabel")

    def stop_job(self) -> None:
        if self.job and self.job.status == "running":
            self.jobs.stop(self.job.id)
            self._log("\n[stopped]\n", "warn")

    def _log(self, text: str, tag: str = "plain") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self) -> None:
        if self.job:
            chunk = self.jobs.tail(self.job, self.job_offset)
            if chunk["text"]:
                self.job_offset = chunk["offset"]
                self._log(chunk["text"])
            job = self.jobs.get(self.job.id)
            if self.follow.get() and self.ref is None and job:
                ref = newest_since(self.workspace, job.started)
                if ref:
                    self.refresh_runs()
                    self.open(ref, live=True)
            if job and job.status != "running":
                self._log(f"\n[{job.status}, exit {job.exit_code}]\n", "ok" if job.status == "done" else "bad")
                self.job = None
                self.refresh_runs()
        if self.tail:
            new = self.tail.read_new()
            if new:
                self.player.extend(new)
            elif self.mode == "live" and self.player.done and self._finished():
                self.mode = "replay"  # the run finished: stay on its last frame
                self.player.paused = True
                self.refresh_runs()
        now = time.monotonic()
        if self.follow.get() and not self.job and self.mode != "replay" and now - self.last_follow > 1.5:
            self.last_follow = now
            ref = newest_active(self.roots["claude"])
            if ref and ref != self.ref:  # a Claude Code session is working: show it
                self.refresh_runs()
                self.open(ref, live=True)

    def _finished(self) -> bool:
        """A pipeline run is over once it goes quiet; a Claude session only when it ends (it idles between prompts)."""
        if self.tail is None or self.ref is None:
            return True
        if self.ref.source == "claude":
            return (self.tail.folder / "summary.json").exists()
        return not is_live(self.tail.folder, 30)

    # ------------------------------------------------------------------ drawing

    def _resize(self, e: tk.Event) -> None:
        self.W, self.H = e.width, e.height
        self.canvas.delete("bg")
        step = 26
        for x in range(step // 2, self.W, step):
            for y in range(step // 2, self.H, step):
                self.canvas.create_rectangle(x, y, x + 1, y + 1, fill="#141b29", outline="", tags="bg")

    def _layout(self) -> None:
        cols = [c for c in self.brain.layers if c]
        pad_x, top, bottom = 90, 70, 70
        for li, col in enumerate(cols):
            x = pad_x + (self.W - 2 * pad_x) * (li / max(1, len(cols) - 1))
            for i, n in enumerate(col):
                y = top + (self.H - top - bottom) * ((i + 1) / (len(col) + 1))
                ox, oy = self.pos.get(n.name, (x, y))
                self.pos[n.name] = (ox + (x - ox) * 0.2, oy + (y - oy) * 0.2)

    def _curve(self, a: str, b: str) -> list[tuple[float, float]]:
        (ax, ay), (bx, by) = self.pos[a], self.pos[b]
        if bx > ax + 1:
            mx = (ax + bx) / 2
            return [(ax, ay), (mx, ay), (mx, by), (bx, by)]
        floor = max(y for _, y in self.pos.values())  # feedback synapses (review -> fix) loop under every neuron
        low = min(floor + 120 + 0.1 * abs(ax - bx), self.H - 8)  # a cubic only reaches ~3/4 of this depth
        return [(ax, ay), (ax - (ax - bx) * 0.15, low), (bx + (ax - bx) * 0.15, low), (bx, by)]

    def _spawn(self, fx: Any) -> None:
        if fx.kind == "pulse" and fx.a in self.brain.neurons and fx.b in self.brain.neurons:
            self.pulses.append({"a": fx.a, "b": fx.b, "t": 0.0, "label": fx.label})
        elif fx.kind == "spark":
            for _ in range(6):
                ang, v = random.uniform(0, math.tau), random.uniform(40, 120)
                self.sparks.append([fx.a, 0.0, 0.0, math.cos(ang) * v, math.sin(ang) * v, 1.0, fx.bad])
        elif fx.kind == "shock":
            self.shocks.append([fx.a, 0.0, fx.bad])

    def frame(self) -> None:
        if self.closed:
            return
        now = time.monotonic()
        dt = min(0.1, now - self.last)
        self.last = now
        try:
            if now - self.last_poll > 0.4:
                self.last_poll = now
                self._poll()
            if now - self.last_runs > 8:
                self.refresh_runs()
            if self.mode == "live":
                self._apply(self.player.catch_up())
            elif self.mode == "replay":
                self._apply(self.player.advance(dt))
            self.brain.tick(dt)
            if self.W:
                self._layout()
                self._draw(now, dt)
                self._draw_ribbon()
                self._draw_timeline()
            self._draw_inspector()
            self._flush_feed()
            self._status()
        finally:
            if not self.closed:
                self.tick = self.root.after(max(1, int(1000 / FPS - (time.monotonic() - now) * 1000)), self.frame)

    def _draw(self, now: float, dt: float) -> None:
        c, b = self.canvas, self.brain
        c.delete("dyn")
        if not b.count and self.mode == "idle":
            msg = (
                "waiting for the run to start…"
                if self.job
                else "Press ▶ Demo to watch a free scripted team, or open a run"
            )
            c.create_text(self.W / 2, 36, text=msg, fill=MUTED, font=(UI_FONT, 13), tags="dyn")
        speed = max(1.0, min(self.player.speed, 4.0))

        # dendrites: the static wiring, brighter next to the working neuron
        for a, bn in b.dendrites():
            hot = a.state == "active" or bn.state == "active"
            c.create_line(*bezier(self._curve(a.name, bn.name)), fill=blend(ACCENT, BG, 0.35) if hot else LINE,
                          width=1, tags="dyn")  # fmt: skip
        # synapses: handoffs that actually happened, thicker with use, glowing right after firing
        for s in b.synapses.values():
            if s.a not in self.pos or s.b not in self.pos:
                continue
            curve = self._curve(s.a, s.b)
            w = 1.5 + 1.8 * math.log2(1 + s.weight)
            c.create_line(*bezier(curve), fill=blend(VIOLET, BG, 0.35 + 0.65 * s.heat), width=w, tags="dyn",
                          capstyle="round")  # fmt: skip
            if s.weight > 1:
                mx, my = at(curve, 0.5)
                c.create_text(mx, my - 9, text=f"×{s.weight}", fill=MUTED, font=(MONO, 9), tags="dyn")

        # pulses: the handoff payload travelling to the next agent
        alive = []
        for p in self.pulses:
            p["t"] += dt * 0.9 * speed
            if p["t"] >= 1 or p["a"] not in self.pos or p["b"] not in self.pos:
                continue
            alive.append(p)
            curve = self._curve(p["a"], p["b"])
            for k in range(7):
                x, y = at(curve, max(0.0, p["t"] - k * 0.022))
                r = 6 - k * 0.7
                c.create_oval(x - r, y - r, x + r, y + r, fill=blend(ACCENT, BG, 1 - k * 0.13), outline="", tags="dyn")
            if p["label"]:
                x, y = at(curve, p["t"])
                c.create_text(x, y - 16, text=p["label"][:38], fill=ACCENT, font=(MONO, 10), tags="dyn")
        self.pulses = alive

        # neurons
        for n in b.neurons.values():
            if n.name not in self.pos:
                continue
            x, y = self.pos[n.name]
            col = tint(n)
            pot = n.potential
            r = 20 + 5 * pot
            for k in range(5, 0, -1):  # glow
                rr = r + k * 6 * (0.3 + pot)
                f = pot * (0.36 - k * 0.06)
                if f > 0.01:
                    c.create_oval(x - rr, y - rr, x + rr, y + rr, fill=blend(col, BG, f), outline="", tags="dyn")
            c.create_oval(x - r, y - r, x + r, y + r, fill=PANEL2, outline=col, width=2.5, tags="dyn")
            nr = 3 + 9 * pot
            c.create_oval(x - nr, y - nr, x + nr, y + nr, fill=blend(col, PANEL2, 0.35 + 0.65 * pot), outline="",
                          tags="dyn")  # fmt: skip
            if n.state == "active":
                spin = (now * 140) % 360
                for k in range(3):
                    c.create_arc(x - r - 8, y - r - 8, x + r + 8, y + r + 8, start=spin + k * 120, extent=46,
                                 style="arc", outline=col, width=2, tags="dyn")  # fmt: skip
            if n.name == self.selected:
                c.create_oval(x - r - 15, y - r - 15, x + r + 15, y + r + 15, outline=WARN, dash=(3, 5), width=1,
                              tags="dyn")  # fmt: skip

        # labels last, so no neighbour's glow washes them out
        for n in b.neurons.values():
            if n.name not in self.pos:
                continue
            x, y = self.pos[n.name]
            r = 20 + 5 * n.potential
            c.create_text(x, y + r + 18, text=n.title, fill=TEXT if n.state == "active" else MUTED,
                          font=(UI_FONT, 12, "bold"), tags="dyn")  # fmt: skip
            meta = " · ".join(filter(None, [f"×{n.fires}" if n.fires else "", f"{n.tools} tools" if n.tools else ""]))
            if meta:
                c.create_text(x, y + r + 34, text=meta, fill=MUTED, font=(MONO, 10), tags="dyn")
            if n.state == "active" and (n.text or n.label):
                msg = (n.text or n.label)[:48]
                tw = self.bubble_font.measure(msg) + 20
                bx = min(max(x - tw / 2, 6), self.W - tw - 6)
                by = y - r - 46
                c.create_rectangle(bx, by, bx + tw, by + 26, fill=PANEL, outline=tint(n), tags="dyn")
                c.create_text(bx + 10, by + 13, text=msg, fill=TEXT, anchor="w", font=self.bubble_font, tags="dyn")

        # shockwaves: an agent firing (or a gate landing)
        live_shocks = []
        for s in self.shocks:
            s[1] += dt * 1.3 * speed
            if s[1] < 1 and s[0] in self.pos:
                live_shocks.append(s)
                x, y = self.pos[s[0]]
                rr = 24 + 70 * s[1]
                c.create_oval(x - rr, y - rr, x + rr, y + rr, outline=blend(BAD if s[2] else ACCENT, BG, 1 - s[1]),
                              width=2, tags="dyn")  # fmt: skip
        self.shocks = live_shocks

        # sparks: tool calls (red when the guard blocked one)
        live_sparks = []
        for s in self.sparks:
            s[5] -= dt * 1.6
            if s[5] <= 0 or s[0] not in self.pos:
                continue
            s[1] += s[3] * dt
            s[2] += s[4] * dt
            live_sparks.append(s)
            x, y = self.pos[s[0]]
            px, py = x + s[1], y + s[2]
            c.create_oval(px - 2.5, py - 2.5, px + 2.5, py + 2.5, fill=blend(BAD if s[6] else ACCENT, BG, s[5]),
                          outline="", tags="dyn")  # fmt: skip
        self.sparks = live_sparks
        c.tag_lower("bg")

    def _draw_ribbon(self) -> None:
        c = self.ribbon
        c.delete("all")
        seen = [p for p, _ in self.brain.phases]
        if set(seen) <= set(PHASES):
            names = [p for p in PHASES if p != "reverse" or "reverse" in seen]
        else:  # a Claude Code session: its own phases, in the order they happened
            names = list(dict.fromkeys(reversed(seen)))[:7][::-1]
        current = self.brain.phase
        x = 4
        for i, name in enumerate(names):
            done = name in seen and name != current
            col = ACCENT if name == current else (OK if done else DIM)
            label = name.upper()
            if name == current and self.brain.phases[-1][1]:
                label += " " + self.brain.phases[-1][1]
            w = 9 * len(label) + 26
            c.create_rectangle(x, 6, x + w, 28, outline=col, fill=blend(col, BG, 0.18 if name == current else 0.05))
            c.create_text(x + w / 2, 17, text=label, fill=col, font=(MONO, 10, "bold"))
            x += w + 6
            if i < len(names) - 1:
                c.create_text(x + 2, 17, text="›", fill=DIM, font=(MONO, 12))
                x += 12

    def _draw_timeline(self) -> None:
        c = self.timeline
        c.delete("all")
        w = c.winfo_width()
        evs = self.player.events
        if not evs:
            c.create_text(10, 17, text="timeline", anchor="w", fill=DIM, font=(MONO, 10))
            return
        n = len(evs)
        for i, ev in enumerate(evs):
            x = 8 + (w - 16) * (i / max(1, n - 1))
            kind = ev.get("kind")
            if kind == "phase":
                c.create_line(x, 4, x, 30, fill=VIOLET)
            elif kind == "gate":
                col = OK if "pass" in str(ev.get("state", "")).lower() else BAD
                c.create_rectangle(x - 2, 8, x + 2, 26, fill=col, outline="")
            elif kind == "agent_start":
                c.create_line(x, 10, x, 24, fill=ACCENT, width=2)
            elif kind == "agent_end" and not ev.get("ok"):
                c.create_line(x, 10, x, 24, fill=BAD, width=2)
            else:
                c.create_line(x, 14, x, 20, fill=DIM)
        px = 8 + (w - 16) * self.player.progress
        c.create_line(px, 0, px, 34, fill=WARN, width=2)
        c.create_polygon(px - 5, 0, px + 5, 0, px, 6, fill=WARN, outline="")

    def _scrub(self, e: tk.Event) -> None:
        if self.player.events:
            w = max(1, self.timeline.winfo_width() - 16)
            self.seek(round((e.x - 8) / w * len(self.player.events)))
            self.player.paused = True

    def _hit(self, x: float, y: float) -> str | None:
        best = None
        for name, (nx, ny) in self.pos.items():
            d = math.hypot(nx - x, ny - y)
            if d < 38 and (best is None or d < best[0]):
                best = (d, name)
        return best[1] if best else None

    def _click(self, e: tk.Event) -> None:
        name = self._hit(e.x, e.y)
        if name:
            self.selected = name
            self.pinned_until = time.monotonic() + 15

    def _hover(self, e: tk.Event) -> None:
        self.canvas.configure(cursor="hand2" if self._hit(e.x, e.y) else "crosshair")

    def _draw_inspector(self) -> None:
        n = self.brain.neurons.get(self.selected) or self.brain.neuron("developer")
        col = tint(n)
        self.i_name.configure(text=n.title)
        self.i_state.configure(text=n.state.upper(), bg=col)
        stats = {
            "calls": n.fires, "tool calls": n.tools, "blocked": n.blocked, "failures": n.failures, "turns": n.turns,
            "cost": f"${n.cost:.2f}", "time": f"{n.seconds:.0f}s", "model": n.model or "–",
        }  # fmt: skip
        for key, value in stats.items():
            self.i_stats[key].configure(text=str(value))
        self.i_doing.configure(text=(n.text or n.label) if n.state == "active" else (n.text or ""))

        s = self.scope
        s.delete("all")
        w, h = s.winfo_width(), s.winfo_height()
        for k in range(1, 4):
            s.create_line(0, h * k / 4, w, h * k / 4, fill="#121a28")
        s.create_line(0, h - 8 - (h - 16) * 0.45, w, h - 8 - (h - 16) * 0.45, fill=DIM, dash=(2, 4))
        hist = list(n.history)
        if len(hist) > 1:
            pts: list[float] = []
            for i, v in enumerate(hist):
                pts += [w * i / (len(hist) - 1), h - 8 - (h - 16) * v]
            s.create_line(*pts, fill=blend(col, BG, 0.35), width=5)
            s.create_line(*pts, fill=col, width=1.6)
        s.create_text(6, 6, text="membrane potential", anchor="nw", fill=DIM, font=(MONO, 9))

        b = self.brain
        passed = sum("pass" in st.lower() for _, st in b.gates)
        self.tiles["cost"].configure(text=f"${b.total_cost:.2f}")
        self.tiles["agent calls"].configure(text=str(b.calls))
        gate_tone = (OK if passed == len(b.gates) else BAD) if b.gates else TEXT
        self.tiles["gates"].configure(text=f"{passed}/{len(b.gates)}" if b.gates else "–", foreground=gate_tone)
        self.tiles["elapsed"].configure(text=f"{b.elapsed:.0f}s" if b.count else "–")
        g = self.gatebar
        g.delete("all")
        for i, (_label, st) in enumerate(b.gates[-14:]):
            colr = OK if "pass" in st.lower() else BAD
            g.create_rectangle(i * 22, 4, i * 22 + 18, 22, fill=blend(colr, PANEL, 0.3), outline=colr)
            g.create_text(i * 22 + 9, 13, text="✓" if colr == OK else "✕", fill=colr, font=(UI_FONT, 10, "bold"))

    def _flush_feed(self) -> None:
        new = self.brain.said - self.fed
        if new <= 0:
            return
        self.fed = self.brain.said
        self.feed.configure(state="normal")
        for line in list(self.brain.feed)[-new:]:
            self.feed.insert("end", hhmmss(line.t) + "  ", "time")
            self.feed.insert("end", line.text + "\n", line.tone or "plain")
        self.feed.see("end")
        self.feed.configure(state="disabled")

    def _status(self) -> None:
        if self.job and self.job.status == "running" and self.mode != "live":
            text, color = " STARTING ", WARN
        elif self.mode == "live":
            text, color = " ● LIVE ", OK
        elif self.mode == "replay":
            text, color = (" PAUSED ", WARN) if self.player.paused else (" REPLAY ", VIOLET)
        else:
            text, color = " IDLE ", MUTED
        self.pill.configure(text=text, bg=color)
        self.play_btn.configure(text="▶" if self.player.paused or self.mode == "idle" else "⏸")
        b = self.brain
        self.clock.configure(text=f"event {self.player.index}/{len(self.player.events)}   {hhmmss(b.last_t or 0)}"
                             f"   {self.player.speed:g}×")  # fmt: skip

    def close(self) -> None:
        if self.job and self.job.status == "running":
            if not messagebox.askyesno("Swarm Desk", "A job is still running. Stop it and quit?"):
                return
            self.jobs.shutdown()
        self.closed = True  # no frame may draw into a destroyed window
        self.root.after_cancel(self.tick)
        self.root.destroy()
