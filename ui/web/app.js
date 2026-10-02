"use strict";
// No innerHTML anywhere: all dynamic text goes through textContent, so run output can never inject markup.
// No inline style attributes either (the CSP forbids them): sizes are set through the element's style object.

const $ = (id) => document.getElementById(id);
const h = (tag, props = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
};

// --- token: arrives once in the URL fragment, lives in sessionStorage, is stripped from the address bar
let token = "";
try {
  const m = /token=([\w-]+)/.exec(location.hash);
  if (m) sessionStorage.setItem("swarm-token", m[1]);
  token = sessionStorage.getItem("swarm-token") || "";
} catch { /* storage blocked: token only works for this page load */ }
if (!token) { const m = /token=([\w-]+)/.exec(location.hash); token = m ? m[1] : ""; }
history.replaceState(null, "", location.pathname);

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts, headers: { Authorization: `Bearer ${token}`, ...(opts.body ? { "Content-Type": "application/json" } : {}) },
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { status: res.status });
  return data;
}

// --- theme: follows the system until the button pins light or dark
const THEMES = ["auto", "light", "dark"];
let theme = "auto";
try { theme = localStorage.getItem("swarm-theme") || "auto"; } catch { /* storage blocked */ }
if (!THEMES.includes(theme)) theme = "auto";
function applyTheme() {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  $("theme").textContent = `Theme: ${theme}`;
}
applyTheme();
$("theme").addEventListener("click", () => {
  theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length];
  try { localStorage.setItem("swarm-theme", theme); } catch { /* storage blocked */ }
  applyTheme(); viz.recolor();
});

const KINDS = {
  run: { label: "Build", text: "Task", ph: "Describe what to build or change…", project: "optional", checks: ["no_research", "commit"] },
  review: { label: "Review", text: "Focus (optional)", ph: "Anything to look at closely?", project: "required", checks: ["fix"] },
  audit: { label: "Audit", text: "Focus (optional)", ph: "Security areas to examine most closely?", project: "required", checks: ["fix"] },
  research: { label: "Research", text: "Question", ph: "e.g. best way to rate-limit an async endpoint", project: "none", checks: [] },
};
const CHECKS = { no_research: "Skip research", commit: "Commit to a new branch", fix: "Let the developer fix findings" };
let kind = "run", state = { runs: [], projects: [], job: null }, selected = null;
let mon = null, monError = "";

// --- command form
function renderKinds() {
  $("kinds").replaceChildren(...Object.entries(KINDS).map(([k, v]) =>
    h("button", { type: "button", role: "tab", "aria-selected": k === kind, onclick: () => { kind = k; renderKinds(); } }, v.label)));
  const cfg = KINDS[kind];
  $("textLabel").textContent = cfg.text; $("text").placeholder = cfg.ph;
  $("projectRow").hidden = cfg.project === "none";
  $("checks").replaceChildren(...cfg.checks.map((c) =>
    h("label", {}, h("input", { type: "checkbox", id: `chk-${c}` }), CHECKS[c])));
  fillProjects();
}
function fillProjects() {
  const sel = $("project"), prev = sel.value;
  const opts = KINDS[kind].project === "optional" ? [h("option", { value: "" }, "new project")] : [];
  sel.replaceChildren(...opts, ...state.projects.map((p) => h("option", { value: p }, p)));
  if (state.projects.includes(prev)) sel.value = prev;
}
$("cmd").addEventListener("submit", async (e) => {
  e.preventDefault();
  const spec = { kind, text: $("text").value, budget: Number($("budget").value) };
  if (!$("projectRow").hidden && $("project").value) spec.project = $("project").value;
  if ($("model").value) spec.model = $("model").value;
  if ($("rounds").value !== "") spec.rounds = Number($("rounds").value);
  for (const c of KINDS[kind].checks) spec[c] = $(`chk-${c}`).checked;
  $("launch").disabled = true; $("msg").textContent = "";
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(spec) });
    logJob = job.id; logOffset = 0; $("log").textContent = "";
    $("text").value = "";
    await refresh();
  } catch (err) { $("msg").textContent = err.message; }
  $("launch").disabled = !!state.job;
});

