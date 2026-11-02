"""Optional proposition groups extend the assessed set per installation."""
import time
import json

import pytest

import semantic_drain
import semantic_jev as jev
import semantic_policy
import semantic_runtime
import semantic_store
from semantic_testkit import _cfg, _FakeJev
from test_patient_context_refresh import runtime as runtime
from test_patient_context_refresh import _raw_proof, seed


def test_extra_propositions_config_validates_ids():
    scfg, errors = semantic_policy.runtime_config(
        _cfg('assist', extra_propositions=['E01', 'E06']))
    assert not errors
    assert scfg['extra_propositions'] == ['E01', 'E06']
    for bad in ('E01', [1], ['E99'], ['E01', 'E01'], ['P01'], [{}], [['E01']]):
        _scfg, errors = semantic_policy.runtime_config(
            _cfg('assist', extra_propositions=bad))
        assert 'config: semantic_extra_propositions_invalid' in errors
        assert _scfg['mode'] == 'off'


def test_extra_proposition_set_binds_policy_and_execution_generation(monkeypatch):
    base = semantic_policy.runtime_config(_cfg('assist'))[0]
    old = {key: value for key, value in base.items() if key != 'extra_propositions'}
    first = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E06', 'E01']))[0]
    second = semantic_policy.runtime_config(_cfg('assist', extra_propositions=['E01', 'E06']))[0]
    assert first['extra_propositions'] == second['extra_propositions'] == ['E01', 'E06']
    assert semantic_policy.policy_fingerprint(base) == semantic_policy.policy_fingerprint(old)
    assert semantic_policy.policy_fingerprint(first) == semantic_policy.policy_fingerprint(second)
    assert semantic_policy.policy_fingerprint(first) != semantic_policy.policy_fingerprint(base)
    assert semantic_runtime.execution_generation(first) != semantic_runtime.execution_generation(base)
    before = semantic_policy.policy_fingerprint(first)
    changed = {key: dict(value) for key, value in jev.OPTIONAL_PROPOSITIONS.items()}
    changed['E01']['instructions'] += ' synthetic change'
    monkeypatch.setattr(jev, 'OPTIONAL_PROPOSITIONS', changed)
    assert semantic_policy.policy_fingerprint(first) != before
    assert semantic_policy.policy_fingerprint(base) == semantic_policy.policy_fingerprint(old)


def test_active_propositions_defaults_to_required_set():
    assert set(jev.active_propositions({})) == set(jev.PROPOSITIONS)
    assert set(jev.active_propositions(None)) == set(jev.PROPOSITIONS)
    active = jev.active_propositions({'extra_propositions': ['E02', 'E06']})
    assert set(active) == set(jev.PROPOSITIONS) | {'E02', 'E06'}
    assert active['E02']['label_ja'] == '転倒' and active['E06']['label_ja'] == '評価者への指示文'
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
    verdicts, flag = semantic_drain._assess_target(
        led, off, bundle, client, 1, 1, bundle['source_fingerprint'],
        semantic_policy.policy_fingerprint(off), None,
        time.monotonic() + 60, diagnostics={})
    assert flag is None and set(verdicts) == set(jev.PROPOSITIONS)
    assert semantic_drain._reprocess_target_state(led, row, off) == 'current'
    assert semantic_drain._reprocess_target_state(led, row, on) == 'current'


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

    row = led.db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND message_id=1 "
                         "ORDER BY artifact_id DESC LIMIT 1", (semantic_drain.KIND_ASSESS,)).fetchone()
    content, meta = json.loads(row['content']), json.loads(row['meta'])
    content['verdicts']['E01'] = {'noul': 0.9, 'verdict': 'MATCH'}
    meta['policy_fingerprint'] = semantic_policy.policy_fingerprint(wider)
    led.artifact_add(semantic_drain.KIND_ASSESS, json.dumps(content), project_id=1,
                     message_id=1, model=jev.JEV_MODEL, meta=meta)
    report = semantic_drain.proposition_quality_report(led, 1, on)
    assert report['messages'] == 0 and report['superseded'] == 1


def test_proposition_quality_report_excludes_changed_source(runtime):
    led, _, _ = runtime
    cfg = semantic_policy.runtime_config(_cfg('assist'))[0]
    seed(led, 1, proof=False)
    _raw_proof(led, 1, 1)
    bundle = semantic_store.thread_bundle(led.db, 1, 1, [1])
    semantic_drain._assess_target(led, cfg, bundle, _FakeJev(noul=0.9), 1, 1,
        bundle['source_fingerprint'], semantic_policy.policy_fingerprint(cfg), None,
        time.monotonic() + 60, diagnostics={})
    assert semantic_drain.proposition_quality_report(led, 1, cfg)['messages'] == 1
    led.db.execute("UPDATE messages SET body_text='異なる合成本文' WHERE message_id=1")
    assert semantic_drain.proposition_quality_report(led, 1, cfg)['messages'] == 0
