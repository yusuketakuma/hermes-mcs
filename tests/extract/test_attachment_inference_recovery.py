"""Synthetic OCR inference retains paid answers and local evidence across refusals."""
import json

import pytest

import attachment_inference as worker
import cloud_inference
import cloud_policy
from anthropic_client import AnthropicClient
from test_attachment_inference import page_store as page_store, run
from test_cloud_after_local import local_then_cloud  # noqa: F401
from test_cloud_response_recovery import legacy_cloud_routes
from test_anthropic_runtime import routed  # noqa: F401


def _financial(ledger):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind IN ('cloud_usage_reserve','cloud_usage_result',"
        "'cloud_usage_disposition') ORDER BY artifact_id")]


def _local(ledger, key):
    return [tuple(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? AND json_extract(meta,'$.source_key')=? "
        "AND json_extract(meta,'$.stage')='local' ORDER BY artifact_id", (worker.KIND, key))]


def test_paid_response_survives_config_unavailable_and_recovers_without_resend(page_store, monkeypatch):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    cfg['cloud_llm']['mode'] = 'on'

    def config():
        return {} if cloud_calls else cfg

    monkeypatch.setattr(cloud_inference, 'load_config', config)
    monkeypatch.setattr(worker, 'load_config', config)
    with legacy_cloud_routes():
        first = run(ledger, cfg, 'cloud')
    assert first['done'] == 0 and first['deferred'] == 1
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert len(ledger.artifacts('cloud_usage_result')) == 1
    assert not ledger.artifacts('cloud_usage_disposition')
    assert _local(ledger, key) == local
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False
    financial = _financial(ledger)
    monkeypatch.setattr(cloud_inference, 'load_config', lambda: cfg)
    monkeypatch.setattr(worker, 'load_config', lambda: cfg)
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert len(cloud_calls) == len(local_calls) == 1
    assert _financial(ledger) == financial
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is True


def test_parent_source_change_during_http_settles_cost_but_rejects_adoption(page_store, monkeypatch):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    cfg['cloud_llm']['mode'] = 'on'
    request = AnthropicClient.request

    def changed(self, method, body, timeout, **kwargs):
        answer = request(self, method, body, timeout, **kwargs)
        with ledger.db:
            ledger.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
        return answer

    monkeypatch.setattr(AnthropicClient, 'request', changed)
    with legacy_cloud_routes():
        report = run(ledger, cfg, 'cloud')
    assert report['done'] == 0 and report['deferred'] == 1
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    receipt, = ledger.artifacts('cloud_usage_result')
    assert json.loads(receipt['meta'])['http_class'] == 'http_200'
    dispositions = [json.loads(row['meta'])['disposition']
                    for row in ledger.artifacts('cloud_usage_disposition')]
    assert dispositions == ['discarded_scope']
    assert _local(ledger, key) == local
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert not ledger.db.execute("SELECT 1 FROM artifacts WHERE kind=? "
                                 "AND json_extract(meta,'$.stage')='cloud'", (worker.KIND,)).fetchone()


def test_quota_refusal_preserves_local_artifact_without_transport(page_store, monkeypatch):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    cfg['cloud_llm']['mode'] = 'on'
    monkeypatch.setattr(cloud_policy, 'block_reason', lambda *args, **kwargs: 'monthly_budget')
    with legacy_cloud_routes():
        report = run(ledger, cfg, 'cloud')
    assert report['done'] == 0 and report['deferred'] == 1
    assert not cloud_calls and not _financial(ledger)
    assert _local(ledger, key) == local
    with legacy_cloud_routes():
        current = worker.current(ledger.db, 1, key, cfg=cfg)
    assert current['stage'] == 'local' and current['ready'] is False


