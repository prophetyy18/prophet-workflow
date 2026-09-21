# Workflow overview

This directory holds the machine-readable plan for the project. It is the
**only** authoritative progress source: code that exists but is not
reflected here has no acceptance evidence.

## Files

- `config.yaml` — JSON-compatible YAML. The controller parses it with the
  Python standard library before any dependency is installed.
- `WORKFLOW.md` — command reference.
- `schemas/` — JSON Schemas for structured agent results.
- `phases/P*/` — task contracts, one folder per phase, one `Tnnn.md` per
  task.
- `evidence/P*/Tnnn/attempt-NNN-developer.json` — sealed developer results.
- `reviews/P*/Tnnn/review-NNN.json` and `.md` — sealed reviewer results.
- `amendments/Annnn/` — owner amendment audit trail.
- `maintenance/Mnnnn/` — maintenance repair audit trail.

## State machine

Twelve states. See `tools/workflow/core.py` for the full transition table.

```
PLANNED → READY → IN_DEVELOPMENT → AWAITING_REVIEW → APPROVED
                ↘                              ↘
                 TRIAGE_REQUIRED → {CHANGES_REQUESTED | PLANNING |
                                     OWNER_DECISION_REQUIRED | BLOCKED}
PLANNING → AWAITING_PLAN_REVIEW → {CHANGES_REQUESTED | PLANNING |
                                    PLAN_REVIEW_BLOCKED}
BLOCKED → READY
APPROVED is terminal.
```

Exactly one task may be non-`{PLANNED, APPROVED}` at any time. The
controller enforces this.

## Contract lifecycle

Every numbered task has a frozen contract at
`todo/phases/<phase>/<task-id>.md`. The contract is what the Developer
implements and what the Reviewer checks against. It contains:

- **Outcome** — what the task delivers.
- **Deliverables** — concrete artifacts (files, test names, schemas).
- **Acceptance** — the test the reviewer runs.
- **Must not** — explicit guardrails.
- **References** — pointers to intent / spec / spec sections.

The contract is frozen at the time the task is `READY`. An Owner
amendment on a `PLANNED` task may rewrite it; once `IN_DEVELOPMENT` has
run, only the Owner can amend it (and only via `PROPHET`).

## Amendment lifecycle

The amendment axis is separate from the task axis. An Owner instruction
creates an `A` record with one of four layers. The planner or prophet
authors the candidate in a worktree, an independent reviewer verifies it,
and the controller merges the candidate on approval.

While an amendment is in flight, no task may move. While a maintenance
repair is in flight, no task may move either, and no amendment may start.

## Continuous invariants

- Exactly one in-flight task at most.
- Exactly one in-flight amendment at most.
- Exactly one in-flight maintenance repair at most.
- No Developer may modify a protected path. The reviewer may not modify
  anything except the structured result file.
- No agent may self-approve.
- Every transition is recorded by the controller; conversation text and
  uncommitted files are never workflow state.
