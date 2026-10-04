"""Regressions for the 2026-09-30 adversarial review: each test pins the
corrected behavior of a reproduced defect. Synthetic data only."""
import json
import time
from types import SimpleNamespace

import extract_llm
import mcs_adapter
import notify_views
import rollup
import run_check
import semantic
import semantic_runtime as runtime
from semantic_testkit import (
    _AuditFailJev, _canonical_cfg, _ledger,
    _llm_v2, _message, _patient, _seeded_two,
)


def test_cap_gate_rejects_failed_unstored_reply(tmp_path):
    db = _ledger(tmp_path)
    try:
        p = _patient(db)
        p.messages = [_message(1)]
        reply = _message(2, parent=1)
        reply.body_state = "snippet"
        p.messages[0].replies = [reply]
        db.save_patient(p)
        db.job_add("reply", 1, 2, parent_id=1)
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET state='failed' WHERE kind='reply'")
        db.set_history_floor(1, 0)
        db.set_coverage(1, db.high_watermark(1))
        assert db.db.execute("SELECT body_state FROM messages WHERE message_id=2").fetchone()[0] == "snippet"
        assert db.unread_cap_cleared(1, 1) is False
        p.messages = []
        p.fetch_state = "pending"
        acknowledgements = []

        class Adapter:
            def list_unread(self):
                return mcs_adapter.UnreadSnapshot(1790000000, [p])

            def fetch_unread_messages(self, *args):
                return mcs_adapter.MessageBatch([], error=mcs_adapter.MCSError("http_error", status=400))

            def oldest_unread_id(self, pid):
                return 1

            def fetch_latest(self, pid):
                return {"message_id": 1}

            def mark_patient_read(self, pid, ts, fallback_plain=False):
                acknowledgements.append(fallback_plain)

        result = {"errors": [], "incomplete": [], "messages": 0,
                  "new_messages": 0, "marked_read": []}
        run_check.stage_unread(Adapter(), db, SimpleNamespace(mark_read=True),
                               result, time.monotonic()+300, None)
        assert acknowledgements == []
        assert result["marked_read"] == []
    finally:
        db.close()


def test_budget_defer_keeps_fact_repair_until_dispatch(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    db = _seeded_two(tmp_path)
    calls = []
    cfg = _canonical_cfg()
    cfg["semantic"]["job_budget_seconds"] = 450.0
    choices = {f"has_{c}": "absent" for c in __import__("semantic_facts").MANDATORY_CATEGORIES}
    choices["has_medication"] = "present"

    def llm(prompt, timeout=None):
        calls.append(prompt)
        clock[0] += 200
        return _llm_v2(prompt)

    try:
        first = semantic.run_due(db, cfg, {"errors": []}, clock[0] + 450,
                                  jev_client=_AuditFailJev(choice_map=choices), llm_fn=llm)
        assert first["deferred"] == 1
        repairs = db.artifacts("semantic_fact_repair", message_id=1)
        if not repairs:
            from semantic_policy import KIND_FACT_REPAIR
            repairs = db.artifacts(KIND_FACT_REPAIR, message_id=1)
        assert len(calls) == 1
        # the repair never left: its one-shot reservation is released
        assert not repairs
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        second = semantic.run_due(db, cfg, {"errors": []}, clock[0] + 450,
                                   jev_client=_AuditFailJev(choice_map=choices), llm_fn=llm)
        assert len(calls) == 2          # the repair is dispatched now
        assert second["job_metrics"]
    finally:
        db.close()


def test_summary_button_reads_newest_summary_over_rollup(tmp_path):
    db = _ledger(tmp_path)
    try:
        _patient(db)
        db.karte_summary_store(1, 1, {"comment": "SYNTH-OLD", "updated_at": "2026-09-29T00:00:00+09:00"})
        rollup.rebuild(db, 1)
        db.karte_summary_store(1, 1, {"comment": "SYNTH-NEW", "updated_at": "2026-09-30T00:00:00+09:00"})
        text = notify_views.patient_summary_text(db.db, 1)[1]
        assert "SYNTH-NEW" in text and "SYNTH-OLD" not in text
    finally:
        db.close()


def test_exhausted_revive_prefix_does_not_starve_tail(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    try:
        for i in range(extract_llm.REVIVE_MAX * 4 + 1):
            db.save_messages([_message(i + 1, body="synthetic exhausted input")], project_id=1)
            db.artifact_add("extract_llm", '{"_error":true}', project_id=1, message_id=i+1,
                            meta={"error": True, "extract_version": extract_llm.EXTRACT_VERSION,
                                  "attempts": 5, "hash": "synthetic",
                                  "auto_retry": 3 if i < extract_llm.REVIVE_MAX * 4 else 0})
        with db.db:
            db.db.execute("UPDATE artifacts SET created_at=?", (now - 25000,))
        result = extract_llm.revive_failed(db, now)
        assert result == {"revived": 1, "skipped_cap": 120}
        tail = json.loads(db.artifacts("extract_llm")[-1]["meta"])
        assert tail["auto_retry"] == 1 and tail["attempts"] == 4
    finally:
        db.close()


def test_chunk_merge_keeps_requests_with_different_condition_and_due():
    first = {"requests": [{"to": "医師", "action": "連絡", "kind": "request",
                            "condition": "血圧が160を超えたら", "due_text": "明日"}]}
    second = {"requests": [{"to": "医師", "action": "連絡", "kind": "request",
                             "condition": "SpO2が90未満なら", "due_text": "今夜"}]}
    result = extract_llm._merge([first, second])
    assert [r["condition"] for r in result["requests"]] == [
        "血圧が160を超えたら", "SpO2が90未満なら"]
