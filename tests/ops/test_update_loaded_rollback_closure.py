"""Cold updater retains its finite rollback closure after a synthetic old-tree cut."""
import update_apply
import update_cli
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_restore
import update_runtime_migration
import update_services
from pathlib import Path
import os
import shutil
import subprocess
import sys

import pytest


_CHILD = r'''
import json, os, pathlib, shutil, socket, sqlite3, subprocess, sys
checkout, root, case = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
case, _, fault = case.partition(":")
sys.path.insert(0, str(checkout / "mcs"))
import _mcs_path

def forbidden(*args, **kwargs):
    raise AssertionError("unexpected process or network operation")
socket.create_connection = forbidden
socket.socket.connect = forbidden
subprocess.run = forbidden
subprocess.Popen = forbidden
if case == "missing_gate":
    (checkout / "mcs/ops/release_apply_owner.py").unlink()
if case == "cloud_owner_missing":
    (checkout / "mcs/llm/cloud_inference.py").unlink()
import mcs_update as updater
# The shared cloud-contract owner must already be captured from this
# generation: a lazy import after the tree reset cannot satisfy it.
cloud_owner = sys.modules.get("cloud_inference")
assert cloud_owner is not None
if case == "missing_gate":
    import contextlib, io
    assert updater._release_legacy_scope is None
    assert updater._build_parser().parse_args(["status"]).cmd == "status"
    (checkout / "mcs/ops/release_apply_owner.py").write_text("raise AssertionError('new generation imported')\n")
    called = []
    with contextlib.redirect_stdout(io.StringIO()):
        assert updater._call_legacy(lambda: called.append(True)) == 2
    assert called == []
    assert updater._release_legacy_blocked({}) is True
    print("synthetic cross-generation closure ok")
    sys.exit(0)
# Reach only updater owners already captured before the old-tree cut.
lifecycle_owners = tuple(sys.modules[name] for name in (
    "update_lifecycle_context", "update_repository", "update_preflight",
    "update_planning", "update_services", "update_runtime_migration",
    "update_execution", "update_apply", "update_restore", "update_recovery", "update_cli"))

def patch_updater(name, value):
    setattr(updater, name, value)
    for owner in lifecycle_owners:
        if name in vars(owner):
            setattr(owner, name, value)

import ledger
root.mkdir()
data = root / "data"
data.mkdir()
patch_updater('HOME', str(root))
patch_updater('DATA', str(data))
patch_updater('REPO', str(checkout))
patch_updater('LEDGER', str(data / "ledger.db"))
patch_updater('STATE_PATH', str(data / "update_state.json"))
patch_updater('RESTORE_REPORT_PATH', str(data / "restore_report.json"))
backup = data / "backup.db"
for path, version, count in [(updater.LEDGER, 9, 3), (str(backup), 7, 2)]:
    store = ledger.Ledger(path)
    try:
        store.db.execute("PRAGMA user_version=" + str(version))
        for mid in range(1, count + 1):
            store.db.execute("INSERT INTO messages(message_id,project_id,body_state,content_hash) VALUES(?,1,'full','synthetic')", (mid,))
        store.db.commit()
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        store.close()
entry = {"sha": "new", "prev_sha": "old", "rollback": True,
         "schema_bump": True, "backup_path": str(backup)}
state = updater.journal_store.default_state()
state["applying"] = dict(entry)
updater.journal_store.save_state(updater.STATE_PATH, state)
trace = []
updater.mcs_util.load_config = lambda *a, **k: {}
patch_updater('_restore_runtime', lambda value: trace.append("runtime"))
patch_updater('_rollback_layout_error', lambda value: None)
patch_updater('_local_changes_outside', lambda *a: [])
patch_updater('_head_sha', lambda: "old")
patch_updater('_tree_clean', lambda: True)
patch_updater('_cancel_unsent_notices', lambda: 0)
patch_updater('_services_reconcile', lambda: trace.append("services"))
updater.service_management.retire_to_snapshot = lambda *a, **k: []
patch_updater('_agent_pid', lambda label: 1)
updater.restore_evidence.find_restore_consent = lambda *a, **k: None if case == "hold" else "synthetic-bound-consent"
original_swap = updater._replace_database
original_load = updater.journal_store.load_state
original_save = updater.journal_store.save_state

def swap(*args, **kwargs):
    trace.append("swap")
    result = original_swap(*args, **kwargs)
    if fault == "save_failure":
        def failed_save(path, state):
            if (state.get("applying") or {}).get("restore_incomplete"):
                raise OSError("synthetic incomplete journal publication failure")
            return original_save(path, state)
        updater.journal_store.save_state = failed_save
    elif fault == "unreadable":
        updater.journal_store.load_state = lambda path: {"_corrupt": True}
    return result
patch_updater('_replace_database', swap)
# Stubs are attached only to modules the updater itself already captured;
# importing them in this test before the cut would mask the original bug.
diagnostic = sys.modules.get("environment_diagnostics")
local = sys.modules.get("local_llm")
if diagnostic is not None and local is not None:
    local.bounded_request = lambda url, *a, **k: (200, {}, json.dumps([{}, {}, {}] if url.endswith("/slots") else {}).encode())
    check_llm = diagnostic.check_llm
    def check_environment(cfg, **resources):
        assert resources["paths"].checkout == str(checkout)
        errors, _ = check_llm({})
        assert errors == []
        return ["synthetic baseline", "synthetic new"], []
    diagnostic.check_environment = check_environment
loaded = {name: module for name, module in sys.modules.items()
          if name in {"notify_spool", "notify_reconcile", "environment_diagnostics", "notify_flush",
                      "notify_sender", "local_llm", "llm_admission", "mcs_restore",
                      "present_discord", "present_slack", "present_lineworks", "release_apply_owner",
                      "release_apply_checks"}}
loaded.update({name: sys.modules[name] for name in ("provider_settings", "cloud_policy", "semantic_jev")})
loaded["cloud_inference"] = cloud_owner
loaded.update({owner.__name__: owner for owner in lifecycle_owners})
# processing_route reaches the shared owner only lazily through _route;
# importing it before the cut keeps this probe on the loaded generation.
import processing_route
known_contract = cloud_owner.is_supported_cloud_contract
gate_scope = updater._release_legacy_scope
gate_globals = gate_scope.__wrapped__.__globals__
gate_owner = sys.modules.get("release_apply_checks")
assert gate_owner is not None
assert gate_scope is sys.modules["release_apply_owner"]._legacy_scope
assert gate_globals is gate_owner.__dict__

def cut(args):
    assert args == ["reset", "--hard", "old"]
    trace.append("reset")
    shutil.rmtree(checkout / "mcs")
    (checkout / "mcs" / "core").mkdir(parents=True)
    (checkout / "mcs" / "core" / "ledger.py").write_text("raise AssertionError('mixed generation import')\n")
    assert updater._release_legacy_scope is gate_scope
    assert updater._release_legacy_scope.__wrapped__.__globals__ is gate_globals
    assert sys.modules.get("release_apply_checks") is gate_owner
    # The shared owner must stay the loaded generation and a lazy consumer
    # must still reach its real contract validation after the cut.
    assert sys.modules.get("cloud_inference") is cloud_owner
    called = []
    def spy(contract):
        called.append(True)
        return known_contract(contract)
    cloud_owner.is_supported_cloud_contract = spy
    try:
        contract = dict(cloud_owner._CLOUD_CONTRACT)
        row = {"kind": "extract_llm", "model": contract["model"]}
        meta = {**contract, "actual_model": contract["model"]}
        assert processing_route._route(row, meta, {"unit": 1}) == "cloud"
        assert called
    finally:
        cloud_owner.is_supported_cloud_contract = known_contract
    if case == "postcheck":
        # Shared restore gates must also remain the captured generation.
        journal = data / "update_state.json"
        original_journal = journal.read_bytes()
        backup_witness = data / "backup_restore_blocked.json"
        try:
            state = updater.journal_store.default_state()
            state["restore_consent"] = {"report_id": "synthetic"}
            updater.journal_store.save_state(str(journal), state)
            assert updater._restore_writer_hold_marker() is not None
            assert updater._restore_hold_active()
            updater.journal_store.save_state(str(journal), updater.journal_store.default_state())
            assert not (data / "restore_pending.json").exists()
            backup_witness.write_text("{}")
            assert updater._awaiting_consent_marker() == {"unreadable": True}
            assert updater._restore_writer_hold_marker() is not None
        finally:
            backup_witness.unlink(missing_ok=True)
            journal.write_bytes(original_journal)
patch_updater('_git_out', cut)
if case == "postcheck":
    cut(["reset", "--hard", "old"])
    errors = updater._postcheck({"baseline_check": ["synthetic baseline"]}, "old")
    assert errors == ["new_env_error: synthetic new"], errors
else:
    if case in ("rollback", "recover"):
        patch_updater('_check_runtime_restore', lambda value: None)
        patch_updater('_rollback_backup_error', lambda value: None)
        patch_updater('_read_manifest_snapshot', lambda: {})
        patch_updater('acquire_update_lock', lambda: os.open(root / "update-lock", os.O_CREAT | os.O_RDWR, 0o600))
        patch_updater('_acquire_run_lock_wait', lambda **kw: os.open(root / "run-lock", os.O_CREAT | os.O_RDWR, 0o600))
        patch_updater('quiesce', lambda: trace.append("quiesce"))
        patch_updater('restart_agents', forbidden)
        patch_updater('_enqueue_notice', forbidden)
        patch_updater('_remove_marker', forbidden)
        patch_updater('_report', lambda *a, **kw: trace.append("report"))
        patch_updater('_clean_stale_git_locks', lambda: 0)
        patch_updater('_last_report', lambda: {})
        if case == "rollback":
            state["applying"] = None
            state["applied"] = [entry]
        else:
            state["applying"] = {**entry, "sha": "old", "prev_sha": "new"}
            state["stages"] = [{"stage": "rollback", "at": 1}]
            cut(["reset", "--hard", "old"])
        updater.journal_store.save_state(updater.STATE_PATH, state)
        try:
            rc = updater.rollback() if case == "rollback" else updater.recover_interrupted()
        except OSError:
            # recover releases only locks when publication fails; it must
            # not restart writers or delete either marker on that path.
            assert case == "recover" and fault == "save_failure"
        else:
            assert rc in (1, 2)
        saved = original_load(updater.STATE_PATH)
        if not fault:
            assert saved["applying"]["restore_incomplete"] is True
        connection = sqlite3.connect(updater.LEDGER)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
            assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        finally:
            connection.close()
        assert sys.modules["notify_spool"].restore_pending(str(data))["phase"] == "restored"
        assert "services" not in trace
        print("synthetic cross-generation closure ok")
        sys.exit(0)
    try:
        updater._rollback_tree(entry, on_hold=lambda report: trace.append("hold"))
    except updater.RestoreConsentPending as pending:
        assert case == "hold"
        assert pending.report["backup_schema"] == 7
        assert pending.report["stored_since_backup"]["messages"] == 1
    except updater.UpdateError as blocked:
        assert case == "approved"
        assert str(blocked) == "restore_reconcile_schema_blocked"
    else:
        raise AssertionError("rollback must remain held")
    assert trace.index("runtime") < trace.index("reset") < trace.index("hold")
    marker = sys.modules["notify_spool"].restore_pending(str(data))
    connection = sqlite3.connect(updater.LEDGER)
    try:
        count = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    if case == "hold":
        assert marker["phase"] == "awaiting_consent"
        assert "swap" not in trace and "services" not in trace
        assert count == 3 and version == 9
    else:
        assert trace.index("hold") < trace.index("swap")
        assert "services" not in trace
        assert marker["phase"] == "restored" and count == 2
        assert version == 7
        saved = updater.journal_store.load_state(updater.STATE_PATH)
        assert saved["applying"]["restore_incomplete"] is True
        # A second pass sees matching live/backup versions. It must still
        # retry the blocked reconcile and never clear the hold or migrate.
        try:
            updater._restore_db(str(backup))
        except updater.UpdateError as blocked:
            assert str(blocked) == "restore_reconcile_schema_blocked"
        else:
            raise AssertionError("matching backup version must not finish")
        connection = sqlite3.connect(updater.LEDGER)
        try:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        finally:
            connection.close()
        assert sys.modules["notify_spool"].restore_pending(str(data)) == marker
for name, module in loaded.items():
    assert sys.modules.get(name) is module, name
assert {"notify_spool", "notify_reconcile", "environment_diagnostics", "notify_flush",
        "notify_sender", "local_llm", "llm_admission", "mcs_restore",
        "present_discord", "present_slack", "present_lineworks", "release_apply_owner",
        "release_apply_checks"} <= loaded.keys()
print("synthetic cross-generation closure ok")
'''


