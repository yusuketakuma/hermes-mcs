"""Synthetic independent invariants for context scan planning and source validation."""
import copy
import pytest
import patient_context_plan as plan


def test_scan_cursor_and_complete_quoted_unit_do_not_mutate_inputs():
    item = {"category": "living", "text": "家族は転居を希望していません。",
            "evidence": "家族は転居を希望していません。", "subject": "family"}
    source = {"body_text": item["text"], "items": [item]}
    saved = {"cursor": True, "facts": [{"id": "obsolete"}]}
    original = copy.deepcopy((source, saved))
    chunks, cursor, reusable, items = plan.scan_plan(source, saved)
    assert chunks == [item["text"]] and cursor == 0 and reusable == {}
    assert items == [item]  # A complete quote does not prove complete extraction.
    assert plan.scan_items({"patient_context": [item]}, chunks[0]) == [item]
    with pytest.raises(ValueError, match="patient_context_unsupported_items"):
        plan.scan_items({"patient_context": [{**item, "text": "家族は転居を希望しています。"}]}, chunks[0])
    with pytest.raises(ValueError, match="patient_context_invalid_items"):
        plan.scan_items({"patient_context": [{**item, "status": "current"}]}, chunks[0])
    assert (source, saved) == original


def test_split_scan_resumes_where_the_stopped_chunk_began_and_is_bounded():
    body = "本日訪問しました。合成の記録として、ご本人は落ち着いて過ごされていました。" * 60
    source = {"body_text": body, "items": []}
    coarse = plan.plan_chunks(body, plan.SCAN_CHUNK)
    finer = plan.split_scan(source, {}, 0)
    assert finer == {"cursor": 0, "chunks": len(plan.plan_chunks(body, plan.SCAN_CHUNK >> 1)), "split": 1}
    chunks, cursor, _, _ = plan.scan_plan(source, finer)
    assert "".join(chunks) == body and cursor == 0 and len(chunks) > len(coarse)
    second = plan.split_scan(source, finer, 1)      # stopped at the second finer chunk
    finest = plan.plan_chunks(body, plan.SCAN_CHUNK >> second["split"])
    starts = [sum(map(len, finest[:i])) for i in range(len(finest))] + [len(body)]
    offset = len(chunks[0])
    assert second["split"] > finer["split"] and starts[second["cursor"]] <= offset < starts[second["cursor"] + 1]
    assert len(finest[second["cursor"]]) < len(chunks[1])   # a level that actually shrinks it
    assert plan.split_scan(source, {"split": plan.MAX_SCAN_SPLIT}, 0) is None
    short = {"body_text": "合成の短い記録。", "items": []}
    assert plan.split_scan(short, {}, 0) is None     # nothing to split: an ordinary failure
    assert plan.scan_plan(source, {"split": 99, "cursor": 3})[1] == 0   # invalid state restarts


def test_rejected_items_name_index_and_reason_only():
    chunk = "生活環境: 合成の居住記録。"
    answer = {"patient_context": [
        {"category": "living", "text": chunk, "evidence": chunk, "subject": "unspecified"},
        {"category": "living", "text": "合成の別文", "evidence": "合成の別文", "subject": "unspecified"},
        {"category": "unknown", "text": chunk, "evidence": chunk, "subject": "unspecified"}]}
    assert plan.rejected_items(answer, chunk) == [
        {"index": 1, "reason": "not_verbatim_or_invalid"}, {"index": 2, "reason": "not_verbatim_or_invalid"}]


def test_an_invalid_cursor_restarts_the_plan_and_its_split_together():
    body = "本日訪問しました。合成の記録として、ご本人は落ち着いて過ごされていました。" * 160
    source = {"body_text": body, "items": []}
    for cursor in (True, -1, 999):
        chunks, at, saved, _ = plan.scan_plan(source, {"split": 1, "cursor": cursor})
        assert at == 0 and saved == {} and chunks == plan.plan_chunks(body, plan.SCAN_CHUNK)


def test_grounded_items_keep_only_what_passes_on_its_own():
    chunk = "生活環境: 合成の居住記録。"
    good = {"category": "living", "text": chunk, "evidence": chunk, "subject": "unspecified"}
    answer = {"patient_context": [good, {"category": "living", "text": "合成の別文",
                                         "evidence": "合成の別文", "subject": "unspecified"}]}
    assert plan.grounded_items(answer, chunk) == plan.context_items({"patient_context": [good]}, chunk)
