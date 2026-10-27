"""Synthetic patient rollups distinguish measurement dates from posting dates."""
import json
import sqlite3

import pytest

import extract_llm
import inference_dependency_loss
import inference_version as inference
import history_walk
import notify_views
import mcs_queries
import rollup
from common_testkit import make_ledger
from extract_testkit import _hash, _message


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *a, **k: None)
    ledger = make_ledger(tmp_path)
    ledger.ensure_patient(1)
    cohort = "a" * 32
    capture = {"version": 1, "cohort_id": cohort, "boundary_ts": 1,
               "boundary_source": "server", "started_at": 1, "updated_at": 1,
               "capture_phase": "complete", "saved_count": 0,
               "capture_proof": {"v": 1, "scope": history_walk.scope(1, cohort), "digest": "b" * 64}}
    ledger.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1,initial_import=? WHERE project_id=1",
                      (json.dumps(capture),))
    ledger.db.commit()
    yield ledger
    ledger.close()


def _save(store, mid, posted_at, body, *, vitals=None, labs=None, project_id=1):
    store.save_messages([_message(mid=mid, project_id=project_id, body=body, posted_at=posted_at)])
    doc = {}
    if vitals is not None:
        doc["vitals"] = vitals
    if labs is not None:
        doc["labs"] = labs
    row = dict(store.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(store.db, project_id, mid)
    row["_target"] = ("", extract_llm.resolved_model({}))
    quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
               "completeness_chunks": 1,
               "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(store, row, json.dumps(doc, ensure_ascii=False), integrity=quality)


def _lab(body, value, day, *, subject="patient", status="current"):
    # A separate sentence must carry its own date and source quote.
    anchor = body.find("Cr")
    start = body.rfind("。", 0, anchor) + 1 if anchor >= 0 else 0
    return {"name": "Cr", "value": value, "unit": "mg/dL", "subject": subject,
            "status": status, "unverified": False, "evidence": body[start:],
            **({"measured_on": day} if day is not None else {})}


def _seed_out_of_order(store):
    newer_measurement = "本人の2026-10-02測定で体温36.4℃でした。本人の2026-10-02採血でCr 1.1mg/dLでした。"
    older_measurement = "本人の2026-10-01測定で体温37.2℃でした。本人の2026-10-01採血でCr 2.0mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", newer_measurement,
          labs=[_lab(newer_measurement, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    _save(store, 2, "2026-10-05T09:00:00+09:00", older_measurement,
          labs=[_lab(older_measurement, "2.0", "2026-10-01")], vitals={"bt": 37.2})


def test_measurement_day_not_posting_order_selects_latest(store):
    _seed_out_of_order(store)
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert [(item["value"], item["measured_on"]) for item in out["recent_labs"]] == [("1.1", "2026-10-02")]
    assert out["latest_vitals"]["bt"] == 36.4
    # The existing at field remains the selected source's posting time.
    assert out["latest_vitals"]["at"] == "2026-10-03T09:00:00+09:00"


def test_same_measurement_day_with_conflicting_values_has_no_latest_value(store):
    for mid, posted, value, temperature in (
        (1, "2026-10-03T09:00:00+09:00", "1.1", 36.4),
        (2, "2026-10-05T09:00:00+09:00", "2.0", 37.2),
    ):
        body = (f"本人の2026-10-02測定で体温{temperature}℃でした。"
                f"本人の2026-10-02採血でCr {value}mg/dLでした。")
        _save(store, mid, posted, body, labs=[_lab(body, value, "2026-10-02")], vitals={"bt": temperature})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs")
    assert not out.get("latest_vitals")
    labs = [r for r in out["measurement_reports"]["items"] if r["kind"] == "lab"]
    assert {r["value"] for r in labs} == {"1.1", "2"}
    assert all(r["measured_on"] == "2026-10-02" for r in labs)


def test_same_measurement_day_with_matching_values_remains_usable(store):
    for mid, posted in ((1, "2026-10-03T09:00:00+09:00"), (2, "2026-10-05T09:00:00+09:00")):
        body = "本人の2026-10-02測定で体温36.4℃でした。本人の2026-10-02採血でCr 1.1mg/dLでした。"
        _save(store, mid, posted, body, labs=[_lab(body, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert [item["value"] for item in out["recent_labs"]] == ["1.1"]
    assert out["latest_vitals"]["bt"] == 36.4


def test_undated_new_report_does_not_create_order_or_revive_old_latest(store):
    known = "本人の2026-10-02測定で体温36.4℃でした。本人の2026-10-02採血でCr 1.1mg/dLでした。"
    undated = "本人の採血Cr 2.0mg/dL、体温37.2℃を測定しました。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", known,
          labs=[_lab(known, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    _save(store, 2, "2026-10-05T09:00:00+09:00", undated,
          labs=[_lab(undated, "2.0", None)], vitals={"bt": 37.2})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs")
    assert not out.get("latest_vitals")
    unknown = [r for r in out["measurement_reports"]["items"] if r["message_id"] == 2]
    assert unknown and all(r["measured_on"] is None for r in unknown)


@pytest.mark.parametrize("scope", ["family", "planned", "past"])
def test_other_person_or_explicit_noncurrent_report_does_not_replace_patient_value(store, scope):
    body = "本人の2026-10-02測定で体温36.4℃でした。本人の2026-10-02採血でCr 1.1mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(body, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    later = ("家族の2026-10-04採血Cr 2.0mg/dL、体温37.2℃を測定しました。" if scope == "family" else
             "本人の2026-10-04採血Cr 2.0mg/dL、体温37.2℃の測定を予定しています。" if scope == "planned" else
             "本人は以前の2026-10-04採血Cr 2.0mg/dL、体温37.2℃の結果を共有しました。")
    _save(store, 2, "2026-10-05T09:00:00+09:00", later,
          labs=[_lab(later, "2.0", "2026-10-04", subject="family" if scope == "family" else "patient",
                     status=scope if scope in ("planned", "past") else "current")], vitals={"bt": 37.2})
    out = rollup.build_rollup(store.db, 1, cfg={})
    prior = [item for item in out["measurement_reports"]["items"]
             if item["kind"] == "vitals" and item["message_id"] == 1]
    assert prior and prior[0]["values"]["bt"] == 36.4
    if scope in ("family", "planned"):
        assert out["latest_vitals"]["bt"] == 36.4
    else:
        # 10/04 is later than 10/02 even when it is described as a past event.
        assert not out.get("latest_vitals")
        unresolved = [item for item in out["measurement_reports"]["items"]
                      if item["kind"] == "vitals" and item["message_id"] == 2]
        assert unresolved and unresolved[0]["state"] == "unconfirmed"
        assert unresolved[0]["content_hash"] == _hash(store, 2)
    if scope == "family":
        assert [item["value"] for item in out["recent_labs"]] == ["1.1"]
    else:
        # The lab date/subject is not independently bound by the model's status.
        assert not out.get("recent_labs")


def test_noncurrent_vital_shortcuts_refuse_ambiguous_sentences():
    classify = rollup._noncurrent_vital_reference
    assert classify("家族の2026-10-04体温37.2℃でした。本人の体温も37.2℃でした。", {"bt": 37.2}) is None
    assert classify("本人の体温37.2℃の測定を予定していません。", {"bt": 37.2}) is None
    assert classify("本人は測定後に体温37.2℃の測定を予定しています。", {"bt": 37.2}) is None
    assert classify("本人は以前の体温37.2℃、現在の体温36.4℃を報告しました。", {"bt": 37.2}) is None


def test_incomplete_acquisition_does_not_claim_latest_measurement(store):
    _seed_out_of_order(store)
    store.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    store.db.commit()
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs") and not out.get("latest_vitals")
    assert out["measurement_coverage"]["complete"] is False
    assert {r["message_id"] for r in out["measurement_reports"]["items"]} == {1, 2}


def test_saved_rollup_freshness_changes_when_acquisition_becomes_incomplete(store):
    _seed_out_of_order(store)
    rollup.rebuild(store, 1, cfg={})
    assert rollup.inference_coverage(store.db, 1)["current"] is True
    with store.db:
        store.db.execute("UPDATE patients SET fetch_state='incomplete' WHERE project_id=1")
    assert rollup.inference_coverage(store.db, 1)["current"] is False


def test_saved_rollup_freshness_changes_when_extraction_metadata_changes_in_place(store):
    _seed_out_of_order(store)
    rollup.rebuild(store, 1, cfg={})
    assert rollup.inference_coverage(store.db, 1)["current"] is True
    row = store.db.execute(
        "SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm' AND message_id=1"
    ).fetchone()
    meta = json.loads(row["meta"])
    meta["invalidated"] = True
    with store.db:
        store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
    assert rollup.inference_coverage(store.db, 1)["current"] is False


def test_legacy_history_floor_is_not_cohort_proof(store):
    _seed_out_of_order(store)
    store.db.execute("UPDATE patients SET initial_import=NULL WHERE project_id=1")
    store.db.commit()
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["history_state"] == "legacy_floor"
    assert out["measurement_coverage"]["complete"] is True
    assert out["measurement_reports"]["total"] > 0


@pytest.mark.parametrize("field,value", [
    ("actual_model", "old-model"),
    ("plan_version", -1),
    ("integrity", {"completeness_chunks": 1, "completeness": {"0": {"state": "pending"}}}),
])
def test_unfinished_or_old_model_is_not_a_complete_measurement_source(store, field, value):
    body = "本人の2026-10-02測定で体温36.4℃、Cr 1.1mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(body, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    row = store.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()
    meta = json.loads(row["meta"])
    meta[field] = value
    with store.db:
        store.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), row["artifact_id"]))
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["complete"] is False
    assert not out.get("latest_vitals") and not out.get("recent_labs")
    assert not out["measurement_reports"]["items"]


def test_absent_configuration_and_canonical_without_policy_cannot_prove_source(store):
    body = "本人の2026-10-02測定で体温36.4℃、Cr 1.1mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(body, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    out = rollup.build_rollup(store.db, 1)
    assert out["measurement_coverage"]["complete"] is False
    assert not out.get("latest_vitals") and not out.get("recent_labs")
    row = store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert not rollup._measurement_source_bound(store.db, row, "canonical_projection", {})


def test_candidate_fingerprint_handles_rollup_internal_sentinel_and_binds_policy(monkeypatch):
    import semantic_candidate

    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    before = semantic_candidate.candidate_fingerprint({})
    assert isinstance(before, str) and len(before) == 64
    monkeypatch.setattr(rollup, 'PERIOD_CHECK_VERSION', rollup.PERIOD_CHECK_VERSION + 1)
    monkeypatch.setattr(semantic_candidate, '_LOADED_SOURCES', None)
    assert semantic_candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize('count', [0, 1, 64, 65])
def test_source_batch_matches_single_and_preserves_duplicates_and_page_limit(store, monkeypatch, count):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    row = store.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    expected = rollup._measurement_source_bound(store.db, row, 'extract_llm', {})
    assert expected
    sizes = []
    original = extract_llm.progress_for_messages

    def record(db, rows, *, model):
        sizes.append(len(rows))
        return original(db, rows, model=model)

    monkeypatch.setattr(extract_llm, 'progress_for_messages', record)
    assert rollup._measurement_sources_bound(store.db, [row] * count, ['extract_llm'] * count, refs, {}) == [expected] * count
    assert sizes == ([64, 1] if count == 65 else [count] if count else [])


def test_source_batch_matches_single_for_scope_model_and_source_selection(store):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    row = store.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    other = {**dict(row), 'project_id': 2}
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    rows = [row, other, row, row, row]
    kinds = ['extract_llm', 'extract_llm', 'canonical_projection', 'semantic_facts_v4', None]
    expected = [rollup._measurement_source_bound(store.db, item, kind, {}) for item, kind in zip(rows, kinds)]
    assert expected == [True, False, False, False, False]
    assert rollup._measurement_sources_bound(store.db, rows, kinds, refs, {}) == expected
    assert rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], refs, None) == [False]
    wrong_model = {'local_llm': {'model': 'a-different-model'}}
    assert not rollup._measurement_source_bound(store.db, row, 'extract_llm', wrong_model)
    assert rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], refs, wrong_model) == [
        rollup._measurement_source_bound(store.db, row, 'extract_llm', wrong_model)]


