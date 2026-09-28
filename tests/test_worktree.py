"""End-to-end test: worktree creation must not depend on a hypothesis existing.

A person may want the isolated tree before writing the hypothesis. Refusing to
create a worktree in that order blocks a cheap, useful action on a technicality.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.prophet.__main__ import main  # noqa: E402


class TestWorktreeWithoutSpec(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.repo, check=True
        )

    def tearDown(self) -> None:
        subprocess.run(["git", "worktree", "prune"], cwd=self.repo, capture_output=True)
        self.tmp.cleanup()

    def test_new_works_without_spec(self) -> None:
        cwd = Path.cwd()
        try:
            import os

            os.chdir(self.repo)
            rc = main(["new", "some slice"])
        finally:
            import os

            os.chdir(cwd)
        self.assertEqual(rc, 0)
        self.assertTrue((self.repo / ".prophet-worktrees" / "some-slice").exists())

    def test_new_refuses_duplicate_worktree(self) -> None:
        cwd = Path.cwd()
        try:
            import os

            os.chdir(self.repo)
            self.assertEqual(main(["new", "dupe"]), 0)
            self.assertEqual(main(["new", "dupe"]), 1)
        finally:
            import os

            os.chdir(cwd)


if __name__ == "__main__":
    unittest.main()
