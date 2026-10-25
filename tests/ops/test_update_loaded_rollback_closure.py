"""Cold updater retains its finite rollback closure after a synthetic old-tree cut."""
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
import mcs_update as updater
import ledger
root.mkdir()
data = root / "data"
data.mkdir()
updater.HOME, updater.DATA = str(root), str(data)
updater.REPO = str(checkout)
updater.LEDGER = str(data / "ledger.db")
updater.STATE_PATH = str(data / "update_state.json")
updater.RESTORE_REPORT_PATH = str(data / "restore_report.json")
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
updater._restore_runtime = lambda value: trace.append("runtime")
updater._rollback_layout_error = lambda value: None
updater._local_changes_outside = lambda *a: []
updater._head_sha = lambda: "old"
updater._tree_clean = lambda: True
updater._cancel_unsent_notices = lambda: 0
updater._services_reconcile = lambda: trace.append("services")
updater.service_management.retire_to_snapshot = lambda *a, **k: []
updater._agent_pid = lambda label: 1
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
updater._replace_database = swap
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
                      "present_discord", "present_slack", "present_lineworks", "release_apply_owner"}}

def cut(args):
    assert args == ["reset", "--hard", "old"]
    trace.append("reset")
    shutil.rmtree(checkout / "mcs")
    (checkout / "mcs" / "core").mkdir(parents=True)
    (checkout / "mcs" / "core" / "ledger.py").write_text("raise AssertionError('mixed generation import')\n")
updater._git_out = cut
if case == "postcheck":
    cut(["reset", "--hard", "old"])
    errors = updater._postcheck({"baseline_check": ["synthetic baseline"]}, "old")
    assert errors == ["new_env_error: synthetic new"], errors
else:
    if case in ("rollback", "recover"):
        updater._check_runtime_restore = lambda value: None
        updater._rollback_backup_error = lambda value: None
        updater._read_manifest_snapshot = lambda: {}
        updater.acquire_update_lock = lambda: os.open(root / "update-lock", os.O_CREAT | os.O_RDWR, 0o600)
        updater._acquire_run_lock_wait = lambda **kw: os.open(root / "run-lock", os.O_CREAT | os.O_RDWR, 0o600)
        updater.quiesce = lambda: trace.append("quiesce")
        updater.restart_agents = forbidden
        updater._enqueue_notice = forbidden
        updater._remove_marker = forbidden
        updater._report = lambda *a, **kw: trace.append("report")
        updater._clean_stale_git_locks = lambda: 0
        updater._last_report = lambda: {}
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
        "present_discord", "present_slack", "present_lineworks", "release_apply_owner"} <= loaded.keys()
print("synthetic cross-generation closure ok")
'''


@pytest.mark.parametrize("case", ["hold", "approved", "postcheck", "rollback", "recover",
                                      "rollback:save_failure", "rollback:unreadable",
                                      "recover:save_failure", "recover:unreadable"])
def test_cold_updater_uses_loaded_restore_and_postcheck_after_old_tree_cut(tmp_path, case):
    source = Path(__file__).resolve().parents[2]
    checkout = tmp_path / "checkout"
    shutil.copytree(source / "mcs", checkout / "mcs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    recovery = checkout / "deployment" / "recovery"
    recovery.mkdir(parents=True)
    (recovery / "mcs_recover.py").write_text("def request_gateway_restart(*args, **kwargs):\n    raise AssertionError('unexpected restart')\n")
    root = tmp_path / "runtime"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _CHILD, str(checkout), str(root), case],
        cwd=tmp_path, env={**os.environ, "MCS_ROOT": str(root)},
        capture_output=True, text=True, timeout=45, check=True)
    assert result.stdout.strip() == "synthetic cross-generation closure ok"


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
    monkeypatch.setattr(mcs_restore, "writers_resumed", lambda path: adopted)
    marker = {"v": 1, "by": owner, "phase": phase,
              "restored_at": 1, "report_id": "synthetic-report"}
    (tmp_path / "restore_pending.json").write_text(json.dumps(marker))
    assert (mcs_update._restore_writer_hold_marker() is not None) is expected
    # Delivery remains denied after adoption; it is not writer authority.
    assert notify_spool.restore_pending(str(tmp_path)) is not None
    assert json.loads((tmp_path / "restore_pending.json").read_text()) == marker


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
    monkeypatch.setattr(mcs_update, "STATE_PATH", str(state_path))
    monkeypatch.setattr(mcs_update, "MARKER_PATH", str(update_marker))
    monkeypatch.setattr(mcs_update, "acquire_update_lock", lambda: os.open(
        tmp_path / "update-lock", os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(mcs_update, "_acquire_run_lock_wait", lambda **kw: os.open(
        tmp_path / "run-lock", os.O_CREAT | os.O_RDWR, 0o600))
    reports = []
    monkeypatch.setattr(mcs_update, "_report",
                        lambda *args, **kwargs: reports.append((args, kwargs)))
    def forbidden(*args, **kwargs):
        pytest.fail("unfinished restore must not restart or inspect the checkout")
    monkeypatch.setattr(mcs_update, "restart_agents", forbidden)
    monkeypatch.setattr(mcs_update, "_clean_stale_git_locks", forbidden)
    monkeypatch.setattr(mcs_update, "_enqueue_notice", forbidden)
    assert mcs_update.recover_interrupted() == 1
    assert update_marker.read_text() == "synthetic update witness"
    assert json.loads(restore_marker.read_text()) == marker
    assert (state_path.read_bytes() if state_path.exists() else None) == before
    assert reports[-1][0][0] == "restore_reconcile_blocked"
    assert reports[-1][1]["writers_stopped"] is True
