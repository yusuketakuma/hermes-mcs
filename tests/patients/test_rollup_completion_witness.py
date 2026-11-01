"""A completion witness rechecks saved aggregation without replacing its evidence."""
from contextlib import closing
from datetime import datetime, timedelta
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

import extract_llm
from extract_testkit import _ledger, _message
import inference_dependency_loss
import inference_version
import rollup


@pytest.fixture
def saved(tmp_path, monkeypatch):
    cfg = {"local_llm": {"model": "synthetic-witness-model"}}
    now = [time.time() + 60]
    monkeypatch.setattr(rollup, "time", SimpleNamespace(
        time=lambda: now[0], monotonic=time.monotonic))
    monkeypatch.setattr(extract_llm, "resolved_model", lambda config: config["local_llm"]["model"])
    with closing(_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        with ledger.db:
            ledger.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
        ledger.save_messages([_message(
            body="本人の2026-10-01測定で体温36.4℃でした。",
            posted_at="2026-10-01T09:00:00+09:00")])
        row = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        row["_source_binding"] = inference_dependency_loss.source_binding(ledger.db, 1, 1)
        row["_target"] = ("", cfg["local_llm"]["model"])
        quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
                   "completeness_chunks": 1,
                   "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
        with ledger.db:
            assert extract_llm._replace_current_tx(
                ledger, row, '{"vitals":{"bt":36.4},"urgency":"routine"}',
                integrity=quality) == "written"
        aid = rollup.rebuild(ledger, 1, cfg=cfg)
        assert rollup.inference_coverage(ledger.db, 1)["current"]
        stored = json.loads(ledger.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
        assert stored["measurement_coverage"]["complete"]
        yield ledger, cfg, now, aid


def _rows(db):
    return [tuple(row) for row in db.execute(
        "SELECT artifact_id,content,meta,model,created_at FROM artifacts "
        "WHERE kind=? AND project_id=1 ORDER BY artifact_id", (rollup.KIND,))]


def _reject(result):
    assert result["current"] is False
    assert result["status"] in {"missing", "stale", "unverified", "config_unverified"}
    assert result[inference_version.VERSION_KEY] == inference_version.CURRENT_VERSION


def test_matched_saved_rollup_is_current_without_any_write(saved):
    ledger, cfg, _, _ = saved
    db = ledger.db
    before, changes = _rows(db), db.total_changes
    db.execute("SAVEPOINT witness_caller")
    db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in (
        sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK)
    try:
        proof = rollup.completion_coverage(db, 1, cfg)
        assert proof["current"] is True and proof["status"] == "current"
        assert proof[inference_version.VERSION_KEY] == inference_version.CURRENT_VERSION
        assert isinstance(proof["source_sig"], str) and len(proof["source_sig"]) == 64
        assert db.in_transaction and db.total_changes == changes
        db.execute("ROLLBACK TO witness_caller")
        db.execute("RELEASE witness_caller")
    finally:
        db.set_authorizer(None)
    assert _rows(db) == before


@pytest.mark.parametrize("cfg", [None, [], "unavailable"])
def test_missing_configuration_cannot_certify_a_current_stamp(saved, cfg):
    ledger, _, _, _ = saved
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_changed_model_cannot_reuse_an_identical_source_marker(saved):
    ledger, _, _, _ = saved
    assert rollup.inference_coverage(ledger.db, 1)["current"]
    before = _rows(ledger.db)
    _reject(rollup.completion_coverage(ledger.db, 1, {"local_llm": {"model": "other-synthetic-model"}}))
    assert _rows(ledger.db) == before


def test_content_tampering_cannot_borrow_valid_metadata(saved):
    ledger, cfg, _, aid = saved
    content = json.loads(_rows(ledger.db)[0][1])
    content["latest_vitals"] = {"bt": 99.0}
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(content), aid))
    assert rollup.inference_coverage(ledger.db, 1)["current"]
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_changed_source_keeps_the_existing_rollup_unverified(saved):
    ledger, cfg, _, _ = saved
    with ledger.db:
        ledger.db.execute("UPDATE messages SET profession='合成の別職種' WHERE message_id=1")
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


@pytest.mark.parametrize(("column", "value"), [("content", "{"), ("content", "[]"), ("meta", "{")])
def test_malformed_saved_evidence_is_not_repaired_by_a_read(saved, column, value):
    ledger, cfg, _, aid = saved
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET " + column + "=? WHERE artifact_id=?", (value, aid))
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_duplicate_current_rollups_are_not_chosen_arbitrarily(saved):
    ledger, cfg, _, aid = saved
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,project_id,message_id,content,model,meta,created_at FROM artifacts "
                          "WHERE artifact_id=?", (aid,))
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert len(_rows(ledger.db)) == 2 and _rows(ledger.db) == before
    assert ledger.db.total_changes == changes


