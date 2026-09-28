# Why v2

This records the measurements that motivated replacing v1. The point is to keep
the decision falsifiable: if someone proposes reinstating a v1 mechanism, the
numbers below are what they have to argue with.

Measurements come from one project running v1 for 15 days: 470 commits, 88
numbered tasks, 43 amendments, 8 agent roles.

## 1. Governance outgrew the product

| Commit class | Count | Share |
|---|---|---|
| Governance / docs / tooling only | 344 | 73.2% |
| `src/` + `tests/` only | 7 | 1.5% |
| Both | 117 | 24.9% |

By lines: governance added 85k and **deleted 7k — 74% of all deletions in the
repository were to governance files.** `todo/config.yaml` alone was modified 298
times.

Rewriting a plan is the most common event in real development. v1 treated it
as the rarest and most dangerous, requiring four roles and an independent review.

## 2. Retiring one task produced 17 amendments

A single `SUPERSEDE` of task T109 cascaded into 17 amendments, 40% of all
amendments ever created, almost all of them "repoint a depends_on entry from T109
to T112". Each cost a planner, a plan reviewer, a review record, and a commit.

Git already keeps history. A merged commit does not need to be retired; it needs
to be superseded by newer code.

## 3. The template was modified out of existence

v1 shipped 2026-09-21 and was never revised. Within 15 days of real use:

| File | Template | In use |
|---|---|---|
| `tools/workflow/core.py` | 2,472 lines | 4,553 lines (+73 symbols) |
| `.claude/agents/prophet.md` | 77 lines | 176 lines |
| Amendment layers | `CONTRACT`/`SPEC`/`PROPHET`/`SUPERSEDE` | 3 of 4 |

The template could not absorb that. It had to be forked, and once forked there
was no path back. A template whose machinery every project must edit is not a
template.

## 4. The process deadlocked on its own rules

From the project's own `WORKFLOW_RULES_CHANGE_2026-09-20.md`:

> 规则 A: once a task carries `superseded_by`, its line in ARCHITECTURE.md §2.2
> **must** name the successor.
> 规则 B: `finish-amendment` on the SUPERSEDE layer permits **only**
> `todo/config.yaml`.

A requires a document edit; B forbids it. The amendment could not be fixed by
retrying within its own layer. Owner intervention was the only exit, and one
proposal needed two such interventions because deleting the runtime record also
rewound the ID counter and collided with the previous branch name.

The same project's `2026-09-23` proposal found three more dead ends by modelling
the state machine as a graph. The lessons are recorded there as a rule worth
keeping:

> **Every rule must name the layer that satisfies it, and that layer must
> demonstrably have write access to whatever the rule requires.** If no such
> layer exists, it is not a hard rule — it is a hard check placed in the layer
> that can perform it.

v2 applies this by construction: there is no rule without an executor.

## 5. Review passed code that was broken

The project's `CI_GATE_DEBT_2026-09-23.md` documents a candidate that was
APPROVED while introducing both a formatting violation and a real runtime bug
(`TypeError` on cancel of a non-terminal run). The cause was scope, not
diligence:

> Reviews ran `ruff` and `mypy` scoped to their own files. The repository-level
> gates had been red since 2026-09-17 and blocked nothing.

149 mypy errors sat in the repository for six days without blocking a single
commit. A gate that is advisory is decoration.

v2's answer is the incremental baseline, and the helper's test suite pins it:
a gate that fails *without producing findings* is refused at baseline time,
because an empty baseline makes every later regression invisible. This was not
hypothetical — it was the first defect found when the helper was tested.

## 6. Reviewers could see the design was wrong but not change it

One `FAIL` review recorded findings of a design nature — the candidate never
extended `AuditEvent` to carry the cursor it needed, and no end-to-end path
exercised the new surface. Both correct, both unusable: the verdict could only
be `FAIL`, and the only route to a different design ran planner → plan-reviewer
→ review → seal.

The critic in v2 has no verdict to give and nothing to fix. It reports findings
and proposes the next hypothesis. You decide. That is the whole mechanism by
which design changes stop requiring a ceremony.

## 7. A single lane on work that did not collide

Of 25 outstanding tasks, the longest dependency chain was 15 and **the number
of pairs declaring the same `src/` or `tests/` file was zero.** Every one of the
98 overlapping pairs was a documentation file — and documentation conflicts are
cheapest to resolve serially at the end.

The single-active-work lane serialized 25 rounds to deliver work with no code
conflicts at all.

## 8. Strategy text outweighed the work

Read on every task start: `WORKFLOW.md` 39 KB, `todo/README.md` 28 KB,
`PROJECT_GOALS.md` 46 KB, `AGENTS.md` + `CLAUDE.md` + `README.md` 18 KB —
**≈ 39k tokens of policy per task**, plus ≈ 14k tokens of agent definitions.

The configured model had a 1,000,000-token context window. The constraints were
being spent on context that could hold the entire repository.

The same repository already owned the highest-value defense available:
deterministic offline checkers (`check_citations`, `check_imports`,
`check_acceptance`). These are more reliable than any reviewer agent, and v1
ran them selectively. v2 runs them unconditionally and judges by delta.

## What v2 keeps

- Worktree isolation per slice (already worked, cost nothing)
- An independent reviewer, downgraded to advisory with a new duty: propose the
  next hypothesis
- Deterministic gates, upgraded to unconditional and delta-based
- Commit-bound evidence, now in the commit itself rather than a sidecar JSON

## What v2 drops

State machine, task contracts, amendment axis, structured handoffs,
protected-path snapshots, retirement semantics, the single lane, and the
constitutional/escalation vocabulary — with the measurement for each above.
