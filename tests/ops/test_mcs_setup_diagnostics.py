"""Synthetic mcs setup diagnostics contracts; shared helpers preserve isolated runtime state."""
import json
import time
import setup_config_snapshot
import setup_integration
import environment_diagnostics
import service_contract
import service_management
import runtime_inspection
import lifecycle_io
import mcs_config
from pathlib import Path
from types import SimpleNamespace
import pytest
import mcs_setup
import local_llm
import mcs_util
from ops_testkit import git_reads_or
from mcs_setup_testkit import _check_only as _check_only, _linux_probe as _linux_probe, _runtime_darwin as _runtime_darwin, _stub_py as _stub_py


@pytest.mark.parametrize("configured,env,probed", [
    (True, None, True), (False, "1", False), (None, "1", True), (None, None, False)])
def test_check_admission_probe_follows_config_then_env(monkeypatch, configured, env, probed):
    _linux_probe(monkeypatch)
    cfg = {"notify_target": "slack"}
    if configured is not None:
        cfg["local_llm"] = {"admission": configured}
    monkeypatch.setattr(mcs_util, "load_config", lambda *a, **k: cfg)
    if env is None:
        monkeypatch.delenv("MCS_LLM_ADMISSION", raising=False)
    else:
        monkeypatch.setenv("MCS_LLM_ADMISSION", env)
    monkeypatch.setattr(local_llm, "_broker", lambda: SimpleNamespace(routes={}, is_open=lambda: True))
    errors, _ = mcs_setup.check_environment(cfg)
    assert any("mcs.semantic, mcs.extract" in e and "local_llm.admission" in e for e in errors) is probed


def test_check_explicit_jev_mode_without_private_key_is_a_warning(monkeypatch):
    _linux_probe(monkeypatch)
    monkeypatch.setattr(mcs_util, "private_env_value", lambda *a, **k: None)
    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack", "semantic": {"mode": "shadow", "project_ids": [1]}})
    assert any("no_api_key" in w for w in warnings)
    assert not any("no_api_key" in e for e in errors)


def test_check_environment_resolves_hermes_binary(monkeypatch,
                                                  tmp_path):
    """check must resolve the hermes CLI the way mcs_util.hermes_exe
    does: config hermes_bin wins, else PATH, else the user-local
    fallback — an unresolvable binary is an error because notifications
    cannot be sent."""
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))

    # unresolvable on PATH and on disk -> error
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: None)
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: False)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("Hermesコマンドが見つかりません" in e for e in errors)

    # PATH hit -> clean
    monkeypatch.setattr(mcs_util.shutil, "which",
                        lambda *a: "/usr/bin/hermes")
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert not any("Hermesコマンド" in e for e in errors)

    # explicit hermes_bin is honored over PATH
    calls = []
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile",
                        lambda p: calls.append(p) or True)
    errors, _ = mcs_setup.check_environment(
        {"notify_target": "slack", "hermes_bin": "/opt/h/hermes"})
    assert not any("Hermesコマンド" in e for e in errors)
    assert calls[-1] == "/opt/h/hermes"

    # the profile pattern is validate_config's alone — `check` joins both
    # lists, so a second copy here printed and counted the blocker twice
    errors, _ = mcs_setup.check_environment(
        {"notify_bot_profile": "Bad Name!"})
    assert not any("notify_bot_profile" in e for e in errors)
    assert any("notify_bot_profile" in e for e in
               mcs_setup.validate_config({"notify_bot_profile": "Bad Name!"})[0])


def test_check_environment_detects_locked_keychain(monkeypatch):
    """An existing-but-locked Keychain entry must surface as a distinct
    error — 'locked' (unlock to recover) vs 'not found' (re-register)."""
    from types import SimpleNamespace

    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=36 if "-w" in argv else 0,
                               stdout="", stderr="")
    monkeypatch.setattr(lifecycle_io.subprocess, "run", fake_run)

    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("キーチェーンのロックや" in e.lower() for e in errors)

    # missing entry -> the re-register message, not the locked one
    def missing(argv, **kw):
        return SimpleNamespace(returncode=44, stdout="",
                               stderr="could not be found")
    monkeypatch.setattr(lifecycle_io.subprocess, "run", missing)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any(mcs_setup.KEYCHAIN_SERVICE in e and "見つかりません" in e for e in errors)
    assert not any("キーチェーンのロックや" in e for e in errors)


