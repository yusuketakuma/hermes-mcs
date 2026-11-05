"""Patient memo-summary identity, history and transactional failure recovery."""
import json
import sqlite3
import time
from contextlib import closing
import pytest
import ledger
from ingest_testkit import _ledger, _message, _unread_patient


CORRUPT_SUMMARY_JSON = [
    '{"synthetic":' + '1' * 5000 + '}',
    '{"synthetic":' + '[' * 10000 + '0' + ']' * 10000 + '}',
]



# ---------- 連携サマリー: patients.karte_id + karte_summary artifacts ----------

def _karte_patient(pid, karte_id):
    p = _unread_patient(pid)
    p.karte_id = karte_id
    return p


def _summary(comment="合成サマリー", updated="2026-09-30T10:00:00+09:00"):
    return {"comment": comment, "updated_at": updated, "is_editable": True,
            "user": {"profession": "看護師", "name": "合成 花子"}}


def test_karte_id_migration_is_additive_and_idempotent(tmp_path):
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.db.execute("ALTER TABLE patients DROP COLUMN karte_id")  # pre-#21 shape
    db.db.execute("ALTER TABLE patients DROP COLUMN karte_summary_due")
    db.db.execute("ALTER TABLE patients DROP COLUMN karte_summary_failed_at")
    db.db.commit()
    db.close()
    for _ in range(2):  # reopen twice: columns added once, then a no-op
        db = ledger.Ledger(path)
        cols = {r[1] for r in db.db.execute("PRAGMA table_info(patients)")}
        assert {"karte_id", "karte_summary_due", "karte_summary_failed_at"} <= cols
        assert db.db.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        db.close()


def test_karte_id_persisted_by_both_upserts_and_never_cleared(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_karte_patient(1, 10))
    db.save_patient(_karte_patient(2, 20))
    assert db.karte_id(1) == 10 and db.karte_id(2) == 20
    db.save_patient(_unread_patient(1))          # no karte_id -> preserved
    db.upsert_patient_info(_unread_patient(2))
    assert db.karte_id(1) == 10 and db.karte_id(2) == 20
    assert db.karte_id(3) is None
    db.close()


def test_karte_summary_store_dedups_by_sha_and_keeps_history(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_karte_patient(1, 10))
    assert db.karte_summary_store(1, 10, _summary()) is True
    first = db.karte_summary_current(1)
    assert first["comment"] == "合成サマリー" and first["empty"] is False
    assert first["updater"] == {"profession": "看護師", "name": "合成 花子"}
    assert isinstance(first["fetched_at"], float)

    time.sleep(0.01)
    assert db.karte_summary_store(1, 10, _summary()) is False   # same sha
    assert len(db.artifacts("karte_summary", project_id=1)) == 1
    assert db.karte_summary_current(1)["fetched_at"] > first["fetched_at"]

    assert db.karte_summary_store(1, 10, _summary(comment="変更後")) is True
    rows = db.artifacts("karte_summary", project_id=1)
    assert len(rows) == 2
    assert db.karte_summary_current(1)["comment"] == "変更後"
    meta = json.loads(rows[-1]["meta"])
    assert meta["karte_id"] == 10 and len(meta["sha256"]) == 64
    db.close()


def test_karte_summary_empty_is_stored_distinct_from_never_fetched(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_karte_patient(1, 10))
    assert db.karte_summary_current(1) is None                  # 未取得
    assert db.karte_summary_store(1, 10, None) is True
    cur = db.karte_summary_current(1)                            # 空
    assert cur["empty"] is True and cur["comment"] is None
    assert cur["updater"] == {"profession": "", "name": ""}
    assert db.karte_summary_store(1, 10, None) is False
    db.close()


def test_karte_summary_missing_orders_oldest_first_and_limits(tmp_path):
    db = _ledger(tmp_path)
    for pid, karte in ((1, 10), (2, 20), (3, 30)):
        db.upsert_patient_info(_karte_patient(pid, karte))
    db.upsert_patient_info(_unread_patient(4))                   # no karte_id
    db.db.execute("UPDATE patients SET last_seen=project_id*-1")  # 3 oldest
    db.db.commit()
    assert db.karte_summary_missing(10) == [3, 2, 1]
    assert db.karte_summary_missing(2) == [3, 2]
    db.karte_summary_store(2, 20, None)
    assert db.karte_summary_missing(10) == [3, 1]
    db.karte_summary_failed(3)                                   # backoff: skipped
    assert db.karte_summary_missing(10) == [1]
    db.db.execute("UPDATE patients SET karte_summary_failed_at=?",
                  (time.time() - ledger.KARTE_SUMMARY_BACKOFF_S - 1,))
    db.db.commit()
    assert db.karte_summary_missing(10) == [3, 1]
    db.close()


