# Architecture

Prophet Workflow is a **loop**, not a state machine. Everything in this
repository exists to make one round of that loop cheap enough to run many
times, and honest enough that the human knows what changed.

## The loop

```
  SHAPE ──▶ BUILD ──▶ SHOW ──▶ DECIDE ──▶ FOLD
    ▲                                      │
    └──────────────────────────────────────┘
```

| Step | Who | Produces |
|---|---|---|
| SHAPE | human + shaper | one falsifiable hypothesis in `.prophet/spec.md` |
| BUILD | builder | a commit on a branch, gates run, results reported |
| SHOW | builder | a runnable artifact a human can open or read |
| DECIDE | human | keep / redirect / discard |
| FOLD | shaper | spec rewritten, outcome appended to `LOG.md`, decisions recorded |

Each round is one small, verifiable slice. "Verifiable" is the load-bearing
word: the human must be able to look at a produced artifact, not read a summary
and take it on faith.

## The central claim: ceremony scales with irreversibility

Most process failures come from spending the wrong amount of ceremony in the
wrong place. This repository's entire risk model follows one rule:

> **Uncertainty and irreversibility are different axes. Weight ceremony by
> irreversibility. Weight exploration by information gain.**

The failure mode this replaces: a light process on irreversible work (ship it
and find out), and a heavy process on uncertain work (six approvals before
learning whether the approach works at all).

### Tier 0 — disposable

Exploration, spikes, one-off scripts, "does this library even do X".

- Loop: BUILD → SHOW, then stop.
- No worktree, no critic, no gates.
- A branch that can be deleted at will.

### Tier 1 — product (the default)

Ordinary feature work. Roughly 95% of a project's slices.

- Full loop, worktree isolated, full gates with an incremental baseline.
- Critic optional; recommended when the slice touches shared surface.
- No approvals, no JSON handoff, no state machine.

### Tier 2 — irreversible

Anything whose cost of being wrong is not bounded by a revert: schema
migrations, public API changes, anything touching money, keys, credentials,
deleting data, or sending anything outward.

- SHAPE gets an extra round: a critic runs against the hypothesis itself
  before any code is written.
- The slice must state its rollback path **before** implementation starts.
- Human confirms explicitly, having read the artifact.
- If a slice mixes tiers, only the Tier 2 part gets this treatment. The
  surrounding work stays Tier 1.

Tiers are assigned by **what the slice does**, not by which module it lives in.
A Tier 2 predicate is narrow; routing it through a slow lane for everything
that imports it is the same mistake as a waterfall.

## What this repository does not contain, and why

Each of these existed in v1. The measurement that motivated removing it is in
`docs/why-v2.md`; the short version:

| Removed | Reason |
|---|---|
| Task state machine (`todo/config.yaml`, 12 states) | State belongs in git. A second source of truth drifts, and repairing the drift consumed more effort than the work it tracked. |
| Amendment axis (4 layers) | Rewriting a plan is the normal case, not an exception. Requiring a four-role ceremony to change a sentence in a document made the document expensive to improve. |
| Structured JSON handoff | Its consumer was another agent. The human now reads an artifact directly, so the intermediate representation is pure cost. |
| Protected-path snapshots | A CI check answers the same question — "did this diff touch something it should not" — without a mechanism to maintain. |
| Retirement semantics (`SUPERSEDE`) | Git already keeps history. A commit does not need to be retired; it needs to be superseded by newer code. |
| Single-active-work lane | Slices that touch disjoint files can proceed at once. Serializing them costs real time and buys nothing. |
| Constitutional / escalation vocabulary | Governance that can be rewritten by the project is not constitutional. Removing the need for it is simpler than defining it. |

**What remains is a defense against one specific failure**: the builder
silently rewriting the project's rules so its own output passes review. That
is prevented by making `.prophet/spec.md` un-writable by the builder, and
nothing else needs protecting.

## Files

```
.claude/agents/builder.md   the slice builder
.claude/agents/critic.md    independent attacker + next-slice proposer
.claude/agents/shaper.md    hypothesis writer + spec rewriter
.prophet/                   created in the consuming project
  spec.md                   current hypothesis — rewritten every round
  LOG.md                    what each round actually showed — append only
  DECISIONS.md              choices and why, one line each
tools/prophet/              optional helper (~100 lines, 5 commands)
```

There is no controller. `tools/prophet/` is a convenience for directory
setup and gate running; deleting it leaves the workflow fully functional,
because the workflow is three prompts and a convention.

## Why there is no machinery

A workflow template that projects must fork is a workflow template that will be
forked. v1 tried to prevent forks by making the machinery constitution — and
guaranteed the fork by making the machinery 2,472 lines of state machine that
every real project needed to modify.

The v2 template contains no machine, so a project that wants different rules
edits its own prompt files. That is the same act as editing `CLAUDE.md`, it
requires no governance process, and it creates no version conflict.

Upgrades work because the prompts are text: copy the new prompt files over an
existing project. `.prophet/spec.md`, `LOG.md`, and `DECISIONS.md` are the
project's content and are never touched by an upgrade.

## Adapting for your tool

The three agents are plain markdown with frontmatter. If your tool uses a
different subagent format, keep the bodies and change only the frontmatter
fields. The bodies carry the actual method; the frontmatter is plumbing.
