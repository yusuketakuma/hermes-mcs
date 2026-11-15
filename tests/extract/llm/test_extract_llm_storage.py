"""Synthetic extract llm storage contracts with isolated shared fixtures."""
import extract_llm_carry
import extract_llm_context
import extract_llm_engine
import extract_llm_errors
import extract_llm_prepare
import extract_llm_provider
import extract_llm_qc
import extract_llm_resident
import extract_llm_runner
import extract_llm_selection
import extract_llm_source_contract
import extract_llm_store
import extract_validation
import json
import time
import pytest
import extract_llm
import cloud_inference
import inference_dependency_loss as dependency_loss
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_projection import PROJECTION_VERSION
from extract_testkit import _hash, _ledger, _message
import mcs_queries
from extract_llm_testkit import _run as _run, _seed_human_report as _seed_human_report, _v1_artifact as _v1_artifact


@pytest.mark.parametrize("kind_field", [{}, {"kind": None}, {"kind": "unknown"}])
@pytest.mark.parametrize("body,verified", [
    ("医師へ連絡をお願いします。", True),
    ("医師へ連絡できますか。", True),
    ("医師へ連絡しました。", False),
    ("医師へ連絡します。", False),
    ("医師へ連絡をお願いしたい、という依頼ではありません。", False),
    ("医師へ連絡をお願いします。その依頼を取り消しました。", False),
])
def test_descriptive_request_kind_needs_only_explicit_source_request_proof(kind_field, body, verified):
    quote = body.split("。")[0] + "。"
    item, = extract_validation.validate({"requests": [{"to": "医師", "action": "医師へ連絡",
        "evidence": quote, **kind_field}]}, body)["requests"]
    assert "kind" not in item and item["evidence"] == quote
    assert (item.get("unverified") is not True) is verified


def test_shard_selects_disjoint_partition(tmp_path, monkeypatch):
    """--shard I/N partitions the pending set by message_id so two
    concurrent drainers never re-process each other's rows."""
    db = _ledger(tmp_path)
    for mid in (1, 2, 3, 4):
        db.save_messages([_message(mid=mid, body=f"msg {mid}")])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "ok"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)

    seen = set()
    for shard in ((0, 2), (1, 2)):
        before = db.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm'"
        ).fetchone()[0]
        res = extract_llm.run_pending(db, limit=10, budget_s=30,
                                      shard=shard)
        assert res["done"] == 2
        rows = db.db.execute(
            "SELECT message_id FROM artifacts WHERE kind='extract_llm'"
        ).fetchall()
        new = {r[0] for r in rows} - seen
        assert all(mid % 2 == shard[0] for mid in new)
        seen |= {r[0] for r in rows}
        assert len(rows) - before == 2
    assert seen == {1, 2, 3, 4}
    db.close()


def test_legacy_row_is_retained_beside_atomic_current_replacement(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="プレドニン中止の連絡")])
    _v1_artifact(db, 1, {"meds": [{"name": "プレドニン",
                                   "action": "stop"}]}, _hash(db))

    res = _run(db, monkeypatch,
               {"meds": [{"name": "プレドニン", "action": "stop",
                          "status": "past"}]})

    assert res["done"] == 1 and res["left"] == 0
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 2 and VERSION_KEY not in json.loads(rows[0]["meta"])
    meta = json.loads(rows[-1]["meta"])
    assert meta["extract_version"] == extract_llm.EXTRACT_VERSION
    assert meta[VERSION_KEY] == CURRENT_VERSION
    assert json.loads(rows[-1]["content"])["meds"][0]["status"] == "past"
    db.close()


def test_legacy_row_stays_history_and_is_not_a_current_reader_fallback(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="現行本文")])
    _v1_artifact(db, 1, {"summary": "v1"}, _hash(db))

    assert mcs_queries.current_message_extract(db.db, "extract_llm", 1) is None
    assert json.loads(db.artifacts("extract_llm", message_id=1)[0]["content"])["summary"] == "v1"
    # but the write side treats it as pending
    assert not extract_llm._current(db, 1, _hash(db))
    db.close()


