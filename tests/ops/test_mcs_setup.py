"""mcs_setup.validate_config — the typesafe required-condition gate."""
from pathlib import Path

import pytest

import mcs_setup
import local_llm
import mcs_util


def test_missing_required_keys():
    errors, warnings = mcs_setup.validate_config({})
    assert "missing required key: mcs_login_id" in errors
    assert "missing required key: notify_target" in errors


def test_minimal_valid_config():
    errors, _ = mcs_setup.validate_config(
        {"mcs_login_id": "u1", "notify_target": "slack:#mcs"})
    assert errors == []


def test_daily_digest_block_is_typed():
    base = {"mcs_login_id": "u1", "notify_target": "slack:#mcs"}
    errors, warnings = mcs_setup.validate_config(
        {**base, "daily_digest": {"enabled": True, "hour_jst": 8}})
    assert errors == [] and warnings == []
    errors, _ = mcs_setup.validate_config(
        {**base, "daily_digest": {"enabled": "yes", "hour_jst": 24}})
    assert errors == ["daily_digest.enabled: must be a boolean",
                      "daily_digest.hour_jst: must be an integer in [0,23]"]


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 10 ** 400])
def test_unbounded_numeric_config_is_rejected(value):
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "synthetic", "notify_target": "local",
        "job_budget_seconds": value, "notify_max_age_h": value,
        "signals": {"digest_interval_h": value},
        "update": {"auto_delay_h": value},
    })
    for key in ("job_budget_seconds", "notify_max_age_h",
                "digest_interval_h", "auto_delay_h"):
        assert any(key in error for error in errors)


@pytest.mark.parametrize("value", ["inf", "nan", "1e999"])
def test_numeric_wizard_rejects_nonfinite_without_crashing(value):
    assert mcs_setup._parse_answer("num", value)[0] is False


def test_plugin_csv_list_escapes_literal_values():
    import json
    text = 'user"quote,user\\slash'
    assert json.loads(mcs_setup._csv_yaml(text)) == text.split(",")


def test_type_violations():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "",                    # empty -> error
        "notify_target": "   ",                # blank -> error
        "discover_archived": "yes",            # str not bool
        "trickle_pages": 0,                    # below range
        "signals": {"notify": "on"},           # str not bool
        "notify_bot_profile": "Bad Name",      # pattern mismatch
    })
    assert any("mcs_login_id" in e for e in errors)
    assert any("notify_target" in e for e in errors)
    assert any("discover_archived" in e for e in errors)
    assert any("trickle_pages" in e for e in errors)
    assert any("signals.notify" in e for e in errors)
    assert any("notify_bot_profile" in e for e in errors)


def test_bool_strictness_int_is_not_bool():
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "deep_history": 1})                    # int 1 is not bool True
    assert any("deep_history" in e for e in errors)


def test_unknown_keys_warn_not_fail():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "future_key": True})
    assert errors == []
    assert warnings == ["unknown config key: future_key"]


def test_semantic_block_delegates_to_production_validator():
    # mode enabled without project_ids -> semantic_config's own error
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": {"mode": "shadow"}})
    assert any("semantic_project_scope_required" in e for e in errors)
    # malformed semantic block
    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "semantic": "enabled"})
    assert any("semantic" in e for e in errors)


def test_health_block_validates_types_and_ranges():
    errors, warnings = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": 300, "max_missed_runs": 4}})
    assert errors == []
    assert not any("health" in w for w in warnings)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": 0, "max_missed_runs": 4.5}})
    assert any("health.tick_interval_s" in e for e in errors)
    assert any("health.max_missed_runs" in e for e in errors)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": {"tick_interval_s": float("nan"),
                   "max_missed_runs": -1}})
    assert any("health.tick_interval_s" in e for e in errors)
    assert any("health.max_missed_runs" in e for e in errors)

    errors, _ = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "health": "enabled"})
    assert any("health" in e for e in errors)


def test_env_write_merges_and_preserves(tmp_path):
    p = tmp_path / ".env"
    p.write_text("KEEP=1\nSAMPLE_TOKEN=old\n", encoding="utf-8")
    mcs_setup._env_write(str(p), {"SAMPLE_TOKEN": "new",
                                 "TYPESAFE_API_KEY": "k2"})
    text = p.read_text(encoding="utf-8")
    assert "KEEP=1" in text
    assert "SAMPLE_TOKEN=new" in text and "old" not in text
    assert "TYPESAFE_API_KEY=k2" in text
    assert oct(p.stat().st_mode & 0o777) == "0o600"


def test_check_environment_resolves_hermes_binary(monkeypatch,
                                                  tmp_path):
    """check must resolve the hermes CLI the way notify_flush._hermes_exe
    does: config hermes_bin wins, else PATH, else the user-local
    fallback — an unresolvable binary is an error because notifications
    cannot be sent."""
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))

    # unresolvable on PATH and on disk -> error
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: None)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("hermes CLI not resolvable" in e for e in errors)

    # PATH hit -> clean
    monkeypatch.setattr(mcs_setup.shutil, "which",
                        lambda *a: "/usr/bin/hermes")
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert not any("hermes CLI" in e for e in errors)

    # explicit hermes_bin is honored over PATH
    calls = []
    monkeypatch.setattr(mcs_setup.os.path, "isfile",
                        lambda p: calls.append(p) or True)
    errors, _ = mcs_setup.check_environment(
        {"notify_target": "slack", "hermes_bin": "/opt/h/hermes"})
    assert not any("hermes CLI" in e for e in errors)
    assert calls[-1] == "/opt/h/hermes"

    # invalid profile pattern still an error
    errors, _ = mcs_setup.check_environment(
        {"notify_bot_profile": "Bad Name!"})
    assert any("notify_bot_profile" in e for e in errors)


def test_check_environment_detects_locked_keychain(monkeypatch):
    """An existing-but-locked Keychain entry must surface as a distinct
    error — 'locked' (unlock to recover) vs 'not found' (re-register)."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))

    def fake_run(argv, **kw):
        return SimpleNamespace(returncode=36 if "-w" in argv else 0,
                               stdout="", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)

    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("keychain is locked" in e.lower() for e in errors)

    # missing entry -> the re-register message, not the locked one
    def missing(argv, **kw):
        return SimpleNamespace(returncode=44, stdout="",
                               stderr="could not be found")
    monkeypatch.setattr(mcs_setup.subprocess, "run", missing)
    errors, _ = mcs_setup.check_environment({"notify_target": "slack"})
    assert any("not found" in e for e in errors)
    assert not any("keychain is locked" in e.lower() for e in errors)


def test_check_environment_flags_installed_but_unloaded_agent(
        monkeypatch, tmp_path):
    """plist presence is not liveness — the 2026-09-28 outage had both
    extract drainers installed but unloaded while `check` stayed green.
    An installed-but-unloaded agent is an error; an unreachable GUI
    domain (headless session) is a warning, never a false error."""
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(mcs_setup.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(mcs_setup, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0))

    loaded = {"ai.mcs.extract-drainer"}
    monkeypatch.setattr(mcs_setup, "_agent_loaded",
                        lambda label: label in loaded)
    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    missing = [lb for lb in mcs_setup.AGENT_LABELS if lb not in loaded]
    for label in missing:
        assert any(label in e and "not loaded" in e for e in errors)

    # GUI domain unreachable -> warn once, no per-agent errors
    monkeypatch.setattr(mcs_setup, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=1))
    monkeypatch.setattr(mcs_setup, "_agent_loaded", lambda label: False)
    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("not loaded" in e for e in errors)
    assert any("unreachable" in w for w in warnings)


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
        assert any("stalled" in w for w in mcs_setup._queue_warnings(cfg))
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
    assert "stalled" in warnings[0]
    assert warnings[1] == "queue health unreadable (OperationalError)"


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
    assert len(warnings) == 1 and "extract_qc jobs pending=1" in warnings[0]
    assert "stalled" in warnings[0]
    assert not (tmp_path / "runtime").exists()


@pytest.mark.parametrize("source,needs_restart", [
    ("hermes_plugin/mcs_delivery/__init__.py", True),
    ("adapters/common/worker.py", True),
    ("adapters/slack/actions.py", True),
    ("adapters/discord/delivery.py", True),
    ("adapters/lineworks/actions.py", False),
])
def test_plugin_newer_ignores_pycache(monkeypatch, tmp_path, source, needs_restart):
    """The gateway regenerates __pycache__ at load — pyc mtimes are
    always newer than the process start. Only source mtimes may
    trigger the stale-plugin warning."""
    import os
    import time
    src = tmp_path / source
    plugin = src.parent
    cache = plugin / "__pycache__"
    cache.mkdir(parents=True)
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(tmp_path))

    class R:
        returncode = 0
        stdout = "Mon Sep 28 13:00:00 2026"
    started = time.mktime(time.strptime(R.stdout, "%a %b %d %H:%M:%S %Y"))
    monkeypatch.setattr(mcs_setup, "_run", lambda *a, **k: R())

    src.write_text("x = 1")
    pyc = cache / "worker.cpython-311.pyc"
    pyc.write_bytes(b"")
    os.utime(src, (started - 100, started - 100))     # loaded before start
    os.utime(pyc, (started + 100, started + 100))     # regenerated at load
    assert not mcs_setup._plugin_newer_than_gateway("PID 123 running")
    # a docs-only edit needs no restart
    doc = plugin / "README.md"
    doc.write_text("docs")
    os.utime(doc, (started + 100, started + 100))
    assert not mcs_setup._plugin_newer_than_gateway("PID 123 running")
    # Newer Hermes-owned source warns; the independent LINE worker does not.
    os.utime(src, (started + 100, started + 100))
    assert mcs_setup._plugin_newer_than_gateway("PID 123 running") is needs_restart


def test_keychain_store_sends_password_via_stdin_not_argv(monkeypatch):
    """FIX-SU1: the password must travel on `security -i` stdin and be
    verified by read-back — it must never appear in any child argv."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:               # read-back returns the secret
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is True
    assert not any("s3cret pw" in a for argv, _ in calls for a in argv)
    write = next(call for call in calls if call[0] == ["security", "-i"])
    assert "s3cret pw" in write[1]["input"]
    assert any("-w" in argv for argv, _ in calls)   # verify pass ran