def test_check_environment_flags_installed_but_unloaded_agent(
        monkeypatch, tmp_path):
    """plist presence is not liveness — the 2026-09-28 outage had both
    extract drainers installed but unloaded while `check` stayed green.
    An installed-but-unloaded agent is an error; an unreachable GUI
    domain (headless session) is a warning, never a false error."""
    from types import SimpleNamespace
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(lifecycle_io.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        lambda *a, **k: SimpleNamespace(returncode=0))

    loaded = {"ai.mcs.extract-drainer"}
    monkeypatch.setattr(service_management, 'agent_loaded',
                        lambda label, **_service_kwargs: label in loaded)
    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    missing = [lb for lb in service_contract.AGENT_LABELS if lb not in loaded]
    for label in missing:
        assert any(label in e and "読み込まれていません" in e for e in errors)

    # GUI domain unreachable -> warn once, no per-agent errors
    monkeypatch.setattr(lifecycle_io, 'run_bounded',
                        lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(service_management, 'agent_loaded', lambda label, **_service_kwargs: False)
    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("読み込まれていません" in e for e in errors)
    assert any("接続できない" in w for w in warnings)


def test_queue_warnings_distinguish_stall_from_lag(monkeypatch,
                                                   tmp_path):
    """Backfill lag is steady-state (Jev budget cap) — warn only on a
    real stall (pending work but zero completions in 24h) or a queue
    that has been growing for days."""
    import time

    import ledger
    home = tmp_path / "home"
    (home / "data").mkdir(parents=True)
    monkeypatch.setattr(mcs_setup, "HOME", str(home))
    monkeypatch.setattr(setup_integration, "HOME", str(home))

    # no ledger at all -> silence, not noise
    assert mcs_setup._queue_warnings({"semantic": {"mode": "shadow"}}) \
        == []

    db = ledger.Ledger(str(home / "data" / "ledger.db"))
    try:
        now = time.time()
        cfg = {"semantic": {"mode": "shadow", "extract_qc": "annotate", "project_ids": [1]}}
        db.job_add("extract_qc", 1, 1, payload={"hash": "h"})
        assert not mcs_setup._queue_warnings(cfg)
        db.db.execute("UPDATE fetch_jobs SET updated_at=?,next_try=?",
                      (now - 2 * 86400, now - 2 * 86400))
        db.db.commit()
        assert not mcs_setup._queue_warnings({})
        assert not mcs_setup._queue_warnings({"semantic": {**cfg["semantic"], "project_ids": [2]}})
        assert any("進捗がありません" in w for w in mcs_setup._queue_warnings(cfg))
        db.db.execute("UPDATE fetch_jobs SET state='done',updated_at=?", (now,))
        db.db.commit()
        db.job_add("extract_qc", 1, 1)
        assert not mcs_setup._queue_warnings(cfg)
        db.db.execute("UPDATE fetch_jobs SET next_try=?", (now + 86400,))
        db.db.commit()
        assert not mcs_setup._queue_warnings(cfg)
    finally:
        db.close()


def test_queue_warnings_preserve_findings_when_later_query_fails(
        monkeypatch, tmp_path):
    import sqlite3

    (tmp_path / "data").mkdir()
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup.time, "time", lambda: 3 * 86400)
    db = sqlite3.connect(tmp_path / "data" / "ledger.db")
    try:
        db.execute("CREATE TABLE fetch_jobs(kind,state,project_id,next_try,updated_at)")
        db.execute("INSERT INTO fetch_jobs VALUES('semantic','pending',1,1,1)")
        db.commit()
    finally:
        db.close()

    warnings = mcs_setup._queue_warnings({
        "semantic": {"mode": "shadow", "project_ids": [1]}})
    assert len(warnings) == 2
    assert "進捗がありません" in warnings[0]
    assert warnings[1] == "待機処理の状態を読み取れません（OperationalError）"


