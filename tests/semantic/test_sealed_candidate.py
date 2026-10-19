"""Synthetic adversarial cases for candidate-bound sealed evaluation."""
import semantic_transport
import copy
import json
from pathlib import Path

import pytest

import semantic_candidate as candidate
import semantic_evaluation as evaluation
import semantic_holdout as holdout
import semantic_snapshot as snapshot
from semantic_policy import semantic_config
from semantic_testkit import MANIFEST, CRITERIA, _record
from candidate_testkit import approved_config
from mcs_util import payload_hash


def test_approved_candidate_rejects_each_interpretation_change():
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    assert not semantic_config(cfg)[1]
    for field, value in (('model', 'new'), ('match_threshold', 0.99), ('fact_source_gate', 'x')):
        changed = copy.deepcopy(cfg)
        changed['semantic'][field] = value
        result, errors = semantic_config(changed)
        assert errors and result['fact_source'] == 'legacy'
    for field, value in (('extract', {'patient_state_context': True}),
                         ('local_llm', {'model': 'new-local'})):
        changed = {**cfg, field: value}
        assert semantic_config(changed)[0]['fact_source'] == 'legacy'


def test_plan_and_prompt_change_invalidates_candidate(monkeypatch):
    import extract_llm
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    monkeypatch.setattr(extract_llm, 'PLAN_VERSION', extract_llm.PLAN_VERSION + 1)
    assert semantic_config(cfg)[0]['fact_source'] == 'legacy'


def test_synthetic_human_labels_never_g6():
    result = evaluation.evaluate_records([_record('human')], MANIFEST, CRITERIA)
    assert result['gate']['pass']  # Historical v1 point scores remain comparable.
    assert not result['gate']['g6_eligible']
    assert result['data_origin'] == {'real_authorized': 0, 'synthetic': 1}


def test_duplicate_receipt_cannot_count_twice():
    first = _record('human')
    second = copy.deepcopy(first)
    second['case_id'] = 'second'
    with pytest.raises(evaluation.EvaluationError, match='label_receipt_duplicate'):
        evaluation.evaluate_records([first, second], MANIFEST, CRITERIA)


def _sealed():
    record = _record('human')
    record.update(patient_id='synthetic-patient', stratum='medication', data_origin='real_authorized')
    label = record['label']
    digest = payload_hash({k: v for k, v in label.items() if k != 'receipt'})
    label['reviews'] = [{'reviewer': r, 'receipt_id': 'review-' + r, 'blind': True,
                         'candidate_sha256': payload_hash(record['candidate']),
                         'label_sha256': digest} for r in ('one', 'two')]
    manifest = {**MANIFEST, 'data_origin': 'real_authorized', 'reviewers': ['one', 'two', 'three'],
                'split_registry': {'dev': [], 'calibration': [], 'test': [
                    {'patient_id': record['patient_id'], 'thread_id': record['thread_id']}]}}
    manifest['holdout_manifest_sha256'] = payload_hash(holdout.holdout_payload([record]))
    return record, manifest


def test_holdout_requires_seal_registry_independent_blind_reviews_and_adjudication():
    record, manifest = _sealed()
    assert holdout.evidence_summary([record], manifest, {})['sealed']
    for mutation in ('seal', 'split', 'duplicate', 'blind', 'judgment', 'stratum'):
        row, fixed = copy.deepcopy(record), copy.deepcopy(manifest)
        criteria = {}
        if mutation == 'seal':
            fixed['holdout_manifest_sha256'] = '0' * 64
        elif mutation == 'split':
            fixed['split_registry']['dev'] = fixed['split_registry']['test']
        elif mutation == 'duplicate':
            row['label']['reviews'][1]['reviewer'] = 'one'
        elif mutation == 'blind':
            row['label']['reviews'][1]['blind'] = False
        elif mutation == 'judgment':
            row['label']['reviews'][1]['label_sha256'] = '0' * 64
        else:
            criteria['strata_minimums'] = {'urgency': 1}
        assert not holdout.evidence_summary([row], fixed, criteria)['sealed'], mutation
    record['label']['reviews'][1]['label_sha256'] = '0' * 64
    record['label']['adjudication'] = {'reviewer': 'three', 'receipt_id': 'adjudication',
        'label_sha256': record['label']['reviews'][0]['label_sha256']}
    assert holdout.evidence_summary([record], manifest, {})['sealed']