def test_keychain_store_readback_mismatch_preserves_previous_entry(monkeypatch):
    """A failed replacement restores the existing credential through stdin."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append((list(argv), kw))
        if "-w" in argv:
            return R(out="different\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert not any("delete-generic-password" in argv for argv, _ in calls)
    assert calls[-1][0] == ["security", "-i"]
    assert '"different"' in calls[-1][1]["input"]


def test_keychain_store_verifies_adapter_read_path(monkeypatch):
    """The read-back must use the adapter's argv — service-only, no -a —
    so a shadowing stale entry under the same service fails loudly."""
    calls = []

    class R:
        def __init__(self, rc=0, out=""):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    def fake_run(argv, **kw):
        calls.append(list(argv))
        if argv[:2] == ["security", "-i"]:
            return R()
        if "-w" in argv:
            # adapter-style read (no -a) hits a stale shadow entry
            if "-a" not in argv:
                return R(out="stale-other-account\n")
            return R(out="s3cret pw\n")
        return R()

    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup._keychain_store("mcs", "s3cret pw") is False
    assert not any("delete-generic-password" in argv for argv in calls)
    assert any(a[:2] == ["security", "find-generic-password"]
               and "-w" in a and "-a" not in a for a in calls)


@pytest.mark.parametrize("secret", [' leading and trailing ', '"quoted"', r"slash\quote'"])
def test_env_credentials_roundtrip_exactly(tmp_path, secret):
    from mcs_util import env_value
    path = tmp_path / ".env"
    mcs_setup._env_write(str(path), {"SYNTHETIC_CREDENTIAL": secret})
    assert env_value("SYNTHETIC_CREDENTIAL", paths=[path], check_env=False) == secret


@pytest.mark.parametrize("secret", ["line\nOTHER=value", "line\rvalue", "nul\x00value"])
def test_control_characters_never_reach_credential_writers(tmp_path, monkeypatch, secret):
    path = tmp_path / ".env"
    path.write_text("KEEP=synthetic\n")
    with pytest.raises(ValueError, match="invalid_env_update"):
        mcs_setup._env_write(str(path), {"SYNTHETIC_CREDENTIAL": secret})
    assert path.read_text() == "KEEP=synthetic\n"
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k:
                        pytest.fail("credential command must not run"))
    assert mcs_setup._keychain_store("synthetic", secret) is False


def test_init_heals_nondict_semantic(monkeypatch, tmp_path):
    """init is the provisioning/repair path: a malformed `semantic`
    block is reset with a notice instead of crashing on item
    assignment."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": "off", "signals": "yes"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--semantic-mode", "shadow",
         "--signals-notify"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["semantic"] == {"mode": "shadow"}
    assert cfg["signals"] == {"notify": True}


def test_init_secrets_from_env_not_argv(monkeypatch, tmp_path):
    """FIX-SU1: secrets come from env vars; no --password/--typesafe-key
    flags exist on the CLI."""
    monkeypatch.setenv("MCS_SETUP_PASSWORD", "env-pw")
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    stored = []
    monkeypatch.setattr(mcs_setup, "_keychain_store",
                        lambda acct, pw: stored.append(pw) or True)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes", "--login-id", "u1",
         "--notify-target", "slack:#mcs"])
    assert mcs_setup.main() == 0
    assert stored == ["env-pw"]
    assert "TYPESAFE_API_KEY=env-key" in (tmp_path / ".env").read_text()


def test_password_argv_flag_rejected(monkeypatch):
    import pytest
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes", "--password", "x"])
    with pytest.raises(SystemExit):
        mcs_setup.main()


def test_check_environment_locked_keychain_with_env_fallback(monkeypatch):
    """With MCS_PASSWORD in .env, a locked keychain is a warning, not a
    hard error — the reboot-fallback keeps re-login unmanned."""
    from types import SimpleNamespace

    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/hermes")
    monkeypatch.setattr(
        local_llm, "bounded_request",
        lambda *a, **k: (200, {}, b"{}"))
    monkeypatch.setattr(mcs_setup, "env_value",
                        lambda *a, **k: "env_pw")
    monkeypatch.setattr(
        mcs_setup.subprocess, "run",
        lambda argv, **kw: SimpleNamespace(
            returncode=36 if "-w" in argv else 0, stdout="", stderr=""))

    errors, warnings = mcs_setup.check_environment(
        {"notify_target": "slack"})
    assert not any("keychain is locked" in e.lower() for e in errors)
    assert any("keychain is locked" in w.lower() for w in warnings)
    assert any("env fallback" in w.lower() for w in warnings)


def test_signals_new_keys_validated():
    base = {"mcs_login_id": "u", "notify_target": "slack"}
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": "みどり薬局",          # str, not list
        "self_professions": ["薬剤師", 3],           # non-str member
        "med_exclude_names": [""],                  # empty member
        "digest": "yes",                            # str not bool
        "digest_interval_h": 0,                     # not positive
        "tiers": ["immediate"],                     # not object
    }})
    for k in ("self_organizations", "self_professions",
              "med_exclude_names", "digest", "digest_interval_h", "tiers"):
        assert any(k in e for e in errors), k
    errors, _ = mcs_setup.validate_config({**base, "signals": {
        "self_organizations": ["みどり薬局"],
        "self_professions": ["薬剤師"],
        "med_exclude_names": ["在宅酸素"],
        "digest": False, "digest_interval_h": 12,
        "tiers": {"med_change_no_followup": "immediate"},
    }})
    assert errors == []


def test_init_self_identity_flags(monkeypatch, tmp_path):
    """--self-org/--self-professions write signals.self_* as a manual
    override of the MCS-derived self profile."""
    import json
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda: {"mcs_login_id": "u1",
                                 "notify_target": "slack"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--self-org", "みどり薬局", "--self-org", "そら薬局,梅薬局",
         "--self-professions", "薬剤師,管理薬剤師"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    sig = cfg["signals"]
    assert sig["self_organizations"] == ["みどり薬局", "そら薬局", "梅薬局"]
    assert sig["self_professions"] == ["薬剤師", "管理薬剤師"]


def test_self_posts_and_notify_block_validated():
    base = {"mcs_login_id": "u", "notify_target": "slack"}
    errors, _ = mcs_setup.validate_config({**base, "self_posts": "yes"})
    assert any("self_posts" in e for e in errors)
    errors, _ = mcs_setup.validate_config({**base, "self_posts": True})
    assert errors == []

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "cards",                    # invalid enum
        "card_thread": "yes",                      # not bool
        "route_epoch": 0,                          # not positive
        "card_thread_archive_min": -5,
        "operator": 5,                             # not str
        "discord": {"profile": "p"},               # partial scope
    }})
    for k in ("interactive", "card_thread", "route_epoch",
              "card_thread_archive_min", "operator",
              "application_id", "guild_id", "channel_id"):
        assert any(k in e for e in errors), k

    # interactive=discord without a scope is an error
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "discord"}})
    assert any("notify.discord" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "discord", "card_thread": True,
        "route_epoch": 2, "card_thread_archive_min": 10080,
        "operator": "111",
        "discord": {"profile": "p", "application_id": "1",
                    "guild_id": "2", "channel_id": "3"}}})
    assert errors == []

    # slack transport is a first-class interactive value — the update
    # precheck/postcheck runs validate_config, so a slack deployment
    # must validate or every update is vetoed as a config error
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack"}})
    assert any("notify.slack" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "team_id": "T1", "channel_id": "C1"}}})
    assert errors == []

    # slack scope must not carry a discord tenant field
    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "team_id": "T1", "channel_id": "C1",
                  "guild_id": "9"}}})
    assert any("guild_id" in e for e in errors)

    errors, _ = mcs_setup.validate_config({**base, "notify": {
        "interactive": "slack",
        "slack": {"profile": "p", "application_id": "1",
                  "channel_id": "C1"}}})
    assert any("team_id" in e for e in errors)


