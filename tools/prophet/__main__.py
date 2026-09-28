#!/usr/bin/env python3
"""Prophet Workflow helper.

Small on purpose. The workflow is three prompts and a convention; this script
only removes mechanical friction around running a round. Deleting it leaves the
workflow fully functional.

    python -m tools.prophet init          create .prophet/ with starter files
    python -m tools.prophet new <name>    create a worktree + branch for a slice
    python -m tools.prophet gates         run the project's gates, compare to baseline
    python -m tools.prophet baseline      record current gate findings as the baseline
    python -m tools.prophet status        show the current hypothesis and last rounds

Run from the repository root.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import TypedDict

PROPHET = Path(".prophet")
SPEC = PROPHET / "spec.md"
LOG = PROPHET / "LOG.md"
DECISIONS = PROPHET / "DECISIONS.md"
BASELINE = PROPHET / "baseline.json"
WORKTREES = Path(".prophet-worktrees")

GATES_FILE = PROPHET / "gates.txt"

STARTER_SPEC = """# Current hypothesis

<!-- Rewritten every round. Keep this file short enough to read in a minute.
     History belongs in LOG.md; settled choices belong in DECISIONS.md.
     Verify the shape with: python -m tools.prophet check -->

**Round:** _(unset)_
**Tier:** _(0 / 1 / 2 — see ARCHITECTURE.md)_
**Rollback:** _(required for Tier 2; what undoes this if it goes wrong)_

## Hypothesis

**Change:** _(the concrete thing you will do)_
**Outcome:** _(the observable result — a number, a count, a state you could see)_
**Verify by:** _(the exact command or page you open that shows it)_

<!--
An Outcome must be able to be proved FALSE. "it gets faster" cannot be;
"a repeat visit renders in under 200ms" can. `prophet check` rejects the
first kind. The placeholder text in _( ) counts as not-yet-written.
-->

## Slice scope

**In scope:**
-
**Out of scope:**
-

## Known risks

-
"""

STARTER_LOG = """# Rounds log

Append one entry per round. What actually happened, not what was intended.

## R1 — <title> (<date>)

**Hypothesis:** <the sentence>

**Showed:** <what the artifact demonstrated>
**Did not show:** <what stayed unproven>
**Human decision:** <keep / redirect / discard>
**Next hypothesis:** <what we now think>
"""

STARTER_DECISIONS = """# Decisions

One line each: date, choice, why. For "why did we decide X" six months from now.

