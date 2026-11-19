"""Finals bound under a retired generation count as patient-context material
only when the owner marks that generation (fully synthetic)."""
import json

import pytest

import extract_llm
import extract_llm_historical
import extract_llm_progress
import inference_dependency_loss
from extract_llm_source_contract import _INTERNAL_LEAF_CLAIMS
from extract_testkit import _ledger, _message
from extraction_provenance import provider_result_hash
from inference_version import CURRENT_VERSION, VERSION_KEY

MODEL = 'synthetic'
BODY = '生活環境: 合成の居住記録。'


@pytest.fixture
def led(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.db.execute('INSERT OR IGNORE INTO patients(project_id,patient_name) VALUES (1,?)',
                      ('synthetic-1',))
    ledger.db.commit()
    ledger.save_messages([_message(1, BODY, 'full')])
    yield ledger
    ledger.close()


def _row(led):
    row = dict(led.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row['_source_binding'] = inference_dependency_loss.source_binding(led.db, 1, 1)
    row['_target'] = ('', MODEL)
    return row


def _final(led, *, generation=None, binding=None, leaf_claims=True,
           document=None, extra_meta=None):
    """A current final whose context binding uses ``generation`` (None = live)."""
    row = _row(led)
    context = extract_llm._thread_context(led.db, row)
    chunks = extract_llm.plan_chunks(row['body_text'], extract_llm._saved_chunk_size(led.db, row, context))
    claims = {str(i): {'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}
              for i in range(len(chunks))}
    if binding is None:
        kw = {} if generation is None else {'prompt_head': generation[1], 'schema': generation[2]}
        binding = extract_llm._chunk_context(row, context, **kw)
    content = dict(document or {'summary': '合成の経過報告'})
    if leaf_claims:
        content[_INTERNAL_LEAF_CLAIMS] = claims
    raw = json.dumps(content, ensure_ascii=False)
    meta = {VERSION_KEY: CURRENT_VERSION, 'hash': row['content_hash'],
            'extract_version': extract_llm.EXTRACT_VERSION, 'actual_model': MODEL,
            'plan_version': extract_llm.PLAN_VERSION,
            'source_binding': row['_source_binding'], 'thin_retried': True,
            'context_binding': binding,
            'integrity': {'completeness_chunks': len(chunks), 'completeness': claims},
            'provider_provenance': {'version': 1, 'stage': 'extract',
                                    'source_binding': row['_source_binding'],
                                    'result_hash': provider_result_hash(raw),
                                    'complete': True, 'providers': ['local']},
            **(extra_meta or {})}
    led.artifact_add('extract_llm', raw, project_id=1, message_id=1, model=MODEL, meta=meta)
    return _row(led)


def _material(led, row):
    return extract_llm_progress.material_proof_for_message(led.db, row, model=MODEL)['state']


def _old():
    return extract_llm_historical.known_old_contracts()[0]


def _enable(monkeypatch, *gens):
    monkeypatch.setattr(extract_llm_historical, 'material_contracts', lambda: gens)


def test_live_binding_fixture_is_complete_material(led):
    assert _material(led, _final(led)) == 'complete'


@pytest.mark.parametrize('generation_index', [0, 1])
def test_shipped_contracts_mark_both_retired_generations(led, generation_index):
    # Owner decision 2026-11-13 #1: both retired generations are material, leaf-sealed finals only.
    assert extract_llm_historical.material_contracts() == extract_llm_historical.known_old_contracts()
    assert len(extract_llm_historical.known_old_contracts()) == 2
    assert all(len(gen) == 3 for gen in extract_llm_historical.known_old_contracts())
    generation = extract_llm_historical.material_contracts()[generation_index]
    assert _material(led, _final(led, generation=generation)) == 'complete'


def test_shipped_marks_still_require_a_leaf_proof(led):
    assert _material(led, _final(led, generation=_old(), leaf_claims=False)) != 'complete'


def test_marked_generation_admits_leaf_sealed_final(led, monkeypatch):
    _enable(monkeypatch, _old())
    assert _material(led, _final(led, generation=_old())) == 'complete'


def test_retired_binding_does_not_authenticate_a_local_rebuild(led):
    row = _final(led, generation=_old())
    assert _material(led, row) == 'complete'
    authenticity = extract_llm_progress.material_proof_for_message(
        led.db, row, model=MODEL, adverse_review=False)
    assert authenticity['state'] != 'complete'


def test_unmarked_generation_is_not_admitted(led, monkeypatch):
    _enable(monkeypatch, extract_llm_historical.known_old_contracts()[1])
    assert _material(led, _final(led, generation=_old())) != 'complete'


def test_ambiguous_generation_is_not_admitted(led, monkeypatch):
    gen_id, head, schema = _old()
    _enable(monkeypatch, (gen_id, head, schema), (gen_id + '-copy', head, schema))
    assert _material(led, _final(led, generation=_old())) != 'complete'


@pytest.mark.parametrize('case', [
    {'binding': 'f' * 64},
    {'leaf_claims': False},
    {'document': {'summary': '合成の経過報告', 'vitals': {'hr': True}}},
    {'extra_meta': {'qc_fix': {'applied': False}}},
    {'extra_meta': {'human_fix': {'applied': False}}},
    {'extra_meta': {'provider_provenance': {'result_hash': '0' * 64}}},
])
def test_marked_generation_keeps_every_other_gate(led, monkeypatch, case):
    _enable(monkeypatch, _old())
    kw = {'generation': _old(), **case}
    assert _material(led, _final(led, **kw)) != 'complete'


def test_progress_path_is_unchanged_when_marked(led, monkeypatch):
    row = _final(led, generation=_old())
    before = extract_llm_progress.progress_for_message(led.db, row, model=MODEL)
    _enable(monkeypatch, _old())
    after = extract_llm_progress.progress_for_message(led.db, row, model=MODEL)
    assert after == before and after['state'] != 'complete'


def test_loader_reads_material_flag(tmp_path, monkeypatch):
    doc = json.loads(extract_llm_historical._CONTRACTS_PATH.read_text(encoding='utf-8'))
    for g in doc['generations']:  # the shipped file marks both owner-approved generations
        g['material'] = False
    doc['generations'][0]['material'] = True
    path = tmp_path / 'contracts.json'
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(extract_llm_historical, '_CONTRACTS_PATH', path)
    monkeypatch.setattr(extract_llm_historical, '_KNOWN_OLD', None)
    gen = doc['generations'][0]
    assert extract_llm_historical.material_contracts() == ((gen['id'], gen['prompt_head'], gen['schema']),)
    assert len(extract_llm_historical.known_old_contracts()) == len(doc['generations'])
    assert all(len(g) == 3 for g in extract_llm_historical.known_old_contracts())


def test_loader_rejects_non_bool_material(tmp_path, monkeypatch):
    doc = json.loads(extract_llm_historical._CONTRACTS_PATH.read_text(encoding='utf-8'))
    doc['generations'][0]['material'] = 'yes'
    path = tmp_path / 'contracts.json'
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setattr(extract_llm_historical, '_CONTRACTS_PATH', path)
    monkeypatch.setattr(extract_llm_historical, '_KNOWN_OLD', None)
    with pytest.raises(ValueError, match='historical_contracts_invalid'):
        extract_llm_historical.material_contracts()
