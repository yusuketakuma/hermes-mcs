"""CI shards select every test once, complete failures, and own their children."""
import importlib.util
import json
import os
from pathlib import Path
import random
import shlex
import shutil
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "ci/run_pytest_groups.py"
# A pytest-linux CI job sets these for the suite itself; the nested runners
# below must start from the local defaults.
ENV = {key: value for key, value in os.environ.items()
       if key not in ("MCS_CI_SKIP_PERF", "MCS_CI_TEST_SCOPE", "MCS_TEST_SHARDS")}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


groups = _load("ci_pytest_groups", SOURCE)
shards = _load("ci_pytest_shards", ROOT / "ci/pytest_shards.py")
durations_tool = _load("ci_update_pytest_durations", ROOT / "ci/update_pytest_durations.py")


def _test_files(targets):
    return sorted(str(path.relative_to(ROOT)) for target in targets
                  for path in (ROOT / target).rglob("*.py")
                  if path.name.startswith("test_") or path.name.endswith("_test.py"))


# ---------- the partition ----------

@pytest.mark.parametrize("lane", sorted(groups.LANES))
def test_every_lane_file_is_selected_by_exactly_one_shard(lane):
    targets = groups.LANES[lane]
    # Directory discovery keeps root collect_ignore_glob active for Hermes tests.
    assert all((ROOT / target).is_dir() for target in targets)
    files = _test_files(targets)
    nodeids = [f"{name}::test_{case}" for name in files for case in ("a", "b")]
    table, split = shards.load_durations(ROOT / f"ci/pytest_durations.{lane}.json")
    for count in (1, 2, 3, 8, len(files) + 3):
        assignment, info = shards.plan(nodeids, count, table, split)
        assert set(assignment) == set(nodeids)
        assert set(assignment.values()) <= set(range(count))
        owners = {}
        for nodeid, shard in assignment.items():
            owners.setdefault(shards.file_of(nodeid), set()).add(shard)
        assert all(len(found) == 1 for name, found in owners.items() if name not in split)
        assert info["files_total"] == len(files)
        assert len(set(assignment.values())) == min(count, len(files))


def test_lanes_run_the_ci_targets():
    assert groups.LANES == {"linux": ("tests", "integration"), "macos": ("tests/meta", "tests/ops")}


@pytest.mark.parametrize("lane", ["linux", "macos"])
def test_durations_tables_are_readable_and_name_repository_files(lane):
    path = ROOT / f"ci/pytest_durations.{lane}.json"
    data = json.loads(path.read_text())
    assert set(data) == {"files", "split"}
    table, split = shards.load_durations(path)
    assert len(table) == len(data["files"]) and split == set(data["split"])
    assert all(not name.startswith(("/", "..")) for name in table)
    assert all((ROOT / name).is_file() for name in split)


def test_plan_is_deterministic_and_balances_recorded_seconds():
    table = {"a.py": 4.0, "b.py": 3.0, "c.py": 3.0, "d.py": 2.0}
    nodeids = [f"{name}::t{i}" for name in table for i in range(3)]
    first, info = shards.plan(nodeids, 2, table)
    shuffled = nodeids[:]
    random.Random(7).shuffle(shuffled)
    assert shards.plan(shuffled, 2, table)[0] == first
    assert info["unit"] == "seconds" and info["planned"] == [6.0, 6.0]
    assert {assignment for name, assignment in first.items() if name.startswith(("a.py", "d.py"))} == {0}
    assert info["heaviest"] == {"file": "a.py", "weight": 4.0, "chunks": 1}


def test_unknown_files_weigh_the_median_and_an_empty_table_weighs_test_counts():
    nodeids = ["known.py::t", "slow.py::t", "new.py::t", "new.py::u"]
    _, info = shards.plan(nodeids, 1, {"known.py": 1.0, "slow.py": 9.0, "gone.py": 2.0})
    assert info["planned"] == [1.0 + 9.0 + 2.0]
    _, info = shards.plan(nodeids, 1, {})
    assert info["unit"] == "tests" and info["planned"] == [4.0]