// --- live job + log
let logJob = null, logOffset = 0;
$("stop").addEventListener("click", async () => {
  if (state.job && confirm("Stop the running job? Work in progress is kept in the project folder.")) {
    await api(`/api/jobs/${state.job.id}/stop`, { method: "POST" }).catch((e) => ($("msg").textContent = e.message));
    refresh();
  }
});
async function pollLog() {
  const job = state.job || (state.jobs || [])[0];
  if (!job) return;
  if (logJob !== job.id) { logJob = job.id; logOffset = 0; $("log").textContent = ""; }
  try {
    const r = await api(`/api/jobs/${job.id}/log?offset=${logOffset}`);
    if (r.text) {
      const box = $("log"), pinned = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
      box.textContent += r.text;
      if (box.textContent.length > 400000) box.textContent = box.textContent.slice(-300000);
      if (pinned) box.scrollTop = box.scrollHeight;
    }
    logOffset = r.offset;
    setStatus($("jobStatus"), r.job.status);
  } catch { /* transient */ }
}

// --- formatting
const fmtTime = (s) => (s == null ? "–" : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m` : s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`);
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)}m ago` : s < 86400 ? `${Math.floor(s / 3600)}h ago` : `${Math.floor(s / 86400)}d ago`; };
const clock = (t) => new Date(t * 1000).toTimeString().slice(0, 5);
const money = (n) => `$${Number(n || 0).toFixed(2)}`;
const compact = new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 });
const whole = new Intl.NumberFormat("en");
const gb = (mb) => `${(mb / 1024).toFixed(1)} GB`;
function setStatus(el, status) { el.className = `pill s-${status}`; el.textContent = status.replaceAll("_", " "); }
const pill = (status) => { const p = h("span"); setStatus(p, status); return p; };
const fact = (k, v) => h("div", { class: "fact" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v));

// --- the monitor: what is happening now, at a glance
const STATES = {
  idle: ["Idle", "Nothing is running."],
  working: ["Working", ""],
  quiet: ["Quiet", "An item is marked as running but its run has stopped writing. It may have been interrupted."],
  waiting: ["Waiting", "Claude work is paused until the plan resets."],
};
function itemLine(it) {
  return `#${it.id} · ${it.project || "no project"} · tier ${it.tier}${it.tier === 2 ? " (Claude)" : it.tier === 1 ? " (local model)" : ""}`;
}
function engineText(live) {
  const run = live.run, model = run && (run.model || (run.active[0] || {}).model) || "";
  if (live.engine === "local") return `Local model${model ? ` · ${model}` : ""}`;
  if (live.engine === "claude") return `Claude plan${model ? ` · ${model}` : ""}`;
  return model || "not recorded";
}
function renderHero() {
  const word = $("stateWord"), sub = $("stateSub"), body = $("heroBody");
  if (!mon) {
    word.textContent = monError ? "No data" : "Connecting…"; word.className = "state is-down";
    sub.textContent = monError; body.replaceChildren(); return;
  }
  const [label, text] = STATES[mon.state] || STATES.idle;
  word.textContent = label; word.className = `state is-${mon.state}`;
  const live = mon.live, auto = mon.autopilot;
  if (live) {
    const run = live.run, it = live.item;
    sub.textContent = mon.state === "quiet" ? text : it ? itemLine(it) : `${live.project} · started from this panel`;
    const roles = run && run.active.length ? run.active.map((a) => `${a.role}${a.label ? ` — ${a.label}` : ""}`).join("; ") : run ? "between steps" : "starting…";
    const gates = run ? run.gates : [];
    body.replaceChildren(
      h("p", { class: "hero-task" }, (it ? it.task : live.task) || "(no task text)"),
      h("div", { class: "facts" },
        fact("Engine", engineText(live)), fact("Working now", roles), fact("Phase", (run && run.phase) || "–"),
        live.started_at ? fact("Running for", fmtTime(mon.now - live.started_at)) : null,
        fact("Last activity", live.last_t ? ago(live.last_t) : "none yet")),
      h("div", { class: "chips" }, h("span", { class: "muted small" }, "Gates"),
        gates.length ? gates.map((g) => h("span", { class: `chip ${g.ok ? "ok" : "bad"}` }, `${g.label}: ${g.ok ? "pass" : "fail"}`))
          : h("span", { class: "muted small" }, "no results yet")),
      run && run.activity ? h("div", { class: "now" }, run.activity) : null);
    return;
  }
  sub.textContent = text;
  const lines = [];
  if (auto.claude_paused) lines.push(h("div", {}, h("b", {}, "Claude paused "), `until ${clock(auto.paused_until)}${auto.reason ? ` — ${auto.reason}` : ""}`));
  if (mon.next) lines.push(h("div", {}, h("b", {}, "Next up "), `${itemLine(mon.next)}: ${mon.next.task}`));
  else lines.push(h("div", { class: "muted" }, mon.queue.length ? "Nothing left to do in the queue." : "The queue is empty."));
  if (mon.last) lines.push(h("div", {}, h("b", {}, "Last result "), `#${mon.last.id} ${mon.last.state}${mon.last.note ? ` — ${mon.last.note}` : ""}`));
  body.replaceChildren(h("div", { class: "hero-lines" }, lines));
}
const tile = (k, dot, v, sub) => h("div", { class: "tile" }, h("div", { class: "k" }, k),
  h("div", { class: "v" }, dot ? h("span", { class: `dot ${dot}`, "aria-hidden": "true" }) : null, v), h("div", { class: "sub" }, sub));