def test_v2_rejects_small_denominators_and_wilson_uncertainty():
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    report = cfg['semantic']['fact_source_evidence']
    metric = 'important_fact_recall'
    report['splits']['test']['metrics'][metric] = evaluation.success_metric(3, 3)
    gate = evaluation._gate(report, report['criteria'], report['label_provenance'])
    assert 'denominator_insufficient:' + metric in gate['reasons']
    assert 'below_threshold:' + metric in gate['reasons']


@pytest.mark.parametrize('field', ['urgency_recall', 'ai_priority_accuracy', 'patient_state_ab_noninferiority'])
def test_missing_new_safety_metrics_block_promotion(field):
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    report = cfg['semantic']['fact_source_evidence']
    report['splits']['test']['metrics'][field] = evaluation.success_metric(0, 200)
    assert candidate.promotion_problem(cfg, report) == 'gate_failed'


@pytest.mark.parametrize('field', ['body', 'text', 'messages', 'source_text', 'patient_id'])
def test_text_in_report_cannot_activate(field):
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    report = cfg['semantic']['fact_source_evidence']
    report['run'][field] = 'SYNTHETIC_CANARY'
    assert candidate.promotion_problem(cfg, report) == 'report_not_metrics_only'


def test_local_snapshot_runs_real_helpers_with_stubbed_models_and_keeps_missing_gates(monkeypatch):
    import extract_llm
    import semantic_extraction
    record = _record()
    record.pop('candidate')
    record.pop('label')
    record['data_origin'] = 'synthetic'
    record['bundle']['messages'] = [{'message_id': 'm1', 'body': 'fully synthetic'}]
    manifest = {**MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout.holdout_payload([record]))}
    source = {'records': [record], 'manifest': manifest, 'data_origin': 'synthetic'}
    calls = []
    monkeypatch.setattr(extract_llm, 'llm_extract', lambda *a, **k: {'urgency': 'high'})
    def extract(fn, member, deadline, **kwargs):
        calls.append(member)
        assert kwargs['jev_client'] is None
        return {'doc': {'facts': []}, 'extraction_complete': False}
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', extract)
    cfg = {'semantic': {'fact_source': 'canonical'}}
    result = snapshot.generate(source, cfg)
    assert len(calls) == 1
    assert result['candidate_fingerprint'] == candidate.candidate_fingerprint(cfg)
    assert result['run']['external_audit_used'] is False
    assert result['records'][0]['candidate']['status'] == 'deferred'
    assert 'delivered_fact_ids' not in result['records'][0]['candidate']
    assert result['records'][0]['patient_state_ab']['off_fingerprint'] != result['records'][0]['patient_state_ab']['on_fingerprint']
    reviewed = copy.deepcopy(result['records'])
    reviewed[0]['candidate']['status'] = 'complete'
    with pytest.raises(evaluation.EvaluationError, match='review_prediction_changed'):
        snapshot.score(result, reviewed, cfg, CRITERIA)


def test_runner_rejects_remote_endpoint_before_inference():
    with pytest.raises(evaluation.EvaluationError, match='loopback_model_required'):
        snapshot.generate({}, {'local_llm': {'url': 'https://example.com/v1/chat/completions'}})


def test_runner_never_reads_private_sources_inside_repo():
    with pytest.raises(evaluation.EvaluationError, match='private_local_path_required'):
        snapshot._private(str(Path(__file__)))


