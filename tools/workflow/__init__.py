"""Deterministic task workflow for isolated development and review agents.

This package is the controller. It owns the mechanical parts of the
workflow and nothing else: state, transitions, worktrees, protected
paths, structured-result validation, and the audit trail. It does not
reason about any project's domain; that is the job of the task
contracts and the agents.

Public entry points:

    WorkflowManager(...)   -- the controller itself
    WorkflowError           -- raised for any controller-level invariant violation

See ARCHITECTURE.md for the layer model and the seam list.
"""

from .core import WorkflowError, WorkflowManager

__all__ = ["WorkflowError", "WorkflowManager"]
