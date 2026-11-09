"""Doctor is local by default and shares only bounded diagnostic facts."""
import setup_config_snapshot

import environment_diagnostics
import lifecycle_io
import service_management
import runtime_inspection
import json
import os
from types import SimpleNamespace

import pytest

import local_llm
import mcs_setup
from ops_testkit import bounded_python, git_reads_or




@pytest.fixture
def local_config(monkeypatch):
    monkeypatch.setattr(mcs_setup, '_diagnostic_config', lambda: {
        "mcs_login_id": "synthetic-private-id", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: {
        "python": "3.14.8", "sqlite": "3.51.3",
        "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version", "3.51.3")
    monkeypatch.setattr(environment_diagnostics, "env_value",
                        lambda *a, **k: pytest.fail("secret access"))
    monkeypatch.setattr(mcs_setup, "_queue_warnings",
                        lambda *a: pytest.fail("DB access"))


@pytest.mark.parametrize("marker", ["pending", "authority"])
@pytest.mark.parametrize("output", ["text", "json", "share"])
def test_doctor_holds_invalid_provider_without_running_probes(tmp_path, monkeypatch, capsys, marker, output):
    import cloud_cli

    tmp_path.chmod(0o700)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mcs_login_id": "synthetic-secret-canary", "notify_target": "local"}))
    config.chmod(0o600)
    witness = data / (".provider-settings-" + marker + ".json")
    witness.write_text('{"synthetic":"private-canary"}')
    witness.chmod(0o600)
    before = (config.read_bytes(), witness.read_bytes())
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(config))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(config))
    monkeypatch.setattr(cloud_cli, "diagnostics", lambda *a, **k: pytest.fail("provider probe"))
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda *a, **k: pytest.fail("runtime probe"))
    monkeypatch.setattr(lifecycle_io, "run_bounded", lambda *a, **k: pytest.fail("service probe"))
    args = SimpleNamespace(json=output == "json", share=output == "share", probe=["data", "services", "llm"])
    assert mcs_setup.cmd_doctor(args) == 1
    text = capsys.readouterr().out
    assert "synthetic-secret-canary" not in text and "private-canary" not in text
    if output == "text":
        assert "提供元設定" in text and "要修正" in text
    else:
        checks = json.loads(text)["checks"]
        assert checks["configuration"] == {"status": "blocked", "errors": 1, "warnings": 0}
        assert all(checks[name]["status"] == "not_checked" for name in ("services", "data", "llm"))
    assert (config.read_bytes(), witness.read_bytes()) == before


def test_check_holds_provider_without_running_services(tmp_path, monkeypatch, capsys):
    tmp_path.chmod(0o700)
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    config = tmp_path / "config.json"
    config.write_text('{}')
    pending = data / ".provider-settings-pending.json"
    pending.write_text('{}')
    pending.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(config))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(config))
    monkeypatch.setattr(service_management, "check_runtime", lambda *a, **k: pytest.fail("service probe"))
    assert mcs_setup.cmd_check(None) == 1
    assert "提供元設定" in capsys.readouterr().out
    assert pending.read_text() == '{}'


def test_default_doctor_checks_no_services_secrets_or_network(local_config, monkeypatch, capsys):
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *a, **k: pytest.fail("service probe")))
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: pytest.fail("network"))
    assert mcs_setup.main(["doctor", "--json"]) == 0
    out = capsys.readouterr().out
    checks = json.loads(out)["checks"]
    assert checks["configuration"]["status"] == "healthy"
    for scope in ("llm", "services", "credentials", "data"):
        assert checks[scope]["status"] == "not_checked"
    assert "synthetic-private-id" not in out