def test_v1_permanent_error_does_not_block_v2(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="リトライ対象")])
    # a v1 error row: no extract_version -> not joined as v2 attempts
    db.artifact_add("extract_llm", '{"_error":true}', project_id=1,
                    message_id=1,
                    meta={"hash": _hash(db), "error": True, "attempts": 5,
                          "next_try": 0})

    res = _run(db, monkeypatch, {"summary": "ok"})

    assert res["done"] == 1
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 2 and json.loads(rows[0]["meta"])["error"] is True
    assert json.loads(rows[-1]["meta"])[VERSION_KEY] == CURRENT_VERSION
    db.close()


def test_fail_stamps_extract_version(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    row = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 0)
    meta = json.loads(db.artifacts("extract_llm", message_id=1)[0]["meta"])
    assert meta["extract_version"] == extract_llm.EXTRACT_VERSION
    assert type(meta["extract_version"]) is int
    db.close()


def test_v2_error_row_respects_backoff(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    row = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 0)  # next_try in the future
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: pytest.fail("backoff must suppress retry"))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 0
    db.close()


@pytest.mark.parametrize("skew", [-3 * 86400, 30 * 86400])
def test_backoff_reads_python_clock_not_sqlite(tmp_path, monkeypatch, skew):
    """next_try is written from time.time(); the pending scan must compare
    it against the same clock even when SQLite's 'now' disagrees."""
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + skew)
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="backoff row"),
                      _message(mid=2, body="fresh row")])
    row = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 0)  # next_try = python now + 300
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(body) or {"summary": "ok"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    for _ in range(2):  # a repeated drain must not leak the backoff row
        extract_llm.run_pending(db, limit=10, budget_s=30)
    assert calls == ["fresh row"]
    # once the Python clock passes the backoff, the row retries
    monkeypatch.setattr(time, "time", lambda: real() + skew + 7200)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1 and calls == ["fresh row", "backoff row"]
    db.close()


def test_replace_current_keeps_invalid_history_without_poisoning_reprocessing(tmp_path, monkeypatch):
    """Invalid history is retained and cannot veto evaluation of the current source."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    with db.db:
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
            "VALUES('extract_llm',1,1,'{}','{broken')")
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: calls.append(body) or {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1 and calls == ["body"]
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 2 and sum(row["meta"] == "{broken" for row in rows) == 1
    db.close()


def test_replace_current_rerenders_card_once_written(tmp_path, monkeypatch):
    """A newly current extraction re-renders its thread card at once; a
    refused write does nothing and a render failure never undoes it."""
    import notify_cards
    monkeypatch.setattr(extract_llm, "load_config", lambda: {"notify": {"interactive": "slack"}})
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: {"notify": {"interactive": "slack"}})
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(db.db,row['project_id'],row['message_id'])
    pid = row['project_id']
    calls = []
    monkeypatch.setattr(notify_cards, "rerender_message_cards",
                        lambda ledger, cfg, p, mid: calls.append((p, mid)))
    assert extract_llm._replace_current(
        db, {**row, "content_hash": "stale"}, "{}") is False
    assert calls == []
    assert extract_llm._replace_current(db, row, '{"summary":"合成"}') is True
    assert calls == [(pid, 1)]

    def boom(*_a):
        raise RuntimeError("render down")
    monkeypatch.setattr(notify_cards, "rerender_message_cards", boom)
    assert extract_llm._replace_current(db, row, '{"summary":"合成2"}') is True
    assert [a["content"] for a in db.artifacts("extract_llm", message_id=1)] \
        == ['{"summary":"合成2"}']
    db.close()


def test_v1_success_plus_v2_error_backoff_suppresses(tmp_path, monkeypatch):
    """Current backoff suppresses retries while legacy success remains history only."""
    db = _ledger(tmp_path)
    db.save_messages([_message(body="対象本文")])
    _v1_artifact(db, 1, {"summary": "v1"}, _hash(db))
    row = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 0)  # v2 error, next_try in future
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: pytest.fail("backoff must suppress"))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 0 and res["selected"] == 0
    assert mcs_queries.current_message_extract(db.db, "extract_llm", 1) is None
    assert any(json.loads(row["content"]).get("summary") == "v1" for row in db.artifacts("extract_llm", message_id=1))
    db.close()


# ---------- F13/F14 regression ----------

def test_extract_claim_is_exclusive_and_releases(tmp_path):
    """F14: only one worker can hold a row's lease; release frees it."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    row = dict(db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(db.db,row['project_id'],row['message_id'])
    lease = extract_llm._claim(db, row)
    assert lease is not None
    assert extract_llm._claim(db, row) is None
    extract_llm._release(db, row, lease)
    assert extract_llm._claim(db, row) is not None
    db.close()


def test_extract_claim_expired_lease_is_stealable(tmp_path):
    """A crashed worker's claim self-expires — no permanent stall."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    row = dict(db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(db.db,row['project_id'],row['message_id'])
    extract_llm._claim(db, row)
    db.db.execute(
        "UPDATE fetch_jobs SET next_try=? WHERE kind='extract_claim'",
        (time.time() - 1,))
    db.db.commit()
    assert extract_llm._claim(db, row) is not None
    db.close()


def test_pending_selection_is_bounded(tmp_path, monkeypatch):
    """F14: the pending query returns at most `limit` rows — the full
    backlog is no longer materialized per run."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=i, body=f"msg{i}")
                      for i in range(1, 30)])
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: {"summary": "x"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    r = extract_llm.run_pending(db, limit=7, budget_s=60)
    assert r["selected"] == 7 and r["done"] == 7
    db.close()


def test_tampered_final_completeness_claim_cannot_promote_progress(tmp_path, monkeypatch):
    """Editing only the final artifact meta — minting a resolved completeness
    claim over an unresolved leaf — must not promote progress to complete:
    the reader binds the claim to the replayed leaf record."""
    body = "本人に発熱と咳があります。本人の血圧120/80です。"
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, body=body)])
    doc = {"symptoms": [
        {"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱"},
        {"text": "咳", "status": "ongoing", "subject": "patient", "evidence": "咳"}]}
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda p, **kw: json.loads(json.dumps(doc)))
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    res = extract_llm.run_pending(db, budget_s=300)
    assert res["done"] == 1
    art = db.artifacts("extract_llm", message_id=1)[0]
    meta = json.loads(art["meta"])
    # the honest claim records the vitals gap as unresolved
    assert meta["integrity"]["completeness"]["0"]["state"] == "unresolved"
    meta["integrity"]["completeness"]["0"] = {
        "state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                  (json.dumps(meta, ensure_ascii=False), art["artifact_id"]))
    source = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    assert extract_llm.progress_for_message(
        db.db, source, model=extract_llm.MODEL)["state"] != "complete"
    db.close()


# ---------- carried over from test_mcs_ingestion ----------

def test_llm_validation_rejects_string_boolean_and_bool_vital():
    assert extract_validation.validate({"symptoms": [
        {"text": "pain", "negated": "false"}
    ]}) is None
    assert extract_validation.validate({"vitals": {"hr": True}}) is None


def test_llm_error_retry_resets_after_body_change(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="A")])
    row = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 4)
    db.save_messages([_message(body="B")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)

    result = extract_llm.run_pending(db, limit=1, budget_s=5)

    assert result["done"] == 1
    db.close()


def test_stale_error_row_does_not_gate_fresh_body(tmp_path, monkeypatch):
    """With the sweep gated off (per-process cadence), a stale error
    artifact at the attempts ceiling must not block the edited body —
    selection and fold-in both require the artifact's hash to match."""
    db = _ledger(tmp_path)
    db.save_messages([_message(body="A")])
    row = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row, 4)
    db.save_messages([_message(body="B")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_stale_gc_at", time.time())
    monkeypatch.setattr(extract_llm_selection, '_stale_gc_at', extract_llm._stale_gc_at)

    result = extract_llm.run_pending(db, limit=1, budget_s=5)

    assert result["done"] == 1
    # and a fresh-body failure folds in only same-hash prior attempts —
    # the lingering stale row must not roll it back to the ceiling
    row_b = db.db.execute(
        "SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._fail(db, row_b, 0)
    meta = json.loads(db.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm' "
        "AND json_extract(meta,'$.error')=1").fetchone()[0])
    assert meta["attempts"] == 1
    db.close()


def test_malformed_old_metadata_is_preserved_without_poisoning_current_retry(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="完全合成の記録です")])
    with db.db:
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
            "VALUES('extract_llm',1,1,'{}','{broken')")
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: calls.append(body) or {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)

    result = extract_llm.run_pending(db, limit=1, budget_s=5)

    assert calls == ["完全合成の記録です"]
    assert result["done"] == 1
    assert db.db.execute("SELECT 1 FROM messages m WHERE " + extract_llm.pending_pred(db.db)).fetchone() is None
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 2 and sum(row["meta"] == "{broken" for row in rows) == 1
    current = [row for row in rows if row["meta"] != "{broken"]
    assert len(current) == 1 and json.loads(current[0]["meta"])[VERSION_KEY] == CURRENT_VERSION
    db.close()


