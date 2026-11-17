"""Synthetic quality-check identity: only the checking code withdraws a PASS (P0-2c)."""
import functools
import json
import sys
from contextlib import closing
from types import SimpleNamespace

import pytest

import cloud_inference
import extract_llm
import patient_context_plan as context_plan
import patient_context_quality_identity as quality_identity
import patient_context_refresh as context
import patient_context_refresh_state as state
import semantic_audit
import semantic_candidate
import semantic_jev
import semantic_policy
import semantic_runtime
from extract_testkit import _hash
from inference_version import CURRENT_VERSION, VERSION_KEY
from ledger import Ledger
from mcs_util import payload_hash
from test_cloud_cli import _complete_context, world as world

__all__ = ['world']

POLICY_CFG = {'semantic': {'mode': 'assist', 'project_ids': [1]}}
CHECKING_PATH = {
    'semantic_audit._probability', 'semantic_audit._patient_quality_sources',
    'semantic_audit._patient_quality_facts', 'semantic_audit.patient_source_question_keys',
    'semantic_audit.patient_source_quality_pages', 'semantic_audit.patient_source_quality_identity',
    'semantic_audit.audit_patient_quality_page', 'semantic_audit.audit_patient_summary_chunk',
    'semantic_jev.choice_question', 'semantic_jev.evaluate_once_more', 'semantic_jev.validate_answers',
    'semantic_jev._validate_usage', 'semantic_jev._validate_answer', 'semantic_jev.finite_value',
    'semantic_jev.is_answer_defect', 'semantic_jev.failure_class',
    'patient_context_quality_identity.patient_quality_expected',
}


def _policy():
    policy, errors = semantic_policy.semantic_config(POLICY_CFG, key_present=True)
    assert not errors
    return policy


def _old_formula():
    """The pre-P0-2c code: every loaded source digest and the whole candidate witness."""
    return state._digest([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()])


def _unrelated_release(monkeypatch):
    """Change loaded code that never decides a quality answer, and prove it moved the old code."""
    before = _old_formula()
    monkeypatch.setattr(semantic_candidate, 'loaded_source_contract', lambda: {'synthetic': 'b' * 64})
    monkeypatch.setitem(extract_llm._LOADED_SOURCE_DIGESTS, 'semantic_store', '0' * 64)
    assert _old_formula() != before


def test_the_hashed_set_is_exactly_the_checking_path():
    names = {f'{module}.{name}' for module, name in quality_identity.QUALITY_FUNCTIONS}
    assert names == CHECKING_PATH
    # A new patient-quality helper in semantic_audit must be classified here.
    helpers = {f'semantic_audit.{name}' for name, value in vars(semantic_audit).items()
               if callable(value) and getattr(value, '__module__', None) == 'semantic_audit'
               and 'patient' in name}
    assert helpers <= names
    for module, name in quality_identity.QUALITY_FUNCTIONS:
        function = getattr(__import__(module), name)
        assert function.__closure__ is None and not hasattr(function, '__wrapped__'), name
    constants = {f'{module}.{name}' for module, name in quality_identity.QUALITY_CONSTANTS}
    assert {'semantic_audit.FACT_SUPPORT_OPTIONS', 'semantic_audit.COVERAGE_OPTIONS',
            'semantic_jev.CLAIM_SUPPORT_OPTIONS', 'semantic_jev.JEV_MODEL'} <= constants


def test_an_unrelated_release_keeps_the_assessment(monkeypatch):
    policy = _policy()
    code = quality_identity.patient_quality_assessment_code()
    assessment = state._quality_assessment(policy)
    assert assessment == {'code': code, 'policy': semantic_policy.policy_fingerprint(policy)}
    _unrelated_release(monkeypatch)
    assert quality_identity.patient_quality_assessment_code() == code
    assert state._quality_assessment(policy) == assessment


@pytest.mark.parametrize('change', ['page_checker', 'support_option', 'jev_retry', 'expected_answer'])
def test_a_checking_code_change_moves_the_code_and_restores_after_undo(monkeypatch, change):
    code = quality_identity.patient_quality_code()
    with monkeypatch.context() as patch:
        if change == 'page_checker':
            def audit_patient_quality_page(jev_client, page, deadline, threshold, *,
                                           prior_answers=None, on_valid_answers=None, on_attempt_result=None):
                return {'evaluated': True, 'findings': [], 'answers': {}}
            patch.setattr(semantic_audit, 'audit_patient_quality_page', audit_patient_quality_page)
        elif change == 'support_option':
            patch.setitem(semantic_audit.FACT_SUPPORT_OPTIONS, 'synthetic_partly', 'a synthetic extra option')
        elif change == 'jev_retry':
            def evaluate_once_more(client, state, questions, deadline, *, on_valid_answers=None,
                                   on_attempt_result=None):
                return client.evaluate(state, questions, deadline)
            patch.setattr(semantic_jev, 'evaluate_once_more', evaluate_once_more)
        else:
            def patient_quality_expected(identity, fid, selected):
                return 'supports'
            patch.setattr(quality_identity, 'patient_quality_expected', patient_quality_expected)
        assert quality_identity.patient_quality_code() != code
    assert quality_identity.patient_quality_code() == code


