"""Preupdate backup retention/disk check, overdue
housekeeping, batched attachment prune, leftover cleanup, log targets
and lookup indexes. Temp files and synthetic rows only."""
import json
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace

import pytest

import ledger
import maintenance
from ingest_testkit import _ledger, _message


def _old(path, age=None):
    t = time.time() - (age or maintenance.PREUPDATE_GRACE_S + 60)
    os.utime(path, (t, t))


def test_prune_keeps_only_actionable_preupdate_backups(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    names = ["old-bump", "new-bump", "plain", "applying", "consent", "orphan"]
    paths = {n: backups / f"preupdate-{n}.db" for n in names}
    for p in paths.values():
        p.write_bytes(b"synthetic")
        _old(p)
    state = {
        "applied": [
            {"backup_path": str(paths["old-bump"]), "schema_bump": True},
            {"backup_path": str(paths["new-bump"]), "schema_bump": True},
            {"backup_path": str(paths["plain"]), "schema_bump": False},
        ],
        "applying": {"backup_path": str(paths["applying"])},
        "restore_consent": {"backup_path": str(paths["consent"])},
    }
    state_path = tmp_path / "update_state.json"
    state_path.write_text(json.dumps(state))
    assert maintenance.prune_preupdate_backups(str(state_path)) == 2
    assert sorted(p.name for p in backups.iterdir()) == [
        "preupdate-applying.db", "preupdate-consent.db", "preupdate-new-bump.db",
        "preupdate-old-bump.db"]


def test_preupdate_backup_refuses_when_disk_is_low(tmp_path, monkeypatch):
    src = tmp_path / "ledger.db"
    ledger.Ledger(str(src)).close()
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(maintenance.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=0))
    with pytest.raises(maintenance.MaintenanceError, match="backup_disk_low"):
        maintenance.preupdate_backup(str(src))
    assert os.listdir(tmp_path / "backups") == []


def test_backup_overdue_only_with_a_stale_newest_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(tmp_path))
    assert maintenance.backup_overdue() is False          # fresh install
    old = tmp_path / "ledger-20260101.db"
    old.write_bytes(b"x")
    _old(old, maintenance.BACKUP_OVERDUE_S + 60)
    assert maintenance.backup_overdue() is True
    (tmp_path / "ledger-20260102.db").write_bytes(b"x")
    assert maintenance.backup_overdue() is False


def test_prune_attachments_commits_before_unlinking(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(1), _message(2)])
    old = time.time() - maintenance.ATTACHMENT_KEEP_S - 60
    att = tmp_path / "attachments"
    att.mkdir()
    for aid in (1, 2):
        path = att / str(aid)
        path.write_bytes(b"synthetic")
        db.db.execute(
            "INSERT INTO attachments(attachment_id,message_id,name,local_path,"
            "state,downloaded_at) VALUES(?,?,'f.pdf',?,'downloaded',?)",
            (aid, aid, str(path), old))
    db.db.commit()
    db.close()
    monkeypatch.setattr(maintenance, "PRUNE_BATCH", 1)
    real_unlink = os.unlink
    seen = []

    def unlink(path, *a, **k):
        with closing(sqlite3.connect(tmp_path / "ledger.db")) as other, other:
            seen.append(other.execute(
                "SELECT state,local_path FROM attachments WHERE attachment_id=?",
                (int(os.path.basename(path)),)).fetchone())
        real_unlink(path, *a, **k)

    monkeypatch.setattr(maintenance.os, "unlink", unlink)
    assert maintenance.prune_attachments(str(tmp_path / "ledger.db")) == 2
    assert seen == [("pruned", str(att / "1")), ("pruned", str(att / "2"))]
    assert not (att / "1").exists() and not (att / "2").exists()