def _init_env(monkeypatch, tmp_path, cfg):
    """Common stubs for init tests — no real FS/keychain/prompts."""
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(tmp_path / "c.json"))
    monkeypatch.setattr(mcs_setup, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.delenv("MCS_SETUP_PASSWORD", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    # plugin integration is tested separately — keep init tests off the
    # real hermes CLI entirely
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration",
                        lambda c, a: True)
    monkeypatch.setattr(mcs_setup, "load_config", lambda: dict(cfg))


def test_init_set_flag_covers_any_key(monkeypatch, tmp_path):
    """--set KEY=JSON writes arbitrary (dotted) keys under --yes."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setattr(
        mcs_setup.sys, "argv",
        ["mcs_setup", "init", "--yes",
         "--set", "self_posts=true",
         "--set", 'notify.interactive="discord"',
         "--set", "notify.card_thread=false"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is True
    assert cfg["notify"] == {"interactive": "discord",
                             "card_thread": False}


@pytest.mark.parametrize("source,settings", [
    ("legacy", ['semantic.fact_source="canonical"',
                'semantic.fact_source_gate="fabricated:token"']),
    ("legacy", ['semantic={"mode":"off","fact_source":"canonical",'
                '"fact_source_gate":"fabricated:token"}']),
    ("canonical", ['semantic.fact_source_gate="fabricated:replacement"']),
])
def test_init_set_cannot_mint_or_repin_canonical(
        monkeypatch, tmp_path, capsys, source, settings):
    """--set is not a path around `fact-source canonical --gate-evidence`:
    promoting or re-pinning canonical is refused and nothing is written."""
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "semantic": {"mode": "off", "fact_source": source,
                     "fact_source_gate": "existing:pin"}})
    argv = ["mcs_setup", "init", "--yes"]
    for setting in settings:
        argv += ["--set", setting]
    monkeypatch.setattr(mcs_setup.sys, "argv", argv)
    assert mcs_setup.main() == 1
    assert "fact-source canonical --gate-evidence" in capsys.readouterr().out
    assert not (tmp_path / "c.json").exists()


def test_init_keeps_existing_canonical_on_unrelated_set(monkeypatch, tmp_path):
    import json
    sem = {"mode": "off", "fact_source": "canonical",
           "fact_source_gate": "existing:pin"}
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack", "semantic": sem})
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--set", "self_posts=true"])
    assert mcs_setup.main() == 0
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["self_posts"] is True
    assert saved["semantic"] == sem


def test_wizard_defaults_and_gates(monkeypatch, tmp_path):
    """Enter-on-everything accepts defaults; gated blocks stay out."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is False
    assert cfg["deep_history"] is True
    assert cfg["trickle_pages"] == 3
    # interactive defaults to off -> discord/card keys never asked
    assert cfg["notify"] == {"interactive": "off"}
    # signals.notify off -> digest items gated out
    assert cfg["signals"] == {"notify": False}
    # semantic.mode off -> detail keys gated out
    assert cfg["semantic"] == {"mode": "off"}


def test_wizard_discord_scope_and_answers(monkeypatch, tmp_path):
    """Choosing interactive=discord unlocks the scope prompts; typed
    answers are validated and stored under the nested key."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack"})

    def answer(prompt=""):
        if "notify.interactive" in prompt:
            return "discord"
        if "notify.discord." in prompt:
            return {"notify.discord.profile": "main",
                    "notify.discord.application_id": "app1",
                    "notify.discord.guild_id": "g1",
                    "notify.discord.channel_id": "c1"}[
                        prompt.split(" ")[2]]
        if "self_posts" in prompt:
            return "y"
        if "trickle_pages" in prompt:
            return "5"
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    n = cfg["notify"]
    assert n["interactive"] == "discord"
    assert n["discord"] == {"profile": "main", "application_id": "app1",
                            "guild_id": "g1", "channel_id": "c1"}
    assert n["card_thread"] is True            # default kept via Enter
    assert cfg["self_posts"] is True           # typed bool answer
    assert cfg["trickle_pages"] == 5           # typed int answer


def test_wizard_slack_scope_and_answers(monkeypatch, tmp_path):
    """interactive=slack prompts the notify.slack.* scope (team_id, not
    guild_id) and gates out the discord-only keys."""
    import json
    _init_env(monkeypatch, tmp_path,
              {"mcs_login_id": "u1", "notify_target": "slack:#mcs"})
    prompts = []

    def answer(prompt=""):
        prompts.append(prompt)
        if "notify.interactive" in prompt:
            return "slack"
        if "notify.slack." in prompt:
            return {"notify.slack.profile": "ops",
                    "notify.slack.application_id": "A1",
                    "notify.slack.team_id": "T1",
                    "notify.slack.channel_id": "C1"}[
                        prompt.split(" ")[2]]
        return ""

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    n = cfg["notify"]
    assert n["interactive"] == "slack"
    assert n["slack"] == {"profile": "ops", "application_id": "A1",
                          "team_id": "T1", "channel_id": "C1"}
    assert "discord" not in n
    # discord-only keys were never prompted; card keys were (shared gate)
    assert not any("notify.discord." in p for p in prompts)
    assert not any("notify.operator" in p for p in prompts)
    assert any("notify.card_thread" in p for p in prompts)


def test_wizard_keeps_current_values(monkeypatch, tmp_path):
    """An existing config value is shown and kept on Enter."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "discord:9",
        "self_posts": True, "trickle_pages": 7})
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert cfg["self_posts"] is True and cfg["trickle_pages"] == 7


def test_wizard_dash_removes_optional_key(monkeypatch, tmp_path):
    """Typing '-' on an optional key deletes it from the config."""
    import json
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack",
        "notify_max_age_h": 24})
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "-" if "notify_max_age_h" in prompt else "")
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 0
    cfg = json.loads((tmp_path / "c.json").read_text())
    assert "notify_max_age_h" not in cfg


# ---- services (launchd + hermes cron automation) --------------------

def _services_env(monkeypatch, tmp_path, cron_names=frozenset(),
                  loaded=frozenset(), cron_entries=None,
                  cron_applies=True):
    """Isolate cmd_services: temp dirs, no real launchctl/hermes.
    `cron_names` is legacy sugar: it fabricates verified `cron list`
    entries whose schedules match CRON_JOBS (=> 'exists', no edit).
    The fake hermes cron is stateful — create/edit/remove change what
    the next `cron list` returns — unless `cron_applies` is False
    (a zero-exit call that changed nothing)."""
    from types import SimpleNamespace
    calls = []
    loaded = set(loaded)
    entries = list(cron_entries or [])
    sched_by_script = {s: sched for _, sched, s in mcs_setup.CRON_JOBS}
    for i, name in enumerate(cron_names):
        entries.append({"id": f"9{i:05d}", "name": name,
                        "schedule": sched_by_script.get(name,
                                                        "0 0 * * *"),
                        "script": name})
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mcs_setup, "MANIFEST_PATH",
                        str(tmp_path / "data" / "service_manifest.json"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/h/venv/bin/python")
    # interpreter probe has its own tests — keep it out of `calls`
    monkeypatch.setattr(mcs_setup, "_hermes_py_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path))
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "_agent_loaded",
                        lambda label: label in loaded)
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda cfg: "/x/hermes")
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda path=None: mcs_util.load_config(path) if path else {})
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda h: entries)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)
    monkeypatch.setattr(mcs_setup.os, "getuid", lambda: 501)

    def fake_run(argv, **kw):
        calls.append(list(argv))
        # simulate launchd: bootstrap registers the label, bootout drops it
        if argv[:2] == ["launchctl", "bootstrap"]:
            loaded.add(argv[3].rsplit("/", 1)[-1][:-6])
        elif argv[:2] == ["launchctl", "bootout"]:
            loaded.discard(argv[2].rsplit("/", 1)[-1])
        elif argv[1:3] == ["cron", "create"] and cron_applies:
            entries.append({"id": f"c{len(calls):05d}",
                            "name": argv[argv.index("--name") + 1],
                            "schedule": argv[3],
                            "script": argv[argv.index("--script") + 1]})
        elif argv[1:3] == ["cron", "edit"] and cron_applies:
            next(e for e in entries if e["id"] == argv[3])["schedule"] = \
                argv[argv.index("--schedule") + 1]
        elif argv[1:3] == ["cron", "remove"] and cron_applies:
            entries[:] = [e for e in entries if e["id"] != argv[3]]
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    return calls, SimpleNamespace(dry_run=False)


def test_services_renders_bootstraps_and_registers(monkeypatch, tmp_path):
    # dedupe is by SCRIPT filename — deployed job names drifted
    # ("MCS job drain" vs canonical "MCS durable drain")
    calls, args = _services_env(
        monkeypatch, tmp_path, cron_names={"mcs_check.sh"})
    assert mcs_setup.cmd_services(args) == 0
    # wrapper scripts rendered with placeholders substituted
    s = (tmp_path / "scripts" / "mcs_check.sh").read_text()
    assert "__PYTHON__" not in s and "/h/venv/bin/python" in s
    # all 4 launchd plists rendered and bootstrapped
    for label in mcs_setup.AGENT_LABELS:
        body = (tmp_path / "agents" / f"{label}.plist").read_text()
        assert "__REPO__" not in body
    boots = [a for a in calls if a[:2] == ["launchctl", "bootstrap"]]
    assert len(boots) == 4
    # cron: the job whose script is already registered is skipped;
    # the other 5 (incl. the update check) are created
    creates = [" ".join(a) for a in calls
               if a[:3] == ["/x/hermes", "cron", "create"]]
    assert len(creates) == 5
    assert not any("mcs_check.sh" in c for c in creates)
    assert any("mcs_deep.sh" in c for c in creates)
    assert any("mcs_health.sh" in c for c in creates)
    assert any("mcs_update.sh" in c for c in creates)
    # the service manifest was written with all rendered identities
    import json as _json
    manifest = _json.loads(
        (tmp_path / "data" / "service_manifest.json").read_text())
    assert len(manifest["scripts"]) == 6
    assert len(manifest["agents"]) == 4
    assert len(manifest["cron"]) == 6


