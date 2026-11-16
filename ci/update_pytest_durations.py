#!/usr/bin/env python3
"""Rebuild a lane's per-file pytest seconds from that lane's CI junit XML files.

Use the shard-*.xml files of one complete CI run of the same lane (the
"pytest diagnostics" artifact), for example:

    python3 ci/update_pytest_durations.py --output ci/pytest_durations.linux.json shard-*.xml

The XML must come from the runner (junit_family=xunit1 writes each test's
file). A run with failures or errors is refused, because a test that stops
early would be recorded as cheap. Files whose tests mostly skipped and took
almost no time are left out, so they weigh the lane's median instead of zero
(for example macOS-only files in a Linux run). The "split" list of the
existing output file is kept. Never run from tests or the periodic jobs.
"""
import argparse
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def _file_from_classname(classname, root):
    """Longest prefix of a dotted classname that names an existing test file."""
    parts = classname.split(".")
    for end in range(len(parts), 0, -1):
        candidate = "/".join(parts[:end]) + ".py"
        if (root / candidate).is_file():
            return candidate
    return None


def collect(paths, root=ROOT):
    """Return {file: {"seconds", "cases", "skipped", "failed"}} from junit files."""
    cases = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            name = case.get("file") or _file_from_classname(case.get("classname") or "", root)
            if not name or name.startswith(("/", "..")):
                continue
            key = (name, case.get("classname"), case.get("name"))
            seconds = float(case.get("time") or 0)
            skipped = case.find("skipped") is not None
            failed = case.find("failure") is not None or case.find("error") is not None
            previous = cases.get(key)
            if previous is not None:   # one testcase can repeat (call failure + teardown error)
                seconds = max(seconds, previous[0])
                skipped, failed = skipped and previous[1], failed or previous[2]
            cases[key] = (seconds, skipped, failed)
    files = {}
    for (name, _, _), (seconds, skipped, failed) in cases.items():
        row = files.setdefault(name, {"seconds": 0.0, "cases": 0, "skipped": 0, "failed": 0})
        row["seconds"] += seconds
        row["cases"] += 1
        row["skipped"] += int(skipped)
        row["failed"] += int(failed)
    return files


def table(files, min_seconds):
    """Recorded seconds per file, without fast files that mostly skipped."""
    kept, dropped = {}, []
    for name, row in sorted(files.items()):
        if row["skipped"] * 2 >= row["cases"] and row["seconds"] < min_seconds:
            dropped.append(name)
        else:
            kept[name] = round(row["seconds"], 1)
    return kept, dropped


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("junit", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True,
                        help="durations file to write, e.g. ci/pytest_durations.linux.json")
    parser.add_argument("--min-seconds", type=float, default=0.5,
                        help="mostly-skipped files below this many seconds are left out (default 0.5)")
    parser.add_argument("--allow-failures", action="store_true",
                        help="record a run with failures or errors anyway")
    args = parser.parse_args(argv)
    try:
        files = collect(args.junit)
    except (OSError, ET.ParseError) as error:
        parser.error(f"unreadable junit XML: {error}")
    if not files:
        parser.error("the junit XML has no testcases with a file")
    failed = sum(row["failed"] for row in files.values())
    if failed and not args.allow_failures:
        print(f"refusing: {failed} testcases failed or errored; record a complete passing run",
              file=sys.stderr)
        return 2
    kept, dropped = table(files, args.min_seconds)
    split = []
    try:
        previous = json.loads(args.output.read_text())
        split = sorted(previous.get("split", [])) if isinstance(previous, dict) else []
    except (OSError, ValueError):
        pass
    args.output.write_text(json.dumps({"files": kept, "split": split}, indent=1, sort_keys=True) + "\n")
    total = sum(kept.values())
    print(f"{args.output}: {len(kept)} files, {total:.1f} s; left out {len(dropped)} mostly-skipped files")
    for name, seconds in sorted(kept.items(), key=lambda item: (-item[1], item[0]))[:5]:
        print(f"  {seconds:8.1f} s  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