def test_metrics_report_contains_no_original_text():
    report = evaluation.evaluate_records([_record()], MANIFEST, CRITERIA)
    assert candidate.metrics_only(report)
    assert 'ORIGINAL-SECRET' not in json.dumps(report)


def test_fully_synthetic_manifest_cannot_be_relabelled_real():
    record = _record('human')
    record['data_origin'] = 'real_authorized'
    with pytest.raises(evaluation.EvaluationError, match='data_origin_mismatch'):
        evaluation.evaluate_records([record], MANIFEST, CRITERIA)
    manifest = {**MANIFEST, 'data_origin': 'real_authorized', 'source': 'fully_synthetic'}
    with pytest.raises(evaluation.EvaluationError, match='manifest_data_origin_mismatch'):
        evaluation.evaluate_records([record], manifest, CRITERIA)


def test_report_cannot_self_approve_without_separate_human_receipt(monkeypatch, tmp_path):
    import mcs_util
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    assert not semantic_config(cfg)[1]
    monkeypatch.setattr(mcs_util, 'HOME', str(tmp_path / 'no-approval-store'))
    assert semantic_config(cfg)[0]['fact_source'] == 'legacy'


@pytest.mark.parametrize('raw', ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}'])
def test_nonfinite_or_duplicate_json_evidence_is_rejected(raw):
    with pytest.raises(ValueError):
        candidate.strict_json(raw)


@pytest.mark.parametrize('field', ['urgency_recall', 'ai_priority_accuracy', 'patient_state_ab_noninferiority'])
def test_zero_denominator_new_safety_metrics_block_promotion(field):
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    report = cfg['semantic']['fact_source_evidence']
    report['splits']['test']['metrics'][field] = evaluation.success_metric(0, 0)
    gate = evaluation._gate(report, report['criteria'], report['label_provenance'])
    assert 'denominator_zero:' + field in gate['reasons']
    assert not gate['g6_eligible']


@pytest.mark.parametrize('project_type,body,expected', [
    ('medical', '本人は意識がなく、呼吸が停止しています。', True),
    ('medical', '家族は意識がなく、呼吸が停止しています。', False),
    ('group', '本人は意識がなく、呼吸が停止しています。', False),
    ('medical', '緊急時は連絡してください。', False),
])
def test_snapshot_ai_priority_is_production_guarded_badge(project_type, body, expected):
    bundle = {'project_type': project_type, 'messages': [{'body': body}]}
    raw = {'urgency': 'high', 'urgency_evidence': [body]}
    assert snapshot.effective_ai_priority(bundle, [raw]) is expected
    assert snapshot.effective_ai_priority(bundle, [None]) is None


@pytest.mark.parametrize('side', ['candidate', 'label'])
def test_ai_priority_contract_is_boolean(side):
    row = _record()
    row[side]['ai_priority'] = 'high'
    with pytest.raises(evaluation.EvaluationError, match=side + '_ai_priority_invalid'):
        evaluation.evaluate_records([row], MANIFEST, CRITERIA)


def test_ai_priority_accuracy_counts_effective_flags_not_raw_urgency():
    row = _record()
    row['candidate'].update(urgency='high', ai_priority=False)
    row['label'].update(urgency='high', ai_priority=False)
    report = evaluation.evaluate_records([row], MANIFEST, CRITERIA)
    assert report['metrics']['ai_priority_accuracy']['rate'] == 1
    row['label']['ai_priority'] = True
    report = evaluation.evaluate_records([row], MANIFEST, CRITERIA)
    assert report['metrics']['ai_priority_accuracy']['rate'] == 0


def test_snapshot_never_opens_shared_admission_database(monkeypatch):
    monkeypatch.setenv('MCS_LLM_ADMISSION', '1')
    with pytest.raises(evaluation.EvaluationError, match='shared_admission_evaluation_forbidden'):
        snapshot.generate({}, {})


def test_vital_threshold_change_invalidates_candidate():
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    cfg['vital_urgency'] = {'mode': 'high', 'thresholds': {'spo2_lte': 91}}
    assert semantic_config(cfg)[0]['fact_source'] == 'legacy'


