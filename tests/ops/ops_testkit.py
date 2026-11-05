"""Shared synthetic fixtures for the ops test family — the seeded
source ledger/snapshot and request-create command, the signal-layer
message/extract_v1 row writers, the updater's temp git repos, schema
DBs and restore-consent receipts, and the path loader for the
repo-external recovery tool.  Not a test module (no ``test_`` prefix);
sibling files import it via the tests/ sys.path bootstrap."""
import update_execution
import update_lifecycle_context
import update_planning
import update_preflight
import update_recovery
import update_restore
import restore_evidence
import hashlib
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import suppress
from pathlib import Path

import extract
import ledger
import mcs_update
import mcs_view
from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_contract import Attachment, Message


def _source(tmp_path):
    db = ledger.Ledger(str(tmp_path / "source.db"))
    for pid in (1, 2, 3):
        db.ensure_patient(pid)
    for mid, pid, parent, state in ((1, 1, None, "full"), (2, 1, None, "unknown"),
                                    (3, 1, 1, "full"), (4, 1, None, "full"), (5, 2, None, "full")):
        msg = Message(mid, pid, parent, 1, "synthetic sender", "user", "", "",
                      "2026-09-19T00:00:00+09:00" if mid != 4 else "",
                      "<p>確認お願いします literal %_</p>", state, False, 0)
        if mid == 1:
            msg.reply_count = 2
            msg.attachments = [Attachment("file", "synthetic.txt", "https://invalid.test/secret-signed")]
        db.save_messages([msg])
    db.set_history_floor(2, 0)
    db.set_history_floor(3, 100)
    with db.db:
        db.db.execute("UPDATE patients SET url='https://www.medical-care.net/projects/medical/1',fetch_reason='schema_error' WHERE project_id=1")
    extract.run_pending(db)
    return db


def _snapshot(db, tmp_path):
    path = ledger.publish_snapshot(str(tmp_path / "source.db"), str(tmp_path / "snapshots"))
    return mcs_view.View(path)


def _create(db, **changes):
    return {"version": 1, "cmd": "request.create", "command_id": str(uuid.uuid4()),
            "actor": "synthetic reviewer", "human_confirmed": True, "project_id": 1,
            "source_message_id": 1,
            "source_hash": db.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0],
            "title": "synthetic confirmed request", "assignee": "synthetic owner",
            "due_date": "2026-09-30", "reason": "synthetic confirmation",
            **changes}


NOW = 1789975073.0   # 2026-09-21 JST
DAY = 86400


def _msg(db, mid, pid=1, ts=NOW - 30 * DAY, chash="h1", body="b",
         prof="看護師", org="org", parent=None):
    db.execute(
        "INSERT INTO messages(message_id,project_id,parent_id,sender_id,"
        "sender_name,sender_type,profession,organization,posted_at,"
        "posted_at_ts,body_text,body_state,content_hash,reply_count,"
        "is_unread,first_seen,updated_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (mid, pid, parent, 1, "n", "staff", prof, org,
         "2026-08-22T10:00:00+09:00", ts, body, "full", chash, 0, 0,
         ts, ts))
    db.execute(
        "INSERT OR IGNORE INTO patients(project_id,is_archived,"
        "created_at,last_seen) VALUES (?,0,?,?)", (pid, ts, ts))


def _extract_v1(db, mid, chash, periods):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',?,?,?)",
        (mid, json.dumps({"med_periods": periods}),
         json.dumps({"hash": chash, VERSION_KEY: CURRENT_VERSION})))


def _git(repo, *args, check=True):
    r = subprocess.run(["git", "-C", repo, *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr}")
    return r


def _make_repo(tmp_path):
    """A repo with v1.0.0 (lightweight) and v1.1.0 (annotated) tags."""
    bare = tmp_path / "remote.git"
    work = tmp_path / "remote-work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "f.txt").write_text("one")
    _git(work, "add", ".")
    _git(work, "commit", "-qm", "c1")
    _git(work, "tag", "v1.0.0")            # lightweight — no peel line
    (work / "f.txt").write_text("two")
    _git(work, "commit", "-qam", "c2")
    _git(work, "tag", "-a", "v1.1.0", "-m", "release")   # annotated
    _git(work, "init", "-q", "--bare", "-b", "main", str(bare))
    _git(work, "push", "-q", str(bare), "main",
         "v1.0.0", "v1.1.0")

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", str(bare), str(repo)],
                   check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    return repo, bare


def _receipts_db(path):
    con = sqlite3.connect(path)
    con.execute("""CREATE TABLE IF NOT EXISTS command_receipts(
      command_id TEXT PRIMARY KEY, payload_hash TEXT, project_id INTEGER,
      request_id INTEGER,
      outcome TEXT CHECK(outcome IN ('applied','rejected')),
      receipt_json TEXT, processed_at REAL)""")
    return con


def _seed_consent(ledger_path, backup_path, cid="cid-consent",
                  report=None):
    """Drop an ops.restore_approve receipt bound to the CURRENT loss
    report into the live DB — the same row the human-approval path
    commits. Returns the report it was bound to."""
    if report is None:
        report = restore_evidence.create_loss_report(live_path=mcs_update.LEDGER, backup_path=backup_path, report_path=mcs_update.RESTORE_REPORT_PATH)
    con = _receipts_db(ledger_path)
    con.execute(
        "INSERT OR REPLACE INTO command_receipts VALUES(?,?,NULL,NULL,"
        "'applied',?,?)",
        (cid, "h" * 64, json.dumps({
            "cmd": "ops.restore_approve", "scheduled": True,
            "command_id": cid, "report_id": report["report_id"],
            "backup_sha256": report["backup_sha256"],
            "backup_schema": report["backup_schema"]}), time.time()))
    con.commit()
    con.close()
    return report


def _mk_schema(path, version, messages=0):
    """A real Ledger-created DB pinned to `version` — passes the real
    valid_mcs_db gate, so consent tests exercise the true restore path."""
    import ledger as _ledger
    lg = _ledger.Ledger(str(path))
    lg.db.execute(f"PRAGMA user_version={version}")
    for i in range(messages):
        lg.db.execute(
            "INSERT INTO messages(message_id,project_id,posted_at,"
            "posted_at_ts,body_html,body_state,content_hash,first_seen)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (i + 1, 1, "2026-01-01", 100 + i, "<b>x</b>", "full",
             f"h{i}", 1))
    lg.db.commit()
    lg.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    lg.db.close()
    for side in (str(path) + "-wal", str(path) + "-shm"):
        with suppress(OSError):
            os.unlink(side)


