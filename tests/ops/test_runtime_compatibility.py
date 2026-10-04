"""Selected runtime metadata and SQLite checks using only synthetic resources."""
import json
import hashlib
import os
from pathlib import Path
import plistlib
import sys
from types import SimpleNamespace

import pytest

import mcs_setup
from test_mcs_update import updater as updater


def _facts(python="3.11.15", sqlite="3.51.3"):
    return {"python": python, "sqlite": sqlite,
            "packages": {"discord.py": "2.7.1", "slack-bolt": "1.30.0",
                         "slack-sdk": "3.45.0"}}


@pytest.mark.parametrize(("version", "safe"), [
    ((3, 7, 0), False), ((3, 43, 9), False), ((3, 44, 0), False),
    ((3, 44, 5), False), ((3, 44, 6), True), ((3, 44, 7), True),
    ((3, 45, 0), False), ((3, 49, 9), False), ((3, 50, 4), False),
    ((3, 50, 6), False), ((3, 50, 7), True), ((3, 50, 8), True),
    ((3, 51, 0), False), ((3, 51, 2), False), ((3, 51, 3), True),
    ((3, 52, 0), True), ((3, 53, 4), True),
])
def test_supported_wal_versions(version, safe):
    assert mcs_setup.sqlite_wal_safe(version) is safe


@pytest.mark.parametrize(("python", "sqlite", "recovery", "supported"), [
    ("3.9.6", "3.51.3", False, False),
    ("3.10.0", "3.51.3", False, True),
    ("3.13.2", "3.50.4", False, False),
    ("3.13.2", "3.50.7", False, True),
    ("3.9.6", "3.44.6", True, True),
    ("3.8.10", "3.51.3", True, False),
    ("3.9.6", "3.51.0", True, False),
])
def test_runtime_compatibility_has_separate_recovery_python_contract(
        python, sqlite, recovery, supported):
    assert (mcs_setup._runtime_problem(_facts(python, sqlite), recovery=recovery)
            is None) is supported


@pytest.fixture
def executable(tmp_path):
    path = tmp_path / "selected-python"
    path.touch()
    path.chmod(0o700)
    return str(path)


def test_probe_uses_selected_executable_and_never_sdk_import(executable, monkeypatch):
    calls = []
    def run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(returncode=0, stdout=json.dumps(_facts()))
    monkeypatch.setattr(mcs_setup, "_run", run)
    assert mcs_setup._runtime_probe(executable) == _facts()
    argv, opts = calls[0]
    assert argv[:3] == [executable, "-I", "-c"]
    assert opts == {"timeout": 20}


def test_probe_executes_real_stdlib_script_inside_isolated_test_environment():
    # Test-runner Python only, not the machine's scheduled or SDK interpreter.
    facts = mcs_setup._runtime_probe(sys.executable)
    assert facts is not None
    assert facts["python"] == ".".join(map(str, sys.version_info[:3]))
    assert facts["sqlite"] == mcs_setup.sqlite3.sqlite_version
    assert set(facts["packages"]) == {"discord.py", "slack-bolt", "slack-sdk"}


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=124, stdout="synthetic-secret"),
    SimpleNamespace(returncode=1, stdout=json.dumps(_facts())),
    SimpleNamespace(returncode=0),
    SimpleNamespace(returncode=0, stdout="synthetic-secret"),
    SimpleNamespace(returncode=0, stdout="[]"),
    SimpleNamespace(returncode=0, stdout=json.dumps({**_facts(), "python": "bad"})),
    SimpleNamespace(returncode=0, stdout=json.dumps({**_facts(), "sqlite": None})),
    SimpleNamespace(returncode=0, stdout=json.dumps({**_facts(), "packages": {}})),
    SimpleNamespace(returncode=0, stdout=json.dumps({
        **_facts(), "packages": {**_facts()["packages"], "discord.py": "synthetic-secret"}})),
])
def test_unknown_metadata_never_means_healthy(executable, monkeypatch, result):
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: result)
    facts = mcs_setup._runtime_probe(executable)
    assert facts is None
    assert mcs_setup._runtime_problem(facts) is not None


