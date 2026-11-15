"""OCR request recovery separates raw cloud inputs from local adoption generations."""
from contextvars import Context
import fcntl
import json
import os
import time

import pytest

import attachment_inference as worker
import attachment_ocr
import local_llm
from mcs_util import payload_hash
from test_attachment_inference import page_store as page_store, run
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_anthropic_runtime import routed  # noqa: F401


def _legacy(source, local):
    return payload_hash(['attachment-inference/v1', source['_source_binding'], local['artifact_id'],
                         local['result_hash'], local['contract']])


def _financial(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


def _two_pages(ledger, cfg, key):
    artifact_id = int(key.split(':')[1])
    raw = json.loads(ledger.db.execute('SELECT content FROM artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()[0])
    raw['pages'].append({**raw['pages'][0], 'page': 2})
    raw['text_hash'] = attachment_ocr._hash(json.dumps(raw['pages'], ensure_ascii=False, sort_keys=True))
    with ledger.db:
        ledger.db.execute('UPDATE artifacts SET content=? WHERE artifact_id=?', (json.dumps(raw), artifact_id))
    for _ in range(4):
        run(ledger, cfg)
    cfg['cloud_llm']['mode'] = 'on'
    return artifact_id


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
    assert run(ledger, cfg)['done'] == 1
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
