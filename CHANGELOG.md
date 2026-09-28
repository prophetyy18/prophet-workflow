# Changelog

All notable changes to this template are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## Versioning policy

v2 has no state machine, no schema, and no configuration seam, so the historical
version policy no longer applies. What matters now is only: **do the agent
prompts and the helper's behaviour change in a way a consuming project would
notice?**

- **MAJOR** — the loop's shape changes, an agent's method changes materially,
  or the helper's commands change incompatibly.
- **MINOR** — a new agent, a new helper command, a new finding format parsed.
- **PATCH** — fixes and documentation.

## Compatibility

| Template | Consumer upgrade |
|---|---|
| 2.x | Copy `.claude/agents/*.md` and `tools/prophet/` over the project. `.prophet/spec.md`, `LOG.md`, and `DECISIONS.md` are untouched. |
| 1.x | Not compatible. v1 is a different artifact: see `docs/why-v2.md` for what was removed and why. Migrating means adopting the loop and, if you have numbered task contracts, deciding what to keep as history and what to re-plan. |

## Unreleased

Nothing yet.

## 2.0.0

The loop replaces the state machine. Measurements and reasoning:
`docs/why-v2.md`.

### Added

- Three agents: `builder` (implement a slice, produce a runnable artifact),
  `critic` (attack a candidate, propose the next hypothesis, no verdict and no
  fix permission), `shaper` (write the falsifiable hypothesis, rewrite the spec
  each round, record decisions).
- `.prophet/` convention: `spec.md` (rewritten every round), `LOG.md` (what
  actually happened, append-only), `DECISIONS.md` (choices and why).
- `tools/prophet/`: `init`, `new`, `baseline`, `gates`, `status`, `check`.
- Incremental gate judgement — `baseline` records existing findings, `gates`
  fails only when a slice adds findings. A red baseline blocks nobody.
- `baseline` refuses to record when a gate cannot run at all, since an empty
  baseline would make every later regression invisible.
- Risk tiers (disposable / product / irreversible), assigned by what a slice
  does rather than by which module it lives in.

### Removed

- Task state machine and `todo/config.yaml`. State belongs in git.
- Amendment axis and its four layers. Rewriting a plan is the normal case.
- Structured JSON handoffs. The human now reads an artifact directly.
- Protected-path snapshots. A CI check and a visible diff answer the same
  question without a mechanism to maintain.
- Retirement (`SUPERSEDE`) semantics. Git keeps history on its own.
- The single-active-work lane. Independent slices may proceed at once.
- `workflow.yml` and the whole Layer 1 / 2 / 3 policy split. There is no
  machinery left to parameterize.
- Constitutional and escalation vocabulary. Governance that the project may
  rewrite is not constitutional.

### Changed

- The template no longer ships a controller. A project adapts it by editing its
  own agent files, which is the supported path rather than a fork.
- Runtime dependencies: none. The helper is standard-library only.