- _(none yet)_
"""


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not installed"


def cmd_init(_: argparse.Namespace) -> int:
    PROPHET.mkdir(exist_ok=True)
    for path, text in (
        (SPEC, STARTER_SPEC),
        (LOG, STARTER_LOG),
        (DECISIONS, STARTER_DECISIONS),
    ):
        if path.exists():
            print(f"keep    {path} (exists)")
            continue
        path.write_text(text, encoding="utf-8")
        print(f"create  {path}")
    if not GATES_FILE.exists():
        GATES_FILE.write_text(
            "# One gate command per line. Run by `prophet gates`.\n"
            "# Lines starting with # are ignored.\n"
            "# pytest\n"
            "# ruff check .\n"
            "# mypy src\n",
            encoding="utf-8",
        )
        print(f"create  {GATES_FILE} (edit me)")
    print("\nNext: write your first hypothesis into .prophet/spec.md")
    return 0


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "slice"


def cmd_new(args: argparse.Namespace) -> int:
    # Creating a worktree is cheap and does not require a hypothesis to exist
    # yet. A person may reasonably want the isolated tree first and the
    # hypothesis second, so do not refuse it here.
    slug = _slug(args.name)
    branch = f"slice/{slug}"
    WORKTREES.mkdir(exist_ok=True)
    wt = WORKTREES / slug
    if wt.exists():
        print(f"worktree already exists: {wt}")
        return 1
    rc, out = _run(["git", "worktree", "add", "-b", branch, str(wt), args.base or "HEAD"])
    if rc != 0:
        print(out, file=sys.stderr)
        return rc
    print(f"branch    {branch}")
    print(f"worktree  {wt}")
    if SPEC.exists():
        print(f"\nHand the builder: the hypothesis from {SPEC}, and this worktree.")
    else:
        print(f"\nNo {SPEC} yet — write the hypothesis, then hand over the worktree.")
    return 0


def _gate_commands() -> list[str]:
    if not GATES_FILE.exists():
        return []
    out = []
    for line in GATES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def _finding_lines(output: str) -> list[str]:
    """Reduce gate output to comparable finding lines.

    Each tool prints findings differently. We extract the stable identity of a
    finding — its code, its location and its message — so the same finding is
    recognized across runs and a *new* finding is distinguishable.

    Supported shapes (the common gates; extend as needed):
      ruff     ``F401 [*] `os` imported but unused`` / `` --> src/x.py:1:8``
      pytest    ``FAILED tests/test_x.py::test_y - assert ...``
      mypy      ``src/x.py:12: error: Incompatible types ...``
      generic   any line starting with ``error:`` / ``ERROR:``

    Lines that are progress noise or code excerpts are dropped: a context line
    such as ``1 | import os`` must not become its own "finding" or every run
    would look different from the last.

    Known limitation: identity includes the line number, so a finding that
    merely *moved* counts as new. That errs toward strict (a slice gets told
    about something that is not really new), which is the safe direction. The
    converse — a genuinely new finding hiding at a recycled line number — needs
    the message to differ too, and a message change makes it visible. Dropping
    line numbers entirely would be looser but would let a real new problem hide
    behind a matching old one.
    """
    findings: list[str] = []
    lines = output.splitlines()

    for i, raw in enumerate(lines):
        s = raw.strip()
        if not s:
            continue

        # ruff: code + message, location on a following " --> file:line:col"
        m = re.match(r"^([A-Z]+\d+)\s+(?:\[\*\]\s+)?(.*)$", s)
        if m and _next_is_ruff_location(lines, i):
            loc = _ruff_location(lines, i)
            findings.append(f"ruff {m.group(1)} {loc} {m.group(2)[:80]}")
            continue

        # pytest: FAILED / ERROR lines are the stable identity
        m = re.match(r"^(FAILED|ERROR)\s+(\S+)", s)
        if m:
            findings.append(f"pytest {m.group(1)} {m.group(2)}")
            continue

        # mypy: file:line: error: message
        m = re.match(r"^([^:\s]+\.py):(\d+):\s*(error|note|warning):\s*(.*)$", s)
        if m:
            findings.append(f"mypy {m.group(1)}:{m.group(2)} {m.group(3)} {m.group(4)[:80]}")
            continue

        # generic
        m = re.match(r"^(error|ERROR|warning|WARNING)\s*:?\s+(.*)$", s)
        if m:
            findings.append(f"msg {m.group(1)} {m.group(2)[:120]}")

    return sorted(set(findings))


def _next_is_ruff_location(lines: list[str], i: int) -> bool:
    return i + 1 < len(lines) and bool(re.match(r"^\s*-->", lines[i + 1]))


def _ruff_location(lines: list[str], i: int) -> str:
    nxt = lines[i + 1]
    m = re.search(r"-->\s*(\S+)", nxt)
    return m.group(1) if m else "?"


class GateResult(TypedDict):
    exit: int
    findings: list[str]
    unusable: bool


def _run_gates(cwd: Path | None = None) -> dict[str, GateResult]:
    results: dict[str, GateResult] = {}
    for cmd in _gate_commands():
        rc, out = _run(["bash", "-lc", cmd], cwd=cwd)
        findings = sorted(_finding_lines(out))
        # A non-zero exit with nothing parseable means the gate itself is
        # broken (missing tool, bad config, crash) rather than reporting
        # findings. Treating that as "zero findings" would let anything pass.
        unusable = rc != 0 and not findings and _looks_unrunnable(out)
        results[cmd] = {"exit": rc, "findings": findings, "unusable": unusable}
    return results


def _looks_unrunnable(output: str) -> bool:
    low = output.lower()
    markers = (
        "no module named",
        "command not found",
        "not installed",
        "traceback (most recent call last)",
        "syntaxerror",
        "importerror",
        "modulenotfounderror",
    )
    return any(m in low for m in markers)


def _findings(result: object) -> list[str]:
    """Read findings out of a possibly-untrusted baseline entry."""
    if isinstance(result, dict):
        found = result.get("findings")
        if isinstance(found, list):
            return [str(x) for x in found]
    return []


def cmd_baseline(_: argparse.Namespace) -> int:
    gates = _gate_commands()
    if not gates:
        print(f"no gates in {GATES_FILE}", file=sys.stderr)
        return 1
    print("running gates to record baseline...")
    results = _run_gates()
    broken = [c for c, r in results.items() if r["unusable"]]
    if broken:
        # Refuse before writing anything. An unreadable gate would otherwise
        # leave a valid-looking empty baseline on disk, which silently permits
        # every later regression.
        print("could not read findings from:", file=sys.stderr)
        for cmd in broken:
            print(f"  {cmd}", file=sys.stderr)
        print(
            "\nfix these before baselining — an unreadable gate records an empty\n"
            "baseline, which makes every later regression invisible.",
            file=sys.stderr,
        )
        return 1

    BASELINE.write_text(
        json.dumps({"date": date.today().isoformat(), "gates": results}, indent=2),
        encoding="utf-8",
    )
    total = sum(len(v["findings"]) for v in results.values())
    print(f"recorded {BASELINE} ({total} existing findings)")
    for cmd, r in results.items():
        mark = "OK  " if r["exit"] == 0 else "FAIL"
        print(f"  {mark} {cmd}  ({len(r['findings'])} findings)")
    print("\nBaseline red is fine. The point is that a slice may not ADD findings.")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    if not GATES_FILE.exists():
        print(f"no {GATES_FILE}", file=sys.stderr)
        return 1
    cwd = Path(args.dir) if args.dir else None
    results = _run_gates(cwd)
    baseline = {}
    if BASELINE.exists():
        try:
            loaded = json.loads(BASELINE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                gates = loaded.get("gates")
                if isinstance(gates, dict):
                    baseline = gates
        except json.JSONDecodeError:
            print("baseline.json unreadable; treating as empty", file=sys.stderr)

    print(f"\n{'gate':<40} {'exit':>4} {'now':>5} {'base':>5}  verdict")
    print("-" * 78)
    regressions: list[str] = []
    for cmd, r in results.items():
        base_findings = _findings(baseline.get(cmd))
        now_findings = r["findings"]
        added = sorted(set(now_findings) - set(base_findings))
        removed = len(set(base_findings) - set(now_findings))
        if base_findings:
            if added:
                note = f"REGRESSION +{len(added)}"
                regressions.extend(f"  {cmd}: {a}" for a in added)
            elif removed:
                note = f"ok (baseline -{removed})"
            else:
                note = "ok (matches baseline)"
        elif r["exit"] == 0:
            note = "ok (clean)"
        else:
            if r["unusable"]:
                note = "UNUSABLE (gate did not run)"
                regressions.append(f"  {cmd}: gate did not run, cannot judge")
            else:
                note = "FAIL (no baseline)"
                if args.strict:
                    regressions.append(f"  {cmd}: exit {r['exit']}, no baseline")
        print(
            f"{cmd[:39]:<40} {r['exit']:>4} {len(now_findings):>5} {len(base_findings):>5}  {note}"
        )

    if regressions:
        print("\nregressions (a slice may not add these):")
        for line in regressions:
            print(line)
        return 1
    print("\nno regressions against baseline")
    return 0


def _current_hypothesis(spec: str) -> str | None:
    """Find the current hypothesis sentence.

    Accept both shapes the template and a human might write:
      ``**Hypothesis:** if we ...``            (bold inline label)
      ``## Hypothesis\\n\\nIf we ...``         (heading followed by text)
    """
    m = re.search(r"\*\*Hypothesis:?\*\*:?\s*(.+)", spec)
    if m and m.group(1).strip():
        return " ".join(m.group(1).split())
    m = re.search(r"^#{1,4}\s*Hypothesis\b\s*\n+(.+)$", spec, re.M)
    if m and m.group(1).strip():
        return " ".join(m.group(1).split())
    return None


# --- hypothesis shape checking -------------------------------------------
#
# "Falsifiable" is the load-bearing word of this whole workflow. Left as prose
# in a prompt it degrades within a few rounds, because nothing enforces it.
# These checks make it mechanical: a hypothesis must name three things, and the
# observable outcome must not be a word that cannot be measured.

SLOTS = ("Change", "Outcome", "Verify by")

# Words that cannot be observed, so an outcome containing one is not falsifiable.
# Deliberately over-inclusive: a false positive costs the shaper one rewrite of
# a sentence, a false negative ships a slice that proves nothing.
VAGUE_TERMS = (
    "as appropriate",
    "as needed",
    "as necessary",
    "as much as possible",
    "if possible",
    "to be determined",
    "to be decided",
    "to be worked out",
    "user-friendly",
    "best possible",
    "best practice",
    "intuitive",
    "intuitiveness",
    "seamless",
    "seamlessly",
    "elegant",
    "elegantly",
    "robust",
    "properly",
    "correctly",
    "appropriately",
    "adequately",
    "reasonably",
    "suitable",
    "optimal",
    "efficient",
    "efficiently",
    "performant",
    "scalable",
    "flexible",
    "straightforward",
    "significant",
    "substantially",
    "improve",
    "improved",
    "improvement",
    "better",
    "faster",
    "quicker",
    "quickly",
    "enough",
    "good",
    "nice",
    "clean",
    "simple",
    "proper",
    "tbd",
    "todo",
    "???",
)

# Derived forms a \b-only match misses: "cleaner", "simpler", "improves",
# "fastest". A \b-only check skips exactly the words people reach for when they
# are being vague, which defeats the purpose.
#
# Listed explicitly rather than generated from stems: English does not derive
# predictably (simple -> simpler drops the e), and guessing produced false
# positives on unrelated words like "goods". An explicit list is auditable.
VAGUE_DERIVED = (
    "cleaner",
    "cleanest",
    "cleanly",
    "simpler",
    "simplest",
    "improves",
    "improving",
    "improved",
    "better",
    "best",
    "faster",
    "fastest",
    "quicker",
    "quickest",
    "slower",
    "slowly",
    "slowness",
    "more",
    "less",
    "fewer",
    "most",
    "least",
    "higher",
    "lower",
    "larger",
    "bigger",
    "smaller",
    "greater",
    "worse",
    "worst",
    "degrades",
    "degrade",
    "degraded",
    "efficiently",
    "efficiency",
    "inefficient",
    "robustness",
    "properly",
    "proper",
    "goodness",
    "good",
    "safely",
    "safety",
    "nicer",
    "nicest",
    "optimally",
    "optimum",
    "smoothly",
    "smooth",
    "elegantly",
    "elegant",
    "usability",
    "readability",
    "maintainability",
    "reliability",
)

_ALL_VAGUE = tuple(VAGUE_TERMS) + VAGUE_DERIVED

_VAGUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_ALL_VAGUE, key=len, reverse=True)) + r")\b",
    re.IGNORECASE,
)


_NUMERIC = re.compile(r"\d")

# Of the vague words, these are comparative: vague alone ("fewer errors"),
# measurable with a number present ("0 errors instead of 3").
_COMPARATIVES = (
    "more",
    "less",
    "fewer",
    "most",
    "least",
    "higher",
    "lower",
    "larger",
    "bigger",
    "smaller",
    "greater",
    "better",
    "best",
    "worse",
    "worst",
    "faster",
    "fastest",
    "quicker",
    "quickest",
    "slower",
    "improves",
    "improved",
    "improvement",
    "degrades",
    "degrade",
    "degraded",
    "increase",
    "increases",
    "increase",
    "decrease",
    "decreases",
    "reduce",
    "reduces",
    "saves",
    "gain",
    "savings",
)

# Comparative words are vague on their own ("fewer errors") but measurable when
# a number is nearby ("fewer errors, 0 instead of 3"). The number is the actual
# test, so exempt the word rather than special-casing every phrase.
_COMPARATIVE_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _COMPARATIVES) + r")\b", re.IGNORECASE
)


def _vague_hits(text: str) -> list[tuple[str, int, str]]:
    """Return (term, line number, line) for each vague term in `text`.

    A comparative word on a line that also contains a number is not a finding:
    "12 rows instead of 15" is measurable, "more rows" is not.
    """
    hits = []
    for n, line in enumerate(text.splitlines(), start=1):
        has_number = bool(_NUMERIC.search(line))
        seen: set[str] = set()
        for m in _VAGUE_RE.finditer(line):
            term = m.group(1).lower()
            if term in seen:
                continue
            # Exempt a comparative ONLY when this line carries a number.
            # "12 rows instead of 15" is measurable; "more rows" is not.
            if has_number and _COMPARATIVE_RE.fullmatch(term):
                continue
            seen.add(term)
            hits.append((term, n, line.strip()))
    return hits


# Placeholder markers. A starter template ships with `_(..._)` slots; those
# must read as "not written yet", not as a filled-in value. Without this the
# blank starter spec passes the falsifiability check, which is the worst
# possible failure: the gate says yes before anyone has thought.
_PLACEHOLDERS = re.compile(
    r"_\(.*?\)_|^<.*>$|^\.\.\.$|^\s*tbd\s*$|\bTODO\b",
    re.IGNORECASE,
)


def _is_placeholder(value: str) -> bool:
    """True if a slot value is still the unfilled starter template."""
    return not value.strip() or bool(_PLACEHOLDERS.search(value.strip()))


def _slot_values(spec: str) -> dict[str, str | None]:
    """Extract the three required slot values from a spec.

    A slot present but still holding the starter template's placeholder is
    reported as missing, not as filled in.
    """
    out: dict[str, str | None] = {}
    for slot in SLOTS:
        m = re.search(rf"^\s*\*\*{re.escape(slot)}:?\*\*:?\s*(.+)$", spec, re.M)
        if not m:
            out[slot] = None
            continue
        value = m.group(1).strip()
        out[slot] = None if _is_placeholder(value) else value
    return out


def check_spec(spec: str) -> tuple[list[str], list[str]]:
    """Check a spec's hypothesis. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    slots = _slot_values(spec)
    missing = [s for s in SLOTS if not slots[s]]
    if missing:
        errors.append(
            "the hypothesis must name three things, and these are missing: "
            + ", ".join(missing)
            + "\n      use this shape:\n"
            + "\n".join(f"      **{s}:** <...>" for s in SLOTS)
        )
        return errors, warnings

    outcome = slots["Outcome"] or ""
    hits = _vague_hits(outcome)
    if hits:
        listed = ", ".join(sorted({h[0] for h in hits}))
        errors.append(
            f"the outcome is not measurable — it contains: {listed}\n"
            f'      "{outcome.strip()[:120]}"\n'
            f"      rewrite it as something an observation could prove false.\n"
            f'      bad:  it gets "better"\n'
            f"      good: second open of the same feed is under 200ms, timed with curl -w"
        )

    for slot in ("Change", "Verify by"):
        value = slots[slot] or ""
        for term, line_no, line in _vague_hits(value):
            warnings.append(f"**{slot}** (line {line_no}) contains {term!r}: {line[:100]}")

    # Warn about vague wording in the rest of the spec, but skip the template's
    # own instructional text. Scanning the commentary is how you get a check
    # nobody reads.
    prose = _strip_html_comments(spec)
    seen: set[str] = set()
    for term, line_no, line in _vague_hits(prose):
        msg = f"line {line_no} contains {term!r}: {line[:100]}"
        if msg not in seen:
            seen.add(msg)
            warnings.append(msg)
    return errors, warnings


