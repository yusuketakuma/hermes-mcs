"""Synthetic writer connections preserve outputs, process identity and EOF ownership."""
import json
import logging
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from adapters.common import eventlog
from adapters.lineworks import service as lineworks_service
import cloud_backlog
import mcs_util
import run_log
import runtime_inspection
import service_contract
import update_launch
from mcs_standalone import runtime, service

REPO = Path(__file__).resolve().parents[2]



@pytest.mark.parametrize("captured", [False, True])
def test_mcs_events_keep_existing_logger_and_single_canonical_route(tmp_path, monkeypatch, caplog, captured):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    if captured:
        monkeypatch.setenv("MCS_RUN_LOG_CAPTURED", "1")
    else:
        monkeypatch.delenv("MCS_RUN_LOG_CAPTURED", raising=False)
    caplog.set_level(logging.INFO, logger="mcs.synthetic")
    eventlog.make_logger("mcs.synthetic")("synthetic_event", error="private text", text="not allowed", code=3)
    expected = {"event": "synthetic_event", "error": "redacted", "code": 3}
    assert [json.loads(record.getMessage()) for record in caplog.records] == [expected]
    if captured:
        assert not (data / "mcs.log").exists()
    else:
        assert [json.loads(line) for line in (data / "mcs.log").read_text().splitlines()] == [expected]


def test_event_append_failure_is_visible_without_source_content(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MCS_RUN_LOG_CAPTURED", raising=False)
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    monkeypatch.setattr(run_log, "append", lambda *a: (_ for _ in ()).throw(run_log.RunLogError("synthetic")))
    eventlog.make_logger("mcs.synthetic")("safe", body="PRIVATE_CANARY")
    assert capsys.readouterr().err == "mcs_log_write_failed\n"


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_native_service_candidates_capture_only_the_original_command(tmp_path, monkeypatch, platform):
    helper = str(tmp_path / "immutable-helper.py")
    monkeypatch.setattr(service, "helper_path", lambda *a, **k: helper)
    monkeypatch.setattr(service, "python_executable", lambda *a, **k: sys.executable)
    _, body = service.render(tmp_path, platform=platform)
    if platform == "darwin":
        candidate = plistlib.loads(body.encode())
        argv = candidate["ProgramArguments"]
        assert argv[:7] == [sys.executable, "-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--"]
        assert argv[7:] == [sys.executable, "-m", "mcs_standalone", "run", "--root", str(tmp_path)]
        assert candidate["StandardOutPath"] == candidate["StandardErrorPath"] == os.devnull
        assert candidate["ExitTimeOut"] == 3660 and candidate["AbandonProcessGroup"]
    else:
        assert "immutable-helper.py" in body and "StandardOutput=null" in body
        assert "KillMode=control-group" in body and "TimeoutStopSec=3660" in body


def test_lineworks_candidate_does_not_open_a_raw_log(tmp_path, monkeypatch):
    import run_log_install
    helper = str(tmp_path / "helper.py")
    monkeypatch.setattr(run_log_install, "helper_path", lambda *a, **k: helper)
    path = lineworks_service.generate(tmp_path, python_executable=sys.executable, platform="darwin")
    value = plistlib.loads(path.read_bytes())
    assert value["ProgramArguments"][1:7] == ["-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--"]
    assert value["StandardOutPath"] == value["StandardErrorPath"] == os.devnull
    assert not list((tmp_path / "data" / "lineworks_state").glob("*.log"))


def test_detached_update_has_no_parent_drain_thread_or_raw_log(tmp_path, monkeypatch):
    import run_log_install
    monkeypatch.setattr(mcs_util, "load_config", lambda: {"runtime_mode": "standalone"})
    helper = str(tmp_path / "helper.py")
    monkeypatch.setattr(run_log_install, "helper_path", lambda root, *a, **kw: helper if kw["require_installed"] else pytest.fail("unverified"))
    seen = []
    monkeypatch.setattr(update_launch.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)))
    monkeypatch.setattr(run_log, "PipeCapture", lambda *a: pytest.fail("detached parent must not own a thread"))
    update_launch.spawn_detached(root=tmp_path, data_dir=tmp_path / "data", hermes_wrapper="/synthetic/unused",
                                 environment={"SYNTHETIC": "keep", "MCS_JOB_PID": "1"})
    argv, kw = seen[0]
    assert argv == [sys.executable, "-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--", str(tmp_path / "scripts/mcs_update.sh")]
    assert kw["env"] == {"SYNTHETIC": "keep"} and kw["start_new_session"]
    assert kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    assert not (tmp_path / "data" / "update.log").exists()