def test_services_skips_loaded_agents_and_existing_cron(
        monkeypatch, tmp_path):
    loaded = set(mcs_setup.AGENT_LABELS)
    calls, args = _services_env(
        monkeypatch, tmp_path, loaded=loaded,
        cron_entries=[{"id": f"{i:06d}", "name": n, "schedule": s,
                       "script": sc}
                      for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)])
    # pre-render identical plists so 'loaded + unchanged' is exercised
    subs = {"PYTHON": mcs_setup.HERMES_PY, "REPO": mcs_setup.REPO_ROOT,
            "DATA": str(tmp_path / "data")}
    (tmp_path / "agents").mkdir(parents=True)
    for label in mcs_setup.AGENT_LABELS:
        body = mcs_setup._render_template(
            (Path(mcs_setup.REPO_ROOT) / "deployment/launchagents"
             / f"{label}.plist").read_text(), subs)
        (tmp_path / "agents" / f"{label}.plist").write_text(body)
    assert mcs_setup.cmd_services(args) == 0
    assert not any(a[:2] == ["launchctl", "bootstrap"] for a in calls)
    assert not any(a[:3] == ["/x/hermes", "cron", "create"]
                   for a in calls)


def test_services_dry_run_writes_nothing(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    args.dry_run = True
    assert mcs_setup.cmd_services(args) == 0
    assert not (tmp_path / "scripts").exists()
    assert not (tmp_path / "agents").exists()
    assert calls == []


def test_service_templates_preserve_literal_paths(monkeypatch, tmp_path):
    import plistlib
    import subprocess
    calls, args = _services_env(monkeypatch, tmp_path)
    unusual = str(tmp_path / "space & <tag> $(touch SHOULD_NOT_EXIST) 'quote'")
    monkeypatch.setattr(mcs_setup, "HERMES_PY", unusual + "/python")
    monkeypatch.setattr(mcs_setup, "HOME", unusual)
    assert mcs_setup.cmd_services(args) == 0
    script = (tmp_path / "scripts" / "mcs_health.sh").read_text()
    # Replace just the final invocation with a literal-value print; no service runs.
    script = script[:script.index('"$PY"')] + 'printf "%s" "$PY"\n'
    # _services_env stubs subprocess.run, so use Popen for this isolated shell.
    with subprocess.Popen(["/bin/sh"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, cwd=tmp_path) as proc:
        out, err = proc.communicate(script, timeout=5)
        assert proc.returncode == 0, err
    assert out == unusual + "/python"
    assert not (tmp_path / "SHOULD_NOT_EXIST").exists()
    plist = plistlib.loads((tmp_path / "agents" / "local.mcs-cmd.plist").read_bytes())
    assert plist["ProgramArguments"][0] == unusual + "/python"
    assert plist["WatchPaths"] == [unusual + "/data/cmd"]


@pytest.mark.parametrize("output,expected", [
    ("unexpected output", None), ("", None),
    ("No scheduled jobs.\nCreate one with hermes cron create", []),
])
def test_unverifiable_cron_list_is_not_an_empty_schedule(monkeypatch, output, expected):
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.subprocess, "run", lambda *a, **k:
                        SimpleNamespace(returncode=0, stdout=output))
    assert mcs_setup._cron_list("/synthetic/hermes") == expected


def test_decorated_schedule_compares_actual_five_fields():
    assert mcs_setup._norm_sched("cron: */5 * * * * (UTC)") == "*/5 * * * *"


def test_services_retires_previous_manifest_agent(monkeypatch, tmp_path):
    import json
    obsolete = "ai.mcs.extract-obsolete"
    calls, args = _services_env(monkeypatch, tmp_path, loaded={obsolete})
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / (obsolete + ".plist")).write_text("synthetic")
    (tmp_path / "data").mkdir()
    Path(mcs_setup.MANIFEST_PATH).write_text(json.dumps({"agents": [{"label": obsolete}]}))
    assert mcs_setup.cmd_services(args) == 0
    assert not (agents / (obsolete + ".plist")).exists()
    assert ["launchctl", "bootout", "gui/501/" + obsolete] in calls


def test_services_hermes_missing_reports_problem(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: False)
    assert mcs_setup.cmd_services(args) == 1


def _cron_creates(calls):
    return [a for a in calls if a[1:3] == ["cron", "create"]]


def test_services_rerun_keeps_one_cron_per_script(monkeypatch, tmp_path):
    calls, args = _services_env(monkeypatch, tmp_path)
    assert mcs_setup.cmd_services(args) == 0
    assert mcs_setup.cmd_services(args) == 0
    entries = mcs_setup._cron_list("/x/hermes")
    scripts = [e["script"] for e in entries]
    assert sorted(scripts) == sorted(s for _, _, s in mcs_setup.CRON_JOBS)
    # the second run found every job and created nothing
    assert len(_cron_creates(calls)) == len(mcs_setup.CRON_JOBS)


def _seed_manifest(tmp_path):
    import json
    path = tmp_path / "data" / "service_manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = {"v": 1, "cron": [{"name": "MCS unread check",
                                  "script": "mcs_check.sh",
                                  "id": "000001"}],
                "agents": [], "scripts": []}
    path.write_text(json.dumps(previous))
    return path, previous


def test_services_failure_preserves_last_recoverable_manifest(
        monkeypatch, tmp_path):
    import json
    calls, args = _services_env(monkeypatch, tmp_path)
    path, previous = _seed_manifest(tmp_path)
    monkeypatch.setattr(mcs_setup, "_cron_list", lambda h: None)
    assert mcs_setup.cmd_services(args) == 1
    assert json.loads(path.read_text()) == previous
    assert not _cron_creates(calls)


def test_services_duplicate_owned_script_blocks_cron_mutation(
        monkeypatch, tmp_path):
    import json
    dup = [{"id": f"00000{i}", "name": "MCS unread check",
            "schedule": "*/5 * * * *", "script": "mcs_check.sh"}
           for i in (1, 2)]
    # an obsolete owned job would normally be removed — not while blocked
    stale = {"id": "000009", "name": "old", "schedule": "0 0 * * *",
             "script": "mcs_old.sh"}
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=dup + [stale])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append({"script": "mcs_old.sh"})
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 1
    assert not any(a[1:2] == ["cron"] and a[2] != "list" for a in calls)
    assert json.loads(path.read_text()) == previous


def test_services_noop_create_is_a_problem_not_a_manifest_entry(
        monkeypatch, tmp_path):
    """`cron create` exiting 0 without a job appearing must not be
    recorded as installed."""
    calls, args = _services_env(monkeypatch, tmp_path, cron_applies=False)
    assert mcs_setup.cmd_services(args) == 1
    assert _cron_creates(calls)
    assert not (tmp_path / "data" / "service_manifest.json").exists()


def _desired_cron_entries():
    return [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
            for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)]


def _stale_owned_cron():
    return {"id": "000009", "name": "old", "schedule": "0 0 * * *",
            "script": "mcs_old.sh"}


def test_services_noop_remove_keeps_undesired_job(
        monkeypatch, tmp_path):
    """`cron remove` exiting 0 while the job remains is not converged.
    The previous manifest must still name the script, or a later run
    no longer knows to remove it."""
    import json
    stale = _stale_owned_cron()
    calls, args = _services_env(
        monkeypatch, tmp_path,
        cron_entries=_desired_cron_entries() + [stale],
        cron_applies=False)
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append(dict(stale))
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 1
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    assert json.loads(path.read_text()) == previous
    # stale manifest from this run: a later pass still tries to remove
    calls.clear()
    assert mcs_setup.cmd_services(args) == 1
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    assert json.loads(path.read_text()) == previous


def test_services_removed_undesired_cron_leaves_manifest(
        monkeypatch, tmp_path):
    """A remove that actually drops the job converges: problems 0 and
    the rewritten manifest no longer lists it."""
    import json
    stale = _stale_owned_cron()
    calls, args = _services_env(
        monkeypatch, tmp_path,
        cron_entries=_desired_cron_entries() + [dict(stale)])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append({"script": stale["script"]})
    path.write_text(json.dumps(previous))
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", stale["id"]] in calls
    kept = json.loads(path.read_text())
    scripts = [c.get("script") for c in kept["cron"]]
    assert stale["script"] not in scripts
    assert scripts == [s for _, _, s in mcs_setup.CRON_JOBS]