function renderTiles() {
  if (!mon) { $("tiles").replaceChildren(); return; }
  const st = mon.status, o = mon.ollama, a = mon.autopilot, c = mon.counts;
  const mode = st && st.load.mode, age = st && st.generated_at ? ` · as of ${ago(st.generated_at)}` : "";
  const loaded = o.models.map((m) => `${m.name} loaded${m.vram_mb ? ` · ${gb(m.vram_mb)} video memory` : ""}`).join("; ");
  $("tiles").replaceChildren(
    tile("PC load mode", mode ? (mode === "full" ? "ok" : "warn") : "", mode ? mode.replaceAll("_", " ") : "Unknown",
      st ? `${st.load.reasons[0] || "nothing competing for the machine"}${age}` : "no status snapshot yet"),
    tile("Local model (Ollama)", o.up ? "ok" : "bad", o.up ? "Up" : "Down", o.up ? loaded || "no model loaded" : "not answering on 127.0.0.1:11434"),
    tile("Claude plan", a.claude_paused ? "warn" : "ok", a.claude_paused ? "Paused" : "Available",
      a.claude_paused ? `until ${clock(a.paused_until)}${a.reason ? ` — ${a.reason}` : ""}` : "the autopilot has not hit a limit"),
    tile("Queue", c.doing ? "run" : "", `${c.todo} to do`, `${c.doing} running · ${c.done} done · ${c.blocked} blocked`));
}
const ORDER = { doing: 0, todo: 1, blocked: 2, done: 3 };
function renderQueue() {
  if (!mon) { $("queue").replaceChildren(h("div", { class: "empty" }, monError || "Loading…")); return; }
  const items = mon.queue.map((it, i) => [it, i]).sort((x, y) => ORDER[x[0].state] - ORDER[y[0].state] || x[1] - y[1]).map((x) => x[0]);
  $("queueNote").textContent = mon.autopilot.updated_at ? `autopilot last ran ${ago(mon.autopilot.updated_at)}` : "";
  $("queue").replaceChildren(...(items.length ? items.map((it) => {
    const tries = it.attempts_local + it.attempts_claude;
    return h("div", { class: `qitem is-${it.state}${it.state === "doing" ? " current" : ""}`, title: it.check ? `Check: ${it.check}` : null },
      h("span", { class: "id" }, `#${it.id}`), h("span", { class: "task" }, it.task), pill(it.state),
      h("span", { class: "meta" }, h("span", {}, it.project || "no project"), h("span", {}, `tier ${it.tier}`), it.urgent ? h("span", {}, "urgent") : null,
        tries ? h("span", {}, `${it.attempts_local} local / ${it.attempts_claude} Claude attempts`) : null, it.note ? h("span", {}, it.note) : null));
  }) : [h("div", { class: "empty" }, "The queue is empty. Add items to queue.json.")]));
}
function renderLog() {
  const box = $("autolog"), lg = mon ? mon.log : null;
  const text = lg && lg.lines.length ? lg.lines.join("\n") : "No autopilot output yet. It appears here when `swarm autopilot` runs.";
  $("logNote").textContent = lg && lg.source ? `${lg.source} · ${ago(lg.updated)}` : "";
  if (box.textContent !== text) { box.textContent = text; box.scrollTop = box.scrollHeight; }
}
function meter(label, pct, value) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0)), fill = h("i");
  fill.style.width = `${p}%`;
  return h("div", { class: "meter-row" }, h("span", {}, label),
    h("div", { class: `meter${p >= 90 ? " bad" : p >= 75 ? " warn" : ""}`, role: "meter", "aria-label": label, "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(p) }, fill),
    h("span", { class: "val" }, value));
}
function renderHealth() {
  const st = mon && mon.status;
  if (!st) { $("healthNote").textContent = ""; $("health").replaceChildren(h("div", { class: "empty" }, mon ? "No status snapshot yet. The hourly status task writes docs/status.json." : monError || "Loading…")); return; }
  $("healthNote").textContent = st.generated_at ? `hourly snapshot · ${ago(st.generated_at)}` : "";
  const rows = [];
  if (st.cpu_percent != null) rows.push(meter("CPU", st.cpu_percent, `${Math.round(st.cpu_percent)}%`));
  if (st.mem_total_mb) rows.push(meter("Memory", 100 * st.mem_used_mb / st.mem_total_mb, `${gb(st.mem_used_mb)} of ${gb(st.mem_total_mb)}`));
  const facts = [];
  for (const g of st.gpus) {
    if (g.util_pct != null) rows.push(meter("GPU", g.util_pct, `${Math.round(g.util_pct)}%`));
    if (g.mem_total_mb) rows.push(meter("Video memory", 100 * g.mem_used_mb / g.mem_total_mb, `${gb(g.mem_used_mb)} of ${gb(g.mem_total_mb)}`));
    if (g.temp_c != null) facts.push(h("span", {}, "GPU temperature ", h("b", {}, `${Math.round(g.temp_c)} °C`)));
    if (g.power_w != null) facts.push(h("span", {}, "GPU power ", h("b", {}, `${Math.round(g.power_w)} W`)));
  }
  for (const v of st.volumes) if (v.used_pct != null) rows.push(meter(`Disk ${v.mount}`, v.used_pct, `${v.free_gb} GB free`));
  const sick = st.disks.filter((d) => d.health && d.health !== "Healthy");
  if (st.disks.length) facts.push(h("span", {}, "Drives ", h("b", {}, sick.length ? `${sick.map((d) => `${d.name}: ${d.health}`).join(", ")}` : `${st.disks.length} healthy`)));
  $("health").replaceChildren(...rows, facts.length ? h("div", { class: "kv" }, facts) : null);
}
function renderUsage() {
  const st = mon && mon.status;
  if (!st) { $("usageNote").textContent = ""; $("usage").replaceChildren(h("div", { class: "empty" }, mon ? "No status snapshot yet." : monError || "Loading…")); return; }
  const days = st.claude.by_day, max = Math.max(1, ...days.map((d) => d.output || 0)), t = st.claude.total, l = st.local;
  $("usageNote").textContent = st.claude.since_days ? `last ${st.claude.since_days} days: ${compact.format(t.output || 0)} output tokens` : "";
  const bars = days.map((d) => {
    const bar = h("div", { class: "bar" });
    bar.style.width = `${Math.max(0.5, 78 * (d.output || 0) / max)}%`;
    return h("div", { class: "bar-row", title: `${d.day}: ${whole.format(d.output || 0)} output and ${whole.format(d.input || 0)} input tokens in ${whole.format(d.messages || 0)} messages` },
      h("span", { class: "day" }, String(d.day).slice(5)), h("div", { class: "bar-wrap" }, bar, h("span", {}, compact.format(d.output || 0))));
  });
  $("usage").replaceChildren(
    h("h3", {}, "Claude output tokens per day"), ...(bars.length ? bars : [h("div", { class: "muted small" }, "No Claude usage recorded.")]),
    h("div", { class: "sect" }, h("h3", {}, "Local model"), l.calls ? h("div", { class: "kv" },
      h("span", {}, h("b", {}, whole.format(l.calls)), " agent calls"), h("span", {}, h("b", {}, compact.format((l.tokens_in || 0) + (l.tokens_out || 0))), " tokens"),
      h("span", {}, h("b", {}, `${l.kwh} kWh`), ` (about $${l.electricity_usd})`), h("span", {}, h("b", {}, fmtTime(l.seconds)), " of model time"))
      : h("div", { class: "muted small" }, "No local model work recorded.")));
}

// --- runs (under workspace/, plus the queue items' own project folders)
function render() {
  const live = state.runs.filter((r) => r.status === "running").length + (mon && mon.live && mon.live.item ? 1 : 0);
  $("stats").replaceChildren(
    h("div", { class: "stat" }, h("b", {}, live), h("span", {}, "running")),
    h("div", { class: "stat" }, h("b", {}, state.run_count), h("span", {}, "runs")),
    h("div", { class: "stat" }, h("b", {}, money(state.total_cost_usd)), h("span", {}, "est. cost")));
  const q = $("filter").value.toLowerCase();
  const all = [...(mon ? mon.recent : []), ...state.runs].sort((a, b) => b.updated - a.updated);
  const list = all.filter((r) => !q || `${r.project} ${r.task} ${r.status}`.toLowerCase().includes(q));
  $("runs").replaceChildren(...(list.length ? list.map((r) =>
    h("button", { class: "run", type: "button", title: r.item != null ? "Replay this run in the team view" : "Open run details",
      onclick: () => (r.item != null ? watchQueueRun(r) : openRun(r.project, r.run)) },
      h("span", { class: "task" }, r.task || "(no task)"), pill(r.status),
      h("span", { class: "meta" }, h("span", {}, r.project), r.item != null ? h("span", {}, "queue run") : null, r.phase && r.status === "running" ? h("span", {}, r.phase) : null,
        h("span", {}, money(r.cost_usd)), h("span", {}, fmtTime(r.seconds)), r.open_findings ? h("span", {}, `${r.open_findings} open finding(s)`) : null,
        h("span", {}, ago(r.updated))))) : [h("div", { class: "empty" }, "No runs yet.")]));
  const job = state.job, last = (state.jobs || [])[0];
  const shown = job || last;
  $("jobCard").hidden = !shown;
  if (shown) { $("jobTitle").textContent = shown.title; setStatus($("jobStatus"), shown.status); }
  $("stop").hidden = !job;
  $("launch").disabled = !!job;
  if (!$("launch").disabled && $("msg").textContent === "") $("launch").title = "";
  if (job) $("launch").title = "A job is already running";
  fillProjects();
}
function watchQueueRun(r) { startWatch(r, false); replay(); window.scrollTo({ top: 0, behavior: "smooth" }); }

// --- drawer
async function openRun(project, run) {
  selected = { project, run };
  $("drawer").hidden = $("scrim").hidden = false;
  $("dProject").textContent = project; $("dTitle").textContent = "Loading…"; $("dBody").replaceChildren();
  await renderDetail();
}
async function renderDetail() {
  if (!selected) return;
  let d;
  try { d = await api(`/api/runs/${selected.project}/${selected.run}`); } catch (e) { $("dTitle").textContent = e.message; return; }
  $("dTitle").textContent = d.task.split("\n")[0].slice(0, 120) || selected.run;
  const docBox = h("pre", { class: "doc" }, "Pick a file above.");
  const tabs = h("div", { class: "tabs" }, d.files.map((f) =>
    h("button", { class: "btn", type: "button", "aria-pressed": "false", onclick: async (ev) => {
      tabs.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === ev.currentTarget)));
      docBox.textContent = "Loading…";
      try { docBox.textContent = (await api(`/api/runs/${d.project}/${d.run}/file/${f}`)).text; } catch (e) { docBox.textContent = e.message; }
    } }, f)));
  const section = (title, ...kids) => h("section", {}, h("h3", {}, title), ...kids);
  $("dBody").replaceChildren(
    h("div", { class: "row-tight" }, pill(d.status), h("span", { class: "muted" }, `${money(d.cost_usd)} · ${fmtTime(d.seconds)} · run ${d.run}`),
      h("button", { class: "btn", type: "button", onclick: () => { startWatch(d, false); closeDrawer(); replay(); window.scrollTo({ top: 0, behavior: "smooth" }); } }, "Watch in network")),
    section("Timeline", h("ul", { class: "tl" }, d.events.map((e) =>
      h("li", {}, h("time", {}, e.time), h("span", { class: e.text.startsWith("PHASE") ? "phase" : "" }, e.text))))),
    d.gates.length ? section("Test & lint gates", h("table", {}, h("tr", {}, ["Command", "Result"].map((t) => h("th", {}, t))),
      d.gates.map((g) => h("tr", {}, h("td", {}, h("code", {}, g.command)), h("td", { class: g.ok ? "s-success" : "s-failed" }, g.skipped ? "skipped" : g.ok ? "pass" : "fail"))))) : null,
    d.calls.length ? section("Agent calls", h("table", {}, h("tr", {}, ["Role", "Step", "Model", "Turns", "Time", "Cost"].map((t) => h("th", {}, t))),
      d.calls.map((c) => h("tr", {}, h("td", {}, c.role), h("td", {}, c.label), h("td", {}, c.model || "–"), h("td", { class: "num" }, c.turns),
        h("td", { class: "num" }, fmtTime(c.seconds)), h("td", { class: "num" }, money(c.cost_usd)))))) : null,
    d.findings.length ? section("Open findings", h("ul", { class: "tl" }, d.findings.map((f) =>
      h("li", {}, h("span", { class: "s-needs_attention" }, f.severity), h("span", {}, `${f.location || "-"}: ${(f.problem || "").split("\n")[0]}`))))) : null,
    d.changed_files.length ? section("Changed files", h("pre", { class: "doc" }, d.changed_files.join("\n"))) : null,
    d.notes.length ? section("Notes", h("pre", { class: "doc" }, d.notes.join("\n"))) : null,
    section("Artifacts", tabs, docBox));
}
function closeDrawer() { selected = null; $("drawer").hidden = $("scrim").hidden = true; }
$("dClose").addEventListener("click", closeDrawer);
$("scrim").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("filter").addEventListener("input", render);

