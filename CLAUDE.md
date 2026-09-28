# Claude Code entry point

Follow `AGENTS.md` for repository-wide policy.

The workflow is three agent definitions under `.claude/agents/` — `builder`,
`critic`, `shaper` — plus the convention described in `ARCHITECTURE.md`. There is
no controller and no state machine. `tools/prophet/` is an optional helper.

When a consuming project installs this template, the loop is:

1. `shaper` writes one falsifiable hypothesis into `.prophet/spec.md`
2. `builder` implements it on a branch and produces a runnable artifact
3. the human opens the artifact and decides: keep / redirect / discard
4. `shaper` rewrites `spec.md` and appends the outcome to `.prophet/LOG.md`

Read the loop, not a rulebook. The point is to make each round cheap enough to
repeat, not to constrain it.