def _cold_child(tmp_path, case):
    source = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "checkout"
    shutil.copytree(source / "mcs", checkout / "mcs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    recovery = checkout / "deployment" / "recovery"
    recovery.mkdir(parents=True)
    (recovery / "mcs_recover.py").write_text("def request_gateway_restart(*args, **kwargs):\n    raise AssertionError('unexpected restart')\n")
    root = tmp_path / "runtime"
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", _CHILD, str(checkout), str(root), case],
        cwd=tmp_path, env={**os.environ, "MCS_ROOT": str(root)},
        capture_output=True, text=True, timeout=45)


@pytest.mark.parametrize("case", ["missing_gate", "hold", "approved", "postcheck", "rollback", "recover",
                                      "rollback:save_failure", "rollback:unreadable",
                                      "recover:save_failure", "recover:unreadable"])
def test_cold_updater_uses_loaded_restore_and_postcheck_after_old_tree_cut(tmp_path, case):
    result = _cold_child(tmp_path, case)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "synthetic cross-generation closure ok"


def test_cold_updater_fails_when_the_shared_cloud_owner_is_absent(tmp_path):
    """The preload is load-bearing: without cloud_inference in the synthetic
    tree the cold updater must refuse to run rather than mix generations."""
    result = _cold_child(tmp_path, "cloud_owner_missing")
    assert result.returncode != 0
    assert "cloud_inference" in result.stderr


