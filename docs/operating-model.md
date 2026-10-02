# Operating model: how the swarm works for its owner

Written 2026-10-01 from the owner's direction that day, a team discussion held on the local model
(`docs/team-discussion-2026-10-01.md`), and what was measured during the day's work.

## The deal

The owner gives **one sentence**. The team builds it, keeps improving it, and reports. The owner drops in
occasionally to steer. Two goals rank above everything else: **efficiency** (plan tokens, the owner's PC, time)
and **security**.

## Product lifecycle

| Stage | What happens | Who moves it on |
|---|---|---|
| 1. Build | spec with acceptance criteria, implementation, test gates, review, security audit | pipeline: all gates green and no blocking finding |
| 2. Refine | bug-fix, UI polish, hardening and performance, one improvement cycle at a time; each cycle is logged | **the owner or the CEO role** declares it ready. Nobody else can. |
| 3. Maintain | scheduled updates every **1 to 6 months**, containing security fixes and features | the security role sets the interval (below) |

The update interval is chosen by the security auditor at each release and recorded with its reason:

| Finding since the last release | Next update due |
|---|---|
| critical or high: exploitable, data exposure, auth bypass, a dependency with a known exploited CVE | now, out of cycle |
| medium, or a dependency security advisory | within 1 month |
| low only, or routine dependency updates | within 3 months |
| nothing | 6 months at most; never longer |

Features ride the same train: they are collected between releases and ship with the next scheduled update. A
feature never shortens the interval on its own; a security finding does.

Nothing with a real-world effect ships itself. Deploying, changing production settings, publishing, spending
money and contacting people are drafts until the owner approves (see the guard and the README safety model).

## Which engine does the work

Token rule: **Claude-plan tokens are spent only when needed.** `swarm.load.route()` implements this table and
is unit-tested.

| Tier | Work | Engine |
|---|---|---|
| 0 | tests, lint, grep, scripted checks, status snapshots | no model |
| 1 | docs and docstrings, test scaffolds, summaries, compressing a long conversation into a handoff file, triage, single-function edits with a test that gates them | local model (Ollama) |
| 2 | multi-file changes, bug hunts, integration, code review, security audit | Claude Sonnet |
| 3 | the spec and interfaces, the final full-suite run, anything touching production | Claude lead session |

A tier 1 task moves to Claude only when one of these is true:
1. the local model failed a real test or lint gate twice (a handoff file goes with it),
2. no local model is available,
3. the PC is in use **and** the task is marked urgent.

A busy PC alone never spends tokens: non-urgent work runs in the background or waits.

Rules the team added in discussion, adopted:
- **Every task carries a machine-checkable acceptance criterion before it starts.** No criterion, no task.
- **Classification is mechanical, not a judgment call.** A task is tier 1 only if it names at most one file to
  change and one command that proves it. Anything else is tier 2.
- **Code review and security audit stay on Claude.** Measured the same day: the local model built a small module
  whose tests passed while one requested test case was missing, and its own reviewer did not notice.

## Respecting the owner's PC

`swarm load` shows the live decision. Before every local model call the swarm looks at what is running:

| Mode | When | What the local run does |
|---|---|---|
| full | nobody is using the machine | GPU, normal priority |
| background | a video call is live, or the CPU is moderately busy | keeps the GPU, 4 CPU threads, model server at idle priority |
| cpu_only | a game or fullscreen app is running, other apps hold the GPU, or video memory is short | stays off the GPU, unloads the model from video memory, 4 threads, idle priority |
| defer | CPU above 90%, or GPU and CPU both busy | waits up to 10 minutes (`SWARM_DEFER_MAX_S`), then runs cpu_only |

Detection: games are GPU processes running from Steam, Epic, Riot or game folders (Wallpaper Engine is excluded);
a live Zoom meeting is `CptHost.exe`; Zoom, Teams or OBS merely being open is only noted. A fullscreen foreground
window counts as GPU contention. Missing signals never count as busy. `SWARM_LOAD_AWARE=0` switches it off.

Measured on this PC (RTX 2080 8 GB, 6 cores, 34 GB RAM): `qwen2.5-coder` 7B at a 16K context runs fully on the
GPU at about 76 tokens/second and holds about 5.5 GB of video memory while loaded.

## Keeping agents accurate

- **Narrow briefs.** One agent, one disjoint list of files, one command that proves the work.
- **Step cap of 25 tool calls.** Measured: 6 of 15 Claude agents overran it, and broad "hunt" or "fix
  everything" briefs were the ones that did. Split those.
- **Hallucination threshold.** An agent stops and writes a handoff file when it reaches the cap, when the same
  error survives two different fixes, when it is about to rely on something it has not seen this session, or
  when it notices it contradicted itself. A fresh agent continues from the file. The lead does the same:
  `docs/HANDOFF.md` is rewritten at each checkpoint and a new session starts from it.
- **The local model is challenged, not trusted.** If a writing role claims it is done without having called a
  single tool, the local backend tells it so and makes it do the work (bounded to two challenges).
- **An agent's "tests pass" is not evidence.** The lead runs the full suite. On 2026-10-01 that caught 3
  failures the agents' partial runs missed, and two independent reviewers then found 2 blocking bugs in code
  whose tests were green.

## What the owner sees

The Swarm Status page: queue, live sites, tests, Claude tokens by day and project, local-model work and its
electricity, PC health (GPU temperature and power, memory, disk health and free space), the current load mode,
and recommendations. `swarm status --json` produces the data.