def test_missing_rollup_is_not_created_by_a_completion_check(saved):
    ledger, cfg, _, _ = saved
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind=?", (rollup.KIND,))
    changes = ledger.db.total_changes
    proof = rollup.completion_coverage(ledger.db, 1, cfg)
    _reject(proof)
    assert proof["status"] == "missing"
    assert _rows(ledger.db) == [] and ledger.db.total_changes == changes


def test_only_generated_at_is_ignored_when_time_moves(saved):
    ledger, cfg, now, _ = saved
    before = _rows(ledger.db)
    now[0] += 30
    assert rollup.completion_coverage(ledger.db, 1, cfg)["current"]
    assert _rows(ledger.db) == before


def test_medication_period_boundary_is_not_hidden_by_timestamp_normalization(saved):
    ledger, cfg, now, _ = saved
    tomorrow = datetime.fromtimestamp(now[0], rollup.JST).date() + timedelta(days=1)
    end = tomorrow + timedelta(days=1)
    body = f"合成薬を{tomorrow.isoformat()}から{end.isoformat()}まで内服。"
    ledger.save_messages([_message(mid=2, body=body)])
    chash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=2").fetchone()[0]
    ledger.artifact_add("extract_v1", json.dumps({"med_periods": [{
        "raw": body, "start": tomorrow.isoformat(), "end": end.isoformat()}]}),
        project_id=1, message_id=2,
        meta={"hash": chash, inference_version.VERSION_KEY: inference_version.CURRENT_VERSION})
    rollup.rebuild(ledger, 1, cfg=cfg)
    assert rollup.completion_coverage(ledger.db, 1, cfg)["current"]
    before = _rows(ledger.db)
    now[0] = datetime.combine(tomorrow, datetime.min.time(), rollup.JST).timestamp()
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before


def test_rebuild_comparison_borrows_the_same_snapshot_as_the_marker(saved, tmp_path, monkeypatch):
    ledger, cfg, _, _ = saved
    real_build = rollup.build_rollup
    observed = []

    def changed_elsewhere(connection, pid, **kwargs):
        assert connection is ledger.db and connection.in_transaction
        original = connection.execute("SELECT profession FROM messages WHERE message_id=1").fetchone()[0]
        with closing(sqlite3.connect(tmp_path / "ledger.db")) as other:
            with other:
                other.execute("UPDATE messages SET profession='合成の並行訂正' WHERE message_id=1")
        observed.append(connection.execute("SELECT profession FROM messages WHERE message_id=1").fetchone()[0])
        assert observed[-1] == original
        return real_build(connection, pid, **kwargs)

    monkeypatch.setattr(rollup, "build_rollup", changed_elsewhere)
    before, changes = _rows(ledger.db), ledger.db.total_changes
    proof = rollup.completion_coverage(ledger.db, 1, cfg)
    assert observed and proof["current"] is False
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes
    assert ledger.db.execute("SELECT profession FROM messages WHERE message_id=1").fetchone()[0] == "合成の並行訂正"
    assert rollup.inference_coverage(ledger.db, 1)["current"] is False


def test_normal_dirty_scan_rebuilds_after_only_the_model_changes(saved):
    ledger, cfg, _, _ = saved
    first = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert first.dirty == () and not first.incomplete
    changed = {"local_llm": {"model": "new-synthetic-model"}}
    scan = rollup.scan_dirty_projects(ledger, cfg=changed)
    assert scan.dirty == (1,) and not scan.incomplete
    _reject(rollup.completion_coverage(ledger.db, 1, changed))
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=changed) == 1
    after = rollup.scan_dirty_projects(ledger, cfg=changed)
    assert after.dirty == () and not after.incomplete
    assert rollup.completion_coverage(ledger.db, 1, changed)["current"]