@pytest.mark.parametrize("name", ["runtime#literal", "runtime?literal", "runtime%23literal"])
def test_queue_warning_uses_the_exact_database_read_only(monkeypatch, tmp_path, name):
    import sqlite3

    import ledger

    home = tmp_path / name
    (home / "data").mkdir(parents=True)
    db = ledger.Ledger(str(home / "data" / "ledger.db"))
    try:
        db.job_add("extract_qc", 1, 1)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET updated_at=1,next_try=1")
    finally:
        db.close()
    monkeypatch.setattr(mcs_setup, "HOME", str(home))
    monkeypatch.setattr(setup_integration, "HOME", str(home))
    monkeypatch.setattr(mcs_setup.time, "time", lambda: 3 * 86400)
    connect = sqlite3.connect

    def read_only_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE synthetic_write_attempt(value)")
        return connection

    monkeypatch.setattr(sqlite3, "connect", read_only_connect)
    warnings = mcs_setup._queue_warnings({
        "semantic": {"extract_qc": "annotate", "project_ids": [1]}})
    assert len(warnings) == 1 and "extract_qc: 待機1件" in warnings[0]
    assert "進捗がありません" in warnings[0]
    assert not (tmp_path / "runtime").exists()


def test_process_start_is_parsed_in_the_c_locale(monkeypatch):
    """`ps -o lstart=` follows LC_ALL/LANG (ja_JP prints a weekday in
    kanji); both the check warning and doctor must still parse it."""
    import time
    stamp = "Sun Oct 11 16:24:47 2026"

    def ps(argv, **kw):
        if argv[0] == "launchctl":
            return SimpleNamespace(returncode=0, stdout="pid = 123\n")
        c_locale = (kw.get("env") or {}).get("LC_ALL") == "C"
        return SimpleNamespace(returncode=0, stdout=stamp if c_locale else "\u65e5 10/11 16:24:47 2026")
    monkeypatch.setenv("LC_ALL", "ja_JP.UTF-8")
    monkeypatch.setattr(lifecycle_io, 'run_bounded', ps)
    started = time.mktime(time.strptime(stamp, "%a %b %d %H:%M:%S %Y"))
    monkeypatch.setattr(environment_diagnostics, "gateway_sources_mtime", lambda checkout: started + 60)
    assert environment_diagnostics.plugin_newer_than_gateway("PID 123 running", checkout=mcs_setup.REPO_ROOT) is True
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(lifecycle_io.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    gateway = environment_diagnostics.source_status({}, probe=True, paths=mcs_setup._service_paths())["gateway"]
    assert gateway["started_at"] == started and gateway["restart_required"] is True


def test_check_environment_locked_keychain_with_env_fallback(monkeypatch):
    """With MCS_PASSWORD in .env, a locked keychain is a warning, not a
    hard error — the reboot-fallback keeps re-login unmanned."""
    from types import SimpleNamespace

    monkeypatch.setattr(environment_diagnostics.sys, "platform", "darwin")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(environment_diagnostics, "env_value",
                        lambda *a, **k: "env_pw")
    monkeypatch.setattr(
        lifecycle_io.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=36 if "-w" in argv else 0, stdout="", stderr=""))

    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("キーチェーンのロックや" in e for e in errors)
    assert any("キーチェーンのロックや" in w.lower() for w in warnings)
    assert any("予備の認証情報" in w for w in warnings)


def test_invalid_runtime_doctor_fails_without_service_probes(monkeypatch, capsys):
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup, '_diagnostic_config', lambda: {"runtime_mode": ["standalone"]})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda cfg: [])
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda cfg: pytest.fail("invalid mode must not probe Hermes"))
    monkeypatch.setattr(lifecycle_io, 'run_bounded', git_reads_or(lambda *args, **kwargs: pytest.fail("invalid mode must not probe services")))
    assert mcs_setup.cmd_doctor(SimpleNamespace()) == 1
    assert "常駐処理の実行環境: 要修正" in capsys.readouterr().out