def test_prune_leftovers_removes_only_day_old_temp_and_invalid(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    att = tmp_path / "data" / "attachments"
    cmd = tmp_path / "data" / "cmd"
    att.mkdir(parents=True)
    cmd.mkdir()
    files = [att / ".download-a.part", cmd / "x.json.invalid",
             att / "7", cmd / "y.json"]
    for f in files:
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # all fresh
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == 2
    assert sorted(p.name for p in att.iterdir()) == ["7"]
    assert sorted(p.name for p in cmd.iterdir()) == ["y.json"]


def test_rotate_log_covers_update_recovery_cmd_int_and_llamacpp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    checked = []

    real = os.lstat

    def lstat(path, *args, **kwargs):
        checked.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "lstat", lstat)
    maintenance.rotate_log()
    names = {os.path.basename(p) for p in checked}
    assert {"update.log", "recovery.log", "cmd_int.log", "llamacpp.log"} <= names
    assert str(tmp_path / ".hermes" / "logs" / "llamacpp.log") in checked


def test_rotate_log_covers_standalone_llamacpp_log(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path / "mcs"))
    checked = []

    real = os.lstat

    def lstat(path, *args, **kwargs):
        checked.append(path)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(maintenance.os, "lstat", lstat)
    maintenance.rotate_log()
    assert str(tmp_path / "mcs" / "logs" / "llamacpp.log") in checked
    assert str(tmp_path / "user" / ".hermes" / "logs" / "llamacpp.log") in checked


def test_rotate_log_keeps_rotated_copy_private_under_loose_umask(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    fresh, stale = tmp_path / "fresh.log", tmp_path / "stale.log"
    for log in (fresh, stale):
        log.write_bytes(b"synthetic line\n")
        log.chmod(0o600)
    (tmp_path / "stale.log.1").write_bytes(b"old")
    (tmp_path / "stale.log.1").chmod(0o644)
    old = os.umask(0o022)
    try:
        maintenance.rotate_log([str(fresh), str(stale)])
    finally:
        os.umask(old)
    for log in (fresh, stale):
        rotated = tmp_path / (log.name + ".1")
        assert rotated.read_bytes() == b"synthetic line\n"
        assert rotated.stat().st_mode & 0o777 == 0o600
        assert log.read_bytes() == b""


def test_prune_leftovers_removes_stranded_backup_and_snapshot_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    backups, snaps = tmp_path / "backups", tmp_path / "snapshots"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(snaps))
    backups.mkdir()
    snaps.mkdir()
    for f in (backups / "ledger-20260101.db.tmp", backups / "preupdate-x-1.db.tmp",
              backups / "ledger-20260101.db", backups / "preupdate-x-2.db",
              snaps / ".snapshot.abc.tmp", snaps / "ledger-snapshot.db"):
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # in-flight copies stay
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == 3
    assert sorted(p.name for p in backups.iterdir()) == [
        "ledger-20260101.db", "preupdate-x-2.db"]
    assert [p.name for p in snaps.iterdir()] == ["ledger-snapshot.db"]


def test_prune_leftovers_removes_stranded_staging_sidecars(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "HOME", str(tmp_path))
    backups, snaps = tmp_path / "backups", tmp_path / "snapshots"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(snaps))
    backups.mkdir()
    snaps.mkdir()
    stranded = [backups / "ledger-20260101-ab.db.tmp-journal",
                backups / "preupdate-x-1.db.tmp-wal",
                backups / "preupdate-x-1.db.tmp-shm",
                snaps / ".snapshot.abc.tmp-journal",
                snaps / ".snapshot.abc.tmp-wal",
                snaps / ".snapshot.abc.tmp-shm"]
    kept = [backups / "ledger-20260101.db", snaps / "ledger-snapshot.db"]
    for f in stranded + kept:
        f.write_bytes(b"x")
    assert maintenance.prune_leftovers() == 0               # in-flight copies stay
    monkeypatch.setattr(maintenance, "LEFTOVER_KEEP_S", -60)
    assert maintenance.prune_leftovers() == len(stranded)
    assert sorted(p for p in [*backups.iterdir(), *snaps.iterdir()]) == sorted(kept)


def test_rotate_log_refuses_symlinked_copy_and_still_rotates_the_rest(monkeypatch, tmp_path):
    monkeypatch.setattr(maintenance, "LOG_MAX", 3)
    linked, plain = tmp_path / "linked.log", tmp_path / "plain.log"
    for log in (linked, plain):
        log.write_bytes(b"synthetic line\n")
    target = tmp_path / "elsewhere.txt"
    target.write_bytes(b"keep")
    (tmp_path / "linked.log.1").symlink_to(target)
    with pytest.raises(maintenance.MaintenanceError, match="log_rotate_symlink_refused"):
        maintenance.rotate_log([str(linked), str(plain)])
    assert target.read_bytes() == b"keep"                   # never written through
    assert linked.read_bytes() == b"synthetic line\n"      # not truncated unrotated
    assert (tmp_path / "plain.log.1").read_bytes() == b"synthetic line\n"
    assert plain.read_bytes() == b""


