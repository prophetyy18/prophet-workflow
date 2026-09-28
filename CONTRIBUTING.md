# Contributing

Python 3.10 or newer.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
```

## What to change

The agent prompts in `.claude/agents/` are the product. When you change one,
say in the PR **which failure mode it prevents** — a prompt change with no
targeted failure is usually a matter of taste and usually wrong.

The helper in `tools/prophet/` stays small and standard-library-only. Changes to
`_finding_lines` require tests: it is the incremental-baseline mechanism, and
if it under-reports, every regression gate downstream becomes decorative.

Do not reintroduce a state machine, task-contract freeze, amendment process, or
structured inter-agent handoff. Each was measured and removed; see
`docs/why-v2.md` for the numbers.

## Hygiene

Do not commit caches, slice worktrees (`.prophet-worktrees/`), or
`.prophet/baseline.json`. `.prophet/spec.md`, `LOG.md`, and `DECISIONS.md` are
project content and belong in version control when present.

For a release, update the version in `pyproject.toml` and `CHANGELOG.md`
together.
