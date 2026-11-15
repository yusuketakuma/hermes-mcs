"""OCR request recovery separates raw cloud inputs from local adoption generations."""
from contextvars import Context
import fcntl
import json
import os
import time

import pytest

import attachment_extraction
import attachment_inference as worker
import attachment_ocr
import cloud_policy
import local_llm
from anthropic_client import AnthropicClient
from mcs_util import payload_hash
from test_attachment_inference import page_store as page_store, run
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_cloud_response_recovery import legacy_cloud_routes
from test_anthropic_runtime import routed  # noqa: F401


def _legacy(source, local):
    return payload_hash(['attachment-inference/v1', source['_source_binding'], local['artifact_id'],
                         local['result_hash'], local['contract']])


def _financial(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


@pytest.mark.parametrize('legacy', [False, True])
def test_contract_upgrade_revalidates_paid_answer_without_another_http(page_store, monkeypatch, legacy):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    generation = ['a' * 64]
    monkeypatch.setattr(attachment_extraction, 'contract', lambda *a, **kw: generation[0])
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with monkeypatch.context() as old:
        if legacy:
            old.setattr(worker, '_input_binding', _legacy)
        with legacy_cloud_routes():
            assert run(ledger, cfg, 'cloud')['done'] == 1
    paid = _financial(ledger)
    previous = worker.current(ledger.db, 1, key, cfg=cfg)
    generation[0] = 'b' * 64
    assert run(ledger, cfg, 'local')['done'] == 1
    local = worker.current(ledger.db, 1, key, cfg=cfg)
    assert not local['ready'] and local['artifact_id'] != previous['local_artifact_id']
    assert run(ledger, cfg, 'cloud')['done'] == 1
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    assert result['ready'] and result['local_artifact_id'] == local['artifact_id']
    assert len(cloud_calls) == 1 and len(local_calls) == 2 and _financial(ledger) == paid


@pytest.mark.parametrize('fault', ['scope', 'old_local_proof', 'discarded'])
def test_legacy_success_cannot_be_recovered_without_current_scope_and_original_proof(page_store, monkeypatch, fault):
    ledger, cfg, key, _local_calls, cloud_calls, _ = page_store
    generation = ['a' * 64]
    monkeypatch.setattr(attachment_extraction, 'contract', lambda *a, **kw: generation[0])
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with monkeypatch.context() as old:
        old.setattr(worker, '_input_binding', _legacy)
        with legacy_cloud_routes():
            assert run(ledger, cfg, 'cloud')['done'] == 1
    original = worker.current(ledger.db, 1, key, cfg=cfg)
    if fault == 'scope':
        cfg['cloud_llm']['project_ids'] = [1, 2]
    elif fault == 'old_local_proof':
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET content=json_set(content,'$.result_hash',?) WHERE artifact_id=?",
                              ('0' * 64, original['local_artifact_id']))
    else:
        reserve = ledger.artifacts('cloud_usage_reserve')[0]
        with ledger.db:
            ledger.db.execute('BEGIN IMMEDIATE')
            cloud_policy.record_disposition_tx(ledger, json.loads(reserve['meta']), 'discarded_scope')
    generation[0] = 'b' * 64
    assert run(ledger, cfg, 'local')['done'] == 1
    report = run(ledger, cfg, 'cloud')
    assert report['done'] == 0 and report['deferred'] == 1
    assert len(cloud_calls) == 1 and not worker.current(ledger.db, 1, key, cfg=cfg)['ready']


