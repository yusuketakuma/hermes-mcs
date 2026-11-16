#!/usr/bin/env python3
"""Judge a CI gate job (lint-test, macos) from the results of the jobs it needs.

The gate keeps the job name that required checks and the release lanes use,
while the work runs in other jobs. It passes only when the scope job decided
a known mode and every needed job succeeded, or was skipped in a mode where
ci.yml skips it by design. A job skipped because something upstream failed,
or skipped in full mode, fails the gate. Writes `tests=ran|skipped` to
GITHUB_OUTPUT (whether shard reports exist to verify), a line per job to
GITHUB_STEP_SUMMARY, and the verdict to MCS_CI_GATE_DIR/verdict.json.
Standard library only.
"""
import json
import os
from pathlib import Path
import sys


MODES = ("full", "docs", "draft")
# gate -> {needed job: modes in which ci.yml skips that job by design}.
# full allows no skip: a release tag, the schedule and manual runs are full.
GATES = {
    "lint-test": {"scope": (), "lint": (), "pytest-linux": ("draft",)},
    "macos": {"scope": (), "pytest-macos": ("docs", "draft")},
}
# the job whose shard reports the gate verifies when it ran
TESTS = {"lint-test": "pytest-linux", "macos": "pytest-macos"}


def decide(gate, needs):
    """Return (ok, mode, lines, tests_ran) for `gate` given the toJSON(needs) object."""
    table = GATES[gate]
    if not isinstance(needs, dict) or set(needs) != set(table):
        found = sorted(needs) if isinstance(needs, dict) else needs
        return False, None, [f"needs must be exactly {sorted(table)}, got {found}"], False
    results = {job: (value.get("result") if isinstance(value, dict) else None) for job, value in needs.items()}
    outputs = needs["scope"].get("outputs") if isinstance(needs["scope"], dict) else None
    mode = outputs.get("mode") if isinstance(outputs, dict) else None
    lines, ok = [], True
    if results["scope"] != "success":
        return False, None, [f"scope: {results['scope']} (the run's mode is unknown)"], False
    if mode not in MODES:
        return False, None, [f"scope: unknown mode {mode!r}"], False
    for job in sorted(table):
        result = results[job]
        if result == "success":
            lines.append(f"{job}: success")
        elif result == "skipped" and mode in table[job]:
            lines.append(f"{job}: skipped by design in {mode} mode (tests not run)")
        else:
            ok = False
            lines.append(f"{job}: {result} — not allowed in {mode} mode")
    return ok, mode, lines, results[TESTS[gate]] == "success"


def _append(variable, text):
    path = os.environ.get(variable)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1 or argv[0] not in GATES:
        print(f"usage: ci_gate.py {{{'|'.join(GATES)}}}  (reads MCS_CI_NEEDS)", file=sys.stderr)
        return 2
    gate = argv[0]
    try:
        needs = json.loads(os.environ.get("MCS_CI_NEEDS", ""))
    except ValueError:
        needs = None
    ok, mode, lines, tests_ran = decide(gate, needs)
    verdict = "pass" if ok else "fail"
    for line in lines:
        print(f"{gate}: {line}", flush=True)
    print(f"{gate}: {verdict} (mode {mode})", flush=True)
    _append("GITHUB_OUTPUT", f"tests={'ran' if ok and tests_ran else 'skipped'}\n")
    note = "" if not ok or tests_ran else " — tests not run in this mode; this green is not test evidence"
    _append("GITHUB_STEP_SUMMARY", f"### {gate}: {verdict} (mode {mode}){note}\n\n"
            + "".join(f"- {line}\n" for line in lines) + "\n")
    directory = os.environ.get("MCS_CI_GATE_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / "verdict.json").write_text(json.dumps(
            {"gate": gate, "verdict": verdict, "mode": mode, "tests_ran": ok and tests_ran,
             "jobs": lines, "needs": needs}, indent=1, sort_keys=True) + "\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
