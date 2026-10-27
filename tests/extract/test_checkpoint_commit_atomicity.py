"""Synthetic races between accepted checkpoints and final extraction writes."""

import json
import sqlite3

import pytest

import extract_llm as llm
import inference_dependency_loss as dependency_loss
from extract_testkit import _ledger, _message


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("aba", [False, True])
def test_checkpoint_changed_after_replay_ack_is_not_committed(tmp_path, monkeypatch, workers, aba):
    ledger = _ledger(tmp_path)
    try:
        ledger.save_messages([
            _message(mid=mid, body="本人は合成の経過を共有しました。" * 10)
            for mid in range(1, workers + 1)
        ])
        monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
        monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
        monkeypatch.setattr(llm, "_rerender_cards", lambda *args: None)
        monkeypatch.setattr(llm, "_probe_format", lambda **kwargs: "plain")
        monkeypatch.setattr(llm, "load_config", lambda: {
            "extract": {"patient_state_context": False, "prefilter": False},
            "local_llm": {"model": "synthetic-model"},
        })
        pending, _ = llm._select_pending(ledger, workers, None, None, False)
        jobs, skipped, blocked = llm._prepare_jobs(
            ledger, pending, llm._resolved_llm(), False, allow_cloud=False)
        assert len(jobs) == workers and not skipped and not blocked
        quality = {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
        a = {"summary": "合成A", "_quality_result": quality}
        b = {"summary": "合成B", "_quality_result": quality}
        for row, context, _, _, _ in jobs:
            assert llm._persist_chunks(ledger, row, {0: a}, context,
                                       chunk_size=row["_chunk_size"])

        original = llm._persist_chunks
        inserted = []

        def after_ack(current_ledger, row, chunks, context=None, **kwargs):
            accepted = original(current_ledger, row, chunks, context, **kwargs)
            if kwargs.get("replay") and row["message_id"] == 1 and accepted and not inserted:
                common = {"chunk_size": kwargs["chunk_size"], "splits": kwargs["splits"],
                          "lease": kwargs["lease"]}
                assert original(current_ledger, row, {0: b}, context, **common)
                if aba:
                    assert original(current_ledger, row, {0: a}, context, **common)
                newest = current_ledger.db.execute(
                    "SELECT artifact_id FROM artifacts WHERE kind='extract_llm_chunk' "
                    "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
                inserted.append(newest[0])
            return accepted

        monkeypatch.setattr(llm, "_persist_chunks", after_ack)
        monkeypatch.setattr(llm, "_llm_call", lambda *args, **kwargs: pytest.fail("unexpected dispatch"))
        result = llm.run_pending(ledger, limit=workers, budget_s=200, workers=workers)

        assert len(inserted) == 1
        assert result["deferred"] >= 1
        assert not ledger.db.execute(
            "SELECT 1 FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()
        latest = ledger.db.execute(
            "SELECT artifact_id, content FROM artifacts WHERE kind='extract_llm_chunk' "
            "AND message_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
        assert latest is not None and latest["artifact_id"] == inserted[0]
        assert json.loads(latest["content"])["summary"] == ("合成A" if aba else "合成B")
    finally:
        ledger.close()


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    ledger.ensure_patient(1)
    ledger.save_messages([_message(body="本人に息切れはありません。家族には軽い発熱があります。")])
    ledger.db.commit()
    monkeypatch.setattr(llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(llm, "_rerender_cards", lambda *args: None)
    row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = dependency_loss.source_binding(ledger.db, 1, 1)
    value = {"summary": "合成A", "_quality_result": {
        "state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}
    assert llm._persist_chunks(ledger, row, {0: value})
    assert llm._claim(ledger, row) is not None
    llm._capture_checkpoint_state(ledger.db, row, None)
    yield ledger, row, value
    ledger.close()


@pytest.mark.parametrize("failure_point", ["writable", "binding"])
def test_claim_rolls_back_owned_transaction_on_source_failure(tmp_path, monkeypatch, failure_point):
    ledger = _ledger(tmp_path)
    try:
        ledger.ensure_patient(1)
        ledger.save_messages([_message(body="本人の合成記録")])
        ledger.db.commit()
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        error = sqlite3.OperationalError("synthetic source failure")

        def fail(db, *_args):
            db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
            raise error

        if failure_point == "writable":
            monkeypatch.setattr(llm, "_source_writable", fail)
        else:
            monkeypatch.setattr(llm, "_source_writable", lambda *_args: True)
            monkeypatch.setattr(dependency_loss, "source_binding", fail)
        with pytest.raises(sqlite3.OperationalError) as caught:
            llm._claim(ledger, row)
        assert caught.value is error and not ledger.db.in_transaction
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None
        assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    finally:
        ledger.close()


def test_claim_does_not_touch_borrowed_transaction(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    try:
        ledger.ensure_patient(1)
        ledger.save_messages([_message(body="本人の合成記録")])
        ledger.db.commit()
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
        monkeypatch.setattr(llm, "_source_writable", lambda *_args: pytest.fail("borrowed transaction read"))
        assert llm._claim(ledger, row) is None
        assert ledger.db.in_transaction
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] == "synthetic"
        assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
        ledger.db.rollback()
        assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None
    finally:
        ledger.close()


def _final(ledger, row, value, *, keys=(0,)):
    return llm._commit_extraction_atomic(
        ledger, row, json.dumps(value, ensure_ascii=False), None, None, None, None,
        keys, check_current=False)


def test_final_and_cleanup_commit_together(prepared, monkeypatch):
    ledger, row, value = prepared
    renders = []
    monkeypatch.setattr(llm, "_rerender_cards",
                        lambda *args: renders.append(ledger.db.in_transaction))
    assert _final(ledger, row, {"summary": value["summary"]}) == "written"
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone()
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    assert renders == [False]


def test_cleanup_failure_rolls_back_final_and_retains_checkpoint(prepared, monkeypatch):
    ledger, row, value = prepared
    def fail(*args):
        raise RuntimeError("synthetic cleanup failure")
    monkeypatch.setattr(llm, "_delete_checkpoint_keys_tx", fail)
    with pytest.raises(RuntimeError, match="synthetic cleanup failure"):
        _final(ledger, row, {"summary": value["summary"]})
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone()
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()


def test_borrowed_transaction_is_not_committed_or_rendered(prepared, monkeypatch):
    ledger, row, value = prepared
    ledger.ensure_patient(1)
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
    renders = []
    monkeypatch.setattr(llm, "_rerender_cards", lambda *args: renders.append(True))
    assert _final(ledger, row, {"summary": value["summary"]}) == "rejected"
    assert not llm._replace_current(ledger, row, '{"summary":"合成"}')
    assert ledger.db.in_transaction and not renders
    ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone()


@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
def test_borrowed_replay_auto_rollback_preserves_error_identity(prepared, monkeypatch, code):
    ledger, row, value = prepared
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
    error = sqlite3.OperationalError("synthetic checkpoint failure")
    error.sqlite_errorcode = code
    renders = []

    def fail(db, _row):
        db.rollback()  # Simulate SQLite discarding the caller's transaction.
        raise error

    monkeypatch.setattr(llm, "_source_writable", fail)
    monkeypatch.setattr(llm, "_rerender_cards", lambda *_args: renders.append(True))
    with pytest.raises(sqlite3.OperationalError) as caught:
        llm._persist_chunks(ledger, row, {0: value}, chunk_size=row["_chunk_size"],
                            splits=row["_splits"], replay=True, lease=row["_current_lease"],
                            guard=row["_checkpoint_guard"])
    assert caught.value is error and caught.value.sqlite_errorcode == code
    assert not ledger.db.in_transaction and not renders
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1


@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_FULL,
                                  sqlite3.SQLITE_IOERR | (3 << 8)])
def test_capture_auto_rollback_preserves_error_and_previous_state(prepared, monkeypatch, code):
    ledger, row, _ = prepared
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
    previous_guard = row["_checkpoint_guard"]
    previous_identities = row["_checkpoint_identity"]
    error = sqlite3.DatabaseError("synthetic capture failure")
    error.sqlite_errorcode = code

    def fail(db, *_args, **_kwargs):
        db.rollback()
        raise error

    monkeypatch.setattr(llm, "_saved_chunk_size", fail)
    with pytest.raises(sqlite3.DatabaseError) as caught:
        llm._capture_checkpoint_state(ledger.db, row, None)
    assert caught.value is error and caught.value.sqlite_errorcode == code
    assert not ledger.db.in_transaction
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None
    assert row["_checkpoint_guard"] is previous_guard and row["_checkpoint_identity"] is previous_identities


def test_capture_success_does_not_hide_release_failure(prepared):
    ledger, row, _ = prepared
    previous_guard = row["_checkpoint_guard"]
    previous_identities = row["_checkpoint_identity"]
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic' WHERE project_id=1")
    ledger.db.set_authorizer(lambda action, arg1, arg2, *_: sqlite3.SQLITE_DENY
                             if action == sqlite3.SQLITE_SAVEPOINT and arg1 == "RELEASE"
                             and arg2 == "extract_checkpoint_snapshot" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            llm._capture_checkpoint_state(ledger.db, row, None)
        assert ledger.db.in_transaction
        assert row["_checkpoint_guard"] is previous_guard and row["_checkpoint_identity"] is previous_identities
    finally:
        ledger.db.set_authorizer(None)
        ledger.db.rollback()
    assert ledger.db.execute("SELECT fetch_reason FROM patients WHERE project_id=1").fetchone()[0] is None


def test_replaced_claim_with_same_expiry_is_not_the_same_owner(prepared):
    ledger, row, value = prepared
    claim = ledger.db.execute("SELECT payload FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
    payload = json.loads(claim[0])
    payload["owner_token"] = "synthetic-other-owner"
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='extract_claim'", (json.dumps(payload),))
    ledger.db.commit()
    assert _final(ledger, row, {"summary": value["summary"]}) == "rejected"
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()


def test_own_pending_started_and_completed_transitions_are_accepted(prepared):
    ledger, row, value = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    for status in ("pending", "started"):
        assert llm._persist_chunks(ledger, row, {0: {"summary": "合成A", "_quality_status": status}},
                                   **common)
    assert llm._persist_chunks(ledger, row, {0: value}, **common)
    assert _final(ledger, row, {"summary": value["summary"]}) == "written"


def test_identical_owned_pending_checkpoint_reuses_only_latest_record(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    identity = row["_checkpoint_identity"][0]
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    after = [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    assert after == before and row["_checkpoint_identity"][0] == identity
    _, saved = llm._saved_chunk_state(ledger.db, row, chunk_size=row["_chunk_size"])
    assert saved[0]["summary"] == "合成A" and saved[0]["_quality_status"] == "pending"


def test_pending_release_after_started_is_recorded_then_identical_pending_is_reused(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    started = {"summary": "合成A", "_quality_status": "started"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert llm._persist_chunks(ledger, row, {0: started}, **common)
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = [r[0] for r in ledger.db.execute("SELECT json_extract(meta,'$.quality_status') FROM artifacts "
                                              "WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    assert before[-3:] == ["pending", "started", "pending"]
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == len(before)


def test_intervening_invalid_row_prevents_pending_reuse(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    previous = ledger.db.execute("SELECT content,model,meta FROM artifacts WHERE kind='extract_llm_chunk' "
                                 "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    invalid_meta = {**json.loads(previous["meta"]), "invalidated": True}
    ledger.artifact_add("extract_llm_chunk", previous["content"], project_id=1, message_id=1,
                        model=previous["model"], meta=invalid_meta)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before + 1


def test_intervening_other_chunk_prevents_pending_reuse(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    previous = ledger.db.execute("SELECT content,model,meta FROM artifacts WHERE kind='extract_llm_chunk' "
                                 "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    meta = json.loads(previous["meta"])
    meta["chunk"] = 1
    ledger.artifact_add("extract_llm_chunk", previous["content"],
                        project_id=1,
                        message_id=1, model=previous["model"], meta=meta)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before + 1


def test_foreign_project_artifact_is_rejected_and_owned_pending_is_reused(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    previous = ledger.db.execute("SELECT content,model,meta FROM artifacts WHERE kind='extract_llm_chunk' "
                                 "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    ledger.ensure_patient(2)
    before = [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,project_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    with pytest.raises(sqlite3.IntegrityError, match="g1: artifacts message relation"):
        ledger.artifact_add("extract_llm_chunk", previous["content"], project_id=2, message_id=1,
                            model=previous["model"], meta=json.loads(previous["meta"]))
    ledger.db.rollback()
    assert [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,project_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")] == before
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,project_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")] == before


def test_pending_reuse_rejects_expired_lease(prepared, monkeypatch):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    monkeypatch.setattr(llm.time, "time", lambda: row["_current_lease"] + 1)
    assert not llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before


def test_pending_reuse_still_rerenders_after_the_owned_transaction(prepared, monkeypatch):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    renders = []
    monkeypatch.setattr(llm, "_rerender_cards", lambda *_args: renders.append(ledger.db.in_transaction))
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before
    assert renders == [False]


def test_pending_reuse_select_failure_is_not_suppressed(prepared, monkeypatch):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    monkeypatch.setattr(llm, "_source_writable", lambda *_args: True)
    monkeypatch.setattr(llm, "_owns_current_source", lambda *_args: True)
    monkeypatch.setattr(llm, "_checkpoint_matches", lambda *_args: True)
    ledger.db.set_authorizer(lambda action, table, *_args: sqlite3.SQLITE_DENY
                             if action == sqlite3.SQLITE_READ and table == "artifacts" else sqlite3.SQLITE_OK)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            llm._persist_chunks(ledger, row, {0: pending}, **common)
        assert not ledger.db.in_transaction
    finally:
        ledger.db.set_authorizer(None)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before


def test_identical_pending_is_not_reused_after_claim_owner_changes(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    raw = ledger.db.execute("SELECT payload FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
    payload = {**json.loads(raw), "owner_token": "synthetic-other-owner"}
    ledger.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='extract_claim'", (json.dumps(payload),))
    ledger.db.commit()
    assert not llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before


def test_identical_pending_is_not_reused_after_source_revision_aba(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    ledger.save_messages([_message(body="一時的な別の合成記録")])
    ledger.save_messages([_message(body=row["body_text"])])
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    assert not llm._persist_chunks(ledger, row, {0: pending}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before


def test_started_checkpoint_is_not_reused_as_an_idempotent_pending(prepared):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    started = {"summary": "合成A", "_quality_status": "started"}
    assert llm._persist_chunks(ledger, row, {0: started}, **common)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    assert llm._persist_chunks(ledger, row, {0: started}, **common)
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before + 1


def test_changed_pending_insert_error_propagates_without_replacing_checkpoint(prepared, monkeypatch):
    ledger, row, _ = prepared
    common = {"lease": row["_current_lease"], "guard": row["_checkpoint_guard"],
              "chunk_size": row["_chunk_size"], "splits": row["_splits"]}
    pending = {"summary": "合成A", "_quality_status": "pending"}
    assert llm._persist_chunks(ledger, row, {0: pending}, **common)
    before = [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    error = sqlite3.OperationalError("synthetic insert failure")
    def fail(*_args, **_kwargs):
        raise error
    monkeypatch.setattr(ledger, "artifact_add_tx", fail)
    with pytest.raises(sqlite3.OperationalError) as caught:
        llm._persist_chunks(ledger, row, {0: {**pending, "summary": "合成B"}}, **common)
    assert caught.value is error and not ledger.db.in_transaction
    assert [tuple(item) for item in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")] == before


def test_unrelated_new_key_is_preserved(prepared):
    ledger, row, value = prepared
    # The body is shorter than the normal root size, so use a synthetically
    # separate artifact with the same binding but a different valid key.
    old = ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    meta = json.loads(old["meta"])
    meta["chunk"] = 1
    other = ledger.artifact_add("extract_llm_chunk", old["content"], project_id=1,
                                message_id=1, model=old["model"], meta=meta)
    assert _final(ledger, row, {"summary": value["summary"]}) == "written"
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (other,)).fetchone()


def test_layout_change_after_snapshot_defers(prepared):
    ledger, row, value = prepared
    old = ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    meta = json.loads(old["meta"])
    meta.update(chunk=-1, chunk_size=10)
    layout_id = ledger.artifact_add("extract_llm_chunk", "{}", project_id=1,
                                    message_id=1, model=old["model"], meta=meta)
    assert _final(ledger, row, {"summary": value["summary"]}) == "rejected"
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (layout_id,)).fetchone()
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone()


def test_parent_split_change_after_snapshot_defers(prepared):
    ledger, row, value = prepared
    size = row["_chunk_size"]
    assert llm._persist_chunks(ledger, row, {0: {}}, chunk_size=size, splits={0: 10}, split_size=10)
    assert llm._persist_chunks(ledger, row, {"0:0": value}, chunk_size=size, splits={0: 10})
    llm._capture_checkpoint_state(ledger.db, row, None)
    assert llm._persist_chunks(ledger, row, {0: {}}, chunk_size=size, splits={0: 8}, split_size=8)
    assert _final(ledger, row, {"summary": value["summary"]}, keys=("0:0",)) == "rejected"
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm'").fetchone()


def test_protected_prior_hold_and_checkpoint_cleanup_commit_together(prepared):
    ledger, row, _ = prepared
    prior = {"symptoms": [{"text": "息切れ", "negated": True, "status": "ongoing",
                          "subject": "patient", "evidence": "本人に息切れはありません。"}]}
    prior_id = ledger.artifact_add("extract_llm", json.dumps(prior, ensure_ascii=False),
        project_id=1, message_id=1, meta={
            llm.inference.VERSION_KEY: llm._INFERENCE_VERSION, "hash": row["content_hash"],
            "source_binding": row["_source_binding"], "extract_version": llm.EXTRACT_VERSION - 1})
    assert _final(ledger, row, {"summary": "合成の別候補"}) == "held"
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (prior_id,)).fetchone()
    assert ledger.db.execute("SELECT 1 FROM artifacts WHERE kind=?", (llm.UPGRADE_KIND,)).fetchone()
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()


def test_identity_only_uses_the_same_metadata_selection_without_validating_content(prepared, monkeypatch):
    ledger, row, _ = prepared
    old = ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()
    original = json.loads(old["meta"])
    variants = []
    for key, value in (("invalidated", True), ("hash", "other"), ("context", "other"),
                       ("ver", llm.EXTRACT_VERSION + 1), ("chunk_size", row["_chunk_size"] + 1)):
        variant = dict(original)
        variant[key] = value
        variants.append((json.dumps(variant), old["content"], 1))
    variants.extend([
        ("{invalid", old["content"], 1),
        (old["meta"][:-1] + ', "extra": NaN}', old["content"], 1),
        (old["meta"][:-1] + ', "chunk": 1, "chunk": 0}', "{invalid", 1),
    ])
    for meta, content, project in variants:
        ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta) VALUES(?,?,?,?,?,?)",
            ("extract_llm_chunk", project, 1, content, old["model"], meta))
    ledger.db.commit()
    before_other_patient = {}
    llm._saved_chunk_state(ledger.db, row, chunk_size=row["_chunk_size"], identities=before_other_patient)
    ledger.ensure_patient(2)
    ledger.save_messages([_message(mid=2, project_id=2, body="別患者の合成記録")])
    ledger.artifact_add("extract_llm_chunk", old["content"], project_id=2, message_id=2,
                        model=old["model"], meta=original)
    full = {}
    llm._saved_chunk_state(ledger.db, row, chunk_size=row["_chunk_size"], identities=full)
    assert full == before_other_patient
    monkeypatch.setattr(llm.extract_validation, "validate",
                        lambda *args, **kwargs: pytest.fail("identity-only validated content"))
    fast = {}
    llm._saved_chunk_state(ledger.db, row, chunk_size=row["_chunk_size"],
                           identities=fast, identity_only=True)
    assert fast == full
    assert 0 in fast and fast[0][2] == "{invalid"


def test_guarded_unchanged_replay_uses_captured_validation(prepared, monkeypatch):
    ledger, row, value = prepared
    original = llm._saved_chunk_state

    def identity_only(*args, **kwargs):
        assert kwargs.get("identity_only") is True
        return original(*args, **kwargs)

    monkeypatch.setattr(llm, "_saved_chunk_state", identity_only)
    assert llm._persist_chunks(ledger, row, {0: value}, chunk_size=row["_chunk_size"],
                               splits=row["_splits"], replay=True, lease=row["_current_lease"],
                               guard=row["_checkpoint_guard"])


def test_multi_key_checkpoint_is_rejected_without_writes(prepared):
    ledger, row, value = prepared
    before = [tuple(record) for record in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    assert not llm._persist_chunks(ledger, row, {0: value, 1: value}, chunk_size=row["_chunk_size"],
                                   splits=row["_splits"], lease=row["_current_lease"],
                                   guard=row["_checkpoint_guard"])
    after = [tuple(record) for record in ledger.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id")]
    assert after == before and not ledger.db.in_transaction


@pytest.mark.parametrize("real_guard", [False, True])
def test_unproved_replay_uses_full_reader(prepared, monkeypatch, real_guard):
    ledger, row, value = prepared
    original = llm._saved_chunk_state
    calls = []

    def observed(*args, **kwargs):
        calls.append(kwargs.get("identity_only", False))
        return original(*args, **kwargs)

    monkeypatch.setattr(llm, "_saved_chunk_state", observed)
    if real_guard:
        row["_checkpoint_replay_snapshot"]["saved"][0] = {"summary": "合成の別候補"}
    guard = row["_checkpoint_guard"] if real_guard else object()
    assert llm._persist_chunks(ledger, row, {0: value}, chunk_size=row["_chunk_size"],
                               splits=row["_splits"], replay=True, lease=row["_current_lease"], guard=guard)
    assert False in calls


@pytest.mark.parametrize("mismatch", ["size", "context"])
def test_guard_does_not_authorize_a_different_checkpoint_contract(prepared, mismatch):
    ledger, row, value = prepared
    size = row["_chunk_size"] - 1 if mismatch == "size" else row["_chunk_size"]
    context = "synthetic other context" if mismatch == "context" else None
    assert not llm._persist_chunks(ledger, row, {0: value}, context, chunk_size=size,
                                   splits=row["_splits"], lease=row["_current_lease"],
                                   guard=row["_checkpoint_guard"])
    assert row["_checkpoint_conflict"]
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1


def test_ignored_started_insert_is_not_acknowledged(prepared):
    ledger, row, _ = prepared
    ledger.db.execute("""CREATE TRIGGER synthetic_ignore_started BEFORE INSERT ON artifacts
        WHEN NEW.kind='extract_llm_chunk' AND json_extract(NEW.meta, '$.quality_status')='started'
        BEGIN SELECT RAISE(IGNORE); END""")
    ledger.db.commit()
    assert not llm._persist_chunks(
        ledger, row, {0: {"summary": "合成A", "_quality_status": "started"}},
        chunk_size=row["_chunk_size"], splits=row["_splits"], lease=row["_current_lease"],
        guard=row["_checkpoint_guard"])
    assert row["_checkpoint_conflict"]
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1


def test_same_key_trigger_cannot_replace_the_acknowledged_value(prepared):
    ledger, row, value = prepared
    ledger.db.execute("""CREATE TRIGGER synthetic_replace_checkpoint AFTER INSERT ON artifacts
        WHEN NEW.kind='extract_llm_chunk' AND json_extract(NEW.meta, '$.quality_status')='started'
             AND NEW.content != '{"summary":"合成B"}'
        BEGIN
          INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at)
          VALUES(NEW.kind,NEW.project_id,NEW.message_id,'{"summary":"合成B"}',NEW.model,NEW.meta,NEW.created_at);
        END""")
    ledger.db.commit()
    assert not llm._persist_chunks(
        ledger, row, {0: {"summary": value["summary"], "_quality_status": "started"}},
        chunk_size=row["_chunk_size"], splits=row["_splits"], lease=row["_current_lease"],
        guard=row["_checkpoint_guard"])
    assert row["_checkpoint_conflict"]
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == 1
