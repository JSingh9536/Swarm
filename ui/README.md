# swarm-ui

A fast, local-only dashboard to **monitor** and **command** the swarm. Standard library only, no build step, no CDN.

```powershell
..\.venv\Scripts\python.exe -m swarm_ui          # from this folder; opens your browser
..\.venv\Scripts\python.exe -m swarm_ui --port 9000 --no-browser
```

The terminal prints a link like `http://127.0.0.1:8765/#token=...`. Open that exact link (it is opened for you by
default). Ctrl+C stops the panel and any job it started.

## What it does
- **Monitor** (always on, read-only): one large word says what the swarm is doing — *Idle*, *Working*, *Quiet*
  (an item is running but its run has written nothing for 15 minutes) or *Waiting* (Claude is paused and the next
  item needs it). Beside it: the queue item being worked, the role and engine working on it, its test gates, the
  next item and the last result. Below: the work queue, the autopilot log, PC health and load mode (from the hourly
  snapshot, shown with its age), whether the local model is up, and Claude and local usage. Light and dark themes
  (button in the header, follows the system by default); one column on a phone-sized screen.
- **Team activity**: a neural-net view of the agents. The glowing neuron is the agent working now, pulses
  travel along the edges on each handoff, sparks fire on every tool call, and a live feed says exactly what each agent
  is doing. It follows the live run automatically; *Replay* / *Watch in network* replays any finished run.
  Data comes from `events.jsonl`, which the swarm pipeline now writes into each run folder (older runs are rebuilt
  from `progress.md`).
- **Runs**: the autopilot's queue runs (replay only) and every run under `workspace/*/.swarm/runs/` with live status, phase, cost, time, open findings; click one for
  the timeline, test gates, per-agent calls, findings, changed files and the plan/research/report files.
- **Command**: Build (`swarm run`), Review, Audit, Research, with budget, model and fix-round limits. Live log + Stop.
  One job at a time; jobs run at low priority (`--nice`) so your PC stays usable.

## Security model (local by design)
- Listens on **127.0.0.1 only**; refuses any other bind address.
- Random **token per launch**, sent as a bearer header, kept in `sessionStorage`, stripped from the URL at once.
- **Host and Origin checks** on every request (blocks DNS rebinding and cross-site requests); no CORS.
- Strict **CSP** (`default-src 'none'`, scripts/styles from self only), `nosniff`, no-store, frame denial; the UI never
  uses `innerHTML`, so run output cannot inject markup.
- Only four commands can be launched (`run|review|audit|research`), built as an argv list (**no shell**), user text after
  `--`, budget capped at $10, model/rounds whitelisted, projects must already exist under `workspace/`.
- Read access is limited to a whitelist of artifact names inside validated run folders (no path traversal).
- The monitor is read-only. The browser names a queue run by its item number; the server looks the folder up in
  `queue.json`, and project paths are never sent to the page (folder names only).
- It adds no dependencies. Its one network call is to the local model's status address on this PC
  (`127.0.0.1:11434`, no proxy, cached); nothing leaves the machine.

Not a substitute for a firewall or user separation: anyone who can run code as you on this PC, or who sees the
token, can use the panel. Do not port-forward or proxy it.

## Layout
`swarm_ui/` server (`server.py`), checks (`security.py`), job control (`jobs.py`), artifact reader (`runs.py`), monitor (`monitor.py`);
`web/` the UI; `tests/` offline tests: `..\.venv\Scripts\python.exe -m pytest` from this folder.
