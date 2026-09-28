# prophet-workflow

A loop for AI-assisted development, built around one question:

> **What can you run, look at, and judge right now?**

Not "what did the agent report it did." The artifact is the point.

## The loop

```
  SHAPE ──▶ BUILD ──▶ SHOW ──▶ DECIDE ──▶ FOLD
    ▲                                      │
    └──────────────────────────────────────┘
```

| Step | Who | Produces |
|---|---|---|
| **SHAPE** | you + `shaper` | one falsifiable hypothesis in `.prophet/spec.md` |
| **BUILD** | `builder` | a commit on a branch, gates run |
| **SHOW** | `builder` | a runnable artifact you can open or read |
| **DECIDE** | you | keep / redirect / discard |
| **FOLD** | `shaper` | spec rewritten, outcome logged, decisions recorded |

One round = one small, verifiable slice. Not a description of a slice — a
thing you can actually try.

## Why this shape

Two failure modes destroy AI-assisted development, and this design targets both.

**Ceremony on exploration.** Slowing down the uncertain phase means paying full
price for a wrong guess. So uncertainty gets the light path: change the
hypothesis, build, look, adjust.

**No ceremony where it counts.** Shipping something irreversible — a migration,
a credential path, a public API — with the same light touch is how you lose a
week. So irreversibility gets the heavy path, and it is marked *before* the
slice starts.

Concretely:

| Tier | Use for | Process |
|---|---|---|
| **0** disposable | spikes, "does this library even do X" | build, look, delete the branch |
| **1** product (default) | ordinary feature work | full loop, isolated worktree, gates vs. baseline, critic optional |
| **2** irreversible | migrations, money, keys, credentials, deletions, outward sends | extra critic round on the hypothesis *before* coding, rollback path written first, explicit human confirmation |

A tier is assigned by **what the slice does**, not which directory it lives in.
A slice mixing tiers runs Tier 1 for most of it and Tier 2 only for the part
that earns it.

## What you get

- **Three agents.** `builder` implements a slice and produces a runnable
  artifact. `critic` attacks the candidate independently and proposes the next
  hypothesis. `shaper` writes the hypothesis and rewrites the spec each round.
- **Gates with an incremental baseline.** `prophet baseline` records the
  findings you already have. `prophet gates` then fails only if a slice *adds*
  findings. A red baseline does not block anyone; new problems do.
- **Isolation.** `prophet new <name>` creates a worktree and branch per slice,
  so independent slices can run at once.
- **Three files in your repo.** `.prophet/spec.md` (rewritten every round),
  `LOG.md` (what actually happened), `DECISIONS.md` (choices and why).

## What you do not get

No state machine. No task contracts with frozen acceptance criteria. No
amendment process for changing a plan. No JSON handoffs between agents. No
role that exists only to classify failures.

The plan is supposed to change every round. A process that makes changing it
expensive will have you defending a plan you already know is wrong.

The single thing protected from the builder is `.prophet/spec.md` — otherwise a
builder could rewrite the hypothesis it is being measured against. Nothing else
needs protecting: a diff shows what changed.

## Install

```bash
cp -r prophet-workflow/.claude/agents/* your-project/.claude/agents/
cp -r prophet-workflow/tools your-project/

cd your-project
python3 -m tools.prophet init
$EDITOR .prophet/gates.txt      # your test/lint/typecheck commands
python3 -m tools.prophet baseline
```

Or use it straight from here with git submodules. Either way, `.prophet/` is
your project's content; the agents are just text you can edit.

## Running a round

```bash
# 1. SHAPE — with the shaper, until you have a falsifiable hypothesis
#    written into .prophet/spec.md

# 2. BUILD — give the builder the hypothesis and a worktree
python3 -m tools.prophet new report-page
#    → builder implements, runs gates, hands back a commit + an artifact

# 3. SHOW — you open the artifact. Not a summary of it.

# 4. DECIDE — keep / redirect / discard

# 5. FOLD — the shaper rewrites spec.md and appends to LOG.md
python3 -m tools.prophet status
```

Optional, when the slice touches shared surface or the stakes are unclear:

```bash
python3 -m tools.prophet gates --dir .prophet-worktrees/report-page
```

then hand the candidate to the `critic`. It cannot fix anything and cannot
approve; it reports what it found and what the next hypothesis should be.

## Gates

One command per line in `.prophet/gates.txt`; `#` comments ignored.

```bash
pytest tests/ -q
python3 -m ruff check src/
python3 -m mypy src
```

`prophet baseline` refuses to record a baseline if a gate cannot run at all
(missing tool, broken config). That matters more than it sounds: a gate that
fails without producing findings would record an empty baseline and make every
later regression invisible.

Finding identity includes file, line and message, so a finding that merely moved
counts as new. That errs toward strict, which is the safe direction — a slice
gets told about something that is not really new, rather than a real problem
hiding behind a matching old one.

## Adapting it

The three agent files are plain markdown. Edit them for your project — that is
the intended extension mechanism, not a fork. Keep the method, change the
details.

If your tool names subagents differently, keep the bodies and adjust only the
frontmatter.

`tools/prophet/` is optional. Deleting it leaves you with three prompts and a
convention, which is the whole workflow.

## Upgrading

Copy the new agent files over your project. `.prophet/spec.md`, `LOG.md`, and
`DECISIONS.md` are yours and are never touched.

See `CHANGELOG.md` for what changed between versions and `ARCHITECTURE.md` for
the reasoning.

## Development

```bash
make check    # pytest + ruff + mypy
```

## License

No license selected yet. Add one before redistributing.
