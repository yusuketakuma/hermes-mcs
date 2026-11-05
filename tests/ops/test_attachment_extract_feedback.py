"""Synthetic OCR feedback reopens only the pinned page and preserves paid uncertainty."""
import copy
import json
import uuid
from types import SimpleNamespace

import pytest

from adapters.common import envelopes, human_commands
import attachment_feedback
import attachment_inference as worker
from anthropic_client import AnthropicClient
import mcs_requests
import original_source_keys
from test_attachment_inference import page_store as base_page_store, run
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401

__all__ = ['base_page_store']


@pytest.fixture
def page_store(base_page_store):
    ledger = base_page_store[0]
    ledger.ensure_patient(1)
    with ledger.db:
        ledger.db.execute('UPDATE patients SET is_archived=0 WHERE project_id=1')
    return base_page_store


def _request(ledger, cfg, key):
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    return envelopes.extract_feedback('synthetic-operator', {'project_id': 1, 'extract_ref': {
        'message_id': 1, 'artifact_id': result['artifact_id'], 'source_key': key}},
        'meds', '完全合成のOCR誤り報告')


def _financial(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


def test_report_reextracts_only_ocr_once_and_rejects_old_checkpoints(page_store, monkeypatch):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    monkeypatch.setattr(attachment_feedback, 'load_config', lambda: cfg)
    assert run(ledger, cfg, 'local')['done'] == 1
    old = worker.current(ledger.db, 1, key, cfg=cfg)
    source = original_source_keys.source(ledger.db, 1, key)
    assert worker._local_checkpoints(ledger, source, cfg, worker.writer_lock.updater_state())['chunks_in']
    req = _request(ledger, cfg, key)
    assert 'source_key' in human_commands.CONTROL_FIELDS['extract_feedback']
    assert human_commands._control_reference(SimpleNamespace(db=ledger.db), req) is None
    first = mcs_requests.apply_command(ledger, req, cfg=cfg)
    assert first['outcome'] == 'applied'
    assert mcs_requests.apply_command(ledger, req, cfg=cfg) == first
    assert len(ledger.artifacts(attachment_feedback.KIND)) == 1
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert human_commands._control_reference(SimpleNamespace(db=ledger.db), req) == 'extraction_changed'
    assert not worker._local_checkpoints(ledger, source, cfg, worker.writer_lock.updater_state())['chunks_in']
    with pytest.raises(ValueError, match='source_changed'):
        worker._guard(ledger, source, cfg, worker.writer_lock.updater_state())
    assert run(ledger, cfg, 'local')['done'] == 1
    new = worker.current(ledger.db, 1, key, cfg=cfg)
    assert new['artifact_id'] > old['artifact_id'] and new['ready']
    assert len(local_calls) == 2 and not cloud_calls
    assert not ledger.artifacts('extract_feedback_v1')
    assert not ledger.artifacts('extract_llm')
    calls = len(local_calls)
    assert run(ledger, cfg, 'local')['done'] == 0
    assert len(local_calls) == calls
    assert mcs_requests.apply_command(ledger, {**req, 'command_id': str(uuid.uuid4())}, cfg=cfg)['error'] == 'extraction_changed'


def test_cloud_report_keeps_old_accounting_and_binds_new_cloud_to_new_local(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    assert run(ledger, cfg, 'cloud')['done'] == 1
    old = worker.current(ledger.db, 1, key, cfg=cfg)
    old_financial = _financial(ledger)
    req = _request(ledger, cfg, key)
    assert mcs_requests.apply_command(ledger, req, cfg=cfg)['outcome'] == 'applied'
    assert _financial(ledger) == old_financial
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert run(ledger, cfg, 'local')['done'] == 1
    local = worker.current(ledger.db, 1, key, cfg=cfg)
    assert local['stage'] == 'local' and not local['ready']
    assert local['artifact_id'] > old['local_artifact_id']
    assert run(ledger, cfg, 'cloud')['done'] == 1
    current = worker.current(ledger.db, 1, key, cfg=cfg)
    assert current['local_artifact_id'] == local['artifact_id'] and current['ready']
    assert _financial(ledger)[:len(old_financial)] == old_financial
    assert len(local_calls) == len(cloud_calls) == 2
    before = (len(local_calls), len(cloud_calls))
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert (len(local_calls), len(cloud_calls)) == before


@pytest.mark.parametrize('mutation', ['human', 'reason', 'foreign', 'parent', 'page', 'artifact', 'withdrawn', 'archived'])
def test_invalid_or_stale_ocr_feedback_never_reopens_source(page_store, mutation):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    req = _request(ledger, cfg, key)
    if mutation == 'human':
        req['human_confirmed'] = False
    elif mutation == 'reason':
        req['reason'] = ''
    elif mutation == 'foreign':
        req['project_id'] = 2
    elif mutation == 'parent':
        req['message_id'] = 2
    elif mutation == 'page':
        req['source_key'] = key[:-1] + '2'
    elif mutation == 'artifact':
        req['artifact_id'] += 1000
    elif mutation == 'withdrawn':
        with ledger.db:
            ledger.db.execute("UPDATE attachments SET state='withdrawn'")
    else:
        with ledger.db:
            ledger.db.execute('UPDATE patients SET is_archived=1 WHERE project_id=1')
    assert mcs_requests.apply_command(ledger, req, cfg=cfg)['outcome'] == 'rejected'
    assert not ledger.artifacts(attachment_feedback.KIND)


def test_unknown_paid_cloud_cannot_be_reclassified_by_human_feedback(page_store, monkeypatch):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    req = _request(ledger, cfg, key)
    cfg['cloud_llm']['mode'] = 'on'
    sends = []

    def unknown(self, method, body, timeout, **kwargs):
        sends.append(body)
        raise KeyboardInterrupt('synthetic unknown transport')

    monkeypatch.setattr(AnthropicClient, 'request', unknown)
    with pytest.raises(KeyboardInterrupt):
        run(ledger, cfg, 'cloud')
    financial = _financial(ledger)
    assert mcs_requests.apply_command(ledger, req, cfg=cfg)['error'] == 'extraction_changed'
    assert _financial(ledger) == financial and not ledger.artifacts(attachment_feedback.KIND)
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(sends) == 1 and _financial(ledger) == financial


@pytest.mark.parametrize('key', [None, 4, '', 'karte:1', 'attachment_ocr:1:31'])
def test_non_ocr_feedback_key_is_rejected_before_io(key):
    req = {'version': 1, 'cmd': 'ops.extract_feedback', 'command_id': str(uuid.uuid4()),
           'actor': 'synthetic-operator', 'human_confirmed': True, 'project_id': 1,
           'message_id': 1, 'artifact_id': 1, 'field': 'meds', 'reason': '合成理由', 'source_key': key}
    assert mcs_requests.validate(req) == 'bad_extract_ref'


def test_no_feedback_does_not_change_contract_or_unrelated_page(page_store):
    ledger, cfg, key, *_ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    source = original_source_keys.source(ledger.db, 1, key)
    before = copy.deepcopy(worker.current(ledger.db, 1, key, cfg=cfg))
    contract = worker._contract(cfg)
    ledger.artifact_add(attachment_feedback.KIND, '{}', project_id=1, message_id=1,
        meta={'source_key': key[:-1] + '2', 'source_binding': source['_source_binding']})
    assert worker._contract(cfg) == contract
    assert worker.current(ledger.db, 1, key, cfg=cfg) == before
    assert not ledger.artifacts('extract_feedback_v1')
    stored = json.loads(ledger.artifacts(worker.KIND)[-1]['content'])
    assert stored['contract'] == contract
