"""Private extraction status contains current bound counts, never source or chunk text."""
import pytest

import extraction_progress
import extract_llm
import notify_views
import semantic_extraction
from notify_testkit import led, _msg, _patient

__all__ = ["led"]


def test_private_summary_uses_only_validated_counts_and_no_checkpoint_text(led, monkeypatch):
    _patient(led)
    for mid in (1, 2, 3):
        _msg(led, mid, body="SOURCE_CANARY")
    states = {1: {"state": "complete", "completed": 2, "total": 2},
              2: {"state": "processing", "completed": 1, "total": 3},
              3: {"state": "attention", "completed": 1, "total": 2}}

    def read(db, row, **kwargs):
        assert row["project_id"] == 1
        return {**states[row["message_id"]], "content": "CHUNK_CANARY", "body": "SOURCE_CANARY"}

    monkeypatch.setattr(extract_llm, "progress_for_message", read, raising=False)
    led.db.execute("PRAGMA query_only=ON")
    progress = extraction_progress.patient_progress(led.db, 1, cfg={})
    assert progress == {"processing": 1, "complete": 1, "attention": 1,
                        "completed": 4, "total": 7, "posts": 3, "limited": False}
    text = notify_views.patient_summary_text(led.db, 1, cfg={})[1]
    assert "処理中1・完了1・要確認1（7区間中4区間完了）" in text
    assert "完了は抽出処理のみ" not in text
    assert "CANARY" not in text and "CANARY" not in str(progress)


def test_canonical_active_never_falls_back_to_completed_legacy(led, monkeypatch):
    _patient(led)
    _msg(led, 1)
    calls = []
    monkeypatch.setattr(extract_llm, "progress_for_message", lambda *a, **k: pytest.fail("legacy fallback"), raising=False)

    def semantic_reader(db, row, **kwargs):
        calls.append(kwargs)
        return {"state": "processing", "completed": 1, "total": 3}

    monkeypatch.setattr(semantic_extraction, "progress_for_message", semantic_reader, raising=False)
    cfg = {"semantic": {"mode": "shadow", "fact_source": "canonical",
                        "project_ids": [1], "fact_source_gate": "synthetic"},
           "local_llm": {"model": "synthetic-local"}}
    progress = extraction_progress.patient_progress(led.db, 1, cfg=cfg)
    assert progress["processing"] == 1 and progress["complete"] == 0
    assert calls[0]["model"] == "synthetic-local"
    assert isinstance(calls[0]["policy_fingerprint"], str)


@pytest.mark.parametrize("cfg", [None, {"semantic": "unknown"}, {"semantic": {"mode": "invalid"}}])
def test_unknown_configuration_is_attention_not_complete(led, monkeypatch, cfg):
    _patient(led)
    _msg(led, 1)
    monkeypatch.setattr(extract_llm, "progress_for_message", lambda *a, **k: pytest.fail("unbound reader"), raising=False)
    assert extraction_progress.patient_progress(led.db, 1, cfg=cfg)["attention"] == 1


@pytest.mark.parametrize("fault", ["snippet", "null", "bad_hash"])
def test_unfetched_or_partial_source_cannot_be_complete(led, monkeypatch, fault):
    _patient(led)
    _msg(led, 1)
    with led.db:
        if fault == "bad_hash":
            led.db.execute("UPDATE messages SET content_hash='unknown' WHERE message_id=1")
        else:
            led.db.execute("UPDATE messages SET body_state=? WHERE message_id=1",
                           (None if fault == "null" else "snippet",))
    monkeypatch.setattr(extract_llm, "progress_for_message", lambda *a, **k: pytest.fail("partial source"), raising=False)
    assert extraction_progress.patient_progress(led.db, 1, cfg={})["attention"] == 1


@pytest.mark.parametrize("reply", [None, {"state": "complete", "completed": 4, "total": 1},
                                  {"state": "complete", "completed": True, "total": 1}])
def test_malformed_backend_result_does_not_claim_completion(led, monkeypatch, reply):
    _patient(led)
    _msg(led, 1)
    monkeypatch.setattr(extract_llm, "progress_for_message", lambda *a, **k: reply, raising=False)
    assert extraction_progress.patient_progress(led.db, 1, cfg={})["attention"] == 1


def test_newest_twenty_scope_is_explicit_and_other_patient_deleted_posts_excluded(led, monkeypatch):
    _patient(led)
    _patient(led, 2)
    for mid in range(1, 24):
        _msg(led, mid)
    _msg(led, 100, pid=2)
    with led.db:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=23")
    seen = []

    def read(db, row, **kwargs):
        seen.append(row["message_id"])
        return {"state": "complete", "completed": 1, "total": 1}

    monkeypatch.setattr(extract_llm, "progress_for_message", read, raising=False)
    lines = extraction_progress.summary_lines(led.db, 1, cfg={})
    assert seen == list(range(22, 2, -1))
    assert "最新20投稿" in lines[0] and "完了20" in lines[0]
    assert "以前の投稿は対象外" in lines[0] and len(lines) == 1


def test_no_fetched_post_is_unconfirmed_not_a_zero_post_completion(led):
    _patient(led)
    assert extraction_progress.summary_lines(led.db, 1, cfg={}) == [
        "抽出の処理状況: 要確認（表示対象の取得済み投稿なし）"]


@pytest.mark.parametrize("change", ["none", "source_edit", "model"])
def test_real_legacy_checkpoint_reaches_private_summary_read_only(led, change):
    _patient(led)
    body = "SOURCE_CANARY 完全合成の自由文。\n" * 300
    _msg(led, 1, body=body)
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    model = "synthetic-progress-model"
    row["_target"] = ("", model)
    context = extract_llm._thread_context(led, row)
    pieces = extract_llm.plan_chunks(body, extract_llm._CHUNK_SIZE)
    assert len(pieces) > 1
    extract_llm._persist_chunks(led, row, {0: {}}, context)
    cfg = {"local_llm": {"model": model}}
    if change == "source_edit":
        with led.db:
            led.db.execute("UPDATE messages SET body_text=?,content_hash=? WHERE message_id=1",
                           (body + "完全合成の訂正。", "e" * 64))
    elif change == "model":
        cfg = {"local_llm": {"model": "synthetic-different-model"}}
    led.db.execute("PRAGMA query_only=ON")
    progress = extraction_progress.patient_progress(led.db, 1, cfg=cfg)
    assert progress["complete"] == 0 and progress["processing"] == 1
    assert progress["total"] > 1
    assert progress["completed"] == (1 if change == "none" else 0)
    text = notify_views.patient_summary_text(led.db, 1, cfg=cfg)[1]
    assert f"{progress['total']}区間中{progress['completed']}区間完了" in text
    assert "SOURCE_CANARY" not in text and "完了は抽出処理のみ" not in text
