#!/usr/bin/env python3
"""Run the CI lane's tests as N parallel shards with isolated test runners.

Without --slice the shards are sized by the host (local and cloud runs). With
--slice K/M --per-slice P the lane is cut into M*P shards in total and this
process runs only slice K's P shards, so M matrix jobs together run every
shard once; --verify-reports then checks the M jobs' shard reports together.
MCS_CI_SKIP_PERF=1 deselects tests marked perf, and MCS_CI_TEST_SCOPE=docs
runs only the tests a documentation change can affect (ci/ci_scope.py).
"""
import argparse
from collections import deque
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
# Directory targets keep the root conftest's collect_ignore_glob active.
LANES = {
    "linux": ("tests", "integration"),
    "macos": ("tests/meta", "tests/ops"),
}
MAX_SHARDS = 64
TAIL_LINES = 20
REPORT_NAME = re.compile(r"shard-([0-9]+)\.report\.json")


def _relay(name, stream, log, tail):
    with stream, log:
        for line in stream:
            log.write(line)
            log.flush()
            tail.append(line)
            print(f"[{name}] {line}", end="", flush=True)


def _stop_children(processes):
    for process in processes:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        # The shell can exit before a grandchild that still owns its pipes.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _terminate(signum, _frame):
    raise SystemExit(128 + signum)


def _cpu_count():
    counter = getattr(os, "process_cpu_count", None) or os.cpu_count
    return counter() or 2


def _shard_count(parser, value):
    if value is None:
        value = os.environ.get("MCS_TEST_SHARDS") or None
    if value is None:
        return max(2, min(_cpu_count(), 8))
    try:
        count = int(value)
    except ValueError:
        count = 0
    if not 1 <= count <= MAX_SHARDS:
        parser.error(f"shard count must be an integer from 1 to {MAX_SHARDS}, got {value!r}")
    return count


def _positive(text):
    return int(text) if str(text).isdigit() and int(text) >= 1 else None


def _partition(parser, args):
    """Return (indexes this process runs, total shards, slice label or None)."""
    if args.slice is None:
        if args.per_slice is not None:
            parser.error("--per-slice needs --slice or --verify-reports")
        count = _shard_count(parser, args.shards)
        return list(range(1, count + 1)), count, None
    if args.shards is not None:
        parser.error("--slice and --shards cannot be combined: --per-slice sets this job's shards")
    number, sep, slices = args.slice.partition("/")
    number, slices, per_slice = _positive(number), _positive(slices), _positive(args.per_slice)
    if not sep or number is None or slices is None or number > slices:
        parser.error(f"--slice must look like K/M with 1 <= K <= M, got {args.slice!r}")
    if per_slice is None:
        parser.error("--slice needs --per-slice P with P >= 1")
    total = _total(parser, slices, per_slice)
    return slice_indexes(number, per_slice), total, f"{number}/{slices}"


def _total(parser, slices, per_slice):
    total = slices * per_slice
    if total > MAX_SHARDS:
        parser.error(f"slices x per-slice must be at most {MAX_SHARDS}, got {total}")
    return total


def slice_indexes(number, per_slice):
    """1-based shard indexes of slice `number` (1-based) when every slice runs `per_slice` shards."""
    return list(range((number - 1) * per_slice + 1, number * per_slice + 1))


def _env_choice(parser, name, choices):
    value = os.environ.get(name, "")
    if value not in choices:
        parser.error(f"{name} must be one of {sorted(choices)}, got {value!r}")
    return value