def test_karte_summary_due_flag_is_durable_until_stored(tmp_path):
    db = _ledger(tmp_path)
    for pid, karte in ((1, 10), (2, 20)):
        db.upsert_patient_info(_karte_patient(pid, karte))
    db.upsert_patient_info(_unread_patient(3))                   # no karte_id
    db.db.execute("UPDATE patients SET last_seen=project_id*-1")  # 2 oldest
    db.db.commit()
    assert db.karte_summary_due() == []
    for pid in (1, 2, 3, 1):
        db.karte_summary_mark_due(pid)
    assert db.karte_summary_due() == [1, 2]                      # newest first; 3 has no karte_id
    db.karte_summary_store(1, 10, _summary())                    # new artifact clears
    assert db.karte_summary_due() == [2]
    db.karte_summary_mark_due(1)
    db.karte_summary_store(1, 10, _summary())                    # same sha clears too
    assert db.karte_summary_due() == [2]
    db.karte_summary_mark_due(2, due=False)                      # non-retryable failure path
    assert db.karte_summary_due() == []
    db.close()


@pytest.mark.parametrize("raw", ["{broken", "[]", "null", '"synthetic"',
                                  '{"nested":' + '[' * 10000 + '0' + ']' * 10000 + '}'],
                         ids=["malformed", "array", "null", "string", "deep"])
@pytest.mark.parametrize(("source", "entry"), [
    ("capture", "recapture"), ("shadow", "shadow"),
    ("capture", "publish"), ("shadow", "failure"),
])
def test_corrupt_optional_metadata_does_not_block_recapture(tmp_path, raw, source, entry, recursion_limited_json):
    from message_observations import get_message_metadata, get_metadata_shadow_status

    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="synthetic old")])
        with db.db:
            db.db.execute(
                "INSERT INTO message_metadata(message_id,source,content,checked_at) "
                "VALUES(1,?,?,1)", (source, raw))
        refreshed = _message(body="synthetic current")
        refreshed.metadata = {"reactions": []}
        if entry == "recapture":
            db.save_messages([refreshed])
            assert db.db.execute("SELECT body_html FROM messages WHERE message_id=1"
                                 ).fetchone()[0] == "synthetic current"
        else:
            db.save_metadata_shadow(refreshed, publish=entry == "publish",
                                    error="fetch_error" if entry == "failure" else None)
        stored = db.db.execute(
            "SELECT content,last_error FROM message_metadata WHERE message_id=1 AND source=?",
            (source,)).fetchone()
        if entry == "failure":
            assert json.loads(stored["content"]) == {}
            assert stored["last_error"] == "fetch_error"
            assert get_metadata_shadow_status(db.db, 1)["state"] == "failed"
        else:
            content = json.loads(stored["content"])
            assert content["reactions"]["value"] == []
            assert content["reactions"]["observed_at"] > 1
            assert stored["last_error"] is None
            if source == "capture":
                assert get_message_metadata(db.db, 1)["reactions_status"] == "observed"
    finally:
        db.close()


@pytest.mark.parametrize("column", ["content", "meta"])
@pytest.mark.parametrize("raw", CORRUPT_SUMMARY_JSON, ids=["integer_limit", "deep_json"])
def test_corrupt_summary_json_is_unknown(tmp_path, column, raw, recursion_limited_json):
    db = _ledger(tmp_path)
    db.karte_summary_store(1, 10, _summary())
    with db.db:
        db.db.execute(f"UPDATE artifacts SET {column}=? WHERE kind='karte_summary'", (raw,))
    assert db.karte_summary_current(1) is None
    assert db.db.execute(f"SELECT {column} FROM artifacts WHERE kind='karte_summary'").fetchone()[0] == raw
    db.close()


@pytest.mark.parametrize("raw", CORRUPT_SUMMARY_JSON, ids=["integer_limit", "deep_json"])
def test_fresh_summary_recovers_from_corrupt_metadata_without_erasing_history(tmp_path, raw):
    db = _ledger(tmp_path)
    db.karte_summary_store(1, 10, _summary())
    with db.db:
        db.db.execute("UPDATE artifacts SET meta=? WHERE kind='karte_summary'", (raw,))
    assert db.karte_summary_store(1, 10, _summary()) is True
    assert db.karte_summary_current(1)["comment"] == _summary()["comment"]
    rows = db.db.execute("SELECT meta FROM artifacts WHERE kind='karte_summary' ORDER BY artifact_id").fetchall()
    assert len(rows) == 2 and rows[0][0] == raw
    assert db.karte_summary_store(1, 10, _summary()) is False
    db.close()


