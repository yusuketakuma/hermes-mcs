"""Repo-contained, fully synthetic released-schema upgrade/recovery matrix.

DDL origins and the limits of historical evidence are in origins.json. Tests
need neither Git history nor a live database. Service/Git boundaries in public
rollback are stubbed; SQLite migration, backup, consent, replacement,
reconciliation, locks and updater bookkeeping remain real.
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
from typing import TypedDict

import pytest

import ledger
import maintenance
import mcs_setup
import mcs_update
import mcs_view
import notify_cards
import notify_transport
from ops_testkit import _seed_consent


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "schema_upgrade"
ORIGINS = json.loads((FIXTURES / "origins.json").read_text(encoding="utf-8"))
RELEASES = ORIGINS["releases"]
HISTORICAL = [*RELEASES, *ORIGINS["pre_release_shapes"]]
ATTACHMENT = b"synthetic attachment data\n"


class Origin(TypedDict):
    release: str
    fixture: str
    fixture_sha256: str
    schema_version: int


Records = dict[str, tuple[
    tuple[str, ...], list[tuple[str | int | float | bytes | None, ...]]]]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def _historical(root: Path, origin: Origin) -> Path:
    """Build a genuine historical shape without calling the current writer."""
    root.mkdir(mode=0o700, parents=True)
    path = root / "ledger.db"
    ddl = (FIXTURES / origin["fixture"]).read_bytes()
    assert hashlib.sha256(ddl).hexdigest() == origin["fixture_sha256"]
    with closing(sqlite3.connect(path)) as db:
        db.executescript(ddl.decode())
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript((FIXTURES / "core-seed.sql").read_text())
        if origin["schema_version"] >= 7:
            db.execute("UPDATE patients SET is_archived=1 WHERE project_id=102")
            db.execute("UPDATE messages SET notified_at=111 WHERE message_id=201")
        tables = {r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "notification_cards" in tables:
            db.executescript((FIXTURES / "notification-seed.sql").read_text())
        if "message_metadata" in tables:
            db.executescript((FIXTURES / "metadata-seed.sql").read_text())
        if "thread_read_marks" in tables:
            db.execute("INSERT INTO thread_read_marks VALUES(201,101,202,'confirmed',111)")
        db.commit()
        assert db.execute("PRAGMA user_version").fetchone()[0] == origin["schema_version"]
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        columns = {r[1] for r in db.execute("PRAGMA table_info(fetch_jobs)")}
        assert ("reason_code" in columns) is (origin["schema_version"] >= 9)
        if origin["schema_version"] >= 9:
            db.execute("UPDATE fetch_jobs SET reason_code='network_error' WHERE job_id=502")
            db.commit()
    (root / "attachments").mkdir()
    (root / "attachments" / "synthetic.txt").write_bytes(ATTACHMENT)
    assert ledger.valid_mcs_db(str(path))
    return path


def _records(path: Path) -> Records:
    """Capture every original column, not just counts or handpicked fields."""
    with closing(_connect(path)) as db:
        tables = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT GLOB 'messages_fts_*' "
            "AND name != 'snapshot_meta' ORDER BY name")]
        return {
            table: (
                tuple(r[1] for r in db.execute(f"PRAGMA table_info({table})")),
                db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall(),
            ) for table in tables}


def _preserved(path: Path, expected: Records) -> None:
    with closing(_connect(path)) as db:
        for table, (columns, rows) in expected.items():
            names = ",".join(f'"{column}"' for column in columns)
            assert db.execute(
                f'SELECT {names} FROM "{table}" ORDER BY rowid').fetchall() == rows, table
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'upgrade' "
            "ORDER BY rowid").fetchall() == [(201,), (202,)]
        for html, digest in db.execute("SELECT body_html,content_hash FROM messages"):
            assert hashlib.sha256(html.encode()).hexdigest() == digest
        attachment = db.execute(
            "SELECT local_path,bytes,sha256 FROM attachments WHERE attachment_id=401"
        ).fetchone()
        assert (path.parent / attachment[0]).read_bytes() == ATTACHMENT
        assert attachment[1:] == (len(ATTACHMENT), hashlib.sha256(ATTACHMENT).hexdigest())


@pytest.mark.parametrize("origin", HISTORICAL, ids=lambda o: o["release"])
def test_released_schema_upgrade_preserves_original_records(tmp_path, origin):
    source = _historical(tmp_path / "source", origin)
    original_hash = _sha(source)
    expected = _records(source)
    working = tmp_path / "working"
    shutil.copytree(source.parent, working)
    migrated = working / source.name

    db = ledger.Ledger(str(migrated))
    try:
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        assert [tuple(row) for row in db.db.execute(
            "SELECT job_id,reason_code FROM fetch_jobs ORDER BY job_id")
        ] == [(501, None), (502, "network_error" if origin["schema_version"] >= 9 else None)]
    finally:
        db.close()

    _preserved(migrated, expected)
    assert _sha(source) == original_hash
    assert ledger.valid_mcs_db(str(migrated))
    once = _records(migrated)
    reopened = ledger.Ledger(str(migrated))
    reopened.close()
    _preserved(migrated, once)
    # Exercise the real trigger-maintained index after migration as well.
    writer = ledger.Ledger(str(migrated))
    try:
        with writer.db:
            writer.db.execute("UPDATE messages SET body_text='synthetic indexprobe' "
                              "WHERE message_id=202")
        assert [r[0] for r in writer.db.execute(
            "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'indexprobe'")
        ] == [202]
    finally:
        writer.close()


@pytest.mark.parametrize("origin", HISTORICAL, ids=lambda o: o["release"])
def test_released_backup_snapshot_and_restore_are_nonmutating(tmp_path, monkeypatch, origin):
    source = _historical(tmp_path / "source", origin)
    expected, original_hash = _records(source), _sha(source)
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance.time, "strftime", lambda fmt: "synthetic-stamp")
    # Both interfaces must handle the genuine old static schema, not a
    # latest-schema database with only its user_version changed.
    backup = Path(maintenance.preupdate_backup(str(source)))
    maintenance.daily_backup(str(source))
    daily = backups / "ledger-synthetic-stamp.db"
    snapshot = Path(ledger.publish_snapshot(str(source), str(tmp_path / "snapshots")))
    for copy in (backup, daily, snapshot):
        with closing(_connect(copy)) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == origin["schema_version"]
        shutil.copytree(source.parent / "attachments", copy.parent / "attachments",
                        dirs_exist_ok=True)
        _preserved(copy, expected)
        assert ledger.valid_mcs_db(str(copy))
    assert _sha(source) == original_hash
    reader = mcs_view.View(str(snapshot))
    try:
        assert [item["message_id"] for item in reader.read(
            "thread", project=101, message_id=201)["items"]] == [202]
    finally:
        reader.close()
    # Manual restore uses the verified backup in a new ephemeral root.
    restored = tmp_path / "restored"
    restored.mkdir()
    shutil.copy2(backup, restored / "ledger.db")
    shutil.copytree(source.parent / "attachments", restored / "attachments")
    backup_hash = _sha(backup)
    db = ledger.Ledger(str(restored / "ledger.db"))
    db.close()
    _preserved(restored / "ledger.db", expected)
    assert _sha(source) == original_hash
    assert _sha(backup) == backup_hash


@pytest.fixture
def updater(tmp_path, monkeypatch):
    """Point every updater-owned path at this test's private root."""
    root = tmp_path / "update"
    root.mkdir(mode=0o700)
    data = root / "data"
    data.mkdir(mode=0o700)
    from test_runtime_compatibility import _facts
    (root / "venv/bin").mkdir(parents=True)
    selected = root / "venv/bin/python3"
    selected.touch()
    selected.chmod(0o700)
    # The independent recovery interpreter lives outside the updated tree.
    recovery = tmp_path / "recovery-runtime/bin/python3"
    recovery.parent.mkdir(parents=True)
    recovery.touch()
    recovery.chmod(0o700)
    agents = root / "agents"
    agents.mkdir(mode=0o700)
    import plistlib
    (agents / "org.mcs.recovery.plist").write_bytes(plistlib.dumps(
        {"ProgramArguments": [str(recovery), "synthetic-recovery.py"]}))
    monkeypatch.setattr(mcs_setup, "HOME", str(root))
    monkeypatch.setattr(mcs_setup, "HERMES_PY", str(selected))
    monkeypatch.setattr(mcs_setup, "AGENTS_DIR", str(agents))
    monkeypatch.setattr(mcs_setup, "_runtime_probe", lambda exe: _facts())
    paths = {
        "REPO": root, "DATA": data, "LEDGER": data / "ledger.db",
        "STATE_PATH": data / "update_state.json", "UPDATE_LOCK": data / "update.lock",
        "RUN_LOCK": data / "run.lock", "MARKER_PATH": data / "update_in_progress",
        "REPORT_PATH": data / "recovery_report.json",
        "RESTORE_REPORT_PATH": data / "restore_report.json",
        "MANIFEST_PATH": data / "service_manifest.json", "BACKUP_DIR": data / "backups",
    }
    for name, path in paths.items():
        monkeypatch.setattr(mcs_update, name, str(path))
    monkeypatch.setattr(mcs_update, "load_config", lambda: {})
    monkeypatch.setattr(mcs_update, "_enqueue_notice", lambda *a, **kw: True)
    monkeypatch.setattr(mcs_update, "_services_reconcile", lambda: None)
    monkeypatch.setattr(mcs_update, "_reconcile_membership", lambda manifest: [])
    monkeypatch.setattr(mcs_setup, "check_environment", lambda cfg: ([], []))
    monkeypatch.setattr(mcs_update, "_agent_pid", lambda label: 12345)
    return mcs_update


