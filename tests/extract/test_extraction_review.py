"""Synthetic regressions for extraction review: state, leases and recovery."""
import json
import time

import pytest

import extract
import extract_bench
import extract_llm
from ledger import Ledger
from mcs_adapter import Message
import rollup


def _message(mid=1, body="合成本文", day=20):
    return Message(message_id=mid, project_id=1, parent_id=None,
                   sender_id=1, sender_name="SYNTH", sender_type="user",
                   profession="", organization="",
                   posted_at=f"2026-09-{day:02d}T00:00:00+09:00",
                   body_html=body, body_state="full", is_unread=False,
                   reply_count=0)


@pytest.fixture
def db(tmp_path):
    led = Ledger(str(tmp_path / "ledger.db"))
    led.ensure_patient(1)
    yield led
    led.close()


def artifact(db, mid, content):
    chash = db.db.execute("SELECT content_hash FROM messages WHERE message_id=?",
                         (mid,)).fetchone()[0]
    db.artifact_add("extract_llm", json.dumps(content), project_id=1,
                    message_id=mid, meta={"hash": chash,
                                         "extract_version": extract_llm.EXTRACT_VERSION})


def test_family_mention_does_not_cancel_patient_medication(db):
    db.save_messages([_message(1, day=19), _message(2, day=20)])
    artifact(db, 1, {"meds": [{"name": "合成薬A", "subject": "patient",
                               "status": "current", "action": "start"}]})
    artifact(db, 2, {"meds": [{"name": "合成薬A", "subject": "family",
                               "status": "past", "action": "stop"}]})
    assert [m["name"] for m in rollup.build_rollup(db, 1)["medications"]] == ["合成薬A"]


def test_same_message_stop_supersedes_start_and_rule_candidate(db):
    db.save_messages([_message()])
    artifact(db, 1, {"meds": [
        {"name": "合成薬A", "subject": "patient", "status": "current", "action": "start"},
        {"name": "合成薬A", "subject": "patient", "status": "current", "action": "stop"}]})
    assert not rollup.build_rollup(db, 1).get("medications")


def test_family_symptom_does_not_become_patient_state(db):
    db.save_messages([_message()])
    artifact(db, 1, {"symptoms": [{"text": "合成症状", "subject": "family",
                                   "status": "ongoing", "negated": False}]})
    assert not rollup.build_rollup(db, 1).get("recent_symptoms")


def test_schema_allows_subject_and_raw_relative_deadline():
    props = extract_llm._SCHEMA["schema"]["properties"]
    assert "subject" in props["symptoms"]["items"]["properties"]
    assert "due_text" in props["requests"]["items"]["properties"]


def test_claimed_head_does_not_starve_other_pending_messages(db, monkeypatch):
    db.save_messages([_message(1, day=20), _message(2, day=19)])
    row = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
    extract_llm._claim(db, row)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *a, **kw: {"summary": "合成"})
    result = extract_llm.run_pending(db, limit=1)
    assert result["done"] == 1
    assert db.artifacts("extract_llm")[0]["message_id"] == 2


def test_changed_source_is_not_overwritten_by_inflight_extraction(db, monkeypatch):
    db.save_messages([_message(body="古い合成本文")])

    def infer(*args, **kwargs):
        db.save_messages([_message(body="変更後の合成本文")])
        artifact(db, 1, {"summary": "変更後の結果"})
        return {"summary": "古い結果"}

    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    result = extract_llm.run_pending(db)
    assert result["done"] == 0
    assert [json.loads(a["content"])["summary"]
            for a in db.artifacts("extract_llm")] == ["変更後の結果"]


@pytest.mark.parametrize("workers", [1, 2])
def test_chunk_survives_later_exception_and_all_leases_release(db, monkeypatch, workers):
    db.save_messages([_message(1, body="あ" * 2500 + "\n" + "い" * 1200),
                      _message(2, body="あ" * 2500 + "\n" + "い" * 1200)])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kw: "plain")

    def infer(prompt, **kwargs):
        if "い" * 100 in prompt:
            raise RuntimeError("synthetic interruption")
        return {"summary": "完了チャンク"}

    monkeypatch.setattr(extract_llm, "_llm_call", infer)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        extract_llm.run_pending(db, workers=workers)
    assert db.artifacts("extract_llm_chunk")
    assert not db.artifacts("extract_llm")
    assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0


