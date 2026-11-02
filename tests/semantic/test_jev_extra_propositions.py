"""Optional proposition groups extend the assessed set per installation."""
import time

import pytest

import semantic_drain
import semantic_jev as jev
import semantic_policy
import semantic_store
from semantic_testkit import _cfg, _FakeJev
from test_patient_context_refresh import runtime as runtime
from test_patient_context_refresh import _raw_proof, seed


def test_extra_propositions_config_validates_ids():
    scfg, errors = semantic_policy.runtime_config(
        _cfg('assist', extra_propositions=['E01', 'E06']))
    assert not errors
    assert scfg['extra_propositions'] == ['E01', 'E06']
    for bad in ('E01', [1], ['E99'], ['E01', 'E01'], ['P01']):
        _scfg, errors = semantic_policy.runtime_config(
            _cfg('assist', extra_propositions=bad))
        assert 'config: semantic_extra_propositions_invalid' in errors


def test_active_propositions_defaults_to_required_set():
    assert set(jev.active_propositions({})) == set(jev.PROPOSITIONS)
    assert set(jev.active_propositions(None)) == set(jev.PROPOSITIONS)
    active = jev.active_propositions({'extra_propositions': ['E02', 'E06']})
    assert set(active) == set(jev.PROPOSITIONS) | {'E02', 'E06'}
    # an unknown id can never widen the evaluated set
    assert set(jev.active_propositions({'extra_propositions': ['ZZ']})) \
        == set(jev.PROPOSITIONS)


def test_partial_assessment_scope_follows_the_active_set():
    off = semantic_policy.runtime_config(_cfg('assist'))[0]
    on = semantic_policy.runtime_config(
        _cfg('assist', extra_propositions=['E06']))[0]
    partial = {'E06': {'noul': 0.8}}
    assert semantic_drain._partial_assessment(partial, on)['E06']['verdict'] \
        == 'MATCH'
    with pytest.raises(ValueError):
        semantic_drain._partial_assessment(partial, off)
    with pytest.raises(ValueError):
        semantic_drain._partial_assessment({'E06': {'noul': 'x'}}, on)


def test_optional_proposition_verdicts_bind_the_active_set(runtime):
    led, _, _ = runtime
    on = semantic_policy.runtime_config(
        _cfg('assist', summary_mode='off', loop_mode='off',
             extra_propositions=['E06']))[0]
    off = semantic_policy.runtime_config(
        _cfg('assist', summary_mode='off', loop_mode='off'))[0]
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    client = _FakeJev()
    verdicts, flag = semantic_drain._assess_target(
        led, on, bundle, client, 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(on), None,
        time.monotonic() + 60, diagnostics={})
    assert flag is None
    assert set(verdicts) == set(jev.PROPOSITIONS) | {'E06'}
    row = led.db.execute(
        "SELECT m.*, p.project_id AS patient_id, p.fetch_reason "
        "FROM messages m JOIN patients p ON p.project_id=m.project_id "
        "WHERE m.message_id=1").fetchone()
    led.db.execute("UPDATE fetch_jobs SET state='done' WHERE state != 'done'")
    led.db.commit()
    assert semantic_drain._reprocess_target_state(led, row, on) == 'current'
    # required-only history cannot satisfy an enabled installation, and the
    # wider row cannot satisfy a disabled one either
    assert semantic_drain._reprocess_target_state(led, row, off) == 'held'


def test_proposition_quality_report_counts_only_current_active_rows(runtime):
    led, _, _ = runtime
    on = semantic_policy.runtime_config(_cfg('assist'))[0]
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    verdicts, flag = semantic_drain._assess_target(
        led, on, bundle, _FakeJev(noul=0.9), 1, 1,
        bundle['source_fingerprint'], semantic_policy.policy_fingerprint(on),
        None, time.monotonic() + 60, diagnostics={})
    assert flag is None and verdicts
    report = semantic_drain.proposition_quality_report(led, 1, on)
    assert report['messages'] == 1 and report['superseded'] == 0
    assert set(report['propositions']) == set(jev.PROPOSITIONS)
    p01 = report['propositions']['P01']
    assert p01['verdicts']['MATCH'] == 1 and p01['noul_mean'] == 0.9
    # a row written under a wider enabled set is superseded, not averaged in
    wider = semantic_policy.runtime_config(
        _cfg('assist', extra_propositions=['E01']))[0]
    report_wider = semantic_drain.proposition_quality_report(led, 1, wider)
    assert report_wider['messages'] == 0 and report_wider['superseded'] == 1
