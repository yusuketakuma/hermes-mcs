"""An intervention-only repair must invalidate source-bound context scans."""
import extract_llm_runner
import extract_llm_source_contract
import json

import extract
import extract_llm
import mcs_util
import patient_context_refresh as refresh
import semantic_candidate
from test_patient_context_refresh import CFG, finish, runtime, seed

__all__ = ["runtime"]


def test_intervention_repair_keeps_receipts_but_requires_a_new_context_scan(runtime, monkeypatch, tmp_path):
    led, clock, calls = runtime
    monkeypatch.setattr(extract.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm_runner, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract_llm_source_contract, '_WRITER_STATE', extract_llm._WRITER_STATE)
    monkeypatch.setattr(extract, "_RECONCILE_IDLE_SECONDS", 0)
    monkeypatch.setattr(extract, "_RECONCILE_SECONDS", 60)
    # Approval storage is synthetic and does not access the real MCS root.
    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    body = "看護師が2026年9月21日に本人を訪問する予定です。"
    seed(led, body=body)
    with led.db:
        led.db.execute("UPDATE messages SET body_html='broken' WHERE message_id=1")
    assert extract.run_pending(led)["done"] == 1
    rule = led.db.execute("SELECT artifact_id,content,meta FROM artifacts "
                          "WHERE kind='extract_v1' AND message_id=1").fetchone()
    assert json.loads(rule["content"])["intervention_schedule"]["coverage"]["state"] == "invalid_source"
    # Restore verified source bytes before producing the pre-repair context proof.
    with led.db:
        led.db.execute("UPDATE messages SET body_html=? WHERE message_id=1", (body,))
    finish(runtime)
    source = led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    prior = led.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm' AND message_id=1 "
                           "ORDER BY artifact_id DESC LIMIT 1").fetchone()
    with led.db:
        extract_llm._record_hold_tx(led, source, prior, "qc_failed", remove_chunks=False)
    hold_query = "SELECT EXISTS(" + extract_llm._hold_sql() + ") FROM messages m WHERE m.message_id=1"
    assert led.db.execute(hold_query).fetchone()[0] == 1

    before, _, pending = refresh._pending(led.db, 1, CFG, now=clock.wall)
    old_scan = refresh._current_scans(led.db, 1, before["sources"], before["contract"])[1]
    assert old_scan["complete"] and old_scan["audited"] and not pending["B"]
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is not None
    protected_kinds = (refresh.KIND, refresh.SCAN, refresh.CHUNK, extract_llm.UPGRADE_KIND)
    protected = [tuple(row) for row in led.db.execute(
        "SELECT * FROM artifacts WHERE kind IN (?,?,?,?) ORDER BY artifact_id", protected_kinds)]

    # This receipt approves a fixed candidate/report, not an arbitrary future snapshot.
    report = {"criteria_sha256": "a" * 64, "run": {"snapshot_sha256": "b" * 64},
              "evidence": {"holdout_manifest_sha256": "c" * 64}}
    approval = semantic_candidate.record_approval(CFG, report)
    approval_path = tmp_path / "data" / "semantic_approvals" / (approval + ".json")
    receipt = approval_path.read_bytes()
    assert semantic_candidate.approval_matches(CFG, report, approval)
    calls_before = len(calls)
    with led.db:
        led.db.execute("UPDATE messages SET body_html=? WHERE message_id=1", (body,))
    assert extract._reconcile_intervention_page(led) == (1, {1}, None)

    repaired = led.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE artifact_id=?",
                              (rule["artifact_id"],)).fetchone()
    assert repaired["meta"] == rule["meta"]
    assert json.loads(repaired["content"])["intervention_schedule"]["coverage"]["state"] == "observed"
    assert len(calls) == calls_before  # Reconciliation itself makes no LLM call.
    after, _, pending = refresh._pending(led.db, 1, CFG, now=clock.wall)
    assert after["sources"][0]["content_hash"] == before["sources"][0]["content_hash"]
    assert after["sources"][0]["extraction_sig"] != before["sources"][0]["extraction_sig"]
    assert refresh._current_scans(led.db, 1, after["sources"], after["contract"]) == {}
    assert pending["B"] is True
    assert refresh.context_view_input(led.db, 1, cfg=CFG) is None
    assert [tuple(row) for row in led.db.execute(
        "SELECT * FROM artifacts WHERE kind IN (?,?,?,?) ORDER BY artifact_id", protected_kinds)] == protected
    assert led.db.execute(hold_query).fetchone()[0] == 1
    assert approval_path.read_bytes() == receipt
    assert semantic_candidate.approval_matches(CFG, report, approval)
    different_snapshot = {**report, "run": {"snapshot_sha256": "d" * 64}}
    assert not semantic_candidate.approval_matches(CFG, different_snapshot, approval)