@pytest.mark.parametrize('origin,manifest_origin', [
    ('real_authorized', None), ('real_authorized', 'synthetic'),
    ('synthetic', 'real_authorized')])
def test_snapshot_origin_mismatch_rejected_before_any_model(monkeypatch, origin, manifest_origin):
    import local_llm
    import extract_llm
    import semantic_extraction
    def forbidden(*args, **kwargs):
        pytest.fail('model called before origin validation')
    monkeypatch.setattr(local_llm, 'chat', forbidden)
    monkeypatch.setattr(extract_llm, 'llm_extract', forbidden)
    monkeypatch.setattr(semantic_extraction, 'extract_facts_v2', forbidden)
    record = _record()
    record.pop('candidate')
    record.pop('label')
    record['data_origin'] = origin
    manifest = {**MANIFEST, 'holdout_manifest_sha256': payload_hash(holdout.holdout_payload([record]))}
    if manifest_origin is not None:
        manifest['data_origin'] = manifest_origin
    with pytest.raises(evaluation.EvaluationError, match='manifest_data_origin_mismatch'):
        snapshot.generate({'records': [record], 'manifest': manifest, 'data_origin': origin}, {})


def test_loaded_jev_method_change_only_affects_new_worker_fingerprint(monkeypatch):
    import semantic_jev
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    original = candidate.candidate_fingerprint({})
    def changed_evaluate(self, *args, **kwargs):
        raise AssertionError('synthetic method is fingerprinted, never called')
    monkeypatch.setattr(semantic_jev.JevClient, 'evaluate', changed_evaluate)
    assert candidate.candidate_fingerprint({}) == original
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    assert candidate.candidate_fingerprint({}) != original


@pytest.mark.parametrize('name', ['item_scope', 'scoped_vitals', 'vital_line'])
def test_moved_clinical_display_changes_only_new_worker_identity(monkeypatch, name):
    import clinical_display

    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    original = candidate.candidate_fingerprint({})

    def changed(*args, **kwargs):
        raise AssertionError('interpretation code is fingerprinted, never called')

    changed.__module__ = clinical_display.__name__
    monkeypatch.setattr(clinical_display, name, changed)
    assert candidate.candidate_fingerprint({}) == original
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    assert candidate.candidate_fingerprint({}) != original


@pytest.mark.parametrize('accessor', ['method', 'static', 'class_method', 'fget', 'fset', 'fdel'])
def test_class_callable_surfaces_are_fingerprinted_without_evaluation(monkeypatch, accessor):
    import sys
    import types
    module = types.ModuleType('synthetic_candidate_surface')
    exec('''class Surface:
    def method(self): return 1
    @staticmethod
    def static(): return 1
    @classmethod
    def class_method(cls): return 1
    @property
    def prop(self): raise AssertionError("descriptor must not execute")
    @prop.setter
    def prop(self, value): pass
    @prop.deleter
    def prop(self): pass
''', module.__dict__)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(candidate, '_MODULES', (module.__name__,))
    original = candidate._sources()
    def changed(*args):
        return 2
    cls = module.Surface
    if accessor in ('fget', 'fset', 'fdel'):
        old = vars(cls)['prop']
        methods = {name: getattr(old, name) for name in ('fget', 'fset', 'fdel')}
        methods[accessor] = changed
        setattr(cls, 'prop', property(**methods))
    else:
        value = staticmethod(changed) if accessor == 'static' else classmethod(changed) if accessor == 'class_method' else changed
        setattr(cls, accessor, value)
    assert candidate._sources() != original


