"use strict";
// Neural-net view of the team: agents are neurons, handoffs are pulses along the edges, tool calls are sparks.
// Pure canvas 2D, no dependencies. Feed it the swarm's events with viz.apply(event).

(function () {
  // layer index -> agents in that layer (left = input side of the pipeline, right = output side)
  const LAYOUT = [["researcher"], ["architect"], ["developer"], ["gate"], ["tester", "debugger"], ["reviewer", "security-auditor"]];
  const NAMES = { gate: "tests / lint", "security-auditor": "security" };
  const MAX_FEED = 14;
  const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

  window.createViz = function (canvas, feedEl) {
    const ctx = canvas.getContext("2d");
    let W = 0, H = 0, dpr = 1, colors = {}, raf = 0, running = true;
    let nodes = new Map(), layers = [], pulses = [], sparks = [], lastActive = null, feed = [];

    function readColors() {
      const s = getComputedStyle(document.documentElement), g = (n) => s.getPropertyValue(n).trim();
      colors = { line: g("--line"), text: g("--text"), muted: g("--muted"), accent: g("--accent"), ok: g("--ok"), run: g("--run"), warn: g("--warn"), bad: g("--bad"), bg: g("--panel") };
    }
    function reset() {
      nodes = new Map(); layers = LAYOUT.map((names, li) => names.map((n) => addNode(n, li)));
      pulses = []; sparks = []; lastActive = null; feed = []; renderFeed(); layout();
    }
    function addNode(name, layer) {
      const n = { name, layer, x: 0, y: 0, state: "idle", text: "", energy: 0, runs: 0, x0: 0 };
      nodes.set(name, n); return n;
    }
    function node(name) {
      if (nodes.has(name)) return nodes.get(name);
      const n = addNode(name, LAYOUT.length - 1); layers[LAYOUT.length - 1].push(n); layout(); return n;
    }
    function layout() {
      const pad = 70, top = 36, bottom = 44;
      layers.forEach((col, li) => col.forEach((n, i) => {
        n.tx = pad + (W - 2 * pad) * (li / (layers.length - 1));
        n.ty = top + (H - top - bottom) * ((i + 1) / (col.length + 1));
        if (!n.x) { n.x = n.tx; n.y = n.ty; }
      }));
    }
    function resize() {
      dpr = Math.min(devicePixelRatio || 1, 2);
      const r = canvas.getBoundingClientRect(); W = r.width; H = r.height;
      canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0); layout(); readColors();
    }

    function hhmmss(t) { return t ? new Date(t * 1000).toTimeString().slice(0, 8) : ""; }
    function pushFeed(ev, text, cls) {
      feed.push({ time: hhmmss(ev.t), text, cls }); if (feed.length > MAX_FEED) feed.shift(); renderFeed();
    }
    function renderFeed() {
      if (!feedEl) return;
      feedEl.replaceChildren(...feed.slice().reverse().map((f) => {
        const li = document.createElement("li"); if (f.cls) li.className = f.cls;
        const t = document.createElement("time"); t.textContent = f.time;
        li.append(t, document.createTextNode(f.text)); return li;
      }));
      if (!feed.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "Waiting for the team to start…"; feedEl.append(li); }
    }

    function handoff(from, to) {
      if (!from || from === to || reduceMotion) return;
      pulses.push({ a: from, b: to, t: 0 });
    }
    function spark(n, bad) {
      if (reduceMotion) return;
      for (let i = 0; i < 4; i++) {
        const a = Math.random() * Math.PI * 2, v = 0.4 + Math.random() * 0.9;
        sparks.push({ x: n.x, y: n.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v, life: 1, bad });
      }
    }

    function apply(ev) {
      switch (ev.kind) {
        case "phase":
          pushFeed(ev, `${ev.name.toUpperCase()} ${ev.detail || ""}`.trim(), "phase"); break;
        case "agent_start": {
          const n = node(ev.role); handoff(lastActive, n.name);
          n.state = "active"; n.energy = 1; n.runs++; n.text = ev.label || ""; lastActive = n.name;
          pushFeed(ev, `${ev.role} started — ${ev.label || ""}`, "start"); break;
        }
        case "activity": {
          const n = node(ev.role); n.state = "active"; n.text = ev.text || n.text; n.energy = 1; spark(n, ev.blocked);
          pushFeed(ev, `${ev.role} · ${ev.text || ev.what}`, ev.blocked ? "bad" : ""); break;
        }
        case "agent_end": {
          const n = node(ev.role); n.state = ev.ok ? "done" : "failed"; n.energy = 0.6; n.text = ev.ok ? "" : ev.error || "failed";
          pushFeed(ev, `${ev.role} ${ev.ok ? "finished" : "FAILED"} · $${Number(ev.cost || 0).toFixed(2)}`, ev.ok ? "ok" : "bad"); break;
        }
        case "gate": {
          const n = node("gate"); handoff(lastActive, "gate"); lastActive = "gate"; n.energy = 1;
          n.state = /pass/i.test(ev.state) ? "done" : "failed"; n.text = `${ev.label}: ${ev.state}`;
          pushFeed(ev, `gate ${ev.label}: ${ev.state}`, n.state === "done" ? "ok" : "bad"); break;
        }
      }
    }

    const tint = (n) => (n.state === "failed" ? colors.bad : n.state === "done" ? colors.ok : n.state === "active" ? colors.accent : colors.muted);
    function edge(a, b, alpha, hot) {
      ctx.beginPath(); ctx.moveTo(a.x, a.y);
      const mx = (a.x + b.x) / 2; ctx.bezierCurveTo(mx, a.y, mx, b.y, b.x, b.y);
      ctx.strokeStyle = hot ? colors.accent : colors.line; ctx.globalAlpha = alpha; ctx.lineWidth = hot ? 1.6 : 1; ctx.stroke(); ctx.globalAlpha = 1;
    }
    function bez(a, b, t) {
      const mx = (a.x + b.x) / 2, u = 1 - t;
      return [u ** 3 * a.x + 3 * u * u * t * mx + 3 * u * t * t * mx + t ** 3 * b.x, u ** 3 * a.y + 3 * u * u * t * a.y + 3 * u * t * t * b.y + t ** 3 * b.y];
    }

    function frame(ts) {
      raf = requestAnimationFrame(frame);
      if (document.hidden || !W) return;
      ctx.clearRect(0, 0, W, H);
      for (const n of nodes.values()) { n.x += (n.tx - n.x) * 0.15; n.y += (n.ty - n.y) * 0.15; n.energy *= n.state === "active" ? 0.992 : 0.97; }

      for (let li = 0; li < layers.length - 1; li++)
        for (const a of layers[li]) for (const b of layers[li + 1])
          edge(a, b, a.state === "active" || b.state === "active" ? 0.9 : 0.55, a.state === "active" || b.state === "active");

      pulses = pulses.filter((p) => (p.t += 0.018) < 1);
      for (const p of pulses) {
        const a = nodes.get(p.a), b = nodes.get(p.b); if (!a || !b) continue;
        for (let k = 0; k < 5; k++) {
          const [x, y] = bez(a, b, Math.max(0, p.t - k * 0.025));
          ctx.beginPath(); ctx.arc(x, y, 4 - k * 0.6, 0, 7); ctx.fillStyle = colors.accent; ctx.globalAlpha = 1 - k * 0.2; ctx.fill();
        }
        ctx.globalAlpha = 1;
      }
      sparks = sparks.filter((s) => (s.life -= 0.03) > 0);
      for (const s of sparks) {
        s.x += s.vx; s.y += s.vy; ctx.beginPath(); ctx.arc(s.x, s.y, 2, 0, 7);
        ctx.fillStyle = s.bad ? colors.bad : colors.accent; ctx.globalAlpha = s.life; ctx.fill();
      }
      ctx.globalAlpha = 1;

      for (const n of nodes.values()) {
        const c = tint(n), active = n.state === "active", r = 15 + (active ? 2 * Math.sin(ts / 260) : 0);
        if (active || n.energy > 0.05) {
          const g = ctx.createRadialGradient(n.x, n.y, r * 0.5, n.x, n.y, r * 2.6);
          g.addColorStop(0, c); g.addColorStop(1, "transparent");
          ctx.globalAlpha = 0.35 * Math.min(1, n.energy + (active ? 0.4 : 0)); ctx.fillStyle = g;
          ctx.beginPath(); ctx.arc(n.x, n.y, r * 2.6, 0, 7); ctx.fill(); ctx.globalAlpha = 1;
        }
        ctx.beginPath(); ctx.arc(n.x, n.y, r, 0, 7); ctx.fillStyle = colors.bg; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = c; ctx.stroke();
        if (active && !reduceMotion) {
          ctx.save(); ctx.translate(n.x, n.y); ctx.rotate(ts / 700); ctx.setLineDash([4, 6]);
          ctx.beginPath(); ctx.arc(0, 0, r + 6, 0, 7); ctx.strokeStyle = c; ctx.lineWidth = 1; ctx.stroke(); ctx.restore();
        } else if (n.state === "done" || n.state === "failed") {
          ctx.beginPath(); ctx.arc(n.x, n.y, 5, 0, 7); ctx.fillStyle = c; ctx.fill();
        } else { ctx.beginPath(); ctx.arc(n.x, n.y, 3, 0, 7); ctx.fillStyle = colors.line; ctx.fill(); }
        ctx.fillStyle = active ? colors.text : colors.muted; ctx.font = "600 11px system-ui, sans-serif"; ctx.textAlign = "center";
        ctx.fillText(NAMES[n.name] || n.name, n.x, n.y + r + 16);
        if (n.runs > 1) { ctx.fillStyle = colors.muted; ctx.font = "10px system-ui, sans-serif"; ctx.fillText(`×${n.runs}`, n.x, n.y + r + 28); }
        if (active && n.text) {
          ctx.font = "11px ui-monospace, Consolas, monospace"; const t = n.text.length > 34 ? n.text.slice(0, 33) + "…" : n.text;
          const w = ctx.measureText(t).width + 14, bx = Math.min(Math.max(n.x - w / 2, 4), W - w - 4), by = n.y - r - 30;
          ctx.fillStyle = colors.bg; ctx.strokeStyle = c; ctx.lineWidth = 1; ctx.beginPath(); ctx.roundRect(bx, by, w, 20, 6); ctx.fill(); ctx.stroke();
          ctx.fillStyle = colors.text; ctx.textAlign = "left"; ctx.fillText(t, bx + 7, by + 14);
        }
      }
    }

    new ResizeObserver(resize).observe(canvas);
    matchMedia("(prefers-color-scheme: light)").addEventListener("change", readColors);
    resize(); reset(); raf = requestAnimationFrame(frame);
    return { apply, reset, recolor: readColors, settle() { pulses = []; sparks = []; }, stop() { running = false; cancelAnimationFrame(raf); }, get running() { return running; } };
  };
})();
