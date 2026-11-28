"""Synthetic source quality repair: confidence decides protection, never whether a FAIL can be repaired."""
import json

import pytest

import extract_llm
import extract_llm_context
import extract_llm_qc
import extract_llm_resident
import mcs_queries
import patient_context_artifacts
import patient_context_plan
import patient_context_refresh_readiness
import patient_context_refresh_sources
import patient_context_refresh_state
import semantic_candidate
import semantic_policy
import semantic_runtime
from anthropic_context_testkit import (
    CFG, _REPAIR_BED, _REPAIR_HOME, cloud, drain, runtime, source_repair, step
)
from test_patient_context_refresh import seed

__all__ = ["runtime", "cloud", "source_repair"]

_PENDING = ("summary_quality_pending", "source_quality_pending")


def _scans(led):
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG, hydrate=False)
    return patient_context_refresh_sources._current_scans(
        led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)


def _held(led):
    return [scan for scan in _scans(led).values() if scan.get('quality_state') == 'held']


def _coverage_requests(led):
    return sum(request['state']['target']['id'] == 'source_coverage' for request in led.synthetic_jev_requests)


def _run_until(runtime, done, steps=90):
    """Step the real lane, moving the clock only past waits, until ``done(result)`` holds."""
    _, clock, _ = runtime
    for _ in range(steps):
        result = step(runtime)
        if done(result):
            return
        if result['status'] != 'progressed' and result.get('reason') not in _PENDING:
            clock.wall += 61
    pytest.fail('synthetic context did not reach the expected state')


def _until_fail(runtime):
    led, _, _ = runtime
    _run_until(runtime, lambda _: bool(led.artifacts('patient_context_source_quality_fail_v1', project_id=1)), steps=20)
    rows = led.db.execute('SELECT content FROM artifacts WHERE kind=? ORDER BY artifact_id',
                          (patient_context_refresh_state.CHUNK,)).fetchall()
    return {fact['id']: fact for (content,) in rows for fact in json.loads(content)['facts']}


def _repair_origin(led):
    [header] = led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)
    return json.loads(header['meta'])


def _published_facts(led):
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    scan = patient_context_refresh_sources._current_scans(
        led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)[1]
    assert scan['accepted_candidate_ref'] and scan['audited']
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    return list(patient_context_refresh_sources._scan_facts(led.db, 1, snapshot['sources'][0], snapshot['route'], scan))


def test_low_confidence_missing_answer_repairs_once_and_keeps_the_confident_supported_fact(runtime, source_repair):
    led, _, _ = runtime
    source_repair['qa_fault'] = 'low_confidence'  # the coverage answer is below the threshold
    supported = _until_fail(runtime)
    assert {fact['text'] for fact in supported.values()} == {_REPAIR_HOME}
    coverage = _coverage_requests(led)
    drain(runtime)
    # The low-confidence missing answer names the finding instead of holding the source.
    assert len(source_repair['sent']) == 2
    assert _repair_origin(led)['protected_sig'] == patient_context_refresh_state._facts_sig(list(supported.values()))
    facts = _published_facts(led)
    assert {fact['text'] for fact in facts} == {_REPAIR_HOME, _REPAIR_BED}
    for fid, fact in supported.items():
        stored = {key: value for key, value in next(value for value in facts if value['id'] == fid).items()
                  if key != 'temporal'}
        assert patient_context_refresh_state._facts_sig([stored]) == patient_context_refresh_state._facts_sig([fact])
    # The repaired candidate was audited again before it was taken up.
    assert _coverage_requests(led) > coverage


def test_low_confidence_supported_fact_is_not_protected_and_does_not_block_the_repair(runtime, source_repair):
    led, _, _ = runtime
    source_repair['qa_fault'] = 'low_confidence_support'  # confident missing, unconfident supports
    supported = _until_fail(runtime)
    assert {fact['text'] for fact in supported.values()} == {_REPAIR_HOME}
    drain(runtime)
    assert len(source_repair['sent']) == 2
    assert _repair_origin(led)['protected_sig'] == patient_context_refresh_state._facts_sig([])
    assert {fact['text'] for fact in _published_facts(led)} == {_REPAIR_HOME, _REPAIR_BED}