def test_check_warns_when_gateway_unsupervised(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_util.shutil, "which", lambda *a: "/x/h")
    monkeypatch.setattr(environment_diagnostics.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(environment_diagnostics.os, "access", lambda p, m: True)

    def fake_cli(exe, profile, *argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(service_management, 'hermes_cli', fake_cli)
    monkeypatch.setattr(local_llm, "bounded_request",
                        lambda *a, **k: (200, {}, b"{}"))
    _, warnings = mcs_setup.check_environment(
        {"mcs_login_id": "u", "notify_target": "discord:1",
         "notify": {"interactive": "discord"}})
    assert any("gateway" in w for w in warnings)


def test_stale_transport_scope_warns(monkeypatch):
    """A scope block for the non-active transport is flagged — a
    discord->slack switch must not silently leave dead config."""
    _e, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack:#m",
        "notify": {"interactive": "slack",
                   "slack": {"profile": "p", "application_id": "a",
                              "team_id": "t", "channel_id": "c"},
                   "discord": {"profile": "p", "application_id": "a",
                               "guild_id": "g", "channel_id": "c"}}})
    assert any("notify.discord" in w and "使いません" in w
               for w in warnings)


def test_check_reports_deployed_script_drift(monkeypatch, tmp_path, capsys):
    """A checkout update without a `services` rerun left stale cron
    wrappers running (2026-09: old mcs_check.sh night filter gapped
    unread polling past the session limit). `check` compares the
    deployed wrappers against the exact `services` render."""
    src = tmp_path / "repo" / "deployment" / "scripts"
    src.mkdir(parents=True)
    (src / "a.sh").write_text("#!/bin/sh\nexec __PYTHON__ __REPO__/x\n")
    (src / "b.sh").write_text("#!/bin/sh\necho __DATA__\n")
    (src / "README.md").write_text("not a script")
    helper = tmp_path / "repo" / "mcs" / "core" / "run_log.py"
    helper.parent.mkdir(parents=True)
    helper.write_bytes((Path(__file__).resolve().parents[2] / "mcs" / "core" / "run_log.py").read_bytes())
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setattr(setup_integration, "REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/h/venv/bin/python")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path / "home dir"))
    monkeypatch.setattr(setup_integration, "HOME", str(tmp_path / "home dir"))

    # nothing deployed yet (fresh install) -> warning, not an error
    errors, warnings = service_management.script_drift(paths=mcs_setup._service_paths())
    assert errors == []
    assert any("a.sh, b.sh" in w and "services" in w for w in warnings)

    # the services render is by definition in sync
    service_management.sync_scripts(service_management.service_substitutions(mcs_util.load_config(), paths=mcs_setup._service_paths()), {'scripts': []}, lambda m: None, dry=False, paths=mcs_setup._service_paths())
    assert "'" in (tmp_path / "scripts" / "b.sh").read_text()  # quoted
    assert service_management.script_drift(paths=mcs_setup._service_paths()) == ([], [])

    # repo template changed, deployed copy not resynced -> named error
    (src / "a.sh").write_text("#!/bin/sh\nexec __PYTHON__ __REPO__/y\n")
    errors, warnings = service_management.script_drift(paths=mcs_setup._service_paths())
    assert warnings == []
    assert len(errors) == 1 and "a.sh" in errors[0] \
        and "b.sh" not in errors[0] and "mcs_setup.py services" in errors[0]

    _previous_service_load_config_1976 = mcs_util.load_config
    monkeypatch.setattr(mcs_util, 'load_config',
                        lambda path=None: _previous_service_load_config_1976(path) if path else {})
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    monkeypatch.setattr(mcs_setup, "check_environment",
                        lambda cfg: ([], []))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda cfg: [])
    monkeypatch.setattr(service_management, 'check_runtime', lambda cfg, **_service_kwargs: ([], []))
    assert mcs_setup.cmd_check(None) == 1
    assert "a.sh" in capsys.readouterr().out


def test_check_flags_invalid_config_file(monkeypatch, tmp_path, capsys):
    _check_only(monkeypatch)
    conf = tmp_path / "c.json"
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(conf))
    assert mcs_setup.cmd_check(None) == 0      # absent = fresh, not broken
    conf.write_text("{")
    assert mcs_setup.cmd_check(None) == 1
    assert "読み取れないか、形式が不正" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ['{"a": 1, "a": 2}', '{"local_llm": {"disk_guard_mb": NaN}}'])
