"""Tests for the prophet helper.

The gate-finding extractor is the load-bearing mechanism of the whole
workflow: if it records an empty baseline while a gate is red, every later
regression becomes invisible. These tests pin that behaviour.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.prophet.__main__ import (  # noqa: E402
    _current_hypothesis,
    _finding_lines,
    _looks_unrunnable,
    _slug,
)

RUFF_OUT = """\
F401 [*] `os` imported but unused
 --> src/calc.py:1:8
  |
1 | import os
  |        ^^
help: Remove unused import: `os`
  |
  - import os
1 |
  |

F841 Local variable `x` is assigned to but never used
  --> src/calc.py:9:5
   |
 8 | def div(a, b):
 9 |     x = 1
   |     ^
10 |     return a / b
   |
help: Remove assignment to unused variable `x`

Found 2 errors.
"""

PYTEST_OUT = """\
============================= test session starts ==============================
collected 3 items

tests/test_calc.py .F.                                                       [100%]

=================================== FAILURES ===================================
_______________________________ test_div _______________________________
    def test_div():
>       assert div(6, 0) == 2
E       ZeroDivisionError: division by zero
tests/test_calc.py:8: ZeroDivisionError
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_div - ZeroDivisionError: division by zero
=========== 1 failed, 2 passed in 0.12s =============
"""

MYPY_OUT = """\
src/calc.py:9: error: Incompatible return value type (got "float", expected "int")
src/calc.py:12: error: Missing return statement  [return]
Found 2 errors in 1 file (checked 3 source files)
"""


class TestFindingExtraction(unittest.TestCase):
    def test_ruff_findings_are_captured_with_location(self) -> None:
        found = _finding_lines(RUFF_OUT)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("F401" in f and "src/calc.py:1:8" in f for f in found))
        self.assertTrue(any("F841" in f and "src/calc.py:9:5" in f for f in found))

    def test_ruff_context_lines_are_not_findings(self) -> None:
        """Code excerpt lines must not become findings themselves."""
        found = _finding_lines(RUFF_OUT)
        for f in found:
            self.assertNotIn("|", f, f"excerpt leaked into a finding: {f}")

    def test_ruff_clean_output_has_no_findings(self) -> None:
        self.assertEqual(_finding_lines("All checks passed!"), [])

    def test_pytest_failure_is_one_stable_finding(self) -> None:
        found = _finding_lines(PYTEST_OUT)
        self.assertEqual(len(found), 1, found)
        self.assertIn("tests/test_calc.py::test_div", found[0])

    def test_pytest_passing_output_has_no_findings(self) -> None:
        self.assertEqual(_finding_lines("2 passed in 0.10s\n"), [])

    def test_mypy_findings_captured(self) -> None:
        found = _finding_lines(MYPY_OUT)
        self.assertEqual(len(found), 2, found)
        self.assertTrue(any("src/calc.py:9" in f and "Incompatible" in f for f in found))

    def test_extraction_is_deterministic(self) -> None:
        self.assertEqual(_finding_lines(RUFF_OUT), _finding_lines(RUFF_OUT))

    def test_identical_run_produces_identical_findings(self) -> None:
        """The whole incremental judgement depends on this."""
        self.assertEqual(_finding_lines(MYPY_OUT), _finding_lines(MYPY_OUT))


class TestUnrunnableDetection(unittest.TestCase):
    def test_missing_module_is_unrunnable(self) -> None:
        self.assertTrue(_looks_unrunnable("No module named pytest"))

    def test_command_not_found_is_unrunnable(self) -> None:
        self.assertTrue(_looks_unrunnable("bash: ruff: command not found"))

    def test_traceback_is_unrunnable(self) -> None:
        self.assertTrue(_looks_unrunnable("Traceback (most recent call last):\n  ..."))

    def test_normal_findings_are_not_unrunnable(self) -> None:
        self.assertFalse(_looks_unrunnable(RUFF_OUT))
        self.assertFalse(_looks_unrunnable(PYTEST_OUT))


class TestHypothesisExtraction(unittest.TestCase):
    def test_bold_inline_label(self) -> None:
        spec = "## Hypothesis\n\n**Hypothesis:** If we add X, then Y.\n"
        self.assertIsNotNone(_current_hypothesis(spec))

    def test_heading_then_paragraph(self) -> None:
        spec = "**Round:** 3\n\n## Hypothesis\n\nIf we add X, then Y.\n"
        self.assertEqual(_current_hypothesis(spec), "If we add X, then Y.")

    def test_template_placeholder_is_ignored(self) -> None:
        """The starter template's <placeholder> is not a real hypothesis."""
        spec = "## Hypothesis\n\nIf we <change>, then <observable outcome>.\n"
        got = _current_hypothesis(spec)
        self.assertIsNotNone(got)
        assert got is not None
        self.assertIn("<change>", got)

    def test_missing_hypothesis_returns_none(self) -> None:
        self.assertIsNone(_current_hypothesis("# Current hypothesis\n\nnothing yet\n"))