def test_disk_edit_does_not_replace_loaded_class_code(tmp_path, monkeypatch):
    import importlib.util
    import sys
    source = tmp_path / 'synthetic_loaded_surface.py'
    source.write_text('class Surface:\n    def method(self): return 1\n')
    spec = importlib.util.spec_from_file_location('synthetic_loaded_surface', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(candidate, '_MODULES', (module.__name__,))
    original = candidate._sources()
    source.write_text('class Surface:\n    def method(self): return 999\n')
    assert candidate._sources() == original


@pytest.mark.parametrize('module_name,constant,kind', [
    ('clinical_urgency', '_URGENT', 'regex'),
    ('extract_schema', 'MEDICATION_TOKEN', 'regex'),
    ('semantic_jev', 'PROPOSITIONS', 'dict'),
    ('semantic_facts', 'FACT_KINDS', 'tuple'),
    ('semantic_relations', '_GENERIC_WORDS', 'set'),
    ('semantic_jev', 'JEV_ALLOWED_ENDPOINTS', 'frozenset'),
    ('semantic_assembly', '_SENTENCE_BREAK', 'regex'),
    ('semantic_projection', '_CARE_EVENT_KEYWORDS', 'list'),
])
def test_loaded_interpretation_collections_change_new_worker_identity(monkeypatch, module_name, constant, kind):
    import importlib
    import re
    module = importlib.import_module(module_name)
    before = getattr(module, constant)
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    original = candidate.candidate_fingerprint({})
    if kind == 'dict':
        changed = {**before, 'synthetic_added_rule': {'options': ['one', 'two']}}
    elif kind == 'regex':
        changed = re.compile(before.pattern, before.flags ^ re.IGNORECASE)
    elif kind in ('set', 'frozenset'):
        changed = type(before)([*before, 'synthetic-added-option'])
    else:
        changed = type(before)([*before, 'synthetic-added-rule'])
    monkeypatch.setattr(module, constant, changed)
    assert candidate.candidate_fingerprint({}) == original
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    assert candidate.candidate_fingerprint({}) != original


def test_constant_normalization_is_order_independent_but_preserves_type_and_regex_flags():
    import re
    from zoneinfo import ZoneInfo
    normalize = candidate._constant
    assert normalize({'a': (1, {'x', 'y'}), 'b': None}) == normalize({'b': None, 'a': (1, {'y', 'x'})})
    assert normalize([1, 2]) != normalize((1, 2))
    assert normalize({'a'}) != normalize(frozenset({'a'}))
    assert normalize(True) != normalize(1)
    assert normalize(re.compile('rule', re.I)) != normalize(re.compile('rule'))
    assert normalize(ZoneInfo('Asia/Tokyo')) != normalize(ZoneInfo('UTC'))
    assert normalize({'nested': ['a', 'b']}) != normalize({'nested': ['a', 'c']})


@pytest.mark.parametrize('value', [object(), {'nested': object()}, float('nan'), float('inf')])
def test_unknown_or_nonfinite_constant_fails_instead_of_silently_disappearing(monkeypatch, value):
    import sys
    import types
    module = types.ModuleType('synthetic_constant_contract')
    module.RULE = value
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(candidate, '_MODULES', (module.__name__,))
    with pytest.raises((TypeError, ValueError), match='candidate_constant'):
        candidate._sources()


def test_runtime_counters_do_not_change_candidate_sources(monkeypatch):
    original = candidate._sources()
    monkeypatch.setattr(semantic_transport, '_LONG_UNSAVED', {'synthetic-message': 2})
    monkeypatch.setattr(semantic_transport, '_LONG_MARK_FAILURES', {'busy': 10, 'error': 3})
    monkeypatch.setattr(semantic_transport, '_FMT_MODE', 'plain')
    monkeypatch.setattr(semantic_transport, '_FMT_TS', 1234.5)
    monkeypatch.setattr(semantic_transport, '_LLM_FAILURE', 'synthetic-failure')
    assert candidate._sources() == original


@pytest.mark.parametrize("surface", ["function", "class_method"])
def test_loaded_code_identity_ignores_sharing_and_interning_but_binds_nested_code(monkeypatch, surface):
    import sys
    import types
    module = types.ModuleType("synthetic_code_sharing_contract")
    exec('''def transform(values):
    def inner(): return "synthetic function text !"
    return inner, values[:3], values in {"synthetic alpha !", "synthetic beta !"}
class Surface:
    @classmethod
    def transform(cls, values):
        def inner(): return "synthetic method text ?"
        return inner, values[:3], values in {"synthetic gamma ?", "synthetic delta ?"}
''', module.__dict__)
    function = module.transform if surface == "function" else module.Surface.transform.__func__
    # Exercise slice constants even on interpreters predating literal-slice constant folding.
    function.__code__ = function.__code__.replace(co_consts=function.__code__.co_consts + (
        slice(None, 3, None), frozenset({"synthetic retained text !", "synthetic retained text ?"})))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(candidate, "_MODULES", (module.__name__,))
    before = candidate._sources()
    original = function.__code__
    shared = []

    def retain(value):
        shared.append(value)
        if isinstance(value, str):
            shared.append(sys.intern(value))
        elif isinstance(value, types.CodeType):
            retain(value.co_consts)
        elif isinstance(value, (tuple, frozenset)):
            for item in value:
                retain(item)
        elif isinstance(value, slice):
            for item in (value.start, value.stop, value.step):
                retain(item)
    retain(original)
    assert shared and function.__code__ is original
    assert candidate._sources() == before
    # An actual nested definition change must still invalidate the same outer definition.
    nested = next(value for value in original.co_consts if isinstance(value, types.CodeType))
    changed = nested.replace(co_consts=nested.co_consts + ("synthetic definition changed",))
    function.__code__ = original.replace(co_consts=tuple(
        changed if value is nested else value for value in original.co_consts))
    assert candidate._sources() != before


def test_loaded_code_identity_distinguishes_slice_from_similarly_named_tuple(monkeypatch):
    import sys
    import types
    module = types.ModuleType("synthetic_code_constant_types")
    exec("def transform(value): return value", module.__dict__)
    original = module.transform.__code__
    module.transform.__code__ = original.replace(co_consts=original.co_consts + (slice(None, 3, None),))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(candidate, "_MODULES", (module.__name__,))
    before = candidate._sources()
    module.transform.__code__ = original.replace(co_consts=original.co_consts + (("slice", None, 3, None),))
    assert candidate._sources() != before


@pytest.mark.parametrize("module_name,constant", [("llm_availability", "_CIRCUIT_TRIP"),
    ("self_identity", "SELF_PROFILE_KIND")])
def test_moved_core_constants_change_only_new_worker_identity(monkeypatch, module_name, constant):
    import importlib
    module = importlib.import_module(module_name)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    value = getattr(module, constant)
    monkeypatch.setattr(module, constant, value + 1 if type(value) is int else value + "_synthetic")
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


def test_mutable_availability_files_do_not_change_loaded_source_identity(tmp_path):
    import sqlite3
    import llm_availability
    db = sqlite3.connect(tmp_path / "ledger.db")
    try:
        before = candidate._sources()
        llm_availability.circuit_failure(db)
        assert candidate._sources() == before
        llm_availability.circuit_success(db)
        assert candidate._sources() == before
    finally:
        db.close()


@pytest.mark.parametrize("name", ["acquire_run_lock", "code_stamp", "updater_state", "unlocked_transport", "wait_run_lock", "RunLockLost.__init__"])
def test_writer_definition_changes_only_new_worker_identity(monkeypatch, name):
    import writer_lock
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    def changed(*args, **kwargs):
        raise AssertionError("definition fingerprint must not execute lock operations")
    changed.__module__ = writer_lock.__name__
    owner, attr = (writer_lock.RunLockLost, "__init__") if "." in name else (writer_lock, name)
    monkeypatch.setattr(owner, attr, changed)
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("constant", ["RUN_LOCK_REACQUIRE_S", "RUN_LOCK_POLL_S", "UPDATE_MARKER_NAME"])
def test_writer_policy_constant_changes_only_new_worker_identity(monkeypatch, constant):
    import writer_lock
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    old = getattr(writer_lock, constant)
    monkeypatch.setattr(writer_lock, constant, old + 1 if isinstance(old, float) else old + "_synthetic")
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before



@pytest.mark.parametrize("name", ["_MCS_ROOT", "RUN_LOCK"])
def test_checkout_and_run_lock_paths_do_not_change_candidate_identity(monkeypatch, tmp_path, name):
    import writer_lock
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    monkeypatch.setattr(writer_lock, name, str(tmp_path / "different-checkout-or-data-root"))
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) == before