def test_same_source_unknown_cannot_be_resent_after_generation_change(page_store, monkeypatch):
    ledger, cfg, key, _local_calls, _cloud_calls, _ = page_store
    generation = ['a' * 64]
    monkeypatch.setattr(attachment_extraction, 'contract', lambda *a, **kw: generation[0])
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    sends = []

    def unknown(self, method, body, timeout, **kwargs):
        sends.append(body)
        raise KeyboardInterrupt('synthetic unknown transport')

    monkeypatch.setattr(AnthropicClient, 'request', unknown)
    with monkeypatch.context() as old:
        old.setattr(worker, '_input_binding', _legacy)
        with pytest.raises(KeyboardInterrupt):
            with legacy_cloud_routes():
                run(ledger, cfg, 'cloud')
    paid = _financial(ledger)
    generation[0] = 'b' * 64
    assert run(ledger, cfg, 'local')['done'] == 1
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(sends) == 1 and _financial(ledger) == paid
    assert not worker.current(ledger.db, 1, key, cfg=cfg)['ready']
    previous = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
    candidate = {**previous, 'model': 'gpt-6.1-sol'}
    with pytest.raises(cloud_policy.AnthropicError) as error:
        cloud_policy._guard_repeated_request(ledger, candidate, current_body=sends[0])
    assert error.value.detail == 'cloud_unit_unresolved'