def test_check_flags_duplicate_key_and_nan_like_every_reader(monkeypatch, tmp_path, capsys, raw):
    _check_only(monkeypatch)
    conf = tmp_path / "c.json"
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    monkeypatch.setattr(setup_config_snapshot, "CONF_PATH", str(conf))
    conf.write_text(raw)
    assert mcs_setup.cmd_check(None) == 1
    assert "読み取れないか、形式が不正" in capsys.readouterr().out
    assert mcs_util.load_config(str(conf)) == {}


def test_check_prints_prioritized_blocker_summary(monkeypatch, capsys):
    """Blockers end the output in priority order (runtime before config),
    each with a one-line fix."""
    _check_only(monkeypatch,
                runtime=(["Python（/h/py）が見つかりません。"
                          "`/r/install.sh`を再実行してください（手順2）"], []),
                config=(["必須の設定項目がありません: mcs_login_id"],
                        ["未対応の設定項目: x"]))
    assert mcs_setup.cmd_check(None) == 1
    out = capsys.readouterr().out
    summary = out[out.index("動作を妨げる問題が2件"):]
    assert summary.index("Python（/h/py）が見つかりません") \
        < summary.index("必須の設定項目がありません")
    assert "対処: /r/install.sh" in summary
    assert "対処: mcs_setup.py init" in summary
    assert out.rstrip().endswith("設定確認: 要修正 （問題2件、注意1件）")


def test_check_ok_prints_no_blocker_summary(monkeypatch, capsys):
    _check_only(monkeypatch)
    assert mcs_setup.cmd_check(None) == 0
    assert "動作を妨げる問題" not in capsys.readouterr().out


def test_doctor_is_local_and_does_not_run_check(monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, '_diagnostic_config',
                        lambda: {"mcs_login_id": "synthetic", "notify_target": "local"})
    monkeypatch.setattr(mcs_setup, "_config_problem", lambda: None)
    monkeypatch.setattr(runtime_inspection, "probe_python", lambda exe: {
        "python": "3.14.8", "sqlite": "3.51.3",
        "packages": {"discord.py": None, "slack-bolt": None, "slack-sdk": None}})
    monkeypatch.setattr(mcs_setup.sqlite3, "sqlite_version_info", (3, 51, 3))
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: pytest.fail("not local"))
    assert mcs_setup.cmd_doctor(SimpleNamespace(json=True)) == 0
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert checks["runtime"]["status"] == "healthy"
    assert checks["services"]["status"] == "not_checked"


def test_check_runtime_verifies_services_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    cfg = {"hermes_bin": _stub_py(tmp_path / "hermes", 0)}
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(tmp_path / "absent"))
    errors, _ = service_management.check_runtime(cfg, paths=mcs_setup._service_paths())
    assert any("absent）が見つからない" in e and "install.sh" in e
               for e in errors)
    monkeypatch.setattr(mcs_setup, "HERMES_PY",
                        _stub_py(tmp_path / "old-py", 1))
    errors, _ = service_management.check_runtime(cfg, paths=mcs_setup._service_paths())
    assert any("起動できないか、3.14未満" in e for e in errors)
    monkeypatch.setattr(mcs_setup, "HERMES_PY",
                        _stub_py(tmp_path / "py", 0))
    assert service_management.check_runtime(cfg, paths=mcs_setup._service_paths()) == ([], [])


def test_check_runtime_hermes_must_resolve_on_launchd_path(monkeypatch,
                                                          tmp_path):
    """hermes found only via the login shell PATH (e.g. Homebrew) is
    invisible to launchd/cron wrappers — an error with the exact fix."""
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(service_management, 'hermes_py_problem', lambda**_service_kwargs: None)
    shell_only = "/opt/homebrew/bin/hermes"
    monkeypatch.setattr(mcs_util.shutil, "which",
                        lambda cmd, mode=None, path=None:
                        shell_only if path is None else None)
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: exe == shell_only)
    errors, _ = service_management.check_runtime({}, paths=mcs_setup._service_paths())
    assert any("常駐処理の検索先" in e
               and f"ln -s {shell_only} ~/.local/bin/hermes" in e
               for e in errors)
    # an explicit hermes_bin is what every job uses -> fine
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda exe: True)
    assert service_management.check_runtime({'hermes_bin': shell_only}, paths=mcs_setup._service_paths()) == ([], [])


