#!/usr/bin/env python3
"""Mine docs/dev-records for tracked incident IDs and verify each defect
is covered by a gate or a regression test.

Incident classes:
  - FIX-*/BUG-*/INCIDENT-*/REGRESSION-* : defects — require status
    "covered" in ci/gates-coverage.json with >=1 existing gate or test.
  - AUDIT-*/EVAL-*/TEST-*               : process/review tasks — require a
    manifest entry (any status) so unfinished audits stay visible.

`--check` exits 1 on: an incident ID missing from the manifest, a covered
entry with no gate/test, or a referenced gate/test that does not exist.
Without --check it prints the same report and always exits 0.

Also prints a failure-keyword heatmap (informational): which records
concentrate which defect vocabulary — a rising count on a new record is
the signal to add a gate, not a failure by itself.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RECORDS = ROOT / "docs" / "dev-records"
MANIFEST = ROOT / "ci" / "gates-coverage.json"
TESTS = ROOT / "tests"

_DEFECT_ID = re.compile(
    r"\b(?:FIX|BUG|INCIDENT|HOTFIX|REGRESSION)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
_PROCESS_ID = re.compile(
    r"\b(?:AUDIT|EVAL|TEST)-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")

_KEYWORDS = [
    "flock", "lock", "retry", "fallback", "rollback", "duplicate",
    "partial", "timeout", "欠陥", "失敗", "regress", "secret",
    "credential", "payload", "orphan", "race",
]


def extract_ids() -> dict[str, set[str]]:
    defects, process = set(), set()
    # rglob — incident IDs in a nested record must be extracted too; a
    # non-recursive glob silently skipped subdirectory files (FIX-G2)
    for path in sorted(RECORDS.rglob("*")):
        if not path.is_file() or path.suffix not in {".md", ".json"}:
            continue
        text = path.read_text(encoding="utf-8")
        defects.update(_DEFECT_ID.findall(text))
        process.update(_PROCESS_ID.findall(text))
    return {"defects": defects, "process": process}


def gate_registry() -> set[str]:
    text = (ROOT / "ci" / "gates.py").read_text(encoding="utf-8")
    m = re.search(r"^GATES = \{(.*?)^\}", text, re.S | re.M)
    return set(re.findall(r'"([a-z_]+)"\s*:', m.group(1))) if m else set()


def test_names() -> set[str]:
    names = set()
    for path in TESTS.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names.update(node.name for node in ast.walk(tree)
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                     and node.name.startswith("test_"))
    return names


def heatmap() -> None:
    print("\n## defect-keyword heatmap (informational)")
    for path in sorted(RECORDS.rglob("*.md")):
        text = path.read_text(encoding="utf-8").lower()
        hits = {k: text.count(k) for k in _KEYWORDS}
        top = sorted(hits.items(), key=lambda kv: -kv[1])[:5]
        print(f"  {path.name}: "
              + ", ".join(f"{k}={v}" for k, v in top if v))


def main() -> int:
    check = "--check" in sys.argv
    ids = extract_ids()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) \
        if MANIFEST.exists() else {"incidents": {}}
    entries = manifest.get("incidents", {})
    gates = gate_registry()
    tests = test_names()
    problems: list[str] = []

    print("## defect incidents (require coverage)")
    for iid in sorted(ids["defects"]):
        e = entries.get(iid)
        if e is None:
            problems.append(f"{iid}: no manifest entry")
            print(f"  UNCOVERED {iid}")
            continue
        if e.get("status") != "covered":
            problems.append(f"{iid}: status={e.get('status')!r}")
            print(f"  {e.get('status', '?').upper()} {iid} — "
                  f"{e.get('summary', '')}")
            continue
        refs = list(e.get("gates", [])) + list(e.get("tests", []))
        if not refs:
            problems.append(f"{iid}: covered but no gate/test listed")
        problems += [
            f"{iid}: gate {g!r} not in gates.py"
            for g in e.get("gates", []) if g not in gates]
        problems += [
            f"{iid}: test {t!r} not in tests/"
            for t in e.get("tests", []) if t not in tests]
        print(f"  COVERED  {iid} — {e.get('summary', '')} [{len(refs)} refs]")

    print("\n## process/review tasks (visibility only)")
    for iid in sorted(ids["process"]):
        e = entries.get(iid)
        status = e.get("status", "MISSING") if e else "MISSING"
        if e is None:
            problems.append(f"{iid}: no manifest entry")
        print(f"  {status.upper():<10} {iid} — "
              f"{(e or {}).get('summary', '')}")

    heatmap()

    if problems:
        print(f"\n{len(problems)} coverage problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1 if check else 0
    print("\nall extracted incidents covered or tracked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
