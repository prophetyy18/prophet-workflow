# prophet-workflow

A generic, commit-bound task workflow for AI-assisted software projects.

This repository is a **template**. It is not itself a project — it is the
machine that runs *your* project's plan. Drop it into a fresh repository,
fill in `workflow.yml`, write your phase and task contracts, and you have a
deterministic controller that drives a Manager, Developer, Reviewer, and a
small set of planning agents against immutable commits.

## What it gives you

- A **state machine** for tasks (`PLANNED → READY → IN_DEVELOPMENT →
  AWAITING_REVIEW → APPROVED`, with explicit branches for retries, triage,
  planning, and owner amendment).
- **Two-role separation** (Developer + Reviewer) running in **independent
  Git worktrees**, against exact base and candidate SHAs. A reviewer cannot
  read the developer's scratch space, and the developer's candidate commit
  is sealed before the reviewer sees it.
- **Protected paths**: the workflow, intent documents, spec documents, and
  task contracts cannot be edited by a Developer. The reviewer verifies
  that.
- **Structured results**: every agent writes a JSON result that the
  controller validates against a JSON Schema before sealing.
- **Owner-directed amendments** in four layers — `CONTRACT`, `SPEC`,
  `PROPHET`, `SUPERSEDE` — each with its own author and reviewer role.
- **Maintenance repairs** for bounded, low-risk fixes that don't deserve a
  numbered product task.
- **Continuation**: an unfinished Developer can hand off a structured
  checkpoint and a fresh Developer resumes in the same attempt.

## What it does not give you

- **No business code**. The template ships no domain logic. Your project
  writes its own application.
- **No model lock-in**. The controller does not name a specific model or
  vendor. You declare your runtime in `workflow.yml`.
- **No intent or spec content**. `docs/intent/`, `docs/spec/`, and
  `docs/implement/` are scaffolding only. Your project writes them.
- **No product-specific CI gates**. The template tests and lints its own
  controller, while consumer projects add their domain checks to task contracts
  and CI.

## Quick start

```bash
# 1. Copy this template into your project (or clone and strip).
cp -r prophet-workflow/. my-project/
cd my-project

# 2. Edit workflow.yml — set project name, model, protected paths.
$EDITOR workflow.yml

# 3. Install and scaffold the workflow tree.
python3 -m pip install -e '.[dev]'
python3 scripts/init_workflow.py

# 4. Verify.
python3 -m tools.workflow validate
python3 -m tools.workflow status
make check
```

## Roles

The workflow ships with eight agent definitions in `.claude/agents/`. Each
maps to a subagent type in your AI coding tool's Agent tool.

| Agent | Purpose | Initiated by |
|---|---|---|
| `workflow-manager` | Interactive control plane: runs prepare/finish gates, launches other agents | the human Owner |
| `stage-developer` | Implements one task against an exact base commit, writes developer result | Manager |
| `stage-reviewer` | Independently verifies a candidate commit, writes review result | Manager |
| `issue-triager` | Classifies exceptional blockers without editing code or contracts | Manager |
| `planner` | Transcribes an Owner direction into planning changes (amendment route) | Manager |
| `plan-reviewer` | Independently reviews a planner candidate | Manager |
| `prophet` | States goals, restructures plan, corrects collateral (PROPHET layer) | Manager |
| `prophet-reviewer` | Independently reviews a PROPHET candidate | Manager |

## Directory layout

```
prophet-workflow/
├── .github/workflows/ci.yml        # controller quality gate
├── .gitignore / .editorconfig     # repository hygiene
├── AGENTS.md / CLAUDE.md          # coding-agent policy and entry point
├── README.md                     # this file
├── ARCHITECTURE.md               # seams, layers, upgrade protocol
├── CHANGELOG.md                  # version protocol
├── CONTRIBUTING.md / SECURITY.md # contribution and reporting policy
├── pyproject.toml / Makefile     # packaging and local quality commands
├── workflow.yml.example          # example project config
├── tools/
│   └── workflow/                 # the controller
│       ├── __init__.py
│       ├── __main__.py
│       ├── cli.py
│       └── core.py
├── todo/
│   ├── README.md                 # workflow overview for agents
│   ├── WORKFLOW.md               # command reference
│   └── schemas/                  # JSON Schemas for structured results
├── .claude/
│   └── agents/                   # agent definitions
├── scripts/
│   └── init_workflow.py          # project scaffolding
└── tests/                        # controller regression suite
```

## License

No distribution license has been selected yet. Add an explicit license before
publishing or redistributing the template.