def test_check_runtime_nothing_installed_only_warns(monkeypatch, tmp_path):
    _runtime_darwin(monkeypatch, tmp_path)
    errors, warnings = service_management.check_runtime({'hermes_bin': '/x'}, paths=mcs_setup._service_paths())
    assert errors == []
    assert any("org.mcs.recovery" in w and "未導入" in w
               for w in warnings)
    assert any("ローカルAIの常駐設定がありません" in w for w in warnings)


def test_check_runtime_recovery_watchdog(monkeypatch, tmp_path):
    repo, loaded = _runtime_darwin(
        monkeypatch, tmp_path,
        loaded={"org.mcs.recovery", "ai.mcs.llamaserver"})
    rec = tmp_path / "recovery"
    (rec / "mcs_recover.py").write_text("v1")
    (rec / "repo_path").write_text(f"{repo}\n")
    import plistlib
    (tmp_path / "agents" / "org.mcs.recovery.plist").write_bytes(
        plistlib.dumps({"ProgramArguments": ["/synthetic/recovery-python", "watchdog.py"]}))
    assert service_management.check_runtime({'hermes_bin': '/x'}, paths=mcs_setup._service_paths()) == ([], [])

    (rec / "mcs_recover.py").write_text("v0")
    (rec / "repo_path").write_text("/elsewhere\n")
    loaded.discard("org.mcs.recovery")
    errors, warnings = service_management.check_runtime({'hermes_bin': '/x'}, paths=mcs_setup._service_paths())
    assert any("配布版と異なります" in w for w in warnings)
    assert any("対象は/elsewhere" in w for w in warnings)
    assert any("org.mcs.recoveryは導入済みですが読み込まれていません" in e
               and "launchctl bootstrap" in e for e in errors)


def test_check_runtime_llama_agent_installed_but_unloaded(monkeypatch,
                                                         tmp_path):
    _runtime_darwin(monkeypatch, tmp_path)
    (tmp_path / "agents" / "ai.mcs.llamaserver.plist").write_text("x")
    errors, _ = service_management.check_runtime({'hermes_bin': '/x'}, paths=mcs_setup._service_paths())
    assert any("ai.mcs.llamaserverは導入済みですが読み込まれておらず" in e
               for e in errors)