@pytest.mark.parametrize("owner,phase,adopted,expected", [
    ("mcs_update", "restored", False, True),
    ("mcs_recover", "restored", False, True),
    ("manual", "restored", False, False),
    ("mcs_backup_restore", "awaiting_consent", False, True),
    ("mcs_backup_restore", "awaiting_consent", True, False),
    ("mcs_backup_restore", "restored", False, True),
    ("mcs_backup_restore", "restored", True, True),
])
def test_writer_hold_distinguishes_updater_restore_from_backup_adoption(
        tmp_path, monkeypatch, owner, phase, adopted, expected):
    import json
    import mcs_restore
    import mcs_update
    import notify_spool

    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_restore, "writers_resumed", lambda path: adopted)
    marker = {"v": 1, "by": owner, "phase": phase,
              "restored_at": 1, "report_id": "synthetic-report"}
    (tmp_path / "restore_pending.json").write_text(json.dumps(marker))
    assert (mcs_update._restore_writer_hold_marker() is not None) is expected
    # Delivery remains denied after adoption; it is not writer authority.
    assert notify_spool.restore_pending(str(tmp_path)) is not None
    assert json.loads((tmp_path / "restore_pending.json").read_text()) == marker


@pytest.mark.parametrize("owner,phase,adopted,expected", [
    ("mcs_update", "restored", False, True),
    ("mcs_recover", "restored", False, True),
    ("manual", "restored", False, False),
    ("mcs_backup_restore", "awaiting_consent", False, True),
    ("mcs_backup_restore", "awaiting_consent", True, False),
    ("mcs_backup_restore", "restored", False, True),
    ("mcs_backup_restore", "restored", True, True),
])
def test_shared_writer_hold_gate_matches_the_updater_reading(
        tmp_path, monkeypatch, owner, phase, adopted, expected):
    """standalone and CLI entry points read the same hold matrix through
    notify_spool.restore_writer_hold without importing the updater."""
    import json
    import mcs_restore
    import notify_spool

    monkeypatch.setattr(mcs_restore, "writers_resumed", lambda path: adopted)
    marker = {"v": 1, "by": owner, "phase": phase,
              "restored_at": 1, "report_id": "synthetic-report"}
    (tmp_path / "restore_pending.json").write_text(json.dumps(marker))
    assert (notify_spool.restore_writer_hold(str(tmp_path)) is not None) is expected