def test_snippet_message_is_not_extracted(tmp_path, monkeypatch):
    """F05: extraction consumes only complete bodies — a 'snippet'
    row stays pending instead of producing partial-input facts."""
    db = _ledger(tmp_path)
    db.save_messages([_message(mid=1, body="partial...", state="snippet")])
    monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: {})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    result = extract_llm.run_pending(db, limit=10, budget_s=5)
    assert result["done"] == 0
    db.close()


def test_v3_pass_folds_v1v2_inline(tmp_path, monkeypatch):
    """The v3 drain performs the v1/v2 rule work simultaneously: a
    claimed row mints its extract_v1 artifact inside the pass AND its
    rule parse rides the LLM call as `hints` — one pass, both lanes."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(
        mid=1,
        body="体温36.5度、SpO2 97%。カロナール500mgを9/1-9/21の期間服用。"
             "次回9/25訪問予定。確認お願いします")])
    seen = {}

    def fake(body, **kw):
        seen.update(kw)
        return {"summary": "s"}

    monkeypatch.setattr(extract_llm, "llm_extract", fake)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    out = extract_llm.run_pending(db, limit=5, budget_s=10)
    assert out["done"] == 1
    v1 = db.artifacts("extract_v1", message_id=1)
    assert v1 and "カロナール" in v1[0]["content"]
    import extract
    body = db.db.execute(
        "SELECT body_text FROM messages WHERE message_id=1").fetchone()[0]
    assert seen["hints"] == extract.extract_message(
        body, "2026-09-19T00:00:00+09:00")
    db.close()


@pytest.mark.parametrize("bad", ["meta_scalar", "content_scalar", "foreign_patient"])
def test_invalid_chunk_checkpoint_is_reinferred(tmp_path, monkeypatch, bad):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成薬を確認")])
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(db.db,row['project_id'],row['message_id'])
    assert extract_llm._persist_chunks(db, row, {0: {"summary": "旧要約"}})
    if bad == "meta_scalar":
        db.db.execute("UPDATE artifacts SET meta='[]' WHERE kind='extract_llm_chunk'")
    elif bad == "content_scalar":
        db.db.execute("UPDATE artifacts SET content='7' WHERE kind='extract_llm_chunk'")
    else:
        # Plant a malformed legacy checkpoint without disabling other guards.
        db.db.execute("DROP TRIGGER g1_artifacts_msg_upd")
        db.db.execute("UPDATE artifacts SET project_id=2 WHERE kind='extract_llm_chunk'")
    db.db.commit()
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call",
                        lambda *a, **kw: calls.append(1) or {"symptoms": [], "summary": "再抽出"})
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    result = extract_llm.run_pending(db, budget_s=180)
    assert result["done"] == 1 and calls == [1]
    assert json.loads(db.artifacts("extract_llm")[0]["content"])["summary"] == "再抽出"
    db.close()


def test_delayed_rule_write_preserves_new_source_artifact(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="旧本文")])
    stale = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    stale['_source_binding'] = dependency_loss.source_binding(db.db,stale['project_id'],stale['message_id'])
    db.save_messages([_message(body="改訂本文")])
    current = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    current['_source_binding'] = dependency_loss.source_binding(db.db,current['project_id'],current['message_id'])
    assert stale['_source_binding'] != current['_source_binding']
    extract_llm._ensure_v1(db, current, {"v": 1, "summary": "改訂"})
    extract_llm._ensure_v1(db, stale, {"v": 1, "summary": "旧版"})
    rows = db.artifacts("extract_v1")
    assert len(rows) == 1
    assert json.loads(rows[0]["content"])["summary"] == "改訂"
    db.close()


def test_v4_publication_between_prepare_and_insert_fences_legacy(tmp_path):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="合成本文")])
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    row['_source_binding'] = dependency_loss.source_binding(db.db,row['project_id'],row['message_id'])
    other = _ledger(tmp_path)
    published = []

    def publish_before_insert(sql):
        # The prior comparison now owns a write lock; race before that boundary.
        if not published and sql == "BEGIN IMMEDIATE":
            other.artifact_add("semantic_facts_v4", '{"meds":[]}',
                               project_id=1, message_id=1,
                               meta={"hash": row["content_hash"], "engine_version": 4, VERSION_KEY: CURRENT_VERSION,
                                     "projection_version": PROJECTION_VERSION})
            published.append(True)

    db.db.set_trace_callback(publish_before_insert)
    try:
        assert extract_llm._replace_current(db, row, '{"summary":"旧経路"}') is False
        assert published == [True] and db.artifacts("extract_llm") == []
    finally:
        db.db.set_trace_callback(None)
        other.close()
        db.close()


@pytest.mark.parametrize("second", [None, {"semantic": {"fact_source": "canonical"}}])
def test_resident_reloads_and_holds_invalid_admission_config(monkeypatch, second):
    now = [0.0]
    config = [{}]
    admitted = []
    monkeypatch.setattr(extract_llm, "load_config", lambda: config[0])
    monkeypatch.setattr(extract_llm_context, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_prepare, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_provider, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_qc, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_resident, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_runner, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_selection, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(extract_llm_store, 'load_config', extract_llm.load_config)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: config[0])
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(extract_llm.time, "sleep",
                        lambda seconds: now.__setitem__(0, now[0] + seconds))
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--stop-after", "2"])
    # Writer lifetime guards are exercised separately with real locks.
    monkeypatch.setattr(extract_llm, "_open_current_ledger",
                        lambda **_: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm_resident, '_open_current_ledger', extract_llm._open_current_ledger)
    monkeypatch.setattr(extract_llm, "_writer_current", lambda **kw: True)
    monkeypatch.setattr(extract_llm_carry, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_errors, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_resident, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_runner, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_selection, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm_source_contract, '_writer_current', extract_llm._writer_current)
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm.cloud_backlog.CloudBacklogPool, "sync", lambda *a, **kw: None)

    def run(*args, **kwargs):
        admitted.append(kwargs["admitted_ids"])
        config[0] = second
        return {"done": 1, "failed": 0, "left": 1, "selected": 1}

    monkeypatch.setattr(extract_llm, "run_pending", run)
    monkeypatch.setattr(extract_llm_resident, 'run_pending', extract_llm.run_pending)
    assert extract_llm.main() == 0
    assert admitted == [None, set()]


def test_request_free_text_fields_are_length_capped():
    """U05-F08 hardening: ungrounded request text is bounded."""
    d = extract_validation.validate({"requests": [
        {"to": "医" * 100, "from": "看" * 100, "action": "合" * 500}]})
    rq = d["requests"][0]
    assert len(rq["to"]) == 30 and "from" not in rq
    assert rq["unverified"] is True
    # the prompt now asks for 30字 (#20 order 1) but the stored cap
    # stays 60 so existing expected actions keep substring-matching
    assert len(rq["action"]) == 60
    short = extract_validation.validate({"requests": [
        {"to": "医師", "action": "合成確認"}]})["requests"][0]
    assert short["to"] == "医師" and short["action"] == "合成確認"


@pytest.mark.parametrize(("cfg", "expect"), [
    ({}, None),
    ({"semantic": {"fact_source": "canonical",
                   "fact_source_gate": "g6-v1:synthetic"}}, set()),
    # canonical without its gate errors back to legacy: fail CLOSED
    ({"semantic": {"fact_source": "canonical"}}, set()),
    ({"semantic": {"fact_source": "bogus"}}, set()),
    (None, set()),
    ("not a dict", set()),
])
def test_legacy_admissions_single_t18_boundary(monkeypatch, cfg, expect):
    import semantic_conversion
    monkeypatch.setattr(semantic_conversion, "active_legacy_admissions",
                        lambda ledger: {7})
    assert extract_llm.legacy_admissions(object(), cfg) == expect


def test_legacy_admissions_accepts_independently_bound_evidence(monkeypatch):
    import semantic_conversion
    from candidate_testkit import approved_config
    cfg = approved_config({"semantic": {"fact_source": "canonical"}})
    monkeypatch.setattr(semantic_conversion, "active_legacy_admissions", lambda ledger: {7})
    assert extract_llm.legacy_admissions(object(), cfg) == {7}


def test_legacy_admissions_manifest_error_fails_closed(monkeypatch):
    import semantic_conversion
    from candidate_testkit import approved_config
    called = []

    def boom(ledger):
        called.append(True)
        raise RuntimeError("manifest")
    monkeypatch.setattr(semantic_conversion, "active_legacy_admissions", boom)
    cfg = approved_config({"semantic": {"fact_source": "canonical"}})
    assert extract_llm.legacy_admissions(object(), cfg) == set()
    assert called == [True]


def test_llm_call_stats_dedups_shared_meta_and_ignores_junk():
    shared = {"calls": 1, "timings": {"prompt_ms": 10, "predicted_ms": 5.5},
              "usage": {"total_tokens": 40}}
    stats = extract_llm._llm_call_stats(
        [shared, shared, None, {"calls": True, "usage": {"total_tokens": "9"}},
         {"calls": 2}])
    unreported = {
        "first_calls": None, "repair_calls": None, "repair_wins": None,
        "length_stops": None, "length_resplits": None, "probe_runs": None,
        "format_downgrades": None, "checkpoint_reuses": None,
        "first_pass_evaluated_chunks": None, "first_pass_accepted_chunks": None,
        "prompt_n": None, "cache_n": None, "repair_prompt_n": None,
        "repair_cache_n": None, "repair_issues": None, "cache_hit_ratio": None}
    assert stats == {"calls": 3, "prompt_ms": 10, "predicted_ms": 5.5,
                     "tokens": 40, **unreported}
    assert extract_llm._llm_call_stats([{"calls": 2}]) == {
        "calls": 2, "prompt_ms": None, "predicted_ms": None, "tokens": None,
        **unreported}
    assert extract_llm._llm_call_stats([None, {}]) is None


def test_human_report_re_extracts_exactly_once(tmp_path, monkeypatch):
    """A ⚠ report re-pends the message once: the new result replaces
    the reported artifact (meta.human_fix) and the next run selects
    nothing — no LLM call every cycle."""
    db = _ledger(tmp_path)
    aid, fid = _seed_human_report(db)
    calls = []
    monkeypatch.setattr(
        extract_llm, "llm_extract",
        lambda body, **_: calls.append(1) or {
            "summary": "新", "symptoms": [{"text": "咳"}],
            "vitals": {"bt": 37.5}})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res["done"] == 1 and len(calls) == 1
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 1 and rows[0]["artifact_id"] != aid
    assert json.loads(rows[0]["content"])["summary"] == "新"
    assert json.loads(rows[0]["meta"])["human_fix"] == {
        "feedback_id": fid, "applied": True}
    res2 = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res2["selected"] == 0 and len(calls) == 1
    db.close()


def test_human_report_failure_settles(tmp_path, monkeypatch):
    """A failed report re-extract re-mints the reported content with
    human_fix.applied=false so the row leaves pending."""
    db = _ledger(tmp_path)
    aid, fid = _seed_human_report(db)
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(1) or None)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **_: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    extract_llm.run_pending(db, limit=10, budget_s=30)
    rows = db.artifacts("extract_llm", message_id=1)
    assert len(rows) == 1 and rows[0]["artifact_id"] != aid
    assert json.loads(rows[0]["content"])["summary"] == "旧"
    assert json.loads(rows[0]["meta"])["human_fix"] == {
        "feedback_id": fid, "applied": False}
    res2 = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res2["selected"] == 0 and len(calls) == 1
    db.close()


def test_human_report_thin_result_is_not_thin_retried(tmp_path,
                                                     monkeypatch):
    """The report re-extract is the one extraction for that report — a
    thin outcome is not re-pended by the thin retry."""
    db = _ledger(tmp_path)
    _seed_human_report(db, body="合成の長い本文です。" * 30)
    calls = []
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **_: calls.append(1)
                        or {"urgency": "routine"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    extract_llm.run_pending(db, limit=10, budget_s=30)
    res2 = extract_llm.run_pending(db, limit=10, budget_s=30)
    assert res2["selected"] == 0 and len(calls) == 1
    db.close()


def test_periodic_revive_reopens_exhausted_extract_errors(tmp_path):
    """An error row at the 5-attempt ceiling gets one more attempt after
    the cooldown (attempts back to 4), capped per body hash; a refail keeps the
    count so the cap holds; a new body starts fresh."""
    from ledger import Ledger
    db = Ledger(str(tmp_path / "revive.db"))
    try:
        db.save_messages([_message(7)])
        now = time.time()
        r = dict(db.db.execute("SELECT * FROM messages WHERE message_id=7").fetchone())
        r["_source_binding"] = dependency_loss.source_binding(db.db, 1, 7)
        extract_llm._fail(db, r, 4)                       # attempts=5
        with db.db:
            db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='extract_llm'",
                          (now - extract_llm.REVIVE_COOLDOWN_S - 1,))
        for revival in range(1, extract_llm.REVIVE_PER_INPUT + 1):
            assert extract_llm.revive_failed(db, now)["revived"] == 1
            meta = json.loads(db.db.execute(
                "SELECT meta FROM artifacts WHERE kind='extract_llm'").fetchone()[0])
            assert meta["attempts"] == 4 and meta["auto_retry"] == revival
            extract_llm._fail(db, r, 4)                   # the extra try fails
            with db.db:
                db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='extract_llm'",
                              (now - extract_llm.REVIVE_COOLDOWN_S - 1,))
        assert extract_llm.revive_failed(db, now) == {"revived": 0, "skipped_cap": 1}
        db.save_messages([_message(7, body="合成の訂正済み本文")])
        r = dict(db.db.execute("SELECT * FROM messages WHERE message_id=7").fetchone())
        r["_source_binding"] = dependency_loss.source_binding(db.db, 1, 7)
        extract_llm._fail(db, r, 4)
        with db.db:
            db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='extract_llm'",
                          (now - extract_llm.REVIVE_COOLDOWN_S - 1,))
        assert extract_llm.revive_failed(db, now)["revived"] == 1
    finally:
        db.close()


def test_periodic_revive_refail_through_run_pending_keeps_cap(tmp_path, monkeypatch):
    """The production refail path (run_pending with a failing model)
    carries auto_retry forward, so REVIVE_PER_INPUT still stops a body
    that always fails."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の本文 常に失敗")])
        r = {"project_id": 1, "message_id": 1, "content_hash": _hash(db)}
        extract_llm._fail(db, r, 4)                       # attempts=5
        monkeypatch.setattr(extract_llm, "llm_extract", lambda body, **_: None)
        monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
        monkeypatch.setattr(extract_llm, "_llm_up", lambda **_: True)
        monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
        now = time.time()

        def age():
            with db.db:
                db.db.execute("UPDATE artifacts SET created_at=? WHERE kind='extract_llm'",
                              (now - extract_llm.REVIVE_COOLDOWN_S - 1,))
        age()
        for revival in range(1, extract_llm.REVIVE_PER_INPUT + 1):
            assert extract_llm.revive_failed(db, now)["revived"] == 1
            assert extract_llm.run_pending(db, limit=10, budget_s=30)["failed"] == 1
            metas = [json.loads(x["meta"]) for x in db.artifacts("extract_llm", message_id=1)]
            assert len(metas) == 1
            assert metas[0]["error"] and metas[0]["attempts"] == 5
            assert metas[0]["auto_retry"] == revival
            age()
        assert extract_llm.revive_failed(db, now) == {"revived": 0, "skipped_cap": 1}
    finally:
        db.close()


def test_refail_ignores_older_version_auto_retry(tmp_path):
    """An older extract version's spent revivals for the same body must
    not carry into the current version's first failure."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の本文 版違い")])
        r = {"project_id": 1, "message_id": 1, "content_hash": _hash(db)}
        extract_llm._fail(db, r, 4)
        with db.db:
            db.db.execute(
                "UPDATE artifacts SET meta=json_set(meta,'$.extract_version',?,"
                "'$.auto_retry',?) WHERE kind='extract_llm'",
                (extract_llm.EXTRACT_VERSION - 1, extract_llm.REVIVE_PER_INPUT))
        extract_llm._fail(db, r, 0)
        metas = [json.loads(x["meta"]) for x in db.artifacts("extract_llm", message_id=1)]
        assert len(metas) == 1
        assert metas[0]["extract_version"] == extract_llm.EXTRACT_VERSION
        assert "auto_retry" not in metas[0]
    finally:
        db.close()