def _karte_due_patient(db, pid=1):
    p = _unread_patient(pid)
    p.karte_id = pid * 10
    db.upsert_patient_info(p)
    db.karte_summary_mark_due(pid)


@pytest.mark.parametrize("caller_open", [False, True])
def test_a_failed_due_clear_never_leaves_half_a_summary(tmp_path, caller_open):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("CREATE TRIGGER synthetic_fail BEFORE UPDATE OF karte_summary_due ON patients "
                      "BEGIN SELECT RAISE(ABORT, 'synthetic due failure'); END")
        db.db.commit()
        if caller_open:                              # a caller's own uncommitted write
            db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        with pytest.raises(sqlite3.DatabaseError, match="synthetic due failure"):
            db.karte_summary_store(1, 10, {"comment": "合成"})
        db.db.execute("DROP TRIGGER synthetic_fail")
        db.db.execute("UPDATE patients SET last_seen=last_seen WHERE project_id=1")   # unrelated
        db.db.commit()
        stored = db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='karte_summary'").fetchone()[0]
        disease = db.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0]
        assert stored == 0                           # insert and due clear commit together or not at all
        assert db.karte_summary_due() == [1]
        if caller_open:                              # only this store was undone
            assert disease == "synthetic-pending"
    finally:
        db.close()


def test_a_failed_commit_never_leaves_half_a_summary(tmp_path):
    db = _ledger(tmp_path)
    denied = []

    def deny_commit(action, arg1, *_):
        if action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT":
            denied.append(arg1)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK                     # ROLLBACK and everything else stay allowed
    try:
        _karte_due_patient(db)
        db.db.set_authorizer(deny_commit)
        try:
            with pytest.raises(sqlite3.DatabaseError):
                db.karte_summary_store(1, 10, {"comment": "合成"})
        finally:
            db.db.set_authorizer(None)
        assert len(denied) == 1 and not db.db.in_transaction
        with closing(sqlite3.connect(str(tmp_path / "ledger.db"))) as reader:
            assert reader.execute("SELECT COUNT(*) FROM artifacts WHERE kind='karte_summary'").fetchone()[0] == 0
        db.db.execute("UPDATE patients SET last_seen=last_seen WHERE project_id=1")   # unrelated
        db.db.commit()
        assert db.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='karte_summary'").fetchone()[0] == 0
        assert db.karte_summary_due() == [1]
    finally:
        db.close()


def _summary_rows(path):
    with closing(sqlite3.connect(str(path))) as reader:
        return reader.execute("SELECT COUNT(*) FROM artifacts WHERE kind='karte_summary'").fetchone()[0]


def _deny(db, rules):
    """Authorizer denying the listed (action, arg1) pairs once each, counting denials."""
    denied = []

    def hook(action, arg1, *_):
        if (action, arg1) in rules and (action, arg1) not in denied:
            denied.append((action, arg1))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    db.db.set_authorizer(hook)
    return denied


@pytest.mark.parametrize("unchanged", [False, True])
@pytest.mark.parametrize("caller_open", [False, True])
def test_summary_store_failures_never_persist_half_a_store(tmp_path, unchanged, caller_open):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        if unchanged:
            db.karte_summary_store(1, 10, {"comment": "合成"})
            db.karte_summary_mark_due(1)
        before = _summary_rows(tmp_path / "ledger.db")
        fetched = db.karte_summary_current(1) and db.karte_summary_current(1)["fetched_at"]
        db.db.execute("CREATE TRIGGER synthetic_fail BEFORE UPDATE OF karte_summary_due ON patients "
                      "BEGIN SELECT RAISE(ABORT, 'synthetic due failure'); END")
        db.db.commit()
        if caller_open:
            db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        with pytest.raises(sqlite3.DatabaseError, match="synthetic due failure"):
            db.karte_summary_store(1, 10, {"comment": "合成"})
        assert db.db.in_transaction is caller_open    # only this store was undone
        db.db.execute("DROP TRIGGER synthetic_fail")
        db.db.commit()
        assert _summary_rows(tmp_path / "ledger.db") == before
        assert db.karte_summary_due() == [1]
        if unchanged:
            assert db.karte_summary_current(1)["fetched_at"] == fetched
        disease = db.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0]
        assert (disease == "synthetic-pending") is caller_open
    finally:
        db.close()


