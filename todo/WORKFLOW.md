# Command reference

All commands are run from the repository root with the project's Python:

```bash
python -m tools.workflow <command> [args]
```

Use `--repo <path>` to point at a different checkout. Use
`--worktree-root <path>` to override where isolated worktrees are created
(default: `<repo-parent>/<repo-name>-worktrees`).

## Repository health

```bash
python -m tools.workflow validate
# → {"status": "OK"} or {"status": "ERROR", "error": "..."}
```

Checks `workflow.yml` schema, `todo/config.yaml` schema, the protected
path snapshot mechanism, and the existence of every required agent
definition.

```bash
python -m tools.workflow status
# → {active_phase, active_task, workflow_state, task_status, attempt, ...}
```

Returns the effective state, including any active worktree.

## Task execution (the normal route)

```bash
# 1. Activate a dependency-complete PLANNED task.
python -m tools.workflow ready T002

# 2. Prepare a development worktree and get the developer prompt.
python -m tools.workflow prepare-develop T002
# → {branch, development_worktree, prompt, agent: "stage-developer", ...}

# 3. Launch the stage-developer agent visibly with the returned prompt.

# 4. When the agent finishes, seal its structured result.
python -m tools.workflow finish-develop T002

# 5. Prepare an independent detached worktree for the reviewer.
python -m tools.workflow prepare-review T002

# 6. Launch the stage-reviewer agent visibly.

# 7. Record the review.
python -m tools.workflow finish-review T002
# → {"status": "APPROVED" | "CHANGES_REQUESTED" | "BLOCKED", "report": "..."}
```

If a Developer returns `CONTINUATION_REQUIRED`, use:

```bash
python -m tools.workflow continue-develop T002
```

Pass `--max-turns-exhausted` only when the developer's session was killed
by a hard turn limit before it could write the handoff.

To retry after `CHANGES_REQUESTED`:

```bash
python -m tools.workflow prepare-retry T002
# → fresh developer prompt in the same worktree
```

## Triage

If a Developer returns `TRIAGE_REQUIRED`:

```bash
python -m tools.workflow prepare-triage T002
# → {prompt, agent: "issue-triager", ...}
# launch the issue-triager agent
python -m tools.workflow finish-triage T002
# → {"status": "CHANGES_REQUESTED" | "OWNER_DECISION_REQUIRED" | ...}
```

## Owner amendments

To plan a change before implementation (an amendment does not start any
target task's implementation):

```bash
python -m tools.workflow prepare-amendment \
    --task T002 --task T003 \
    --layer CONTRACT \
    --summary "split T002 obligation" \
    --owner-direction "separate the two deliverables into T002 and T003"
# → {amendment_id, branch, worktree, prompt, agent: "planner", ...}

# launch planner; then:
python -m tools.workflow finish-amendment A0001
python -m tools.workflow prepare-amendment-review A0001
# launch plan-reviewer; then:
python -m tools.workflow finish-amendment-review A0001
# → {"status": "APPROVED" | "CHANGES_REQUESTED" | "BLOCKED", ...}
```

If an amendment will not land, close it without deleting its history:

```bash
python -m tools.workflow withdraw-amendment A0001 \
    --reason "Owner chose a different direction"
```

The controller records `withdrawal.md` on the amendment branch and marks the
runtime record `ABANDONED`. Both `APPROVED` and `ABANDONED` records are terminal:
they keep the amendment ID consumed for auditability but no longer block the next
amendment, task, or maintenance repair.

The `PROPHET` layer takes no `--task` because it targets no existing
contract; it may create new tasks instead.

```bash
python -m tools.workflow prepare-amendment \
    --layer PROPHET \
    --summary "add Phase 5 for research universe" \
    --owner-direction "..."
```

The `SUPERSEDE` layer retires already-`APPROVED` work:

```bash
python -m tools.workflow prepare-amendment \
    --task T022 --layer SUPERSEDE \
    --summary "retire T022 in favor of T026" \
    --owner-direction "T022 implementation is reused; T026 is the live contract"
```

## Maintenance repairs

For a bounded, low-risk bug outside an active task:

```bash
python -m tools.workflow prepare-maintenance \
    --summary "fix typo in error message" \
    --reason "the user reported the misspelling in <issue>" \
    --path src/<your-package>/foo.py \
    --check "pytest tests/test_foo.py" \
    --related-task T002
# → {maintenance_id, branch, worktree, prompt, agent: "stage-developer", ...}
```

The path list (1–5 entries) is the repair's only writable surface. The
check list (1–8 commands) is what the reviewer verifies.

## Path protection check

```bash
python -m tools.workflow check-paths <base-commit>
# → {"changed_paths": ["..."]}
```

Returns the paths that differ between `base-commit` and the working tree.
Useful before approving a candidate, to verify nothing in the protected
set has drifted.

## Status of a specific record

```bash
python -m tools.workflow amendment-status A0001
python -m tools.workflow maintenance-status M0007
```

## What the controller refuses

- A Developer that has modified a protected path.
- A reviewer that has modified anything other than `.workflow/review-result.json`.
- A planner or prophet that has edited outside its declared layer scope.
- A maintenance developer that has changed any path outside `allowed_paths`.
- Any state transition not in `ALLOWED_TRANSITIONS`.
- Activating a task while an amendment or maintenance repair is in flight.
- Self-approval (the controller refuses if the developer and reviewer
  worktrees share state).