def test_lease_covers_whole_batch_budget(db, monkeypatch):
    db.save_messages([_message()])

    def infer(*args, **kwargs):
        expiry = db.db.execute("SELECT next_try FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0]
        assert expiry - time.time() >= 3600
        return {"summary": "合成"}

    monkeypatch.setattr(extract_llm, "llm_extract", infer)
    assert extract_llm.run_pending(db, budget_s=3600)["done"] == 1


def test_missing_post_date_does_not_fabricate_regimen_year():
    out = extract.extract_message("内服薬 9/1-9/14", "不明")
    assert all(not p.get("start") and not p.get("end")
               for p in out.get("med_periods", []))


def test_failed_benchmark_counts_safety_attribute_misses():
    case = {"id": "synthetic", "expect": {"meds": [
        {"name": "合成薬A", "status": "past", "subject": "patient", "negated": False}]}}
    scored = extract_bench._score_case(case, None)
    assert scored["fields"]["med_status"]["fn"] == 1
    assert scored["fields"]["med_subject"]["fn"] == 1
    assert scored["fields"]["med_negated"]["fn"] == 1


@pytest.mark.parametrize("body,posted,start,end", [
    ("内服薬12/28-1/10まで投与した", "2027-01-20", "2026-12-28", "2027-01-10"),
    ("内服薬2025/9/1-9/14", "2026-09-23", "2025-09-01", "2025-09-14"),
])
def test_regimen_keeps_explicit_year_and_recent_wrapped_period(body, posted, start, end):
    period = extract.extract_message(body, posted)["med_periods"][0]
    assert (period["start"], period["end"]) == (start, end)


def test_rule_change_reprocesses_unchanged_body(db):
    db.save_messages([_message(body="内服薬9/24-10/7の予定")])
    chash = db.db.execute("SELECT content_hash FROM messages").fetchone()[0]
    db.artifact_add("extract_v1", json.dumps({"med_periods": [
        {"start": "2025-09-24", "end": "2026-10-07"}]}),
        project_id=1, message_id=1, meta={"hash": chash})
    assert extract.run_pending(db)["done"] == 1
    period = json.loads(db.artifacts("extract_v1")[0]["content"])["med_periods"][0]
    assert period["start"] == "2026-09-24"
    assert extract.run_pending(db)["done"] == 0


def test_resident_idle_wait_honors_stop_and_reports_loaded_generation(monkeypatch, capsys):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(extract_llm.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(extract_llm.time, "sleep", clock.sleep)
    monkeypatch.setattr(extract_llm.sys, "argv", ["extract_llm", "--all", "--stop-after", "2"])
    monkeypatch.setattr(extract_llm, "Ledger", lambda *a: type("DB", (), {"close": lambda self: None})())
    monkeypatch.setattr(extract_llm, "run_pending", lambda *a, **kw: {
        "done": 0, "failed": 0, "left": 0, "selected": 0})
    assert extract_llm.main() == 0
    assert clock.now == 2
    record = json.loads(capsys.readouterr().err)
    assert record["extract_version"] == extract_llm.EXTRACT_VERSION
    assert record["source_digests"] == extract_llm._LOADED_SOURCE_DIGESTS


def test_chunk_checkpoint_requires_same_interpretation_context(db):
    db.save_messages([_message()])
    row = db.db.execute("SELECT * FROM messages").fetchone()
    extract_llm._persist_chunks(db, row, {0: {"summary": "合成"}}, "元の文脈")
    assert extract_llm._saved_chunks(db, row, "元の文脈")
    assert not extract_llm._saved_chunks(db, row, "変更後の文脈")


def test_chunk_merge_preserves_restart_and_separate_subjects():
    def med(action, dose):
        return {"name": "合成薬", "subject": "patient", "action": action, "dose": dose}
    merged = extract_llm._merge([
        {"meds": [med("start", "1mg")],
         "symptoms": [{"text": "合成症状", "subject": "patient"}]},
        {"meds": [med("stop", None)]},
        {"meds": [med("start", "2mg")],
         "symptoms": [{"text": "合成症状", "subject": "family", "negated": True}]},
    ])
    assert merged["meds"][-1] == med("start", "2mg")
    assert {s["subject"] for s in merged["symptoms"]} == {"patient", "family"}