@pytest.mark.parametrize("explicit", [False, True])
def test_data_probe_reaches_restore_diagnosis_only_when_requested(
        local_config, monkeypatch, capsys, explicit):
    import card_worker_status
    probes = []
    def diagnose(root, cfg, *, probe_data=False):
        probes.append(probe_data)
        return {"state": "unknown", "reason": "worker_start_unverified",
                "restore_delivery_held": False if probe_data else None,
                "recovery_scope": "collect_only"}
    monkeypatch.setattr(card_worker_status, "diagnose", diagnose)
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *a, **k: pytest.fail("service probe")))
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: pytest.fail("network"))
    args = ["doctor", "--json"] + (["--probe", "data"] if explicit else [])
    assert mcs_setup.main(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert probes == [explicit]
    assert report["card_worker"]["restore_delivery_held"] is (False if explicit else None)


def test_explicit_llm_probe_is_bounded_and_other_scopes_stay_unchecked(
        local_config, monkeypatch, capsys):
    calls = []
    def request(url, method, body, timeout):
        calls.append((url, method, body, timeout))
        return 200, {}, b'[{}, {}, {}]' if url.endswith("/slots") else b'{"data":[]}'
    monkeypatch.setattr(local_llm, "bounded_request", request)
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *a, **k: pytest.fail("service probe")))
    assert mcs_setup.main(["doctor", "--probe", "llm", "--json"]) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["llm"]["status"] == "healthy"
    assert checks["services"]["status"] == "not_checked"
    assert len(calls) == 2
    assert all(method == "GET" and body is None and timeout == 3
               for _, method, body, timeout in calls)


@pytest.mark.parametrize("output", ["text", "json", "share"])
def test_failed_probe_is_warning_and_never_echoes_response(local_config, monkeypatch, capsys, output):
    raw = b'{"synthetic-secret":"synthetic-body-and-id"}'
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (500, {}, raw))
    args = ["doctor", "--probe", "llm"] + ([] if output == "text" else ["--" + output])
    assert mcs_setup.main(args) == 0
    captured = capsys.readouterr()
    if output != "text":
        assert json.loads(captured.out)["checks"]["llm"]["status"] == "warning"
    assert "synthetic-secret" not in captured.out + captured.err
    assert "synthetic-body-and-id" not in captured.out + captured.err