class TestSlug(unittest.TestCase):
    def test_slugify(self) -> None:
        self.assertEqual(_slug("Add the report page"), "add-the-report-page")

    def test_slugify_strips_punctuation(self) -> None:
        self.assertEqual(_slug("fix: handle --weird/name?"), "fix-handle-weird-name")

    def test_slugify_never_empty(self) -> None:
        self.assertEqual(_slug("!!!"), "slice")


class TestBaselineEndToEnd(unittest.TestCase):
    """Exercise baseline + gates against a real repo with a red gate."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.tool = Path(__file__).resolve().parent.parent
        (self.repo / "src").mkdir()
        (self.repo / "src" / "bad.py").write_text("import os\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _prophet(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(self.tool), "PATH": "/usr/bin:/bin:/usr/local/bin"}
        return subprocess.run(
            [sys.executable, "-m", "tools.prophet", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def test_baseline_refuses_unreadable_gate(self) -> None:
        """A gate that cannot run must not produce an empty baseline."""
        (self.repo / ".prophet").mkdir()
        (self.repo / ".prophet" / "gates.txt").write_text(
            "definitely-not-a-real-command --x\n", encoding="utf-8"
        )
        result = self._prophet("baseline")
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse(
            (self.repo / ".prophet" / "baseline.json").exists(),
            "baseline.json must not be written when a gate is unreadable",
        )

    def test_gate_regression_is_detected(self) -> None:
        """A new finding must be reported as a regression."""
        ruff = shutil_which("ruff")
        if ruff is None:
            self.skipTest("ruff not on PATH")
        (self.repo / ".prophet").mkdir()
        (self.repo / ".prophet" / "gates.txt").write_text(f"{ruff} check src/\n", encoding="utf-8")
        # baseline with one finding
        (self.repo / "src" / "bad.py").write_text("import os\nimport sys\n", encoding="utf-8")
        r1 = self._prophet("baseline")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        baseline = json.loads(
            (self.repo / ".prophet" / "baseline.json").read_text(encoding="utf-8")
        )
        total = sum(len(v["findings"]) for v in baseline["gates"].values())
        self.assertGreater(total, 0, "baseline should record the existing findings")

        # same findings -> no regression
        r2 = self._prophet("gates")
        self.assertEqual(r2.returncode, 0, r2.stdout)
        self.assertIn("no regressions", r2.stdout)

        # add a new finding -> regression
        (self.repo / "src" / "bad.py").write_text(
            "import os\nimport sys\nimport json\n", encoding="utf-8"
        )
        r3 = self._prophet("gates")
        self.assertNotEqual(r3.returncode, 0, r3.stdout)
        self.assertIn("REGRESSION", r3.stdout)


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


if __name__ == "__main__":
    unittest.main()
