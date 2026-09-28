# Repository policy for coding agents

This repository contains a reusable development **loop** — prompts and a small
helper — not a governance engine. If a change makes the workflow heavier to run
without making it more honest, it is the wrong change.

## Change boundaries

- The three agent definitions in `.claude/agents/` are the product. They are
  plain markdown on purpose: a project edits them to fit itself, and that is the
  supported extension path, not a fork.
- The helper in `tools/prophet/` stays small and standard-library-only. Changes to
  `_finding_lines` or `check_spec` require tests: those two are the
  incremental-baseline mechanism and the falsifiability gate, and if either
  under-rejects, every slice downstream ships without a real test or without a
  real hypothesis.
- Do not reintroduce a state machine, task-contract freeze, amendment process,
  or structured inter-agent handoff. Each was measured and removed; the numbers
  are in `docs/why-v2.md`.
- Do not add runtime dependencies. The helper must stay installable anywhere.
- Changes to finding extraction in `_finding_lines` require tests. That function
  is the incremental-baseline mechanism; if it under-reports, every regression
  gate in every consuming project becomes decorative.

## Verification

```bash
make check
```

which runs pytest, ruff, and strict mypy. Behavioral changes require a test
covering both the success path and the failure path the change protects. Report
any check you skipped.