@pytest.mark.parametrize("raw", [b'{"a":0,"b":1,"c":2}', b'null', b'[' * 2000 + b']' * 2000], ids=["object", "null", "deep"])
def test_check_rejects_unreadable_slots_and_uses_bounded_probe(monkeypatch, raw):
    import local_llm
    monkeypatch.setattr(environment_diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(environment_diagnostics.os.path, "exists", lambda p: True)
    monkeypatch.setattr(runtime_inspection, "executable_available", lambda e: True)
    calls = []
    def request(url, method, body, timeout):
        calls.append((url, method, body, timeout))
        return 200, {}, raw if url.endswith("/slots") else b'{}'
    monkeypatch.setattr(local_llm, "bounded_request", request)
    _, warnings = mcs_setup.check_environment({"local_llm": {"url": "http://127.0.0.1:8089/v1/chat/completions"}})
    assert calls == [("http://127.0.0.1:8089/v1/models", "GET", None, 3),
                     ("http://127.0.0.1:8089/slots", "GET", None, 3)]
    assert any("同時処理枠を読み取れない" in w for w in warnings)


@pytest.mark.parametrize("raw", [b'\xff', b'[' * 2000 + b']' * 2000], ids=["encoding", "deep"])
def test_malformed_service_manifest_is_recoverable(monkeypatch, tmp_path, raw):
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    monkeypatch.setattr(mcs_setup, "MANIFEST_PATH", str(path))
    assert service_management.load_manifest(paths=mcs_setup._service_paths()) == {}


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
def test_check_warns_on_a_static_project_scope_over_the_runner_cap(transport):
    import notify_cmds
    assert mcs_config.PROJECT_SCOPE_MAX == notify_cmds.SCOPE_MAX
    ids = list(range(1, mcs_config.PROJECT_SCOPE_MAX + 2))
    cfg = {"notify": {transport: {"project_ids": ids}}}
    warnings = [w for w in mcs_setup.validate_config(cfg)[1] if "project_ids" in w]
    assert len(warnings) == 1 and f"notify.{transport}.project_ids" in warnings[0]
    assert all("project_ids" not in e or "positive" in e for e in mcs_setup.validate_config(cfg)[0])
    cfg["notify"][transport]["project_ids_auto"] = True
    assert not [w for w in mcs_setup.validate_config(cfg)[1] if "project_ids" in w]
    cfg["notify"][transport] = {"project_ids": ids[:-1]}
    assert not [w for w in mcs_setup.validate_config(cfg)[1] if "project_ids" in w]


def _install_progress_fixture(monkeypatch, tmp_path, record):
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if record is not None:
        (data / "install_progress.json").write_text(json.dumps(record))


def _install_progress_record(**overrides):
    record = {
        "version": 1, "status": "running", "stage": "services",
        "runtime_mode": "hermes", "invocation": "install",
        "run_id": "1-2", "updated_at": 1,
    }
    record.update(overrides)
    return record


def test_install_progress_warning_silent_without_running_record(monkeypatch, tmp_path):
    _install_progress_fixture(monkeypatch, tmp_path, None)
    assert mcs_setup._install_progress_warning() is None
    _install_progress_fixture(monkeypatch, tmp_path, _install_progress_record(
        status="complete", stage="done"))
    assert mcs_setup._install_progress_warning() is None


def test_install_progress_warning_reports_leftover_stage_without_time_claim(
        monkeypatch, tmp_path):
    """A leftover running record warns honestly — never asserts interruption
    or in-progress from the timestamp alone."""
    for updated in (1, time.time()):
        _install_progress_fixture(monkeypatch, tmp_path, _install_progress_record(
            stage="services", updated_at=updated))
        warning = mcs_setup._install_progress_warning()
        assert warning is not None
        assert "常駐サービス" in warning and "残っています" in warning
        assert "完了は記録されていません" in warning
        assert "install.sh" in warning
        assert "中断" not in warning and "実行中です" not in warning


def test_install_progress_warning_update_invocation_points_to_recover(
        monkeypatch, tmp_path):
    _install_progress_fixture(monkeypatch, tmp_path, _install_progress_record(
        stage="recovery", invocation="update", updated_at=time.time()))
    warning = mcs_setup._install_progress_warning()
    assert warning is not None and "mcs recover" in warning and "更新処理" in warning
    assert "残っています" in warning and "中断" not in warning


@pytest.mark.parametrize("record", [
    {"status": "running"},                                   # missing fields
    _install_progress_record(stage=[]),                      # non-string stage
    _install_progress_record(stage={}),                      # non-string stage
    _install_progress_record(stage="bogus_stage"),           # unknown stage
    _install_progress_record(status="running", stage="done"),  # inconsistent
    _install_progress_record(status="complete", stage="services"),
    _install_progress_record(version=2),                     # unknown version
    _install_progress_record(invocation="sideways"),
    _install_progress_record(run_id=""),
    _install_progress_record(run_id=None),
    _install_progress_record(updated_at="soon"),
    _install_progress_record(updated_at=True),
    [], "not a dict",
])
def test_install_progress_warning_unreadable_shape_warns_fixed(
        monkeypatch, tmp_path, record):
    """A present but unverifiable record gets one fixed warning — the
    check itself never crashes and never echoes record contents."""
    _install_progress_fixture(monkeypatch, tmp_path, record)
    warning = mcs_setup._install_progress_warning()
    assert warning == ("導入の途中経過の記録を確認できませんでした。"
                       "install.sh の状態を確認してください")


def test_install_progress_warning_swallows_unreadable_file(monkeypatch, tmp_path):
    _install_progress_fixture(monkeypatch, tmp_path, None)
    journal = tmp_path / "data" / "install_progress.json"
    journal.write_text("{broken json")
    assert mcs_setup._install_progress_warning() == (
        "導入の途中経過の記録を確認できませんでした。install.sh の状態を確認してください")
    journal.unlink()
    journal.symlink_to(tmp_path / "data" / "config.json")
    (tmp_path / "data" / "config.json").write_text("{}")
    assert mcs_setup._install_progress_warning() == (
        "導入の途中経過の記録を確認できませんでした。install.sh の状態を確認してください")