// --- neural view: follows the live run (a queue item's or one started here), or replays any run
const viz = createViz($("net"), $("feed"));
let watch = null, replayTimer = 0; // {project, run, item, next, follow, replaying}
const same = (a, b) => a && b && a.project === b.project && a.run === b.run && (a.item ?? null) === (b.item ?? null);
const eventsUrl = (w, since) => (w.item != null ? `/api/monitor/events?item=${w.item}&run=${w.run}&since=${since}` : `/api/runs/${w.project}/${w.run}/events?since=${since}`);
function setNow(ev) {
  if (ev.kind === "activity") $("now").textContent = `${ev.role} — ${ev.text || ev.what}`;
  else if (ev.kind === "agent_start") $("now").textContent = `${ev.role} — ${ev.label || "starting"}`;
  else if (ev.kind === "phase") $("now").textContent = `phase: ${ev.name} ${ev.detail || ""}`;
}
function startWatch(r, follow) {
  clearInterval(replayTimer); viz.reset(); $("now").textContent = "Idle";
  watch = { project: r.project, run: r.run, item: r.item ?? null, next: 0, follow, replaying: false, status: r.status };
  $("watching").textContent = `${r.project} · ${r.run}${r.item != null ? " · queue run" : ""}`;
}
async function pollEvents(snapshot) {
  if (!watch || watch.replaying) return;
  const w = watch;
  try {
    const r = await api(eventsUrl(w, w.next));
    if (watch !== w) return;
    r.events.forEach((e) => { viz.apply(e); setNow(e); });
    w.next = r.next;
    if (snapshot && r.events.length) viz.settle();
    if (r.events.length === 400) return pollEvents(snapshot);
  } catch { /* run folder not ready yet */ }
}
async function replay() {
  if (!watch) return;
  const w = watch; clearInterval(replayTimer); viz.reset(); w.replaying = true; w.follow = false;
  let all = [], since = 0;
  try { for (;;) { const r = await api(eventsUrl(w, since)); all = all.concat(r.events); if (!r.events.length || r.events.length < 400) break; since = r.next; } } catch { /* ignore */ }
  let i = 0;
  replayTimer = setInterval(() => {
    if (watch !== w || i >= all.length) { clearInterval(replayTimer); w.replaying = false; w.next = all.length; return; }
    viz.apply(all[i]); setNow(all[i]); i++;
  }, 380);
}
function liveTarget() {
  const l = mon && mon.live;
  if (l && l.run && !l.quiet) return { project: l.project, run: l.run.id, item: l.item ? l.item.id : null, status: "running" };
  return state.runs.find((r) => r.status === "running") || null;
}
function latestTarget() {
  return [...(mon ? mon.recent : []), ...state.runs].sort((a, b) => b.updated - a.updated)[0] || null;
}
function updateNetwork() {
  const live = liveTarget();
  if (live && (!watch || watch.follow || !same(watch, live) && watch.status !== "running")) { if (!same(watch, live)) startWatch(live, true); }
  else if (!watch && latestTarget()) { startWatch(latestTarget(), true); watch.snapshot = true; }
  $("follow").hidden = !watch || (watch.follow && !watch.replaying);
  $("replay").hidden = !watch;
  if (!live && watch && watch.follow && !watch.replaying) $("now").textContent = watch.next ? "Finished — press Replay to watch it again" : "Idle";
}
$("follow").addEventListener("click", () => { const r = liveTarget() || latestTarget(); if (r) startWatch(r, true); });
$("replay").addEventListener("click", replay);

