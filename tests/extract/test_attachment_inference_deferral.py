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