def test_source_batch_matches_single_after_source_aba_and_missing_artifact(store):
    original = '本人の2026-10-02測定で体温36.4℃でした。'
    changed = '本人の2026-10-02測定で体温38.1℃でした。'
    posted = '2026-10-03T09:00:00+09:00'
    _save(store, 1, posted, original, vitals={'bt': 36.4})
    store.save_messages([_message(mid=1, project_id=1, body=changed, posted_at=posted)])
    store.save_messages([_message(mid=1, project_id=1, body=original, posted_at=posted)])
    row = store.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    expected = rollup._measurement_source_bound(store.db, row, 'extract_llm', {})
    assert not expected
    assert rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], refs, {}) == [expected]
    assert rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], {}, {}) == [False]


@pytest.mark.parametrize('field,value', [
    ('actual_model', 'old-model'),
    ('plan_version', -1),
    ('integrity', {'completeness_chunks': 1, 'completeness': {'0': {'state': 'pending'}}}),
])
def test_source_batch_matches_single_for_invalid_or_incomplete_artifact(store, field, value):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    artifact = store.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='extract_llm' AND message_id=1").fetchone()
    meta = json.loads(artifact['meta'])
    meta[field] = value
    with store.db:
        store.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), artifact['artifact_id']))
    row = store.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    assert not rollup._measurement_source_bound(store.db, row, 'extract_llm', {})
    assert rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], refs, {}) == [False]