@pytest.mark.parametrize(("mode", "sqlite", "expected"), [
    ("hermes", "3.50.4", 1), ("hermes", "3.51.3", 0),
    ("standalone", "3.51.2", 1), ("standalone", "3.50.7", 0),
])
def test_doctor_reports_selected_versions_not_parent(
        monkeypatch, tmp_path, capsys, mode, sqlite, expected):
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "runtime_mode": mode, "mcs_login_id": "synthetic-private",
        "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/synthetic/hermes-python")
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    calls = []
    def probe(exe):
        calls.append(exe)
        return _facts(sqlite=sqlite)
    monkeypatch.setattr(mcs_setup, "_runtime_probe", probe)
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: pytest.fail("service access"))
    assert mcs_setup.cmd_doctor(SimpleNamespace(json=True)) == expected
    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["runtimes"]["selected"]["sqlite"] == sqlite
    assert report["runtimes"]["selected"]["packages"]["discord.py"] == "2.7.1"
    assert report["checks"]["interpreter"]["status"] == "healthy"
    assert report["checks"]["runtime"]["status"] == ("blocked" if expected else "healthy")
    assert report["checks"]["recovery_runtime"]["status"] == "not_checked"
    assert calls == [mcs_setup._services_py({"runtime_mode": mode})]
    assert "synthetic-private" not in out and "/synthetic/" not in out


@pytest.mark.parametrize("use_program", [False, True])
def test_recovery_probes_actual_deployed_program(monkeypatch, tmp_path, use_program):
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path))
    plist = {"ProgramArguments": ["/synthetic/system-python", "watchdog.py"]}
    if use_program:
        plist["Program"] = "/synthetic/overridden-python"
    (tmp_path / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(plist))
    calls = []
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: calls.append(exe) or _facts("3.9.6", "3.51.0"))
    facts = mcs_setup._recovery_runtime()
    assert calls == [plist.get("Program", plist["ProgramArguments"][0])]
    assert mcs_setup._runtime_problem(facts, recovery=True) is not None


def test_doctor_blocks_unsafe_recovery_without_touching_services(
        monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path))
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "synthetic", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    (tmp_path / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": ["/synthetic/recovery-python", "watchdog.py"]}))
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe:
                        _facts("3.9.6", "3.51.0") if exe == "/synthetic/recovery-python"
                        else _facts())
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: pytest.fail("service access"))
    assert mcs_setup.cmd_doctor(SimpleNamespace(json=True)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["checks"]["recovery_runtime"]["status"] == "blocked"
    assert report["checks"]["services"]["status"] == "not_checked"


@pytest.mark.parametrize("content", [b"broken", plistlib.dumps({}),
                                   plistlib.dumps({"ProgramArguments": ["relative"]})])
def test_invalid_recovery_plist_is_unverified(monkeypatch, tmp_path, content):
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path))
    (tmp_path / "org.mcs.recovery.plist").write_bytes(content)
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: pytest.fail("invalid program"))
    assert mcs_setup._recovery_runtime() is None


