# Changelog

All notable changes to this template are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project adheres to
[Semantic Versioning](https://semver.org/).

## Versioning policy

- **MAJOR** (x.0.0): changes to the state machine, required schema fields,
  the protected-path invariant, the continuation mechanic, or any seam
  semantic that breaks `workflow.yml` compatibility.
- **MINOR** (0.x.0): new commands, new optional `workflow.yml` fields
  (with defaults), new agent roles, new structured-result fields
  (optional). Existing consumer configurations continue to work.
- **PATCH** (0.0.x): bug fixes, internal refactors, documentation. No
  behavior change visible to a consumer's workflow.

## Compatibility matrix

| Template version | Consumer `workflow.yml` version | Notes |
|---|---|---|
| 1.1.x | 1 | Backward-compatible recoverability, review persistence, and project tooling. |
| 1.0.0 | 1 | Initial release. |

## Unreleased

No changes yet.

## 1.1.0 — 2026-09-21

### Added

- Python packaging, local quality commands, CI, repository policies, and a
  regression suite for the reusable controller.
- `withdraw-amendment`, with durable `APPROVED` and `ABANDONED` amendment
  records that do not block later work.
- Backward-compatible `original_base_commit` amendment records for safe retries.

### Fixed

- Persist failed and blocked review state with review evidence in the candidate
  branch instead of discarding the state update.
- Include the previous independent review in task retry prompts.
- Restore missing `PlanRecord` deserialization and `maintenance-status` support.
- Persist maintenance review evidence before resolving its worktree.

## 1.0.0 — initial

First published version.