def test_runtime_spawns_verified_exec_helper_and_retains_original_orphan_argv(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    original = list(host.argv["extract-0"])
    assert original[1].endswith("extract_llm.py")
    helper = str(tmp_path / "immutable-helper.py")
    monkeypatch.delenv("MCS_RUN_LOG_HELPER", raising=False)
    monkeypatch.setattr(runtime, "helper_path", lambda root, recovery, **kw: helper if kw["require_installed"] else pytest.fail("unverified"))
    seen = []
    child = SimpleNamespace(pid=1234, poll=lambda: None)
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)) or child)
    monkeypatch.setattr(runtime, "log", lambda *a, **k: None)
    host.start("extract-0", "background", 1)
    argv, kw = seen[0]
    assert argv[:7] == [sys.executable, "-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--"]
    assert argv[7:] == original and host.argv["extract-0"] == original
    assert host.children["extract-0"]["process"].pid == 1234
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL


def test_runtime_refuses_an_unverified_helper_before_start(tmp_path, monkeypatch):
    import run_log_install
    (tmp_path / "data").mkdir()
    host = runtime.Runtime(tmp_path, {"runtime_mode": "standalone"})
    monkeypatch.delenv("MCS_RUN_LOG_HELPER", raising=False)
    monkeypatch.setattr(runtime, "helper_path", lambda *a, **k: (_ for _ in ()).throw(run_log_install.LogInstallError("synthetic")))
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: pytest.fail("unverified spawn"))
    monkeypatch.setattr(runtime, "log", lambda *a, **k: None)
    host.start("extract-0", "background", 1)
    assert host.children == {} and host.restarting


def test_cloud_pool_exec_preserves_parent_and_worker_arguments(tmp_path, monkeypatch):
    import extract_llm
    (tmp_path / "data").mkdir()
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1], "max_concurrency": 1}}
    monkeypatch.setattr(extract_llm, "_cloud_settings", lambda cfg: cfg["cloud_llm"])
    monkeypatch.setattr(extract_llm.cloud_policy, "selected_key", lambda *a, **k: "synthetic")
    monkeypatch.setattr(cloud_backlog, "memory_workers", lambda limit: 1)
    monkeypatch.delenv("MCS_RUN_LOG_HELPER", raising=False)
    helper = str(tmp_path / "helper.py")
    monkeypatch.setattr(cloud_backlog, "helper_path", lambda *a, **k: helper)
    seen = []
    child = SimpleNamespace(poll=lambda: None, terminate=lambda: None, wait=lambda **kw: 0)
    monkeypatch.setattr(cloud_backlog.subprocess, "Popen", lambda argv, **kw: seen.append((argv, kw)) or child)
    pool = cloud_backlog.Pool(tmp_path)
    try:
        pool.sync(cfg)
        argv, kw = seen[0]
        assert argv[:7] == [sys.executable, "-I", helper, "exec", "--data-dir", str(tmp_path / "data"), "--"]
        assert argv[7:] == [sys.executable, str(Path(cloud_backlog.__file__).resolve()), "--parent", str(os.getpid())]
        assert kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
    finally:
        pool.close()


