"""Synthetic split history recognizes its exact prior provider without adopting old results."""
import extract_llm_store
import hashlib
import json

import pytest
import anthropic_client
import cloud_policy

import cloud_inference
import extract_llm as worker
import extraction_context as frames
import openai_client
from extract_testkit import _ledger, _message
from inference_dependency_loss import source_binding
from inference_version import CURRENT_VERSION, VERSION_KEY


def contracts():
    model = 'gpt-6-luna'
    choices = [cloud_inference.cloud_provider_contract(selection={
        'provider': 'openai', 'model': model, 'effort': effort})
        for selected, effort in sorted(openai_client.VALID_MODEL_EFFORTS) if selected == model]
    assert len(choices) >= 2
    return choices[0], choices[1]


def history(ledger):
    prior, current = contracts()
    ledger.save_messages([_message(body='完全合成の対象記録です。' * 20)])
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1),
               _target=('', prior['model']), _provider_contract=prior, _cloud_mode='on',
               qc_src=None, thin_src=None, human_src=None, attempts=0)
    attempt = {'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0}
    frame = frames.prepare(ledger, row, model=prior['model'], contract=worker._context_contract(),
        attempt=attempt, enabled=False, base_context=None, version=worker.EXTRACT_VERSION)
    row['_frame'] = frame
    meta = {'hash': row['content_hash'], 'ver': worker.EXTRACT_VERSION, VERSION_KEY: CURRENT_VERSION,
            'context': worker._chunk_context(row, None, checkpoint=True), 'chunk': 0,
            'chunk_size': worker._CHUNK_SIZE, 'split_size': 25,
            'piece_sha256': hashlib.sha256(row['body_text'].encode()).hexdigest(),
            'actual_model': prior['model'], 'plan_version': worker.PLAN_VERSION}
    aid = ledger.artifact_add('extract_llm_chunk', '{}', project_id=1, message_id=1,
                              model=prior['model'], meta=meta)
    row['_provider_contract'] = current
    return row, frame, attempt, aid


def test_legacy_split_after_same_model_profile_change_recognizes_only_original_frame(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        row, frame, attempt, _aid = history(ledger)
        before = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        current = dict(row['_provider_contract'])
        found = worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False)
        assert found == frame and row['_provider_contract'] == current
        assert [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
        # Recognizing history does not load the prior profile's split or clinical results into the current profile.
        assert worker._saved_chunk_state(ledger.db, row, None, chunk_size=worker._CHUNK_SIZE) == ({}, {})
    finally:
        ledger.close()


def test_legacy_layout_after_profile_change_is_recognized_without_adopting_it(tmp_path):
    ledger = _ledger(tmp_path)
    try:
        row, frame, attempt, aid = history(ledger)
        saved = ledger.artifacts('extract_llm_chunk', message_id=1)[0]
        meta = json.loads(saved['meta'])
        meta.pop('split_size')
        meta['chunk'] = -1
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=? WHERE artifact_id=?', (json.dumps(meta), aid))
        assert worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False) == frame
        assert worker._saved_chunk_size(ledger.db, row, None) == worker._CHUNK_SIZE
    finally:
        ledger.close()


@pytest.mark.parametrize('damage', ['unknown_explicit', 'local_explicit', 'invalid_leaf', 'contradictory_leaf',
    'frame_id', 'frame_digest', 'repair_proof', 'split_type', 'split_size', 'model', 'context', 'untyped', 'duplicate_frame'])
def test_invalid_or_ambiguous_history_never_falls_back_to_another_contract(tmp_path, damage):
    ledger = _ledger(tmp_path)
    try:
        row, frame, attempt, aid = history(ledger)
        saved = dict(ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (aid,)).fetchone())
        meta = json.loads(saved['meta'])
        content = {}
        if damage == 'unknown_explicit':
            meta['checkpoint_provider_contract'] = {**contracts()[0], 'prompt': 'f' * 16}
        elif damage == 'local_explicit':
            meta['checkpoint_provider_contract'] = None
        elif damage == 'invalid_leaf':
            meta['provider_leaf'] = {}
        elif damage == 'contradictory_leaf':
            meta['provider_leaf'] = {'version': 1, 'providers': ['openai'], 'complete': True,
                                     'provider_contract': contracts()[0]}
        elif damage == 'frame_id':
            meta['context_snapshot'] = frame['artifact_id'] + 100
        elif damage == 'frame_digest':
            meta['context_digest'] = 'f' * 64
        elif damage == 'repair_proof':
            content['__quality_repair_v1'] = {'version': 1}
        elif damage == 'split_type':
            meta['split_size'] = True
        elif damage == 'split_size':
            meta['split_size'] = meta['chunk_size']
        elif damage == 'model':
            meta['actual_model'] = 'unsupported-synthetic-model'
        elif damage == 'context':
            meta['context'] = 'f' * 64
        elif damage == 'untyped':
            meta.pop('split_size')
        elif damage == 'duplicate_frame':
            original = ledger.db.execute('SELECT * FROM artifacts WHERE artifact_id=?', (frame['artifact_id'],)).fetchone()
            ledger.artifact_add(frames.KIND, original['content'], project_id=1, message_id=1,
                                model=original['model'], meta=json.loads(original['meta']))
        with ledger.db:
            ledger.db.execute('UPDATE artifacts SET meta=?,content=? WHERE artifact_id=?',
                              (json.dumps(meta), json.dumps(content), aid))
        before = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        with pytest.raises(ValueError, match='pending_context_unverifiable'):
            worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False)
        assert [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    finally:
        ledger.close()


def test_new_split_seals_original_provider_and_frame_but_keeps_current_reader_separate(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', worker._rerender_cards)
    try:
        row, frame, attempt, _aid = history(ledger)
        row['_provider_contract'] = contracts()[0]
        # The shared producer, not a manually fabricated metadata shape, seals future plans.
        lease = worker._claim(ledger, row)
        assert lease is not None
        worker._capture_checkpoint_state(ledger.db, row, None)
        assert worker._persist_chunks(ledger, row, {0: {}}, None, chunk_size=worker._CHUNK_SIZE,
            split_size=20, splits=row['_splits'], lease=lease, guard=row['_checkpoint_guard'])
        newest = ledger.db.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id DESC LIMIT 1").fetchone()
        meta = json.loads(newest[0])
        assert meta['checkpoint_provider_contract'] == contracts()[0]
        assert {key: meta[key] for key in worker._frame_meta(row)} == worker._frame_meta(row)
        row['_provider_contract'] = contracts()[1]
        before = [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
        assert worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False) == frame
        assert worker._saved_chunk_state(ledger.db, row, None, chunk_size=worker._CHUNK_SIZE) == ({}, {})
        assert [tuple(r) for r in ledger.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    finally:
        ledger.close()


@pytest.mark.parametrize('outcome', ['unknown', 'settled'])
def test_recognizing_prior_split_never_relaxes_financial_resend_proof(tmp_path, monkeypatch, outcome):
    from test_cloud_resend_guard import _reserve, _result, _medium_selection, _openai_usage
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    try:
        row, frame, attempt, _aid = history(ledger)
        old = _reserve(ledger, 'synthetic prompt', 'openai', route='reextract', unit='old')
        metadata = _result(old, 'timeout' if outcome == 'unknown' else 'http_200',
                           'timeout' if outcome == 'unknown' else None)
        if outcome == 'settled':
            metadata['usage'] = _openai_usage()
        cloud_policy.record_result(ledger, old, metadata)
        before = [tuple(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
            "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")]
        assert worker._pending_context_frame(ledger.db, row, attempt=attempt, enabled=False) == frame
        assert [tuple(r) for r in ledger.db.execute("SELECT * FROM artifacts WHERE kind IN "
            "('cloud_usage_reserve','cloud_usage_result') ORDER BY artifact_id")] == before
        selected = _medium_selection('reextract')
        if outcome == 'unknown':
            with pytest.raises(anthropic_client.AnthropicError) as denied:
                _reserve(ledger, 'synthetic prompt', 'openai', route='reextract', unit='new',
                         selection=selected, max_tokens=selected['max_tokens'])
            assert denied.value.detail == 'cloud_unit_unresolved'
            assert len(ledger.artifacts('cloud_usage_reserve')) == 1
        else:
            _reserve(ledger, 'synthetic prompt', 'openai', route='reextract', unit='new',
                     selection=selected, max_tokens=selected['max_tokens'])
            assert len(ledger.artifacts('cloud_usage_reserve')) == 2
    finally:
        ledger.close()


def test_supported_contract_enumeration_is_finite_exact_and_returns_owned_values():
    expected = [cloud_inference.cloud_provider_contract(selection={
        'provider': 'openai', 'model': model, 'effort': effort})
        for model, effort in sorted(openai_client.VALID_MODEL_EFFORTS)]
    assert list(cloud_inference.supported_cloud_contracts('gpt-6-luna')) == [
        contract for contract in expected if contract['model'] == 'gpt-6-luna']
    for contract in cloud_inference.supported_cloud_contracts():
        assert cloud_inference.is_supported_cloud_contract(contract)
        assert not cloud_inference.is_supported_cloud_contract({**contract, 'extra': True})
    first = cloud_inference.supported_cloud_contracts('gpt-6-luna')[0]
    first['prompt'] = 'f' * 16
    assert not cloud_inference.is_supported_cloud_contract(first)
    assert cloud_inference.supported_cloud_contracts('unsupported-synthetic-model') == ()


def proof_history(ledger, monkeypatch):
    row, frame, attempt, _aid = history(ledger)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', worker._rerender_cards)
    row['_provider_contract'] = contracts()[0]
    lease = worker._claim(ledger, row)
    assert lease is not None
    worker._capture_checkpoint_state(ledger.db, row, None)
    value = worker._EvidenceLeaf({'symptoms': [], '_quality_result': {
        'state': 'hint_gaps_resolved', 'remaining_hint_candidates': 0}}, worker._provider_evidence(['openai'], True))
    assert worker._persist_chunks(ledger, row, {0: value}, None, chunk_size=worker._CHUNK_SIZE,
        splits=row['_splits'], lease=lease, guard=row['_checkpoint_guard'])
    row['_provider_contract'] = contracts()[1]
    return row, frame, attempt


def test_chunk_history_does_not_retain_raw_clinical_content_across_rows(tmp_path, monkeypatch):
    import weakref
    ledger = _ledger(tmp_path)
    try:
        row, frame, attempt = proof_history(ledger, monkeypatch)
        source = ledger.db.execute("SELECT * FROM artifacts WHERE kind='extract_llm_chunk' ORDER BY artifact_id DESC LIMIT 1").fetchone()
        for _ in range(8):
            ledger.artifact_add('extract_llm_chunk', source['content'], project_id=1, message_id=1,
                                model=source['model'], meta=json.loads(source['meta']))
        references, retained = [], []
        class ObservedContent(str):
            pass
        class Reader:
            def execute(self, sql, parameters=()):
                cursor = ledger.db.execute(sql, parameters)
                if sql.startswith('SELECT artifact_id,kind,project_id,message_id,model,meta,content ') \
                        and "WHERE kind='extract_llm_chunk'" in sql:
                    def stream():
                        for saved in cursor:
                            content = ObservedContent(saved[6])
                            references.append(weakref.ref(content))
                            yield (*saved[:6], content)
                    return stream()
                if sql.startswith('SELECT artifact_id,content,meta FROM artifacts WHERE kind='):
                    retained.append(sum(reference() is not None for reference in references))
                return cursor
        assert worker._pending_context_frame(Reader(), row, attempt=attempt, enabled=False) == frame
        assert len(references) == 10 and retained and max(retained) <= 1
    finally:
        ledger.close()


@pytest.mark.parametrize('changed', ['missing', 'kind', 'project_id', 'message_id', 'model', 'meta', 'content'])
def test_matching_history_refetch_cannot_join_different_row_snapshots(tmp_path, changed):
    ledger = _ledger(tmp_path)
    try:
        row, _frame, attempt, aid = history(ledger)
        ledger.save_messages([_message(mid=2, project_id=1 if changed == 'message_id' else 2,
                                       body='完全合成の別入力。')])
        altered = []
        class Reader:
            def execute(self, sql, parameters=()):
                if sql.startswith('SELECT artifact_id,kind,project_id,message_id,model,meta,content ') \
                        and 'WHERE artifact_id=?' in sql and parameters == (aid,) and not altered:
                    with ledger.db:
                        if changed == 'missing':
                            ledger.db.execute('DELETE FROM artifacts WHERE artifact_id=?', (aid,))
                        elif changed in ('kind', 'model'):
                            ledger.db.execute(f'UPDATE artifacts SET {changed}=? WHERE artifact_id=?',
                                              ('synthetic-changed', aid))
                        elif changed == 'project_id':
                            # Move to a valid different source pair; never disable the G1 relation guard.
                            ledger.db.execute('UPDATE artifacts SET project_id=2,message_id=2 WHERE artifact_id=?', (aid,))
                        elif changed == 'message_id':
                            ledger.db.execute('UPDATE artifacts SET message_id=2 WHERE artifact_id=?', (aid,))
                        else:
                            # Even formatting-only changes may not be stitched into the earlier snapshot.
                            ledger.db.execute(f"UPDATE artifacts SET {changed}={changed} || ' ' WHERE artifact_id=?", (aid,))
                    altered.append(True)
                return ledger.db.execute(sql, parameters)
        with pytest.raises(ValueError, match='pending_context_unverifiable'):
            worker._pending_context_frame(Reader(), row, attempt=attempt, enabled=False)
        assert altered == [True]
    finally:
        ledger.close()