// --- refresh loop (paused while the tab is hidden); nothing here needs a manual reload
async function refresh() {
  try {
    const [overview, monitor] = await Promise.all([api("/api/overview"), api("/api/monitor").catch((e) => ({ failed: e }))]);
    state = overview;
    if (monitor.failed) { mon = null; monError = monitor.failed.status === 404 ? "This swarm-ui process is an older version. Restart it to see the monitor." : `Monitor data unavailable: ${monitor.failed.message}`; }
    else { mon = monitor; monError = ""; }
    $("conn").className = "conn ok"; $("conn").textContent = `local · v${state.version}`;
    renderHero(); renderTiles(); renderQueue(); renderLog(); renderHealth(); renderUsage();
    render();
    updateNetwork();
    await pollEvents(watch && watch.snapshot);
    if (watch) watch.snapshot = false;
    await pollLog();
    if (selected && state.runs.find((r) => r.project === selected.project && r.run === selected.run)?.status === "running") renderDetail();
  } catch (e) {
    $("conn").className = "conn bad";
    $("conn").textContent = e.status === 401 ? "not authorised — open the link printed by swarm-ui" : "server unreachable";
    mon = null; monError = e.status === 401 ? "Open the link printed by swarm-ui: it carries this launch's access token." : "The swarm-ui server is not answering. Start it with: python -m swarm_ui";
    renderHero(); renderTiles();
  }
}
function loop() {
  const busy = state.job || (mon && mon.state === "working");
  const delay = document.hidden ? 5000 : busy ? 1500 : 3000;
  setTimeout(async () => { if (!document.hidden) await refresh(); loop(); }, delay);
}
renderKinds(); refresh().then(loop);
