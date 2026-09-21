# Architecture

The workflow separates **pure machinery** from **parameterized policy** from
**project content**. Every piece of code that could vary by project lives
in `workflow.yml`. Everything that must not vary lives in `tools/workflow/`.

## The three layers

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Project content                                    │
│   - todo/config.yaml, todo/phases/P*/T*.md                  │
│   - docs/intent/, docs/spec/, docs/implement/               │
│   - the actual application code                             │
│   - CLAUDE.md, AGENTS.md, README.md                         │
├─────────────────────────────────────────────────────────────┤
│ Layer 2: Parameterized policy (workflow.yml)                │
│   - project name, agent_runtime                             │
│   - protected prefixes and files                            │
│   - maintenance forbidden prefixes and files                 │
│   - required agent definitions                              │
│   - PROPHET editable paths                                  │
│   - runtime dir name, max continuations                     │
├─────────────────────────────────────────────────────────────┤
│ Layer 1: Pure machinery (tools/workflow/)                   │
│   - state machine                                           │
│   - SHA / ID patterns                                       │
│   - git worktree management                                 │
│   - protected path snapshot                                 │
│   - structured result validation                            │
│   - amendment / maintenance lifecycle                       │
│   - continuation mechanics                                  │
└─────────────────────────────────────────────────────────────┘
```

A consumer only edits Layer 3. Layer 2 is set once per project and rarely
touched. Layer 1 is updated by bumping the template version.

## The state machine

```
              ┌──────────┐
              │ PLANNED  │◄──────────────────┐
              └────┬─────┘                   │
                   │ ready                   │
                   ▼                         │
              ┌──────────┐                   │
              │  READY   │                   │
              └────┬─────┘                   │
                   │ prepare-develop         │
                   ▼                         │
              ┌──────────────┐               │
              │IN_DEVELOPMENT│               │
              └─┬────┬───┬───┘               │
                │    │   │                   │
   CANDIDATE_   │    │   │ TRIAGE_           │
   READY        │    │   │ REQUIRED          │
                │    │   └──►────────────┐   │
                │    │ BLOCKED           │   │
                ▼    ▼                   ▼   │
        ┌──────────┐ ┌─────────┐ ┌────────────┐
        │AWAITING_ │ │ BLOCKED │ │   TRIAGE_  │
        │ REVIEW   │ └────┬────┘ │  REQUIRED  │
        └─┬──┬──┬──┘      │      └─┬──┬───┬───┘
          │  │  │         │        │  │   │
   APPROVED│  │  │TRIAGE_  │        │  │   │
          │  │  │REQUIRED │        │  │   │
          │  │  └────┐    │        │  │   │
          ▼  ▼       ▼    ▼        ▼  ▼   ▼
       APPROVED  CHANGES_  ...   (issue-triager routes
                 REQUESTED         to implementation repair,
                                  planning, owner decision,
                                  or external blocker)
```

The full transition table lives in `tools/workflow/core.py` as
`ALLOWED_TRANSITIONS`. Any transition not in the table is illegal.

## The amendment axis

Owner-directed changes happen on a separate axis from task execution. A
task can be `PLANNED` while an amendment is in flight; the amendment never
creates implementation attempts on the target tasks.

Four layers, each with its own author and reviewer:

| Layer | Edits | Authored by | Reviewed by |
|---|---|---|---|
| `CONTRACT` | task contracts and `depends_on` | `planner` | `plan-reviewer` |
| `SPEC` | task contracts + `docs/spec/` | `planner` | `plan-reviewer` |
| `SUPERSEDE` | `superseded_by` field only | `planner` | `plan-reviewer` |
| `PROPHET` | intent, spec, implement, plan restructure, new task creation | `prophet` | `prophet-reviewer` |

The `PROPHET` layer is the only one that may create new task contracts;
it may never modify an existing one. This guarantees that an existing
contract's approval always describes the same candidate.

Amendment records are durable. A successful amendment closes as `APPROVED`; an
Owner can close an unapplied change as `ABANDONED` with `withdraw-amendment`.
Closed records no longer occupy the single amendment lane, but their IDs remain
consumed. Retries also retain `original_base_commit`, so a PROPHET repair may
edit files created by its own earlier attempt while files that predated the
amendment remain frozen.

## The maintenance axis

A bounded, low-risk implementation defect outside an active task can be
repaired without inventing a numbered product task. The repair declares
its `allowed_paths` and `verification_commands` up front; the controller
refuses any change outside that boundary.

Maintenance is **not** an escape hatch for spec or intent changes — those
go through the amendment axis.

## The continuation mechanic

A Developer that runs out of context budget may write a
`CONTINUATION_REQUIRED` result with a structured `continuation` block
(`completed_work`, `remaining_work`, `next_actions`, `changed_paths`).
The Manager then runs `continue-develop`, which spawns a fresh Developer
in the same attempt and worktree. The continuation count is bounded
(typically 1) so the budget is finite.

If the developer's session is killed by a hard turn limit before it can
write the handoff, the Manager may pass `--max-turns-exhausted`. The
controller synthesizes a minimal checkpoint from the actual worktree state.
This flag is **not** a substitute for a proper `CONTINUATION_REQUIRED`
handoff — it only exists because the model cannot speak past its budget.

## The protected path invariant

Before a Developer starts, the controller snapshots every protected file
(`workflow.yml` `protected.prefixes` + `protected.files`). After the
Developer finishes, the controller re-snapshots. If any file in the
protected set has changed, the candidate is refused. The reviewer can
therefore trust that `docs/intent/`, `docs/spec/`, the workflow code, and
the task contracts are exactly as they were at the base commit.

The same snapshot mechanism protects the amendment and maintenance
attempts.

## Upgrade protocol

Template versions follow semantic versioning.

- **PATCH**: bug fixes, doc fixes, internal refactors. No seam changes.
- **MINOR**: new commands, new optional fields, new agent roles. Existing
  `workflow.yml` continues to work unchanged.
- **MAJOR**: state machine changes, schema-required field changes, seam
  semantic changes. Consumer must update `workflow.yml`.

A consumer pins to a version with the `template_version` field in
`workflow.yml`. To upgrade:

```bash
# Fetch the new template version.
git fetch template v1.1.0

# Merge selectively — only Layer 1 directories.
git checkout v1.1.0 -- tools/workflow/ todo/schemas/ .claude/agents/

# Validate.
python -m tools.workflow validate
python -m tools.workflow status
```

The Manager role is responsible for keeping the consumer's content
(`docs/intent/`, `docs/spec/`, task contracts) compatible with the new
template.

## What you must never do

- Edit `tools/workflow/` from a Developer agent. It is protected.
- Edit `docs/intent/` or `docs/spec/` from a Developer agent. They are
  protected.
- Edit task contracts from a Developer agent. They are protected.
- Edit `workflow.yml` from a Developer agent. It is protected.
- Use the maintenance route to make a behavioral change. Maintenance is
  for bugs only.
- Self-approve. The author and the reviewer must be different sessions.