def test_split_file_is_cut_into_contiguous_sorted_chunks():
    nodeids = [f"big.py::t{i:02d}" for i in range(12)] + ["small.py::t"]
    assignment, info = shards.plan(nodeids, 3, {"big.py": 30.0, "small.py": 1.0}, {"big.py"})
    assert info["heaviest"]["chunks"] == 3
    owners = [assignment[f"big.py::t{i:02d}"] for i in range(12)]
    assert len(set(owners)) == 3
    assert owners == sorted(owners, key=owners.index)   # each chunk is one run of sorted ids
    assert sorted(assignment) == sorted(nodeids)


def test_zero_second_files_still_spread_over_every_shard():
    # Fast files are recorded as 0.0 s, and a 0.0 median weighs unknown files 0.0 too.
    nodeids = [f"f{i}.py::t{k}" for i in range(10) for k in range(3)]
    for table in ({f"f{i}.py": 0.0 for i in range(10)}, {"f0.py": 0.0, "f1.py": 0.0}):
        assignment, info = shards.plan(nodeids, 4, table)
        assert info["unit"] == "seconds"
        assert len(set(assignment.values())) == 4 and all(load > 0 for load in info["planned"])
        assert sorted(assignment) == sorted(nodeids)


def test_non_finite_or_huge_durations_are_dropped_and_split_still_plans(tmp_path):
    path = tmp_path / "durations.json"
    path.write_text('{"files": {"inf.py": 1e999, "nan.py": NaN, "huge.py": 1' + "0" * 400
                    + ', "ok.py": 2.5}, "split": ["inf.py"]}')
    table, split = shards.load_durations(path)
    assert table == {"ok.py": 2.5} and split == {"inf.py"}
    nodeids = [f"{name}::t{k}" for name in ("inf.py", "nan.py", "huge.py", "ok.py") for k in range(4)]
    assignment, _ = shards.plan(nodeids, 3, table, split)
    assert sorted(assignment) == sorted(nodeids)


@pytest.mark.parametrize("spec", ["", "1", "0/2", "3/2", "a/b", "1/0", "-1/2", "1/2/3"])
def test_invalid_shard_specs_are_rejected(spec):
    with pytest.raises(ValueError):
        shards.parse_spec(spec)


@pytest.mark.parametrize("text", [None, "{", "[]", '{"files": [], "split": {}}',
                                  '{"files": {"a.py": "slow", "b.py": -1, "c.py": true}}',
                                  '{"files": {"a.py": 1e999, "b.py": NaN, "c.py": -Infinity}}'])
def test_unreadable_durations_count_as_an_empty_table(tmp_path, text):
    path = tmp_path / "durations.json"
    if text is not None:
        path.write_text(text)
    assert shards.load_durations(path) == ({}, set())


def test_report_sums_add_up_to_the_whole_collection():
    nodeids = [f"f{i % 4}.py::t{i}" for i in range(20)]
    assignment, info = shards.plan(nodeids, 3)
    reports = [shards.report(nodeids, i, 3, assignment, info, 0) for i in (1, 2, 3)]
    assert sum(r["selected"] for r in reports) == 20
    assert len({r["total_digest"] for r in reports}) == 1
    assert sum(int(r["selected_sum"], 16) for r in reports) % (1 << 256) == int(reports[0]["total_sum"], 16)


# ---------- the root conftest option ----------

def _nested(tmp_path, *args):
    shutil.copy(ROOT / "conftest.py", tmp_path / "conftest.py")
    (tmp_path / "ci").mkdir(exist_ok=True)
    shutil.copy(ROOT / "ci/pytest_shards.py", tmp_path / "ci/pytest_shards.py")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=",
                           "--rootdir", str(tmp_path), *args],
                          cwd=tmp_path, capture_output=True, text=True, timeout=120)


def _synthetic_tests(tmp_path, files):
    (tmp_path / "tests").mkdir()
    for index in range(files):
        (tmp_path / f"tests/test_f{index}.py").write_text(
            "import pytest\n\n@pytest.mark.parametrize('n', range(3))\ndef test_case(n):\n    pass\n")
    (tmp_path / "tests/test_skipped_module.py").write_text(
        "import pytest\npytest.skip('synthetic module skip', allow_module_level=True)\n")


