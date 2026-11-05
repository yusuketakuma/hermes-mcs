"""Synthetic extract llm quality feedback contracts with isolated shared fixtures."""
import extract_llm_engine
import extract_llm_runner
import json
import pytest
import extract_llm
from inference_version import CURRENT_VERSION, VERSION_KEY
from extract_testkit import _extract_artifact, _hash, _ledger, _message, _qc_artifact, _seed_qc_flagged


def test_llm_extract_injects_qc_feedback(monkeypatch):
    seen = []

    def fake(prompt, **_):
        seen.append(prompt)
        return {"symptoms": [], "summary": "ok"}

    monkeypatch.setattr(extract_llm, "_llm_call", fake)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extract_llm.llm_extract(
        "本文", feedback=["vitals項目「bs=48」は本文に裏付けがありません"])
    assert "監査フィードバック" in seen[0]
    assert "裏付けがありません" in seen[0]


def test_qc_flagged_row_reextracts_with_feedback(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    _seed_qc_flagged(db)
    seen = {}

    def fake(body, **kw):
        seen["feedback"] = kw.get("feedback")
        return {"vitals": {"hr": 48}, "summary": "s2"}

    monkeypatch.setattr(extract_llm, "llm_extract", fake)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["done"] == 1
    assert seen["feedback"] and "裏付け" in seen["feedback"][0]
    art = db.artifacts("extract_llm", message_id=1)[-1]
    meta = json.loads(art["meta"])
    assert meta["qc_fix"]["applied"] is True
    assert json.loads(art["content"])["vitals"] == {"hr": 48}
    # qc_fix ends the loop — no re-selection, no further calls
    res2 = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res2["selected"] == 0 and res2["done"] == 0


def test_qc_flagged_failed_repair_settles_flag(tmp_path, monkeypatch):
    """A failed feedback re-extract re-mints the audited artifact with
    qc_fix applied=False — the row stops re-entering pending instead of
    burning an LLM call every cycle."""
    db = _ledger(tmp_path)
    _seed_qc_flagged(db)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *a, **k: None)
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    monkeypatch.setattr(extract_llm, "_llm_up", lambda **k: True)
    monkeypatch.setattr(extract_llm_runner, '_llm_up', extract_llm._llm_up)
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["failed"] == 1
    art = db.artifacts("extract_llm", message_id=1)[-1]
    meta = json.loads(art["meta"])
    assert meta["qc_fix"]["applied"] is False
    assert json.loads(art["content"])["vitals"] == {"bs": 48}
    res2 = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res2["selected"] == 0


def test_qc_urgency_mismatch_flags_row(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body="経過観察の記録")])
    chash = _hash(db, 1)
    src = _extract_artifact(db, 1, {"summary": "s",
                                    "urgency": "routine"}, chash)
    _qc_artifact(db, 1, src, chash,
                 {"qc": "done", "items": [],
                  "urgency": {"extracted": "routine", "jev": "high",
                              "confidence": 0.9}})
    seen = {}
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda body, **kw: seen.update(kw)
                        or {"summary": "s", "urgency": "high"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["done"] == 1
    assert "緊急度" in seen["feedback"][0]


def test_qc_clean_artifact_stays_settled(tmp_path, monkeypatch):
    """A MATCH verdict must not re-pend the row."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    chash = _hash(db, 1)
    src = _extract_artifact(db, 1, {"summary": "s"}, chash)
    _qc_artifact(db, 1, src, chash,
                 {"qc": "done", "items": [
                     {"section": "meds", "index": 0, "item": {"name": "薬"},
                      "verdict": "MATCH", "noul": 0.9}]})
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *a, **k: {"summary": "x"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["selected"] == 0


@pytest.mark.parametrize("text", ["no match", "No-Match", "NO_MATCH"])
def test_qc_match_verdict_with_no_match_text_stays_settled(
        tmp_path, monkeypatch, text):
    """U05-F05: the SQL flag must agree with _qc_feedback — a MATCH
    audit whose item TEXT happens to contain 'no match' (LIKE is
    case-insensitive and '_' is a wildcard) was selected, skipped
    unsettled and re-selected every cycle."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    chash = _hash(db, 1)
    src = _extract_artifact(db, 1, {"summary": "s"}, chash)
    _qc_artifact(db, 1, src, chash,
                 {"qc": "done", "items": [
                     {"section": "points", "index": 0,
                      "item": {"text": f"合成 {text} 記載"},
                      "verdict": "MATCH", "noul": 0.9}]})
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *a, **k: {"summary": "x"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    for _ in range(2):
        res = extract_llm.run_pending(db, limit=10, budget_s=120)
        assert res["selected"] == 0


def test_qc_feedback_ignores_superseded_generation_audit(tmp_path,
                                                         monkeypatch):
    """_qc_feedback reads the same current-generation audit the SQL
    flag pinned: an older-version audit on the same source is not the
    newest current QC."""
    db = _ledger(tmp_path)
    _seed_qc_flagged(db)
    chash = _hash(db, 1)
    src = db.artifacts("extract_llm", message_id=1)[-1]["artifact_id"]
    db.artifact_add(
        "extract_qc", json.dumps({"qc": "done", "items": []}),
        project_id=1, message_id=1, model="jev-test",
        meta={VERSION_KEY: CURRENT_VERSION, "hash": chash,
              "extract_version": extract_llm.EXTRACT_VERSION - 1,
              "source_artifact_id": src, "qc": "done"})
    fb = extract_llm._qc_feedback(db, src)
    assert fb is not None and "裏付け" in fb["notes"][0]


def test_qc_stale_source_artifact_does_not_flag(tmp_path, monkeypatch):
    """A QC row pinned to a SUPERSEDED extraction never re-pends the
    message — only the verdict on the current artifact counts."""
    db = _ledger(tmp_path)
    db.save_messages([_message()])
    chash = _hash(db, 1)
    old = _extract_artifact(db, 1, {"summary": "old"}, chash)
    _qc_artifact(db, 1, old, chash,
                 {"qc": "done", "items": [
                     {"section": "meds", "index": 0, "item": {"name": "薬"},
                      "verdict": "NO_MATCH", "noul": 0.1}]})
    _extract_artifact(db, 1, {"summary": "new"}, chash)  # supersedes
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *a, **k: {"summary": "x"})
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    res = extract_llm.run_pending(db, limit=10, budget_s=120)
    assert res["selected"] == 0


def test_newer_clean_qc_supersedes_older_flag(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    chash = _seed_qc_flagged(db)
    src = db.artifacts("extract_llm")[0]["artifact_id"]
    _qc_artifact(db, 1, src, chash, {"qc": "done", "items": []})
    monkeypatch.setattr(extract_llm, "llm_extract",
                        lambda *a, **kw: pytest.fail("latest QC is clean"))
    monkeypatch.setattr(extract_llm_runner, 'llm_extract', extract_llm.llm_extract)
    assert extract_llm.run_pending(db)["selected"] == 0
    db.close()
