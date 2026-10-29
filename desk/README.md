# swarm-desk

A native desktop window (not a web page) that shows the swarm as a small nervous system, and a harness to launch,
follow and replay its runs. Standard library only (Tk); it reuses `ui/swarm_ui` for run reading and job launching,
so the same validation and no-shell rules apply.

```powershell
..\.venv\Scripts\python.exe -m swarm_desk            # Windows, from this folder
../.venv/bin/python -m swarm_desk --demo             # macOS/Linux: open and start the free demo at once
../.venv/bin/python -m swarm_desk --open demo-20261029-013910/20261029-013910   # replay one run
```

## What you see
- **Neuron field.** Each agent is a neuron, and the test/lint gate is one too. A neuron *fires* (a shockwave) when the
  agent starts. Every tool call is a spike with sparks, red when the guard blocked it. A working agent holds a resting
  potential with spinning rings and a bubble saying what it is doing.
- **Synapses learn.** Faint lines are the pipeline's static wiring. Violet synapses are handoffs that really happened,
  thicker each time (`×n`) and glowing right after they fire. The handoff label travels along the synapse as a pulse.
  Feedback (review → developer fix) loops underneath.
- **Inspector.** Click a neuron to see its membrane-potential trace (an oscilloscope of its activity), calls, tool
  calls, blocked calls, failures, turns, cost, time and model. Below that are run tiles (cost, agent calls, gates passed,
  elapsed) and a strip of gate results.
- **Phase ribbon and timeline.** The pipeline phases light up in order. The timeline shows every event (phases violet,
  agent starts cyan, gates green/red). Click or drag it to seek.
- **Activity / Harness log.** A colour-coded feed of the run, and the raw output of the job the harness started.

## Harness
- **▶ Demo (free)** runs `swarm demo --pace 0.6`: the scripted team through the real pipeline (real git, real pytest),
  no model calls, slowed down so you can watch it.
- **Build / Review / Audit** start real runs on your Claude plan (asks first; $3 budget cap, low CPU priority).
  Build takes the task text and an optional existing project; Review and Audit need a project.
- With *Follow new runs live* on, the window switches to the run as soon as its folder appears. **■ Stop job** kills it.
- One job at a time. Only the fixed kinds can be launched and nothing goes through a shell (see `ui/swarm_ui/jobs.py`).

Keys: `space` play/pause, `←`/`→` step one event, `Home` restart, `1`–`5` speed 0.5×–8×.

## Layout
`swarm_desk/brain.py` the neuron model (pure, no GUI), `feed.py` live tail and replay clock, `app.py` the window,
`main.py` the entry point. Tests run offline: `../.venv/bin/python -m pytest` from this folder. The GUI test opens a
window for a moment and skips where there is no display.