def _tree(root):
    return {str(path.relative_to(root)): (
        path.lstat().st_mode, path.lstat().st_mtime_ns,
        hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        for path in [root, *sorted(root.rglob("*"))]}


def _deployed_recovery(monkeypatch, tmp_path):
    agents = tmp_path / "runtime-agents"
    agents.mkdir()
    executable = tmp_path / "recovery-python"
    executable.touch()
    executable.chmod(0o700)
    (agents / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": [str(executable), "synthetic-recovery.py"]}))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    return str(executable)


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize(("role", "facts"), [
    ("selected", None), ("selected", _facts(sqlite="3.51.0")),
    ("selected", _facts(python="3.9.6")),
    ("recovery", None), ("recovery", _facts("3.9.6", "3.51.0")),
    ("recovery", _facts("3.8.10")),
])
def test_services_runtime_gate_precedes_all_mutation(
        monkeypatch, tmp_path, capsys, mode, dry, role, facts):
    from test_mcs_setup import _services_env
    calls, args = _services_env(monkeypatch, tmp_path)
    args.dry_run = dry
    cfg = {"runtime_mode": mode}
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: cfg if path is None else {})
    monkeypatch.setattr(mcs_setup, "_standalone_py_problem", lambda cfg: None)
    recovery = _deployed_recovery(monkeypatch, tmp_path)
    selected = mcs_setup._service_subs(cfg)["PYTHON"]
    probes = []

    def probe(exe):
        probes.append(exe)
        return facts if exe == (selected if role == "selected" else recovery) else _facts()

    monkeypatch.setattr(mcs_setup, "_runtime_probe", probe)
    for name in ("_retire_previous_runtime", "_sync_scripts", "_sync_agents",
                 "_sync_standalone_service", "_save_manifest"):
        monkeypatch.setattr(mcs_setup, name,
                            lambda *a, **k: pytest.fail("runtime gate allowed mutation"))
    before = _tree(tmp_path)
    assert mcs_setup.cmd_services(args) == 1
    assert calls == [] and _tree(tmp_path) == before
    assert probes == [selected, recovery]
    assert sys.executable not in probes
    assert role + "_runtime:" in capsys.readouterr().out


def test_safe_service_runtimes_keep_python_roles_and_rendering(monkeypatch, tmp_path):
    from test_mcs_setup import _services_env
    calls, args = _services_env(monkeypatch, tmp_path)
    recovery = _deployed_recovery(monkeypatch, tmp_path)
    selected = mcs_setup._service_subs({})["PYTHON"]
    probes = []

    def probe(exe):
        probes.append(exe)
        return _facts("3.9.6", "3.44.6") if exe == recovery else _facts("3.10.0", "3.50.7")

    monkeypatch.setattr(mcs_setup, "_runtime_probe", probe)
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 0))
    assert mcs_setup.cmd_services(args) == 0
    assert probes == [selected, recovery]
    assert any(call[:2] == ["launchctl", "bootstrap"] for call in calls)
    assert selected in (tmp_path / "scripts/mcs_check.sh").read_text()


@pytest.mark.parametrize("mode", ["hermes", "standalone"])
@pytest.mark.parametrize(("role", "facts", "blocked"), [
    ("selected", _facts("3.10.0", "3.51.3"), False),
    ("selected", None, True), ("selected", _facts(sqlite="3.51.0"), True),
    ("recovery", None, True), ("recovery", _facts("3.9.6", "3.51.0"), True),
    ("recovery", _facts("3.9.6", "3.50.7"), False),
])
def test_updater_precheck_probes_actual_runtimes_not_parent(
        updater, monkeypatch, tmp_path, mode, role, facts, blocked):
    cfg = {"runtime_mode": mode, "mcs_login_id": "synthetic", "notify_target": "local"}
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    monkeypatch.setattr(mcs_setup, "_standalone_py_problem", lambda cfg: None)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: True)
    recovery = _deployed_recovery(monkeypatch, tmp_path)
    selected = mcs_setup._service_subs(cfg)["PYTHON"]
    probes = []

    def probe(exe):
        probes.append(exe)
        return facts if exe == (selected if role == "selected" else recovery) else _facts()

    monkeypatch.setattr(mcs_setup, "_runtime_probe", probe)
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 0))
    before = _tree(tmp_path)
    errors = updater.precheck_local(cfg)
    assert bool(errors) is blocked
    if blocked:
        assert errors == [role + "_runtime: " + mcs_setup._runtime_problem(
            facts, recovery=role == "recovery")]
    assert probes == [selected, recovery] and sys.executable not in probes
    assert _tree(tmp_path) == before