def test_the_repair_rule_version_releases_holds_without_moving_the_quality_code(runtime, cloud, monkeypatch):
    led, _, _ = runtime
    seed(led, body='合成の居住情報です。')
    source = list(mcs_queries.patient_context_sources(led.db, 1, mid=1))[0]
    code = patient_context_refresh_state._quality_assessment_code()
    before = patient_context_refresh_state._source_quality_hold_key(led.db, 1, CFG, source, 'a' * 64)
    monkeypatch.setattr(patient_context_artifacts, 'SOURCE_REPAIR_RULE', patient_context_artifacts.SOURCE_REPAIR_RULE + 1)
    assert patient_context_refresh_state._source_quality_hold_key(led.db, 1, CFG, source, 'a' * 64) != before
    assert patient_context_refresh_state._quality_assessment_code() == code


def _v131_repair_inputs(db, pid, captured, threshold):
    """The v1.3.1 rule, for building a stored hold only: any answer below the threshold held the source."""
    identity = captured['identity']
    for offset in range(0, len(identity['fact_ids']), 12):
        ids = identity['fact_ids'][offset:offset + 12]
        answers = semantic_runtime.patient_source_question_evidence(db, pid, identity, ids)['answers']
        if any(answer['confidence'] < threshold for answer in answers.values()):
            raise ValueError('patient_context_source_repair_unresolved')
    return patient_context_artifacts.source_repair_inputs(db, pid, captured, threshold)


def _settled(led, source_repair):
    return (len(led.synthetic_jev_requests), len(source_repair['sent']),
            len(led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)),
            len(led.artifacts('cloud_usage_reserve', project_id=1)))


@pytest.mark.parametrize('qa_fault,fault', [
    ('low_confidence', None),  # now repairable: proceeds to the one repair
    ('ambiguous', None),       # still names no finding: held again
    (None, 'omit_bed'),        # repaired to the same candidate: held again
])
def test_a_hold_from_the_previous_rule_is_re_evaluated_exactly_once(runtime, source_repair, monkeypatch, qa_fault, fault):
    led, clock, _ = runtime
    source_repair.update(qa_fault=qa_fault, fault=fault)
    with monkeypatch.context() as previous_release:
        previous_release.setattr(patient_context_artifacts, 'SOURCE_REPAIR_RULE', 1)
        with monkeypatch.context() as previous_rule:
            previous_rule.setattr(patient_context_refresh_sources, '_source_repair_inputs', _v131_repair_inputs)
            _run_until(runtime, lambda _: bool(_held(led)))
        [held] = _held(led)
        stored = _settled(led, source_repair)
        assert len(source_repair['sent']) == (2 if fault == 'omit_bed' else 0)
        # Under the old hold key the stored hold stays: nothing is asked or sent again.
        for _ in range(4):
            clock.wall += 61
            step(runtime)
        assert [scan['quality_hold_key'] for scan in _held(led)] == [held['quality_hold_key']]
        assert _settled(led, source_repair) == stored
    if qa_fault == 'low_confidence':
        _run_until(runtime, lambda result: result['status'] == 'complete', steps=40)
        assert len(source_repair['sent']) == 2
        assert len(led.artifacts(patient_context_refresh_state.SOURCE_REPAIR, project_id=1)) == 1
        assert {fact['text'] for fact in _published_facts(led)} == {_REPAIR_HOME, _REPAIR_BED}
    else:
        _run_until(runtime, lambda _: [scan['quality_hold_key'] for scan in _held(led)]
                   not in ([], [held['quality_hold_key']]), steps=40)
        # Re-evaluated from the stored answers and the spent repair: nothing is asked, reserved or sent.
        assert _settled(led, source_repair) == stored
        assert not patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    settled = _settled(led, source_repair)
    for _ in range(4):
        clock.wall += 61
        step(runtime)
    assert _settled(led, source_repair) == settled


_PASS_FILLER, _HELD_FILLER = '合成の申し送りです。', '合成の連絡事項です。'


def _source_pages(requests, filler):
    """Source quality requests about the one source whose text holds ``filler``."""
    return [body for body in requests if body['state']['target']['id'] in ('source_coverage', 'source_facts')
            and filler in json.dumps(body['state']['context'], ensure_ascii=False)]


