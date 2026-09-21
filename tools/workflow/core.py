"""Generic, commit-bound task workflow controller.

The controller owns only the mechanical layer of the workflow:
state transitions, worktree creation, protected-path snapshots,
structured-result validation, and the audit trail. It does not know
about any project's domain — that lives in the task contracts and
the agent definitions.

All project-specific values (project name, model identity, protected
paths, required agent list, runtime dir) are loaded from
``workflow.yml`` at the repository root. See ``workflow.yml.example``
and ``ARCHITECTURE.md`` for the seam list.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

# ──────────────────────────────────────────────────────────────────────
# Patterns and constants
# ──────────────────────────────────────────────────────────────────────

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
TASK_PATTERN = re.compile(r"^T[0-9]{3}$")
MAINTENANCE_PATTERN = re.compile(r"^M[0-9]{4}$")
AMENDMENT_PATTERN = re.compile(r"^A[0-9]{4}$")
PHASE_PATTERN = re.compile(r"^P[0-9]{2}$")

CONFIG_SCHEMA_VERSION = 1

STATES = frozenset(
    {
        "PLANNED",
        "READY",
        "IN_DEVELOPMENT",
        "AWAITING_REVIEW",
        "CHANGES_REQUESTED",
        "TRIAGE_REQUIRED",
        "PLANNING",
        "AWAITING_PLAN_REVIEW",
        "PLAN_REVIEW_BLOCKED",
        "OWNER_DECISION_REQUIRED",
        "BLOCKED",
        "APPROVED",
    }
)

ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "PLANNED": frozenset({"READY"}),
    "READY": frozenset({"IN_DEVELOPMENT", "BLOCKED"}),
    "IN_DEVELOPMENT": frozenset({"AWAITING_REVIEW", "TRIAGE_REQUIRED", "BLOCKED"}),
    "AWAITING_REVIEW": frozenset({"APPROVED", "CHANGES_REQUESTED", "TRIAGE_REQUIRED", "BLOCKED"}),
    "CHANGES_REQUESTED": frozenset({"IN_DEVELOPMENT", "BLOCKED"}),
    "TRIAGE_REQUIRED": frozenset(
        {"CHANGES_REQUESTED", "PLANNING", "OWNER_DECISION_REQUIRED", "BLOCKED"}
    ),
    "PLANNING": frozenset({"AWAITING_PLAN_REVIEW", "OWNER_DECISION_REQUIRED", "BLOCKED"}),
    "AWAITING_PLAN_REVIEW": frozenset({"CHANGES_REQUESTED", "PLANNING", "PLAN_REVIEW_BLOCKED"}),
    "PLAN_REVIEW_BLOCKED": frozenset({"CHANGES_REQUESTED", "PLANNING", "PLAN_REVIEW_BLOCKED"}),
    "OWNER_DECISION_REQUIRED": frozenset({"PLANNING", "BLOCKED"}),
    "BLOCKED": frozenset({"READY"}),
    "APPROVED": frozenset(),
}

AMENDMENT_LAYERS = frozenset({"CONTRACT", "SPEC", "PROPHET", "SUPERSEDE"})
AMENDMENT_STATES = frozenset(
    {
        "PLANNING",
        "AWAITING_REVIEW",
        "CHANGES_REQUESTED",
        "BLOCKED",
        "APPROVED",
        "ABANDONED",
    }
)
TERMINAL_AMENDMENT_STATES = frozenset({"APPROVED", "ABANDONED"})
MAINTENANCE_STATES = frozenset(
    {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED", "ESCALATED", "BLOCKED"}
)

TRIAGE_CLASSIFICATIONS = frozenset(
    {
        "IMPLEMENTATION_DEFECT",
        "CONTRACT_MISMATCH",
        "SPEC_DEFECT",
        "OWNER_DECISION_REQUIRED",
        "EXTERNAL_BLOCKED",
    }
)

DEVELOPER_OUTCOMES = frozenset(
    {"CANDIDATE_READY", "CONTINUATION_REQUIRED", "TRIAGE_REQUIRED", "BLOCKED"}
)

CONTINUATION_REASONS = frozenset({"TURN_BUDGET"})

REVIEW_VERDICTS = frozenset({"PASS", "FAIL", "BLOCKED"})


# ──────────────────────────────────────────────────────────────────────
# Settings (loaded from workflow.yml)
# ──────────────────────────────────────────────────────────────────────

DEFAULT_SETTINGS: dict[str, Any] = {
    "project": "",
    "template_version": "0.0.0",
    "agent_runtime": {
        "provider": "",
        "model": "",
        "context_window_tokens": 0,
        "allow_model_fallback": False,
    },
    "protected": {"prefixes": [], "files": []},
    "maintenance_forbidden": {"prefixes": [], "files": []},
    "required_agents": [
        "workflow-manager",
        "stage-developer",
        "stage-reviewer",
        "issue-triager",
        "planner",
        "plan-reviewer",
        "prophet",
        "prophet-reviewer",
    ],
    "prophet_editable": {"files": [], "prefixes": []},
    "runtime_dir": "workflow-state",
    "max_development_continuations": 1,
    "amendment_layers": list(AMENDMENT_LAYERS),
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into ``base`` and return a new dict."""
    out: dict[str, Any] = {k: v for k, v in base.items()}
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_settings(repo: Path) -> dict[str, Any]:
    """Load ``workflow.yml`` at the repo root and merge with defaults.

    Returns the merged settings dict. Raises ``WorkflowError`` if the
    file exists but cannot be parsed.
    """
    path = repo / "workflow.yml"
    if not path.is_file():
        return _deep_merge(DEFAULT_SETTINGS, {})
    try:
        # workflow.yml is JSON-compatible YAML; parse with the stdlib.
        import yaml

        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except ImportError:
        # Fall back to JSON if PyYAML is not installed.
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"workflow.yml is not valid YAML and PyYAML is unavailable: {exc}"
            ) from exc
    except Exception as exc:  # yaml.YAMLError or others
        raise WorkflowError(f"workflow.yml is not parseable: {exc}") from exc
    if not isinstance(loaded, dict):
        raise WorkflowError("workflow.yml must contain a mapping at the top level")
    return _deep_merge(DEFAULT_SETTINGS, loaded)


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────


class WorkflowError(RuntimeError):
    """Raised when a workflow invariant would be violated."""


# ──────────────────────────────────────────────────────────────────────
# Subprocess and IO helpers
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    stdout: str
    stderr: str
    returncode: int