def test_daily_backup_staging_copy_is_private_from_creation(tmp_path, monkeypatch):
    source = tmp_path / "source.db"
    writer = ledger.Ledger(str(source))
    writer.ensure_patient(1)
    writer.close()
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    monkeypatch.setattr(maintenance, "disk_floor_mb", lambda: 0)
    real_valid = maintenance.valid_mcs_db
    staged = []

    def spy(path, **kwargs):
        if path.endswith(".tmp"):
            staged.append(os.stat(path).st_mode & 0o777)
        return real_valid(path, **kwargs)

    monkeypatch.setattr(maintenance, "valid_mcs_db", spy)
    old = os.umask(0o022)
    try:
        maintenance.daily_backup(str(source))
    finally:
        os.umask(old)
    assert staged == [0o600]
    assert list(backups.glob("*.tmp")) == []
    assert len(list(backups.glob("ledger-*.db"))) == 1


def test_alert_and_failed_job_lookups_use_indexes(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    plan = " ".join(r[3] for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT MAX(created_at) FROM notify_outbox WHERE kind=?",
        ("session_expired",)))
    assert "idx_outbox_kind_created" in plan
    plan = " ".join(r[3] for r in db.db.execute(
        "EXPLAIN QUERY PLAN SELECT kind,COUNT(*) FROM fetch_jobs "
        "WHERE state='failed' AND updated_at>=? GROUP BY kind", (0,)))
    assert "idx_fetch_jobs_failed_recent" in plan
    db.close()


# Regression: preupdate backup survives prune before it is recorded, and a
# failed backup never strands a partial *.db.tmp copy.
def _source(tmp_path):
    src = tmp_path / "ledger.db"
    db = ledger.Ledger(str(src))
    db.ensure_patient(1)
    db.close()
    return str(src)


def test_unrecorded_fresh_preupdate_backup_survives_prune(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    state = tmp_path / "update_state.json"
    state.write_text(json.dumps({"applied": [], "applying": None}))
    fresh = maintenance.preupdate_backup(_source(tmp_path))
    stale = backups / "preupdate-old.db"
    stale.write_bytes(b"x")
    old = time.time() - maintenance.PREUPDATE_GRACE_S - 60
    os.utime(stale, (old, old))
    assert maintenance.prune_preupdate_backups(str(state)) == 1
    assert os.path.exists(fresh)
    assert not stale.exists()


@pytest.mark.parametrize("fn", ["preupdate_backup", "daily_backup"])
def test_failed_backup_removes_tmp(tmp_path, monkeypatch, fn):
    backups = tmp_path / "backups"
    monkeypatch.setattr(maintenance, "BACKUP_DIR", str(backups))
    src = _source(tmp_path)
    real_connect = sqlite3.connect

    class Boom:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, *a):
            return self.conn.execute(*a)

        def backup(self, dst, **_):
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            self.conn.close()

    def connect(path, *a, **kw):
        conn = real_connect(path, *a, **kw)
        return Boom(conn) if kw.get("uri") else conn

    monkeypatch.setattr(maintenance.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError):
        getattr(maintenance, fn)(src)
    assert [p for p in os.listdir(backups) if p.endswith(".tmp")] == []


def test_cloud_retention_is_62_days_and_jev_stays_seven(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    for kind in ("semantic_usage", "cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun",
                 "cloud_compare_v1", "cloud_usage_disposition"):
        for days in (6, 8, 61, 63):
            rid = db.artifact_add(kind, "{}")
            db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (now - days * 86400, rid))
    db.db.commit()
    maintenance.prune_history(db.db, now=now)
    assert len(db.artifacts("semantic_usage")) == 1
    for kind in ("cloud_usage_reserve", "cloud_usage_result", "cloud_usage_dryrun",
                 "cloud_compare_v1", "cloud_usage_disposition"):
        assert len(db.artifacts(kind)) == 3
    db.close()