def test_undersized_llm_slots_block(local_config, monkeypatch, capsys):
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (200, {}, b'[{}]'))
    assert mcs_setup.main(["doctor", "--probe", "llm", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"]["llm"]["status"] == "blocked"


def test_service_domain_failure_is_not_checked(local_config, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        git_reads_or(lambda *a, **k: SimpleNamespace(returncode=1)))
    monkeypatch.setattr(service_management, 'check_runtime', lambda cfg, **_service_kwargs: pytest.fail("unavailable"))
    assert mcs_setup.main(["doctor", "--probe", "services", "--json"]) == 0
    check = json.loads(capsys.readouterr().out)["checks"]["services"]
    assert check == {"status": "not_checked", "errors": 0, "warnings": 1}


@pytest.mark.parametrize("output", ["text", "json", "share"])
def test_service_probe_excludes_arbitrary_diagnostic_text(local_config, monkeypatch, capsys, output):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        git_reads_or(lambda *a, **k: SimpleNamespace(returncode=0)))
    monkeypatch.setattr(service_management, 'check_runtime',
                        lambda cfg, **_service_kwargs: (["synthetic-secret/body/123456"], []))
    args = ["doctor", "--probe", "services"] + ([] if output == "text" else ["--" + output])
    assert mcs_setup.main(args) == 1
    captured = capsys.readouterr()
    if output != "text":
        assert json.loads(captured.out)["checks"]["services"]["status"] == "blocked"
    assert "synthetic-secret/body/123456" not in captured.out + captured.err


@pytest.mark.parametrize("version,safe", [
    ((3, 50, 4), False), ((3, 50, 6), False), ((3, 50, 7), True),
    ((3, 51, 2), False), ((3, 51, 3), True), ((3, 53, 4), True),
    ((3, 44, 5), False), ((3, 44, 6), True), ((3, 45, 9), False),
])
def test_sqlite_wal_fix_controls_doctor_exit(local_config, monkeypatch, capsys, version, safe):
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", version)
    assert mcs_setup.main(["doctor", "--json"]) == (0 if safe else 1)
    assert json.loads(capsys.readouterr().out)["checks"]["interpreter"]["status"] \
        == ("healthy" if safe else "blocked")


def test_service_runtime_sqlite_is_checked_not_path_python(monkeypatch, tmp_path):
    python = tmp_path / "runtime"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    calls = []
    def run(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="3 51 2\n" if "sqlite3" in argv[-1] else "")
    monkeypatch.setattr(runtime_inspection, "run_bounded", run)
    assert runtime_inspection.python_problem(str(python)) is not None
    assert len(calls) == 2 and all(argv[0] == str(python) for argv in calls)
    assert "import" in calls[1][-1] and "mcs" not in calls[1][-1]     # the child stays self-contained


# independent oracle: the maintained fixed releases, not mcs_runtime's own expression
_SAFE = {(3, 44, 6), (3, 44, 7), (3, 50, 7), (3, 50, 9), (3, 51, 3), (3, 52, 0), (3, 53, 4)}


@pytest.mark.parametrize("reported,rc", [
    ("3 44 5\n", 0), ("3 44 6\n", 0), ("3 44 7\n", 0), ("3 45 9\n", 0), ("3 50 6\n", 0),
    ("3 50 7\n", 0), ("3 50 9\n", 0), ("3 51 2\n", 0), ("3 51 3\n", 0), ("3 52 0\n", 0),
    ("3 53 4\n", 0), ("3 51 3\n", 1), ("", 0), ("garbage\n", 0), ("3 51\n", 0),
    ("3 51 3 1\n", 0), ("3.51.3\n", 0)])
def test_service_runtime_sqlite_uses_the_shared_policy_and_fails_closed(monkeypatch, tmp_path, reported, rc):
    python = tmp_path / "runtime"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    monkeypatch.setattr(runtime_inspection, "run_bounded", lambda argv, **kw: SimpleNamespace(
        returncode=rc if "sqlite3" in argv[-1] else 0, stdout=reported if "sqlite3" in argv[-1] else ""))
    parts = reported.split()
    safe = rc == 0 and len(parts) == 3 and all(p.isdigit() for p in parts) \
        and tuple(map(int, parts)) in _SAFE
    problem = runtime_inspection.python_problem(str(python))
    assert (problem is None) is safe
    if not safe:
        assert problem == ("常駐処理のSQLiteに安全なデータ保存の修正がありません"
                           "（3.51.3以上、3.50.7以上、または3.44.6以上の対応系列が必要です）")


@pytest.mark.parametrize("started,modified,expected", [(100, 101, True), (102, 101, False),
                                                       (None, 101, None), (100, None, None)])
def test_doctor_does_not_invent_loaded_revision(monkeypatch, started, modified, expected):
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics, "gateway_sources_mtime", lambda checkout: modified)
    def git(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n")
    monkeypatch.setattr(lifecycle_io.subprocess, "run", git)
    def probe(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="pid = 123\n" if argv[0] == "launchctl" else "invalid")
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(probe))
    monkeypatch.setattr(environment_diagnostics.time, "mktime", lambda value: started)
    monkeypatch.setattr(environment_diagnostics.time, "strptime", lambda value, fmt: ())
    source = environment_diagnostics.source_status({}, probe=True, paths=mcs_setup._service_paths())
    assert source["disk"]["revision"] == "a" * 40
    running = source["gateway"]
    assert running["pid"] == 123 and running["source_revision"] is None
    assert running["matches_disk"] is None
    assert running["restart_required"] is expected
    assert running["status"] == "running_revision_unknown"


def test_doctor_source_default_does_not_probe_services(monkeypatch):
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *args, **kw: pytest.fail("service access")))
    monkeypatch.setattr(lifecycle_io.subprocess, "run", lambda *args, **kw:
                        SimpleNamespace(returncode=1, stdout="synthetic-secret-not-a-sha"))
    source = environment_diagnostics.source_status({}, probe=False, paths=mcs_setup._service_paths())
    assert source["disk"]["revision"] is None
    assert source["gateway"]["status"] == "not_checked"
    assert source["gateway"]["source_revision"] is None
    assert "synthetic-secret" not in json.dumps(source)


@pytest.mark.parametrize("status,pid,expected", [("failed", 123, "failed"),
    ("supervisor_restart_verified", 123, "supervisor_restart_verified"),
    ("supervisor_restart_verified", 999, "stale")])
