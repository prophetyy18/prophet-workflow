"""Command-line entry point for the workflow controller.

Run from the repository root with the project's Python:

    python -m tools.workflow <command> [args]

Every command prints a JSON object to stdout. Errors are emitted as
{"status": "ERROR", "error": "..."} and exit with code 2.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import WorkflowError, WorkflowManager


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tools.workflow")
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--worktree-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)

    # ─── Repository health ────────────────────────────────────────
    sub.add_parser("validate", help="validate configuration and wiring")
    sub.add_parser("status", help="show effective task state, including an active worktree")

    # ─── Task execution ───────────────────────────────────────────
    ready = sub.add_parser("ready", help="activate one dependency-complete PLANNED task")
    ready.add_argument("task_id")

    prep_dev = sub.add_parser("prepare-develop", help="prepare a worktree for a Developer")
    prep_dev.add_argument("task_id")

    fin_dev = sub.add_parser("finish-develop", help="validate and seal a Developer result")
    fin_dev.add_argument("task_id")

    cont_dev = sub.add_parser(
        "continue-develop",
        help="continue an unfinished Developer session in the same attempt",
    )
    cont_dev.add_argument("task_id")
    cont_dev.add_argument("--max-turns-exhausted", action="store_true")

    prep_retry = sub.add_parser(
        "prepare-retry", help="prepare a fresh Developer after CHANGES_REQUESTED"
    )
    prep_retry.add_argument("task_id")

    prep_rev = sub.add_parser(
        "prepare-review", help="prepare an exact detached worktree for a Reviewer"
    )
    prep_rev.add_argument("task_id")

    fin_rev = sub.add_parser("finish-review", help="validate and record a Reviewer result")
    fin_rev.add_argument("task_id")

    # ─── Triage ───────────────────────────────────────────────────
    prep_t = sub.add_parser("prepare-triage", help="prepare an independent issue triager")
    prep_t.add_argument("task_id")
    fin_t = sub.add_parser("finish-triage", help="validate and record a triage result")
    fin_t.add_argument("task_id")

    # ─── Planning (amendment) ─────────────────────────────────────
    prep_plan = sub.add_parser("prepare-plan", help="prepare a Planner for a triaged issue")
    prep_plan.add_argument("task_id")
    prep_plan.add_argument("--owner-decision")
    fin_plan = sub.add_parser("finish-plan", help="validate and seal a Planner result")
    fin_plan.add_argument("task_id")
    prep_pr = sub.add_parser("prepare-plan-review", help="prepare an independent Plan Reviewer")
    prep_pr.add_argument("task_id")
    fin_pr = sub.add_parser("finish-plan-review", help="validate and record a Plan Review")
    fin_pr.add_argument("task_id")

    # ─── Path protection ──────────────────────────────────────────
    check = sub.add_parser("check-paths", help="report paths changed since a base commit")
    check.add_argument("base_commit")

    # ─── Owner amendments ─────────────────────────────────────────
    amend = sub.add_parser(
        "prepare-amendment",
        help="prepare an Owner amendment (CONTRACT, SPEC, PROPHET, or SUPERSEDE)",
    )
    amend.add_argument("--task", dest="task_ids", action="append", default=[])
    amend.add_argument(
        "--layer",
        choices=("CONTRACT", "SPEC", "PROPHET", "SUPERSEDE"),
        required=True,
    )
    amend.add_argument("--summary", required=True)
    amend.add_argument("--owner-direction", required=True)

    for name, help_text in (
        ("finish-amendment", "validate and seal an amendment candidate"),
        ("prepare-amendment-review", "prepare an independent amendment reviewer"),
        ("finish-amendment-review", "validate and record an amendment review"),
        ("prepare-amendment-retry", "repair an amendment after review failure"),
        ("amendment-status", "show one amendment's state"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("amendment_id")
    withdraw = sub.add_parser(
        "withdraw-amendment",
        help="close an amendment without applying it and keep its audit record",
    )
    withdraw.add_argument("amendment_id")
    withdraw.add_argument("--reason", required=True)

    # ─── Maintenance repairs ──────────────────────────────────────
    maint = sub.add_parser(
        "prepare-maintenance",
        help="prepare a bounded low-risk repair outside the product task graph",
    )
    maint.add_argument("--summary", required=True)
    maint.add_argument("--reason", required=True)
    maint.add_argument("--path", dest="paths", action="append", required=True)
    maint.add_argument("--check", dest="checks", action="append", required=True)
    maint.add_argument("--related-task")

    for name, help_text in (
        ("finish-maintenance-develop", "validate and seal a maintenance developer result"),
        ("prepare-maintenance-retry", "repair a maintenance candidate after review failure"),
        ("prepare-maintenance-review", "prepare an independent maintenance reviewer"),
        ("finish-maintenance-review", "validate and record a maintenance review"),
        ("maintenance-status", "show one maintenance repair's state"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("maintenance_id")
    cont_maint = sub.add_parser(
        "continue-maintenance-develop",
        help="continue an unfinished maintenance developer in the same attempt",
    )
    cont_maint.add_argument("maintenance_id")
    cont_maint.add_argument("--max-turns-exhausted", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        manager = WorkflowManager(args.repo, worktree_root=args.worktree_root)
        cmd = args.command

        if cmd == "validate":
            manager.validate_repository()
            output: object = {"status": "OK"}
        elif cmd == "status":
            output = manager.status()
        elif cmd == "ready":
            output = {"status": "READY", "commit": manager.ready(args.task_id)}
        elif cmd == "prepare-develop":
            output = manager.prepare_develop(args.task_id)
        elif cmd == "finish-develop":
            output = manager.finish_develop(args.task_id).to_dict()
        elif cmd == "continue-develop":
            output = manager.continue_develop(
                args.task_id, max_turns_exhausted=args.max_turns_exhausted
            )
        elif cmd == "prepare-retry":
            output = manager.prepare_develop(args.task_id, retry=True)
        elif cmd == "prepare-review":
            output = manager.prepare_review(args.task_id)
        elif cmd == "finish-review":
            state, report = manager.finish_review(args.task_id)
            output = {"status": state, "report": str(report)}
        elif cmd == "prepare-triage":
            output = manager.prepare_triage(args.task_id)
        elif cmd == "finish-triage":
            state, report = manager.finish_triage(args.task_id)
            output = {"status": state, "report": str(report)}
        elif cmd == "prepare-plan":
            output = manager.prepare_plan(args.task_id, owner_decision=args.owner_decision)
        elif cmd == "finish-plan":
            output = manager.finish_plan(args.task_id).to_dict()
        elif cmd == "prepare-plan-review":
            output = manager.prepare_plan_review(args.task_id)
        elif cmd == "finish-plan-review":
            state, report = manager.finish_plan_review(args.task_id)
            output = {"status": state, "report": str(report)}
        elif cmd == "check-paths":
            output = {"changed_paths": manager.check_changed_paths(args.base_commit)}
        elif cmd == "prepare-amendment":
            output = manager.prepare_amendment(
                task_ids=args.task_ids,
                layer=args.layer,
                summary=args.summary,
                owner_direction=args.owner_direction,
            )
        elif cmd == "finish-amendment":
            output = manager.finish_amendment(args.amendment_id).to_dict()
        elif cmd == "prepare-amendment-review":
            output = manager.prepare_amendment_review(args.amendment_id)
        elif cmd == "finish-amendment-review":
            state, report = manager.finish_amendment_review(args.amendment_id)
            output = {"status": state, "report": str(report)}
        elif cmd == "prepare-amendment-retry":
            output = manager.prepare_amendment_retry(args.amendment_id)
        elif cmd == "withdraw-amendment":
            output = manager.withdraw_amendment(args.amendment_id, reason=args.reason)
        elif cmd == "amendment-status":
            output = manager.amendment_status(args.amendment_id)
        elif cmd == "prepare-maintenance":
            output = manager.prepare_maintenance(
                summary=args.summary,
                reason=args.reason,
                allowed_paths=args.paths,
                verification_commands=args.checks,
                related_task=args.related_task,
            )
        elif cmd == "finish-maintenance-develop":
            output = manager.finish_maintenance_develop(args.maintenance_id).to_dict()
        elif cmd == "continue-maintenance-develop":
            output = manager.continue_maintenance_develop(
                args.maintenance_id,
                max_turns_exhausted=args.max_turns_exhausted,
            )
        elif cmd == "prepare-maintenance-retry":
            output = manager.prepare_maintenance_retry(args.maintenance_id)
        elif cmd == "prepare-maintenance-review":
            output = manager.prepare_maintenance_review(args.maintenance_id)
        elif cmd == "finish-maintenance-review":
            state, report = manager.finish_maintenance_review(args.maintenance_id)
            output = {"status": state, "report": str(report)}
        elif cmd == "maintenance-status":
            output = manager.maintenance_status(args.maintenance_id)
        else:
            raise WorkflowError(f"unsupported command {cmd}")

    except WorkflowError as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from exc
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