def test_prune_keeps_unknown_summary_usage_and_invalid_evidence(tmp_path):
    from mcs_util import payload_hash

    db = _ledger(tmp_path)
    now = time.time()
    key = {"input_key": "1" * 64, "candidate_key": "2" * 64,
           "failure_key": "3" * 64, "audit_key": "4" * 64}
    fact = "a" * 64
    request = {"v": 1, **key, "questions": {"q0": fact}}
    base = {"operation": "patient_summary_quality", "jev_requests": 1, "quality_request": request,
            "reserved": True, "request_fp": "b" * 64, "reservation_id": "c" * 32, "timeout_seconds": 10}
    ids = {}
    for name, meta in (
            ("unbound", {"operation": "patient_summary_quality", "jev_requests": 1}),
            ("unknown", base),
            ("resolved_invalid", {**base, "quality_result": {"answers": {}, "invalid": [fact]}}),
            ("missing_answer", {**base, "quality_result": {"answers": {fact: payload_hash(
                {"choice": "supports", "confidence": 0.9})}, "invalid": []}}),
            ("other", {"jev_requests": 1})):
        ids[name] = db.artifact_add("semantic_usage", "{}", project_id=1, meta=meta)
    ids["corrupt"] = db.artifact_add("semantic_usage", "{}", project_id=1)
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                  ('{"operation":"other","operation":"patient_summary_quality"}', ids["corrupt"]))
    db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='semantic_usage'", (now - 8 * 86400,))
    db.db.commit()
    maintenance.prune_history(db.db, now=now)
    remaining = {row[0] for row in db.db.execute("SELECT artifact_id FROM artifacts WHERE kind='semantic_usage'")}
    assert remaining == {ids[name] for name in ("unbound", "unknown", "resolved_invalid", "missing_answer", "corrupt")}
    db.close()