def test_doctor_restart_receipt_is_supervisor_evidence_only(tmp_path, monkeypatch, status, pid, expected):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "gateway_restart.json").write_text(json.dumps(
        {"status": status, "pid": pid, "at": environment_diagnostics.time.time(), "extra": "synthetic-secret"}))
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle_io.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="a" * 40))
    monkeypatch.setattr(lifecycle_io, 'run_bounded', lambda argv, **kw:
                        SimpleNamespace(returncode=0, stdout="pid = 123\n" if argv[0] == "launchctl" else "invalid"))
    source = environment_diagnostics.source_status({}, probe=True, paths=mcs_setup._service_paths())
    assert source["restart_request"]["status"] == expected
    assert source["gateway"]["source_revision"] is None
    assert "synthetic-secret" not in json.dumps(source)


@pytest.mark.parametrize("rc,out,expected", [(0, "", False), (0, " M mcs/ops/x.py\n", True),
                                             (128, "", None)])
def test_doctor_dirty_comes_from_a_refreshing_status(monkeypatch, rc, out, expected):
    """An mtime-only change (touch/rsync) is clean: plumbing diff-index
    compares stale index stat data, status refreshes it."""
    seen = []

    def git(argv, **kw):
        seen.append(argv)
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n")
        if "diff-index" in argv:
            return SimpleNamespace(returncode=1, stdout="")   # stale stat data
        return SimpleNamespace(returncode=rc, stdout=out)
    monkeypatch.setattr(lifecycle_io.subprocess, "run", git)
    assert environment_diagnostics.source_status({}, probe=False, paths=mcs_setup._service_paths())["disk"]["dirty"] is expected
    assert not any("diff-index" in argv for argv in seen)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory", "oversized"])
def test_doctor_restart_receipt_refuses_unsafe_file(tmp_path, monkeypatch, kind):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    target = tmp_path / "outside.json"
    target.write_text(json.dumps({"status": "failed", "at": environment_diagnostics.time.time()}))
    receipt = tmp_path / "data" / "gateway_restart.json"
    if kind == "symlink":
        receipt.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(receipt)
        bounded_python(tmp_path, """
from types import SimpleNamespace
import mcs_setup
import environment_diagnostics
import lifecycle_io
mcs_setup.HOME = sys.argv[1]
lifecycle_io.subprocess.run = lambda *a, **k: SimpleNamespace(returncode=1, stdout='')
environment_diagnostics.gateway_sources_mtime = lambda checkout: None
source = environment_diagnostics.source_status({'runtime_mode': 'standalone'}, probe=True, paths=mcs_setup._service_paths())
assert source['restart_request'] == {'status': 'unknown'}
""", tmp_path)
        return
    elif kind == "directory":
        receipt.mkdir()
    else:
        receipt.write_bytes(b" " * 8193)
    monkeypatch.setattr(lifecycle_io.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(environment_diagnostics, "gateway_sources_mtime", lambda checkout: None)
    source = environment_diagnostics.source_status({"runtime_mode": "standalone"}, probe=True, paths=mcs_setup._service_paths())
    assert source["restart_request"] == {"status": "unknown"}


@pytest.mark.parametrize('state,reason', [('unknown', 'worker_start_unverified'),
                                         ('disabled', 'profile_mismatch')])
def test_doctor_exposes_worker_and_restore_holds(local_config, monkeypatch, capsys, state, reason):
    import card_worker_status
    monkeypatch.setattr(card_worker_status, 'diagnose', lambda *a, **kw: {
        'state': state, 'reason': reason, 'restore_delivery_held': True,
        'recovery_scope': 'collect_only'})
    assert mcs_setup.main(['doctor', '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['card_worker']['reason'] == reason
    assert result['card_worker']['recovery_scope'] == 'collect_only'
    assert result['checks']['card_worker']['status'] == 'warning'
    assert result['checks']['restore_delivery']['status'] == 'warning'


@pytest.mark.parametrize("command", ["check", "doctor"])
@pytest.mark.parametrize("output", ["json", "share"])
def test_diagnostic_split_snapshot_race_blocks_all_probes(tmp_path, monkeypatch, capsys, command, output):
    import hashlib
    import cloud_cli
    import provider_settings

    tmp_path.chmod(0o700)
    config = tmp_path / "config.json"
    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    directory = data / "provider_settings"
    directory.mkdir(mode=0o700)

    def write_json(path, value):
        raw = json.dumps(value, sort_keys=True).encode()
        path.write_bytes(raw)
        path.chmod(0o600)
        return hashlib.sha256(raw).hexdigest()

    common = {"mcs_login_id": "synthetic-canary", "notify_target": "local",
              "cloud_llm": {"mode": "off", "project_ids": []}, "semantic": {"mode": "off"}}
    write_json(config, common)
    hashes = {name: write_json(directory / (name + ".json"), {}) for name in provider_settings.PROVIDERS}
    write_json(data / ".provider-settings-authority.json", {
        "version": 2, "state": "committed", "files": hashes,
        "common_policy_sha256": provider_settings.common_policy_digest(common)})
    original = provider_settings._read_file
    changed = False

    def race(parent, name, **kwargs):
        nonlocal changed
        result = original(parent, name, **kwargs)
        if name == "openai.json" and not changed:
            changed = True
            write_json(data / ".provider-settings-pending.json", {})
        return result

    monkeypatch.setattr(provider_settings, "_read_file", race)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(config))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(config))
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: pytest.fail("second config read"))
    monkeypatch.setattr(cloud_cli, "diagnostics", lambda *a, **k: pytest.fail("provider probe"))
    monkeypatch.setattr(service_management, "check_runtime", lambda *a, **k: pytest.fail("service probe"))
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda *a, **k: pytest.fail("runtime probe"))
    monkeypatch.setattr(lifecycle_io, "run_bounded", lambda *a, **k: pytest.fail("service probe"))
    args = SimpleNamespace(json=output == "json", share=output == "share", probe=["data", "services", "llm"])
    assert (mcs_setup.cmd_check(args) if command == "check" else mcs_setup.cmd_doctor(args)) == 1
    text = capsys.readouterr().out
    assert changed and "synthetic-canary" not in text
    if command == "doctor":
        checks = json.loads(text)["checks"]
        assert checks["configuration"]["status"] == "blocked"
        assert all(checks[name]["status"] == "not_checked" for name in ("llm", "services", "data"))


