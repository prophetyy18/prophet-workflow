# Repository policy for coding agents

This repository contains reusable workflow machinery, not product code. Keep the
controller generic: project names, model identifiers, protected paths, and domain
rules belong in `workflow.yml` or in the consuming repository.

## Change boundaries

- Preserve the separation documented in `ARCHITECTURE.md`: machinery in
  `tools/workflow/`, project policy in `workflow.yml`, and consumer content in
  `todo/` and `docs/`.
- Do not copy product-specific checks, paths, dependencies, credentials, or model
  names into the controller.
- Behavioral changes require regression tests. Cover the success path and the
  invariant or failure path the change protects.
- Keep persisted records backward-compatible within a major version. New record
  fields should normally be optional when reading old state.
- Never weaken path protection, commit identity checks, role separation, or
  structured-result validation merely to make a test pass.

## Verification

Before handing off a change, run `make check` when development dependencies are
available. At minimum run `python3 -m compileall -q tools scripts tests` and
`python3 -m unittest discover -s tests -v`. Inspect the diff and report skipped
checks explicitly.
