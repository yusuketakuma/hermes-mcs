"""Fixed CI groups preserve discovery, complete failures, and own their children."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ci/run_pytest_groups.py"
spec = importlib.util.spec_from_file_location("ci_pytest_groups", SOURCE)
groups = importlib.util.module_from_spec(spec)
spec.loader.exec_module(groups)


def _test_files(arguments):
    ignored = [ROOT / arg.removeprefix("--ignore=")
               for arg in arguments if arg.startswith("--ignore=")]
    return {path.relative_to(ROOT) for arg in arguments if not arg.startswith("-")
            for path in (ROOT / arg).rglob("*.py")
            if (path.name.startswith("test_") or path.name.endswith("_test.py"))
            and not any(path == ignore or ignore in path.parents for ignore in ignored)}


@pytest.mark.parametrize("lane,targets", [
    ("linux", ("tests", "integration")),
    ("macos", ("tests/meta", "tests/ops")),
])
def test_fixed_groups_cover_every_test_file_once(lane, targets):
    left, right = [_test_files(args) for args in groups.GROUPS[lane].values()]
    assert left and right and not left & right
    assert left | right == _test_files(targets)
    # Directory discovery keeps root collect_ignore_glob active for Hermes tests.
    assert all((ROOT / arg).is_dir() for args in groups.GROUPS[lane].values()
               for arg in args if not arg.startswith("-"))


STUB = '''import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
report = Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--junitxml=")))
name = report.stem
root = report.parent
(root / (name + ".args.json")).write_text(json.dumps(args))
(root / (name + ".started")).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
deadline = time.monotonic() + 10
while len(list(root.glob("*.started"))) < 2:
    if time.monotonic() > deadline:
        raise SystemExit("the other group never started")
    time.sleep(0.01)
if os.environ.get("SYNTHETIC_GROUP_CANCEL") or os.environ.get("SYNTHETIC_GROUP_ORPHAN"):
    child = "import os,signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)"
    subprocess.Popen([sys.executable, "-c", child, str(root / (name + ".child"))])
    while not (root / (name + ".child")).exists():
        if time.monotonic() > deadline:
            raise SystemExit("the synthetic child never started")
        time.sleep(0.01)
    print("synthetic child started", flush=True)
    if os.environ.get("SYNTHETIC_GROUP_CANCEL"):
        time.sleep(60)
code = 7 if name == os.environ.get("SYNTHETIC_GROUP_FAIL") else 0
time.sleep(0.1 if not code else 0)
report.write_text('<testsuite tests="1" failures="' + str(int(bool(code))) + '"/>')
(root / (name + ".finished")).write_text(str(code))
print("synthetic result " + name, flush=True)
raise SystemExit(code)
'''


def _synthetic_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "ci").mkdir(parents=True)
    (repo / "scripts").mkdir()
    script = repo / "ci/run_pytest_groups.py"
    script.write_text(SOURCE.read_text())
    stub = repo / "scripts/synthetic_group.py"
    stub.write_text(STUB)
    (repo / "scripts/run_tests.sh").write_text(
        "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(stub)) + ' "$@"\n')
    return script, tmp_path / "reports"


@pytest.mark.parametrize("failed,expected", [(None, 0), ("lifecycle", 1), ("rest", 1)])
def test_both_groups_finish_and_every_exit_is_checked(tmp_path, failed, expected):
    script, output = _synthetic_repo(tmp_path)
    env = dict(os.environ, SYNTHETIC_GROUP_FAIL=failed or "")
    result = subprocess.run([sys.executable, str(script), "linux", "--output-dir", str(output)],
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    starts = []
    for name, targets in groups.GROUPS["linux"].items():
        starts.append(json.loads((output / (name + ".started")).read_text()))
        args = json.loads((output / (name + ".args.json")).read_text())
        assert args == [*targets, "-r", "fEs", "--durations=30", f"--junitxml={output / (name + '.xml')}"]
        code = 7 if name == failed else 0
        assert (output / (name + ".finished")).read_text() == str(code)
        assert f"{name}: exit={code}" in result.stdout
        assert "synthetic result " + name in (output / (name + ".log")).read_text()
        assert (output / (name + ".xml")).is_file()
    assert starts[0]["pgid"] != starts[1]["pgid"]
    assert all(start["pid"] == start["pgid"] for start in starts)


def test_default_reports_use_runner_temp_outside_the_checkout(tmp_path):
    script, _ = _synthetic_repo(tmp_path)
    result = subprocess.run([sys.executable, str(script), "macos"],
                            env=dict(os.environ, RUNNER_TEMP=str(tmp_path)),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    output, = tmp_path.glob("mcs-pytest-macos-*")
    assert {path.name for path in output.glob("*.xml")} == {"meta.xml", "ops.xml"}


def test_existing_reports_and_checkout_paths_are_never_overwritten(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    output.mkdir()
    marker = output / "prior.txt"
    marker.write_text("keep")
    for target in (output, script.parent / "reports"):
        result = subprocess.run([sys.executable, str(script), "linux", "--output-dir", str(target)],
                                text=True, capture_output=True, timeout=10)
        assert result.returncode == 2
    assert marker.read_text() == "keep"
    result = subprocess.run([sys.executable, str(script), "linux"],
                            env=dict(os.environ, RUNNER_TEMP=str(script.parent)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert not list(script.parent.glob("mcs-pytest-*"))


def _running(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            text=True, capture_output=True, timeout=10)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.lstrip().startswith("Z")


def test_exited_worker_cannot_hang_the_run_with_an_orphaned_output_pipe(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    result = subprocess.run([sys.executable, str(script), "linux", "--output-dir", str(output)],
                            env=dict(os.environ, SYNTHETIC_GROUP_ORPHAN="1", SYNTHETIC_GROUP_FAIL="lifecycle"),
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 1, result.stdout + result.stderr
    pids = [int(path.read_text()) for path in output.glob("*.child")]
    assert len(pids) == 2
    deadline = time.monotonic() + 5
    while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(_running(pid) for pid in pids)


@pytest.mark.parametrize("signum,expected", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_interruption_reaps_both_process_groups_and_their_children(tmp_path, signum, expected):
    script, output = _synthetic_repo(tmp_path)
    process = subprocess.Popen([sys.executable, str(script), "linux", "--output-dir", str(output)],
                               env=dict(os.environ, SYNTHETIC_GROUP_CANCEL="1"),
                               text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 15
        while len(list(output.glob("*.child"))) < 2:
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.02)
        pids = [int(path.read_text()) for path in output.glob("*.child")]
        process.send_signal(signum)
        stdout, _ = process.communicate(timeout=15)
        assert process.returncode == expected, stdout
        deadline = time.monotonic() + 5
        while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not any(_running(pid) for pid in pids)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for marker in output.glob("*.started"):
            try:
                os.killpg(json.loads(marker.read_text())["pgid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
