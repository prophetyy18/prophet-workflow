#!/usr/bin/env python3
"""Scaffold the workflow tree in the current repository.

Run from the repository root:

    python scripts/init_workflow.py [--upgrade]

The script is idempotent: it creates directories and example files only
if they do not already exist. It never overwrites an existing file.

With ``--upgrade``, the script validates that the current repository is
already initialized and prints a reminder of which directories are
managed by the workflow (so a manual upgrade can cherry-pick the right
files from a new template release).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

MANAGED_PATHS = (
    "tools/workflow/",
    "tools/__init__.py",
    "todo/schemas/",
    ".claude/agents/",
    ".github/workflows/",
    "tests/",
    "todo/README.md",
    "todo/WORKFLOW.md",
    "workflow.yml.example",
    "ARCHITECTURE.md",
    "README.md",
    "CHANGELOG.md",
    "AGENTS.md",
    "CLAUDE.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "pyproject.toml",
    "Makefile",
    ".gitignore",
    ".editorconfig",
    "scripts/init_workflow.py",
)

EXAMPLE_CONFIG_YAML = """\
{
  "_comment": "Minimal todo/config.yaml. Extend with phases and tasks. This file is parsed as JSON (with optional PyYAML if installed); keep it JSON-compatible.",
  "schema_version": 1,
  "project": "<your-project-name>",
  "template_version": "1.1.0",
  "agent_runtime": {
    "provider": "<your-provider>",
    "model": "<your-model-identifier>",
    "context_window_tokens": 100000,
    "allow_model_fallback": false
  },
  "intent_revision": "v1-initial",
  "spec_revision": "v1-initial",
  "tasks": {}
}
"""


def _ensure(path: Path, is_dir: bool) -> str:
    if path.exists():
        return f"exists  {path.relative_to(REPO)}"
    if is_dir:
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return f"created {path.relative_to(REPO)}"


def _ensure_with_content(path: Path, content: str) -> str:
    if path.exists():
        return f"exists  {path.relative_to(REPO)}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"created {path.relative_to(REPO)}"


def scaffold() -> int:
    """Create directories and example files the project needs."""
    created = []

    # Core directories.
    created.append(_ensure(REPO / "todo" / "phases", is_dir=True))
    created.append(_ensure(REPO / "todo" / "evidence", is_dir=True))
    created.append(_ensure(REPO / "todo" / "reviews", is_dir=True))
    created.append(_ensure(REPO / "todo" / "amendments", is_dir=True))
    created.append(_ensure(REPO / "todo" / "maintenance", is_dir=True))
    created.append(_ensure(REPO / "docs" / "intent", is_dir=True))
    created.append(_ensure(REPO / "docs" / "spec", is_dir=True))
    created.append(_ensure(REPO / "docs" / "implement", is_dir=True))

    # Example config file the user must replace with real content.
    config_path = REPO / "todo" / "config.yaml"
    if not config_path.exists():
        created.append(_ensure_with_content(config_path, EXAMPLE_CONFIG_YAML))
    else:
        created.append(f"exists  {config_path.relative_to(REPO)}")

    # workflow.yml is project-specific; create only if absent.
    workflow_yml = REPO / "workflow.yml"
    if not workflow_yml.exists():
        example = REPO / "workflow.yml.example"
        if example.is_file():
            workflow_yml.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
            created.append(f"created {workflow_yml.relative_to(REPO)} (from .example)")
        else:
            created.append(f"skipped {workflow_yml.relative_to(REPO)} (no .example found)")
    else:
        created.append(f"exists  {workflow_yml.relative_to(REPO)}")

    for line in created:
        print(line)
    print()
    print("Next steps:")
    print("  1. Edit workflow.yml: set project name, model, protected paths.")
    print("  2. Replace todo/config.yaml with your real plan.")
    print("  3. Write your first task contract under todo/phases/P00-*/T000.md.")
    print("  4. Run: python -m tools.workflow validate")
    return 0


def upgrade() -> int:
    """Print which paths are managed by the workflow, for a manual cherry-pick."""
    print("The following paths are managed by the template.")
    print("When upgrading, fetch only these from the new template release:")
    print()
    for p in MANAGED_PATHS:
        print(f"  {p}")
    print()
    print("After fetching, run:")
    print("  python -m tools.workflow validate")
    print("  python -m tools.workflow status")
    print()
    print("Do NOT overwrite workflow.yml or todo/config.yaml unless the")
    print("upgrade is a MAJOR version that intentionally changes them.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--upgrade", action="store_true", help="print upgrade checklist")
    args = parser.parse_args(argv)
    return upgrade() if args.upgrade else scaffold()


if __name__ == "__main__":
    sys.exit(main())
