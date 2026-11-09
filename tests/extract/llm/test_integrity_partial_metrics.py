"""Missing runtime measurements stay unknown; overflow cannot poison valid JSON."""
import json
import sys

import pytest

import extract_llm


def test_recovered_answer_does_not_count_the_original_http_or_tokens_again():
    extract_llm._note_list().clear()
    extract_llm._integrity_note({"status": "ok", "finish_reason": "stop",
        "usage": {"total_tokens": 12}, "reused_response": True}, "repair")
    summary = extract_llm._integrity_summary(extract_llm._note_list())
    assert summary["calls"] == summary["first_calls"] == summary["repair_calls"] == 0
    assert summary["reused_responses"] == 1 and summary["usage"] is None
    extract_llm._integrity_note({"status": "ok", "usage": {"total_tokens": 3}})
    mixed = extract_llm._integrity_summary(extract_llm._note_list())
    assert mixed["calls"] == mixed["first_calls"] == 1
    assert mixed["reused_responses"] == 1 and mixed["usage"] == {"total_tokens": 3}
    extract_llm._note_list().clear()


def test_partial_backend_metrics_do_not_invent_missing_zeros():
    summary = extract_llm._integrity_summary([{
        "finish_reason": "stop", "usage": {"prompt_tokens": 11},
        "timings": {"prompt_ms": 3.0}}])
    assert summary["usage"] == {"prompt_tokens": 11}
    assert summary["timings"] == {"prompt_ms": 3.0}
    assert extract_llm._llm_call_stats([summary]) == {
        "calls": 1, "prompt_ms": 3.0, "predicted_ms": None, "tokens": None,
        "first_calls": 1, "repair_calls": 0, "repair_wins": None,
        "length_stops": 0, "length_resplits": None, "probe_runs": 0,
        "format_downgrades": 0, "checkpoint_reuses": None,
        "first_pass_evaluated_chunks": None, "first_pass_accepted_chunks": None,
        "prompt_n": None, "cache_n": None, "repair_prompt_n": None,
        "repair_cache_n": None, "repair_issues": None, "cache_hit_ratio": None}


@pytest.mark.parametrize("key", ["prompt_ms", "predicted_ms"])
def test_statistics_only_marks_the_measured_timing_known(key):
    stats = extract_llm._llm_call_stats([{"calls": 1, "timings": {key: 5.0}}])
    assert stats[key] == 5.0
    other = "predicted_ms" if key == "prompt_ms" else "prompt_ms"
    assert stats[other] is None


def test_integrity_timing_overflow_is_unknown_and_json_stays_valid():
    notes = [{"timings": {"prompt_ms": sys.float_info.max}}] * 2
    summary = extract_llm._integrity_summary(notes)
    assert summary["calls"] == 2
    assert summary["timings"] is None
    json.dumps(summary, allow_nan=False)


def test_statistics_timing_overflow_is_unknown_without_losing_other_fields():
    stats = extract_llm._llm_call_stats([
        {"calls": 1, "timings": {"prompt_ms": sys.float_info.max, "predicted_ms": 2.0}},
        {"calls": 1, "timings": {"prompt_ms": sys.float_info.max, "predicted_ms": 3.0}}])
    assert stats == {
        "calls": 2, "prompt_ms": None, "predicted_ms": 5.0, "tokens": None,
        "first_calls": None, "repair_calls": None, "repair_wins": None,
        "length_stops": None, "length_resplits": None, "probe_runs": None,
        "format_downgrades": None, "checkpoint_reuses": None,
        "first_pass_evaluated_chunks": None, "first_pass_accepted_chunks": None,
        "prompt_n": None, "cache_n": None, "repair_prompt_n": None,
        "repair_cache_n": None, "repair_issues": None, "cache_hit_ratio": None}
    json.dumps(stats, allow_nan=False)
