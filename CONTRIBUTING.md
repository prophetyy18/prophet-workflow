# Contributing

Use Python 3.10 or newer. Create an isolated environment, then install the local
development dependencies:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
make check
```

Controller changes should stay domain-neutral and include regression coverage.
Do not commit generated caches, workflow worktrees, runtime records, secrets, or a
consumer project's `workflow.yml`.

For a release, update the version in `pyproject.toml`, the compatibility matrix and
the relevant entries in `CHANGELOG.md` together.
