"""Missing runtime measurements stay unknown; overflow cannot poison valid JSON."""
import json
import sys

import pytest

import extract_llm


def test_partial_backend_metrics_do_not_invent_missing_zeros():
    summary = extract_llm._integrity_summary([{
        "finish_reason": "stop", "usage": {"prompt_tokens": 11},
        "timings": {"prompt_ms": 3.0}}])
    assert summary["usage"] == {"prompt_tokens": 11}
    assert summary["timings"] == {"prompt_ms": 3.0}
    assert extract_llm._llm_call_stats([summary]) == {
        "calls": 1, "prompt_ms": 3.0, "predicted_ms": None, "tokens": None}


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
    assert stats == {"calls": 2, "prompt_ms": None, "predicted_ms": 5.0, "tokens": None}
    json.dumps(stats, allow_nan=False)