@pytest.mark.parametrize("count", [2, 6])
def test_conftest_shards_select_each_collected_test_once(tmp_path, count):
    _synthetic_tests(tmp_path, 4)
    seen, reports = [], []
    for index in range(1, count + 1):
        report, xml = tmp_path / f"r{index}.json", tmp_path / f"s{index}.xml"
        result = _nested(tmp_path, "tests", f"--mcs-shard={index}/{count}", f"--mcs-shard-report={report}",
                         "-o", "junit_family=xunit1", f"--junitxml={xml}")
        data = json.loads(report.read_text())
        reports.append(data)
        cases = {(c.get("file"), c.get("name")) for c in ET.parse(xml).getroot().iter("testcase")}
        assert data["finished"] and data["ran"] == data["selected"]
        assert len(cases) == data["selected"] + data["collect_not_passed"] == data["selected"] + 1
        assert result.returncode == (5 if data["selected"] == 0 else 0), result.stdout + result.stderr
        seen += [case for case in cases if case[0] != "tests/test_skipped_module.py"]
    assert sorted(seen) == sorted({(f"tests/test_f{i}.py", f"test_case[{n}]") for i in range(4) for n in range(3)})
    assert {r["total"] for r in reports} == {12} and sum(r["selected"] for r in reports) == 12
    assert (count > 4) is any(r["selected"] == 0 for r in reports)


def test_conftest_without_the_option_collects_everything(tmp_path):
    _synthetic_tests(tmp_path, 2)
    result = _nested(tmp_path, "tests")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "6 passed, 1 skipped" in result.stdout


@pytest.mark.parametrize("args,message", [
    (["--mcs-shard=3/2"], "1 <= I <= N"),
    (["--mcs-shard=x"], "I/N"),
    (["--mcs-shard-report=r.json"], "need --mcs-shard"),
])
def test_conftest_rejects_invalid_shard_options(tmp_path, args, message):
    _synthetic_tests(tmp_path, 1)
    result = _nested(tmp_path, "tests", *args)
    assert result.returncode == 4
    assert message in result.stderr


# ---------- the runner ----------