def test_a_qa_version_bump_moves_only_the_assessment(monkeypatch):
    code = quality_identity.patient_quality_code()
    assessment = quality_identity.patient_quality_assessment_code()
    monkeypatch.setattr(quality_identity, 'QA_VERSION', quality_identity.QA_VERSION + 1)
    assert quality_identity.patient_quality_code() == code
    assert quality_identity.patient_quality_assessment_code() != assessment


def test_the_interpreter_that_shapes_bytecode_is_named(monkeypatch):
    code = quality_identity.patient_quality_code()
    major, minor = sys.version_info[:2]
    monkeypatch.setattr(quality_identity, 'sys', SimpleNamespace(
        version_info=sys.version_info, flags=SimpleNamespace(optimize=sys.flags.optimize + 1)))
    assert quality_identity.patient_quality_code() != code
    monkeypatch.setattr(quality_identity, 'sys', SimpleNamespace(
        version_info=(major, minor + 1, 0), flags=sys.flags))
    assert quality_identity.patient_quality_code() != code


SOURCE = '''def checker(values):
    scale = lambda value: value * 2
    def nested(items):
        return [scale(item) for item in items if item]
    return sum(item for item in nested(values))
'''


def _compiled(text, *, lines=0, filename='synthetic_a.py'):
    namespace = {}
    exec(compile('\n' * lines + text, filename, 'exec'), namespace)
    return namespace['checker']


def test_code_identity_ignores_position_and_file_but_not_behaviour():
    base = semantic_candidate.code_identity(_compiled(SOURCE))
    # Nested def, lambda, comprehension and generator code are all position-free.
    assert semantic_candidate.code_identity(_compiled(SOURCE, lines=37, filename='synthetic_b.py')) == base
    assert semantic_candidate.code_identity(_compiled(SOURCE.replace('value * 2', 'value * 3'))) != base
    assert semantic_candidate.code_identity(_compiled(SOURCE.replace('if item]', 'if item > 1]'))) != base
    assert semantic_candidate.code_identity(_compiled(SOURCE.replace('item for item', 'item + 1 for item'))) != base

    def closure():
        return base
    with pytest.raises(TypeError):
        semantic_candidate.code_identity(closure)

    @functools.wraps(_compiled(SOURCE))
    def wrapper(values):
        return 0
    with pytest.raises(TypeError):
        semantic_candidate.code_identity(wrapper)


def test_non_legacy_fact_source_still_binds_all_loaded_code(monkeypatch):
    """Pinned current behaviour; the owner has not decided the non-legacy case yet."""
    cfg = {'semantic': {**POLICY_CFG['semantic'], 'fact_source': 'shadow'}}
    policy, errors = semantic_policy.semantic_config(cfg, key_present=True)
    assert not errors and policy['candidate_fingerprint']
    assessment = state._quality_assessment(policy)
    monkeypatch.setattr(semantic_candidate, 'loaded_source_contract', lambda: {'synthetic': 'b' * 64})
    moved, errors = semantic_policy.semantic_config(cfg, key_present=True)
    assert not errors and moved['candidate_fingerprint'] != policy['candidate_fingerprint']
    changed = state._quality_assessment(moved)
    assert changed['code'] == assessment['code'] and changed['policy'] != assessment['policy']


def _route(config):
    route = context._route({**config, 'cloud_llm': {'mode': 'off'}})
    route['provider'] = cloud_inference.cloud_provider_contract()
    return route


def _quality_rows(db):
    return db.db.execute("SELECT artifact_id FROM artifacts WHERE kind IN (?,?,?) ORDER BY artifact_id",
                         (context.QUALITY_PASS, 'patient_context_source_quality_v1',
                          'patient_context_source_quality_pass_v1')).fetchall()


def _captured(db, config, route, assessment):
    snapshot = context._snapshot(db.db, 1, config, hydrate=False, route=route)
    [source] = snapshot['sources']
    db.db.execute('BEGIN')
    try:
        captured = context._source_quality_capture(db.db, 1, config, context_plan.source_key(source),
                                                    assessment, route=route)
        summary = context._quality_capture(db.db, 1, config, 'synthetic', [], assessment,
                                           {'groups': {}, 'relations': []}, route=route)
    finally:
        db.db.rollback()
    return source, captured, summary