def test_unknown_transport_keeps_local_and_never_resends(page_store, monkeypatch):
    ledger, cfg, key, _, _, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    cfg['cloud_llm']['mode'] = 'on'
    sends = []

    def unknown(self, method, body, timeout, **kwargs):
        sends.append(body)
        raise KeyboardInterrupt('synthetic unknown transport')

    monkeypatch.setattr(AnthropicClient, 'request', unknown)
    with pytest.raises(KeyboardInterrupt):
        with legacy_cloud_routes():
            run(ledger, cfg, 'cloud')
    assert len(sends) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert not ledger.artifacts('cloud_usage_result')
    financial = _financial(ledger)

    def forbidden(*args, **kwargs):
        pytest.fail('unknown paid input was resent')

    monkeypatch.setattr(AnthropicClient, 'request', forbidden)
    again = run(ledger, cfg, 'cloud')
    assert again['done'] == 0 and again['deferred'] == 1
    assert _financial(ledger) == financial and _local(ledger, key) == local
    current = worker.current(ledger.db, 1, key, cfg=cfg)
    assert current['stage'] == 'local' and current['ready'] is False


def _lose_accounting_lock(ledger, cloud_calls, monkeypatch):
    real = worker.writer_lock.wait_run_lock
    unavailable = [True]

    def wait(path, **kwargs):
        if cloud_calls and unavailable[0]:
            return None
        return real(path, **kwargs)

    monkeypatch.setattr(worker.writer_lock, 'wait_run_lock', wait)
    return unavailable


