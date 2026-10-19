"""Automatic replacements retain source-grounded negation and nonpatient assertions."""
import json

import pytest

import extract_llm as llm
from extract_testkit import _hash, _ledger, _message, _qc_artifact

BODY = "本人に息切れはありません。家族には軽い発熱があります。"
PRIOR = {"summary": "合成の報告", "symptoms": [
    {"text": "息切れ", "negated": True, "status": "ongoing", "subject": "patient", "evidence": "本人に息切れはありません。"},
    {"text": "発熱", "negated": False, "status": "ongoing", "subject": "family", "evidence": "家族には軽い発熱があります。"}]}


def seed(tmp_path, *, version=None, body=BODY):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=body)])
    aid = db.artifact_add("extract_llm", json.dumps(PRIOR, ensure_ascii=False), project_id=1, message_id=1,
        meta={"hash": _hash(db), "extract_version": llm.EXTRACT_VERSION - 1 if version is None else version})
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    return db, aid, row


@pytest.mark.parametrize("route", ["upgrade", "thin", "qc"])
def test_automatic_replacements_preserve_the_original_artifact_and_stop_repeating(tmp_path, route):
    db, aid, row = seed(tmp_path, version=None if route == "upgrade" else llm.EXTRACT_VERSION)
    try:
        if route == "thin":
            # One assertion on a long body qualifies for the actual thin retry.
            db.save_messages([_message(body=BODY + "合成の経過報告です。" * 40)])
            row = dict(db.db.execute("SELECT * FROM messages").fetchone())
            with db.db:
                db.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                    (json.dumps({"symptoms": PRIOR["symptoms"][:1]}), json.dumps({
                        "hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION}), aid))
        if route == "qc":
            _qc_artifact(db, 1, aid, row["content_hash"], {"qc": "done", "items": [{
                "section": "symptoms", "verdict": "NO_MATCH", "item": PRIOR["symptoms"][0]}]})
        assert db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
        original = dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())
        kwargs = {"extra_meta": {"thin_retried": True}} if route == "thin" else (
            {"qc_fix": {"qc": 123, "applied": True}} if route == "qc" else {})
        for _ in range(2):
            assert llm._replace_current(db, row, json.dumps({"summary": "合成の候補", "symptoms": []}), **kwargs) is False
        assert dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()) == original
        receipts = db.artifacts("extract_upgrade_v1", message_id=1)
        assert len(receipts) == 1
        receipt = json.loads(receipts[0]["content"])
        assert receipt["state"] == "held_prior" and receipt["prior_artifact_id"] == aid
        assert receipt["target_version"] == llm.EXTRACT_VERSION
        assert not db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
    finally:
        db.close()


def test_adding_attributes_to_protected_items_is_not_information_loss(tmp_path):
    db, aid, row = seed(tmp_path)
    try:
        candidate = json.loads(json.dumps(PRIOR))
        candidate["symptoms"][1]["severity"] = "mild"
        assert llm._replace_current(db, row, json.dumps(candidate)) is True
        assert not db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
        assert not db.artifacts("extract_upgrade_v1")
    finally:
        db.close()


def test_an_edited_body_does_not_inherit_a_prior_hashs_protection(tmp_path):
    db, _, _ = seed(tmp_path)
    try:
        db.save_messages([_message(body="本人の息切れが改善しました。")])
        row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        assert llm._replace_current(db, row, json.dumps({"summary": "合成の新しい報告"})) is True
        assert not db.artifacts("extract_upgrade_v1")
    finally:
        db.close()


def test_unverified_prior_assertions_do_not_freeze_a_correction(tmp_path):
    db, aid, row = seed(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({
                "symptoms": [{"text": "発熱", "negated": True, "subject": "family", "evidence": "本文にない引用"}]}), aid))
        assert llm._replace_current(db, row, json.dumps({"summary": "合成の候補"})) is True
    finally:
        db.close()


def test_a_located_legacy_negation_is_protected_even_without_new_optional_flags(tmp_path):
    db, aid, row = seed(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({"symptoms": [{
                "text": "息切れ", "negated": True, "evidence": "本人に息切れはありません。"}]}), aid))
        assert llm._replace_current(db, row, json.dumps({"summary": "合成の候補"})) is False
        assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
    finally:
        db.close()