def _ci_scope():
    spec = importlib.util.spec_from_file_location("_mcs_ci_scope", ROOT / "ci/ci_scope.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _junit_cases(path):
    """Distinct testcases in a junit file (a call failure plus a teardown error repeat one)."""
    root = ET.parse(path).getroot()
    return {(case.get("file"), case.get("classname"), case.get("name"))
            for case in root.iter("testcase")}


def union_problems(loaded, total):
    """Problems that keep the reports of shards 1..total from proving one exact run.

    Every shard must have collected the same tests, and the selections must add
    up to that collection with each test selected exactly once."""
    if sorted(loaded) != list(range(1, total + 1)):
        return [f"reports for shards {sorted(set(range(1, total + 1)) - set(loaded))} are missing"]
    first = loaded[1]
    if any(r["total"] != first["total"] or r["total_digest"] != first["total_digest"]
           for r in loaded.values()):
        return ["shards collected different test sets"]
    selected = sum(r["selected"] for r in loaded.values())
    if selected != first["total"]:
        return [f"shards selected {selected} of {first['total']} collected tests"]
    if sum(int(r["selected_sum"], 16) for r in loaded.values()) % (1 << 256) != int(first["total_sum"], 16):
        return ["shards did not select every collected test exactly once"]
    if first["total"] == 0:
        return ["no tests were collected"]
    return []


def _empty(report, total):
    """A shard with nothing to run: only valid when the lane has fewer files than shards."""
    return report["selected"] == 0 and report["files_selected"] == 0 and report["files_total"] < total


def _check(results, reports, xmls, indexes, total):
    """Return (problems, warnings, reports): problems make the run incomplete even when every shard exited 0."""
    problems, warnings, loaded = [], [], {}
    for index in indexes:
        name = f"shard-{index}"
        try:
            report = json.loads(reports[index].read_text())
        except (OSError, ValueError):
            problems.append(f"{name}: no readable shard report (the process died before or during collection)")
            continue
        if not report.get("finished"):
            problems.append(f"{name}: the shard report was never completed (the process died during the run)")
            continue
        if report.get("index") != index or report.get("count") != total:
            problems.append(f"{name}: the report describes shard {report.get('index')}/{report.get('count')}")
            continue
        loaded[index] = report
        empty = _empty(report, total)
        if results[name] == 5 and empty:
            results[name] = 0   # no tests collected: more shards than files
        ran_all = report["ran"] == report["selected"]
        if not ran_all:
            problems.append(f"{name}: ran {report['ran']} of {report['selected']} selected tests")
        try:
            cases = len(_junit_cases(xmls[index]))
        except (OSError, ET.ParseError):
            if not empty:
                problems.append(f"{name}: no readable junit XML")
            continue
        if cases != report["selected"] + report["collect_not_passed"]:
            # The finished report already proves every selected test ran; the
            # junit count depends on how the pinned pytest writes testcases.
            (warnings if ran_all else problems).append(
                f"{name}: junit has {cases} testcases, expected {report['selected']} selected"
                f" + {report['collect_not_passed']} collection reports")
    if len(loaded) == len(indexes):
        if len(indexes) == total:
            problems += union_problems(loaded, total)
        else:
            # A slice sees only its own shards: they must agree on the collection,
            # and --verify-reports later proves the union of every slice.
            first = loaded[indexes[0]]
            if any(r["total"] != first["total"] or r["total_digest"] != first["total_digest"]
                   or r["total_sum"] != first["total_sum"] for r in loaded.values()):
                problems.append("shards collected different test sets")
            elif first["total"] == 0:
                problems.append("no tests were collected")
    return problems, warnings, loaded


def verify_reports(directory, slices, per_slice):
    """Problems with the shard reports that M slice jobs left under `directory` (searched recursively)."""
    total = slices * per_slice
    found = {}
    for path in sorted(Path(directory).rglob("shard-*.report.json")):
        match = REPORT_NAME.fullmatch(path.name)
        if match:
            found.setdefault(int(match.group(1)), []).append(path)
    problems = [f"shard-{index}: a report for a shard outside 1..{total}" for index in sorted(found)
                if not 1 <= index <= total]
    loaded = {}
    for index in range(1, total + 1):
        name = f"shard-{index}"
        paths = found.get(index, [])
        if len(paths) != 1:
            problems.append(f"{name}: {len(paths)} reports (slice {(index - 1) // per_slice + 1}"
                            " must upload exactly one)")
            continue
        try:
            report = json.loads(paths[0].read_text())
        except (OSError, ValueError):
            problems.append(f"{name}: unreadable report {paths[0]}")
            continue
        if not isinstance(report, dict) or not report.get("finished"):
            problems.append(f"{name}: the shard report was never completed")
            continue
        try:
            if report["index"] != index or report["count"] != total:
                problems.append(f"{name}: the report describes shard {report['index']}/{report['count']},"
                                f" expected {index}/{total} ({slices} slices x {per_slice})")
                continue
            if report["ran"] != report["selected"]:
                problems.append(f"{name}: ran {report['ran']} of {report['selected']} selected tests")
                continue
            if not (report["exitstatus"] == 0 or (report["exitstatus"] == 5 and _empty(report, total))):
                problems.append(f"{name}: pytest exit status {report['exitstatus']}")
                continue
            int(report["selected_sum"], 16), int(report["total_sum"], 16)
        except (KeyError, TypeError, ValueError):
            problems.append(f"{name}: the report lacks the fields of a shard report")
            continue
        loaded[index] = report
    if not problems:
        problems = union_problems(loaded, total)
    return problems, loaded


def _verify_main(parser, args):
    if args.slice is not None or args.shards is not None or args.output_dir is not None:
        parser.error("--verify-reports cannot be combined with --slice, --shards or --output-dir")
    slices, per_slice = _positive(args.slices), _positive(args.per_slice)
    if slices is None or per_slice is None:
        parser.error("--verify-reports needs --slices M and --per-slice P with M, P >= 1")
    total = _total(parser, slices, per_slice)
    problems, loaded = verify_reports(args.verify_reports, slices, per_slice)
    for problem in problems:
        print(f"incomplete run: {problem}", file=sys.stderr, flush=True)
    if problems:
        return 1
    first = loaded[1]
    print(f"{args.lane}: {slices} slices x {per_slice} shards = {total} shard reports;"
          f" {first['total']} collected tests, each selected and run exactly once"
          f" (collection sha256 {first['total_digest']})", flush=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--shards", help="number of shards (default: MCS_TEST_SHARDS, else CPUs from 2 to 8)")
    parser.add_argument("--slice", metavar="K/M",
                        help="run slice K of M; the lane has M*P shards in total (MCS_TEST_SHARDS is ignored)")
    parser.add_argument("--per-slice", metavar="P", help="shards per slice, with --slice or --verify-reports")
    parser.add_argument("--verify-reports", type=Path, metavar="DIR",
                        help="check the shard reports that every slice left under DIR, then exit")
    parser.add_argument("--slices", metavar="M", help="number of slices, with --verify-reports")
    args = parser.parse_args(argv)
    if args.verify_reports is not None:
        return _verify_main(parser, args)
    if args.slices is not None:
        parser.error("--slices needs --verify-reports (a test run takes --slice K/M)")
    indexes, total, label = _partition(parser, args)
    skip_perf = _env_choice(parser, "MCS_CI_SKIP_PERF", {"", "0", "1"}) == "1"
    scope = _env_choice(parser, "MCS_CI_TEST_SCOPE", {"", "full", "docs"})
    if args.output_dir is None:
        parent = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())).resolve()
        if parent == ROOT or ROOT in parent.parents:
            parser.error("report directory must be outside the repository")
        output = Path(tempfile.mkdtemp(prefix=f"mcs-pytest-{args.lane}-",
                                       dir=parent))
    else:
        output = args.output_dir.resolve()
        if output == ROOT or ROOT in output.parents:
            parser.error("--output-dir must be outside the repository")
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            parser.error("--output-dir must be empty")
        output.mkdir(parents=True, exist_ok=True)
    print(f"pytest reports: {output}", flush=True)
    if label is None:
        print(f"pytest shards: {total} (usable CPUs: {_cpu_count()})", flush=True)
    else:
        if os.environ.get("MCS_TEST_SHARDS"):
            print("MCS_TEST_SHARDS is ignored: --per-slice sets this job's shards", flush=True)
        print(f"pytest shards: {indexes[0]}-{indexes[-1]} of {total} (slice {label};"
              f" usable CPUs: {_cpu_count()})", flush=True)
    extra = []
    if skip_perf:
        print("perf tests: deselected (MCS_CI_SKIP_PERF=1)", flush=True)
        extra += ["-m", "not perf"]
    if scope == "docs":
        files = _ci_scope().doc_test_files(ROOT, LANES[args.lane])
        only = output / "only-files.txt"
        only.write_text("".join(f"{name}\n" for name in files))
        print(f"test scope: docs, {len(files)} test files that documents can affect:", flush=True)
        for name in files:
            print(f"  {name}", flush=True)
        extra.append(f"--mcs-only-files={only}")
    durations = ROOT / f"ci/pytest_durations.{args.lane}.json"
    xmls = {i: output / f"shard-{i}.xml" for i in indexes}
    reports = {i: output / f"shard-{i}.report.json" for i in indexes}
    processes, threads, results, tails, started, elapsed = {}, [], {}, {}, {}, {}
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    try:
        for index in indexes:
            name = f"shard-{index}"
            command = ["sh", str(ROOT / "scripts/run_tests.sh"), *LANES[args.lane],
                       f"--mcs-shard={index}/{total}", f"--mcs-shard-durations={durations}",
                       f"--mcs-shard-report={reports[index]}", *extra,
                       "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
                       "-r", "fEs", "--durations=30", f"--junitxml={xmls[index]}"]
            log = (output / f"{name}.log").open("w")
            try:
                # run_tests.sh sizes its /dev/shm free-space check for the shards
                # that start together on this host (a slice's, not the lane's total).
                process = subprocess.Popen(command, cwd=ROOT,
                                           env={**os.environ, "MCS_TEST_PARALLEL": str(len(indexes))},
                                           stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, errors="replace",
                                           start_new_session=True)
            except OSError as error:
                with log:
                    log.write(f"could not start {name}: {error}\n")
                print(f"[{name}] could not start: {error}", file=sys.stderr, flush=True)
                results[name] = 1
                continue
            processes[name], started[name], tails[name] = process, time.monotonic(), deque(maxlen=TAIL_LINES)
            thread = threading.Thread(target=_relay, args=(name, process.stdout, log, tails[name]), daemon=True)
            thread.start()
            threads.append(thread)
        # A failed shard must not cancel the other shards' remaining tests.
        for name, process in processes.items():
            results[name] = process.wait()
            elapsed[name] = time.monotonic() - started[name]
    except KeyboardInterrupt:
        return 130
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        # Even an exited worker can leave a grandchild holding stdout open.
        _stop_children(processes.values())
        for thread in threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in threads):
            print("a worker output stream did not close", file=sys.stderr, flush=True)
            results["output"] = 1
        signal.signal(signal.SIGTERM, previous_term)
    problems, warnings, loaded = _check(results, reports, xmls, indexes, total)
    if loaded:
        report = next(iter(loaded.values()))
        if report["unit"] != "seconds":
            print(f"durations: none usable in {durations.name}, balancing by test count", flush=True)
        heaviest = report.get("heaviest")
        if heaviest and report["target"] and heaviest["weight"] > report["target"]:
            print(f"warning: {heaviest['file']} alone weighs {heaviest['weight']} {report['unit']},"
                  f" more than one shard's share ({report['target']}); list it under \"split\""
                  f" in {durations.name} or use fewer shards", flush=True)
    for index in indexes:
        name = f"shard-{index}"
        code = results[name]
        report = loaded.get(index)
        detail = "" if report is None else (
            f"; tests={report['selected']}; planned={report['planned']} {report['unit']}")
        actual = f"; actual={elapsed[name]:.1f}s" if name in elapsed else ""
        print(f"{name}: exit={code}{detail}{actual}; junit={xmls[index]}", flush=True)
        if (code != 0 or report is None) and tails.get(name):
            print(f"{name}: last {len(tails[name])} lines:", flush=True)
            for line in tails[name]:
                print(f"  {line}", end="" if line.endswith("\n") else "\n", flush=True)
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr, flush=True)
    for problem in problems:
        print(f"incomplete run: {problem}", file=sys.stderr, flush=True)
    return int(bool(problems) or any(code != 0 for code in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