@pytest.mark.parametrize("role", ["selected", "recovery"])
@pytest.mark.parametrize("facts", [None, _facts(sqlite="3.51.0")])
@pytest.mark.parametrize("interrupted", [False, True])
def test_apply_runtime_gate_precedes_lock_journal_and_interrupted_recovery(
        updater, monkeypatch, tmp_path, capsys, role, facts, interrupted):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "local"}
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    recovery = _deployed_recovery(monkeypatch, tmp_path)
    selected = mcs_setup._service_subs(cfg)["PYTHON"]
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe:
                        facts if exe == (selected if role == "selected" else recovery)
                        else _facts())
    if interrupted:
        state = updater._default_state()
        state["stages"] = [{"stage": "local_checks", "at": 1.0}]
        updater.save_state(state)
    for name in ("acquire_update_lock", "journal", "save_state", "_git",
                 "_enqueue_notice", "quiesce", "recover_interrupted"):
        monkeypatch.setattr(updater, name,
                            lambda *a, **k: pytest.fail("runtime gate allowed mutation"))
    before = _tree(tmp_path)
    assert updater.apply("v1.2.0", "a" * 40, None) == 1
    assert _tree(tmp_path) == before
    assert role + "_runtime:" in capsys.readouterr().out


def test_installer_explicit_template_probe_never_uses_deployed_or_parent_python(
        monkeypatch, tmp_path):
    _deployed_recovery(monkeypatch, tmp_path)
    template = tmp_path / "template.plist"
    template.write_bytes(plistlib.dumps({
        "Program": "/synthetic/template-python",
        "ProgramArguments": ["/synthetic/unused-python", "recovery.py"]}))
    probes = []
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: probes.append(exe) or _facts("3.9.6", "3.51.3"))
    assert mcs_setup._runtime_problem(mcs_setup._recovery_runtime(template), recovery=True) is None
    assert probes == ["/synthetic/template-python"]


def test_explicit_recovery_init_persists_private_selection(monkeypatch, tmp_path):
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "synthetic", "notify_target": "local"})
    selected = str(tmp_path / "independent-python")
    probes = []
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: probes.append(exe) or _facts("3.9.6"))
    monkeypatch.setattr(sys, "argv", ["mcs_setup", "init", "--yes",
                                    "--recovery-python", selected])
    assert mcs_setup.main() == 0
    path = tmp_path / "c.json"
    assert json.loads(path.read_text())["recovery_python"] == selected
    assert path.stat().st_mode & 0o777 == 0o600
    assert probes == [selected]


@pytest.mark.parametrize("facts", [None, _facts("3.8.10"), _facts(sqlite="3.51.0")])
def test_bad_desired_init_does_not_archive_or_write(monkeypatch, tmp_path, facts):
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, {})
    (tmp_path / "c.json").write_text("broken")
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: facts)
    monkeypatch.setattr(sys, "argv", ["mcs_setup", "init", "--yes",
                                    "--recovery-python", str(tmp_path / "independent-python")])
    before = _tree(tmp_path)
    assert mcs_setup.main() == 1
    assert _tree(tmp_path) == before


