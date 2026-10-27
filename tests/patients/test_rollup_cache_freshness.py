"""Synthetic regressions for current rollup cache inputs and snapshot boundaries.

These tests specify cache safety independently of the unfinished measurement
rollup changes. They use one temporary ledger and no live services or data.
"""
import json
import sqlite3
import time

import pytest

import extract_llm
from drug_map import PROGRESS_KIND
import history_walk
import inference_dependency_loss
import inference_version
import initial_import_state
import rollup
from common_testkit import make_ledger
from extract_testkit import _message
from ledger import LedgerReader, publish_snapshot
from snapshot_access import snapshot_meta


BODY = "本人の2026-10-02測定で体温36.4℃でした。"
EDITED = "本人の2026-10-02測定で体温37.2℃でした。"
POSTED = "2026-10-03T09:00:00+09:00"
CFG_A = {"local_llm": {"model": "synthetic-model-a"}}
CFG_B = {"local_llm": {"model": "synthetic-model-b"}}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    db = make_ledger(tmp_path)
    db.ensure_patient(1)
    with db.db:
        db.db.execute("UPDATE patients SET fetch_state='complete',fetch_reason=NULL,history_floor=-1 "
                      "WHERE project_id=1")
    db.save_messages([_message(mid=1, body=BODY, posted_at=POSTED)])
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(db.db, 1, 1)
    row["_target"] = ("", extract_llm.resolved_model(CFG_A))
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(db, row, json.dumps({"vitals": {"bt": 36.4}}), integrity=integrity)
    rollup.rebuild(db, 1, cfg=CFG_A)
    yield db
    db.close()


def cached(db):
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_rollup' "
                     "AND project_id=1 ORDER BY artifact_id DESC LIMIT 1").fetchone()
    return ((json.loads(row["content"]), json.loads(row["meta"]))
            if row is not None else (None, None))


def read(db, cfg=CFG_A):
    content, meta = cached(db)
    if content is None:
        return {}  # an invalidated cache is not a current measurement
    return rollup.current_cached_refs(db, 1, content, meta, cfg=cfg)


def assert_no_latest(result):
    assert not result.get("latest_vitals")
    assert not result.get("recent_labs")


def test_model_change_cannot_reuse_previous_model_cache(ledger):
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    assert_no_latest(read(ledger.db, CFG_B))


def test_configured_cache_read_rebuilds_all_sections_on_borrowed_snapshot(ledger):
    old, meta = cached(ledger.db)
    old['medications'] = [{'name': '合成の古い薬'}]
    old['recent_requests'] = [{'ctx': '合成の古い依頼'}]
    old['next_planned'] = '合成の古い予定'
    old['summary'] = {'text': '合成の古い要約'}
    before = json.dumps(old, ensure_ascii=False, sort_keys=True)
    ledger.db.execute("UPDATE messages SET body_text='合成の別の本文' WHERE project_id=1 AND message_id=1")
    try:
        assert ledger.db.in_transaction
        result = rollup.current_cached_refs(ledger.db, 1, old, meta, cfg=CFG_A)
        rendered = json.dumps(result, ensure_ascii=False)
        assert all(value not in rendered for value in (
            '合成の古い薬', '合成の古い依頼', '合成の古い予定', '合成の古い要約'))
        assert not result.get('latest_vitals')
        assert ledger.db.in_transaction
        assert json.dumps(old, ensure_ascii=False, sort_keys=True) == before
    finally:
        ledger.db.rollback()
    assert read(ledger.db)['latest_vitals']['bt'] == 36.4


def test_cache_without_config_is_unknown_not_an_empty_or_current_rollup(ledger):
    old, meta = cached(ledger.db)
    result = rollup.current_cached_refs(ledger.db, 1, old, meta)
    assert result['project_id'] == 1 and result['rollup_freshness'] == 'unknown'
    assert 'msg_count' not in result and 'medications' not in result
    assert result['measurement_coverage']['complete'] is False


def test_new_unparsed_source_invalidates_previous_latest(ledger):
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    ledger.save_messages([_message(mid=2, body="本人は体温を測定しました。", posted_at=POSTED)])
    assert_no_latest(read(ledger.db))


def test_failed_acquisition_invalidates_cache_without_erasing_source(ledger):
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    ledger.patient_fetch_failed(1, "network_error")
    assert_no_latest(read(ledger.db))
    source = ledger.db.execute("SELECT body_text,body_state FROM messages WHERE message_id=1").fetchone()
    assert source["body_text"] == BODY and source["body_state"] == "full"


def test_borrowed_uncommitted_acquisition_change_is_visible_but_not_committed(ledger):
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    assert not ledger.db.in_transaction
    ledger.db.execute("UPDATE patients SET fetch_state='incomplete',fetch_reason='network_error' "
                      "WHERE project_id=1")
    assert ledger.db.in_transaction
    assert_no_latest(read(ledger.db))
    assert ledger.db.in_transaction  # reader did not commit or roll back the borrow
    ledger.db.rollback()
    state = ledger.db.execute("SELECT fetch_state,fetch_reason FROM patients WHERE project_id=1").fetchone()
    assert (state["fetch_state"], state["fetch_reason"]) == ("complete", None)
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4


