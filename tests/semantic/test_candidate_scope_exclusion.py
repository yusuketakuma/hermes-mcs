"""The candidate fingerprint keeps today's identity until the owner approves narrowing it (O-11).

``candidate_fingerprint`` still hashes the whole ``extract`` block, so writing
``upgrade_scope`` or a patient-context switch (either spelling) drops an
approved canonical promotion, exactly as on main. The narrowed identity is an
unwired function: it leaves those selection-only keys out and keeps every
interpretation key. Synthetic configuration only."""
import pytest

import extract_llm
import mcs_config
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
INTERPRETATION = [{"patient_state_context": True}, {"prefilter": False}]
MISMATCH = "config: semantic_fact_source_evidence_mismatch"


def test_only_the_three_selection_keys_are_named():
    assert candidate.SELECTION_ONLY_EXTRACT == ("upgrade_scope", "patient_context", "patient_context_nightly")


@pytest.mark.parametrize("extract", SELECTION_ONLY + INTERPRETATION, ids=repr)
def test_the_active_fingerprint_still_moves_with_every_extract_key(extract):
    assert candidate.candidate_fingerprint({"extract": extract}) != candidate.candidate_fingerprint({})


@pytest.mark.parametrize("extract", SELECTION_ONLY, ids=repr)
def test_the_narrowed_fingerprint_ignores_selection_only_settings(extract):
    base = candidate.narrowed_candidate_fingerprint({})
    assert candidate.narrowed_candidate_fingerprint({"extract": extract}) == base
    with_state = {"extract": {"patient_state_context": True}}
    assert candidate.narrowed_candidate_fingerprint({"extract": {**with_state["extract"], **extract}}) == (
        candidate.narrowed_candidate_fingerprint(with_state))


@pytest.mark.parametrize("cfg", [{}, {"extract": {}}, *({"extract": e} for e in INTERPRETATION)], ids=repr)
def test_without_selection_keys_both_identities_agree(cfg):
    # Switching the narrowing on changes nothing for a configuration that never set them.
    assert candidate.narrowed_candidate_fingerprint(cfg) == candidate.candidate_fingerprint(cfg)


@pytest.mark.parametrize("extract", INTERPRETATION, ids=repr)
def test_interpretation_settings_move_the_narrowed_fingerprint(extract):
    assert candidate.narrowed_candidate_fingerprint({"extract": extract}) != (
        candidate.narrowed_candidate_fingerprint({}))


@pytest.mark.parametrize("extract", SELECTION_ONLY, ids=repr)
def test_until_o11_writing_a_selection_key_drops_the_canonical_approval(extract, monkeypatch):
    """The consequence the owner decides O-11 on: the approval no longer matches,
    the fact source falls back to legacy with semantic mode off, legacy extraction
    admits nothing, and the config check reports the mismatch."""
    cfg = approved_config({"semantic": {"fact_source": "canonical"}})
    policy, errors = semantic_config(cfg)
    assert policy["fact_source"] == "canonical" and not errors
    changed = {**cfg, "extract": extract}
    policy, errors = semantic_config(changed)
    assert errors == [MISMATCH] and policy["fact_source"] == "legacy" and policy["mode"] == "off"
    monkeypatch.setattr(extract_llm.semantic_policy, "runtime_key_present", lambda cfg, root: False)
    assert extract_llm.legacy_admissions(None, changed) == set()
    assert f"semantic: 意味解析の設定を確認してください（詳細: {MISMATCH}）" in mcs_config.validate_config(
        {"mcs_login_id": "synthetic", "notify_target": "slack:#synthetic", **changed})[0]


def test_switching_the_narrowing_on_keeps_the_approval(monkeypatch):
    # The one-line switch X9 would make if the owner approves O-11.
    monkeypatch.setattr(candidate, "candidate_fingerprint", candidate.narrowed_candidate_fingerprint)
    cfg = approved_config({"semantic": {"fact_source": "canonical"}})
    assert semantic_config(cfg)[0]["fact_source"] == "canonical" and not semantic_config(cfg)[1]
    for extract in SELECTION_ONLY:
        changed = {**cfg, "extract": extract}
        assert semantic_config(changed)[0]["fact_source"] == "canonical", extract
    for extract in INTERPRETATION:
        changed = {**cfg, "extract": extract}
        assert semantic_config(changed)[0]["fact_source"] == "legacy", extract