def test_known_paid_response_survives_writer_timeout_and_recovers_without_post(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr
    from pathlib import Path
    from safe_io import read_private_bytes

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with legacy_cloud_routes():
        report = run(ledger, cfg, 'cloud')
    assert report['done'] == 0 and report['deferred'] == 1
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    assert not ledger.artifacts('cloud_usage_result')
    files = journal.pending(attachment_ocr._data_root(ledger))
    assert len(files) == 1 and Path(files[0]).stat().st_mode & 0o777 == 0o600
    saved = json.loads(read_private_bytes(files[0], journal.LIMIT))
    assert saved['origin']['source_key'] == key
    assert saved['reservation']['reservation_id'] == files[0].stem
    assert saved['answer']['text'] == '{"symptoms": []}'
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_result')) == 1
    assert not journal.pending(attachment_ocr._data_root(ledger))
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is True


def test_journal_updater_hold_preserves_bytes_and_never_adopts(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    files = journal.pending(attachment_ocr._data_root(ledger))
    before = files[0].read_bytes()
    unavailable[0] = False
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (True, 1))
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert files[0].read_bytes() == before and not ledger.artifacts('cloud_usage_result')
    assert len(cloud_calls) == 1 and worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False
    monkeypatch.setattr(worker.writer_lock, 'updater_state', lambda: (False, 1))
    assert run(ledger, cfg, 'cloud')['done'] == 1 and len(cloud_calls) == 1


def test_journal_settles_obsolete_parent_without_adopting_or_resending(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    with ledger.db:
        ledger.db.execute("UPDATE attachments SET state='withdrawn' WHERE attachment_id=1")
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_result')) == 1
    disposition, = ledger.artifacts('cloud_usage_disposition')
    assert json.loads(disposition['meta'])['disposition'] == 'discarded_scope'
    assert worker.current(ledger.db, 1, key, cfg=cfg) is None
    assert not journal.pending(attachment_ocr._data_root(ledger))


def test_tampered_journal_is_retained_and_cannot_resend_or_adopt(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr
    from mcs_util import payload_hash

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    path, = journal.pending(attachment_ocr._data_root(ledger))
    value = json.loads(path.read_text())
    value['reservation']['max_tokens'] += 1
    value['hash'] = payload_hash({k: v for k, v in value.items() if k != 'hash'})
    path.write_text(json.dumps(value))
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert path.exists() and not ledger.artifacts('cloud_usage_result')
    assert len(cloud_calls) == 1 and worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False


def test_journal_replay_is_idempotent_after_accounting_committed_before_cleanup(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    remove = journal.remove

    def interrupted(path):
        raise OSError('synthetic cleanup interrupted')

    monkeypatch.setattr(journal, 'remove', interrupted)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    assert len(ledger.artifacts('cloud_usage_result')) == 1
    financial = _financial(ledger)
    assert len(journal.pending(attachment_ocr._data_root(ledger))) == 1
    monkeypatch.setattr(journal, 'remove', remove)
    assert run(ledger, cfg, 'cloud')['done'] == 1
    assert len(cloud_calls) == 1 and _financial(ledger) == financial
    assert worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is True


def test_journal_scope_revocation_settles_and_discards_before_reenable(page_store, monkeypatch):
    import attachment_paid_results as journal
    import attachment_ocr

    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    unavailable = _lose_accounting_lock(ledger, cloud_calls, monkeypatch)
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['deferred'] == 1
    cfg['cloud_llm']['mode'] = 'off'
    unavailable[0] = False
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(cloud_calls) == len(ledger.artifacts('cloud_usage_result')) == 1
    disposition, = ledger.artifacts('cloud_usage_disposition')
    assert json.loads(disposition['meta'])['disposition'] == 'discarded_scope'
    assert not journal.pending(attachment_ocr._data_root(ledger))
    cfg['cloud_llm']['mode'] = 'on'
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(cloud_calls) == 1 and worker.current(ledger.db, 1, key, cfg=cfg)['ready'] is False


def test_unowned_page_completes_and_reads_local_despite_broken_cloud_config(page_store):
    ledger, cfg, key, local_calls, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    # A closed send route never requires the cloud: unreadable settings must not
    # make an unowned page's durable local result invisible or retry forever.
    cfg['cloud_llm'] = 'broken'
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    assert result['ready'] is True and result['require_cloud'] is False
    assert result['stage'] == 'local'
    assert _local(ledger, key) == local and not cloud_calls
    assert run(ledger, cfg, 'cloud')['done'] == 0 and not cloud_calls


def test_owned_page_stays_held_when_cloud_config_unverifiable(page_store, monkeypatch):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    local = _local(ledger, key)
    cfg['cloud_llm']['mode'] = 'on'
    sends = []

    def unknown(self, method, body, timeout, **kwargs):
        sends.append(body)
        raise KeyboardInterrupt('synthetic unknown transport')

    monkeypatch.setattr(AnthropicClient, 'request', unknown)
    with pytest.raises(KeyboardInterrupt):
        with legacy_cloud_routes():
            run(ledger, cfg, 'cloud')
    assert len(sends) == len(ledger.artifacts('cloud_usage_reserve')) == 1
    financial = _financial(ledger)
    # A durable reservation still owns the page: unverifiable settings hold it
    # for recovery, never release it to the local result and never resend.
    cfg['cloud_llm'] = 'broken'
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    assert result['ready'] is False and result['require_cloud'] is True
    assert _local(ledger, key) == local
    assert run(ledger, cfg, 'cloud')['done'] == 0
    assert len(sends) == 1 and not cloud_calls and _financial(ledger) == financial


def test_contractless_cloud_artifact_is_unreadable_not_a_crash(page_store):
    ledger, cfg, key, _, cloud_calls, _ = page_store
    assert run(ledger, cfg, 'local')['done'] == 1
    cfg['cloud_llm']['mode'] = 'on'
    with legacy_cloud_routes():
        assert run(ledger, cfg, 'cloud')['done'] == 1
    assert len(cloud_calls) == 1
    # An artifact that predates contract binding (or a torn write) carries no
    # provider_contract; once the profile can no longer be resolved the reader
    # must fail closed — indexing into a missing contract crashed the worker.
    row = ledger.db.execute(
        "SELECT artifact_id,content FROM artifacts WHERE kind=? "
        "AND json_extract(meta,'$.stage')='cloud'", (worker.KIND,)).fetchone()
    value = json.loads(row['content'])
    value.pop('provider_contract', None)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps(value), row['artifact_id']))
    cfg['cloud_llm']['bogus_field'] = 1
    assert worker._stored(ledger.db, 1, key, 'cloud', cfg) is None
    result = worker.current(ledger.db, 1, key, cfg=cfg)
    assert result['ready'] is False and result['require_cloud'] is True
    assert run(ledger, cfg, 'cloud')['done'] == 0 and len(cloud_calls) == 1