def test_withdrawn_cohort_completion_cannot_keep_its_old_latest(ledger):
    cohort = "a" * 32
    state = {"version": 1, "cohort_id": cohort, "boundary_ts": 1,
             "boundary_source": "server", "started_at": 1, "updated_at": 1,
             "capture_phase": "complete", "saved_count": 0,
             "capture_proof": {"v": 1, "scope": history_walk.scope(1, cohort), "digest": "b" * 64}}
    with ledger.db:
        ledger.db.execute("UPDATE patients SET initial_import=? WHERE project_id=1", (json.dumps(state),))
    rollup.rebuild(ledger, 1, cfg=CFG_A)
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    state["capture_phase"] = "held"
    state.pop("capture_proof")
    with ledger.db:
        ledger.db.execute("UPDATE patients SET initial_import=? WHERE project_id=1", (json.dumps(state),))
    assert_no_latest(read(ledger.db))


@pytest.mark.parametrize("kind", ["history", "thread", "reply"])
def test_unfinished_acquisition_job_is_not_a_complete_cache(ledger, kind):
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    assert ledger.job_add(kind, 1, message_id=1 if kind != "history" else 0) is not None
    assert_no_latest(read(ledger.db))


@pytest.mark.parametrize("state", [None, "pending", "unknown", "done"])
def test_acquisition_job_state_controls_measurement_completeness(ledger, state):
    job = ledger.job_add("history", 1, message_id=0)
    with ledger.db:
        ledger.db.execute("UPDATE fetch_jobs SET state=? WHERE job_id=?", (state, job))
    result = rollup.build_rollup(ledger.db, 1, cfg=CFG_A)
    assert result["measurement_coverage"]["complete"] is (state == "done")
    if state == "done":
        assert result["latest_vitals"]["bt"] == 36.4
    else:
        assert_no_latest(result)