def test_recovery_rejects_relative_checkout_and_update_tree_paths(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    update = tmp_path / "update"
    repo.mkdir()
    update.mkdir()
    alias = tmp_path / "outside-alias"
    alias.symlink_to(repo / "python")
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", repo)
    for path in ("relative", str(repo / "python"), str(alias), str(update / "python")):
        assert mcs_setup._recovery_python_problem(path, update)
    assert mcs_setup._recovery_python({}) == "/usr/bin/python3"
    assert mcs_setup._recovery_python_problem(str(tmp_path / "independent-python"), update) is None


def _owned_recovery(monkeypatch, tmp_path):
    root = tmp_path.resolve()
    recovery = root / "independent-recovery"
    recovery.mkdir(mode=0o700)
    agents = root / "agents"
    agents.mkdir(exist_ok=True)
    data = root / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    data.chmod(0o700)
    tool = recovery / "mcs_recover.py"
    tool.write_text("# fully synthetic old recovery tool\n")
    tool.chmod(0o700)
    pointer = recovery / "repo_path"
    pointer.write_text(str(Path(mcs_setup.REPO_ROOT).resolve()) + "\n")
    pointer.chmod(0o600)
    old = "/synthetic/old-python"
    path = agents / "org.mcs.recovery.plist"
    path.write_bytes(plistlib.dumps({
        "Label": mcs_setup.RECOVERY_LABEL,
        "ProgramArguments": [old, str(tool), "--if-stale"]}))
    path.chmod(0o600)
    monkeypatch.setattr(mcs_setup, "HOME", str(root))
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(recovery))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    return path, old


@pytest.mark.parametrize("loaded", [False, True])
def test_services_repairs_owned_unsafe_selection_without_autoenabling(
        monkeypatch, tmp_path, loaded):
    from test_mcs_setup import _services_env
    real_uid = os.getuid()
    _, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(os, "getuid", lambda: real_uid)
    path, old = _owned_recovery(monkeypatch, tmp_path)
    desired = str(tmp_path / "independent-python")
    cfg = {"recovery_python": desired}
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: cfg if path is None else {})
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: _facts(sqlite="3.51.0") if exe == old else _facts())
    state = {"loaded": loaded}
    calls = []
    other_run = mcs_setup._run
    def run(argv, **kwargs):
        if argv[0] != "launchctl":
            return other_run(argv, **kwargs)
        calls.append(argv)
        if argv[1] == "print":
            return SimpleNamespace(returncode=0 if state["loaded"] else 113,
                                   stdout="state = not running\n")
        if argv[1] == "bootout":
            state["loaded"] = False
        if argv[1] == "bootstrap":
            state["loaded"] = True
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(mcs_setup, "_run", run)
    for name in ("_retire_previous_runtime", "_sync_scripts", "_sync_agents",
                 "_sync_standalone_service"):
        monkeypatch.setattr(mcs_setup, name, lambda *a, **kw: 0)
    assert mcs_setup._recovery_owned()
    assert mcs_setup.cmd_services(args) == 0
    actual = plistlib.loads(path.read_bytes())
    assert actual["ProgramArguments"] == [
        desired, str(Path(mcs_setup.RECOVERY_DIR, "mcs_recover.py")), "--if-stale"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert state["loaded"] is loaded
    assert sum(call[1] == "bootstrap" for call in calls) == int(loaded)
    assert mcs_setup.runtime_gate_errors(cfg) == []


@pytest.mark.parametrize("boundary", ["foreign", "active", "unknown"])
def test_owned_recovery_repair_rejects_unknown_or_active_before_writes(
        monkeypatch, tmp_path, boundary):
    path, _ = _owned_recovery(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    if boundary == "foreign":
        path.chmod(0o644)
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout="pid = 123\n" if boundary == "active" else "state = unknown\n"))
    before = _tree(tmp_path)
    assert mcs_setup._sync_recovery(
        {"recovery_python": str(tmp_path / "independent-python")}, lambda text: None, False) == 1
    assert _tree(tmp_path) == before