def test_managed_child_keeps_logging_after_its_parent_has_exited(tmp_path):
    import run_log_install
    data, recovery = tmp_path / "data", tmp_path / "recovery"
    data.mkdir(mode=0o700)
    recovery.mkdir(mode=0o700)
    run_log_install.prepare(tmp_path, REPO, recovery, promote=True)
    helper = run_log_install.helper_path(tmp_path, recovery, require_installed=True)
    gate = tmp_path / "continue"
    worker = tmp_path / "synthetic-worker.py"
    worker.write_text("import json,os,sys,time\nfrom pathlib import Path\n"
                      "print(json.dumps({'pid':os.getpid(),'argv':sys.argv[1:]}),flush=True)\n"
                      "while not Path(sys.argv[1]).exists(): time.sleep(0.01)\n"
                      "print('after-parent-exit',flush=True)\n")
    argv = [sys.executable, "-I", helper, "exec", "--data-dir", str(data), "--",
            sys.executable, str(worker), str(gate)]
    code = ("import json,os,signal,subprocess,sys; "
            "child=subprocess.Popen(json.loads(sys.argv[1]),stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            "print(child.pid,flush=True); os.kill(os.getpid(),signal.SIGKILL)")
    parent = subprocess.run([sys.executable, "-I", "-c", code, json.dumps(argv)], capture_output=True, text=True, timeout=10)
    assert parent.returncode == -9
    child_pid = int(parent.stdout.strip())
    gate.touch()
    until = time.monotonic() + 10
    raw = b""
    while time.monotonic() < until:
        if (data / "mcs.log").exists():
            raw = (data / "mcs.log").read_bytes()
        if b"after-parent-exit" in raw:
            break
        time.sleep(0.01)
    try:
        records = raw.splitlines()
        assert records[-1] == b"after-parent-exit"
        assert json.loads(records[0]) == {"pid": child_pid, "argv": [str(gate)]}
    finally:
        gate.touch()  # A failing assertion must still release the synthetic worker.


@pytest.mark.parametrize("script", ["mcs_check.sh", "mcs_deep.sh"])
@pytest.mark.parametrize("code", [0, 1, 2])
def test_wrapper_records_json_without_changing_alert_stdout_or_exit(tmp_path, script, code):
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    source = tmp_path / "mcs" / "ingest" / "run_check.py"
    source.parent.mkdir(parents=True)
    source.write_text("import json,sys\nprint(json.dumps({'synthetic':True}))\nprint('synthetic-stderr',file=sys.stderr)\nsys.exit(" + str(code) + ")\n")
    (data / "health.json").write_text('{"collection":"ok"}')
    subs = {"PYTHON": sys.executable, "RUN_LOG_HELPER": str(Path(run_log.__file__).resolve()),
            "DATA": str(data), "REPO": str(tmp_path), "WATCHDOG_GRACE": "60"}
    target = tmp_path / script
    target.write_text(service_contract.render_script((REPO / "deployment/scripts" / script).read_text(), subs))
    result = subprocess.run(["/bin/bash", str(target)], capture_output=True, text=True, timeout=20)
    assert result.returncode == code and result.stderr == ""
    if code in (0, 2):
        assert result.stdout == ""
    else:
        assert "run_check exited 1" in result.stdout and "mcs.log" in result.stdout
    until = time.monotonic() + 5
    while time.monotonic() < until:
        raw = (data / "mcs.log").read_bytes()
        if b"synthetic-stderr" in raw and b'"synthetic": true' in raw:
            break
        time.sleep(0.01)
    assert sorted(raw.splitlines()) == sorted([b'{"synthetic": true}', b'synthetic-stderr'])
    assert not (data / "run.log").exists()


@pytest.mark.parametrize("damage", [None, "helper", "target", "outer_python"])
def test_recovery_inspection_unwraps_only_a_trusted_installed_helper(tmp_path, monkeypatch, damage):
    import run_log_install
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    recovery = tmp_path / "recovery"
    recovery.mkdir(mode=0o700)
    run_log_install.prepare(tmp_path, REPO, recovery, promote=True)
    helper = run_log_install.helper_path(tmp_path, recovery, require_installed=True)
    python = "/synthetic/independent-python"
    argv = [python, "-I", helper, "exec", "--data-dir", str(data), "--", python,
            str(recovery / "mcs_recover.py"), "--if-stale"]
    if damage == "helper":
        Path(helper).write_text("synthetic-corruption")
    elif damage == "target":
        argv[8] = "/synthetic/other/mcs_recover.py"
    elif damage == "outer_python":
        argv[0] = "/synthetic/different-python"
    plist = tmp_path / "recovery.plist"
    plist.write_bytes(plistlib.dumps({"ProgramArguments": argv}))
    probes = []
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe, **kw: probes.append(exe) or {"synthetic": True})
    result = runtime_inspection.recovery_runtime(plist, checkout=REPO)
    assert result == ({"synthetic": True} if damage is None else None)
    assert probes == ([python] if damage is None else [])