def _source_texts(led, key):
    snapshot = patient_context_refresh_sources._snapshot(led.db, 1, CFG)
    [source] = [value for value in snapshot['sources'] if patient_context_plan.source_key(value) == key]
    scan = patient_context_refresh_sources._current_scans(
        led.db, 1, snapshot['sources'], snapshot['route'], cfg=CFG)[key]
    assert scan['accepted_candidate_ref'] and scan['audited']
    return {fact['text'] for fact in patient_context_refresh_sources._scan_facts(
        led.db, 1, source, snapshot['route'], scan)}


def _passed(scan):
    return {key: scan.get(key) for key in ('audited', 'accepted_candidate_ref', 'source_quality_audit_key')}


def test_an_upgrade_re_evaluates_only_the_held_source_and_keeps_the_stored_pass(runtime, source_repair, monkeypatch):
    """Legacy fact source: the edited files' digests move, a stored PASS stays and one held source repairs once."""
    led, clock, _ = runtime
    assert CFG['semantic'].get('fact_source', 'legacy') == 'legacy'
    # Message 1 (fixture) is answered surely; message 2 gets the low-confidence missing answer.
    seed(led, mid=2, body=_REPAIR_HOME + _HELD_FILLER * 310 + _REPAIR_BED)
    source_repair.update(qa_fault='low_confidence', qa_fault_marker=_HELD_FILLER)
    scfg = semantic_policy.semantic_config(CFG, key_present=True)[0]
    fingerprint = semantic_candidate.candidate_fingerprint({})
    assessment = patient_context_refresh_state._quality_assessment(scfg)
    with monkeypatch.context() as previous_release:
        # Setup only: the v1.3.1 rule and file digests build the stored PASS and hold.
        previous_release.setattr(patient_context_artifacts, 'SOURCE_REPAIR_RULE', 1)
        previous_release.setattr(patient_context_refresh_sources, '_source_repair_inputs', _v131_repair_inputs)
        old = {**extract_llm._LOADED_SOURCE_DIGESTS,
               'patient_context_artifacts': '0' * 64, 'patient_context_refresh_state': '1' * 64}
        for module in (extract_llm, extract_llm_context, extract_llm_qc, extract_llm_resident):
            previous_release.setattr(module, '_LOADED_SOURCE_DIGESTS', old)
        assert semantic_candidate.candidate_fingerprint({}) != fingerprint
        # Under the legacy fact source the PASS binding ignores the candidate fingerprint.
        assert patient_context_refresh_state._quality_assessment(scfg) == assessment
        _run_until(runtime, lambda _: _scans(led).get(1, {}).get('audited') and [
            key for key, scan in _scans(led).items() if scan.get('quality_state') == 'held'] == [2], steps=120)
        for _ in range(4):
            clock.wall += 61
            step(runtime)
        stored = _settled(led, source_repair)
        # Only the surely answered source spent its one repair; the held one sent nothing.
        assert stored[1] == 2 and stored[2] == 1
        passed, [held] = _passed(_scans(led)[1]), _held(led)
        requests = len(led.synthetic_jev_requests)
    assert semantic_candidate.candidate_fingerprint({}) == fingerprint
    _run_until(runtime, lambda result: result['status'] == 'complete', steps=60)
    after, new = _settled(led, source_repair), led.synthetic_jev_requests[requests:]
    # The stored PASS is neither asked again nor re-verified into a new audit.
    assert not _source_pages(new, _PASS_FILLER)
    assert _passed(_scans(led)[1]) == passed
    # The one held source is re-evaluated once: one repair of the same finite plan and cost.
    assert after[1] - stored[1] == stored[1] and after[2] == 2 and 0 < after[3] - stored[3] <= stored[3]
    assert not _held(led) and held['quality_hold_key']
    # Stored answers about the original candidate are reused; only the repaired candidate is audited.
    coverage = [body for body in _source_pages(new, _HELD_FILLER) if body['state']['target']['id'] == 'source_coverage']
    assert coverage and all({_REPAIR_HOME, _REPAIR_BED} <= {fact['text'] for fact in body['state']['target']['candidate_facts']}
                            for body in coverage)
    assert _source_texts(led, 1) == _source_texts(led, 2) == {_REPAIR_HOME, _REPAIR_BED}
    assert patient_context_refresh_readiness.context_view_input(led.db, 1, cfg=CFG)
    for _ in range(4):
        clock.wall += 61
        step(runtime)
    assert _settled(led, source_repair) == after
