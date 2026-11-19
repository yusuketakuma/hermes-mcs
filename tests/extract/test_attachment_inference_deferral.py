"""Synthetic checks that the local page deferral vocabulary stays fixed-code and count-only."""
import pytest

import attachment_inference_deferral as deferral


@pytest.mark.parametrize("reasons,total,expected", [
    ({}, 1, {"unclassified": 1}),
    (None, 2, {"unclassified": 2}),
    ({"disk_low": 1}, 1, {"disk_low": 1}),
    ({"disk_low": 1}, 3, {"disk_low": 1, "unclassified": 2}),
    ({"disk_low": 4}, 3, {"unclassified": 3}),
    ({"synthetic_free_text": 1}, 1, {"unclassified": 1}),
    ({"disk_low": True, "memory_low": 0, "memory_unknown": -1, "inference_lock_busy": "1"}, 1,
     {"unclassified": 1}),
    ({"unclassified": 5, "disk_low": 1}, 1, {"disk_low": 1}),
    ({"disk_low": 1}, 0, {}),
])
def test_normalize_keeps_known_positive_counts_and_assigns_the_rest(reasons, total, expected):
    normalized = deferral.normalize(reasons, total)
    assert normalized == expected
    if total > 0:
        assert sum(normalized.values()) == total and deferral.valid(normalized, total)


@pytest.mark.parametrize("reasons,expected", [
    ({"disk_low": 2, "unclassified": 1}, "disk_low"),
    ({"disk_low": 1, "unclassified": 1}, "unclassified"),
    ({"unclassified": 3}, "unclassified"),
    ({"memory_low": 1, "disk_low": 1}, "disk_low"),
    ({"memory_low": 2, "disk_low": 1}, "memory_low"),
    (None, "unclassified"),
])
def test_primary_names_a_known_reason_only_when_it_outnumbers_unclassified(reasons, expected):
    assert deferral.primary(reasons) == expected


def test_reason_helpers_never_infer_beyond_the_measurement():
    assert deferral.disk_reason(None, 100) == "disk_unknown"
    assert deferral.disk_reason(99.0, 100) == "disk_low"
    assert deferral.disk_reason(100.0, 100) is None
    assert deferral.memory_reason(None) == "memory_unknown"
    assert deferral.memory_reason(-1) == "memory_low"
    assert deferral.memory_reason(0) is None


@pytest.mark.parametrize("reasons,total", [
    ({"disk_low": 1}, 2), ({"disk_low": True}, 1), ({"disk_low": 0}, 0), ({"synthetic": 1}, 1),
    ({}, 0), ("disk_low", 1), ({"disk_low": 1}, None),
])
def test_valid_rejects_anything_but_an_exact_breakdown(reasons, total):
    assert not deferral.valid(reasons, total)


@pytest.mark.parametrize("raw,code", [
    ("deadline", "llm_deadline"), ("busy", "llm_busy"), ("admission", "llm_admission"),
    ("probe_unobserved", "llm_probe_unobserved"), ("endpoint_down", "llm_endpoint_down"),
    ("checkpoint_unverified", "llm_checkpoint_unverified"),
    ("circuit_open", "llm_circuit_open"), ("currentness_unverified", "llm_currentness_unverified"),
    ("not_sent", "llm_not_sent"), ("quality_checkpoint_unverified", "llm_quality_checkpoint_unverified"),
    ("repair_unavailable", "llm_repair_unavailable"),
    ("local_rebuild_proof_changed", "llm_local_rebuild_proof_changed"),
    ("local_rebuild_unverified", "llm_local_rebuild_unverified"),
])
def test_llm_reason_maps_only_the_fixed_local_vocabulary(raw, code):
    assert deferral.llm_reason(raw) == code
    assert code in deferral._LLM_STATUS_CODES


@pytest.mark.parametrize("raw", [
    None, "", "synthetic_free_text", "cloud_fallback", "cloud_deadline",
    "cloud_unit_unresolved", "cloud_owner_transaction", "cloud_guard",
    "writer_refused", "deadline ", " Deadline", 7, {"x": 1}, ["deadline"],
])
def test_llm_reason_rejects_everything_else(raw):
    assert deferral.llm_reason(raw) is None


def test_observed_keeps_only_fixed_codes_with_positive_counts():
    assert deferral.observed({"llm_deadline": 2, "synthetic": 1, "llm_busy": 0,
                              "llm_admission": True, "disk_low": 1}) == {"llm_deadline": 2}
    assert deferral.observed(None) == {} and deferral.observed("llm_deadline") == {}


@pytest.mark.parametrize("reasons,total,expected", [
    ({"llm_deadline": 1}, 1, True),
    ({"llm_deadline": 1, "llm_busy": 1}, 3, True),
    ({"llm_deadline": 2}, 1, False),       # observations never exceed the deferrals
    ({"llm_deadline": 1}, 0, False),
    ({"disk_low": 1}, 1, False),           # cause codes are not observations
    ({"llm_deadline": True}, 1, False),
    ({"llm_deadline": 0}, 1, False),
    ({}, 1, False), (None, 1, False),
])
def test_observed_valid_bounds_the_breakdown_by_the_deferral_count(reasons, total, expected):
    assert deferral.observed_valid(reasons, total) is expected
