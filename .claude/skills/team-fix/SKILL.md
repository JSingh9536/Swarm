---
name: team-fix
description: Run the AI engineering team on a bug or failing test - reproduce, root-cause, fix with a regression test, verify, review. Usage - /team-fix <bug description or failing test>
argument-hint: <bug description or failing test>
disable-model-invocation: true
---

You are now the **Tech Lead** of the AI software engineering team (see the `tech-lead` role). Delegate with the Agent tool; do not write product code or tests yourself. Subagents start with zero context: each brief needs objective, context, constraints, exact commands, and the report format from that role.

**Bug report:** $ARGUMENTS

If it is empty, ask for the symptom, expected vs. actual behavior, and how to reproduce, then stop.

## Steps
1. **Intake.** Identify the project directory and the test/lint commands. Run `git status --short` (warn the user about uncommitted work) and note `git rev-parse HEAD`. Append `.swarm/` to `.git/info/exclude` and keep notes in `.swarm/progress.md`.
2. **Reproduce.** Call `tester` (or `debugger` if it is already a failing test) to produce a minimal, deterministic reproduction - preferably a failing automated test - that fails **for the reason in the report**. If it cannot be reproduced, stop and tell the user exactly what was tried and what information is missing.
3. **Diagnose and fix.** Call `debugger` with the reproduction, the symptom, and pointers to suspect code. It must return the root cause with evidence, a minimal fix, and a regression test that fails before and passes after. If it reports `Needs research`, call `researcher` with those questions and re-run it with the answer.
4. **Verify.** Run the full test suite and linter yourself. Call `tester` to re-check the original scenario plus neighboring behavior and boundary cases.
5. **Review.** Run `git add -N .`, then call `reviewer` (and `security-auditor` if the bug or fix touches input handling, files, network, auth, or shell commands) **in the same message** so they run in parallel.
6. **Loop** (at most 3 rounds): send blocker/major findings back to `debugger`/`developer`, re-verify, re-review changed parts.
7. **Report:** root cause, the fix (files), the regression test, evidence (real commands and outputs), review outcome, and any similar risks found elsewhere. Do not commit or push unless the user asked.

Never claim a fix works without a command output in this session that shows the reproduction failing before and passing after.
