from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.workflow.core import AmendmentRecord, AttemptRecord, WorkflowManager, load_settings


def _run(root: Path, *args: str) -> None:
    subprocess.run(args, cwd=root, check=True, capture_output=True, text=True)


class SettingsTests(unittest.TestCase):
    def test_yaml_settings_are_merged_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "workflow.yml").write_text(
                "project: example\nruntime_dir: state\nprotected:\n  files: [README.md]\n",
                encoding="utf-8",
            )
            settings = load_settings(root)

        self.assertEqual(settings["project"], "example")
        self.assertEqual(settings["runtime_dir"], "state")
        self.assertEqual(settings["protected"]["files"], ["README.md"])
        self.assertIn("prefixes", settings["protected"])

    def test_amendment_record_reads_legacy_state_and_preserves_freeze_base(self) -> None:
        legacy = {
            "amendment_id": "A0001",
            "status": "PLANNING",
            "attempt": 1,
            "layer": "PROPHET",
            "task_ids": [],
            "base_commit": "a" * 40,
            "candidate_commit": None,
            "branch": "amendment/a0001-attempt-001",
            "worktree": "/tmp/a0001",
        }
        record = AmendmentRecord.from_dict(legacy)
        self.assertEqual(record.freeze_base, "a" * 40)

        retried = AmendmentRecord.from_dict(
            {**record.to_dict(), "base_commit": "b" * 40, "original_base_commit": "a" * 40}
        )
        self.assertEqual(retried.freeze_base, "a" * 40)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        _run(self.root, "git", "init", "-q")
        _run(self.root, "git", "config", "user.name", "Workflow Tests")
        _run(self.root, "git", "config", "user.email", "workflow@example.invalid")
        (self.root / "workflow.yml").write_text(
            "\n".join(
                [
                    "project: test-project",
                    "template_version: 1.1.0",
                    "agent_runtime:",
                    "  provider: test",
                    "  model: test",
                    "  context_window_tokens: 1",
                    "  allow_model_fallback: false",
                    "required_agents: []",
                    "protected: {prefixes: [], files: []}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        (self.root / "todo" / "phases").mkdir(parents=True)
        (self.root / "todo" / "phases" / "T000.md").write_text(
            "# T000\n\n## Acceptance\n\n- Tests pass.\n", encoding="utf-8"
        )
        config = {
            "schema_version": 1,
            "project": "test-project",
            "template_version": "1.1.0",
            "agent_runtime": {
                "provider": "test",
                "model": "test",
                "context_window_tokens": 1,
                "allow_model_fallback": False,
            },
            "active_phase": "P00",
            "active_task": "T000",
            "workflow_state": "AWAITING_REVIEW",
            "tasks": {
                "T000": {
                    "phase": "P00",
                    "status": "AWAITING_REVIEW",
                    "depends_on": [],
                    "task_file": "todo/phases/T000.md",
                    "attempt": 1,
                    "base_commit": None,
                    "candidate_commit": None,
                    "approved_commit": None,
                }
            },
        }
        (self.root / "todo" / "config.yaml").write_text(
            json.dumps(config, indent=2) + "\n", encoding="utf-8"
        )
        _run(self.root, "git", "add", "-A")
        _run(self.root, "git", "commit", "-qm", "initial")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_failed_review_state_is_persisted_in_candidate_branch(self) -> None:
        worktree = Path(self.temp.name) / "dev"
        _run(self.root, "git", "worktree", "add", "-qb", "candidate", str(worktree))
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        attempt = AttemptRecord(
            task_id="T000",
            phase="P00",
            attempt=1,
            base_commit=head,
            candidate_commit=head,
            branch="candidate",
            development_worktree=str(worktree),
        )
        result = {
            "task_id": "T000",
            "base_commit": head,
            "candidate_commit": head,
            "verdict": "FAIL",
            "summary": "Needs repair.",
            "checks": [{"id": "acceptance", "status": "FAIL", "finding": "failed", "evidence": []}],
            "must_not_violations": [],
            "unknowns": [],
            "required_changes": ["Repair it."],
            "residual_risks": [],
        }
        manager = WorkflowManager(self.root, worktree_root=Path(self.temp.name) / "worktrees")
        state, report = manager._record_review_result(attempt, result, "CHANGES_REQUESTED")

        stored = json.loads((worktree / "todo" / "config.yaml").read_text(encoding="utf-8"))
        self.assertEqual(state, "CHANGES_REQUESTED")
        self.assertEqual(stored["tasks"]["T000"]["status"], "CHANGES_REQUESTED")
        self.assertTrue(report.is_file())

    def test_closed_amendment_does_not_occupy_lane(self) -> None:
        manager = WorkflowManager(self.root, worktree_root=Path(self.temp.name) / "worktrees")
        manager.save_amendment(
            AmendmentRecord(
                amendment_id="A0001",
                status="APPROVED",
                attempt=1,
                layer="PROPHET",
                task_ids=(),
                base_commit="a" * 40,
                candidate_commit=None,
                branch="amendment/test-1",
                worktree="/tmp/test-1",
            )
        )
        manager.save_amendment(
            AmendmentRecord(
                amendment_id="A0002",
                status="PLANNING",
                attempt=1,
                layer="PROPHET",
                task_ids=(),
                base_commit="a" * 40,
                candidate_commit=None,
                branch="amendment/test-2",
                worktree="/tmp/test-2",
            )
        )

        self.assertEqual(
            [record.amendment_id for record in manager._active_amendment_records()], ["A0002"]
        )


if __name__ == "__main__":
    unittest.main()