def test_services_noop_remove_keeps_created_job_owned(
        monkeypatch, tmp_path):
    """A create that lands and a remove that does not must both stay
    owned: problems > 0, and a later run that drops the new script from
    the desired set still removes it."""
    import json
    from types import SimpleNamespace
    stale = _stale_owned_cron()
    present = _desired_cron_entries()
    created = present.pop()
    calls, args = _services_env(
        monkeypatch, tmp_path, cron_entries=present + [stale])
    path, previous = _seed_manifest(tmp_path)
    previous["cron"].append(dict(stale))
    path.write_text(json.dumps(previous))
    inner = mcs_setup.subprocess.run
    hold = {"remove": True}
    removed: set[str] = set()
    entries = mcs_setup._cron_list("/x/hermes")

    def listing(_hermes):
        # copy: the remove loop must see every pre-change job. Mutating
        # the iterated list (the helper's slice-assign) skips the next one.
        return [dict(e) for e in entries if e["id"] not in removed]

    def fake_run(argv, **kw):
        if argv[1:3] == ["cron", "remove"]:
            calls.append(list(argv))
            if not hold["remove"]:
                removed.add(argv[3])
            return SimpleNamespace(returncode=0,
                                   stdout="Success: job removed.",
                                   stderr="")
        return inner(argv, **kw)
    monkeypatch.setattr(mcs_setup, "_cron_list", listing)
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    assert mcs_setup.cmd_services(args) == 1
    scripts = [c.get("script") for c in
               json.loads(path.read_text())["cron"]]
    assert created["script"] in scripts
    assert stale["script"] in scripts
    new_id = next(e["id"] for e in mcs_setup._cron_list("/x/hermes")
                  if e.get("script") == created["script"])
    hold["remove"] = False
    calls.clear()
    monkeypatch.setattr(
        mcs_setup, "CRON_JOBS",
        [job for job in mcs_setup.CRON_JOBS
         if job[2] != created["script"]])
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", new_id] in calls
    assert all(e.get("script") != created["script"]
               for e in mcs_setup._cron_list("/x/hermes"))
    retired = [c.get("script") for c in
               json.loads(path.read_text())["cron"]]
    assert created["script"] not in retired


@pytest.mark.parametrize("failure", ["edit", "agent"])
def test_services_failed_run_keeps_created_job_owned(
        monkeypatch, tmp_path, failure):
    """A create confirmed in `after` is owned even when another step
    (desired-job edit, or a later stage) fails: problems > 0, a second
    failing run keeps ownership, and retiring the job removes it."""
    import json
    from types import SimpleNamespace
    entries = _desired_cron_entries()
    created = entries.pop()
    if failure == "edit":
        entries[0]["schedule"] = "1 1 * * *"
    calls, args = _services_env(monkeypatch, tmp_path, cron_entries=entries)
    path, _ = _seed_manifest(tmp_path)
    fail = {"on": True}
    inner = mcs_setup.subprocess.run

    def fake_run(argv, **kw):
        if fail["on"] and argv[1:3] == ["cron", "edit"]:
            calls.append(list(argv))
            return SimpleNamespace(returncode=1, stdout="",
                                   stderr="synthetic edit failed")
        return inner(argv, **kw)
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    if failure == "agent":
        monkeypatch.setattr(mcs_setup, "_agent_reconcile",
                            lambda *a: not fail["on"])
    assert mcs_setup.cmd_services(args) == 1
    assert len(_cron_creates(calls)) == 1
    first = json.loads(path.read_text())
    assert created["script"] in [c.get("script") for c in first["cron"]]
    # a second failing run creates nothing and keeps ownership stable
    calls.clear()
    assert mcs_setup.cmd_services(args) == 1
    assert not _cron_creates(calls)
    assert json.loads(path.read_text()) == first
    new_id = next(e["id"] for e in mcs_setup._cron_list("/x/hermes")
                  if e["script"] == created["script"])
    fail["on"] = False
    calls.clear()
    monkeypatch.setattr(
        mcs_setup, "CRON_JOBS",
        [job for job in mcs_setup.CRON_JOBS if job[2] != created["script"]])
    assert mcs_setup.cmd_services(args) == 0
    assert ["/x/hermes", "cron", "remove", new_id] in calls
    assert all(e["script"] != created["script"]
               for e in mcs_setup._cron_list("/x/hermes"))
    assert created["script"] not in [
        c.get("script") for c in json.loads(path.read_text())["cron"]]


def test_services_unparseable_script_identity_is_a_problem(
        monkeypatch, tmp_path):
    """A job whose name matches but whose Script field is missing is
    unverifiable: no duplicate create, no 'exists' manifest record."""
    entries = [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
               for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)]
    del entries[0]["script"]
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=entries)
    assert mcs_setup.cmd_services(args) == 1
    assert not _cron_creates(calls)
    assert not (tmp_path / "data" / "service_manifest.json").exists()



def test_services_post_verify_tolerates_undecodable_schedule(
        monkeypatch, tmp_path):
    """Post-change verification uses the pre-check's tolerance: a job
    whose displayed schedule does not parse is not drift, so a create
    elsewhere still converges and the manifest is written."""
    entries = [{"id": f"{i:06d}", "name": n, "schedule": s, "script": sc}
               for i, (n, s, sc) in enumerate(mcs_setup.CRON_JOBS)]
    entries[0]["schedule"] = "every 5m"
    del entries[-1]                                  # forces one create
    calls, args = _services_env(monkeypatch, tmp_path,
                                cron_entries=entries)
    assert mcs_setup.cmd_services(args) == 0
    assert len(_cron_creates(calls)) == 1
    assert (tmp_path / "data" / "service_manifest.json").exists()

def test_services_installs_gateway_when_interactive(
        monkeypatch, tmp_path):
    """interactive=discord + unsupervised gateway -> install + start
    through the public `hermes gateway` CLI."""
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    monkeypatch.setattr(
        mcs_setup, "load_config",
        lambda path=None: mcs_util.load_config(path) if path else
        {"notify": {"interactive": "discord"}})
    calls.clear()
    # fake_run returns empty stdout -> "supervised" absent -> install
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert ["/x/hermes", "gateway", "install"] in gw
    assert ["/x/hermes", "gateway", "start"] in gw


def test_services_gateway_supervised_is_noop(monkeypatch, tmp_path):
    from types import SimpleNamespace
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    monkeypatch.setattr(
        mcs_setup, "load_config",
        lambda path=None: mcs_util.load_config(path) if path else
        {"notify": {"interactive": "discord"}})

    def fake_run(argv, **kw):
        calls.append(list(argv))
        return SimpleNamespace(
            returncode=0,
            stdout="✓ Gateway is supervised by launchd (PID 1)",
            stderr="")
    monkeypatch.setattr(mcs_setup.subprocess, "run", fake_run)
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    gw = [a for a in calls if "gateway" in a]
    assert gw == [["/x/hermes", "gateway", "status"]]


def test_services_gateway_skipped_when_off(monkeypatch, tmp_path):
    calls, args = _services_env(
        monkeypatch, tmp_path,
        loaded=set(mcs_setup.AGENT_LABELS),
        cron_names={s for _, _, s in mcs_setup.CRON_JOBS})
    calls.clear()
    assert mcs_setup.cmd_services(args) == 0
    assert not any("gateway" in a for a in calls)


# ---- plugin integration (hermes config CLI, never hermes internals) --

def _plugin_args(**kw):
    from types import SimpleNamespace
    base = {"yes": True, "plugin_profile": "", "plugin_user_ids": None,
                "plugin_chat_ids": None, "plugin_project_ids": None}
    base.update(kw)
    return SimpleNamespace(**base)


def _plugin_env(monkeypatch, existing=None):
    """Stub the hermes CLI surface; `existing` maps config keys that
    `config get` already resolves."""
    sets = []
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda c: "/x/hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda e: True)
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_get",
        lambda e, p, k: (existing or {}).get(k))
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_set",
        lambda e, p, k, v: sets.append((p, k, str(v))) or True)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    return sets


_DISCORD_CFG = {"notify": {"interactive": "discord", "discord": {
    "profile": "cco", "application_id": "app",
    "guild_id": "g", "channel_id": "ch"}}}


def test_plugin_integration_writes_scope_and_lists(monkeypatch):
    """Fixed settings mirror notify.discord + known paths; csv flags
    become YAML list literals; writes go to the serving profile."""
    sets = _plugin_env(monkeypatch)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG),
        _plugin_args(plugin_profile="cco",
                     plugin_user_ids="u1,u2",
                     plugin_project_ids="1,2"))
    keys = {k for _, k, _ in sets}
    for want in ("snapshot", "inbox", "data_root", "interactive",
                 "profile", "application_id", "guild_id",
                 "channel_id", "allowed_user_ids", "project_ids"):
        assert f"{mcs_setup.PLUGIN_SETTINGS}.{want}" in keys
    assert all(p == "cco" for p, k, _ in sets
               if k != "DISCORD_BOT_TOKEN")
    assert ("cco", f"{mcs_setup.PLUGIN_SETTINGS}.allowed_user_ids",
            '["u1", "u2"]') in sets
    # allowed_chat_ids: no flag, no existing value, --yes -> skipped
    assert all(not k.endswith("allowed_chat_ids") for _, k, _ in sets)