def test_spending_window_changes_preserve_clean_rollup_and_retry_state(saved):
    ledger, cfg, now, _ = saved
    with ledger.db:
        ledger.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,payload,state,attempts,next_try,created_at,updated_at) "
            "VALUES('semantic',1,1,?,'failed',3,?,?,?) "
            "ON CONFLICT(kind,project_id,message_id) DO UPDATE SET payload=excluded.payload,"
            "state=excluded.state,attempts=excluded.attempts,next_try=excluded.next_try",
            ('{"generation":"synthetic-retry-generation","attempt_limit":5}',
             now[0] + 900, now[0] - 10, now[0] - 5))
    before, changes = _rows(ledger.db), ledger.db.total_changes
    jobs = [tuple(row) for row in ledger.db.execute("SELECT * FROM fetch_jobs ORDER BY job_id")]
    changed = {**cfg, "semantic": {"until": "2099-01-01T00:00:00+09:00"},
               "cloud_llm": {"until": "2099-01-01T00:00:00+09:00",
                             "window_start": "2026-10-29T00:00:00+09:00",
                             "window_budget_usd": 3.0, "daily_cost_limit_usd": 5.0,
                             "monthly_cost_limit_usd": 20.0}}
    scan = rollup.scan_dirty_projects(ledger, cfg=changed)
    assert scan.dirty == () and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=changed) == 0
    assert rollup.completion_coverage(ledger.db, 1, changed)["current"]
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes
    assert [tuple(row) for row in ledger.db.execute("SELECT * FROM fetch_jobs ORDER BY job_id")] == jobs


def test_cache_built_without_configuration_is_dirty_for_an_explicit_configuration(saved):
    ledger, cfg, _, _ = saved
    rollup.rebuild(ledger, 1, cfg=None)
    assert rollup.scan_dirty_projects(ledger).dirty == ()
    scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
    assert rollup.scan_dirty_projects(ledger, cfg=cfg).dirty == ()
    assert rollup.completion_coverage(ledger.db, 1, cfg)["current"]


def test_changed_model_settles_even_without_a_current_extraction_to_change_the_content(saved):
    ledger, cfg, _, _ = saved
    with ledger.db:
        ledger.db.execute("DELETE FROM artifacts WHERE kind='extract_llm' AND project_id=1")
    rollup.rebuild(ledger, 1, cfg=cfg)
    before = json.loads(_rows(ledger.db)[0][1])
    before.pop("generated_at")
    assert before["measurement_coverage"]["complete"] is False
    assert rollup.scan_dirty_projects(ledger, cfg=cfg).dirty == ()
    changed = {"local_llm": {"model": "another-synthetic-model"}}
    scan = rollup.scan_dirty_projects(ledger, cfg=changed)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=changed) == 1
    after = json.loads(_rows(ledger.db)[0][1])
    after.pop("generated_at")
    assert after == before
    clean = rollup.scan_dirty_projects(ledger, cfg=changed)
    assert clean.dirty == () and not clean.incomplete
    assert rollup.completion_coverage(ledger.db, 1, changed)["current"]


@pytest.mark.parametrize("column", ["content", "meta"])
def test_duplicate_json_keys_cannot_borrow_otherwise_identical_evidence(saved, column):
    ledger, cfg, _, aid = saved
    raw = ledger.db.execute("SELECT " + column + " FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0]
    duplicate = ('{"' + inference_version.VERSION_KEY + '":'
                 + json.dumps(inference_version.CURRENT_VERSION) + ',' + raw[1:])
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET " + column + "=? WHERE artifact_id=?", (duplicate, aid))
    assert rollup.inference_coverage(ledger.db, 1)["current"]
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")], ids=["nan", "infinity", "negative-infinity"])
def test_nonfinite_json_numbers_are_not_accepted_as_saved_evidence(saved, value):
    ledger, cfg, _, aid = saved
    content = json.loads(_rows(ledger.db)[0][1])
    content["msg_count"] = value
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(content), aid))
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


@pytest.mark.parametrize("value", [True, 1.0], ids=["boolean-is-not-integer", "float-is-not-integer"])
def test_saved_json_number_types_must_match_the_recomputed_result(saved, value):
    ledger, cfg, _, aid = saved
    content = json.loads(_rows(ledger.db)[0][1])
    assert type(content["msg_count"]) is int and content["msg_count"] == 1
    content["msg_count"] = value
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(content), aid))
    assert rollup.inference_coverage(ledger.db, 1)["current"]
    before, changes = _rows(ledger.db), ledger.db.total_changes
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


