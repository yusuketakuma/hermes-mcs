"""Synthetic response witnesses preserve source order and failed-detector state."""
import json
import sqlite3

import pytest

import ledger
import mcs_signals
from ops_testkit import DAY, NOW, _msg
import signal_policy


DETECTORS = (
    "_pharmacist_request", "_rx_request_visibility", "_adherence_concern",
    "_discharge_notice", "_symptom_after_med",
)


def _mention(db, name, mid, *, body=None):
    body = body if body is not None else (
        "退院しました。" if name == "_discharge_notice"
        else "薬Aを開始。発熱あり。飲み忘れがあります。")
    _msg(db, mid, pid=mid, ts=NOW - 5 * DAY, chash=f"h{mid}", body=body)
    doc = {"requests": [{"to": "薬剤師" if name == "_pharmacist_request" else "医師",
                         "action": "薬の確認"}]}
    if name == "_adherence_concern":
        doc = {"meds": []}
    elif name == "_discharge_notice":
        doc = {"events": ["discharge"]}
    elif name == "_symptom_after_med":
        doc = {"meds": [{"name": "薬A", "action": "start"}],
               "symptoms": [{"text": "発熱", "status": "new", "negated": False}]}
    db.execute("INSERT INTO artifacts(kind,message_id,content,meta) "
               "VALUES('extract_llm',?,?,?)",
               (mid, json.dumps(doc), json.dumps({"hash": f"h{mid}"})))


def _registered(db, mid):
    db.execute("INSERT INTO requests(project_id,source_message_id,source_hash,title,"
               "status,revision,created_at,updated_at) "
               "VALUES(?,?,?,'synthetic','cancelled',1,?,?)",
               (mid, mid, f"h{mid}", NOW, NOW))


@pytest.fixture
def store(tmp_path):
    value = ledger.Ledger(str(tmp_path / "synthetic.db"))
    try:
        yield value
    finally:
        value.close()


def _mixed_sources(db, name):
    for index in range(32):
        mid = index + 1
        _mention(db, name, mid)
        case = index % 8
        if case == 1:
            _registered(db, mid)
        if case in (2, 3, 4, 5, 6, 7):
            reply = 1000 + mid
            _msg(db, reply, pid=mid if case != 7 else 1000 + mid,
                 ts=NOW - 5 * DAY if case == 4 else NOW - 4 * DAY,
                 chash=f"h{reply}",
                 prof="看護師" if case in (3, 6) else "薬剤師, 看護師",
                 org="synthetic-org, second" if case == 3 else "synthetic-org-suffix")
            if case == 5:
                db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=?",
                           (reply,))
    db.commit()


def _ids(found):
    return [row[1]["evidence"].get("discharge_message_id",
            row[1]["evidence"].get("message_ids", [None])[0]) for row in found]


@pytest.mark.parametrize("name", DETECTORS)
@pytest.mark.parametrize("configured", [False, True])
def test_response_witness_identity_and_order(store, name, configured):
    _mixed_sources(store.db, name)
    cfg = {"self_professions": ["薬剤師"] if configured else [],
           "self_organizations": ["synthetic-org"] if configured else []}
    found = list(getattr(mcs_signals, name)(
        store.db, NOW, signal_policy.resolved_thresholds(store.db), cfg))
    # An exact timestamp, deleted reply, similar organization and other room
    # remain eligible. A cancelled registration still counts as engagement.
    suppressed = (1, 2, 3) if configured else (1,)
    assert _ids(found) == [i + 1 for i in range(32) if i % 8 not in suppressed]


@pytest.mark.parametrize("name", DETECTORS)
@pytest.mark.parametrize("configured", [False, True])
def test_response_witness_queries_are_bounded(store, name, configured):
    _mixed_sources(store.db, name)
    cfg = {"self_professions": ["薬剤師"] if configured else [],
           "self_organizations": ["synthetic-org"] if configured else []}
    th = signal_policy.resolved_thresholds(store.db)
    queries = []
    store.db.set_trace_callback(lambda sql: queries.append(sql)
        if sql.lstrip().upper().startswith(("SELECT", "WITH")) else None)
    try:
        found = list(getattr(mcs_signals, name)(store.db, NOW, th, cfg))
    finally:
        store.db.set_trace_callback(None)
    suppressed = (1, 2, 3) if configured else (1,)
    assert _ids(found) == [i + 1 for i in range(32) if i % 8 not in suppressed]
    # Discharge keeps each original source-scope read; the other detectors
    # need only their original candidate queries plus batched witnesses.
    assert len(queries) <= (40 if name == "_discharge_notice" else 8)