def test_context_scan_generations_and_all_fact_chunks_survive_history_retention(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    ids = []
    for kind, version in (('patient_context_scan_v1', '0.1'), ('patient_context_scan_v1', '1.0'),
                          ('patient_context_scan_chunk_v1', '1.0'), ('canonical_projection', '1.0')):
        rid = db.artifact_add(kind, '{"facts":[]}', meta={'inference_engine_version': version})
        ids.append(rid)
        db.db.execute('UPDATE artifacts SET created_at=? WHERE artifact_id=?', (now-400*86400, rid))
    db.db.commit()
    before = [tuple(row) for row in db.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    maintenance.prune_history(db.db, now=now)
    assert [tuple(row) for row in db.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    assert [row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts ORDER BY artifact_id')] == ids
    db.close()


def _repair_accounting(db, pid, marker, *, header=True, slot=True):
    reservation_id, unit_key, request_fp = marker * 32, marker * 64, 'f' * 64
    binding = {'project_id': pid, 'reservation_id': reservation_id, 'request_fp': request_fp,
               'unit_key': unit_key, 'route': 'patient_context', 'model': 'synthetic', 'price_version': 'synthetic'}
    rows = {}
    for label, kind in (('reserve', 'cloud_usage_reserve'), ('result', 'cloud_usage_result'),
                        ('disposition', 'cloud_usage_disposition'), ('dryrun', 'cloud_usage_dryrun'),
                        ('compare', 'cloud_compare_v1')):
        rows[label] = db.artifact_add(kind, '{}', project_id=pid, meta=binding)
    repair_input_key = 'e' * 64
    if header:
        rows['header'] = db.artifact_add('patient_context_source_quality_repair_v1', '{"phase":"staging"}',
            project_id=pid, meta={'project_id': pid, 'repair_input_key': repair_input_key})
    if slot:
        rows['slot'] = db.artifact_add('patient_context_source_quality_repair_candidate_v1', '{"facts":[]}',
            project_id=pid, meta={'project_id': pid, 'repair_input_key': repair_input_key, 'source_key': 1,
                'unit_key': unit_key, 'reservation_artifact_id': rows['reserve'],
                'reservation_id': reservation_id, 'request_fp': request_fp})
    return rows


def _expire_repair_accounting(db, now):
    db.db.execute('UPDATE artifacts SET created_at=?', (now - 63 * 86400,))
    db.db.commit()


@pytest.mark.parametrize('batch', [1, 5000])
def test_repair_retention_pins_only_exact_original_receipts_and_preserves_staging(tmp_path, monkeypatch, batch):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        unrelated = _repair_accounting(db, 1, 'b', header=False, slot=False)
        other_patient = _repair_accounting(db, 2, 'a', header=False, slot=False)
        other_uuid = _repair_accounting(db, 3, 'c', header=False, slot=False)
        _expire_repair_accounting(db, now)
        immutable = [tuple(db.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (linked[key],)).fetchone())
                     for key in ('reserve', 'result', 'disposition', 'header', 'slot')]
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        maintenance.prune_history(db.db, now=now)
        assert {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')} == {
            *[linked[key] for key in ('reserve', 'result', 'disposition', 'header', 'slot')],
            *[other_patient[key] for key in ('reserve', 'result', 'disposition')]}
        assert immutable == [tuple(db.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (row[0],)).fetchone())
                             for row in immutable]
        assert not set(unrelated.values()).intersection(row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts'))
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        # Financial adoption requires UUID uniqueness across patients.  These
        # conflicting receipts must not disappear and restore a former PASS.
        assert not {other_patient['dryrun'], other_patient['compare']}.intersection(remaining)
        assert not set(other_uuid.values()).intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('broken', ['missing_id', 'missing_hash', 'invalid_source', 'duplicate_json',
                                   'nonfinite_json', 'missing_slot', 'invalid_header', 'changed_reservation'])
def test_broken_repair_binding_holds_only_its_patient_receipts(tmp_path, broken):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        same_patient = _repair_accounting(db, 1, 'b', header=False, slot=False)
        other = _repair_accounting(db, 2, 'c', header=False, slot=False)
        if broken == 'missing_slot':
            db.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (linked['slot'],))
        elif broken == 'invalid_header':
            db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', ('{"broken":', linked['header']))
        elif broken == 'changed_reservation':
            raw = db.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (linked['reserve'],)).fetchone()[0]
            meta = json.loads(raw)
            meta['request_fp'] = 'd' * 64
            db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), linked['reserve']))
        else:
            raw = db.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (linked['slot'],)).fetchone()[0]
            meta = json.loads(raw)
            if broken == 'missing_id':
                meta.pop('reservation_artifact_id')
            elif broken == 'missing_hash':
                meta.pop('request_fp')
            elif broken == 'invalid_source':
                meta['source_key'] = '../unsafe'
            if broken == 'duplicate_json':
                raw = '{"repair_input_key":"' + 'a' * 64 + '","repair_input_key":"' + 'b' * 64 + '"}'
            elif broken == 'nonfinite_json':
                raw = '{"repair_input_key":"' + 'a' * 64 + '","number":1e999}'
            else:
                raw = json.dumps(meta)
            db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (raw, linked['slot']))
        _expire_repair_accounting(db, now)
        maintenance.prune_history(db.db, now=now)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        assert {linked[key] for key in ('reserve', 'result', 'disposition')} <= remaining
        assert {same_patient[key] for key in ('reserve', 'result', 'disposition')} <= remaining
        assert not set(other.values()).intersection(remaining)
        assert not {linked['dryrun'], linked['compare'], same_patient['dryrun'], same_patient['compare']}.intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('orphan', ['header', 'reserve', 'partial_content'])
def test_orphan_or_partial_candidate_keeps_known_receipts_without_pinning_unrelated_usage(tmp_path, orphan):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        unrelated = _repair_accounting(db, 1, 'b', header=False, slot=False)
        if orphan == 'partial_content':
            db.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', ('{"partial":', linked['slot']))
        else:
            db.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (linked[orphan],))
        _expire_repair_accounting(db, now)
        maintenance.prune_history(db.db, now=now)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        assert {linked[key] for key in ('result', 'disposition', 'slot')} <= remaining
        assert not set(unrelated.values()).intersection(remaining)
    finally:
        db.close()