@pytest.mark.parametrize('code', [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_source_batch_does_not_retry_interrupted_sql(store, monkeypatch, code):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    row = store.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone()
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    error = sqlite3.OperationalError('synthetic interruption')
    if code is not None:
        error.sqlite_errorcode = code

    def interrupted(*args, **kwargs):
        raise error

    monkeypatch.setattr(extract_llm, 'progress_for_messages', interrupted)
    monkeypatch.setattr(extract_llm, 'progress_for_message', lambda *a, **k: pytest.fail('unexpected retry'))
    with pytest.raises(sqlite3.OperationalError) as raised:
        rollup._measurement_sources_bound(store.db, [row], ['extract_llm'], refs, {})
    assert raised.value is error


@pytest.mark.parametrize('code', [None, sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_single_fallback_stops_on_interruption_after_noninterrupt_batch_failure(store, monkeypatch, code):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    for mid in (1, 2):
        _save(store, mid, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    rows = list(store.db.execute('SELECT * FROM messages WHERE project_id=1 ORDER BY message_id'))
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    batch_error = sqlite3.OperationalError('too many SQL variables')
    batch_error.sqlite_errorcode = sqlite3.SQLITE_ERROR
    interrupted = sqlite3.OperationalError('synthetic interruption')
    if code is not None:
        interrupted.sqlite_errorcode = code
    visited = []

    def fail_batch(*args, **kwargs):
        raise batch_error

    def fail_first(db, row, *, model):
        visited.append(row['message_id'])
        raise interrupted

    monkeypatch.setattr(extract_llm, 'progress_for_messages', fail_batch)
    monkeypatch.setattr(extract_llm, 'progress_for_message', fail_first)
    with pytest.raises(sqlite3.OperationalError) as raised:
        rollup._measurement_sources_bound(store.db, rows, ['extract_llm'] * 2, refs, {})
    assert raised.value is interrupted
    assert visited == [1]


def test_single_fallback_keeps_other_operational_errors_unverified(store, monkeypatch):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    for mid in (1, 2):
        _save(store, mid, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    rows = list(store.db.execute('SELECT * FROM messages WHERE project_id=1 ORDER BY message_id'))
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    original = extract_llm.progress_for_message
    visited = []

    def fail_batch(*args, **kwargs):
        error = sqlite3.OperationalError('too many SQL variables')
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        raise error

    def progress(db, row, *, model):
        visited.append(row['message_id'])
        if row['message_id'] == 1:
            error = sqlite3.OperationalError('synthetic noninterrupt failure')
            error.sqlite_errorcode = sqlite3.SQLITE_ERROR
            raise error
        return original(db, row, model=model)

    monkeypatch.setattr(extract_llm, 'progress_for_messages', fail_batch)
    monkeypatch.setattr(extract_llm, 'progress_for_message', progress)
    assert rollup._measurement_sources_bound(store.db, rows, ['extract_llm'] * 2, refs, {}) == [False, True]
    assert visited == [1, 2]


@pytest.mark.parametrize('reference', ['missing', 'other_message', 'superseded'])
def test_single_fallback_does_not_bypass_selected_artifact_reference(store, monkeypatch, reference):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    for mid in (1, 2):
        _save(store, mid, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    rows = list(store.db.execute('SELECT * FROM messages WHERE project_id=1 ORDER BY message_id'))
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    if reference == 'missing':
        del refs[(1, 'extract_llm')]
    elif reference == 'other_message':
        refs[(1, 'extract_llm')] = refs[(2, 'extract_llm')]
    else:
        old = store.db.execute('SELECT content,model,meta FROM artifacts WHERE artifact_id=?',
                               (refs[(1, 'extract_llm')],)).fetchone()
        store.artifact_add('extract_llm', old['content'], project_id=1, message_id=1,
                           model=old['model'], meta=json.loads(old['meta']))
    kinds = ['extract_llm'] * 2
    assert rollup._measurement_sources_bound(store.db, rows, kinds, refs, {}) == [False, True]

    def fail_batch(*args, **kwargs):
        error = sqlite3.OperationalError('too many SQL variables')
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        raise error

    monkeypatch.setattr(extract_llm, 'progress_for_messages', fail_batch)
    assert rollup._measurement_sources_bound(store.db, rows, kinds, refs, {}) == [False, True]


def test_source_batch_replays_in_order_if_later_source_check_raises(store, monkeypatch):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    for mid in (1, 2):
        _save(store, mid, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    rows = list(store.db.execute('SELECT * FROM messages WHERE project_id=1 ORDER BY message_id'))
    blobs = mcs_queries.current_extraction_blobs(store.db, 1)
    refs = rollup._clinical_artifact_refs(store.db, 1, blobs)
    original = inference_dependency_loss.source_binding

    def binding(db, project_id, message_id):
        if message_id == 2:
            raise ValueError('later source')
        return original(db, project_id, message_id)

    def earlier_progress(*args, **kwargs):
        raise RuntimeError('earlier progress')

    monkeypatch.setattr(inference_dependency_loss, 'source_binding', binding)
    def fail_batch(*args, **kwargs):
        error = sqlite3.OperationalError('too many SQL variables')
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        raise error

    monkeypatch.setattr(extract_llm, 'progress_for_messages', fail_batch)
    monkeypatch.setattr(extract_llm, 'progress_for_message', earlier_progress)
    with pytest.raises(RuntimeError, match='earlier progress'):
        rollup._measurement_sources_bound(store.db, rows, ['extract_llm'] * 2, refs, {})


def test_build_rollup_preserves_borrowed_transaction_and_rollback(store):
    body = '本人の2026-10-02測定で体温36.4℃でした。'
    _save(store, 1, '2026-10-03T09:00:00+09:00', body, vitals={'bt': 36.4})
    store.db.execute('BEGIN')
    store.db.execute("UPDATE patients SET fetch_state='failed' WHERE project_id=1")
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out['measurement_coverage']['complete']
    assert store.db.in_transaction
    store.db.rollback()
    assert store.db.execute('SELECT fetch_state FROM patients WHERE project_id=1').fetchone()[0] == 'complete'


@pytest.mark.parametrize("suffix", ["ではありません。", "かもしれません。", "か？"])
def test_lab_negation_or_uncertainty_outside_extracted_quote_cannot_be_observation(store, suffix):
    quote = "2026-10-02測定でCr 1.1mg/dL"
    body = "本人の" + quote + suffix
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(quote, "1.1", "2026-10-02")])
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs")
    assert not [r for r in out["measurement_reports"]["items"] if r.get("kind") == "lab"]


@pytest.mark.parametrize("date", ["2026-09-01（昨日）", "2026-09-01（水曜日）"])
def test_inconsistent_lab_date_is_not_promoted(store, date):
    body = f"本人の{date}採血でCr 1.1mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(body, "1.1", "2026-09-01")])
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs")
    assert not [r for r in out["measurement_reports"]["items"] if r.get("kind") == "lab"]


def test_measurement_date_cannot_move_between_different_actions(store):
    body = "本人の2026-10-02測定で脈拍72、その後体温36.4℃を測定しました。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body, vitals={"bt": 36.4})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("latest_vitals")


@pytest.mark.parametrize("suffix", [
    "ではありません。", "ではない。", "かもしれません。", "の可能性があります。",
    "は未確認です。", "の測定はしていません。", "という値は誤りです。",
])
def test_uncertain_vital_is_neither_latest_nor_a_reported_measurement(store, suffix):
    body = "本人の2026-10-02測定で体温36.4℃" + suffix
    _save(store, 1, "2026-10-03T09:00:00+09:00", body, vitals={"bt": 36.4})
    message = store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    grounded, _ = rollup._dated_vitals(message, {"bt": 36.4}, None)
    assert grounded is False
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("latest_vitals")
    assert not [item for item in out["measurement_reports"]["items"]
                if item.get("kind") == "vitals" and item.get("values")]
    assert message["body_text"] == body


@pytest.mark.parametrize("body", [
    "本人の2026-10-02測定で体温36.4℃でした。本人に咳はありません。",
    "本人の2026-10-02測定で体温36.4℃、咳はありません。",
])
def test_other_symptom_negation_does_not_negate_the_temperature(store, body):
    _save(store, 1, "2026-10-03T09:00:00+09:00", body, vitals={"bt": 36.4})
    message = store.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    assert rollup._dated_vitals(message, {"bt": 36.4}, None) == (True, "2026-10-02")
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["latest_vitals"]["bt"] == 36.4


def test_lab_date_is_not_borrowed_across_a_comma(store):
    body = "本人の2026-10-02測定で体温36.4℃、Cr 1.1mg/dLでした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body,
          labs=[_lab(body, "1.1", "2026-10-02")], vitals={"bt": 36.4})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("recent_labs")
    assert out["latest_vitals"]["bt"] == 36.4
    lab, = [item for item in out["measurement_reports"]["items"] if item["kind"] == "lab"]
    assert lab["measured_on"] is None


def test_date_of_a_separate_event_never_dates_the_vital(store):
    body = "本人は2026-10-02に採血しました。体温36.4℃でした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", body, vitals={"bt": 36.4})
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert not out.get("latest_vitals")
    report, = out["measurement_reports"]["items"]
    assert report["values"] == {"bt": 36.4} and report["measured_on"] is None


def test_old_cached_value_is_revalidated_read_only(store):
    _seed_out_of_order(store)
    cached = {inference.VERSION_KEY: inference.CURRENT_VERSION, "project_id": 1,
              "latest_vitals": {"at": "2026-10-05T09:00:00+09:00", "bt": 37.2},
              "recent_labs": [{"at": "2026-10-05T09:00:00+09:00", "name": "Cr", "value": "2.0"}],
              "profile_extension": {"text": "完全合成"}}
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION, "period_check_version": 13}
    store.db.execute("PRAGMA query_only=ON")
    before = store.db.total_changes
    out = rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
    assert out["latest_vitals"]["bt"] == 36.4
    assert out["latest_vitals"]["at"] == "2026-10-03T09:00:00+09:00"
    assert out["recent_labs"][0]["value"] == "1.1"
    assert out["profile_extension"] == cached["profile_extension"]
    assert store.db.total_changes == before


@pytest.mark.parametrize('legacy', [True, False])
@pytest.mark.parametrize('code', [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_cache_revalidation_propagates_interruption_without_finishing_borrowed_transaction(store, monkeypatch, legacy, code):
    cached = {inference.VERSION_KEY: inference.CURRENT_VERSION, 'project_id': 1,
              'latest_vitals': {'bt': 36.4}, 'profile_extension': {'text': '合成'}}
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
            'period_check_version': rollup.PERIOD_CHECK_VERSION - 1 if legacy else rollup.PERIOD_CHECK_VERSION}
    error = sqlite3.OperationalError('synthetic interruption')
    error.sqlite_errorcode = code
    calls = []

    def fail_build(*args, **kwargs):
        calls.append(1)
        raise error

    monkeypatch.setattr(rollup, 'build_rollup', fail_build)
    store.db.execute('BEGIN')
    store.db.execute("UPDATE patients SET fetch_state='failed' WHERE project_id=1")
    with pytest.raises(sqlite3.OperationalError) as raised:
        rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
    assert raised.value is error and calls == [1]
    assert store.db.in_transaction
    assert store.db.execute('SELECT fetch_state FROM patients WHERE project_id=1').fetchone()[0] == 'failed'
    store.db.rollback()
    assert store.db.execute('SELECT fetch_state FROM patients WHERE project_id=1').fetchone()[0] == 'complete'


@pytest.mark.parametrize('legacy', [True, False])
def test_cache_revalidation_other_operational_failure_remains_unverified(store, monkeypatch, legacy):
    cached = {inference.VERSION_KEY: inference.CURRENT_VERSION, 'project_id': 1,
              'latest_vitals': {'bt': 36.4}, 'profile_extension': {'text': '合成'}}
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
            'period_check_version': rollup.PERIOD_CHECK_VERSION - 1 if legacy else rollup.PERIOD_CHECK_VERSION}

    def fail_build(*args, **kwargs):
        error = sqlite3.OperationalError('synthetic noninterrupt failure')
        error.sqlite_errorcode = sqlite3.SQLITE_ERROR
        raise error

    monkeypatch.setattr(rollup, 'build_rollup', fail_build)
    out = rollup.current_cached_refs(store.db, 1, cached, meta, cfg={})
    assert 'latest_vitals' not in out
    assert out['measurement_coverage'] == {'complete': False, 'reason': 'measurement_read_failed'}
    assert out['profile_extension'] == cached['profile_extension']


def test_patient_summary_shows_selected_source_posting_time(store):
    _seed_out_of_order(store)
    rollup.rebuild(store, 1, cfg={})
    _, text = notify_views.patient_summary_text(store.db, 1, cfg={})
    line = next(line for line in text.splitlines() if "36.4" in line)
    assert "2026-10-03T09:00:00+09:00" in line
    assert "2026-10-05T09:00:00+09:00" not in line and "37.2" not in line


@pytest.mark.parametrize("state", ["pending", "downloaded", "withdrawn"])
def test_any_same_patient_attachment_holds_measurement_completeness(store, state):
    observation = "本人の2026-10-02測定で体温36.4℃でした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", observation, vitals={"bt": 36.4})
    store.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(1,'synthetic',?)", (state,))
    assert store.db.in_transaction
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert store.db.in_transaction  # The snapshot read must not commit its caller's write.
    assert out["measurement_coverage"]["attachment_count"] == 1
    assert not out["measurement_coverage"]["complete"]
    assert not out.get("latest_vitals")
    store.db.commit()
    rollup.rebuild(store, 1, cfg={})
    _, text = notify_views.patient_summary_text(store.db, 1, cfg={})
    assert "■ バイタル: 未確認" in text


def test_other_patient_attachment_does_not_hide_current_patient_measurements(store):
    observation = "本人の2026-10-02測定で体温36.4℃でした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", observation, vitals={"bt": 36.4})
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="合成", posted_at="2026-10-04T09:00:00+09:00")])
    store.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(2,'synthetic','pending')")
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["attachment_count"] == 0
    assert out["latest_vitals"]["bt"] == 36.4