@pytest.mark.parametrize("missing", [False, True])
@pytest.mark.parametrize("owner", ["mcs_update", "mcs_recover"])
def test_empty_journal_retry_keeps_updater_restore_witness(tmp_path, monkeypatch,
                                                          missing, owner):
    import json
    import mcs_update

    state_path = tmp_path / "update_state.json"
    update_marker = tmp_path / "update_in_progress"
    restore_marker = tmp_path / "restore_pending.json"
    update_marker.write_text("synthetic update witness")
    marker = {"v": 1, "by": owner, "phase": "restored",
              "restored_at": 1, "report_id": "synthetic-report"}
    restore_marker.write_text(json.dumps(marker))
    if not missing:
        mcs_update.journal_store.save_state(
            str(state_path), mcs_update.journal_store.default_state())
    before = state_path.read_bytes() if state_path.exists() else None
    monkeypatch.setattr(mcs_update, "DATA", str(tmp_path))
    monkeypatch.setattr(update_apply, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_lifecycle_context, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_planning, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_preflight, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_recovery, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_restore, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(update_services, 'DATA', mcs_update.DATA)
    monkeypatch.setattr(mcs_update, "STATE_PATH", str(state_path))
    monkeypatch.setattr(update_apply, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_cli, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_execution, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_lifecycle_context, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_planning, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_preflight, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_recovery, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_restore, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(update_runtime_migration, 'STATE_PATH', mcs_update.STATE_PATH)
    monkeypatch.setattr(mcs_update, "MARKER_PATH", str(update_marker))
    monkeypatch.setattr(update_lifecycle_context, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(update_runtime_migration, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(update_services, 'MARKER_PATH', mcs_update.MARKER_PATH)
    monkeypatch.setattr(mcs_update, "acquire_update_lock", lambda: os.open(
        tmp_path / "update-lock", os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(update_apply, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(update_cli, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(update_recovery, 'acquire_update_lock', mcs_update.acquire_update_lock)
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait", lambda **kw: os.open(
        tmp_path / "run-lock", os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(update_apply, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    monkeypatch.setattr(update_recovery, '_acquire_run_lock_wait', mcs_update._acquire_run_lock_wait)
    reports = []
    monkeypatch.setattr(mcs_update, "_report",
                        lambda *args, **kwargs: reports.append((args, kwargs)))
    monkeypatch.setattr(update_apply, '_report', mcs_update._report)
    monkeypatch.setattr(update_recovery, '_report', mcs_update._report)
    monkeypatch.setattr(update_restore, '_report', mcs_update._report)
    def forbidden(*args, **kwargs):
        pytest.fail("unfinished restore must not restart or inspect the checkout")
    monkeypatch.setattr(mcs_update, "restart_agents", forbidden)
    monkeypatch.setattr(update_apply, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_recovery, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(update_runtime_migration, 'restart_agents', mcs_update.restart_agents)
    monkeypatch.setattr(mcs_update, "_clean_stale_git_locks", forbidden)
    monkeypatch.setattr(update_recovery, '_clean_stale_git_locks', mcs_update._clean_stale_git_locks)
    monkeypatch.setattr(mcs_update, "_enqueue_notice", forbidden)
    monkeypatch.setattr(update_apply, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_cli, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_recovery, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_runtime_migration, '_enqueue_notice', mcs_update._enqueue_notice)
    monkeypatch.setattr(update_services, '_enqueue_notice', mcs_update._enqueue_notice)
    assert mcs_update.recover_interrupted() == 1
    assert update_marker.read_text() == "synthetic update witness"
    assert json.loads(restore_marker.read_text()) == marker
    assert (state_path.read_bytes() if state_path.exists() else None) == before
    assert reports[-1][0][0] == "restore_reconcile_blocked"
    assert reports[-1][1]["writers_stopped"] is True
