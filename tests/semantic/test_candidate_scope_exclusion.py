"""Selection-only extract settings leave the approved candidate identity unchanged.

``upgrade_scope`` and the patient-context switches (either spelling) decide
which work runs, never how a fact is interpreted; ``patient_state_context`` is
interpretation and still moves the fingerprint. Synthetic configuration only."""
import pytest

import semantic_candidate as candidate
from candidate_testkit import approved_config
from semantic_policy import semantic_config

SELECTION_ONLY = [
    {"upgrade_scope": "none"}, {"upgrade_scope": {"project_ids": [3]}}, {"upgrade_scope": "all"},
    {"upgrade_scope": "invalid"}, {"patient_context": {"enabled": False}},
    {"patient_context": {"enabled": True, "publication": False}},
    {"patient_context_nightly": {"enabled": False}},
    {"upgrade_scope": "none", "patient_context": {"enabled": False}},
]


@pytest.mark.parametrize("extract", SELECTION_ONLY, ids=repr)
def test_selection_only_settings_keep_the_fingerprint(extract):
    base = candidate.candidate_fingerprint({})
    assert candidate.candidate_fingerprint({"extract": extract}) == base
    with_state = {"extract": {"patient_state_context": True}}
    assert candidate.candidate_fingerprint({"extract": {**with_state["extract"], **extract}}) == (
        candidate.candidate_fingerprint(with_state))


@pytest.mark.parametrize("extract", [{"patient_state_context": True}, {"prefilter": False}], ids=repr)
def test_interpretation_settings_still_move_the_fingerprint(extract):
    assert candidate.candidate_fingerprint({"extract": extract}) != candidate.candidate_fingerprint({})


def test_an_approved_canonical_source_survives_closing_the_scope():
    cfg = approved_config({"semantic": {"fact_source": "canonical"}})
    assert semantic_config(cfg)[0]["fact_source"] == "canonical" and not semantic_config(cfg)[1]
    for extract in SELECTION_ONLY:
        changed = {**cfg, "extract": extract}
        assert semantic_config(changed)[0]["fact_source"] == "canonical", extract
    changed = {**cfg, "extract": {"patient_state_context": True}}
    assert semantic_config(changed)[0]["fact_source"] == "legacy"