@pytest.mark.parametrize("origin", HISTORICAL, ids=lambda o: o["release"])
def test_released_schema_update_rollback_requires_bound_consent(
        tmp_path, monkeypatch, updater, origin):
    source = _historical(tmp_path / "source", origin)
    expected, original_hash = _records(source), _sha(source)
    live = Path(updater.LEDGER)
    shutil.copy2(source, live)
    shutil.copytree(source.parent / "attachments", live.parent / "attachments")
    monkeypatch.setattr(maintenance, "BACKUP_DIR", updater.BACKUP_DIR)
    backup = Path(maintenance.preupdate_backup(str(live)))
    backup_hash = _sha(backup)
    db = ledger.Ledger(str(live))
    with db.db:
        db.db.execute(
            "INSERT INTO messages(message_id,project_id,body_html,body_text,"
            "body_state,content_hash,posted_at_ts,first_seen) "
            "VALUES(203,101,'<p>synthetic later</p>','synthetic later','full',?,"
            "1788220920,200)", (hashlib.sha256(b"<p>synthetic later</p>").hexdigest(),))
    db.close()
    live_hash = _sha(live)
    head, resets, restarts = ["b" * 40], [], []

    def git_out(args, **kwargs):
        assert args == ["reset", "--hard", "a" * 40]
        resets.append(args)
        head[0] = "a" * 40
        return ""

    monkeypatch.setattr(updater, "_git_out", git_out)
    monkeypatch.setattr(updater, "_head_sha", lambda: head[0])
    monkeypatch.setattr(updater, "_tree_clean", lambda: True)
    monkeypatch.setattr(updater, "quiesce", lambda: updater._write_marker())
    monkeypatch.setattr(updater, "restart_agents",
                        lambda bounce=True: restarts.append(bounce) or [])
    state = updater._default_state()
    bump = origin["schema_version"] < ledger.SCHEMA_VERSION
    state["applied"] = [{
        "tag": origin["release"], "sha": "b" * 40, "prev_sha": "a" * 40,
        "schema_bump": bump, "backup_path": str(backup), "at": 100}]
    updater.save_state(state)

    if not bump:
        latest = _records(live)
        assert updater.rollback("synthetic-rollback") == 0
        assert head[0] == "a" * 40 and restarts == [True]
        assert not Path(updater.RESTORE_REPORT_PATH).exists()
        _preserved(live, latest)
        assert _sha(source) == original_hash and _sha(backup) == backup_hash
        return

    assert updater.rollback("synthetic-rollback") == 2
    assert _sha(live) == live_hash
    assert _sha(backup) == backup_hash
    assert restarts == []
    assert Path(updater.MARKER_PATH).exists()
    marker = notify_cards.restore_awaiting_consent(str(live.parent))
    report = json.loads(Path(updater.RESTORE_REPORT_PATH).read_text())
    assert marker["report_id"] == report["report_id"]
    assert report["backup_schema"] == origin["schema_version"]
    assert report["backup_sha256"] == backup_hash
    assert report["stored_since_backup"]["messages"] == 1
    assert "synthetic-rollback" not in updater.load_state()["executed"]
    with closing(sqlite3.connect(live)) as con:
        con.row_factory = sqlite3.Row
        denied = notify_transport.apply_transport_begin(
            con, {"command_id": "synthetic-begin-while-held"}, {})
        assert denied["granted"] is False
        assert denied["error"] == "denied_restore_pending"
    _seed_consent(str(live), str(backup), report=report)

    assert updater.recover_interrupted() == 0
    assert len(resets) >= 1
    assert restarts == [True]
    assert not Path(updater.MARKER_PATH).exists()
    assert notify_cards.restore_pending(str(live.parent)) is None
    after = updater.load_state()
    assert after["applying"] is None
    assert after["stages"] == []
    assert after["executed"]["synthetic-rollback"]["result"] == "rolled_back"
    with closing(_connect(live)) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        assert con.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
        event = con.execute(
            "SELECT state,next_try,progress FROM notify_outbox WHERE event_id=802"
        ).fetchone()
        assert event[0:2] == ("failed", None)
        assert json.loads(event[2])["hold_reason"] == "restore_text_unverified"
        if "notification_restore_holds" in expected:
            assert con.execute(
                "SELECT reason FROM notification_restore_holds WHERE hold_id=903"
            ).fetchone()[0] == "synthetic-historical-hold"
    # Reconciliation intentionally changes only disputed delivery records.
    durable = {table: rows for table, rows in expected.items()
               if table != "notify_outbox" and not table.startswith("notification_")}
    _preserved(live, durable)
    assert _sha(backup) == backup_hash
    assert _sha(source) == original_hash