def test_plugin_integration_off_is_noop(monkeypatch):
    monkeypatch.setattr(
        mcs_setup, "_hermes_exe",
        lambda c: (_ for _ in ()).throw(AssertionError("must not run")))
    assert mcs_setup._apply_plugin_integration(
        {"notify": {"interactive": "off"}}, _plugin_args()) is True


_SLACK_CFG = {"notify": {"interactive": "slack", "slack": {
    "profile": "ops", "application_id": "A1",
    "team_id": "T1", "channel_id": "C1"}}}


@pytest.mark.parametrize("cfg,failing", [
    (_DISCORD_CFG, "settings.guild_id"),
    (_DISCORD_CFG, "DISCORD_BOT_TOKEN"),
    (_SLACK_CFG, "settings.slack_team_id"),
    (_SLACK_CFG, "SLACK_APP_TOKEN"),
])
def test_plugin_integration_reports_failed_write(monkeypatch, cfg, failing):
    """Any attempted `config set` that fails makes the result False —
    the remaining keys are still attempted."""
    sets = []
    _plugin_env(monkeypatch)
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_set",
        lambda e, p, k, v: sets.append(k) or not k.endswith(failing))
    for tok in ("DISCORD_BOT_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        monkeypatch.setenv(tok, "synthetic-token")
    assert mcs_setup._apply_plugin_integration(
        dict(cfg), _plugin_args(plugin_user_ids="u1")) is False
    assert any(k.endswith(failing) for k in sets)
    assert any(k.endswith("snapshot") for k in sets)


def test_plugin_integration_skipped_keys_are_not_failures(monkeypatch):
    """--yes leaves unanswered allowlists/tokens unset — reported, but
    not a failed write."""
    sets = _plugin_env(monkeypatch)
    assert mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args()) is True
    assert all(not k.endswith("_ids") for _, k, _ in sets)


def test_plugin_integration_without_hermes_cli_fails(monkeypatch):
    monkeypatch.setattr(mcs_setup, "_hermes_exe", lambda c: "/x/hermes")
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda e: False)
    assert mcs_setup._apply_plugin_integration(
        dict(_SLACK_CFG), _plugin_args()) is False


@pytest.mark.parametrize("cfg", [_DISCORD_CFG, _SLACK_CFG])
def test_init_fails_when_plugin_settings_not_written(
        monkeypatch, tmp_path, capsys, cfg):
    """A failed Hermes write still lets init finish the gateway sync
    and check, then exits 1 with a repair hint."""
    import json
    real_integration = mcs_setup._apply_plugin_integration
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "u1", "notify_target": "slack", **cfg})
    _plugin_env(monkeypatch)
    monkeypatch.setattr(mcs_setup, "_apply_plugin_integration",
                        real_integration)
    monkeypatch.setattr(mcs_setup, "_hermes_config_set",
                        lambda e, p, k, v: False)
    ran = []
    monkeypatch.setattr(mcs_setup, "_sync_gateway",
                        lambda *a, **k: ran.append("gateway") or 0)
    monkeypatch.setattr(mcs_setup, "cmd_check",
                        lambda args: ran.append("check") or 0)
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes"])
    assert mcs_setup.main() == 1
    assert ran == ["gateway", "check"]
    assert "init: FAIL — Hermes plugin settings were not fully written" \
        in capsys.readouterr().out
    # the config itself was still written before the Hermes step
    saved = json.loads((tmp_path / "c.json").read_text())
    assert saved["notify"] == cfg["notify"]


def test_plugin_integration_existing_allowlist_kept(monkeypatch):
    """A configured allowlist is reported as set and not rewritten."""
    sets = _plugin_env(
        monkeypatch,
        existing={f"{mcs_setup.PLUGIN_SETTINGS}.allowed_user_ids":
                  "- u1"})
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG),
                                        _plugin_args())
    assert all(not k.endswith("allowed_user_ids") for _, k, _ in sets)


def test_plugin_integration_token_via_env_to_env_file(monkeypatch):
    """DISCORD_BOT_TOKEN goes through `config set` (routes *_TOKEN to
    the profile .env) on the launch profile, never via argv flag."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG),
                                        _plugin_args())
    assert ("", "DISCORD_BOT_TOKEN", "tok") in sets


def test_plugin_integration_slack_writes_scope_and_tokens(monkeypatch):
    """Slack scope maps to slack_* settings keys; both slack tokens go
    through `config set --stdin` (never argv); existing tokens are kept."""
    sets = _plugin_env(
        monkeypatch,
        existing={"SLACK_APP_TOKEN": "xapp-already"})
    monkeypatch.setenv("SLACK_BOT_TOKEN", "bot-tok")
    cfg = {"notify": {"interactive": "slack", "slack": {
        "profile": "ops", "application_id": "A1",
        "team_id": "T1", "channel_id": "C1"}}}
    mcs_setup._apply_plugin_integration(
        cfg, _plugin_args(plugin_profile="ops",
                          plugin_user_ids="U1,U2",
                          plugin_project_ids="1,2"))
    keys = {k for _, k, _ in sets}
    for want in ("snapshot", "inbox", "data_root", "interactive",
                 "slack_adapter_enabled", "slack_profile",
                 "slack_application_id", "slack_team_id",
                 "slack_channel_id", "slack_allowed_user_ids",
                 "project_ids"):
        assert f"{mcs_setup.PLUGIN_SETTINGS}.{want}" in keys, want
    assert ("ops", f"{mcs_setup.PLUGIN_SETTINGS}.slack_adapter_enabled",
            "true") in sets
    assert ("ops",
            f"{mcs_setup.PLUGIN_SETTINGS}.slack_allowed_user_ids",
            '["U1", "U2"]') in sets
    assert ("ops", "SLACK_BOT_TOKEN", "bot-tok") in sets
    # already-configured token is left alone; discord-only scope keys
    # (guild_id / bare allowed_*_ids) are never written for slack
    assert all(k != "SLACK_APP_TOKEN" for _, k, _ in sets)
    bare = {k.rsplit(".", 1)[-1] for _, k, _ in sets
            if k.startswith(mcs_setup.PLUGIN_SETTINGS)}
    assert not bare & {"allowed_user_ids", "allowed_chat_ids",
                       "guild_id", "profile", "application_id",
                       "channel_id"}


def test_any_token_key_is_piped_via_stdin(monkeypatch):
    """The *_TOKEN -> --stdin rule covers every token key, not just
    DISCORD_BOT_TOKEN — a new token name must never leak into argv."""
    from types import SimpleNamespace
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    assert mcs_setup._hermes_config_set("/fake/hermes", "p",
                                        "SLACK_APP_TOKEN", "xapp-secret")
    argv, kwargs = calls[0]
    assert "--stdin" in argv and "xapp-secret" not in " ".join(argv)
    assert kwargs["input"] == "xapp-secret"


def test_env_merge_failure_preserves_existing_credentials(tmp_path, monkeypatch):
    import pytest
    path = tmp_path / ".env"
    path.write_text("SAMPLE_TOKEN=synthetic-original\nUNCHANGED=keep\n")
    original = path.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(mcs_setup.os, "replace", fail_replace)
    with pytest.raises(OSError, match="synthetic publication failure"):
        mcs_setup._env_write(str(path), {"SAMPLE_TOKEN": "synthetic-new"})
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


def test_hermes_token_is_piped_without_argv_fallback(monkeypatch):
    from types import SimpleNamespace
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(mcs_setup.subprocess, "run", run)
    token = "synthetic-bot-credential"
    assert not mcs_setup._hermes_config_set("/fake/hermes", "test-profile",
                                            "DISCORD_BOT_TOKEN", token)
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["/fake/hermes", "-p", "test-profile", "config", "set",
                    "DISCORD_BOT_TOKEN", "--stdin"]
    assert token not in " ".join(argv)
    assert kwargs["input"] == token


def test_plugin_integration_token_written_to_serving_profile(
        monkeypatch):
    """The bot token is a PROFILE-scoped secret — under multiplex a
    named profile never falls through to the default .env, so the
    token must land where the plugin actually serves (F-token)."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco"))
    assert ("cco", "DISCORD_BOT_TOKEN", "tok") in sets
    assert not any(k == "DISCORD_BOT_TOKEN" and p == ""
                   for p, k, _ in sets)


def test_plugin_integration_token_check_reads_serving_profile(
        monkeypatch):
    """'Already set' detection must query the serving profile — a
    token present only in the default .env must NOT silence setup for
    profile cco (it would leave the serving profile unable to auth)."""
    sets = _plugin_env(monkeypatch)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        mcs_setup, "_hermes_config_get",
        lambda e, p, k: "tok" if (p, k) == ("cco", "DISCORD_BOT_TOKEN")
        else None)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco"))
    # token seen on cco -> reported set, no write attempted
    assert not any(k == "DISCORD_BOT_TOKEN" for _, k, _ in sets)


