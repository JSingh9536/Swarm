# swarm - an AI software engineering team

Two front ends share one set of role prompts (`.claude/agents/*.md`):

- **Inside Claude Code (this folder, no extra setup):** `/team-build <task>`, `/team-fix <bug>`, `/team-review [scope]`,
  `/team-research <question>`. The main session acts as tech lead and delegates to the specialist agents. New projects
  go under `workspace/<slug>/`, never into this repo. `claude --agent tech-lead` runs a whole session as the lead.
- **Standalone CLI (`swarm`):** an unattended, test-gated Python pipeline over the Claude Agent SDK. Needs its own login
  (`claude auth login` or `ANTHROPIC_API_KEY`); check with `swarm doctor`; try it free with `swarm demo`.

## Layout
- `.claude/agents/` roles (researcher, architect, developer, tester, reviewer, security-auditor, debugger, docs-writer, tech-lead)
- `.claude/skills/team-*/` the four workflows; `.mcp.json` online code search (grep.app, DeepWiki, Context7)
- `src/swarm/` orchestrator: `pipeline.py` (process), `guard.py` (tool policy), `gates.py` (real test/lint runs),
  `backend.py` (Claude engine via the SDK), `workspace.py` (.swarm/ artifacts + git), `cli.py`
- `ui/` separate project: local-only dashboard to monitor/command the swarm (`python -m swarm_ui`, see `ui/README.md`)
- `tests/` offline suite; `workspace/` output projects (git-ignored)

## Commands (Windows)
- Tests: `.\.venv\Scripts\python.exe -m pytest`   Lint: `.\.venv\Scripts\python.exe -m ruff check src tests`
- Tests must stay offline and free: never add a test that calls the model or the network.

## Rules for changing the toolkit
- Only the `researcher` role may have `WebSearch`/`WebFetch` (agents that read the open web must not also write or
  execute). Reviewers and the security auditor stay read-only. Roles are loaded by name; keep skills and roles in sync.
- `guard.py` is defense in depth, not a sandbox. Any new rule needs a test in both directions (blocks the bad, allows the good).
- Pipeline decisions come from structured reports (`models.py`), never from parsing prose. Loops stay bounded.
- Python 3.11+, line length 120, type hints on public functions.