STUB = '''import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

args = sys.argv[1:]
value = lambda prefix: next(arg.split("=", 1)[1] for arg in args if arg.startswith(prefix))
xml = Path(value("--junitxml="))
report = Path(value("--mcs-shard-report="))
index, count = map(int, value("--mcs-shard=").split("/"))
name = xml.stem
root = xml.parent
(root / (name + ".args.json")).write_text(json.dumps(args))
(root / (name + ".started")).write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(),
                                                    "parallel": os.environ.get("MCS_TEST_PARALLEL")}))
deadline = time.monotonic() + 10
# every shard this runner starts together (a slice runs only its own shards)
while len(list(root.glob("*.started"))) < int(os.environ.get("MCS_TEST_PARALLEL") or count):
    if time.monotonic() > deadline:
        raise SystemExit("the other shards never started")
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
empty = os.environ.get("SYNTHETIC_EMPTY", "")
ids = [f"t{k}" for k in range(1, count + 1) if f"shard-{k}" != empty]
mine = [] if name == empty else [f"t{index}"]
if name == os.environ.get("SYNTHETIC_DUPLICATE"):
    mine = ["t1"]
id_sum = lambda items: sum(int.from_bytes(hashlib.sha256(i.encode()).digest(), "big") for i in items) % (1 << 256)
data = {"index": index, "count": count, "total": len(ids), "total_digest": "d", "total_sum": format(id_sum(ids), "064x"),
        "selected": len(mine), "selected_sum": format(id_sum(mine), "064x"),
        "files_total": int(os.environ.get("SYNTHETIC_FILES") or count), "files_selected": len(mine),
        "collect_not_passed": 0, "unit": "tests", "planned": float(len(mine)), "target": 1.0,
        "heaviest": {"file": "f.py", "weight": 1.0, "chunks": 1}, "finished": False}
report.write_text(json.dumps(data))
if name == os.environ.get("SYNTHETIC_DEAD"):
    print("synthetic last words", flush=True)
    os._exit(1)
code = 7 if name == os.environ.get("SYNTHETIC_GROUP_FAIL") else 0
if not mine:
    code = 5
time.sleep(0.1 if not code else 0)
cases = mine + (["extra"] if name == os.environ.get("SYNTHETIC_EXTRA_CASE") else [])
if name != os.environ.get("SYNTHETIC_NO_XML"):
    xml.write_text("<testsuite>" + "".join(f'<testcase file="f.py" classname="f" name="{i}"/>' for i in cases)
                   + "</testsuite>")
ran = len(mine) - (1 if name == os.environ.get("SYNTHETIC_RAN_SHORT") else 0)
data.update(finished=True, ran=ran, exitstatus=code)
report.write_text(json.dumps(data))
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


def _runner(script, output, *args, timeout=30, **env):
    command = [sys.executable, str(script), "linux", *args]
    if output is not None:
        command += ["--output-dir", str(output)]
    return subprocess.run(command, env={**ENV, **env},
                          text=True, capture_output=True, timeout=timeout)


@pytest.mark.parametrize("failed,expected", [(None, 0), ("shard-1", 1), ("shard-3", 1)])
def test_every_shard_finishes_and_every_exit_is_checked(tmp_path, failed, expected):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--shards", "3", SYNTHETIC_GROUP_FAIL=failed or "")
    assert result.returncode == expected, result.stdout + result.stderr
    assert "pytest shards: 3 (usable CPUs: " in result.stdout
    starts = []
    for index in (1, 2, 3):
        name = f"shard-{index}"
        starts.append(json.loads((output / (name + ".started")).read_text()))
        args = json.loads((output / (name + ".args.json")).read_text())
        assert args == ["tests", "integration", f"--mcs-shard={index}/3",
                        f"--mcs-shard-durations={script.parents[1].resolve() / 'ci/pytest_durations.linux.json'}",
                        f"--mcs-shard-report={output.resolve() / (name + '.report.json')}",
                        "-p", "no:cacheprovider", "-o", "junit_family=xunit1",
                        "-r", "fEs", "--durations=30", f"--junitxml={output.resolve() / (name + '.xml')}"]
        code = 7 if name == failed else 0
        assert (output / (name + ".finished")).read_text() == str(code)
        assert f"{name}: exit={code}; tests=1; planned=1.0 tests; actual=" in result.stdout
        assert "synthetic result " + name in (output / (name + ".log")).read_text()
        assert (output / (name + ".xml")).is_file()
    assert len({start["pgid"] for start in starts}) == 3
    assert all(start["pid"] == start["pgid"] for start in starts)
    assert {start["parallel"] for start in starts} == {"3"}   # sizes run_tests.sh's /dev/shm check
    assert "durations: none usable in pytest_durations.linux.json, balancing by test count" in result.stdout
    assert "incomplete run" not in result.stderr


@pytest.mark.parametrize("args,env,expected", [
    (["--shards", "4"], {}, 4),
    ([], {"MCS_TEST_SHARDS": "3"}, 3),
    (["--shards", "1"], {"MCS_TEST_SHARDS": "5"}, 1),
])
def test_shard_count_comes_from_the_option_then_the_environment(tmp_path, args, env, expected):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, *args, **env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert {path.name for path in output.glob("*.xml")} == {f"shard-{i}.xml" for i in range(1, expected + 1)}


@pytest.mark.parametrize("value", ["0", "65", "two"])
def test_invalid_shard_counts_are_usage_errors(tmp_path, value):
    script, output = _synthetic_repo(tmp_path)
    assert _runner(script, output, "--shards", value).returncode == 2
    assert _runner(script, output, MCS_TEST_SHARDS=value).returncode == 2
    assert not output.exists()


def test_default_shard_count_uses_usable_cpus_between_two_and_eight(monkeypatch):
    for cpus, expected in ((1, 2), (3, 3), (64, 8)):
        monkeypatch.setattr(groups, "_cpu_count", lambda cpus=cpus: cpus)
        monkeypatch.delenv("MCS_TEST_SHARDS", raising=False)
        assert groups._shard_count(None, None) == expected


@pytest.mark.parametrize("files,expected", [(2, 0), (3, 1)])
def test_empty_shard_passes_only_when_there_are_more_shards_than_files(tmp_path, files, expected):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--shards", "3", SYNTHETIC_EMPTY="shard-3", SYNTHETIC_FILES=str(files))
    assert result.returncode == expected, result.stdout + result.stderr
    assert f"shard-3: exit={0 if expected == 0 else 5}; tests=0" in result.stdout


@pytest.mark.parametrize("fault,message", [
    ("SYNTHETIC_NO_XML", "shard-2: no readable junit XML"),
    ("SYNTHETIC_DEAD", "shard-2: the shard report was never completed"),
    ("SYNTHETIC_DUPLICATE", "did not select every collected test exactly once"),
    ("SYNTHETIC_RAN_SHORT", "shard-2: ran 0 of 1 selected tests"),
])
def test_a_missing_or_inconsistent_shard_fails_the_run(tmp_path, fault, message):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--shards", "3", **{fault: "shard-2"})
    assert result.returncode == 1, result.stdout + result.stderr
    assert message in result.stderr
    if fault == "SYNTHETIC_DEAD":
        assert "shard-2: last " in result.stdout and "  synthetic last words" in result.stdout


def test_junit_count_mismatch_is_a_warning_when_every_selected_test_ran(tmp_path):
    # The finished report proves the coverage; the junit count follows pytest's writer.
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--shards", "3", SYNTHETIC_EXTRA_CASE="shard-2")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "warning: shard-2: junit has 2 testcases, expected 1 selected" in result.stderr
    assert "incomplete run" not in result.stderr


def test_default_reports_use_runner_temp_outside_the_checkout(tmp_path):
    script, _ = _synthetic_repo(tmp_path)
    result = subprocess.run([sys.executable, str(script), "macos", "--shards", "2"],
                            env=dict(ENV, RUNNER_TEMP=str(tmp_path)),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    output, = tmp_path.glob("mcs-pytest-macos-*")
    assert {path.name for path in output.glob("*.xml")} == {"shard-1.xml", "shard-2.xml"}
    assert json.loads((output / "shard-1.args.json").read_text())[:2] == ["tests/meta", "tests/ops"]


def test_existing_reports_and_checkout_paths_are_never_overwritten(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    output.mkdir()
    marker = output / "prior.txt"
    marker.write_text("keep")
    for target in (output, script.parent / "reports"):
        result = subprocess.run([sys.executable, str(script), "linux", "--output-dir", str(target)],
                                env=ENV, text=True, capture_output=True, timeout=10)
        assert result.returncode == 2
    assert marker.read_text() == "keep"
    result = subprocess.run([sys.executable, str(script), "linux"],
                            env=dict(ENV, RUNNER_TEMP=str(script.parent)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 2
    assert not list(script.parent.glob("mcs-pytest-*"))


def _running(pid):
    result = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                            text=True, capture_output=True, timeout=10)
    return result.returncode == 0 and result.stdout.strip() and not result.stdout.lstrip().startswith("Z")


def test_exited_worker_cannot_hang_the_run_with_an_orphaned_output_pipe(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--shards", "2", timeout=20,
                     SYNTHETIC_GROUP_ORPHAN="1", SYNTHETIC_GROUP_FAIL="shard-1")
    assert result.returncode == 1, result.stdout + result.stderr
    pids = [int(path.read_text()) for path in output.glob("*.child")]
    assert len(pids) == 2
    deadline = time.monotonic() + 5
    while any(_running(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not any(_running(pid) for pid in pids)


@pytest.mark.parametrize("signum,expected", [(signal.SIGTERM, 143), (signal.SIGINT, 130)])
def test_interruption_reaps_every_process_group_and_their_children(tmp_path, signum, expected):
    script, output = _synthetic_repo(tmp_path)
    process = subprocess.Popen([sys.executable, str(script), "linux", "--shards", "2",
                                "--output-dir", str(output)],
                               env=dict(ENV, SYNTHETIC_GROUP_CANCEL="1"),
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


# ---------- matrix slices ----------

def test_slice_runs_only_its_shards_of_the_fixed_total(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    result = _runner(script, output, "--slice", "2/3", "--per-slice", "2", MCS_TEST_SHARDS="5")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "pytest shards: 3-4 of 6 (slice 2/3; usable CPUs: " in result.stdout
    assert "MCS_TEST_SHARDS is ignored" in result.stdout
    assert {path.name for path in output.glob("*.xml")} == {"shard-3.xml", "shard-4.xml"}
    for index in (3, 4):
        args = json.loads((output / f"shard-{index}.args.json").read_text())
        assert f"--mcs-shard={index}/6" in args and "-m" not in args
        # only this slice's processes share the host's /dev/shm
        assert json.loads((output / f"shard-{index}.started").read_text())["parallel"] == "2"
    assert "incomplete run" not in result.stderr


def test_every_slice_together_is_verified_as_one_exact_run(tmp_path):
    script, _ = _synthetic_repo(tmp_path)
    reports = tmp_path / "downloaded"
    for number in (1, 2, 3):
        output = tmp_path / f"slice-{number}"
        assert _runner(script, output, "--slice", f"{number}/3", "--per-slice", "2").returncode == 0
        # the gate downloads every slice's shard reports into one directory tree
        (reports / f"mcs-pytest-linux-{number}").mkdir(parents=True)
        for path in output.glob("shard-*.report.json"):
            shutil.copy(path, reports / f"mcs-pytest-linux-{number}" / path.name)
    verify = [sys.executable, str(script), "linux", "--verify-reports", str(reports)]
    result = subprocess.run([*verify, "--slices", "3", "--per-slice", "2"], env=ENV,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 slices x 2 shards = 6 shard reports; 6 collected tests" in result.stdout
    # reports planned for another total, or a missing slice, are not this run
    # (the plan depends only on the total, so 2 x 3 would describe the same run)
    for slices, per_slice in (("2", "2"), ("4", "2")):
        result = subprocess.run([*verify, "--slices", slices, "--per-slice", per_slice], env=ENV,
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 1 and "incomplete run" in result.stderr
    shutil.rmtree(reports / "mcs-pytest-linux-3")
    result = subprocess.run([*verify, "--slices", "3", "--per-slice", "2"], env=ENV,
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 1
    assert "shard-5: 0 reports (slice 3 must upload exactly one)" in result.stderr


def _reports(nodeids, total, assignment=None, info=None, **fields):
    if assignment is None:
        assignment, info = shards.plan(nodeids, total)
    out = {}
    for index in range(1, total + 1):
        report = shards.report(nodeids, index, total, assignment, info, 0)
        report.update(finished=True, ran=report["selected"], exitstatus=0 if report["selected"] else 5)
        out[index] = report
    for index, change in fields.items():
        out[int(index[1:])].update(change)
    return out


def _write(directory, reports, per_slice):
    for index, report in reports.items():
        folder = directory / f"slice-{(index - 1) // per_slice + 1}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"shard-{index}.report.json").write_text(json.dumps(report))


@pytest.mark.parametrize("slices,per_slice", [(4, 2), (2, 3), (1, 2)])
def test_slices_partition_the_shards_and_their_reports_prove_the_union(tmp_path, slices, per_slice):
    total = slices * per_slice
    indexes = [i for number in range(1, slices + 1) for i in groups.slice_indexes(number, per_slice)]
    assert indexes == list(range(1, total + 1))   # disjoint and exact
    nodeids = [f"tests/f{i % 11}.py::t{i}" for i in range(60)]
    _write(tmp_path / "ok", _reports(nodeids, total), per_slice)
    problems, loaded = groups.verify_reports(tmp_path / "ok", slices, per_slice)
    assert problems == [] and sorted(loaded) == indexes


@pytest.mark.parametrize("fault,message", [
    ("missing", "shard-2: 0 reports"),
    ("duplicate", "shard-1: 2 reports"),
    ("unfinished", "shard-2: the shard report was never completed"),
    ("short", "shard-2: ran 0 of"),
    ("failed", "shard-2: pytest exit status 1"),
    ("other plan", "shards collected different test sets"),
    ("double selection", "did not select every collected test exactly once"),
    ("stray", "shard-9: a report for a shard outside 1..4"),
])
def test_verify_reports_fails_closed(tmp_path, fault, message):
    nodeids = [f"tests/f{i % 5}.py::t{i}" for i in range(40)]
    reports = _reports(nodeids, 4)
    if fault == "unfinished":
        reports[2]["finished"] = False
    elif fault == "short":
        reports[2]["ran"] = 0
    elif fault == "failed":
        reports[2]["exitstatus"] = 1
    elif fault == "other plan":
        reports[3] = _reports(nodeids[:-1], 4)[3]
    elif fault == "double selection":
        reports[2]["selected_sum"] = reports[1]["selected_sum"]   # counts add up, the ids do not
    _write(tmp_path, reports, 2)
    if fault == "missing":
        (tmp_path / "slice-1/shard-2.report.json").unlink()
    elif fault == "duplicate":
        shutil.copy(tmp_path / "slice-1/shard-1.report.json", tmp_path / "slice-2/shard-1.report.json")
    elif fault == "stray":
        (tmp_path / "slice-2/shard-9.report.json").write_text(json.dumps(reports[1]))
    problems, _ = groups.verify_reports(tmp_path, 2, 2)
    assert any(message in problem for problem in problems), problems


@pytest.mark.parametrize("args", [
    ["--slice", "1/2", "--per-slice", "2", "--shards", "4"],
    ["--slice", "3/2", "--per-slice", "2"],
    ["--slice", "0/2", "--per-slice", "2"],
    ["--slice", "1/2"],
    ["--slice", "x", "--per-slice", "2"],
    ["--per-slice", "2"],
    ["--slice", "1/8", "--per-slice", "9"],
    ["--slices", "2", "--per-slice", "2"],
    ["--verify-reports", "reports", "--slices", "2"],
    ["--verify-reports", "reports", "--slices", "2", "--per-slice", "2", "--slice", "1/2"],
])
def test_invalid_slices_are_usage_errors(tmp_path, args):
    script, output = _synthetic_repo(tmp_path)
    assert _runner(script, output, *args).returncode == 2
    assert not output.exists()


def test_perf_tests_are_deselected_only_when_ci_asks(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    assert _runner(script, output, "--shards", "2", MCS_CI_SKIP_PERF="1").returncode == 0
    args = json.loads((output / "shard-1.args.json").read_text())
    assert args[args.index("-m") + 1] == "not perf"
    assert _runner(script, tmp_path / "bad", MCS_CI_SKIP_PERF="yes").returncode == 2
    # unset or "0" keep the perf tests in the shard selection (the serial
    # slice-1 step selects them itself)
    for tag, value in (("unset", None), ("zero", "0")):
        out = tmp_path / tag
        env = {} if value is None else {"MCS_CI_SKIP_PERF": value}
        assert _runner(script, out, "--shards", "1", **env).returncode == 0
        args = json.loads((out / "shard-1.args.json").read_text())
        assert "-m" not in args


def test_docs_scope_passes_the_document_test_list_to_every_shard(tmp_path):
    script, output = _synthetic_repo(tmp_path)
    repo = script.parents[1]
    shutil.copy(ROOT / "ci/ci_scope.py", repo / "ci/ci_scope.py")
    for name, text in (("tests/meta/test_runner.py", ""), ("tests/core/test_readme.py", "README.md"),
                       ("tests/core/test_db.py", "x = 1\n")):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    (repo / "integration").mkdir()
    result = _runner(script, output, "--shards", "2", MCS_CI_TEST_SCOPE="docs")
    assert result.returncode == 0, result.stdout + result.stderr
    only = output / "only-files.txt"
    assert only.read_text() == "tests/core/test_readme.py\ntests/meta/test_runner.py\n"
    assert "test scope: docs, 2 test files" in result.stdout
    for index in (1, 2):
        assert f"--mcs-only-files={only.resolve()}" in json.loads((output / f"shard-{index}.args.json").read_text())


@pytest.mark.parametrize("count", [1, 2])
def test_conftest_only_files_deselects_before_sharding(tmp_path, count):
    _synthetic_tests(tmp_path, 4)
    listed = tmp_path / "only.txt"
    listed.write_text("tests/test_f1.py\ntests/test_f3.py\n")
    reports = []
    for index in range(1, count + 1):
        report = tmp_path / f"r{index}.json"
        result = _nested(tmp_path, "tests", f"--mcs-only-files={listed}", f"--mcs-shard={index}/{count}",
                         f"--mcs-shard-report={report}")
        assert result.returncode in (0, 5), result.stdout + result.stderr
        reports.append(json.loads(report.read_text()))
    assert {r["total"] for r in reports} == {6} and sum(r["selected"] for r in reports) == 6
    assert {r["files_total"] for r in reports} == {2}
    result = _nested(tmp_path, "tests", f"--mcs-only-files={listed}")
    assert "6 passed, 1 skipped, 6 deselected" in result.stdout
    result = _nested(tmp_path, "tests", f"--mcs-only-files={tmp_path / 'absent.txt'}")
    assert result.returncode == 4 and "--mcs-only-files" in result.stderr


# ---------- the durations table ----------

def _junit(path, cases):
    body = "".join(
        f'<testcase file="{file}" classname="{file[:-3].replace("/", ".")}" name="{name}" time="{seconds}">'
        f"{'<' + outcome + '/>' if outcome else ''}</testcase>"
        for file, name, seconds, outcome in cases)
    path.write_text(f'<testsuites><testsuite tests="{len(cases)}">{body}</testsuite></testsuites>')
    return path


def test_durations_are_summed_per_file_and_keep_the_split_list(tmp_path):
    first = _junit(tmp_path / "shard-1.xml", [
        ("tests/a/test_slow.py", "test_x", "2.25", None),
        ("tests/a/test_slow.py", "test_y", "1.0", None),
        ("tests/test_mac_only.py", "test_launchd", "0.001", "skipped"),
        ("tests/test_skips_slowly.py", "test_z", "3.0", "skipped")])
    second = _junit(tmp_path / "shard-2.xml", [("tests/b/test_fast.py", "test_q", "0.04", None)])
    output = tmp_path / "durations.json"
    output.write_text(json.dumps({"files": {"old.py": 1}, "split": ["tests/a/test_slow.py"]}))
    assert durations_tool.main(["--output", str(output), str(first), str(second)]) == 0
    assert json.loads(output.read_text()) == {
        "files": {"tests/a/test_slow.py": 3.2, "tests/b/test_fast.py": 0.0, "tests/test_skips_slowly.py": 3.0},
        "split": ["tests/a/test_slow.py"]}
    assert shards.load_durations(output) == (
        {"tests/a/test_slow.py": 3.2, "tests/b/test_fast.py": 0.0, "tests/test_skips_slowly.py": 3.0},
        {"tests/a/test_slow.py"})


def test_durations_refuse_a_failing_run_unless_asked(tmp_path, capsys):
    xml = _junit(tmp_path / "shard-1.xml", [("tests/test_a.py", "test_x", "0.1", "failure"),
                                            ("tests/test_a.py", "test_x", "0.2", "error")])
    output = tmp_path / "durations.json"
    assert durations_tool.main(["--output", str(output), str(xml)]) == 2
    assert not output.exists() and "1 testcases failed" in capsys.readouterr().err
    assert durations_tool.main(["--allow-failures", "--output", str(output), str(xml)]) == 0
    assert json.loads(output.read_text())["files"] == {"tests/test_a.py": 0.2}


def test_durations_resolve_a_file_from_the_classname_without_a_file_attribute(tmp_path):
    (tmp_path / "tests/pkg").mkdir(parents=True)
    (tmp_path / "tests/pkg/test_mod.py").write_text("")
    xml = tmp_path / "shard-1.xml"
    xml.write_text('<testsuite><testcase classname="tests.pkg.test_mod.TestGroup" name="test_a" time="1.5"/>'
                   '<testcase classname="elsewhere.test_gone" name="test_b" time="9"/></testsuite>')
    files = durations_tool.collect([xml], root=tmp_path)
    assert files == {"tests/pkg/test_mod.py": {"seconds": 1.5, "cases": 1, "skipped": 0, "failed": 0}}
