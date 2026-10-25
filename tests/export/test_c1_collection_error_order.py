"""validate_collection checks every envelope before current_set evidence, so a bad
second part reports its own error first (synthetic envelopes only)."""
import pytest

import c1_envelopes as envs
from c1_contract import C1ContractError
from test_c1_envelopes import _build, _split_inputs


def _two_parts():
    records, _, wide = _split_inputs()
    parts = _build(records, max_bytes=wide)
    assert len(parts) == 2
    return parts


def test_a_bad_second_envelope_is_reported_before_a_current_set_conflict():
    parts = _two_parts()
    evidence = {**envs.validate_collection(parts)["evidence"], "set": "0" * 64}   # conflicting set
    with pytest.raises(C1ContractError, match="generation_set_conflict"):
        envs.validate_collection(parts, current_set=evidence)
    parts[1]["record_count"] += 1                                   # second part's integrity breaks
    with pytest.raises(C1ContractError, match="envelope_integrity_invalid"):
        envs.validate_collection(parts, current_set=evidence)


def test_reused_groups_keep_the_collection_result():
    parts = _two_parts()
    complete = envs.validate_collection(parts)
    assert complete["status"] == "complete"
    assert envs.validate_collection(list(reversed(parts))) == complete
    assert envs.validate_collection(parts[:1]) == {
        "status": "incomplete", "received_parts": 1, "expected_parts": 2, "evidence": None}