def _run(
    args: Sequence[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int | None = None,
) -> CommandResult:
    proc = subprocess.run(
        list(args),
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    result = CommandResult(tuple(args), proc.stdout, proc.stderr, proc.returncode)
    if check and proc.returncode != 0:
        cmd = " ".join(args)
        detail = proc.stderr.strip() or proc.stdout.strip() or "no output"
        raise WorkflowError(f"command failed ({proc.returncode}): {cmd}\n{detail}")
    return result


def _git(repo: Path, *args: str, check: bool = True) -> CommandResult:
    return _run(("git", *args), cwd=repo, check=check)


def _sha(repo: Path, revision: str = "HEAD") -> str:
    value = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
    if not SHA_PATTERN.fullmatch(value):
        raise WorkflowError(f"Git returned an invalid SHA for {revision}: {value!r}")
    return value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkflowError(f"cannot load JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise WorkflowError(f"{path} must contain an object")
    return value


def _require(value: object, *, type_: type, key: str) -> object:
    if isinstance(type_, tuple):
        if not isinstance(value, type_):
            raise WorkflowError(f"{key} has wrong type")
    else:
        if not isinstance(value, type_):
            raise WorkflowError(f"{key} has wrong type")
    return value


def _require_string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise WorkflowError(f"{key} must be a non-empty string")
    return item


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    item = value.get(key)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise WorkflowError(f"{key} must be null or a non-empty string")
    return item


def _require_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool):
        raise WorkflowError(f"{key} must be an integer")
    return item


def _require_string_list(value: object, key: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise WorkflowError(f"{key} must be a list of strings")
    return list(value)


def _snapshot(paths: Sequence[Path], root: Path) -> dict[str, str]:
    snap: dict[str, str] = {}
    for p in paths:
        rel = p.relative_to(root).as_posix()
        snap[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return snap


def _working_tree_changes(root: Path) -> list[str]:
    tracked = _git(root, "diff", "--name-only").stdout.splitlines()
    staged = _git(root, "diff", "--cached", "--name-only").stdout.splitlines()
    untracked = _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
    return sorted(set(tracked + staged + untracked))


def _change_statuses(root: Path, base: str) -> dict[str, str]:
    """Map changed paths to their single-letter Git status against ``base``."""
    statuses: dict[str, str] = {}
    for line in _git(root, "diff", "--name-status", base, "--").stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[-1]:
            statuses[parts[-1]] = parts[0][0]
    for path in _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines():
        statuses.setdefault(path, "A")
    return statuses


# ──────────────────────────────────────────────────────────────────────
# Records
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AttemptRecord:
    task_id: str
    phase: str
    attempt: int
    base_commit: str
    candidate_commit: str | None
    branch: str
    development_worktree: str
    continuation_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "phase": self.phase,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "development_worktree": self.development_worktree,
            "continuation_count": self.continuation_count,
        }

    @classmethod
    def from_dict(cls, v: Mapping[str, object]) -> AttemptRecord:
        cc = v.get("continuation_count", 0)
        if not isinstance(cc, int) or isinstance(cc, bool) or cc < 0:
            raise WorkflowError("continuation_count must be a non-negative integer")
        return cls(
            task_id=_require_string(v, "task_id"),
            phase=_require_string(v, "phase"),
            attempt=_require_int(v, "attempt"),
            base_commit=_require_string(v, "base_commit"),
            candidate_commit=_optional_string(v, "candidate_commit"),
            branch=_require_string(v, "branch"),
            development_worktree=_require_string(v, "development_worktree"),
            continuation_count=cc,
        )


@dataclass(frozen=True)
class PlanRecord:
    task_id: str
    attempt: int
    classification: str
    base_commit: str
    candidate_commit: str

    def to_dict(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "attempt": self.attempt,
            "classification": self.classification,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PlanRecord:
        return cls(
            task_id=_require_string(value, "task_id"),
            attempt=_require_int(value, "attempt"),
            classification=_require_string(value, "classification"),
            base_commit=_require_string(value, "base_commit"),
            candidate_commit=_require_string(value, "candidate_commit"),
        )


@dataclass(frozen=True)
class AmendmentRecord:
    amendment_id: str
    status: str
    attempt: int
    layer: str
    task_ids: tuple[str, ...]
    base_commit: str
    candidate_commit: str | None
    branch: str
    worktree: str
    original_base_commit: str | None = None

    @property
    def freeze_base(self) -> str:
        """Return the commit that predates every attempt of this amendment."""
        return self.original_base_commit or self.base_commit

    def to_dict(self) -> dict[str, object]:
        return {
            "amendment_id": self.amendment_id,
            "status": self.status,
            "attempt": self.attempt,
            "layer": self.layer,
            "task_ids": list(self.task_ids),
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "worktree": self.worktree,
            "original_base_commit": self.original_base_commit,
        }

    @classmethod
    def from_dict(cls, v: Mapping[str, object]) -> AmendmentRecord:
        aid = _require_string(v, "amendment_id")
        if not AMENDMENT_PATTERN.fullmatch(aid):
            raise WorkflowError(f"invalid amendment ID {aid!r}")
        status = _require_string(v, "status")
        if status not in AMENDMENT_STATES:
            raise WorkflowError(f"invalid amendment status {status!r}")
        layer = _require_string(v, "layer")
        if layer not in AMENDMENT_LAYERS:
            raise WorkflowError(f"invalid amendment layer {layer!r}")
        raw_ids = v.get("task_ids")
        if not isinstance(raw_ids, list):
            raise WorkflowError("amendment task_ids must be a list")
        if not raw_ids and layer != "PROPHET":
            raise WorkflowError(f"{layer} amendment requires at least one target task")
        for tid in raw_ids:
            if not isinstance(tid, str) or not TASK_PATTERN.fullmatch(tid):
                raise WorkflowError(f"amendment task_ids contain invalid ID: {tid!r}")
        return cls(
            amendment_id=aid,
            status=status,
            attempt=_require_int(v, "attempt"),
            layer=layer,
            task_ids=tuple(cast(list[str], raw_ids)),
            base_commit=_require_string(v, "base_commit"),
            candidate_commit=_optional_string(v, "candidate_commit"),
            branch=_require_string(v, "branch"),
            worktree=_require_string(v, "worktree"),
            original_base_commit=_optional_string(v, "original_base_commit"),
        )


@dataclass(frozen=True)
class MaintenanceRecord:
    maintenance_id: str
    status: str
    attempt: int
    base_commit: str
    candidate_commit: str | None
    branch: str
    development_worktree: str
    continuation_count: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "maintenance_id": self.maintenance_id,
            "status": self.status,
            "attempt": self.attempt,
            "base_commit": self.base_commit,
            "candidate_commit": self.candidate_commit,
            "branch": self.branch,
            "development_worktree": self.development_worktree,
            "continuation_count": self.continuation_count,
        }

    @classmethod
    def from_dict(cls, v: Mapping[str, object]) -> MaintenanceRecord:
        mid = _require_string(v, "maintenance_id")
        if not MAINTENANCE_PATTERN.fullmatch(mid):
            raise WorkflowError(f"invalid maintenance ID {mid!r}")
        status = _require_string(v, "status")
        if status not in MAINTENANCE_STATES:
            raise WorkflowError(f"invalid maintenance status {status!r}")
        cc = v.get("continuation_count", 0)
        if not isinstance(cc, int) or isinstance(cc, bool) or cc < 0:
            raise WorkflowError("continuation_count must be a non-negative integer")
        return cls(
            maintenance_id=mid,
            status=status,
            attempt=_require_int(v, "attempt"),
            base_commit=_require_string(v, "base_commit"),
            candidate_commit=_optional_string(v, "candidate_commit"),
            branch=_require_string(v, "branch"),
            development_worktree=_require_string(v, "development_worktree"),
            continuation_count=cc,
        )


# ──────────────────────────────────────────────────────────────────────
# WorkflowManager
# ──────────────────────────────────────────────────────────────────────


class WorkflowManager:
    """Run one Developer and one Reviewer at a time against immutable commits."""

    def __init__(
        self,
        repo: Path,
        *,
        worktree_root: Path | None = None,
    ) -> None:
        self.repo = repo.resolve()
        discovered = Path(_git(self.repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        if discovered != self.repo:
            raise WorkflowError(f"run from repository root {discovered}")
        self.worktree_root = (
            worktree_root or self.repo.parent / f"{self.repo.name}-worktrees"
        ).resolve()
        common_git = _git(self.repo, "rev-parse", "--git-common-dir").stdout.strip()
        common_git_path = Path(common_git)
        if not common_git_path.is_absolute():
            common_git_path = self.repo / common_git_path

        self.settings = load_settings(self.repo)
        runtime_dir_name = self.settings.get("runtime_dir") or "workflow-state"
        self.runtime_dir = common_git_path.resolve() / runtime_dir_name

    # ─── Configuration access ─────────────────────────────────────

    @property
    def config_path(self) -> Path:
        return self.repo / "todo" / "config.yaml"

    def load_config(self, root: Path | None = None) -> dict[str, Any]:
        base = root or self.repo
        config = _load_json(base / "todo" / "config.yaml")
        self.validate_config(config, base)
        return config

    def validate_config(self, config: Mapping[str, Any], root: Path | None = None) -> None:
        base = root or self.repo
        if config.get("schema_version") != CONFIG_SCHEMA_VERSION:
            raise WorkflowError("unsupported todo/config.yaml schema version")
        declared_project = self.settings.get("project")
        if declared_project and config.get("project") != declared_project:
            raise WorkflowError(
                f"config.project {config.get('project')!r} does not match "
                f"workflow.yml project {declared_project!r}"
            )
        runtime = config.get("agent_runtime")
        if not isinstance(runtime, dict):
            raise WorkflowError("agent_runtime must be an object")
        expected_runtime = self.settings.get("agent_runtime", {})
        for key, expected in expected_runtime.items():
            if runtime.get(key) != expected:
                raise WorkflowError(
                    f"agent_runtime.{key} {runtime.get(key)!r} does not match "
                    f"workflow.yml {expected!r}"
                )

        tasks = config.get("tasks")
        if not isinstance(tasks, dict):
            raise WorkflowError("tasks must be an object")
        # An empty task map is valid for a freshly bootstrapped project.
        for tid, raw in tasks.items():
            if not isinstance(tid, str) or not TASK_PATTERN.fullmatch(tid):
                raise WorkflowError(f"invalid task ID: {tid!r}")
            if not isinstance(raw, dict):
                raise WorkflowError(f"{tid} record must be an object")
            phase = raw.get("phase")
            status = raw.get("status")
            deps = raw.get("depends_on")
            task_file = raw.get("task_file")
            if not isinstance(phase, str) or not PHASE_PATTERN.fullmatch(phase):
                raise WorkflowError(f"{tid} has invalid phase")
            if status not in STATES:
                raise WorkflowError(f"{tid} has invalid status {status!r}")
            if not isinstance(deps, list) or len(set(deps)) != len(deps):
                raise WorkflowError(f"{tid} dependencies must be a unique list")
            for d in deps:
                if d not in tasks:
                    raise WorkflowError(f"{tid} depends on unknown task {d}")
            if not isinstance(task_file, str) or not (base / task_file).is_file():
                raise WorkflowError(f"{tid} task file does not exist: {task_file!r}")
            sb = raw.get("superseded_by")
            if sb is not None:
                if not isinstance(sb, str) or not TASK_PATTERN.fullmatch(sb):
                    raise WorkflowError(f"{tid} superseded_by must be a task ID")
                if sb == tid:
                    raise WorkflowError(f"{tid} superseded_by must not reference itself")
                if sb not in tasks:
                    raise WorkflowError(f"{tid} superseded_by references unknown task {sb}")
                if status != "APPROVED":
                    raise WorkflowError(
                        f"{tid} superseded_by requires APPROVED status, found {status}"
                    )
            attempt = raw.get("attempt")
            if attempt is None or not isinstance(attempt, int) or isinstance(attempt, bool):
                raise WorkflowError(f"{tid} attempt must be an integer")
            for key in ("base_commit", "candidate_commit", "approved_commit"):
                v = raw.get(key)
                if v is not None and (not isinstance(v, str) or not SHA_PATTERN.fullmatch(v)):
                    raise WorkflowError(f"{tid}.{key} must be null or a full Git SHA")

        active_task = config.get("active_task")
        if active_task is not None:
            if active_task not in tasks:
                raise WorkflowError("active_task does not exist")
            if config.get("active_phase") != tasks[active_task]["phase"]:
                raise WorkflowError("active_phase does not match active_task")
            if config.get("workflow_state") != tasks[active_task]["status"]:
                raise WorkflowError("workflow_state does not match active task status")
        active_states = [
            t for t, task in tasks.items() if task["status"] not in {"PLANNED", "APPROVED"}
        ]
        if active_states and active_states != (
            [active_task] if isinstance(active_task, str) else []
        ):
            raise WorkflowError("exactly the active task may have an in-progress or READY state")
        self._validate_acyclic(tasks)

    def _validate_acyclic(self, tasks: Mapping[str, Any]) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(tid: str) -> None:
            if tid in visiting:
                raise WorkflowError(f"dependency cycle includes {tid}")
            if tid in visited:
                return
            visiting.add(tid)
            for d in tasks[tid]["depends_on"]:
                visit(d)
            visiting.remove(tid)
            visited.add(tid)

        for tid in tasks:
            visit(tid)

    # ─── Repository health ────────────────────────────────────────

    def validate_repository(self) -> None:
        self.load_config()
        for agent in self.settings["required_agents"]:
            if not (self.repo / ".claude" / "agents" / f"{agent}.md").is_file():
                raise WorkflowError(f"{agent} agent definition is missing")
        for name in (
            "config",
            "amendment-result",
            "amendment-review-result",
            "developer-result",
            "review-result",
            "triage-result",
            "planner-result",
            "plan-review-result",
        ):
            path = self.repo / "todo" / "schemas" / f"{name}.schema.json"
            if not path.is_file():
                raise WorkflowError(f"required schema is missing: {path}")

    # ─── Status ───────────────────────────────────────────────────

    def status(self) -> dict[str, object]:
        config = self.load_config()
        active = config.get("active_task")
        runtime = self.load_attempt(active) if isinstance(active, str) else None
        if runtime is not None and Path(runtime.development_worktree).is_dir():
            config = self.load_config(Path(runtime.development_worktree))
        active_plan = self.load_plan(active) if isinstance(active, str) else None
        active_maintenance = [
            r.to_dict()
            for r in self._maintenance_records()
            if r.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        active_amendments = [r.to_dict() for r in self._active_amendment_records()]
        return {
            "active_phase": config.get("active_phase"),
            "active_task": active,
            "workflow_state": config.get("workflow_state"),
            "task_status": config["tasks"][active]["status"] if active else None,
            "attempt": runtime.to_dict() if runtime else None,
            "plan": active_plan.to_dict() if active_plan else None,
            "active_maintenance": active_maintenance,
            "active_amendments": active_amendments,
        }

    # ─── Internal helpers ─────────────────────────────────────────

    def _task(self, config: Mapping[str, Any], task_id: str) -> dict[str, Any]:
        if not TASK_PATTERN.fullmatch(task_id):
            raise WorkflowError(f"invalid task ID {task_id!r}")
        raw = config["tasks"].get(task_id)
        if not isinstance(raw, dict):
            raise WorkflowError(f"unknown task {task_id}")
        return raw

    def _ensure_clean_main(self) -> None:
        if _git(self.repo, "status", "--porcelain").stdout:
            raise WorkflowError("main checkout must be clean before this transition")

    def _check_dependencies(self, config: Mapping[str, Any], task_id: str) -> None:
        task = self._task(config, task_id)
        incomplete = [d for d in task["depends_on"] if config["tasks"][d]["status"] != "APPROVED"]
        if incomplete:
            raise WorkflowError(f"{task_id} has unapproved dependencies: {', '.join(incomplete)}")
        retired = [
            d
            for d in task["depends_on"]
            if config["tasks"][d].get("superseded_by")
            and config["tasks"][d]["superseded_by"] != task_id
        ]
        if retired:
            raise WorkflowError(
                f"{task_id} depends on superseded task(s): "
                f"{', '.join(retired)}; re-point the dependency at the successor"
            )

    def _set_state(
        self, config: dict[str, Any], task_id: str, state: str, **updates: object
    ) -> None:
        if state not in STATES:
            raise WorkflowError(f"invalid target state {state}")
        task = self._task(config, task_id)
        current = task["status"]
        if state != current and state not in ALLOWED_TRANSITIONS[current]:
            raise WorkflowError(f"illegal transition for {task_id}: {current} -> {state}")
        task["status"] = state
        task.update(updates)
        config["active_phase"] = task["phase"]
        config["active_task"] = task_id
        config["workflow_state"] = state

    # ─── Protected path snapshot ──────────────────────────────────

    def _protected_paths(self, root: Path) -> list[Path]:
        files = self.settings.get("protected", {}).get("files", []) or []
        prefixes = self.settings.get("protected", {}).get("prefixes", []) or []
        pathspecs = [*sorted(files), *prefixes]
        out: list[str] = []
        for spec in pathspecs:
            res = _git(
                root,
                "ls-files",
                "--cached",
                "--others",
                "--exclude-standard",
                "--",
                spec,
            ).stdout.splitlines()
            out.extend(line for line in res if line)
        return sorted(set(root / rel for rel in out if (root / rel).is_file()))

    # ─── Record IO ────────────────────────────────────────────────

    def _attempt_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}.json"

    def _plan_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-plan.json"

    def _protected_snapshot_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-protected.json"

    def _continuation_path(self, task_id: str) -> Path:
        return self.runtime_dir / f"{task_id}-continuation.json"

    def save_attempt(self, attempt: AttemptRecord) -> None:
        _write_json(self._attempt_path(attempt.task_id), attempt.to_dict())

    def load_attempt(self, task_id: str | None) -> AttemptRecord | None:
        if task_id is None:
            return None
        path = self._attempt_path(task_id)
        if not path.is_file():
            return None
        return AttemptRecord.from_dict(_load_json(path))

    def save_plan(self, plan: PlanRecord) -> None:
        _write_json(self._plan_path(plan.task_id), plan.to_dict())

    def load_plan(self, task_id: str) -> PlanRecord | None:
        path = self._plan_path(task_id)
        if not path.is_file():
            return None
        return PlanRecord.from_dict(_load_json(path))

    def _amendment_path(self, amendment_id: str) -> Path:
        return self.runtime_dir / f"{amendment_id}.json"

    def _amendment_request_path(self, amendment_id: str) -> Path:
        return self.runtime_dir / f"{amendment_id}-request.json"

    def save_amendment(self, record: AmendmentRecord) -> None:
        _write_json(self._amendment_path(record.amendment_id), record.to_dict())

    def _amendment_records(self) -> list[AmendmentRecord]:
        if not self.runtime_dir.is_dir():
            return []
        return [
            AmendmentRecord.from_dict(_load_json(p))
            for p in sorted(self.runtime_dir.glob("A[0-9][0-9][0-9][0-9].json"))
        ]

    def _active_amendment_records(self) -> list[AmendmentRecord]:
        """Return only amendments that still occupy the single amendment lane."""
        return [
            record
            for record in self._amendment_records()
            if record.status not in TERMINAL_AMENDMENT_STATES
        ]

    def load_amendment(self, amendment_id: str) -> AmendmentRecord | None:
        if not AMENDMENT_PATTERN.fullmatch(amendment_id):
            raise WorkflowError(f"invalid amendment ID {amendment_id!r}")
        path = self._amendment_path(amendment_id)
        if not path.is_file():
            return None
        return AmendmentRecord.from_dict(_load_json(path))

    def _next_amendment_id(self) -> str:
        numbers: set[int] = set()
        amend_root = self.repo / "todo" / "amendments"
        if amend_root.is_dir():
            for p in amend_root.glob("A[0-9][0-9][0-9][0-9]"):
                if p.is_dir() and AMENDMENT_PATTERN.fullmatch(p.name):
                    numbers.add(int(p.name[1:]))
        if self.runtime_dir.is_dir():
            for p in self.runtime_dir.glob("A[0-9][0-9][0-9][0-9].json"):
                if AMENDMENT_PATTERN.fullmatch(p.stem):
                    numbers.add(int(p.stem[1:]))
        number = max(numbers, default=0) + 1
        if number > 9999:
            raise WorkflowError("amendment ID space is exhausted")
        return f"A{number:04d}"

    def _maintenance_path(self, maintenance_id: str) -> Path:
        return self.runtime_dir / f"{maintenance_id}.json"

    def _maintenance_request_path(self, maintenance_id: str) -> Path:
        return self.runtime_dir / f"{maintenance_id}-request.json"

    def _maintenance_records(self) -> list[MaintenanceRecord]:
        if not self.runtime_dir.is_dir():
            return []
        return [
            MaintenanceRecord.from_dict(_load_json(p))
            for p in sorted(self.runtime_dir.glob("M[0-9][0-9][0-9][0-9].json"))
        ]

    def save_maintenance(self, record: MaintenanceRecord) -> None:
        _write_json(self._maintenance_path(record.maintenance_id), record.to_dict())

    def load_maintenance(self, maintenance_id: str) -> MaintenanceRecord | None:
        if not MAINTENANCE_PATTERN.fullmatch(maintenance_id):
            raise WorkflowError(f"invalid maintenance ID {maintenance_id!r}")
        path = self._maintenance_path(maintenance_id)
        if not path.is_file():
            return None
        return MaintenanceRecord.from_dict(_load_json(path))

    def _next_maintenance_id(self) -> str:
        numbers: set[int] = set()
        maint_root = self.repo / "todo" / "maintenance"
        if maint_root.is_dir():
            for p in maint_root.glob("M[0-9][0-9][0-9][0-9]"):
                if p.is_dir() and MAINTENANCE_PATTERN.fullmatch(p.name):
                    numbers.add(int(p.name[1:]))
        if self.runtime_dir.is_dir():
            for p in self.runtime_dir.glob("M[0-9][0-9][0-9][0-9].json"):
                if MAINTENANCE_PATTERN.fullmatch(p.stem):
                    numbers.add(int(p.stem[1:]))
        number = max(numbers, default=0) + 1
        if number > 9999:
            raise WorkflowError("maintenance ID space is exhausted")
        return f"M{number:04d}"

    # ─── ready / prepare-develop / finish-develop / continue / review

    def ready(self, task_id: str) -> str:
        self._ensure_clean_main()
        if self._active_amendment_records():
            raise WorkflowError("cannot activate a task while an amendment is unfinished")
        config = self.load_config()
        task = self._task(config, task_id)
        if task["status"] != "PLANNED":
            raise WorkflowError(f"ready requires PLANNED, found {task['status']}")
        active = config.get("active_task")
        if isinstance(active, str) and config["tasks"][active]["status"] not in {
            "APPROVED",
            "PLANNED",
        }:
            raise WorkflowError(f"cannot activate {task_id} while {active} is unfinished")
        self._check_dependencies(config, task_id)
        self._set_state(config, task_id, "READY")
        _write_json(self.config_path, config)
        _git(self.repo, "add", "todo/config.yaml")
        _git(self.repo, "commit", "-m", f"chore(workflow): mark {task_id} ready")
        return _sha(self.repo)

    def prepare_develop(self, task_id: str, *, retry: bool = False) -> dict[str, object]:
        if retry:
            attempt = self.load_attempt(task_id)
            if attempt is None:
                raise WorkflowError(f"no retained attempt for {task_id}")
            worktree = Path(attempt.development_worktree)
            config = self.load_config(worktree)
            task = self._task(config, task_id)
            if task["status"] == "BLOCKED":
                self._set_state(config, task_id, "READY")
            elif task["status"] != "CHANGES_REQUESTED":
                raise WorkflowError(
                    f"retry requires CHANGES_REQUESTED or resolved BLOCKED, found {task['status']}"
                )
            attempt = AttemptRecord(
                task_id=attempt.task_id,
                phase=attempt.phase,
                attempt=attempt.attempt + 1,
                base_commit=attempt.base_commit,
                candidate_commit=None,
                branch=attempt.branch,
                development_worktree=attempt.development_worktree,
            )
        else:
            self._ensure_clean_main()
            config = self.load_config()
            task = self._task(config, task_id)
            if task["status"] != "READY":
                raise WorkflowError(f"develop requires READY, found {task['status']}")
            self._check_dependencies(config, task_id)
            if self.load_attempt(task_id) is not None:
                raise WorkflowError(f"runtime record already exists for {task_id}")
            base = _sha(self.repo)
            attempt_number = int(task["attempt"]) + 1
            branch = f"workflow/{task_id.lower()}-attempt-{attempt_number:03d}"
            worktree = self.worktree_root / f"dev-{task_id.lower()}-attempt-{attempt_number:03d}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            if worktree.exists():
                raise WorkflowError(f"development worktree path already exists: {worktree}")
            _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
            attempt = AttemptRecord(
                task_id=task_id,
                phase=task["phase"],
                attempt=attempt_number,
                base_commit=base,
                candidate_commit=None,
                branch=branch,
                development_worktree=str(worktree),
            )

        self._set_state(config, task_id, "IN_DEVELOPMENT", attempt=attempt.attempt)
        _write_json(worktree / "todo" / "config.yaml", config)
        before = _snapshot(self._protected_paths(worktree), worktree)
        self.save_attempt(attempt)
        _write_json(self._protected_snapshot_path(task_id), before)
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Implement exactly {task_id}. The frozen contract is {task_file}. "
            f"The approved base is {attempt.base_commit}. This is attempt "
            f"{attempt.attempt}. Work only in {worktree}. Do not commit. Before "
            f"finishing, write the required structured developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'}."
        )
        latest_review = task.get("latest_review")
        review_path = worktree / latest_review if isinstance(latest_review, str) else None
        if retry and review_path is not None and review_path.is_file():
            prompt += (
                f" This attempt repairs the independent review at {review_path}: address "
                "every required change and do not regress checks that already passed."
            )
        return {**attempt.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def finish_develop(self, task_id: str) -> AttemptRecord:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared development attempt")
        worktree = Path(attempt.development_worktree)
        result_path = worktree / ".workflow" / "developer-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"developer result is missing: {result_path}")
        result = _load_json(result_path)
        before = _load_json(self._protected_snapshot_path(task_id))
        return self._finish_develop(attempt, result, before)

    def continue_develop(
        self,
        task_id: str,
        *,
        max_turns_exhausted: bool = False,
    ) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared development attempt")
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        task = self._task(config, task_id)
        if task["status"] != "IN_DEVELOPMENT" or attempt.candidate_commit is not None:
            raise WorkflowError(
                "development continuation requires an unfinished IN_DEVELOPMENT attempt"
            )
        before = _load_json(self._protected_snapshot_path(task_id))
        after = _snapshot(self._protected_paths(worktree), worktree)
        if before != after:
            raise WorkflowError(
                "developer modified a protected workflow, Intent, Spec, or task file"
            )
        checkpoint = self._load_or_create_continuation_checkpoint(
            task_id, worktree, max_turns_exhausted=max_turns_exhausted
        )
        max_cc = int(self.settings.get("max_development_continuations", 1))
        if attempt.continuation_count >= max_cc:
            raise WorkflowError(
                f"development continuation limit reached for {task_id}; preserve the "
                "worktree and ask the Owner whether to expand the cumulative budget "
                "or triage task scope"
            )
        updated = AttemptRecord(
            task_id=attempt.task_id,
            phase=attempt.phase,
            attempt=attempt.attempt,
            base_commit=attempt.base_commit,
            candidate_commit=None,
            branch=attempt.branch,
            development_worktree=attempt.development_worktree,
            continuation_count=attempt.continuation_count + 1,
        )
        self.save_attempt(updated)
        _write_json(self._continuation_path(task_id), checkpoint)
        checkpoint_path = worktree / ".workflow" / "developer-continuation.json"
        _write_json(checkpoint_path, checkpoint)
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        task_file = config["tasks"][task_id]["task_file"]
        prompt = (
            f"Continue exactly {task_id} in the existing attempt {attempt.attempt}. "
            f"The frozen contract is {task_file}; approved base is "
            f"{attempt.base_commit}. Work only in {worktree}. Read the prior "
            f"checkpoint at {checkpoint_path}, then inspect the actual git diff "
            f"and test state because the worktree is authoritative. Do not commit "
            f"or start over. Before finishing, write the required structured "
            f"developer result to {worktree / '.workflow' / 'developer-result.json'}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def _load_or_create_continuation_checkpoint(
        self,
        task_id: str,
        worktree: Path,
        *,
        max_turns_exhausted: bool,
    ) -> dict[str, Any]:
        result_path = worktree / ".workflow" / "developer-result.json"
        if result_path.is_file():
            result = _load_json(result_path)
            self._validate_developer_result(result, task_id)
            if result["outcome"] != "CONTINUATION_REQUIRED":
                raise WorkflowError(
                    "developer result is terminal; use the matching finish command instead"
                )
            return result
        if not max_turns_exhausted:
            raise WorkflowError(
                "continuation requires a CONTINUATION_REQUIRED handoff or the explicit "
                "--max-turns-exhausted flag"
            )
        changed_paths = [
            p
            for p in _working_tree_changes(worktree)
            if not p.startswith(".workflow/") and p != "todo/config.yaml"
        ]
        return {
            "task_id": task_id,
            "outcome": "CONTINUATION_REQUIRED",
            "summary": "The previous Developer exhausted maxTurns before writing a handoff.",
            "commands": [],
            "residual_risks": [
                "The fresh Developer must reconstruct progress from the actual worktree and "
                "rerun all required checks before producing a candidate."
            ],
            "continuation": {
                "reason": "TURN_BUDGET",
                "completed_work": [],
                "remaining_work": ["Inspect the retained worktree and complete the task contract."],
                "next_actions": ["Review git diff and current test state before making new edits."],
                "changed_paths": changed_paths,
            },
        }

    def _finish_develop(
        self,
        attempt: AttemptRecord,
        result: Mapping[str, Any],
        before: Mapping[str, str],
    ) -> AttemptRecord:
        task_id = attempt.task_id
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        self._validate_developer_result(result, task_id)
        if result["outcome"] == "CONTINUATION_REQUIRED":
            raise WorkflowError(
                "continuation handoff requires continue-develop, not finish-develop"
            )
        after = _snapshot(self._protected_paths(worktree), worktree)
        if before != after:
            raise WorkflowError(
                "developer modified a protected workflow, Intent, Spec, or task file"
            )

        evidence_path = (
            worktree
            / "todo"
            / "evidence"
            / attempt.phase
            / task_id
            / f"attempt-{attempt.attempt:03d}-developer.json"
        )
        _write_json(evidence_path, result)
        outcome = result["outcome"]
        if outcome != "CANDIDATE_READY":
            state = {
                "TRIAGE_REQUIRED": "TRIAGE_REQUIRED",
                "BLOCKED": "BLOCKED",
            }[outcome]
            self._set_state(config, task_id, state, base_commit=attempt.base_commit)
            _write_json(worktree / "todo" / "config.yaml", config)
            (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
            (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
            self._continuation_path(task_id).unlink(missing_ok=True)
            _git(worktree, "add", "-A")
            _git(
                worktree,
                "commit",
                "-m",
                f"chore(workflow): record {task_id} {state.lower()}",
            )
            self.save_attempt(attempt)
            return attempt

        self._set_state(config, task_id, "AWAITING_REVIEW", base_commit=attempt.base_commit)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "diff", "--check")
        changed = _git(worktree, "status", "--porcelain").stdout.strip()
        if not changed:
            raise WorkflowError("developer produced no candidate changes")
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
        _git(worktree, "add", "-A")
        _git(
            worktree,
            "commit",
            "-m",
            f"feat({task_id.lower()}): candidate attempt {attempt.attempt}",
        )
        candidate = _sha(worktree)
        attempt = AttemptRecord(
            task_id=attempt.task_id,
            phase=attempt.phase,
            attempt=attempt.attempt,
            base_commit=attempt.base_commit,
            candidate_commit=candidate,
            branch=attempt.branch,
            development_worktree=attempt.development_worktree,
            continuation_count=attempt.continuation_count,
        )
        self.save_attempt(attempt)
        self._protected_snapshot_path(task_id).unlink(missing_ok=True)
        self._continuation_path(task_id).unlink(missing_ok=True)
        return attempt

    def _validate_developer_result(self, result: Mapping[str, Any], task_id: str) -> None:
        for key in ("task_id", "outcome", "summary", "commands", "residual_risks"):
            if key not in result:
                raise WorkflowError(f"developer result missing key: {key}")
        if result["task_id"] != task_id:
            raise WorkflowError("developer result task_id does not match")
        outcome = result["outcome"]
        if outcome not in DEVELOPER_OUTCOMES:
            raise WorkflowError(f"developer result has invalid outcome {outcome!r}")
        if not isinstance(result["summary"], str):
            raise WorkflowError("developer result summary must be a string")
        commands = result["commands"]
        if not isinstance(commands, list):
            raise WorkflowError("developer result commands must be a list")
        for cmd in commands:
            if not isinstance(cmd, dict):
                raise WorkflowError("developer result command must be an object")
            for key in ("command", "result"):
                if key not in cmd or not isinstance(cmd[key], str):
                    raise WorkflowError(f"developer result command.{key} must be a string")
        _require_string_list(result["residual_risks"], "developer result residual_risks")
        if outcome == "TRIAGE_REQUIRED":
            self._validate_triage_request(result.get("triage_request"))
        elif "triage_request" in result:
            raise WorkflowError("triage_request is allowed only for TRIAGE_REQUIRED")
        if outcome == "CONTINUATION_REQUIRED":
            self._validate_continuation(result.get("continuation"))
            if result.get("blocking_question") is not None:
                raise WorkflowError("continuation must not include a blocking question")
        elif "continuation" in result:
            raise WorkflowError("continuation is allowed only for CONTINUATION_REQUIRED")

    def _validate_continuation(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("CONTINUATION_REQUIRED must include continuation")
        for key in ("reason", "completed_work", "remaining_work", "next_actions", "changed_paths"):
            if key not in value:
                raise WorkflowError(f"continuation missing key: {key}")
        if value["reason"] not in CONTINUATION_REASONS:
            raise WorkflowError("continuation reason must be TURN_BUDGET")
        for key in ("completed_work", "remaining_work", "next_actions", "changed_paths"):
            _require_string_list(value[key], f"continuation {key}")
        if not value["remaining_work"] or not value["next_actions"]:
            raise WorkflowError("continuation must identify remaining work and next actions")

    def _validate_triage_request(self, value: object) -> None:
        if not isinstance(value, dict):
            raise WorkflowError("TRIAGE_REQUIRED must include triage_request")
        for key in ("observed_problem", "evidence", "proposed_classification", "requested_change"):
            if key not in value:
                raise WorkflowError(f"triage_request missing key: {key}")
        if not isinstance(value["observed_problem"], str) or not value["observed_problem"]:
            raise WorkflowError("triage_request observed_problem must be a non-empty string")
        _require_string_list(value["evidence"], "triage_request evidence")
        if value["proposed_classification"] not in TRIAGE_CLASSIFICATIONS:
            raise WorkflowError("triage_request classification is invalid")
        if not isinstance(value["requested_change"], str):
            raise WorkflowError("triage_request requested_change must be a string")

    # ─── Review ───────────────────────────────────────────────────

    def prepare_review(self, task_id: str) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        if attempt is None or attempt.candidate_commit is None:
            raise WorkflowError(f"{task_id} has no candidate to review")
        review_worktree = (
            self.worktree_root / f"review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"review worktree already exists: {review_worktree}")
        _git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(review_worktree),
            attempt.candidate_commit,
        )
        prompt = (
            f"Independently review {task_id}. Base commit: {attempt.base_commit}. "
            f"Candidate commit: {attempt.candidate_commit}. Work only in "
            f"{review_worktree}. Verify the exact diff, every acceptance check, "
            f"and the protected-path invariant. Do not fix anything. Write the "
            f"structured review result only to "
            f"{review_worktree / '.workflow' / 'review-result.json'}."
        )
        return {
            **attempt.to_dict(),
            "agent": "stage-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_review(self, task_id: str) -> tuple[str, Path]:
        attempt = self.load_attempt(task_id)
        if attempt is None or attempt.candidate_commit is None:
            raise WorkflowError(f"{task_id} has no candidate to review")
        review_worktree = (
            self.worktree_root / f"review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"review result is missing: {result_path}")
        result = _load_json(result_path)
        state = self._validate_review_result(result, attempt)
        reviewer_changes = [
            p for p in _working_tree_changes(review_worktree) if p != ".workflow/review-result.json"
        ]
        if reviewer_changes:
            raise WorkflowError(
                "reviewer left changes outside its handoff: " + ", ".join(reviewer_changes)
            )
        if _sha(review_worktree) != attempt.candidate_commit:
            raise WorkflowError("review worktree no longer matches candidate")
        recorded = self._record_review_result(attempt, result, state, _render_review(result))
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        return recorded

    def _record_review_result(
        self,
        attempt: AttemptRecord,
        result: Mapping[str, Any],
        state: str,
        rendered: str | None = None,
    ) -> tuple[str, Path]:
        """Persist review evidence and task state in the candidate branch."""
        worktree = Path(attempt.development_worktree)
        config = self.load_config(worktree)
        relative_json = (
            Path("todo")
            / "reviews"
            / attempt.phase
            / attempt.task_id
            / f"review-{attempt.attempt:03d}.json"
        )
        relative_md = relative_json.with_suffix(".md")
        _write_json(worktree / relative_json, result)
        (worktree / relative_md).write_text(
            rendered if rendered is not None else _render_review(result),
            encoding="utf-8",
        )
        updates: dict[str, object] = {
            "base_commit": attempt.base_commit,
            "candidate_commit": attempt.candidate_commit,
            "latest_review": relative_json.as_posix(),
        }
        if state == "APPROVED":
            updates["approved_commit"] = attempt.candidate_commit
        self._set_state(config, attempt.task_id, state, **updates)
        _write_json(worktree / "todo" / "config.yaml", config)
        _git(worktree, "add", "todo/config.yaml", str(relative_json), str(relative_md))
        _git(
            worktree,
            "commit",
            "-m",
            f"chore(workflow): record {attempt.task_id} review {attempt.attempt}",
        )
        if state == "APPROVED":
            self._ensure_clean_main()
            _git(self.repo, "merge", "--ff-only", attempt.branch)
            _git(self.repo, "worktree", "remove", attempt.development_worktree)
            _git(self.repo, "branch", "-d", attempt.branch)
            self._attempt_path(attempt.task_id).unlink(missing_ok=True)
            return state, self.repo / relative_md
        self.save_attempt(attempt)
        return state, worktree / relative_md

    def _validate_review_result(self, result: Mapping[str, Any], attempt: AttemptRecord) -> str:
        for key in (
            "task_id",
            "base_commit",
            "candidate_commit",
            "verdict",
            "summary",
            "checks",
        ):
            if key not in result:
                raise WorkflowError(f"review result missing key: {key}")
        if (
            result["task_id"] != attempt.task_id
            or result["base_commit"] != attempt.base_commit
            or result["candidate_commit"] != attempt.candidate_commit
        ):
            raise WorkflowError("review identity or commits do not match")
        verdict = result["verdict"]
        if verdict not in REVIEW_VERDICTS:
            raise WorkflowError(f"review verdict {verdict!r} is invalid")
        if not isinstance(result["summary"], str):
            raise WorkflowError("review summary must be a string")
        checks = result["checks"]
        if not isinstance(checks, list):
            raise WorkflowError("review checks must be a list")
        for c in checks:
            if not isinstance(c, dict):
                raise WorkflowError("review check must be an object")
            for key in ("id", "status", "finding", "evidence"):
                if key not in c:
                    raise WorkflowError(f"review check missing key: {key}")
            if c["status"] not in {"PASS", "FAIL", "INCONCLUSIVE"}:
                raise WorkflowError("review check status is invalid")
            if not isinstance(c["finding"], str):
                raise WorkflowError("review check finding must be a string")
            _require_string_list(c["evidence"], f"review check {c['id']} evidence")
        _require_string_list(result.get("must_not_violations", []), "must_not_violations")
        _require_string_list(result.get("unknowns", []), "unknowns")
        _require_string_list(result.get("required_changes", []), "required_changes")
        _require_string_list(result.get("residual_risks", []), "residual_risks")
        if verdict == "PASS":
            if result.get("required_changes") or result.get("unknowns"):
                raise WorkflowError("PASS contradicts unresolved findings")
            return "APPROVED"
        return "BLOCKED" if verdict == "BLOCKED" else "CHANGES_REQUESTED"

    # ─── Triage ───────────────────────────────────────────────────

    def prepare_triage(self, task_id: str) -> dict[str, object]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared attempt")
        worktree = Path(attempt.development_worktree)
        if not (worktree / ".workflow" / "developer-result.json").is_file():
            raise WorkflowError("developer result is missing")
        prompt = (
            f"Classify the blocker in {task_id} without editing code. Base commit: "
            f"{attempt.base_commit}. Work only in {worktree}. Write the structured "
            f"triage result only to {worktree / '.workflow' / 'triage-result.json'}."
        )
        return {**attempt.to_dict(), "agent": "issue-triager", "prompt": prompt}

    def finish_triage(self, task_id: str) -> tuple[str, Path]:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared attempt")
        worktree = Path(attempt.development_worktree)
        result_path = worktree / ".workflow" / "triage-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"triage result is missing: {result_path}")
        result = _load_json(result_path)
        if result.get("task_id") != task_id:
            raise WorkflowError("triage result task_id does not match")
        if result.get("classification") not in TRIAGE_CLASSIFICATIONS:
            raise WorkflowError("triage classification is invalid")
        if not isinstance(result.get("summary"), str):
            raise WorkflowError("triage summary must be a string")
        _require_string_list(result.get("evidence", []), "triage evidence")
        if not isinstance(result.get("recommended_action"), str):
            raise WorkflowError("triage recommended_action must be a string")
        # Map classification to next state.
        classification = result["classification"]
        if classification == "IMPLEMENTATION_DEFECT":
            next_state = "CHANGES_REQUESTED"
        elif classification in {"CONTRACT_MISMATCH", "SPEC_DEFECT", "OWNER_DECISION_REQUIRED"}:
            next_state = "OWNER_DECISION_REQUIRED"
        else:  # EXTERNAL_BLOCKED
            next_state = "BLOCKED"
        config = self.load_config()
        self._set_state(config, task_id, next_state)
        _write_json(self.config_path, config)
        _git(self.repo, "add", "-A")
        _git(
            self.repo,
            "commit",
            "-m",
            f"chore(workflow): triage {task_id} -> {next_state}",
        )
        triage_path = (
            self.repo
            / "todo"
            / "evidence"
            / attempt.phase
            / task_id
            / f"triage-{attempt.attempt:03d}.json"
        )
        _write_json(triage_path, result)
        result_path.unlink()
        return next_state, triage_path

    # ─── Planning (triaged issue -> planner) ──────────────────────

    def prepare_plan(self, task_id: str, *, owner_decision: str | None = None) -> dict[str, object]:
        if owner_decision is None:
            raise WorkflowError(
                "prepare-plan requires --owner-decision; planners transcribe, never invent"
            )
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared attempt")
        worktree = Path(attempt.development_worktree)
        prompt = (
            f"Apply planning change to {task_id}. Owner decision: {owner_decision}. "
            f"Work only in {worktree}. Do not implement business code. Write the "
            f"structured planner result to "
            f"{worktree / '.workflow' / 'planner-result.json'}."
        )
        return {
            **attempt.to_dict(),
            "agent": "planner",
            "owner_decision": owner_decision,
            "prompt": prompt,
        }

    def finish_plan(self, task_id: str) -> PlanRecord:
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no prepared attempt")
        worktree = Path(attempt.development_worktree)
        result_path = worktree / ".workflow" / "planner-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"planner result is missing: {result_path}")
        result = _load_json(result_path)
        for key in ("task_id", "classification", "base_commit", "candidate_commit"):
            if key not in result:
                raise WorkflowError(f"planner result missing key: {key}")
        if result["task_id"] != task_id:
            raise WorkflowError("planner result task_id does not match")
        if not SHA_PATTERN.fullmatch(result["base_commit"]):
            raise WorkflowError("planner base_commit is invalid")
        if not SHA_PATTERN.fullmatch(result["candidate_commit"]):
            raise WorkflowError("planner candidate_commit is invalid")
        plan = PlanRecord(
            task_id=task_id,
            attempt=attempt.attempt,
            classification=result["classification"],
            base_commit=result["base_commit"],
            candidate_commit=result["candidate_commit"],
        )
        self.save_plan(plan)
        return plan

    def prepare_plan_review(self, task_id: str) -> dict[str, object]:
        plan = self.load_plan(task_id)
        if plan is None:
            raise WorkflowError(f"{task_id} has no plan to review")
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no attempt record")
        review_worktree = (
            self.worktree_root / f"plan-review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"plan review worktree already exists: {review_worktree}")
        _git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(review_worktree),
            plan.candidate_commit,
        )
        prompt = (
            f"Independently review planner candidate for {task_id}. Base commit: "
            f"{plan.base_commit}. Candidate commit: {plan.candidate_commit}. Work "
            f"only in {review_worktree}. Do not fix anything. Write the structured "
            f"result to {review_worktree / '.workflow' / 'plan-review-result.json'}."
        )
        return {
            **plan.to_dict(),
            "agent": "plan-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_plan_review(self, task_id: str) -> tuple[str, Path]:
        plan = self.load_plan(task_id)
        if plan is None:
            raise WorkflowError(f"{task_id} has no plan to review")
        attempt = self.load_attempt(task_id)
        if attempt is None:
            raise WorkflowError(f"{task_id} has no attempt record")
        review_worktree = (
            self.worktree_root / f"plan-review-{task_id.lower()}-attempt-{attempt.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "plan-review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"plan review result is missing: {result_path}")
        result = _load_json(result_path)
        for key in ("task_id", "base_commit", "candidate_commit", "verdict", "summary"):
            if key not in result:
                raise WorkflowError(f"plan review result missing key: {key}")
        verdict = result["verdict"]
        if verdict not in REVIEW_VERDICTS:
            raise WorkflowError("plan review verdict is invalid")
        if verdict == "PASS":
            self._ensure_clean_main()
            _git(self.repo, "merge", "--ff-only", attempt.branch)
            _git(self.repo, "worktree", "remove", attempt.development_worktree)
            _git(self.repo, "branch", "-d", attempt.branch)
            config = self.load_config()
            self._set_state(config, task_id, "CHANGES_REQUESTED")
            _write_json(self.config_path, config)
            _git(self.repo, "add", "-A")
            _git(self.repo, "commit", "-m", f"chore(workflow): plan accepted for {task_id}")
            self._attempt_path(task_id).unlink(missing_ok=True)
            self._plan_path(task_id).unlink(missing_ok=True)
            return "CHANGES_REQUESTED", self.repo / "todo" / "config.yaml"
        return "CHANGES_REQUESTED", review_worktree / ".workflow" / "plan-review-result.json"

    # ─── check-paths ──────────────────────────────────────────────

    def check_changed_paths(self, base_commit: str) -> list[str]:
        if not SHA_PATTERN.fullmatch(base_commit):
            raise WorkflowError(f"invalid base commit {base_commit!r}")
        return _git(self.repo, "diff", "--name-only", base_commit, "--").stdout.splitlines()

    # ─── Amendment ────────────────────────────────────────────────

    def prepare_amendment(
        self,
        *,
        task_ids: Sequence[str],
        layer: str,
        summary: str,
        owner_direction: str,
    ) -> dict[str, object]:
        self._ensure_clean_main()
        layer = layer.upper()
        if layer not in AMENDMENT_LAYERS:
            raise WorkflowError(
                "amendment layer must be one of " + ", ".join(sorted(AMENDMENT_LAYERS))
            )
        config = self.load_config()
        active = config.get("active_task")
        active_status = config["tasks"][active]["status"] if isinstance(active, str) else None
        if active_status not in {None, "APPROVED", "PLANNED"}:
            raise WorkflowError(f"cannot start an amendment while {active} is unfinished")
        if self._active_amendment_records():
            raise WorkflowError("another amendment is already active")
        active_repairs = [
            r.maintenance_id
            for r in self._maintenance_records()
            if r.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        if active_repairs:
            raise WorkflowError(
                "cannot start an amendment while maintenance is unfinished: "
                + ", ".join(active_repairs)
            )
        normalized = tuple(dict.fromkeys(task_ids))
        if layer == "PROPHET":
            if normalized:
                raise WorkflowError(
                    "a PROPHET amendment takes no --task: it targets no existing contract"
                )
        else:
            if not normalized:
                raise WorkflowError(f"a {layer} amendment requires at least one target task")
            if len(normalized) > 8:
                raise WorkflowError("an amendment may target at most eight tasks")
            required_status = "APPROVED" if layer == "SUPERSEDE" else "PLANNED"
            for tid in normalized:
                task = self._task(config, tid)
                if task["status"] != required_status:
                    raise WorkflowError(
                        f"{layer} amendment targets must be {required_status}, "
                        f"found {tid}={task['status']}"
                    )
        if not summary.strip() or not owner_direction.strip():
            raise WorkflowError("amendment summary and owner direction must be non-empty")
        amendment_id = self._next_amendment_id()
        base = _sha(self.repo)
        branch = f"amendment/{amendment_id.lower()}-attempt-001"
        worktree = self.worktree_root / f"amendment-{amendment_id.lower()}-attempt-001"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise WorkflowError(f"amendment worktree path already exists: {worktree}")
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
        record = AmendmentRecord(
            amendment_id=amendment_id,
            status="PLANNING",
            attempt=1,
            layer=layer,
            task_ids=normalized,
            base_commit=base,
            candidate_commit=None,
            branch=branch,
            worktree=str(worktree),
            original_base_commit=base,
        )
        request: dict[str, object] = {
            "amendment_id": amendment_id,
            "task_ids": list(normalized),
            "layer": layer,
            "summary": summary.strip(),
            "owner_direction": owner_direction.strip(),
        }
        self.save_amendment(record)
        _write_json(self._amendment_request_path(amendment_id), request)
        request_path = worktree / ".workflow" / "amendment-request.json"
        _write_json(request_path, request)
        if layer == "PROPHET":
            agent, scope = (
                "prophet",
                "Apply this Owner-directed change with the PROPHET layer. It restructures "
                "the plan, states Intent, or corrects collateral documents; it targets no "
                "task in particular",
            )
        else:
            agent, scope = (
                "planner",
                f"Apply Owner-directed amendment {amendment_id} to exactly these tasks: "
                f"{', '.join(normalized)}",
            )
        prompt = (
            f"{scope}. Read {request_path}. Layer: {layer}. Work only in {worktree}. "
            f"Do not implement business code or change workflow state. Make only the "
            f"smallest planning changes required by the recorded Owner direction. "
            f"Write the structured result only to "
            f"{worktree / '.workflow' / 'amendment-result.json'}."
        )
        return {**record.to_dict(), "agent": agent, "prompt": prompt}

    def finish_amendment(self, amendment_id: str) -> AmendmentRecord:
        record = self.load_amendment(amendment_id)
        if record is None or record.status != "PLANNING":
            raise WorkflowError("finish-amendment requires an active PLANNING amendment")
        worktree = Path(record.worktree)
        result_path = worktree / ".workflow" / "amendment-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"amendment result is missing: {result_path}")
        result = _load_json(result_path)
        outcome = self._validate_amendment_result(result, amendment_id)
        changed = [p for p in _working_tree_changes(worktree) if not p.startswith(".workflow/")]
        if record.layer == "PROPHET":
            base_config = self.load_config()
            candidate_config = self.load_config(worktree)
            if not set(base_config["tasks"]).issubset(candidate_config["tasks"]):
                raise WorkflowError("prophet change deleted an existing task")
            comparable = cast(dict[str, Any], json.loads(json.dumps(candidate_config)))
            comparable.pop("intent_revision", None)
            comparable.pop("spec_revision", None)
            comparable["tasks"] = {
                task_id: comparable["tasks"][task_id] for task_id in base_config["tasks"]
            }
            frozen = cast(dict[str, Any], json.loads(json.dumps(base_config)))
            frozen.pop("intent_revision", None)
            frozen.pop("spec_revision", None)
            if comparable != frozen:
                raise WorkflowError(
                    "prophet changed an existing task, workflow state, runtime identity, "
                    "or another frozen config field"
                )
            allowed_prefixes = self.settings.get("prophet_editable", {}).get("prefixes", []) or []
            allowed_files = set(self.settings.get("prophet_editable", {}).get("files", []) or [])
            statuses = _change_statuses(worktree, record.freeze_base)
            forbidden = [
                p
                for p in changed
                if p != "todo/config.yaml"
                and not (
                    statuses.get(p) != "D"
                    and (
                        p in allowed_files
                        or any(p.startswith(pref) for pref in allowed_prefixes)
                        or (_is_task_contract_path(p) and statuses.get(p) == "A")
                    )
                )
            ]
            if forbidden:
                raise WorkflowError(
                    "prophet change altered paths outside its scope: " + ", ".join(forbidden)
                )
        else:
            base_config = self.load_config()
            allowed_contracts = {base_config["tasks"][tid]["task_file"] for tid in record.task_ids}
            if record.layer == "SUPERSEDE":
                allowed = {"todo/config.yaml"}
            else:
                allowed = allowed_contracts | {"todo/config.yaml"}
            if record.layer == "SPEC":
                allowed |= {p for p in changed if p.startswith("docs/spec/")}
            forbidden = [p for p in changed if p not in allowed]
            if forbidden:
                raise WorkflowError(
                    "planner changed paths outside the amendment: " + ", ".join(forbidden)
                )
        if outcome == "NO_CHANGE_REQUIRED" and changed:
            raise WorkflowError("NO_CHANGE_REQUIRED contradicts planner file changes")
        relative_root = Path("todo") / "amendments" / amendment_id
        _write_json(
            worktree / relative_root / "request.json",
            _load_json(self._amendment_request_path(amendment_id)),
        )
        author = "prophet" if record.layer == "PROPHET" else "planner"
        _write_json(worktree / relative_root / f"{author}-{record.attempt:03d}.json", result)
        result_path.unlink()
        (worktree / ".workflow" / "amendment-request.json").unlink(missing_ok=True)
        if outcome == "BLOCKED":
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"chore(amendment): record {amendment_id} blocked")
            updated = replace(record, status="BLOCKED")
            self.save_amendment(updated)
            return updated
        _git(worktree, "diff", "--check")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"docs({amendment_id.lower()}): amendment candidate")
        updated = AmendmentRecord(
            amendment_id=record.amendment_id,
            status="AWAITING_REVIEW",
            attempt=record.attempt,
            layer=record.layer,
            task_ids=record.task_ids,
            base_commit=record.base_commit,
            candidate_commit=_sha(worktree),
            branch=record.branch,
            worktree=record.worktree,
            original_base_commit=record.freeze_base,
        )
        self.save_amendment(updated)
        return updated

    def _validate_amendment_result(self, result: Mapping[str, Any], amendment_id: str) -> str:
        for key in ("amendment_id", "outcome", "summary", "rationale", "unresolved_questions"):
            if key not in result:
                raise WorkflowError(f"amendment result missing key: {key}")
        if result["amendment_id"] != amendment_id:
            raise WorkflowError("amendment result identity does not match")
        outcome = result["outcome"]
        if outcome not in {"AMENDMENT_READY", "NO_CHANGE_REQUIRED", "BLOCKED"}:
            raise WorkflowError(f"amendment result outcome {outcome!r} is invalid")
        if not isinstance(result["summary"], str) or not isinstance(result["rationale"], str):
            raise WorkflowError("amendment summary and rationale must be strings")
        _require_string_list(result["unresolved_questions"], "amendment unresolved_questions")
        return cast(str, outcome)

    def prepare_amendment_review(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is None or record.status != "AWAITING_REVIEW" or record.candidate_commit is None:
            raise WorkflowError("amendment review requires an AWAITING_REVIEW candidate")
        worktree = Path(record.worktree)
        if (
            _sha(worktree) != record.candidate_commit
            or _git(worktree, "status", "--porcelain").stdout
        ):
            raise WorkflowError("amendment worktree must exactly match its clean candidate")
        review_worktree = (
            self.worktree_root / f"amendment-review-{amendment_id.lower()}-{record.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"amendment review worktree already exists: {review_worktree}")
        _git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(review_worktree),
            record.candidate_commit,
        )
        if record.layer == "PROPHET":
            subject = (
                f"Independently review PROPHET change {amendment_id}, which restates a "
                "goal, restructures the plan, or corrects high-level documents. It "
                "targets no task."
            )
            reviewer = "prophet-reviewer"
        else:
            subject = (
                f"Independently review Owner amendment {amendment_id}. Target tasks: "
                f"{', '.join(record.task_ids)}."
            )
            reviewer = "plan-reviewer"
        prompt = (
            f"{subject} Base commit: {record.base_commit}. Candidate commit: "
            f"{record.candidate_commit}. Work only in {review_worktree}; do not edit "
            f"planning files. Write the structured result only to "
            f"{review_worktree / '.workflow' / 'amendment-review-result.json'}."
        )
        return {
            **record.to_dict(),
            "agent": reviewer,
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_amendment_review(self, amendment_id: str) -> tuple[str, Path]:
        record = self.load_amendment(amendment_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{amendment_id} has no amendment candidate")
        review_worktree = (
            self.worktree_root / f"amendment-review-{amendment_id.lower()}-{record.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "amendment-review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"amendment review result is missing: {result_path}")
        result = _load_json(result_path)
        state = self._validate_amendment_review_result(result, record)
        if state == "APPROVED":
            self._ensure_clean_main()
        if [
            p
            for p in _working_tree_changes(review_worktree)
            if p != ".workflow/amendment-review-result.json"
        ]:
            raise WorkflowError("amendment reviewer left changes outside its handoff")
        if _sha(review_worktree) != record.candidate_commit:
            raise WorkflowError("amendment review worktree no longer matches candidate")
        worktree = Path(record.worktree)
        relative_root = Path("todo") / "amendments" / amendment_id
        review_name = f"review-{record.attempt:03d}"
        _write_json(worktree / relative_root / f"{review_name}.json", result)
        report = worktree / relative_root / f"{review_name}.md"
        report.write_text(_render_amendment_review(result), encoding="utf-8")
        _git(
            worktree,
            "add",
            str(relative_root / f"{review_name}.json"),
            str(relative_root / f"{review_name}.md"),
        )
        _git(worktree, "commit", "-m", f"chore(amendment): record {amendment_id} review")
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        if state == "APPROVED":
            _git(self.repo, "merge", "--ff-only", record.branch)
            _git(self.repo, "worktree", "remove", record.worktree)
            _git(self.repo, "branch", "-d", record.branch)
            self.save_amendment(replace(record, status="APPROVED"))
            self._amendment_request_path(amendment_id).unlink(missing_ok=True)
            return state, self.repo / relative_root / f"{review_name}.md"
        updated = replace(record, status=state)
        self.save_amendment(updated)
        return state, report

    def _validate_amendment_review_result(
        self, result: Mapping[str, Any], record: AmendmentRecord
    ) -> str:
        for key in (
            "amendment_id",
            "base_commit",
            "candidate_commit",
            "verdict",
            "summary",
            "required_changes",
            "unknowns",
        ):
            if key not in result:
                raise WorkflowError(f"amendment review result missing key: {key}")
        if (
            result["amendment_id"] != record.amendment_id
            or result["base_commit"] != record.base_commit
            or result["candidate_commit"] != record.candidate_commit
        ):
            raise WorkflowError("amendment review identity or commits do not match")
        verdict = result["verdict"]
        if verdict not in REVIEW_VERDICTS:
            raise WorkflowError("amendment review verdict is invalid")
        if not isinstance(result["summary"], str):
            raise WorkflowError("amendment review summary must be a string")
        _require_string_list(result["required_changes"], "amendment review required_changes")
        _require_string_list(result["unknowns"], "amendment review unknowns")
        if verdict == "PASS":
            if result["required_changes"] or result["unknowns"]:
                raise WorkflowError("PASS contradicts unresolved findings")
            return "APPROVED"
        return "BLOCKED" if verdict == "BLOCKED" else "CHANGES_REQUESTED"

    def prepare_amendment_retry(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is None or record.status not in {"CHANGES_REQUESTED", "BLOCKED"}:
            raise WorkflowError("amendment retry requires CHANGES_REQUESTED or resolved BLOCKED")
        worktree = Path(record.worktree)
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("amendment worktree must be clean before retry")
        updated = AmendmentRecord(
            amendment_id=record.amendment_id,
            status="PLANNING",
            attempt=record.attempt + 1,
            layer=record.layer,
            task_ids=record.task_ids,
            base_commit=_sha(worktree),
            candidate_commit=None,
            branch=record.branch,
            worktree=record.worktree,
            original_base_commit=record.freeze_base,
        )
        self.save_amendment(updated)
        request = _load_json(self._amendment_request_path(amendment_id))
        request_path = worktree / ".workflow" / "amendment-request.json"
        _write_json(request_path, request)
        if record.layer == "PROPHET":
            author = "prophet"
            scope = f"Repair PROPHET change {amendment_id} after independent review"
        else:
            author = "planner"
            scope = f"Repair Owner amendment {amendment_id} after independent review"
        prompt = (
            f"{scope}. Read {request_path} and prior review under "
            f"todo/amendments/{amendment_id}/review-{record.attempt:03d}.json. Work "
            f"only in {worktree}; preserve the layer and, for a task-targeted layer, "
            f"the target tasks. Write the structured result only to "
            f"{worktree / '.workflow' / 'amendment-result.json'}."
        )
        return {**updated.to_dict(), "agent": author, "prompt": prompt}

    def withdraw_amendment(self, amendment_id: str, *, reason: str) -> dict[str, object]:
        """Close an amendment without applying it while preserving its audit record."""
        record = self.load_amendment(amendment_id)
        if record is None:
            raise WorkflowError(f"unknown amendment {amendment_id}")
        if record.status in TERMINAL_AMENDMENT_STATES:
            raise WorkflowError(f"amendment {amendment_id} is already closed ({record.status})")
        if not reason.strip():
            raise WorkflowError("withdraw-amendment requires a non-empty reason")
        worktree = Path(record.worktree)
        relative = Path("todo") / "amendments" / amendment_id / "withdrawal.md"
        target = worktree / relative
        if worktree.is_dir():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "\n".join(
                    [
                        f"# {amendment_id} withdrawal",
                        "",
                        f"- Layer: `{record.layer}`",
                        f"- Status at withdrawal: `{record.status}`",
                        f"- Attempt: {record.attempt}",
                        "",
                        "## Reason",
                        "",
                        reason.strip(),
                        "",
                        "Nothing from this amendment was applied.",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            _git(worktree, "add", str(relative))
            _git(worktree, "commit", "-m", f"chore(amendment): withdraw {amendment_id}")
        closed = replace(record, status="ABANDONED")
        self.save_amendment(closed)
        self._amendment_request_path(amendment_id).unlink(missing_ok=True)
        return {**closed.to_dict(), "withdrawal": str(target)}

    def amendment_status(self, amendment_id: str) -> dict[str, object]:
        record = self.load_amendment(amendment_id)
        if record is not None:
            return record.to_dict()
        root = self.repo / "todo" / "amendments" / amendment_id
        reviews = sorted(root.glob("review-*.json")) if root.is_dir() else []
        if reviews and _load_json(reviews[-1]).get("verdict") == "PASS":
            return {"amendment_id": amendment_id, "status": "APPROVED"}
        raise WorkflowError(f"unknown amendment {amendment_id}")

    # ─── Maintenance ──────────────────────────────────────────────

    def prepare_maintenance(
        self,
        *,
        summary: str,
        reason: str,
        allowed_paths: Sequence[str],
        verification_commands: Sequence[str],
        related_task: str | None = None,
    ) -> dict[str, object]:
        self._ensure_clean_main()
        if self._active_amendment_records():
            raise WorkflowError("cannot start maintenance while an amendment is unfinished")
        config = self.load_config()
        active = config.get("active_task")
        if isinstance(active, str) and config["tasks"][active]["status"] not in {
            "APPROVED",
            "PLANNED",
        }:
            raise WorkflowError(f"cannot start maintenance while {active} is unfinished")
        active_repairs = [
            r.maintenance_id
            for r in self._maintenance_records()
            if r.status in {"IN_DEVELOPMENT", "AWAITING_REVIEW", "CHANGES_REQUESTED"}
        ]
        if active_repairs:
            raise WorkflowError(
                "cannot start maintenance while another repair is active: "
                + ", ".join(active_repairs)
            )
        if related_task is not None:
            task = self._task(config, related_task)
            if task["status"] != "APPROVED":
                raise WorkflowError("maintenance may reference only an APPROVED task")
        paths = self._validate_maintenance_paths(allowed_paths)
        if (
            not verification_commands
            or len(verification_commands) > 8
            or not all(c.strip() for c in verification_commands)
        ):
            raise WorkflowError("maintenance requires between one and eight verification commands")
        maintenance_id = self._next_maintenance_id()
        base = _sha(self.repo)
        request: dict[str, object] = {
            "maintenance_id": maintenance_id,
            "summary": summary.strip(),
            "reason": reason.strip(),
            "allowed_paths": paths,
            "verification_commands": list(verification_commands),
            "related_task": related_task,
            "risk_attestation": "LOW_RISK_IMPLEMENTATION_DEFECT",
        }
        branch = f"maintenance/{maintenance_id.lower()}-attempt-001"
        worktree = self.worktree_root / f"maintenance-{maintenance_id.lower()}-attempt-001"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if worktree.exists():
            raise WorkflowError(f"maintenance worktree path already exists: {worktree}")
        _git(self.repo, "worktree", "add", "-b", branch, str(worktree), base)
        record = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="IN_DEVELOPMENT",
            attempt=1,
            base_commit=base,
            candidate_commit=None,
            branch=branch,
            development_worktree=str(worktree),
        )
        self.save_maintenance(record)
        _write_json(self._maintenance_request_path(maintenance_id), request)
        _write_json(worktree / ".workflow" / "maintenance-request.json", request)
        prompt = (
            f"Implement low-risk maintenance repair {maintenance_id}. Read the "
            f"frozen request at {worktree / '.workflow' / 'maintenance-request.json'}. "
            f"Base commit: {base}. Work only in {worktree}; change only the explicit "
            f"allowed_paths and do not commit. Run the listed verification commands "
            f"once. Write the standard developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'} with "
            f"task_id={maintenance_id}. If the repair changes behavior, public "
            f"interfaces, dependencies, Intent, Spec, safety policy, or requires "
            f"another path, return TRIAGE_REQUIRED instead of widening scope."
        )
        return {**record.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def _validate_maintenance_paths(self, allowed_paths: Sequence[str]) -> list[str]:
        if not allowed_paths or len(allowed_paths) > 5:
            raise WorkflowError("maintenance requires between one and five explicit paths")
        forbidden_files = set(self.settings.get("maintenance_forbidden", {}).get("files", []) or [])
        forbidden_prefixes = list(
            self.settings.get("maintenance_forbidden", {}).get("prefixes", []) or []
        )
        normalized: list[str] = []
        for raw in allowed_paths:
            p = Path(raw)
            if p.is_absolute() or raw != p.as_posix() or ".." in p.parts:
                raise WorkflowError(f"maintenance path must be normalized and relative: {raw!r}")
            if any(ch in raw for ch in "*?[]"):
                raise WorkflowError(f"maintenance path must not contain a glob: {raw!r}")
            if raw in forbidden_files or any(raw.startswith(pref) for pref in forbidden_prefixes):
                raise WorkflowError(f"maintenance path is high-risk or protected: {raw}")
            resolved = self.repo / raw
            if resolved.exists() and not resolved.is_file():
                raise WorkflowError(f"maintenance path must identify a file: {raw}")
            normalized.append(raw)
        if len(set(normalized)) != len(normalized):
            raise WorkflowError("maintenance paths must be unique")
        return normalized

    def finish_maintenance_develop(self, maintenance_id: str) -> MaintenanceRecord:
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status != "IN_DEVELOPMENT":
            raise WorkflowError("maintenance development requires IN_DEVELOPMENT")
        worktree = Path(record.development_worktree)
        request = _load_json(self._maintenance_request_path(maintenance_id))
        result_path = worktree / ".workflow" / "developer-result.json"
        if not result_path.is_file():
            raise WorkflowError("developer result is missing")
        result = _load_json(result_path)
        self._validate_developer_result(result, maintenance_id)
        if result["outcome"] == "CONTINUATION_REQUIRED":
            raise WorkflowError(
                "continuation handoff requires continue-maintenance-develop, "
                "not finish-maintenance-develop"
            )
        allowed = set(request["allowed_paths"])
        changed = [
            p
            for p in _working_tree_changes(worktree)
            if p
            not in {
                ".workflow/developer-result.json",
                ".workflow/developer-continuation.json",
                ".workflow/maintenance-request.json",
            }
        ]
        forbidden = sorted(set(changed) - allowed)
        if forbidden:
            raise WorkflowError(
                "maintenance developer changed paths outside the request: " + ", ".join(forbidden)
            )
        if result["outcome"] != "CANDIDATE_READY":
            status = "ESCALATED" if result["outcome"] == "TRIAGE_REQUIRED" else "BLOCKED"
            updated = MaintenanceRecord(
                maintenance_id=record.maintenance_id,
                status=status,
                attempt=record.attempt,
                base_commit=record.base_commit,
                candidate_commit=record.candidate_commit,
                branch=record.branch,
                development_worktree=record.development_worktree,
                continuation_count=record.continuation_count,
            )
            self.save_maintenance(updated)
            (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
            self._continuation_path(maintenance_id).unlink(missing_ok=True)
            return updated
        if not changed:
            raise WorkflowError("maintenance developer produced no requested file change")
        relative_root = Path("todo") / "maintenance" / maintenance_id
        _write_json(worktree / relative_root / "request.json", request)
        _write_json(worktree / relative_root / f"developer-{record.attempt:03d}.json", result)
        result_path.unlink()
        (worktree / ".workflow" / "developer-continuation.json").unlink(missing_ok=True)
        (worktree / ".workflow" / "maintenance-request.json").unlink(missing_ok=True)
        _git(worktree, "diff", "--check")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-m", f"fix({maintenance_id.lower()}): {request['summary']}")
        candidate = _sha(worktree)
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="AWAITING_REVIEW",
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=candidate,
            branch=record.branch,
            development_worktree=record.development_worktree,
            continuation_count=record.continuation_count,
        )
        self.save_maintenance(updated)
        self._continuation_path(maintenance_id).unlink(missing_ok=True)
        return updated

    def continue_maintenance_develop(
        self,
        maintenance_id: str,
        *,
        max_turns_exhausted: bool = False,
    ) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status != "IN_DEVELOPMENT" or record.candidate_commit is not None:
            raise WorkflowError(
                "maintenance continuation requires an unfinished IN_DEVELOPMENT attempt"
            )
        worktree = Path(record.development_worktree)
        request = _load_json(self._maintenance_request_path(maintenance_id))
        allowed = set(request["allowed_paths"])
        changed = [p for p in _working_tree_changes(worktree) if not p.startswith(".workflow/")]
        forbidden = sorted(set(changed) - allowed)
        if forbidden:
            raise WorkflowError(
                "maintenance developer changed paths outside the request: " + ", ".join(forbidden)
            )
        checkpoint = self._load_or_create_continuation_checkpoint(
            maintenance_id, worktree, max_turns_exhausted=max_turns_exhausted
        )
        max_cc = int(self.settings.get("max_development_continuations", 1))
        if record.continuation_count >= max_cc:
            raise WorkflowError(
                f"continuation limit reached for {maintenance_id}; preserve the "
                "worktree and ask the Owner whether to expand the cumulative budget "
                "or escalate"
            )
        updated = MaintenanceRecord(
            maintenance_id=record.maintenance_id,
            status=record.status,
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=None,
            branch=record.branch,
            development_worktree=record.development_worktree,
            continuation_count=record.continuation_count + 1,
        )
        self.save_maintenance(updated)
        _write_json(self._continuation_path(maintenance_id), checkpoint)
        checkpoint_path = worktree / ".workflow" / "developer-continuation.json"
        _write_json(checkpoint_path, checkpoint)
        (worktree / ".workflow" / "developer-result.json").unlink(missing_ok=True)
        prompt = (
            f"Continue low-risk maintenance repair {maintenance_id} in existing "
            f"attempt {record.attempt}. Read the frozen request at "
            f"{worktree / '.workflow' / 'maintenance-request.json'} and the prior "
            f"checkpoint at {checkpoint_path}. Inspect the actual git diff and test "
            f"state because the worktree is authoritative. Preserve the original "
            f"allowed_paths and base {record.base_commit}. Work only in {worktree}, "
            f"do not commit, and write the standard developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'} with "
            f"task_id={maintenance_id}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def prepare_maintenance_retry(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is None:
            raise WorkflowError(f"unknown maintenance repair {maintenance_id}")
        if record.status not in {"CHANGES_REQUESTED", "BLOCKED"}:
            raise WorkflowError("maintenance retry requires CHANGES_REQUESTED or resolved BLOCKED")
        worktree = Path(record.development_worktree)
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("maintenance worktree must be clean before retry")
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="IN_DEVELOPMENT",
            attempt=record.attempt + 1,
            base_commit=record.base_commit,
            candidate_commit=None,
            branch=record.branch,
            development_worktree=record.development_worktree,
        )
        self.save_maintenance(updated)
        request_path = worktree / "todo" / "maintenance" / maintenance_id / "request.json"
        prompt = (
            f"Repair maintenance candidate {maintenance_id} after independent review. "
            f"Read the frozen request at {request_path} and prior review at "
            f"{worktree / 'todo' / 'maintenance' / maintenance_id / f'review-{record.attempt:03d}.json'}. "
            f"This is attempt {updated.attempt}; preserve the original allowed_paths "
            f"and base {updated.base_commit}. Work only in {worktree}, do not commit, "
            f"and write the standard developer result to "
            f"{worktree / '.workflow' / 'developer-result.json'} with "
            f"task_id={maintenance_id}."
        )
        return {**updated.to_dict(), "agent": "stage-developer", "prompt": prompt}

    def prepare_maintenance_review(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{maintenance_id} has no candidate to review")
        if record.status != "AWAITING_REVIEW":
            raise WorkflowError("maintenance review requires AWAITING_REVIEW")
        worktree = Path(record.development_worktree)
        if _sha(worktree) != record.candidate_commit:
            raise WorkflowError("maintenance worktree no longer matches its candidate")
        if _git(worktree, "status", "--porcelain").stdout:
            raise WorkflowError("maintenance worktree must be clean before review")
        review_worktree = (
            self.worktree_root / f"review-{maintenance_id.lower()}-attempt-{record.attempt:03d}"
        )
        if review_worktree.exists():
            raise WorkflowError(f"review worktree path already exists: {review_worktree}")
        _git(
            self.repo,
            "worktree",
            "add",
            "--detach",
            str(review_worktree),
            record.candidate_commit,
        )
        request_path = f"todo/maintenance/{maintenance_id}/request.json"
        prompt = (
            f"Independently review low-risk maintenance repair {maintenance_id}. "
            f"The frozen request is {request_path}. Base commit: "
            f"{record.base_commit}. Candidate commit: {record.candidate_commit}. "
            f"Work only in {review_worktree}. Verify the exact diff, the maintenance "
            f"eligibility boundary, allowed paths, and listed commands once. Do not "
            f"fix anything. Write the standard review result only to "
            f"{review_worktree / '.workflow' / 'review-result.json'} with "
            f"task_id={maintenance_id}."
        )
        return {
            **record.to_dict(),
            "agent": "stage-reviewer",
            "review_worktree": str(review_worktree),
            "prompt": prompt,
        }

    def finish_maintenance_review(self, maintenance_id: str) -> tuple[str, Path]:
        record = self.load_maintenance(maintenance_id)
        if record is None or record.candidate_commit is None:
            raise WorkflowError(f"{maintenance_id} has no candidate to review")
        review_worktree = (
            self.worktree_root / f"review-{maintenance_id.lower()}-attempt-{record.attempt:03d}"
        )
        result_path = review_worktree / ".workflow" / "review-result.json"
        if not result_path.is_file():
            raise WorkflowError(f"review result is missing: {result_path}")
        result = _load_json(result_path)
        attempt = AttemptRecord(
            task_id=maintenance_id,
            phase="maintenance",
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=record.candidate_commit,
            branch=record.branch,
            development_worktree=record.development_worktree,
        )
        state = self._validate_review_result(result, attempt)
        reviewer_changes = [
            p for p in _working_tree_changes(review_worktree) if p != ".workflow/review-result.json"
        ]
        if reviewer_changes:
            raise WorkflowError("reviewer left changes outside its handoff")
        if _sha(review_worktree) != record.candidate_commit:
            raise WorkflowError("review worktree no longer matches candidate")
        worktree = Path(record.development_worktree)
        relative_root = Path("todo") / "maintenance" / maintenance_id
        review_name = f"review-{record.attempt:03d}"
        _write_json(worktree / relative_root / f"{review_name}.json", result)
        report = worktree / relative_root / f"{review_name}.md"
        report.write_text(_render_review(result), encoding="utf-8")
        _git(
            worktree,
            "add",
            str(relative_root / f"{review_name}.json"),
            str(relative_root / f"{review_name}.md"),
        )
        _git(worktree, "commit", "-m", f"chore(maintenance): record {maintenance_id} review")
        result_path.unlink()
        _git(self.repo, "worktree", "remove", str(review_worktree), check=False)
        if state == "APPROVED":
            self._ensure_clean_main()
            _git(self.repo, "merge", "--ff-only", record.branch)
            _git(self.repo, "worktree", "remove", record.development_worktree)
            _git(self.repo, "branch", "-d", record.branch)
            self._maintenance_path(maintenance_id).unlink(missing_ok=True)
            self._maintenance_request_path(maintenance_id).unlink(missing_ok=True)
            return state, self.repo / relative_root / f"{review_name}.md"
        updated = MaintenanceRecord(
            maintenance_id=maintenance_id,
            status="CHANGES_REQUESTED" if state == "CHANGES_REQUESTED" else state,
            attempt=record.attempt,
            base_commit=record.base_commit,
            candidate_commit=record.candidate_commit,
            branch=record.branch,
            development_worktree=record.development_worktree,
            continuation_count=record.continuation_count,
        )
        self.save_maintenance(updated)
        return state, report

    def maintenance_status(self, maintenance_id: str) -> dict[str, object]:
        record = self.load_maintenance(maintenance_id)
        if record is not None:
            return record.to_dict()
        root = self.repo / "todo" / "maintenance" / maintenance_id
        reviews = sorted(root.glob("review-*.json")) if root.is_dir() else []
        if reviews and _load_json(reviews[-1]).get("verdict") == "PASS":
            return {"maintenance_id": maintenance_id, "status": "APPROVED"}
        raise WorkflowError(f"unknown maintenance repair {maintenance_id}")


# ──────────────────────────────────────────────────────────────────────
# Render helpers (review markdown)
# ──────────────────────────────────────────────────────────────────────


def _render_review(result: Mapping[str, Any]) -> str:
    lines = [
        f"# {result['task_id']} independent review",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Candidate commit: `{result['candidate_commit']}`",
        f"- Verdict: **{result['verdict']}**",
        "",
        "## Checks",
        "",
    ]
    for check in result["checks"]:
        lines.append(f"### {check['id']} — {check['status']}")
        lines.append("")
        lines.append(check.get("finding") or "No additional finding.")
        lines.append("")
        lines.append("Evidence:")
        lines.append("")
        evidence = check.get("evidence") or ["None recorded."]
        lines.extend(f"- {item}" for item in evidence)
        lines.append("")
    for key, title in (
        ("must_not_violations", "Must-not violations"),
        ("unknowns", "Unknowns"),
        ("required_changes", "Required changes"),
        ("residual_risks", "Residual risks"),
    ):
        lines.extend([f"## {title}", ""])
        values = result.get(key, []) or ["None."]
        lines.extend(f"- {item}" for item in values)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_amendment_review(result: Mapping[str, Any]) -> str:
    lines = [
        f"# {result['amendment_id']} amendment review",
        "",
        f"- Base commit: `{result['base_commit']}`",
        f"- Candidate commit: `{result['candidate_commit']}`",
        f"- Verdict: **{result['verdict']}**",
        "",
        "## Summary",
        "",
        result.get("summary") or "No summary supplied.",
        "",
    ]
    for key, title in (("required_changes", "Required changes"), ("unknowns", "Unknowns")):
        lines.extend([f"## {title}", ""])
        values = result.get(key, []) or ["None."]
        lines.extend(f"- {item}" for item in values)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _is_task_contract_path(path: str) -> bool:
    """True if ``path`` matches the PROPHET task-contract creation pattern."""
    import re as _re

    return bool(_re.match(r"^todo/phases/[^/]+/T[0-9]{3}\.md$", path))