def test_check_warns_when_gateway_unsupervised(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup.shutil, "which", lambda *a: "/x/h")
    monkeypatch.setattr(mcs_setup.os.path, "isfile", lambda p: True)
    monkeypatch.setattr(mcs_setup.os, "access", lambda p, m: True)

    def fake_cli(exe, profile, *argv, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="")
    monkeypatch.setattr(mcs_setup, "_hermes_cli", fake_cli)
    monkeypatch.setattr(local_llm, "bounded_request",
                        lambda *a, **k: (200, {}, b"{}"))
    _, warnings = mcs_setup.check_environment(
        {"mcs_login_id": "u", "notify_target": "discord:1",
         "notify": {"interactive": "discord"}})
    assert any("gateway" in w for w in warnings)


# ---- local_llm config validation ----------------------------------

def test_local_llm_block_validation(monkeypatch):
    """local_llm.url must stay loopback http — a non-loopback URL would
    exfiltrate message bodies; the validator rejects it up front."""
    ok, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "http://127.0.0.1:9999/v1/chat/completions",
                      "model": "M"}})
    assert not [e for e in ok if "local_llm" in e]

    errors, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "https://evil.example.com/v1/chat"}})
    assert any("local_llm.url" in e and "loopback" in e
               for e in errors)

    errors, _w = mcs_setup.validate_config({
        "mcs_login_id": "u", "notify_target": "slack",
        "local_llm": {"url": "http://u:p@127.0.0.1:8080/v1/x"}})
    assert any("local_llm.url" in e for e in errors)   # creds rejected


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
    assert any("notify.discord" in w and "ignored" in w
               for w in warnings)


@pytest.mark.parametrize("outcomes,late,ok,boots,loads", [
    ([5, 0], False, True, 2, True),
    ([5, 5, 5], True, True, 3, True),
    ([5, 5, 5], False, False, 3, True),
    # exit 0 is not proof: the label must answer `print` afterwards
    ([0], False, False, 1, False),
])
def test_agent_reconcile_retries_transient_bootstrap(
        monkeypatch, outcomes, late, ok, boots, loads):
    import subprocess
    from types import SimpleNamespace
    import mcs_util
    state = {"loaded": True, "calls": [], "outcomes": list(outcomes)}

    def fake_run(argv, **kwargs):
        verb = argv[1]
        state["calls"].append(verb)
        rc = 0
        if verb == "bootout":
            state["loaded"] = False
        elif verb == "bootstrap":
            rc = state["outcomes"].pop(0)
            if (rc == 0 and loads) or (late and not state["outcomes"]):
                state["loaded"] = True
        elif verb == "print":
            rc = 0 if state["loaded"] else 113
        return subprocess.CompletedProcess(
            argv, rc, "success", "" if rc == 0 else "5: Input/output error")

    sleeps, notes = [], []
    monkeypatch.setattr(mcs_setup, "_run", fake_run)
    monkeypatch.setattr(mcs_util, "time",
                        SimpleNamespace(time=mcs_util.time.time,
                                        sleep=sleeps.append))
    assert mcs_setup._agent_reconcile("ai.mcs.x", "/p.plist",
                                      notes.append, False) is ok
    assert state["calls"][:2] == ["print", "bootout"]
    assert state["calls"].count("bootstrap") == boots
    assert state["calls"][-1] == "print"
    assert sleeps == [1] * sum(rc != 0 for rc in outcomes)
    assert any("bootstrap failed" in n for n in notes) is (not ok)


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
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(tmp_path / "repo"))
    monkeypatch.setattr(mcs_setup, "SCRIPTS_DIR", str(tmp_path / "scripts"))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", "/h/venv/bin/python")
    monkeypatch.setattr(mcs_setup, "HOME", str(tmp_path / "home dir"))

    # nothing deployed yet (fresh install) -> warning, not an error
    errors, warnings = mcs_setup._script_drift()
    assert errors == []
    assert any("a.sh, b.sh" in w and "services" in w for w in warnings)

    # the services render is by definition in sync
    mcs_setup._sync_scripts(mcs_setup._service_subs(), {"scripts": []},
                            lambda m: None, dry=False)
    assert "'" in (tmp_path / "scripts" / "b.sh").read_text()  # quoted
    assert mcs_setup._script_drift() == ([], [])

    # repo template changed, deployed copy not resynced -> named error
    (src / "a.sh").write_text("#!/bin/sh\nexec __PYTHON__ __REPO__/y\n")
    errors, warnings = mcs_setup._script_drift()
    assert warnings == []
    assert len(errors) == 1 and "a.sh" in errors[0] \
        and "b.sh" not in errors[0] and "mcs_setup.py services" in errors[0]

    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda path=None: mcs_util.load_config(path) if path else {})
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: ([], []))
    monkeypatch.setattr(mcs_setup, "check_environment",
                        lambda cfg: ([], []))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda cfg: [])
    monkeypatch.setattr(mcs_setup, "check_runtime", lambda cfg: ([], []))
    assert mcs_setup.cmd_check(None) == 1
    assert "a.sh" in capsys.readouterr().out


# ---- invalid config.json is never treated as empty ---------------------

def test_init_stops_on_invalid_config(monkeypatch, tmp_path, capsys):
    """A present-but-broken config.json must not be overwritten with a
    fresh one (every setting silently lost) — init stops, file intact."""
    _init_env(monkeypatch, tmp_path, {})
    (tmp_path / "c.json").write_text('{"mcs_login_id": "u1",')
    monkeypatch.setattr(mcs_setup.getpass, "getpass", lambda p="": "")
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init"])
    assert mcs_setup.main() == 1
    assert (tmp_path / "c.json").read_text() == '{"mcs_login_id": "u1",'
    assert "init --yes" in capsys.readouterr().out
    assert not list(tmp_path.glob("c.json.corrupt-*"))


@pytest.mark.parametrize("body", ['{"broken', "[1, 2]"])
def test_init_yes_moves_invalid_config_aside(monkeypatch, tmp_path, body):
    import json
    _init_env(monkeypatch, tmp_path, {})
    (tmp_path / "c.json").write_text(body)
    monkeypatch.setattr(mcs_setup.sys, "argv",
                        ["mcs_setup", "init", "--yes",
                         "--login-id", "u1", "--notify-target", "slack"])
    assert mcs_setup.main() == 0
    (backup,) = tmp_path.glob("c.json.corrupt-*")
    assert backup.read_text() == body
    assert backup.stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "c.json").read_text())["mcs_login_id"] \
        == "u1"


def test_fact_source_refuses_invalid_config(monkeypatch, tmp_path):
    from types import SimpleNamespace
    conf = tmp_path / "c.json"
    conf.write_text("not json")
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup.cmd_fact_source(
        SimpleNamespace(fact_source="shadow", gate_evidence=None)) == 1
    assert conf.read_text() == "not json"


def _check_only(monkeypatch, runtime=([], []), config=([], [])):
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda path=None: mcs_util.load_config(path) if path else {})
    monkeypatch.setattr(mcs_setup, "check_runtime", lambda cfg: runtime)
    monkeypatch.setattr(mcs_setup, "validate_config", lambda cfg: config)
    monkeypatch.setattr(mcs_setup, "check_environment",
                        lambda cfg: ([], []))
    monkeypatch.setattr(mcs_setup, "_script_drift", lambda: ([], []))
    monkeypatch.setattr(mcs_setup, "_queue_warnings", lambda cfg: [])


def test_check_flags_invalid_config_file(monkeypatch, tmp_path, capsys):
    _check_only(monkeypatch)
    conf = tmp_path / "c.json"
    monkeypatch.setattr(mcs_setup, "CONF_PATH", str(conf))
    assert mcs_setup.cmd_check(None) == 0      # absent = fresh, not broken
    conf.write_text("{")
    assert mcs_setup.cmd_check(None) == 1
    assert "unreadable or invalid" in capsys.readouterr().out


def test_check_prints_prioritized_blocker_summary(monkeypatch, capsys):
    """Blockers end the output in priority order (runtime before config),
    each with a one-line fix."""
    _check_only(monkeypatch,
                runtime=(["interpreter /h/py is missing — cannot start; "
                          "re-run `/r/install.sh` (stage 2)"], []),
                config=(["missing required key: mcs_login_id"],
                        ["unknown config key: x"]))
    assert mcs_setup.cmd_check(None) == 1
    out = capsys.readouterr().out
    summary = out[out.index("blockers (2)"):]
    assert summary.index("interpreter /h/py is missing") \
        < summary.index("missing required key")
    assert "fix: /r/install.sh" in summary
    assert "fix: mcs_setup.py init" in summary
    assert out.rstrip().endswith("check: FAIL (2 errors, 1 warnings)")


def test_check_ok_prints_no_blocker_summary(monkeypatch, capsys):
    _check_only(monkeypatch)
    assert mcs_setup.cmd_check(None) == 0
    assert "blockers" not in capsys.readouterr().out


