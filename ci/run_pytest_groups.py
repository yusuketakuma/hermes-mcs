#!/usr/bin/env python3
"""Run the CI lane's tests as N parallel shards with isolated test runners."""
import argparse
from collections import deque
import json
import os
from pathlib import Path
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


def _junit_cases(path):
    """Distinct testcases in a junit file (a call failure plus a teardown error repeat one)."""
    root = ET.parse(path).getroot()
    return {(case.get("file"), case.get("classname"), case.get("name"))
            for case in root.iter("testcase")}


def _check(results, reports, xmls, count):
    """Return (problems, warnings, reports): problems make the run incomplete even when every shard exited 0."""
    problems, warnings, loaded = [], [], {}
    for index in range(1, count + 1):
        name = f"shard-{index}"
        try:
            report = json.loads(reports[index].read_text())
        except (OSError, ValueError):
            problems.append(f"{name}: no readable shard report (the process died before or during collection)")
            continue
        if not report.get("finished"):
            problems.append(f"{name}: the shard report was never completed (the process died during the run)")
            continue
        if report.get("index") != index or report.get("count") != count:
            problems.append(f"{name}: the report describes shard {report.get('index')}/{report.get('count')}")
            continue
        loaded[index] = report
        empty = report["selected"] == 0 and report["files_selected"] == 0 and report["files_total"] < count
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
    if len(loaded) == count:
        first = loaded[1]
        if any(r["total"] != first["total"] or r["total_digest"] != first["total_digest"]
               for r in loaded.values()):
            problems.append("shards collected different test sets")
        elif sum(r["selected"] for r in loaded.values()) != first["total"]:
            problems.append(f"shards selected {sum(r['selected'] for r in loaded.values())}"
                            f" of {first['total']} collected tests")
        elif sum(int(r["selected_sum"], 16) for r in loaded.values()) % (1 << 256) != int(first["total_sum"], 16):
            problems.append("shards did not select every collected test exactly once")
        elif first["total"] == 0:
            problems.append("no tests were collected")
    return problems, warnings, loaded


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=LANES)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--shards", help="number of shards (default: MCS_TEST_SHARDS, else CPUs from 2 to 8)")
    args = parser.parse_args(argv)
    count = _shard_count(parser, args.shards)
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
    print(f"pytest shards: {count} (usable CPUs: {_cpu_count()})", flush=True)
    durations = ROOT / f"ci/pytest_durations.{args.lane}.json"
    xmls = {i: output / f"shard-{i}.xml" for i in range(1, count + 1)}
    reports = {i: output / f"shard-{i}.report.json" for i in range(1, count + 1)}
    processes, threads, results, tails, started, elapsed = {}, [], {}, {}, {}, {}
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    try:
        for index in range(1, count + 1):
            name = f"shard-{index}"
            command = ["sh", str(ROOT / "scripts/run_tests.sh"), *LANES[args.lane],
                       f"--mcs-shard={index}/{count}", f"--mcs-shard-durations={durations}",
                       f"--mcs-shard-report={reports[index]}",
                       "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
                       "-r", "fEs", "--durations=30", f"--junitxml={xmls[index]}"]
            log = (output / f"{name}.log").open("w")
            try:
                # run_tests.sh sizes its /dev/shm free-space check for all shards together.
                process = subprocess.Popen(command, cwd=ROOT, env={**os.environ, "MCS_TEST_PARALLEL": str(count)},
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
    problems, warnings, loaded = _check(results, reports, xmls, count)
    if loaded:
        report = next(iter(loaded.values()))
        if report["unit"] != "seconds":
            print(f"durations: none usable in {durations.name}, balancing by test count", flush=True)
        heaviest = report.get("heaviest")
        if heaviest and report["target"] and heaviest["weight"] > report["target"]:
            print(f"warning: {heaviest['file']} alone weighs {heaviest['weight']} {report['unit']},"
                  f" more than one shard's share ({report['target']}); list it under \"split\""
                  f" in {durations.name} or use fewer shards", flush=True)
    for index in range(1, count + 1):
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