def _strip_html_comments(spec: str) -> str:
    """Blank out <!-- ... --> guidance so it is not scanned for vagueness."""
    return re.sub(r"<!--.*?-->", lambda m: "\n" * m.group(0).count("\n"), spec, flags=re.S)


def cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.file) if args.file else SPEC
    if not path.exists():
        print(f"no {path} — run `python -m tools.prophet init` first", file=sys.stderr)
        return 1
    spec = path.read_text(encoding="utf-8")
    slots = _slot_values(spec)
    if _current_hypothesis(spec) is None and not slots["Change"]:
        print(f"no hypothesis in {path}")
        print("expected these three lines under a '## Hypothesis' heading:")
        for slot in SLOTS:
            print(f"  **{slot}:** <...>")
        return 1

    errors, warnings = check_spec(spec)

    t = re.search(r"\*\*Tier:?\*\*:?\s*(.*)", spec)
    tier = t.group(1).strip() if t else "?"

    if warnings:
        print("warnings (vague wording outside the outcome; usually fine):")
        for w in warnings[:20]:
            print(f"  {w}")
        if len(warnings) > 20:
            print(f"  ... and {len(warnings) - 20} more")
        print()

    if errors:
        print(f"hypothesis is not yet falsifiable  (tier {tier}):")
        for e in errors:
            print(f"  - {e}")
        return 1

    print(f"hypothesis is falsifiable  (tier {tier})")
    for slot in SLOTS:
        print(f"  {slot:<11} {(slots[slot] or '')[:90]}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    if not SPEC.exists():
        print("no .prophet/ — run `python -m tools.prophet init`", file=sys.stderr)
        return 1
    spec = SPEC.read_text(encoding="utf-8")
    hypothesis = _current_hypothesis(spec)
    if hypothesis:
        print("current hypothesis:")
        print(f"  {hypothesis[:300]}")
    else:
        print("current hypothesis: (none written yet)")
    t = re.search(r"\*\*Tier:?\*\*:?\s*(.*)", spec)
    if t:
        print(f"tier: {t.group(1).strip()}")
    if BASELINE.exists():
        try:
            b = json.loads(BASELINE.read_text(encoding="utf-8"))
            total = sum(len(v["findings"]) for v in b.get("gates", {}).values())
            print(f"baseline: {b.get('date')} ({total} findings)")
        except json.JSONDecodeError:
            print("baseline: unreadable")
    if LOG.exists():
        rounds = re.findall(r"^## R(\d+)", LOG.read_text(encoding="utf-8"), re.M)
        print(f"rounds logged: {len(rounds)}" + (f" (latest R{rounds[-1]})" if rounds else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="prophet", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create .prophet/ with starter files").set_defaults(fn=cmd_init)
    sp = sub.add_parser("new", help="create a worktree + branch for a slice")
    sp.add_argument("name", help="slice name")
    sp.add_argument("--base", help="base ref (default HEAD)")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("baseline", help="record current gate findings")
    sp.set_defaults(fn=cmd_baseline)

    sp = sub.add_parser("gates", help="run gates, compare against baseline")
    sp.add_argument("--dir", help="run in this directory (e.g. a worktree)")
    sp.add_argument(
        "--strict",
        action="store_true",
        help="also fail when a gate is red with no baseline recorded",
    )
    sp.set_defaults(fn=cmd_gates)

    sub.add_parser("status", help="show current hypothesis and round count").set_defaults(
        fn=cmd_status
    )

    sp = sub.add_parser("check", help="verify the current hypothesis is actually falsifiable")
    sp.add_argument("--file", help="path to a spec file (default .prophet/spec.md)")
    sp.set_defaults(fn=cmd_check)

    args = p.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