@pytest.mark.parametrize("code", [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_acquisition_interruption_propagates_without_owning_transaction(ledger, monkeypatch, code):
    error = sqlite3.OperationalError("synthetic acquisition interruption")
    if code is not None:
        error.sqlite_errorcode = code

    def interrupted(*args):
        raise error

    monkeypatch.setattr(initial_import_state, "progress", interrupted)
    ledger.db.execute("BEGIN")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            rollup.build_rollup(ledger.db, 1, cfg=CFG_A)
        assert caught.value is error
        assert ledger.db.in_transaction
    finally:
        ledger.db.rollback()


def _synthetic_dictionary_cache(ledger):
    ledger.artifact_add(rollup.REF_KIND, '{}', project_id=1, message_id=1)
    ledger.artifact_add(PROGRESS_KIND, '{}')
    ref = {'name': '合成薬', 'status': 'unresolved', 'dict_id': 'synthetic',
           'dict_sha256': 'a' * 64, 'cands': []}
    content = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
               'project_id': 1, 'medications': [{'name': '合成薬', 'last': POSTED, 'ref': ref}]}
    meta = {inference_version.VERSION_KEY: inference_version.CURRENT_VERSION,
            'period_check_version': rollup.PERIOD_CHECK_VERSION}
    return content, meta


@pytest.mark.parametrize('code', [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_dictionary_revalidation_interruption_preserves_borrowed_transaction(ledger, monkeypatch, code):
    content, meta = _synthetic_dictionary_cache(ledger)
    error = sqlite3.OperationalError('synthetic dictionary interruption')
    if code is not None:
        error.sqlite_errorcode = code
    calls = []

    def interrupted(db, mid):
        calls.append(mid)
        raise error

    monkeypatch.setattr(rollup, 'current_refs', interrupted)
    ledger.db.execute("UPDATE patients SET fetch_reason='synthetic-change' WHERE project_id=1")
    try:
        with pytest.raises(sqlite3.OperationalError) as caught:
            rollup.current_cached_refs(ledger.db, 1, content, meta, cfg=None)
        assert caught.value is error and calls == [1]
        assert ledger.db.in_transaction
        assert ledger.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] == 'synthetic-change'
        assert content['medications'][0]['ref']['name'] == '合成薬'
    finally:
        ledger.db.rollback()
    assert ledger.db.execute('SELECT fetch_reason FROM patients WHERE project_id=1').fetchone()[0] is None


@pytest.mark.parametrize('operational', [False, True])
def test_dictionary_revalidation_other_db_failure_strips_only_annotation(ledger, monkeypatch, operational):
    content, meta = _synthetic_dictionary_cache(ledger)
    error = sqlite3.OperationalError('synthetic schema failure') if operational else sqlite3.DatabaseError('synthetic schema failure')
    error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    calls = []

    def unavailable(db, mid):
        calls.append(mid)
        raise error

    monkeypatch.setattr(rollup, 'current_refs', unavailable)
    result = rollup.current_cached_refs(ledger.db, 1, content, meta, cfg=None)
    assert calls == [1]
    assert result['medications'][0]['name'] == '合成薬'
    assert 'ref' not in result['medications'][0]
    assert content['medications'][0]['ref']['name'] == '合成薬'


@pytest.mark.parametrize('error_type', [sqlite3.DatabaseError, sqlite3.OperationalError])
def test_dictionary_revalidation_unknown_sqlite_error_is_not_retried(ledger, monkeypatch, error_type):
    content, meta = _synthetic_dictionary_cache(ledger)
    error = error_type('synthetic unclassified failure')
    calls = []

    def unavailable(db, mid):
        calls.append(mid)
        raise error

    monkeypatch.setattr(rollup, 'current_refs', unavailable)
    with pytest.raises(error_type) as caught:
        rollup.current_cached_refs(ledger.db, 1, content, meta, cfg=None)
    assert caught.value is error and calls == [1]
    assert content['medications'][0]['ref']['name'] == '合成薬'


def test_same_body_after_edit_is_not_the_original_occurrence(ledger):
    old_binding = inference_dependency_loss.source_binding(ledger.db, 1, 1)
    old_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    assert read(ledger.db)["latest_vitals"]["bt"] == 36.4
    ledger.save_messages([_message(mid=1, body=EDITED, posted_at=POSTED)])
    ledger.save_messages([_message(mid=1, body=BODY, posted_at=POSTED)])
    assert ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0] == old_hash
    assert inference_dependency_loss.source_binding(ledger.db, 1, 1) != old_binding
    assert_no_latest(read(ledger.db))


def test_old_generation_does_not_promote_its_stale_value(ledger):
    old = {inference_version.VERSION_KEY: inference_version.LEGACY_VERSION,
           "project_id": 1, "latest_vitals": {"bt": 39.9, "at": POSTED}}
    meta = {inference_version.VERSION_KEY: inference_version.LEGACY_VERSION}
    before = ledger.db.total_changes
    ledger.db.execute("PRAGMA query_only=ON")
    result = rollup.current_cached_refs(ledger.db, 1, old, meta, cfg=CFG_A)
    assert result.get("latest_vitals", {}).get("bt") != 39.9
    assert ledger.db.total_changes == before
    assert old["latest_vitals"]["bt"] == 39.9


def test_borrowed_published_snapshots_do_not_mix_acquisition_generations(ledger, tmp_path):
    db_path = ledger.db.execute("PRAGMA database_list").fetchone()[2]
    first_path = publish_snapshot(db_path, str(tmp_path / "first"))
    assert first_path is not None
    first = LedgerReader(first_path)
    try:
        first.db.execute("BEGIN")
        first_meta = snapshot_meta(first.db)
        assert first_meta["published"] is True
        assert read(first.db)["latest_vitals"]["bt"] == 36.4
        ledger.patient_fetch_failed(1, "network_error")
        second_path = publish_snapshot(db_path, str(tmp_path / "second"))
        assert second_path is not None
        second = LedgerReader(second_path)
        try:
            second.db.execute("BEGIN")
            assert snapshot_meta(second.db)["generation_id"] != first_meta["generation_id"]
            assert_no_latest(read(second.db))
            # An already borrowed snapshot remains self-consistent; its generation
            # is an as-of record and is not proof of the newer live state.
            assert snapshot_meta(first.db) == first_meta
            assert read(first.db)["latest_vitals"]["bt"] == 36.4
        finally:
            second.close()
    finally:
        first.close()


def prepare_synthetic_scale(ledger, count):
    """Populate a bounded synthetic patient before, never during, measurement."""
    if count not in (10, 100, 1000):
        raise ValueError("unsupported_synthetic_scale")
    integrity = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                 "completeness_chunks": 1,
                 "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    for mid in range(2, count + 1):
        ledger.save_messages([_message(mid=mid, body=BODY, posted_at=POSTED)])
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, 1, mid)
        row["_target"] = ("", extract_llm.resolved_model(CFG_A))
        assert extract_llm._replace_current(ledger, row, json.dumps({"vitals": {"bt": 36.4}}),
                                            integrity=integrity)
    rollup.rebuild(ledger, 1, cfg=CFG_A)


def profile_synthetic_cache(db, *, warmups=2, repetitions=5):
    """Return only timings, query counts and outcome for a seeded temporary DB.

    Query tracing is separate from timing because SQLite trace has overhead.
    The original implementation must be profiled separately on the same
    logical input; no production connection or external service is used.
    """
    if warmups < 0 or repetitions < 1:
        raise ValueError("bad_sample_count")
    for _ in range(warmups):
        read(db)
    query_count = 0

    def count_query(_statement):
        nonlocal query_count
        query_count += 1

    db.set_trace_callback(count_query)
    try:
        read(db)
    finally:
        db.set_trace_callback(None)
    samples, value = [], None
    for _ in range(repetitions):
        started = time.perf_counter_ns()
        value = read(db)
        samples.append(time.perf_counter_ns() - started)
    return {"query_count": query_count, "elapsed_ns": samples,
            "latest_present": bool(value.get("latest_vitals"))}