@pytest.mark.parametrize("corruption", [
    "boolean_count", "float_count", "duplicate_content", "duplicate_meta",
    "content_generated_future", "content_generated_negative", "content_generated_boolean",
    "content_generated_missing", "meta_generated_future", "period_disagreement",
    "nan_count", "infinite_count", "negative_infinite_count",
])
def test_normal_dirty_route_repairs_invalid_saved_evidence(saved, corruption):
    ledger, cfg, now, aid = saved
    row = ledger.db.execute("SELECT content,meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
    content, meta = json.loads(row["content"]), json.loads(row["meta"])
    if corruption == "boolean_count":
        content["msg_count"] = True
    elif corruption == "float_count":
        content["msg_count"] = 1.0
    elif corruption == "content_generated_future":
        content["generated_at"] = now[0] + 3600
    elif corruption == "content_generated_negative":
        content["generated_at"] = -1
    elif corruption == "content_generated_boolean":
        content["generated_at"] = True
    elif corruption == "content_generated_missing":
        del content["generated_at"]
    elif corruption == "meta_generated_future":
        meta["generated_at"] = now[0] + 3600
    elif corruption == "period_disagreement":
        assert content.get("_next_med_period_check") is None
        meta["next_med_period_check"] = now[0] + 900
    elif corruption.endswith("_count"):
        content["msg_count"] = {"nan_count": float("nan"), "infinite_count": float("inf"),
                                "negative_infinite_count": -float("inf")}[corruption]
    raw_content, raw_meta = json.dumps(content), json.dumps(meta)
    prefix = '{"' + inference_version.VERSION_KEY + '":' + json.dumps(inference_version.CURRENT_VERSION) + ','
    if corruption == "duplicate_content":
        raw_content = prefix + raw_content[1:]
    elif corruption == "duplicate_meta":
        raw_meta = prefix + raw_meta[1:]
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?", (raw_content, raw_meta, aid))

    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
    clean = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert clean.dirty == () and not clean.incomplete
    proof = rollup.completion_coverage(ledger.db, 1, cfg)
    assert proof["current"] is True and proof["status"] == "current"
    repaired = json.loads(_rows(ledger.db)[0][1])
    assert type(repaired["msg_count"]) is int and repaired["msg_count"] == 1
    assert type(repaired["generated_at"]) in (int, float) and 0 <= repaired["generated_at"] <= now[0]


@pytest.mark.parametrize("scope", ["unacquired_empty", "acquired_empty", "karte_only"])
def test_normal_dirty_route_reaches_patients_without_messages(tmp_path, monkeypatch, scope):
    cfg = {"local_llm": {"model": "synthetic-empty-model"}}
    now = time.time() + 60
    monkeypatch.setattr(rollup, "time", SimpleNamespace(time=lambda: now, monotonic=time.monotonic))
    monkeypatch.setattr(extract_llm, "resolved_model", lambda config: config["local_llm"]["model"])
    with closing(_ledger(tmp_path)) as ledger:
        ledger.ensure_patient(1)
        if scope == "acquired_empty":
            with ledger.db:
                ledger.db.execute("UPDATE patients SET fetch_state='complete',history_floor=-1 WHERE project_id=1")
        elif scope == "karte_only":
            ledger.karte_summary_store(1, 10, {"comment": "生活環境：完全合成の記録。"})
        assert ledger.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        _reject(rollup.completion_coverage(ledger.db, 1, cfg))
        scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
        assert scan.dirty == (1,) and not scan.incomplete
        assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
        clean = rollup.scan_dirty_projects(ledger, cfg=cfg)
        assert clean.dirty == () and not clean.incomplete
        assert rollup.completion_coverage(ledger.db, 1, cfg)["current"] is True
        content = json.loads(_rows(ledger.db)[0][1])
        assert content["msg_count"] == 0
        assert content["measurement_coverage"]["complete"] is False
        assert (content["karte_summary"] is not None) is (scope == "karte_only")


def _future_reply_source(saved):
    ledger, cfg, now, _ = saved
    tomorrow = datetime.fromtimestamp(now[0], rollup.JST) + timedelta(days=1)
    now[0] = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    future = now[0] + 60
    ledger.save_messages([_message(mid=2, parent_id=1, body="完全合成の後続返信。",
        posted_at=datetime.fromtimestamp(future, rollup.JST).isoformat())])
    rollup.rebuild(ledger, 1, cfg=cfg)
    return future


def test_completion_deadline_stops_at_a_future_reply_before_midnight(saved):
    future = _future_reply_source(saved)
    ledger, cfg, _, _ = saved
    before, changes = _rows(ledger.db), ledger.db.total_changes
    proof = rollup.completion_coverage(ledger.db, 1, cfg)
    assert proof["current"] is True
    assert proof["valid_until"] == future
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_crossing_a_future_reply_while_building_refuses_unchanged_content(saved, monkeypatch):
    future = _future_reply_source(saved)
    ledger, cfg, now, _ = saved
    before, changes = _rows(ledger.db), ledger.db.total_changes
    expected = json.loads(before[0][1])
    expected.pop("generated_at")
    real_build = rollup.build_rollup

    def crosses_boundary(connection, pid, **kwargs):
        result = real_build(connection, pid, **kwargs)
        actual = json.loads(json.dumps(result))
        actual.pop("generated_at")
        assert actual == expected
        now[0] = future + 1
        return result

    monkeypatch.setattr(rollup, "build_rollup", crosses_boundary)
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_missing_newest_observation_caps_deadline_at_an_older_observations_staleness(saved):
    ledger, cfg, now, _ = saved
    tomorrow = datetime.fromtimestamp(now[0], rollup.JST) + timedelta(days=1)
    now[0] = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    ledger.save_messages([_message(mid=2, body="完全合成の最新投稿。",
        posted_at=datetime.fromtimestamp(now[0], rollup.JST).isoformat())])
    crossing = now[0] + 120
    with ledger.db:
        ledger.db.execute("UPDATE messages SET updated_seen=NULL WHERE message_id=2")
        ledger.db.execute("UPDATE messages SET updated_seen=? WHERE message_id=1",
                          (crossing - rollup.STALE_DAYS * 86400,))
    rollup.rebuild(ledger, 1, cfg=cfg)
    before, changes = _rows(ledger.db), ledger.db.total_changes
    proof = rollup.completion_coverage(ledger.db, 1, cfg)
    assert proof["current"] is True and proof["valid_until"] == crossing
    now[0] = crossing + 1
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    assert _rows(ledger.db) == before and ledger.db.total_changes == changes


def test_noop_rebuild_repairs_meta_older_than_content(saved):
    ledger, cfg, now, aid = saved
    row = _rows(ledger.db)[0]
    content, meta = json.loads(row[1]), json.loads(row[2])
    meta['generated_at'] = content['generated_at'] - 1
    assert rollup._newest_source_ts(ledger.db, 1) <= meta['generated_at']
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
    repaired = _rows(ledger.db)[0]
    assert repaired[0] == aid and repaired[1] == row[1]
    assert json.loads(repaired[2])['generated_at'] == now[0]
    assert rollup.scan_dirty_projects(ledger, cfg=cfg).dirty == ()
    assert rollup.completion_coverage(ledger.db, 1, cfg)['current']


def test_future_reply_expiry_updates_marker_on_noop_rebuild(saved):
    future = _future_reply_source(saved)
    ledger, cfg, now, _ = saved
    before = _rows(ledger.db)[0]
    assert json.loads(before[2])['valid_until'] == future
    now[0] = future + 1
    scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
    after = _rows(ledger.db)[0]
    assert after[0] == before[0] and after[1] == before[1]
    assert json.loads(after[2])['valid_until'] > now[0]
    assert rollup.scan_dirty_projects(ledger, cfg=cfg).dirty == ()
    assert rollup.completion_coverage(ledger.db, 1, cfg)['current']


def test_future_reply_rejects_old_proof_then_normal_scan_repairs_it(saved):
    from test_rollup_canonical import _DONE, _DONE_BODY, _REQ, _REQUEST_BODY, _seed_relations, _thread

    ledger, cfg, now, _ = saved
    tomorrow = datetime.fromtimestamp(now[0], rollup.JST) + timedelta(days=1)
    now[0] = tomorrow.replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    future = now[0] + 60
    request_time = datetime.fromtimestamp(now[0] - 60, rollup.JST).isoformat()
    reply_time = datetime.fromtimestamp(future, rollup.JST).isoformat()
    _thread(ledger, [(1, None, 'SYNTH-A', request_time, _REQ),
                     (2, 1, 'SYNTH-B', reply_time, {})],
            bodies={1: _REQUEST_BODY, 2: _DONE_BODY})
    _seed_relations(ledger, 1, '処方変更を確認', [_DONE], local_model=cfg['local_llm']['model'])
    rollup.rebuild(ledger, 1, cfg=cfg)
    before = json.loads(_rows(ledger.db)[0][1])
    assert before['recent_requests'][0].get('reply_state') is None
    assert rollup.completion_coverage(ledger.db, 1, cfg)['current']
    now[0] = future + 1
    _reject(rollup.completion_coverage(ledger.db, 1, cfg))
    scan = rollup.scan_dirty_projects(ledger, cfg=cfg)
    assert scan.dirty == (1,) and not scan.incomplete
    assert rollup.rebuild_many(ledger, scan.dirty, cfg=cfg) == 1
    after = json.loads(_rows(ledger.db)[0][1])
    assert after['recent_requests'][0]['reply_state'] == 'done'
    assert rollup.scan_dirty_projects(ledger, cfg=cfg).dirty == ()
    assert rollup.completion_coverage(ledger.db, 1, cfg)['current']