def test_summary_store_inside_a_caller_savepoint_commits_the_caller_write(tmp_path):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        db.db.execute("SAVEPOINT caller_outer")
        assert db.karte_summary_store(1, 10, {"comment": "合成"}) is True
        assert not db.db.in_transaction                 # the method-owned commit, as before
        assert _summary_rows(tmp_path / "ledger.db") == 1 and db.karte_summary_due() == []
        with closing(sqlite3.connect(str(tmp_path / "ledger.db"))) as reader:
            assert reader.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] \
                == "synthetic-pending"
    finally:
        db.close()


def test_a_denied_commit_inside_a_caller_transaction_keeps_the_caller_write(tmp_path):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        denied = _deny(db, {(sqlite3.SQLITE_TRANSACTION, "COMMIT")})
        try:
            with pytest.raises(sqlite3.DatabaseError):
                db.karte_summary_store(1, 10, {"comment": "合成"})
        finally:
            db.db.set_authorizer(None)
        assert denied == [(sqlite3.SQLITE_TRANSACTION, "COMMIT")] and db.db.in_transaction
        db.db.commit()
        assert _summary_rows(tmp_path / "ledger.db") == 0 and db.karte_summary_due() == [1]
        assert db.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] \
            == "synthetic-pending"
    finally:
        db.close()


def test_sqlite_automatic_rollback_skips_the_savepoint_cleanup(tmp_path):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("CREATE TRIGGER synthetic_rollback BEFORE UPDATE OF karte_summary_due ON patients "
                      "BEGIN SELECT RAISE(ROLLBACK, 'synthetic whole rollback'); END")
        db.db.commit()
        db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        with pytest.raises(sqlite3.DatabaseError, match="synthetic whole rollback"):
            db.karte_summary_store(1, 10, {"comment": "合成"})   # no missing-savepoint error
        assert not db.db.in_transaction
        assert _summary_rows(tmp_path / "ledger.db") == 0
    finally:
        db.close()


@pytest.mark.parametrize("refused", ["ROLLBACK", "RELEASE"])
def test_a_failed_local_undo_drops_the_whole_uncommitted_transaction(tmp_path, refused):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("CREATE TRIGGER synthetic_fail BEFORE UPDATE OF karte_summary_due ON patients "
                      "BEGIN SELECT RAISE(ABORT, 'synthetic due failure'); END")
        db.db.commit()
        db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        denied = _deny(db, {(sqlite3.SQLITE_SAVEPOINT, refused)})
        try:
            with pytest.raises(sqlite3.DatabaseError, match="synthetic due failure"):  # original kept
                db.karte_summary_store(1, 10, {"comment": "合成"})
        finally:
            db.db.set_authorizer(None)
        assert denied == [(sqlite3.SQLITE_SAVEPOINT, refused)] and not db.db.in_transaction
        assert _summary_rows(tmp_path / "ledger.db") == 0
        # the caller's uncommitted write went with the transaction; nothing was claimed saved
        assert db.db.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] \
            != "synthetic-pending"
    finally:
        db.close()


def test_a_failed_full_undo_closes_the_connection(tmp_path):
    db = _ledger(tmp_path)
    try:
        _karte_due_patient(db)
        db.db.execute("CREATE TRIGGER synthetic_fail BEFORE UPDATE OF karte_summary_due ON patients "
                      "BEGIN SELECT RAISE(ABORT, 'synthetic due failure'); END")
        db.db.commit()
        db.db.execute("UPDATE patients SET disease='synthetic-pending' WHERE project_id=1")
        _deny(db, {(sqlite3.SQLITE_SAVEPOINT, "ROLLBACK"), (sqlite3.SQLITE_TRANSACTION, "ROLLBACK")})
        with pytest.raises(sqlite3.DatabaseError, match="synthetic due failure"):
            db.karte_summary_store(1, 10, {"comment": "合成"})
        with pytest.raises(sqlite3.ProgrammingError):          # nothing can commit afterwards
            db.db.commit()
        assert _summary_rows(tmp_path / "ledger.db") == 0
        with closing(sqlite3.connect(str(tmp_path / "ledger.db"))) as reader:
            assert reader.execute("SELECT disease FROM patients WHERE project_id=1").fetchone()[0] \
                != "synthetic-pending"
    finally:
        db.close()                                             # a second close is harmless