@pytest.mark.parametrize("invalid", ["future", "corrupt", "incompatible", "interrupted"])
def test_invalid_recovery_candidate_is_refused_without_replacing_live(
        tmp_path, updater, invalid):
    source = _historical(tmp_path / "source", RELEASES[-1])
    live = Path(updater.LEDGER)
    shutil.copy2(source, live)
    candidate = tmp_path / "candidate.db"
    shutil.copy2(source, candidate)
    if invalid == "corrupt":
        candidate.write_bytes(b"fully synthetic non-SQLite bytes")
    else:
        with closing(sqlite3.connect(candidate)) as db:
            if invalid == "future":
                db.execute(f"PRAGMA user_version={ledger.SCHEMA_VERSION + 1}")
            elif invalid == "incompatible":
                db.execute("ALTER TABLE patients DROP COLUMN project_type")
            else:
                db.execute("CREATE TABLE attachments_v1(synthetic TEXT)")
            db.commit()
    original_hash, candidate_hash = _sha(live), _sha(candidate)

    assert not ledger.valid_mcs_db(str(candidate))
    with pytest.raises(updater.UpdateError, match="backup_invalid"):
        updater._restore_db(str(candidate))
    assert _sha(live) == original_hash
    assert _sha(candidate) == candidate_hash
    assert notify_cards.restore_pending(str(live.parent)) is None
    if invalid != "incompatible":
        with pytest.raises((ledger.MigrationError, sqlite3.DatabaseError)):
            ledger.Ledger(str(candidate))
        assert _sha(candidate) == candidate_hash