@pytest.mark.parametrize("registered", [False, True])
def test_discharge_late_source_error_keeps_generator_order(store, registered):
    _mention(store.db, "_discharge_notice", 1)
    _mention(store.db, "_discharge_notice", 2, body=b"synthetic invalid text")
    if registered:
        _registered(store.db, 2)
    store.db.commit()
    found = mcs_signals._discharge_notice(
        store.db, NOW, signal_policy.resolved_thresholds(store.db),
        {"self_professions": [], "self_organizations": []})
    assert _ids([next(found)]) == [1]
    with pytest.raises(TypeError):
        next(found)


def test_discharge_source_error_does_not_resolve_stored_signal(store, monkeypatch):
    _mention(store.db, "_discharge_notice", 1, body=b"synthetic invalid text")
    _registered(store.db, 1)
    old = {"v": 1, "type": "discharge_notice", "project_id": 1, "state": "open",
           "detected_at": NOW, "evidence": {"discharge_message_id": 1}, "context": {},
           "note": "synthetic existing review candidate"}
    store.db.execute("INSERT INTO artifacts(kind,project_id,content,meta) VALUES(?,?,?,?)",
                     ("signal_v1", 1, json.dumps(old),
                      json.dumps({"key": "discharge_notice:1:1", "type": "discharge_notice"})))
    store.db.commit()
    monkeypatch.setattr(mcs_signals, "DETECTORS", (
        ("discharge_notice", mcs_signals._discharge_notice),))
    result = mcs_signals.evaluate(store, {"signals": {
        "self_professions": [], "self_organizations": []}}, now=NOW)
    assert result["errors"] == ["discharge_notice:TypeError"]
    assert result["detectors_ran"] == [] and result["resolved"] == 0
    assert store.db.execute("SELECT count(*) FROM artifacts WHERE kind='signal_v1'").fetchone()[0] == 1


@pytest.mark.parametrize("name", DETECTORS)
def test_suppressed_prefix_never_hides_a_later_eligible_source(store, name):
    for mid in range(1, 259):
        _mention(store.db, name, mid)
        if mid < 258:
            _registered(store.db, mid)
    store.db.commit()
    found = list(getattr(mcs_signals, name)(store.db, NOW,
        signal_policy.resolved_thresholds(store.db),
        {"self_professions": [], "self_organizations": []}))
    assert _ids(found) == [258]


def test_same_connection_write_between_yields_rechecks_engagement(store):
    for mid in (1, 2):
        _mention(store.db, "_pharmacist_request", mid)
    store.db.commit()
    found = mcs_signals._pharmacist_request(store.db, NOW,
        signal_policy.resolved_thresholds(store.db),
        {"self_professions": [], "self_organizations": []})
    assert _ids([next(found)]) == [1]
    _registered(store.db, 2)
    store.db.commit()
    with pytest.raises(StopIteration):
        next(found)


def test_bulk_read_failure_preserves_original_per_source_error_order(store):
    for mid in (1, 2):
        _mention(store.db, "_pharmacist_request", mid)
    store.db.commit()

    class FailedWitness:
        def __getattr__(self, name):
            return getattr(store.db, name)

        def execute(self, sql, parameters=()):
            if "FROM json_each(?) c" in sql:
                raise sqlite3.OperationalError("synthetic bulk read unavailable")
            if sql.startswith("SELECT 1 FROM requests") and parameters == (2,):
                raise sqlite3.OperationalError("synthetic source witness failure")
            return store.db.execute(sql, parameters)

    found = mcs_signals._pharmacist_request(FailedWitness(), NOW,
        signal_policy.resolved_thresholds(store.db),
        {"self_professions": [], "self_organizations": []})
    assert _ids([next(found)]) == [1]
    with pytest.raises(sqlite3.OperationalError, match="synthetic source witness failure"):
        next(found)
