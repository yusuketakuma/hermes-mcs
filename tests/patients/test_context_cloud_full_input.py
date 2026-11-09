"""Cloud context must inspect full originals after earlier local progress."""
import pytest

import extract_llm
import extract_llm_prepare
import cloud_inference
import patient_context_refresh_state as state
from anthropic_context_testkit import cloud, drain, runtime, step
from test_patient_context_refresh import seed

__all__ = ["cloud", "runtime"]


@pytest.mark.parametrize("finish_local", [False, True])
def test_cloud_reads_full_original_after_local_checkpoint(runtime, cloud, monkeypatch, finish_local):
    ledger, clock, local_calls = runtime
    body = "完全合成の居住記録。" * 170 + "\n\n" + "完全合成の通院記録。" * 170
    seed(ledger, body=body)
    configured = cloud_inference.resolve_cloud_settings({})
    with monkeypatch.context() as patch:
        patch.setattr(cloud_inference, "resolve_cloud_settings", lambda cfg: {**configured, "mode": "off"})
        patch.setattr(extract_llm, "_cloud_mode", lambda *args: "off")
        patch.setattr(extract_llm_prepare, "_cloud_mode", extract_llm._cloud_mode)
        if finish_local:
            drain(runtime)
        else:
            assert step(runtime)["status"] == "progressed"
    # Explicit off -> on changes the overview input; keep its three-hour interval.
    if finish_local:
        clock.wall += 3 * 3600 + 1
    assert local_calls and not cloud[0]
    originals = [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id", (state.CHUNK,))]
    assert originals
    drain(runtime)
    chunks = [prompt["source"] for prompt in cloud[0] if "source" in prompt]
    assert "".join(chunks) == body, "local cursor is not cloud input coverage"
    for old in originals:
        assert tuple(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (old[0],)).fetchone()) == old
    count = len(cloud[0])
    assert step(runtime)["status"] == "complete"
    assert len(cloud[0]) == count


def test_supplemental_summary_is_fully_chunked_and_separate_from_original(runtime):
    import json
    import mcs_queries
    import patient_context_plan as plan
    import patient_context_scan as scan

    ledger, clock, _ = runtime
    seed(ledger, body="完全合成の原文です。")
    source = next(mcs_queries.patient_context_sources(ledger.db, 1))
    text = ('合成の補助要約"\\\u0001' * 1500)
    source["derived_summaries"] = [{"kind": "extract_llm", "artifact_id": 123, "text": text}]
    source["derived_summary_sig"] = plan.source_digest(text)
    calls = []

    def answer(payload):
        assert len(json.dumps(payload, ensure_ascii=False)) <= plan.MAX_PROMPT_CHARS
        assert payload["source"] == source["body_text"]
        assert payload["source_identity"]["signature"] == plan.source_signature(source)
        calls.append(payload)
        return {"patient_context": []}

    result = scan.scan_source(source, {}, "synthetic", answer_for=answer, clock=lambda: clock.wall)
    assert result["complete"] and not result["facts"]
    assert len(calls) > 1
    pages = [page for call in calls for page in call["derived_summaries"]]
    assert "".join(page["text"] for page in pages) == text
    assert [page["span"][0] for page in pages] == [
        sum(len(prior["text"]) for prior in pages[:index]) for index in range(len(pages))]


def test_supplemental_summary_cannot_supply_an_original_quote(runtime):
    import mcs_queries
    import patient_context_scan as scan

    ledger, clock, _ = runtime
    seed(ledger, body="完全合成の原文です。")
    source = next(mcs_queries.patient_context_sources(ledger.db, 1))
    derived = "原文にない合成の指示です。"
    source["derived_summaries"] = [{"kind": "extract_llm", "artifact_id": 123, "text": derived}]
    def answer(payload):
        return {"patient_context": [{"category": "living", "text": derived,
            "evidence": derived, "subject": "unspecified", "details": []}]}
    with pytest.raises(ValueError, match="nightly_unsupported_items"):
        scan.scan_source(source, {}, "synthetic", answer_for=answer, clock=lambda: clock.wall)
