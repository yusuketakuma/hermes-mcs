"""Doctor is local by default and shares only bounded diagnostic facts."""
import json
from types import SimpleNamespace

import pytest

import local_llm
import mcs_setup


@pytest.fixture
def local_config(monkeypatch):
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "synthetic-private-id", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: {
        "python": "3.11.15", "sqlite": "3.51.3",
        "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version", "3.51.3")
    monkeypatch.setattr(mcs_setup, "env_value",
                        lambda *a, **k: pytest.fail("secret access"))
    monkeypatch.setattr(mcs_setup, "_queue_warnings",
                        lambda *a: pytest.fail("DB access"))


def test_default_doctor_checks_no_services_secrets_or_network(local_config, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: pytest.fail("service probe"))
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: pytest.fail("network"))
    assert mcs_setup.main(["doctor", "--json"]) == 0
    out = capsys.readouterr().out
    checks = json.loads(out)["checks"]
    assert checks["configuration"]["status"] == "healthy"
    for scope in ("llm", "services", "credentials", "data"):
        assert checks[scope]["status"] == "not_checked"
    assert "synthetic-private-id" not in out


def test_explicit_llm_probe_is_bounded_and_other_scopes_stay_unchecked(
        local_config, monkeypatch, capsys):
    calls = []
    def request(url, method, body, timeout):
        calls.append((url, method, body, timeout))
        return 200, {}, b'[{}, {}, {}]' if url.endswith("/slots") else b'{"data":[]}'
    monkeypatch.setattr(local_llm, "bounded_request", request)
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: pytest.fail("service probe"))
    assert mcs_setup.main(["doctor", "--probe", "llm", "--json"]) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["llm"]["status"] == "healthy"
    assert checks["services"]["status"] == "not_checked"
    assert len(calls) == 2
    assert all(method == "GET" and body is None and timeout == 3
               for _, method, body, timeout in calls)


def test_failed_probe_is_warning_and_never_echoes_response(local_config, monkeypatch, capsys):
    raw = b'{"synthetic-secret":"synthetic-body-and-id"}'
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (500, {}, raw))
    assert mcs_setup.main(["doctor", "--probe", "llm", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["checks"]["llm"]["status"] == "warning"
    assert "synthetic-secret" not in out
    assert "synthetic-body-and-id" not in out


def test_undersized_llm_slots_block(local_config, monkeypatch, capsys):
    monkeypatch.setattr(local_llm, "bounded_request", lambda *a: (200, {}, b'[{}]'))
    assert mcs_setup.main(["doctor", "--probe", "llm", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["checks"]["llm"]["status"] == "blocked"


def test_service_domain_failure_is_not_checked(local_config, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(mcs_setup, "check_runtime", lambda cfg: pytest.fail("unavailable"))
    assert mcs_setup.main(["doctor", "--probe", "services", "--json"]) == 0
    check = json.loads(capsys.readouterr().out)["checks"]["services"]
    assert check == {"status": "not_checked", "errors": 0, "warnings": 1}


def test_service_probe_excludes_arbitrary_diagnostic_text(local_config, monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0))
    monkeypatch.setattr(mcs_setup, "check_runtime",
                        lambda cfg: (["synthetic-secret/body/123456"], []))
    assert mcs_setup.main(["doctor", "--probe", "services", "--json"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out)["checks"]["services"]["status"] == "blocked"
    assert "synthetic-secret/body/123456" not in out


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
        return SimpleNamespace(returncode=1 if "sqlite3" in argv[-1] else 0)
    monkeypatch.setattr(mcs_setup, "_run", run)
    assert mcs_setup._py_problem(str(python)) is not None
    assert len(calls) == 2 and all(argv[0] == str(python) for argv in calls)


@pytest.mark.parametrize("started,modified,expected", [(100, 101, True), (102, 101, False),
                                                       (None, 101, None), (100, None, None)])
def test_doctor_does_not_invent_loaded_revision(monkeypatch, started, modified, expected):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_gateway_sources_mtime", lambda: modified)
    def git(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n")
    monkeypatch.setattr(mcs_setup.subprocess, "run", git)
    def probe(argv, **kw):
        return SimpleNamespace(returncode=0, stdout="pid = 123\n" if argv[0] == "launchctl" else "invalid")
    monkeypatch.setattr(mcs_setup, "_run", probe)
    monkeypatch.setattr(mcs_setup.time, "mktime", lambda value: started)
    monkeypatch.setattr(mcs_setup.time, "strptime", lambda value, fmt: ())
    source = mcs_setup._doctor_source({}, probe=True)
    assert source["disk"]["revision"] == "a" * 40
    running = source["gateway"]
    assert running["pid"] == 123 and running["source_revision"] is None
    assert running["matches_disk"] is None
    assert running["restart_required"] is expected
    assert running["status"] == "running_revision_unknown"


def test_doctor_source_default_does_not_probe_services(monkeypatch):
    monkeypatch.setattr(mcs_setup, "_run", lambda *args, **kw: pytest.fail("service access"))
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *args, **kw:
                        SimpleNamespace(returncode=1, stdout="synthetic-secret-not-a-sha"))
    source = mcs_setup._doctor_source({}, probe=False)
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
        {"status": status, "pid": pid, "at": mcs_setup.time.time(), "extra": "synthetic-secret"}))
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout="a" * 40))
    monkeypatch.setattr(mcs_setup, "_run", lambda argv, **kw:
                        SimpleNamespace(returncode=0, stdout="pid = 123\n" if argv[0] == "launchctl" else "invalid"))
    source = mcs_setup._doctor_source({}, probe=True)
    assert source["restart_request"]["status"] == expected
    assert source["gateway"]["source_revision"] is None
    assert "synthetic-secret" not in json.dumps(source)