def _load():
    path = (Path(__file__).resolve().parents[2]
            / "deployment" / "recovery" / "mcs_recover.py")
    spec = importlib.util.spec_from_file_location("mcs_recover", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def bounded_python(tmp_path, program, *args):
    """Run one synthetic assertion with test guards and a bounded, reaped child."""
    root = Path(__file__).resolve().parents[2]
    ready = tmp_path / "bounded-child-ready"
    bootstrap = """
import pathlib, runpy, sys
runpy.run_path(sys.argv[1])
pathlib.Path(sys.argv[2]).write_text('ready')
sys.argv = [sys.argv[0], *sys.argv[3:]]
"""
    try:
        result = subprocess.run(
            [sys.executable, "-I", "-c", bootstrap + program,
             str(root / "tests/conftest.py"), str(ready), *map(str, args)],
            env={**os.environ, "MCS_ROOT": str(tmp_path)},
            capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired as error:
        assert ready.is_file(), "synthetic child never reached the checked operation"
        raise AssertionError("synthetic operation exceeded its 5-second deadline") from error
    assert result.returncode == 0, result.stderr
    return result


def select_updater_service_python(monkeypatch, updater, python):
    """Select one synthetic interpreter without masking caller-owned path facts."""
    from dataclasses import replace
    factory = updater._service_paths
    monkeypatch.setattr(updater, "_service_paths",
                        lambda: replace(factory(), hermes_python=python))
    monkeypatch.setattr(update_execution, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_lifecycle_context, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_planning, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_preflight, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_recovery, '_service_paths', updater._service_paths)
    monkeypatch.setattr(update_restore, '_service_paths', updater._service_paths)


class _FakeLaunchd:
    """launchctl stub: bootstrap exit codes come from `outcomes`; a
    successful bootstrap (or `late_load`) marks the label loaded."""

    def __init__(self, outcomes, late_load=False, loads=True):
        self.outcomes = list(outcomes)
        self.late_load = late_load
        self.loads = loads
        self.loaded = False
        self.calls = []

    def __call__(self, argv, *args, **kwargs):
        verb = argv[1]
        self.calls.append(verb)
        rc, out, err = 0, "", ""
        if verb == "bootout":
            self.loaded = False
        elif verb == "bootstrap":
            rc = self.outcomes.pop(0)
            if rc == 0:
                self.loaded = self.loads
            else:
                err = "Bootstrap failed: 5: Input/output error"
                out = "success"          # misleading output is ignored
                if not self.outcomes and self.late_load:
                    self.loaded = True
        elif verb == "print":
            rc = 0 if self.loaded else 113
            out = "\tpid = 4242\n" if self.loaded else ""
        return subprocess.CompletedProcess(argv, rc, out, err)


def _tree(root):
    """Witness contents, names, modes and nanosecond mtimes, not access times."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        st = path.lstat()
        content = (os.readlink(path) if path.is_symlink() else
                   hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
        result[str(path.relative_to(root))] = (st.st_mode, st.st_mtime_ns, st.st_size, content)
    return result


def git_reads_or(stub):
    """run_bounded stand-in for diagnostics tests: only the two read-only git calls of
    environment_diagnostics.source_status reach the real bounded runner (and through it
    any lifecycle_io.subprocess.run patch); every other argv, any other git included,
    goes to ``stub`` so tests keep forbidding service access."""
    import lifecycle_io
    real = lifecycle_io.run_bounded

    def run(argv, **kwargs):
        if (len(argv) >= 4 and argv[0] == "git" and argv[1] == "-C"
                and argv[3:] in (["rev-parse", "HEAD"],
                                 ["status", "--porcelain", "--untracked-files=no"])):
            return real(argv, **kwargs)
        return stub(argv, **kwargs)
    return run


def in_process_post_merge(updater, monkeypatch):
    """Run the post-merge stage in-process with the update.lock the real parent hands its child."""
    def run(expected, *, run_lock_fd, **_kw):
        monkeypatch.setenv(updater.UPDATE_LOCK_FD_ENV, str(updater._POST_MERGE_UPDATE_FD.get()))
        return updater._post_merge(updater.journal_store.load_state(updater.STATE_PATH))
    return run