def test_doctor_and_updater_do_not_mask_deployed_drift(
        updater, monkeypatch, tmp_path, capsys):
    path, old = _owned_recovery(monkeypatch, tmp_path)
    desired = str(tmp_path / "independent-python")
    cfg = {"recovery_python": desired, "mcs_login_id": "synthetic", "notify_target": "local"}
    monkeypatch.setattr(mcs_setup, "load_config", lambda: cfg)
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: _facts(sqlite="3.51.0") if exe == old else _facts())
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **kw: pytest.fail("external action"))
    assert mcs_setup.cmd_doctor(SimpleNamespace(json=True)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["runtimes"]["desired_recovery"]["sqlite"] == "3.51.3"
    assert report["runtimes"]["recovery"]["sqlite"] == "3.51.0"
    assert report["recovery_selection"]["matches_deployed"] is False
    assert report["checks"]["recovery_runtime"]["status"] == "blocked"
    # owned idle drift is repaired after merge (supported update paths);
    # a watchdog services cannot prove it owns still blocks before writes
    path.chmod(0o644)
    before = _tree(tmp_path)
    assert updater.apply("v1.2.0", "a" * 40, None) == 1
    assert _tree(tmp_path) == before
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][0] == old


@pytest.mark.parametrize("facts", [None, _facts("3.8.10"), _facts(sqlite="3.51.0")])
def test_bad_desired_service_selection_blocks_before_any_write(monkeypatch, tmp_path, facts):
    from test_mcs_setup import _services_env
    calls, args = _services_env(monkeypatch, tmp_path)
    desired = str(tmp_path / "independent-python")
    cfg = {"recovery_python": desired}
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: cfg if path is None else {})
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: facts if exe == desired else _facts())
    before = _tree(tmp_path)
    assert mcs_setup.cmd_services(args) == 1
    assert _tree(tmp_path) == before and calls == []


def test_updater_retains_valid_explicit_deployed_selection(updater, monkeypatch, tmp_path):
    recovery = _deployed_recovery(monkeypatch, tmp_path)
    cfg = {"recovery_python": recovery, "mcs_login_id": "synthetic", "notify_target": "local"}
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "synthetic-hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: True)
    probes = []
    monkeypatch.setattr(mcs_setup, "_runtime_probe",
                        lambda exe: probes.append(exe) or _facts())
    before = dict(cfg), _tree(tmp_path)
    assert updater.precheck_local(cfg) == []
    assert probes == [mcs_setup._service_subs(cfg)["PYTHON"], recovery, recovery]
    assert (cfg, _tree(tmp_path)) == before


@pytest.mark.parametrize("bad", [{"backup": {"enabled": True}}, {"watchdog_grace_s": "60"}])
def test_invalid_render_config_is_reported_not_raised(updater, monkeypatch, tmp_path, capsys, bad):
    cfg = {"mcs_login_id": "synthetic", "notify_target": "local", **bad}
    monkeypatch.setattr(updater, "load_config", lambda: cfg)
    monkeypatch.setattr(mcs_setup, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    errors = updater.precheck_local(cfg)
    assert any(e.startswith("config: ") and e.endswith("_config_invalid") for e in errors)
    errors, _ = mcs_setup._script_drift()
    assert errors and errors[0].startswith("config: ")
    before = _tree(tmp_path)
    assert updater.apply("v1.2.0", "a" * 40, None) == 1
    assert "nothing changed" in capsys.readouterr().out
    assert _tree(tmp_path) == before
    monkeypatch.setattr(mcs_setup.mcs_runtime, "mode", lambda c: "hermes")
    assert mcs_setup.cmd_services(SimpleNamespace(dry_run=False)) == 1
    assert "services: config: " in capsys.readouterr().out
    assert _tree(tmp_path) == before


def test_bad_recovery_init_stops_before_keychain_and_env(monkeypatch, tmp_path, capsys):
    from test_mcs_setup import _init_env
    _init_env(monkeypatch, tmp_path, {"mcs_login_id": "synthetic", "notify_target": "local"})
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "synthetic-password")
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda *a, **kw: pytest.fail("secret written before validation"))
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: _facts(sqlite="3.51.0"))
    monkeypatch.setattr(sys, "argv", ["mcs_setup", "init", "--yes",
                                    "--recovery-python", str(tmp_path / "independent-python")])
    assert mcs_setup.main() == 1
    assert not (tmp_path / ".env").exists() and not (tmp_path / "c.json").exists()
    assert "nothing written" in capsys.readouterr().out