def _two_pages(ledger, cfg, key):
    artifact_id = int(key.split(':')[1])
    raw = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()[0])
    raw['pages'].append({**raw['pages'][0], 'page': 2})
    raw['text_hash'] = attachment_ocr._hash(json.dumps(raw['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(raw), artifact_id))
    for _ in range(4):
        run(ledger, cfg, 'local')
    cfg['cloud_llm']['mode'] = 'on'
    return artifact_id


def test_identical_pages_have_distinct_verified_cloud_owners(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    artifact_id = _two_pages(ledger, cfg, key)
    for _ in range(5):
        with legacy_cloud_routes():
            run(ledger, cfg, 'cloud')
    assert all(worker.current(ledger.db, 1, f'attachment_ocr:{artifact_id}:{page}', cfg=cfg)['ready'] for page in (1, 2))
    assert len(local_calls) == len(cloud_calls) == 2


def test_unknown_on_one_page_cannot_block_a_proven_different_page(page_store, monkeypatch):
    ledger, cfg, key, _local_calls, cloud_calls, _ = page_store
    artifact_id = _two_pages(ledger, cfg, key)
    actual, attempts = AnthropicClient.request, []

    def first_unknown(self, method, body, timeout, **kwargs):
        attempts.append(body)
        if len(attempts) == 1:
            raise KeyboardInterrupt('synthetic unknown first page')
        return actual(self, method, body, timeout, **kwargs)

    monkeypatch.setattr(AnthropicClient, 'request', first_unknown)
    with pytest.raises(KeyboardInterrupt):
        with legacy_cloud_routes():
            run(ledger, cfg, 'cloud')
    for _ in range(6):
        with legacy_cloud_routes():
            run(ledger, cfg, 'cloud')
    assert len(attempts) == 2 and len(cloud_calls) == 1
    assert not worker.current(ledger.db, 1, key, cfg=cfg)['ready']
    assert worker.current(ledger.db, 1, f'attachment_ocr:{artifact_id}:2', cfg=cfg)['ready']


def test_a_different_source_key_without_its_local_proof_cannot_bypass_send_guard(page_store):
    ledger, cfg, key, _local_calls, cloud_calls, _ = page_store
    artifact_id = _two_pages(ledger, cfg, key)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['done'] == 1
    previous = json.loads(ledger.artifacts('cloud_usage_reserve')[0]['meta'])
    # A user-supplied different key cannot turn page one's local proof into page two's proof.
    candidate = {**previous, 'unit_key': f'attachment_ocr:{artifact_id}:2:cloud',
                 'owner': {**previous['owner'], 'source_key': f'attachment_ocr:{artifact_id}:2'}}
    with pytest.raises(cloud_policy.AnthropicError) as error:
        cloud_policy._guard_repeated_request(ledger, candidate, current_body=cloud_calls[0])
    assert error.value.detail == 'cloud_unit_unresolved'


@pytest.mark.parametrize('legacy', [False, True])
def test_old_and_stable_paid_journals_recover_across_contract_upgrade(page_store, monkeypatch, legacy):
    import attachment_paid_results as journal
    from test_attachment_inference_recovery import _lose_accounting_lock

    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    generation = ['a' * 64]
    monkeypatch.setattr(attachment_extraction, 'contract', lambda *a, **kw: generation[0])
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with monkeypatch.context() as old:
        if legacy:
            old.setattr(worker, '_input_binding', _legacy)
        with legacy_cloud_routes():
            assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(journal.pending(attachment_ocr._data_root(ledger))) == 1
    assert len(cloud_calls) == 1 and not ledger.artifacts('cloud_usage_result')
    generation[0] = 'b' * 64
    unavailable[0] = False
    assert run(ledger, cfg, 'local')['done'] == 1
    paid = _financial(ledger)
    assert not journal.pending(attachment_ocr._data_root(ledger))
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready']
    assert len(cloud_calls) == 1 and len(local_calls) == 2 and _financial(ledger) == paid


@pytest.mark.parametrize('legacy', [False, True])
def test_paid_journal_with_rehashed_unknown_scope_is_not_replayed(page_store, monkeypatch, legacy):
    import attachment_paid_results as journal
    from test_attachment_inference_recovery import _lose_accounting_lock

    ledger, cfg, key, _local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with monkeypatch.context() as old:
        if legacy:
            old.setattr(worker, '_input_binding', _legacy)
        with legacy_cloud_routes():
            assert run(ledger, cfg, 'cloud')['deferred'] == 1
    path, = journal.pending(attachment_ocr._data_root(ledger))
    value = json.loads(path.read_text())
    value['origin']['scope'] = '0' * 64
    value['hash'] = payload_hash({key: data for key, data in value.items() if key != 'hash'})
    path.write_text(json.dumps(value))
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(cloud_calls) == 1 and not ledger.artifacts('cloud_usage_result') and path.exists()
    assert not worker.current(ledger.db, 1, key, cfg=cfg)['ready']


def test_corrupt_paid_journal_keeps_cloud_closed_but_allows_local(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    directory = attachment_ocr._data_root(ledger) / 'attachment_paid_results'
    directory.mkdir(mode=0o700)
    (directory / 'invalid-name').write_text('synthetic corrupt journal')
    result = run(ledger, cfg, 'local')
    assert result['done'] == 1 and result['paid_recovery_pending'] is True
    assert len(local_calls) == 1 and not cloud_calls
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud') == {'done': 0, 'deferred': 1}
    assert (directory / 'invalid-name').read_text() == 'synthetic corrupt journal'
    with legacy_cloud_routes():
        assert not worker.current(ledger.db, 1, key, cfg=cfg)['ready']


def test_ocr_local_call_uses_context_background_lane_without_environment_mutation(page_store, monkeypatch):
    ledger, cfg, _key, *_ = page_store
    monkeypatch.setenv('MCS_LLM_SLOT', '0')
    monkeypatch.delenv(local_llm.TURN_LOCK_ENV, raising=False)
    actual = worker.extract_llm.llm_extract
    seen = []

    def observed(*args, **kwargs):
        seen.append((local_llm.request_slot(), local_llm._turn_path()))
        assert os.environ['MCS_LLM_SLOT'] == '0' and local_llm.TURN_LOCK_ENV not in os.environ
        assert Context().run(local_llm.request_slot) == local_llm.REALTIME_SLOT
        return actual(*args, **kwargs)

    monkeypatch.setattr(worker.extract_llm, 'llm_extract', observed)
    assert run(ledger, cfg, 'local')['done'] == 1
    assert seen == [(local_llm.BACKGROUND_SLOT, str(attachment_ocr._data_root(ledger) / 'llm_turn.lock'))]
    assert local_llm.request_slot() == local_llm.REALTIME_SLOT and local_llm._turn_path() is None


def test_background_scope_holds_the_existing_shared_turn_lock(tmp_path, monkeypatch):
    monkeypatch.setenv('MCS_LLM_SLOT', '0')
    monkeypatch.delenv(local_llm.TURN_LOCK_ENV, raising=False)
    with local_llm.background_scope(tmp_path), local_llm._turn(time.monotonic() + 1) as held:
        assert held
        with open(tmp_path / 'llm_turn.lock', 'rb') as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert local_llm.request_slot() == local_llm.REALTIME_SLOT