def test_human_confirmed_feedback_can_correct_an_old_wrong_flag(tmp_path, monkeypatch):
    from adapters.common import envelopes
    import mcs_requests
    db, aid, _ = seed(tmp_path)
    try:
        row = dict(db.db.execute("SELECT * FROM messages").fetchone())
        assert llm._replace_current(db, row, '{"summary":"合成の弱い自動候補"}') is False
        request = envelopes.extract_feedback("synthetic-operator", {"project_id": 1, "extract_ref": {
            "message_id": 1, "artifact_id": aid}}, "symptoms", "合成の確認結果")
        assert mcs_requests.apply_command(db, request)["outcome"] == "applied"
        calls = []
        monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: calls.append(1) or {"summary": "人の確認に基づく合成訂正"})
        assert llm.run_pending(db, limit=1, budget_s=10)["done"] == 1
        assert calls == [1]
        result = db.artifacts("extract_llm", message_id=1)
        assert len(result) == 1 and json.loads(result[0]["meta"])["human_fix"]["applied"] is True
    finally:
        db.close()


@pytest.mark.parametrize("field,item,body", [
    ("labs", {"name": "CRP", "value": 1, "subject": "family", "status": "current",
              "evidence": "家族のCRPは1です。"}, "家族のCRPは1です。"),
    ("patient_context", {"category": "living", "text": "独居", "subject": "other",
                         "evidence": "知人は独居です。"}, "知人は独居です。"),
])
def test_nonpatient_labs_and_context_are_also_protected(tmp_path, field, item, body):
    db, aid, row = seed(tmp_path, body=body)
    try:
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps({field: [item]}), aid))
        assert llm._replace_current(db, row, '{"summary":"合成の候補"}') is False
        assert json.loads(db.artifacts(llm.UPGRADE_KIND)[0]["content"])["lost"] == {field: 1}
    finally:
        db.close()


def test_lost_protection_settles_as_held_not_success_or_infinite_retry(tmp_path, monkeypatch):
    db, aid, _ = seed(tmp_path)
    calls = []
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: calls.append(1) or {"summary": "合成の弱い候補"})
    try:
        result = llm.run_pending(db, limit=1, budget_s=10)
        assert (result["done"], result["held_prior"], result["deferred"], result["left"]) == (0, 1, 0, None)
        assert llm.run_pending(db, limit=1, budget_s=10)["selected"] == 0
        assert calls == [1] and db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
        status = llm.upgrade_status(db.db, model=llm.MODEL)
        assert status["held_prior"] == 1 and status["done_clean"] == status["pending"] == 0
    finally:
        db.close()


@pytest.mark.parametrize("route", ["thin", "qc"])
def test_actual_quality_retry_routes_reject_lost_protected_facts(tmp_path, monkeypatch, route):
    body = BODY + ("合成の経過報告です。" * 40 if route == "thin" else "")
    db, aid, _ = seed(tmp_path, version=llm.EXTRACT_VERSION, body=body)
    calls = []
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: calls.append(1) or {"summary": "合成の弱い候補"})
    try:
        if route == "thin":
            with db.db:
                db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                              (json.dumps({"symptoms": PRIOR["symptoms"][:1]}), aid))
        else:
            _qc_artifact(db, 1, aid, _hash(db), {"qc": "done", "items": [{
                "section": "symptoms", "verdict": "NO_MATCH", "item": PRIOR["symptoms"][0]}]})
        original = dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())
        result = llm.run_pending(db, limit=1, budget_s=10)
        assert result["done"] == 0 and result["held_prior"] == 1
        assert dict(db.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()) == original
        assert llm.run_pending(db, limit=1, budget_s=10)["selected"] == 0 and calls == [1]
    finally:
        db.close()