def test_an_unrelated_release_keeps_the_published_pass_without_rechecks(world, monkeypatch):
    _, path, config = world
    with closing(Ledger(str(path))) as db:
        _complete_context(db, config)
        route, policy = _route(config), semantic_policy.semantic_config(config, key_present=True)[0]
        assessment = state._quality_assessment(policy)
        source, captured, summary = _captured(db, config, route, assessment)
        _, meta = state._latest(db.db, context.KIND, 1)
        hold = state._source_quality_hold_key(db.db, 1, config, source, 'a' * 64)
        rows = _quality_rows(db)
        _unrelated_release(monkeypatch)
        assert state._quality_assessment(policy) == assessment
        assert state._source_quality_hold_key(db.db, 1, config, source, 'a' * 64) == hold
        assert meta['quality_schedule_key'] == state._digest([config, state._quality_assessment_code()])
        _, again, overview = _captured(db, config, route, state._quality_assessment(policy))
        assert again['identity']['audit_key'] == captured['identity']['audit_key']
        assert overview['identity']['audit_key'] == summary['identity']['audit_key'] == meta['quality_audit_key']
        snapshot, _, pending = context._pending(db.db, 1, config, route=route)
        assert not any(pending.values()) and snapshot['unfinished'] is False
        assert _quality_rows(db) == rows


def test_a_qa_version_bump_withholds_as_unchecked_and_keeps_the_old_pass(world, monkeypatch):
    _, path, config = world
    with closing(Ledger(str(path))) as db:
        _complete_context(db, config)
        route, policy = _route(config), semantic_policy.semantic_config(config, key_present=True)[0]
        source, _, _ = _captured(db, config, route, state._quality_assessment(policy))
        hold = state._source_quality_hold_key(db.db, 1, config, source, 'a' * 64)
        _, meta = state._latest(db.db, context.KIND, 1)
        rows = _quality_rows(db)
        monkeypatch.setattr(quality_identity, 'QA_VERSION', quality_identity.QA_VERSION + 1)
        assert meta['quality_schedule_key'] != state._digest([config, state._quality_assessment_code()])
        assert state._source_quality_hold_key(db.db, 1, config, source, 'a' * 64) != hold
        snapshot, _, pending = context._pending(db.db, 1, config, route=route)
        # Recheck pending, never "complete with nothing found"; old receipts stay for audit.
        assert snapshot['unfinished'] is True and any(pending.values())
        assert _quality_rows(db) == rows


def test_pass_receipts_and_state_agree_on_the_code(world):
    _, path, config = world
    with closing(Ledger(str(path))) as db:
        _complete_context(db, config)
        route, policy = _route(config), semantic_policy.semantic_config(config, key_present=True)[0]
        assessment = state._quality_assessment(policy)
        _, captured, _ = _captured(db, config, route, assessment)
        identity = captured['identity']
        descriptor = semantic_runtime.patient_source_assessment_descriptor(assessment, policy)
        stale = {**descriptor, 'assessment': {**assessment, 'code': payload_hash(
            [extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()])}}
        db.db.execute('BEGIN')
        try:
            content = semantic_runtime.patient_source_pass_content(db.db, 1, identity, descriptor)
            assert content is not None and content['descriptor']['assessment']['code'] == assessment['code']
            with pytest.raises(ValueError, match='patient_source_descriptor_invalid'):
                semantic_runtime.patient_source_pass_content(db.db, 1, {**identity, 'assessment': stale['assessment']},
                                                             stale)
        finally:
            db.db.rollback()


def test_same_content_reextraction_keeps_overview_and_source_pass(world, monkeypatch):
    _, path, config = world
    item = {'category': 'living', 'text': 'synthetic private body', 'evidence': 'synthetic private body',
            'subject': 'patient'}

    def rule(db, **extra):
        db.artifact_add('extract_v1', json.dumps({'patient_context': [{**item, **extra}]}), project_id=1,
                        message_id=1, meta={VERSION_KEY: CURRENT_VERSION, 'hash': _hash(db, 1),
                                            'rule_version': extract_llm.extract.RULE_VERSION})

    with closing(Ledger(str(path))) as db:
        rule(db)
        _complete_context(db, config)
        route, policy = _route(config), semantic_policy.semantic_config(config, key_present=True)[0]
        assessment = state._quality_assessment(policy)
        _, captured, summary = _captured(db, config, route, assessment)
        _, meta = state._latest(db.db, context.KIND, 1)
        first = db.db.execute("SELECT max(artifact_id) FROM artifacts WHERE kind='extract_v1'").fetchone()[0]
        # A real re-extraction: a new artifact with the same scan items (P0-2b).
        rule(db, confidence=0.9)
        assert db.db.execute("SELECT max(artifact_id) FROM artifacts WHERE kind='extract_v1'").fetchone()[0] != first
        _, again, overview = _captured(db, config, route, assessment)
        assert again['identity']['audit_key'] == captured['identity']['audit_key']
        assert overview['identity']['audit_key'] == summary['identity']['audit_key'] == meta['quality_audit_key']
        snapshot, _, pending = context._pending(db.db, 1, config, route=route)
        assert not any(pending.values()) and snapshot['unfinished'] is False
