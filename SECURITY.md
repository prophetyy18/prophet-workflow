# Security policy

Do not report credentials, private repository data, or exploitable details in a
public issue. Contact the repository owner through the private security-reporting
channel configured by the hosting repository.

## What counts as a workflow integrity issue here

v2 protects very little by design, so the surface is small. Reports are most
useful when they show that a guard which looks load-bearing does not work:

- `prophet baseline` recording a baseline for a gate that did not actually run,
  so later regressions pass unnoticed;
- `_finding_lines` under-reporting a tool's output, so `prophet gates` reports
  "no regressions" when there are some;
- a builder being able to write `.prophet/spec.md`, the one file it is measured
  against.

The helper shells out to the gate commands listed in `.prophet/gates.txt`. Those
commands are the project owner's own; this template does not execute anything
from a repository it is not installed into.

Reports should include the affected version, a minimal reproduction, and expected
versus actual behavior, without including real secrets.