@pytest.mark.parametrize("missing_column", [False, True])
def test_legacy_attachment_schema_is_unknown_not_empty(store, missing_column):
    observation = "本人の2026-10-02測定で体温36.4℃でした。"
    _save(store, 1, "2026-10-03T09:00:00+09:00", observation, vitals={"bt": 36.4})
    baseline = rollup._message_source_signature(store.db, 1, 1)
    store.db.execute("DROP TABLE attachments")
    if missing_column:
        store.db.execute("CREATE TABLE attachments(attachment_id INTEGER)")
    assert rollup._message_source_signature(store.db, 1, 1) != baseline
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["attachment_count"] is None
    assert not out["measurement_coverage"]["complete"]
    assert not out.get("latest_vitals")


@pytest.mark.parametrize("statement", ["PRAGMA table_info(attachments)", "SELECT COUNT(*) FROM attachments"])
@pytest.mark.parametrize("code", [sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_attachment_schema_and_count_sql_errors_propagate(store, statement, code):
    error = sqlite3.OperationalError("synthetic attachment read failure")
    error.sqlite_errorcode = code

    class FailingConnection:
        def __getattr__(self, name):
            return getattr(store.db, name)

        def execute(self, sql, *args):
            if sql.startswith(statement):
                raise error
            return store.db.execute(sql, *args)

    with pytest.raises(sqlite3.OperationalError) as caught:
        rollup.build_rollup(FailingConnection(), 1, cfg={})
    assert caught.value is error


def test_null_body_is_incomplete_but_empty_body_is_not_missing(store):
    _save(store, 1, "2026-10-03T09:00:00+09:00", "本人の2026-10-02測定で体温36.4℃でした。",
          vitals={"bt": 36.4})
    store.save_messages([_message(mid=2, project_id=1, body="", posted_at="2026-10-04T09:00:00+09:00")])
    assert rollup.build_rollup(store.db, 1, cfg={})["measurement_coverage"]["complete"]
    store.db.execute("UPDATE messages SET body_text=NULL WHERE message_id=2")
    assert store.db.in_transaction
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert store.db.in_transaction  # The snapshot read must not commit its caller's write.
    assert out["measurement_coverage"]["incomplete_sources"] == 1
    assert not out.get("latest_vitals")
    store.db.commit()
    rollup.rebuild(store, 1, cfg={})
    _, text = notify_views.patient_summary_text(store.db, 1, cfg={})
    assert "■ バイタル: 未確認" in text


def test_signature_tracks_patient_attachment_presence_and_borrowed_rollback(store):
    _save(store, 1, "2026-10-03T09:00:00+09:00", "本人の2026-10-02測定で体温36.4℃でした。",
          vitals={"bt": 36.4})
    store.ensure_patient(2)
    store.save_messages([_message(mid=2, project_id=2, body="合成", posted_at="2026-10-04T09:00:00+09:00")])
    rollup.rebuild(store, 1, cfg={})
    assert 1 not in rollup.dirty_projects(store)
    base = rollup._message_source_signature(store.db, 1, 1)
    store.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(2,'other','pending')")
    assert rollup._message_source_signature(store.db, 1, 1) == base
    store.db.execute("INSERT INTO attachments(message_id,file_id,state) VALUES(1,'own','pending')")
    assert rollup._message_source_signature(store.db, 1, 1) != base
    assert 1 in rollup.dirty_projects(store)
    assert store.db.in_transaction
    store.db.rollback()
    assert rollup._message_source_signature(store.db, 1, 1) == base
    assert 1 not in rollup.dirty_projects(store)


def test_signature_tracks_missing_body_type_without_changing_content_hash(store):
    _save(store, 1, "2026-10-03T09:00:00+09:00", "本人の2026-10-02測定で体温36.4℃でした。",
          vitals={"bt": 36.4})
    base = rollup._message_source_signature(store.db, 1, 1)
    original_hash = store.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    store.db.execute("UPDATE messages SET body_text=NULL WHERE message_id=1")
    assert rollup._message_source_signature(store.db, 1, 1) != base
    assert store.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0] == original_hash
    store.db.rollback()
    assert rollup._message_source_signature(store.db, 1, 1) == base


@pytest.mark.parametrize("empty_html", ["", "<p> </p>"])
def test_empty_body_needs_matching_html_and_signature_tracks_in_place_mismatch(store, empty_html):
    _save(store, 1, "2026-10-03T09:00:00+09:00", "本人の2026-10-02測定で体温36.4℃でした。",
          vitals={"bt": 36.4})
    store.save_messages([_message(mid=2, project_id=1, body=empty_html, posted_at="2026-10-04T09:00:00+09:00")])
    base = rollup._message_source_signature(store.db, 1, 1)
    assert rollup.build_rollup(store.db, 1, cfg={})["measurement_coverage"]["complete"]
    store.db.execute("UPDATE messages SET body_html='合成の本文' WHERE message_id=2")
    assert rollup._message_source_signature(store.db, 1, 1) != base
    out = rollup.build_rollup(store.db, 1, cfg={})
    assert out["measurement_coverage"]["incomplete_sources"] == 1
    assert not out.get("latest_vitals")
    store.db.rollback()
    assert rollup._message_source_signature(store.db, 1, 1) == base


def test_empty_proofs_isolate_patient_and_preserve_invalid_rows(store):
    store.ensure_patient(2)
    store.save_messages([_message(mid=mid, project_id=pid, body="", posted_at="2026-10-04T09:00:00+09:00")
                         for mid, pid in ((1, 1), (2, 1), (3, 2))])
    store.db.execute("UPDATE messages SET body_html='合成の本文' WHERE message_id IN (2,3)")
    assert rollup._empty_body_proofs(store.db, 1, html_available=True) == {1: True, 2: False}
    assert rollup._empty_body_proofs(store.db, 2, html_available=True) == {3: False}
    store.db.rollback()


def test_many_empty_messages_use_bounded_html_queries_per_reader(store):
    _save(store, 1, "2026-10-03T09:00:00+09:00", "本人の2026-10-02測定で体温36.4℃でした。",
          vitals={"bt": 36.4})
    store.save_messages([_message(mid=mid, project_id=1, body="", posted_at="2026-10-04T09:00:00+09:00")
                         for mid in range(2, 12)])
    statements = []
    store.db.set_trace_callback(statements.append)
    try:
        out = rollup.build_rollup(store.db, 1, cfg={})
        assert out["measurement_coverage"]["complete"]
        # One pass establishes the original as-of boundary; one ordered
        # stream then supplies bounded per-message proofs on that snapshot.
        assert sum("SELECT message_id,content_hash,CASE WHEN typeof(body_html)" in sql
                   for sql in statements) == 2
        statements.clear()
        rollup._message_source_signature(store.db, 1, 1)
        assert sum("SELECT message_id,content_hash,CASE WHEN typeof(body_html)" in sql
                   for sql in statements) == 1
    finally:
        store.db.set_trace_callback(None)


@pytest.mark.parametrize("yield_before_error", [False, True])
def test_empty_body_stream_failure_closes_cursor_and_preserves_borrowed_transaction(store, yield_before_error):
    store.db.execute("UPDATE patients SET fetch_state='complete' WHERE project_id=1")
    error = sqlite3.OperationalError("synthetic stream interruption")
    error.sqlite_errorcode = sqlite3.SQLITE_INTERRUPT

    class InterruptedCursor:
        closed = False

        def __iter__(self):
            if yield_before_error:
                yield 1, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", ""
            raise error

        def close(self):
            self.closed = True

    cursor = InterruptedCursor()

    class InterruptedConnection:
        def execute(self, *_args):
            return cursor

    with pytest.raises(sqlite3.OperationalError) as caught:
        rollup._empty_body_proofs(InterruptedConnection(), 1, html_available=True)
    assert caught.value is error
    assert cursor.closed
    assert store.db.in_transaction
    store.db.rollback()


@pytest.mark.parametrize('cfg', [None, {'local_llm': {'model': 'synthetic-other-model'}}])
def test_patient_summary_with_unproven_model_does_not_show_cached_latest(store, cfg):
    _seed_out_of_order(store)
    rollup.rebuild(store, 1, cfg={})
    before = store.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0]
    _, text = notify_views.patient_summary_text(store.db, 1, cfg=cfg)
    assert '■ バイタル: 未確認' in text
    assert '36.4' not in text and '37.2' not in text
    assert '■ 未完了タスク' in text and '■ 背景・療養情報' in text
    assert store.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0] == before