def test_live_writer_marker_and_descriptor_do_not_change_definition_identity(tmp_path, monkeypatch):
    import writer_lock
    monkeypatch.setattr(writer_lock, "HOME", str(tmp_path))
    (tmp_path / "data").mkdir()
    before = candidate._sources()
    path = tmp_path / "run.lock"
    fd = writer_lock.acquire_run_lock(str(path))
    try:
        (tmp_path / "data" / writer_lock.UPDATE_MARKER_NAME).write_text("synthetic update")
        assert candidate._sources() == before
    finally:
        import os
        os.close(fd)
    assert candidate._sources() == before


@pytest.mark.parametrize('name', [
    'semantic_text_line', 'semantic_chunks', 'semantic_chunk_parts', 'semantic_summary_block',
])
def test_moved_semantic_presentation_invalidates_only_new_worker_approval(monkeypatch, name):
    import semantic_render

    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    cfg = approved_config({'semantic': {'fact_source': 'canonical'}})
    original = candidate.candidate_fingerprint(cfg)
    assert not semantic_config(cfg)[1]

    def changed(*args, **kwargs):
        raise AssertionError('synthetic moved renderer is fingerprinted, never called')

    changed.__module__ = semantic_render.__name__
    monkeypatch.setattr(semantic_render, name, changed)
    assert candidate.candidate_fingerprint(cfg) == original
    assert not semantic_config(cfg)[1]
    monkeypatch.setattr(candidate, '_LOADED_SOURCES', None)
    assert candidate.candidate_fingerprint(cfg) != original
    settings, errors = semantic_config(cfg)
    assert errors and settings['fact_source'] == 'legacy'


