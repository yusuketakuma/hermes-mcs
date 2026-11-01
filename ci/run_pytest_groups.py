#!/usr/bin/env python3
"""Run the CI lane's two complete test groups with isolated test runners."""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
GROUPS = {
    "linux": {
        "lifecycle": ("tests/meta", "tests/ops"),
        "rest": ("tests", "integration", "--ignore=tests/meta", "--ignore=tests/ops"),
    },
    "macos": {"meta": ("tests/meta",), "ops": ("tests/ops",)},
}


def _relay(name, stream, log):
    with stream, log:
        for line in stream:
            log.write(line)
            log.flush()
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=GROUPS)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
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
    processes, threads, results = {}, [], {}
    previous_term = signal.signal(signal.SIGTERM, _terminate)
    try:
        for name, targets in GROUPS[args.lane].items():
            command = ["sh", str(ROOT / "scripts/run_tests.sh"), *targets,
                       "-r", "fEs", "--durations=30", f"--junitxml={output / (name + '.xml')}"]
            log = (output / f"{name}.log").open("w")
            try:
                process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, errors="replace",
                                           start_new_session=True)
            except OSError as error:
                with log:
                    log.write(f"could not start {name}: {error}\n")
                print(f"[{name}] could not start: {error}", file=sys.stderr, flush=True)
                results[name] = 1
                continue
            processes[name] = process
            thread = threading.Thread(target=_relay, args=(name, process.stdout, log), daemon=True)
            thread.start()
            threads.append(thread)
        # A failed group must not cancel the other group's remaining tests.
        for name, process in processes.items():
            results[name] = process.wait()
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
    for name in GROUPS[args.lane]:
        code = results[name]
        print(f"{name}: exit={code}; junit={output / (name + '.xml')}", flush=True)
    return int(any(code != 0 for code in results.values()))


if __name__ == "__main__":
    raise SystemExit(main())