def test_unscoped_repair_corruption_stops_deletion_instead_of_inventing_a_patient_scope(tmp_path):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        db.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (linked['slot'],))
        _expire_repair_accounting(db, now)
        before = [tuple(row) for row in db.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        with pytest.raises(maintenance.MaintenanceError, match='source_repair_retention_scope_invalid'):
            maintenance._prune_cloud_usage(db.db, now - maintenance.CLOUD_USAGE_KEEP_S)
        assert before == [tuple(row) for row in db.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    finally:
        db.close()


def test_unlinked_malformed_old_accounting_keeps_ordinary_retention(tmp_path):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        rows = _repair_accounting(db, 1, 'a', header=False, slot=False)
        db.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', ('{"broken":', rows['result']))
        _expire_repair_accounting(db, now)
        maintenance.prune_history(db.db, now=now)
        assert not db.artifacts('cloud_usage_reserve') and not db.artifacts('cloud_usage_result')
        assert not db.artifacts('cloud_usage_disposition')
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['reserve', 'result', 'disposition'])
def test_exact_repair_receipt_survives_a_corrupt_row_scope(tmp_path, kind):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        db.db.execute('UPDATE artifacts SET project_id=NULL WHERE artifact_id=?', (linked[kind],))
        _expire_repair_accounting(db, now)
        maintenance.prune_history(db.db, now=now)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        assert {linked[key] for key in ('reserve', 'result', 'disposition', 'header', 'slot')} <= remaining
    finally:
        db.close()


@pytest.mark.parametrize('kind', ['result', 'disposition'])
@pytest.mark.parametrize('broken_field', ['request_fp', 'reservation_id'])
def test_ambiguous_receipt_scope_and_binding_pin_the_exact_batch_row(tmp_path, kind, broken_field):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        other = _repair_accounting(db, 3, 'c', header=False, slot=False)
        raw = db.db.execute('SELECT meta FROM artifacts WHERE artifact_id=?', (linked[kind],)).fetchone()[0]
        meta = json.loads(raw)
        meta[broken_field] = 'invalid'
        db.db.execute('UPDATE artifacts SET project_id=2,meta=? WHERE artifact_id=?', (json.dumps(meta), linked[kind]))
        _expire_repair_accounting(db, now)
        maintenance.prune_history(db.db, now=now)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        assert linked[kind] in remaining
        assert {linked[key] for key in ('reserve', 'result', 'disposition', 'header', 'slot')} <= remaining
        assert not set(other.values()).intersection(remaining)
    finally:
        db.close()


@pytest.mark.parametrize('batch', [1, 5000])
@pytest.mark.parametrize('kind', ['result', 'disposition'])
@pytest.mark.parametrize('corrupt', ['syntax', 'duplicate', 'nonfinite'])
def test_unknown_receipt_scope_and_unparseable_metadata_pin_only_that_batch_row(tmp_path, monkeypatch, batch, kind, corrupt):
    db = _ledger(tmp_path)
    try:
        now = time.time()
        linked = _repair_accounting(db, 1, 'a')
        unrelated = _repair_accounting(db, 2, 'b', header=False, slot=False)
        raw = {'syntax': '{"broken":', 'duplicate': '{"project_id":2,"project_id":1}',
               'nonfinite': '{"project_id":1,"unused":1e999}'}[corrupt]
        db.db.execute('UPDATE artifacts SET project_id=2,meta=? WHERE artifact_id=?', (raw, linked[kind]))
        _expire_repair_accounting(db, now)
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        maintenance.prune_history(db.db, now=now)
        remaining = {row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts')}
        assert {linked[key] for key in ('reserve', 'result', 'disposition', 'header', 'slot')} <= remaining
        assert not set(unrelated.values()).intersection(remaining)
        assert linked['dryrun'] not in remaining and linked['compare'] not in remaining
    finally:
        db.close()


@pytest.mark.parametrize('batch', [1, 5000])
@pytest.mark.parametrize('kind', ['cloud_usage_result', 'cloud_usage_disposition'])
def test_global_uuid_conflicts_remain_rejection_evidence_after_retention(tmp_path, monkeypatch, batch, kind):
    from datetime import datetime
    import cloud_policy
    from anthropic_client import MODEL

    db = _ledger(tmp_path)
    try:
        now = datetime(2026, 10, 26, 12, tzinfo=cloud_policy.JST).timestamp()
        monkeypatch.setattr(cloud_policy.time, 'time', lambda: now)
        monkeypatch.setattr(maintenance, 'RETENTION_BATCH', batch)
        cfg = {'cloud_llm': {'mode': 'on', 'project_ids': [1, 2]}}
        repair_key, unit_key = 'e' * 64, 'a' * 64
        owned = {}
        def reserved(owner, reservation, artifact_id):
            owned['reserve'] = artifact_id
            owned['header'] = owner.artifact_add_tx('patient_context_source_quality_repair_v1', '{"phase":"ready"}',
                project_id=1, meta={'project_id': 1, 'repair_input_key': repair_key})
            owned['slot'] = owner.artifact_add_tx('patient_context_source_quality_repair_candidate_v1', '{"phase":"reserved"}',
                project_id=1, meta={'project_id': 1, 'repair_input_key': repair_key, 'source_key': 1,
                    'unit_key': unit_key, 'reservation_artifact_id': artifact_id,
                    'reservation_id': reservation['reservation_id'], 'request_fp': reservation['request_fp']})
            return 2
        request = {'model': MODEL, 'max_tokens': 100}
        reservation = cloud_policy.usage_reserver(db, cfg, route='patient_context', project_id=1,
            unit_key=unit_key, owner={'repair_input_key': repair_key, 'unit_key': unit_key}, on_reserved_tx=reserved)(request, 2)
        result = {'model': MODEL, 'request_fp': reservation['request_fp'], 'usage': {'input_tokens': 10, 'output_tokens': 2},
                  'http_class': 'http_200', 'request_id': 'req_synthetic', 'error_kind': None}
        cloud_policy.record_result(db, reservation, result)
        if kind == 'cloud_usage_disposition':
            # Obtain genuine public-writer bytes without leaving the fixture's
            # original successful occurrence already discarded.
            db.db.execute('SAVEPOINT synthetic_disposition')
            aid = cloud_policy.record_disposition_tx(db, reservation, 'discarded_scope')
            conflict = dict(db.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone())
            db.db.execute('ROLLBACK TO synthetic_disposition')
            db.db.execute('RELEASE synthetic_disposition')
        else:
            conflict = dict(db.db.execute("SELECT * FROM artifacts WHERE kind='cloud_usage_result' AND project_id=1").fetchone())
        other = cloud_policy.usage_reserver(db, cfg, route='patient_context', project_id=2, unit_key='b' * 64)(request, 2)
        cloud_policy.record_result(db, other, {**result, 'request_fp': other['request_fp']})
        unrelated = {row[0] for row in db.db.execute("SELECT artifact_id FROM artifacts WHERE project_id=2 AND kind IN "
            "('cloud_usage_reserve','cloud_usage_result','cloud_usage_disposition')")}
        _expire_repair_accounting(db, now)
        proof = cloud_policy.reservation_evidence(db.db, owned['reserve'])
        with db.db:
            db.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?',
                (json.dumps({'phase': 'complete', 'receipt_sig': proof['witness'], 'facts': []}), owned['slot']))
            meta = {**json.loads(conflict['meta']), 'project_id': 2, 'message_id': None}
            conflict_id = db.db.execute('INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) '
                'VALUES(?,?,NULL,?,?,?,?)', (kind, 2, conflict['model'], conflict['content'], json.dumps(meta), now - 63 * 86400)).lastrowid
        original = [tuple(row) for row in db.db.execute("SELECT * FROM artifacts WHERE project_id=1 AND kind IN "
            "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            cloud_policy.reservation_evidence(db.db, owned['reserve'])
        maintenance.prune_history(db.db, now=now)
        assert db.db.execute('SELECT 1 FROM artifacts WHERE artifact_id=?', (conflict_id,)).fetchone()
        assert original == [tuple(row) for row in db.db.execute("SELECT * FROM artifacts WHERE project_id=1 AND kind IN "
            "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
        assert json.loads(db.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (owned['slot'],)).fetchone()[0])['receipt_sig'] == proof['witness']
        assert not unrelated.intersection(row[0] for row in db.db.execute('SELECT artifact_id FROM artifacts'))
        with pytest.raises(ValueError, match='cloud_receipt_invalid'):
            cloud_policy.reservation_evidence(db.db, owned['reserve'])
    finally:
        db.close()