@pytest.mark.parametrize("change", ["hash", "prior_artifact_id", "target_version", "contract", "project", "invalidated"])
def test_a_hold_is_only_valid_for_its_exact_source_and_contract(tmp_path, change):
    db, _, row = seed(tmp_path)
    try:
        assert llm._replace_current(db, row, '{"summary":"合成の候補"}') is False
        artifact = db.artifacts(llm.UPGRADE_KIND)[0]
        receipt = json.loads(artifact["content"])
        meta = json.loads(artifact["meta"])
        if change == "project":
            with db.db:
                db.db.execute("UPDATE artifacts SET project_id=NULL WHERE artifact_id=?", (artifact["artifact_id"],))
        else:
            if change == "invalidated":
                meta["invalidated"] = True
            else:
                receipt[change] = -1 if change in ("prior_artifact_id", "target_version") else "stale"
            with db.db:
                db.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                              (json.dumps(receipt), json.dumps(meta), artifact["artifact_id"]))
        assert db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
    finally:
        db.close()


def test_failed_human_feedback_never_relabels_old_content_as_the_new_generation(tmp_path, monkeypatch):
    from adapters.common import envelopes
    import mcs_requests
    db, aid, _ = seed(tmp_path)
    monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: None)
    monkeypatch.setattr(llm, "_llm_up", lambda **k: True)
    try:
        request = envelopes.extract_feedback("synthetic-operator", {"project_id": 1, "extract_ref": {
            "message_id": 1, "artifact_id": aid}}, "symptoms", "合成の確認結果")
        assert mcs_requests.apply_command(db, request)["outcome"] == "applied"
        result = llm.run_pending(db, limit=1, budget_s=10)
        assert result["failed"] == 1 and result["deferred"] == result["done"] == 0
        kept = db.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
        assert json.loads(kept["meta"])["extract_version"] == llm.EXTRACT_VERSION - 1
        assert db.db.execute("SELECT (" + llm._human_flagged_sql() + ") FROM messages m").fetchone()[0] is None
        # A normal generation upgrade remains possible; only this report is consumed.
        assert db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
    finally:
        db.close()


def test_terminal_upgrade_failure_keeps_prior_and_remains_revivable(tmp_path):
    db, aid, row = seed(tmp_path)
    try:
        llm._fail(db, row, 4)
        assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
        receipt = json.loads(db.artifacts(llm.UPGRADE_KIND)[0]["content"])
        assert receipt["reason"] == "attempts_exhausted" and receipt["prior_artifact_id"] == aid
        assert llm.upgrade_status(db.db, model=llm.MODEL)["failed"] == 1
        import time
        assert llm.revive_failed(db, now=time.time() + llm.REVIVE_COOLDOWN_S + 1)["revived"] == 1
        assert llm.upgrade_status(db.db, model=llm.MODEL)["pending"] == 1
    finally:
        db.close()


def test_dropped_unprotected_facts_are_counted_on_an_actual_new_result(tmp_path):
    db, aid, row = seed(tmp_path, body="本人に発熱があります。")
    try:
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({"symptoms": [{
                "text": "発熱", "status": "ongoing", "subject": "patient", "negated": False,
                "evidence": "本人に発熱があります。"}]}), aid))
        assert llm._replace_current(db, row, '{"summary":"合成の新結果"}') is True
        comparison = json.loads(db.artifacts(llm.KIND)[0]["meta"])["upgrade"]
        assert comparison == {"prior_artifact_id": aid, "prior_version": llm.EXTRACT_VERSION - 1, "facts_lost": 1}
    finally:
        db.close()


def test_prior_is_read_after_the_write_lock_including_a_concurrent_correction(tmp_path):
    db, _, row = seed(tmp_path)
    other = _ledger(tmp_path)
    published = []

    def publish_before_lock(sql):
        if not published and sql == "BEGIN IMMEDIATE":
            published.append(other.artifact_add(llm.KIND, json.dumps(PRIOR), project_id=1, message_id=1,
                meta={"hash": row["content_hash"], "extract_version": llm.EXTRACT_VERSION - 1}))
    db.db.set_trace_callback(publish_before_lock)
    try:
        assert llm._replace_current(db, row, '{"summary":"合成の弱い候補"}') is False
        assert len(published) == 1
        receipt = json.loads(db.artifacts(llm.UPGRADE_KIND)[0]["content"])
        assert receipt["prior_artifact_id"] == published[0]
        assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (published[0],)).fetchone()
    finally:
        db.db.set_trace_callback(None)
        other.close()
        db.close()