@pytest.mark.parametrize("raw", ["{", "[]", '{"duplicate":1,"duplicate":2}'])
@pytest.mark.parametrize("command", ["check", "doctor"])
def test_unreadable_legacy_config_blocks_before_probes(tmp_path, monkeypatch, capsys, raw, command):
    config = tmp_path / "config.json"
    config.write_text(raw)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(config))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(config))
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: pytest.fail("second config read"))
    monkeypatch.setattr(service_management, "check_runtime", lambda *a, **k: pytest.fail("service probe"))
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda *a, **k: pytest.fail("runtime probe"))
    args = SimpleNamespace(json=True, share=False, probe=["data", "services", "llm"])
    assert (mcs_setup.cmd_check(args) if command == "check" else mcs_setup.cmd_doctor(args)) == 1
    out = capsys.readouterr().out
    if command == "doctor":
        report = json.loads(out)
        assert report["checks"]["configuration"]["status"] == "blocked"
        assert all(report["checks"][name]["status"] == "not_checked" for name in ("llm", "services", "data"))
    else:
        assert "読み取れないか、形式が不正" in out
    assert config.read_text() == raw


def test_diagnostic_missing_legacy_config_is_unconfigured(tmp_path, monkeypatch):
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "config.json"))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(tmp_path / "config.json"))
    assert mcs_setup._diagnostic_config() == {}


def test_check_uses_one_diagnostic_snapshot(monkeypatch, capsys):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "local"}
    calls = []
    monkeypatch.setattr(mcs_setup.mcs_util, "_load_config", lambda path: calls.append(path) or cfg)
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: pytest.fail("second config read"))
    monkeypatch.setattr(service_management, "check_runtime", lambda *a, **k: ([], []))
    monkeypatch.setattr(service_management, "script_drift", lambda *a, **k: ([], []))
    monkeypatch.setattr(mcs_setup, "validate_config", lambda value: ([], []))
    monkeypatch.setattr(mcs_setup, "check_environment", lambda value: ([], []))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda value: [])
    assert mcs_setup.cmd_check(None) == 0
    assert len(calls) == 1
    assert "設定確認: 正常" in capsys.readouterr().out