def test_doctor_prints_facts_then_runs_check(monkeypatch, capsys):
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, "load_config",
                        lambda path=None: mcs_util.load_config(path) if path else {})
    monkeypatch.setattr(mcs_setup, "_hermes_py_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 7)
    assert mcs_setup.cmd_doctor(None) == 7
    out = capsys.readouterr().out
    assert f"repo     : {mcs_setup.REPO_ROOT}" in out
    assert mcs_setup.sys.executable in out


# ---- runtime: services interpreter, launchd hermes, recovery, llama ----

def _stub_py(path, rc):
    path.write_text(f"#!/bin/sh\nexit {rc}\n")
    path.chmod(0o755)
    return str(path)


def test_check_runtime_verifies_services_interpreter(monkeypatch, tmp_path):
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    cfg = {"hermes_bin": _stub_py(tmp_path / "hermes", 0)}
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(tmp_path / "absent"))
    errors, _ = mcs_setup.check_runtime(cfg)
    assert any("absent is missing" in e and "install.sh" in e
               for e in errors)
    monkeypatch.setattr(mcs_setup, "HERMES_PY",
                        _stub_py(tmp_path / "old-py", 1))
    errors, _ = mcs_setup.check_runtime(cfg)
    assert any("not a working Python >= 3.10" in e for e in errors)
    monkeypatch.setattr(mcs_setup, "HERMES_PY",
                        _stub_py(tmp_path / "py", 0))
    assert mcs_setup.check_runtime(cfg) == ([], [])


def test_check_runtime_hermes_must_resolve_on_launchd_path(monkeypatch,
                                                          tmp_path):
    """hermes found only via the login shell PATH (e.g. Homebrew) is
    invisible to launchd/cron wrappers — an error with the exact fix."""
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup, "_hermes_py_problem", lambda: None)
    shell_only = "/opt/homebrew/bin/hermes"
    monkeypatch.setattr(mcs_setup.shutil, "which",
                        lambda cmd, mode=None, path=None:
                        shell_only if path is None else None)
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: exe == shell_only)
    errors, _ = mcs_setup.check_runtime({})
    assert any("launchd PATH" in e
               and f"ln -s {shell_only} ~/.local/bin/hermes" in e
               for e in errors)
    # an explicit hermes_bin is what every job uses -> fine
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: True)
    assert mcs_setup.check_runtime({"hermes_bin": shell_only}) == ([], [])


def _runtime_darwin(monkeypatch, tmp_path, loaded=()):
    from types import SimpleNamespace
    repo = tmp_path / "repo"
    (repo / "deployment" / "recovery").mkdir(parents=True)
    (repo / "deployment" / "recovery" / "mcs_recover.py").write_text("v1")
    for d in ("recovery", "agents"):
        (tmp_path / d).mkdir()
    monkeypatch.setattr(mcs_setup.sys, "platform", "darwin")
    monkeypatch.setattr(mcs_setup, "REPO_ROOT", str(repo))
    monkeypatch.setattr(mcs_setup, "RECOVERY_DIR", str(tmp_path / "recovery"))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(tmp_path / "agents"))
    monkeypatch.setattr(mcs_setup, "_hermes_py_problem", lambda: None)
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda exe: True)
    monkeypatch.setattr(mcs_setup, "_run",
                        lambda *a, **k: SimpleNamespace(returncode=0))
    loaded = set(loaded)
    monkeypatch.setattr(mcs_setup, "_agent_loaded",
                        lambda label: label in loaded)
    return repo, loaded


def test_check_runtime_nothing_installed_only_warns(monkeypatch, tmp_path):
    _runtime_darwin(monkeypatch, tmp_path)
    errors, warnings = mcs_setup.check_runtime({"hermes_bin": "/x"})
    assert errors == []
    assert any("org.mcs.recovery" in w and "not installed" in w
               for w in warnings)
    assert any("no llama-server LaunchAgent" in w for w in warnings)


def test_check_runtime_recovery_watchdog(monkeypatch, tmp_path):
    repo, loaded = _runtime_darwin(
        monkeypatch, tmp_path,
        loaded={"org.mcs.recovery", "ai.mcs.llamaserver"})
    rec = tmp_path / "recovery"
    (rec / "mcs_recover.py").write_text("v1")
    (rec / "repo_path").write_text(f"{repo}\n")
    (tmp_path / "agents" / "org.mcs.recovery.plist").write_text("x")
    assert mcs_setup.check_runtime({"hermes_bin": "/x"}) == ([], [])

    (rec / "mcs_recover.py").write_text("v0")
    (rec / "repo_path").write_text("/elsewhere\n")
    loaded.discard("org.mcs.recovery")
    errors, warnings = mcs_setup.check_runtime({"hermes_bin": "/x"})
    assert any("differs from the repo copy" in w for w in warnings)
    assert any("recovers /elsewhere" in w for w in warnings)
    assert any("org.mcs.recovery installed but not loaded" in e
               and "launchctl bootstrap" in e for e in errors)


def test_check_runtime_llama_agent_installed_but_unloaded(monkeypatch,
                                                         tmp_path):
    _runtime_darwin(monkeypatch, tmp_path)
    (tmp_path / "agents" / "ai.mcs.llamaserver.plist").write_text("x")
    errors, _ = mcs_setup.check_runtime({"hermes_bin": "/x"})
    assert any("ai.mcs.llamaserver installed but not loaded" in e
               for e in errors)


def test_services_refuses_missing_interpreter(monkeypatch, tmp_path,
                                              capsys):
    """Rendering wrappers/plists around a missing interpreter makes every
    job fail at every run — services must stop before writing anything."""
    probe = mcs_setup._hermes_py_problem
    calls, args = _services_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mcs_setup, "_hermes_py_problem", probe)
    monkeypatch.setattr(mcs_setup.os.path, "isfile",
                        lambda p: p != mcs_setup.HERMES_PY)
    assert mcs_setup.cmd_services(args) == 1
    assert calls == []
    assert not (tmp_path / "scripts").exists()
    assert "nothing rendered" in capsys.readouterr().out


def test_plugin_role_ids_written_only_when_given(monkeypatch):
    """--plugin-role-ids lands as allowed_role_ids (a YAML list) for a
    Discord scope; without the flag nothing is written or reported
    missing — roles are optional."""
    sets = _plugin_env(monkeypatch)
    mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco",
                                         plugin_role_ids="555,556"))
    assert ("cco", f"{mcs_setup.PLUGIN_SETTINGS}.allowed_role_ids",
            '["555", "556"]') in sets
    sets.clear()
    mcs_setup._apply_plugin_integration(dict(_DISCORD_CFG), _plugin_args())
    assert all(not k.endswith("allowed_role_ids") for _, k, _ in sets)
    # the guild id is @everyone — refused, never written
    sets.clear()
    assert mcs_setup._apply_plugin_integration(
        dict(_DISCORD_CFG), _plugin_args(plugin_profile="cco",
                                         plugin_role_ids="555,g")) is False
    assert all(not k.endswith("allowed_role_ids") for _, k, _ in sets)


def test_all_replies_config_requires_boolean():
    base = {"mcs_login_id": "u1", "notify_target": "slack:#mcs"}
    assert mcs_setup.validate_config({**base, "notify_all_replies": True}) == ([], [])
    errors, _ = mcs_setup.validate_config({**base, "notify_all_replies": "true"})
    assert errors == ["notify_all_replies: must be a boolean"]


@pytest.mark.parametrize("raw", [b'{"a":0,"b":1,"c":2}', b'null', b'[' * 2000 + b']' * 2000], ids=["object", "null", "deep"])
def test_check_rejects_unreadable_slots_and_uses_bounded_probe(monkeypatch, raw):
    import local_llm
    monkeypatch.setattr(mcs_setup.sys, "platform", "linux")
    monkeypatch.setattr(mcs_setup.os.path, "exists", lambda p: True)
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda e: True)
    calls = []
    def request(url, method, body, timeout):
        calls.append((url, method, body, timeout))
        return 200, {}, raw if url.endswith("/slots") else b'{}'
    monkeypatch.setattr(local_llm, "bounded_request", request)
    _, warnings = mcs_setup.check_environment({"local_llm": {"url": "http://127.0.0.1:8089/v1/chat/completions"}})
    assert calls == [("http://127.0.0.1:8089/v1/models", "GET", None, 3),
                     ("http://127.0.0.1:8089/slots", "GET", None, 3)]
    assert any("/slots unreadable" in w for w in warnings)


@pytest.mark.parametrize("raw", [b'\xff', b'[' * 2000 + b']' * 2000], ids=["encoding", "deep"])
def test_malformed_service_manifest_is_recoverable(monkeypatch, tmp_path, raw):
    path = tmp_path / "manifest.json"
    path.write_bytes(raw)
    monkeypatch.setattr(mcs_setup, "MANIFEST_PATH", str(path))
    assert mcs_setup._load_manifest() == {}


@pytest.mark.parametrize("cfg", [_DISCORD_CFG, _SLACK_CFG])
def test_init_returns_failure_when_gateway_sync_fails(monkeypatch, tmp_path, cfg):
    _init_env(monkeypatch, tmp_path, {
        "mcs_login_id": "synthetic", "notify_target": "local", **cfg})
    monkeypatch.setattr(mcs_setup, "_hermes_ok", lambda e: True)
    monkeypatch.setattr(mcs_setup, "_sync_gateway", lambda *a, **k: 1)
    monkeypatch.setattr(mcs_setup, "cmd_check", lambda args: 0)
    monkeypatch.setattr(mcs_setup.sys, "argv", ["mcs_setup", "init", "--yes"])
    assert mcs_setup.main() == 1