@pytest.mark.parametrize("owner,name", [("semantic_store", "current_artifact"),
    ("semantic_v4", "invalidate_projections"), ("semantic_projection", "projection_transition"),
    ("semantic_facts", "doc_hash"), ("semantic_audit", "published_quantity_findings")])
def test_projection_lifecycle_definition_changes_only_new_worker_identity(monkeypatch, owner, name):
    import importlib
    module = importlib.import_module(owner)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    def changed(*args, **kwargs):
        raise AssertionError("identity checks must not execute publication or I/O")
    changed.__module__ = owner
    monkeypatch.setattr(module, name, changed)
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("module_name,name", [("clinical_values", "absolute_date"), ("semantic_facts", "entity_tokens"),
    *[("clinical_urgency", name) for name in ("clinical_urgency_quote", "urgent_request_quote",
      "patient_clinical_quotes", "patient_rule_urgency_quotes", "_typed_urgency_reasons",
      "message_urgency", "message_urgency_details", "urgency_qc_disagreement")]])
def test_source_value_definition_changes_only_new_worker_identity(monkeypatch, module_name, name):
    import importlib
    module = importlib.import_module(module_name)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    def changed(*args):
        raise AssertionError("candidate identity never executes source parsers")
    changed.__module__ = module_name
    monkeypatch.setattr(module, name, changed)
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("owner,name", [("semantic_facts", "cached_facts_plan"),
    ("semantic_audit", "cached_fact_audit"), ("semantic_audit", "canonical_audit_disposition"),
    ("semantic_audit", "fact_repair_plan"), ("semantic_audit", "fact_audit_verdict"),
    ("semantic_v4_cohort", "hold_unbounded_job"), ("semantic_v4_cohort", "_valid_cohort"),
    ("clinical_values", "vital_threshold_flags"), ("clinical_values", "prose_preserves_current_readings"),
    ("semantic_notice", "origin_event_id"), ("semantic_notice", "record_notice_plan_tx"),
    ("semantic_send_gate", "semantic_gate"),
    ("semantic_v4", "publish_fact_projection"), ("semantic_v4", "publish_target_tx"), ("semantic_runtime", "guard"),
    ("semantic_runtime", "job_matches"), ("semantic_runtime", "llm_request_was_dispatched"), ("semantic_runtime", "JobToken.from_row")])