@pytest.mark.parametrize("module_name", ["extract_schema", "extract_validation", "extract_quality", "clinical_scope", "patient_context", "mcs_queries"])
def test_moved_source_contract_reopens_hold_without_losing_protected_assertions(tmp_path, monkeypatch, module_name):
    import semantic_candidate
    db, aid, row = seed(tmp_path)
    try:
        weak = '{"summary":"合成の候補"}'
        assert not llm._replace_current(db, row, weak)
        assert not db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
        contract = llm._upgrade_contract()
        fingerprint = semantic_candidate.candidate_fingerprint({})
        assert len(llm._LOADED_SOURCE_DIGESTS[module_name]) == 64
        monkeypatch.setattr(llm, "_LOADED_SOURCE_DIGESTS", {
            **llm._LOADED_SOURCE_DIGESTS, module_name: "synthetic-changed-loaded-source"})
        assert llm._upgrade_contract() != contract
        assert semantic_candidate.candidate_fingerprint({}) != fingerprint
        assert db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
        assert not llm._replace_current(db, row, weak)
        receipts = [json.loads(r["content"]) for r in db.artifacts(llm.UPGRADE_KIND)]
        assert len(receipts) == 2
        assert all(r["state"] == "held_prior" and r["prior_artifact_id"] == aid for r in receipts)
        assert receipts[-1]["contract"] == llm._upgrade_contract()
        assert db.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0] == json.dumps(PRIOR, ensure_ascii=False)
        assert not db.db.execute("SELECT 1 FROM messages m WHERE " + llm.pending_pred()).fetchone()
    finally:
        db.close()


@pytest.mark.parametrize("module_name,member", [
    ("extract_schema", "SCHEMA"),
    ("extract_validation", "Validator"),
    ("extract_quality", "quality_improves"),
    ("clinical_scope", "patient_source_scope"),
    ("clinical_values", "patient_vitals"),
    ("patient_context", "pharmacy_state_items"),
    ("patient_context", "PHARMACY_STATE_KEYS"),
    ("mcs_queries", "karte_summary_current"),
    ("mcs_queries", "karte_summary_block"),
    ("mcs_queries", "iter_artifacts"),
])
def test_moved_definition_changes_new_candidate_identity(monkeypatch, module_name, member):
    import importlib
    import semantic_candidate
    module = importlib.import_module(module_name)
    monkeypatch.setattr(semantic_candidate, "_LOADED_SOURCES", None)
    before = semantic_candidate.candidate_fingerprint({})
    assert module_name in semantic_candidate._LOADED_SOURCES
    if member == "SCHEMA":
        monkeypatch.setattr(module, member, {**module.SCHEMA, "synthetic_marker": True})
    elif member == "PHARMACY_STATE_KEYS":
        monkeypatch.setattr(module, member, module.PHARMACY_STATE_KEYS | {"synthetic_marker"})
    else:
        def changed(*args, **kwargs):
            return None
        changed.__module__ = module_name
        if member == "Validator":
            monkeypatch.setattr(module.Validator, "meds", changed)
        else:
            monkeypatch.setattr(module, member, changed)
    # A running worker keeps its captured code identity; a new capture must invalidate approval.
    if member != "SCHEMA":
        assert semantic_candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(semantic_candidate, "_LOADED_SOURCES", None)
    assert semantic_candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("member", ["_scope_events", "_scope_puncts"])
def test_scope_cache_contents_do_not_replace_cached_definition_coverage(monkeypatch, member):
    import clinical_scope
    import semantic_candidate
    wrapper = getattr(clinical_scope, member)
    before = semantic_candidate._sources()
    body = "本人の体温は36.5℃です。家族の血圧は120/80です。"
    clinical_scope.patient_source_scope(body, 0, 2)
    assert wrapper.cache_info().currsize > 0
    assert semantic_candidate._sources() == before
    def changed(*args, **kwargs):
        return ()
    monkeypatch.setattr(wrapper.__wrapped__, "__code__", changed.__code__)
    assert semantic_candidate._sources()["clinical_scope"] != before["clinical_scope"]
