"""The swarm as a small nervous system: agents are neurons, handoffs fire along synapses, tool calls are spikes.

Pure state, no GUI: feed it `events.jsonl` records with `apply()`, advance it with `tick()`, and draw what it holds.
The window reads `effects` (pulses, sparks, shockwaves) and clears them once drawn.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# layer index -> neurons in that layer (left = input side of the pipeline, right = output side)
LAYERS: list[list[str]] = [
    ["lead", "tech-lead", "researcher", "reverse-engineer", "Explore"],
    ["architect", "Plan"],
    ["developer", "general-purpose", "docs-writer"],
    ["gate"],
    ["tester", "debugger"],
    ["reviewer", "security-auditor"],
]
CORE = {"researcher", "architect", "developer", "gate", "tester", "debugger", "reviewer", "security-auditor"}
NAMES = {"gate": "tests / lint", "security-auditor": "security", "reverse-engineer": "rea", "lead": "lead (you)",
         "general-purpose": "general"}  # fmt: skip
HUBS = {"lead", "tech-lead"}  # the main session of a Claude Code terminal: it delegates and gets results back
PHASES = ("research", "reverse", "plan", "implement", "verify", "review", "fix")

REST_ACTIVE = 0.45  # an agent that is working never decays below this
REST_WAITING = 0.18  # a lead waiting on its subagents
TAU_ACTIVE = 2.5  # seconds for a spike to fall back to the working level
TAU_IDLE = 1.2
HISTORY = 240  # potential samples kept per neuron (the inspector's oscilloscope)
MAX_FEED = 400


@dataclass
class Neuron:
    name: str
    layer: int
    state: str = "idle"  # idle | active | waiting | done | failed
    running: int = 0  # calls in flight (parallel subagents of one type share a neuron)
    potential: float = 0.0
    fires: int = 0  # agent calls
    tools: int = 0
    blocked: int = 0
    failures: int = 0
    cost: float = 0.0
    turns: int = 0
    seconds: float = 0.0
    label: str = ""
    text: str = ""
    model: str = ""
    history: deque[float] = field(default_factory=lambda: deque(maxlen=HISTORY))

    @property
    def title(self) -> str:
        return NAMES.get(self.name, self.name)

    def spike(self, amount: float) -> None:
        self.potential = min(1.0, self.potential + amount)


@dataclass
class Synapse:
    a: str
    b: str
    weight: int = 0  # handoffs seen along this edge
    heat: float = 0.0  # 1 right after a handoff, decays


@dataclass
class Effect:
    kind: str  # pulse | spark | shock
    a: str
    b: str = ""
    label: str = ""
    bad: bool = False


@dataclass
class FeedLine:
    t: float
    text: str
    tone: str = ""  # "" | phase | start | ok | bad | warn


class Brain:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.neurons: dict[str, Neuron] = {}
        self.layers: list[list[Neuron]] = [[] for _ in LAYERS]
        for li, names in enumerate(LAYERS):
            for name in names:
                if name in CORE:
                    self._add(name, li)
        self.synapses: dict[tuple[str, str], Synapse] = {}
        self.effects: list[Effect] = []
        self.feed: deque[FeedLine] = deque(maxlen=MAX_FEED)
        self.gates: list[tuple[str, str]] = []
        self.phases: list[tuple[str, str]] = []
        self.last_active: str | None = None
        self.last_ended: str | None = None
        self.first_t: float | None = None
        self.last_t: float | None = None
        self.count = 0  # events applied
        self.said = 0  # feed lines ever written (the window renders the ones it has not seen)

    # ---- topology

    def _add(self, name: str, layer: int) -> Neuron:
        n = Neuron(name, layer)
        self.neurons[name] = n
        self.layers[layer].append(n)
        return n

    def neuron(self, name: str) -> Neuron:
        if name in self.neurons:
            return self.neurons[name]
        layer = next((li for li, names in enumerate(LAYERS) if name in names), len(LAYERS) - 1)
        return self._add(name, layer)

    def dendrites(self) -> list[tuple[Neuron, Neuron]]:
        """The static wiring: every neuron reaches every neuron in the next non-empty layer."""
        cols = [col for col in self.layers if col]
        return [(a, b) for left, right in zip(cols, cols[1:], strict=False) for a in left for b in right]

    def _hub(self) -> Neuron | None:
        return next((n for n in self.neurons.values() if n.name in HUBS and n.fires), None)

    def _handoff(self, to: str, label: str, src: str | None = None) -> None:
        src = src or self.last_active
        self.last_active = to
        if not src or src == to:
            return
        syn = self.synapses.setdefault((src, to), Synapse(src, to))
        syn.weight += 1
        syn.heat = 1.0
        self.effects.append(Effect("pulse", src, to, label))

    # ---- events

    @property
    def phase(self) -> str:
        return self.phases[-1][0] if self.phases else ""

    @property
    def active(self) -> Neuron | None:
        return next((n for n in self.neurons.values() if n.state == "active"), None)

    @property
    def total_cost(self) -> float:
        return sum(n.cost for n in self.neurons.values())

    @property
    def calls(self) -> int:
        return sum(n.fires for n in self.neurons.values() if n.name != "gate")

    @property
    def elapsed(self) -> float:
        return (self.last_t - self.first_t) if self.first_t is not None and self.last_t is not None else 0.0

    def _say(self, ev: dict[str, Any], text: str, tone: str = "") -> None:
        t = ev.get("t")
        self.feed.append(FeedLine(float(t) if isinstance(t, (int, float)) else 0.0, text, tone))
        self.said += 1

    def apply(self, ev: dict[str, Any]) -> None:
        kind = ev.get("kind")
        t = ev.get("t")
        if isinstance(t, (int, float)):
            self.first_t = t if self.first_t is None else self.first_t
            self.last_t = t
        self.count += 1
        role = str(ev.get("role") or "")
        if kind == "phase":
            name, detail = str(ev.get("name") or ""), str(ev.get("detail") or "")
            self.phases.append((name, detail))
            self._say(ev, f"{name.upper()} {detail}".strip(), "phase")
        elif kind == "agent_start" and role:
            n = self.neuron(role)
            label = str(ev.get("label") or "")
            hub = self._hub()
            if ev.get("exclusive", True):  # the pipeline runs one agent at a time: a new start ends any stale one
                self._handoff(role, label)
                for other in self.neurons.values():
                    if other.state == "active" and other is not n:
                        other.state = "done"
            elif role in HUBS:  # a new prompt: the lead wakes, nothing is handed to it
                self.last_active = role
            else:  # a Claude Code subagent, maybe one of several in parallel: delegated by the lead
                self._handoff(role, label, hub.name if hub else None)
                if hub and hub.state == "active":
                    hub.state = "waiting"
            n.running += 1
            n.state, n.label, n.text = "active", label, ""
            n.model = str(ev.get("model") or "")
            n.fires += 1
            n.spike(1.0)
            self.effects.append(Effect("shock", role))
            self._say(ev, f"{role} started: {label}", "start")
        elif kind == "activity" and role:
            n = self.neuron(role)
            bad = bool(ev.get("blocked") or ev.get("failed"))
            n.state = "active"
            n.text = str(ev.get("text") or ev.get("what") or "")
            if ev.get("what", "tool") == "tool":
                n.tools += 1
            n.blocked += bool(ev.get("blocked"))
            n.spike(0.35)
            self.effects.append(Effect("spark", role, bad=bad))
            self._say(ev, f"{role} · {n.text}", "bad" if bad else "")
        elif kind == "agent_end" and role:
            n = self.neuron(role)
            ok = bool(ev.get("ok"))
            n.running = max(0, n.running - 1)
            n.state = ("active" if n.running else "done") if ok else "failed"
            self.last_ended = role
            n.failures += not ok
            n.cost += float(ev.get("cost") or 0.0)
            n.turns += int(ev.get("turns") or 0)
            n.seconds += float(ev.get("seconds") or 0.0)
            n.blocked = max(n.blocked, int(ev.get("denied") or 0))
            n.text = str(ev.get("text") or "") if ok else str(ev.get("error") or "failed")
            stats = f"${float(ev.get('cost') or 0):.2f} · {ev.get('turns', 0)} turns"
            self._say(ev, f"{role} {'finished' if ok else 'FAILED'} · {stats}", "ok" if ok else "bad")
        elif kind == "resume" and role:  # a subagent returned its result to the lead
            n = self.neuron(role)
            src = self.last_ended if self.last_ended and self.last_ended != role else None
            if src:
                self._handoff(role, "result", src)
            busy = any(o.running and o.name not in HUBS for o in self.neurons.values())
            if n.running:
                n.state = "waiting" if busy else "active"
                n.spike(0.5)
            self._say(ev, f"{role} ← {src or 'subagent'}", "start")
        elif kind == "gate":
            n = self.neuron("gate")
            label, state = str(ev.get("label") or ""), str(ev.get("state") or "")
            passed = "pass" in state.lower()
            runner = str(ev.get("role") or "")  # who ran the checks (sessions say; the pipeline's is the last agent)
            self._handoff("gate", label, runner if runner in self.neurons else None)
            n.state = "done" if passed else "failed"
            n.fires += 1
            n.failures += not passed
            n.label, n.text = label, state
            n.spike(1.0)
            self.effects.append(Effect("shock", "gate", bad=not passed))
            self.gates.append((label, state))
            self._say(ev, f"gate {label}: {state}", "ok" if passed else "bad")

    def tick(self, dt: float) -> None:
        """Let potentials and synapse heat decay, and sample every neuron's trace."""
        dt = max(0.0, dt)
        for n in self.neurons.values():
            if n.state in ("active", "waiting"):
                rest = REST_ACTIVE if n.state == "active" else REST_WAITING
                n.potential = rest + (n.potential - rest) * math.exp(-dt / TAU_ACTIVE)
            else:
                n.potential *= math.exp(-dt / TAU_IDLE)
            n.history.append(n.potential)
        for s in self.synapses.values():
            s.heat *= math.exp(-dt / 3.0)

    def take_effects(self) -> list[Effect]:
        out, self.effects = self.effects, []
        return out