def test_canonical_reuse_and_publication_definition_pins_loaded_worker(monkeypatch, owner, name):
    import importlib
    module = importlib.import_module(owner)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    def changed(*args, **kwargs):
        raise AssertionError("identity must not evaluate publication code")
    changed.__module__ = owner
    if name == "JobToken.from_row":
        monkeypatch.setattr(module.JobToken, "from_row", classmethod(changed))
    else:
        monkeypatch.setattr(module, name, changed)
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("owner,cls,method", [("semantic_jev", "JevClient", "request"),
    ("semantic_jev", "JevClient", "evaluate"), ("semantic_runtime", "JevEvaluation", "evaluate")])
def test_jev_execution_definition_changes_only_new_worker_identity(monkeypatch, owner, cls, method):
    import importlib
    module = importlib.import_module(owner)
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    before = candidate.candidate_fingerprint({})
    def changed(*args, **kwargs):
        raise AssertionError("fingerprint must not dispatch transport")
    monkeypatch.setattr(getattr(module, cls), method, changed)
    assert candidate.candidate_fingerprint({}) == before
    monkeypatch.setattr(candidate, "_LOADED_SOURCES", None)
    assert candidate.candidate_fingerprint({}) != before


@pytest.mark.parametrize("fault", ["adoption", "serialize", "fsync"])
def test_approval_receipt_owns_descriptor_on_every_preparation_failure(
        tmp_path, monkeypatch, fault):
    import errno
    import os
    import stat
    import mcs_util
    import safe_io

    monkeypatch.setattr(mcs_util, "HOME", str(tmp_path))
    cfg = {}
    report = {"criteria_sha256": "a" * 64,
              "run": {"snapshot_sha256": "b" * 64},
              "evidence": {"holdout_manifest_sha256": "c" * 64}}
    descriptors = []
    opening = os.open

    def track_open(path, flags, mode=0o777, **kwargs):
        descriptor = opening(path, flags, mode, **kwargs)
        if flags & os.O_EXCL:
            descriptors.append(descriptor)
        return descriptor

    def fail(*args, **kwargs):
        raise OSError("synthetic approval fault")

    monkeypatch.setattr(os, "open", track_open)
    if fault == "adoption":
        monkeypatch.setattr(safe_io.os, "fdopen", fail)
    elif fault == "serialize":
        monkeypatch.setattr(candidate.json, "dump", fail)
    else:
        monkeypatch.setattr(os, "fsync", fail)
    try:
        with pytest.raises(OSError, match="synthetic approval fault"):
            candidate.record_approval(cfg, report)
        assert len(descriptors) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(descriptors[0])
        assert closed.value.errno == errno.EBADF
        # Preserve exclusive receipt recovery: failures never delete/overwrite it.
        receipts = list((tmp_path / "data" / "semantic_approvals").glob("*.json"))
        assert len(receipts) == 1
        assert stat.S_IMODE(receipts[0].stat().st_mode) == 0o600
        assert candidate.approval_matches(cfg, report, receipts[0].stem) is (fault == "fsync")
        if fault != "fsync":
            with pytest.raises(ValueError, match="approval_receipt_conflict"):
                candidate.record_approval(cfg, report)
        else:
            assert candidate.record_approval(cfg, report) == receipts[0].stem
    finally:
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
