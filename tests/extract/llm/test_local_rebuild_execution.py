"""An approved local rebuild runs on local inference only, tags its work and its final with the
approval, is adopted, and never goes back to the cloud (fully synthetic)."""
import hashlib
import json
import uuid

import pytest

import cloud_inference
import cloud_policy
import extract_llm as worker
import extract_llm_engine
import extract_llm_local_rebuild as local_rebuild
import mcs_queries
import mcs_requests
from test_anthropic_runtime import old, real_record_result, real_usage_reserver, routed
from test_cloud_response_recovery import legacy_cloud_routes

__all__ = ['routed']
_PAID_CFG = {'cloud_llm': {'mode': 'on', 'provider': 'openai', 'project_ids': [1]}}


def _paid(ledger, monkeypatch, mid=1, *, result=True):
    """A durable settled HTTP 200 reextract send through the real reserver and recorder
    (test_legacy_negation_paid_proof's seed), then the legacy shape: no input binding, no
    stored answer."""
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    selection, errors = cloud_policy.resolve_profile(_PAID_CFG, route='reextract', purpose='backlog')
    assert not errors
    client = worker.openai_client.OpenAIClient('synthetic', model=selection['model'], effort=selection['effort'])
    request = client.body(f'完全合成の旧要求{mid}-{uuid.uuid4()}', selection['max_tokens'], None)
    with legacy_cloud_routes():
        reservation = real_usage_reserver(ledger, _PAID_CFG, route='reextract', project_id=1, message_id=mid,
            unit_key=str(mid), owner={'lease': 'synthetic-owner'}, selection=selection)(request, 30)
    if result:
        usage = {'input_tokens': 2, 'output_tokens': 1, 'total_tokens': 3,
                 'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}}
        real_record_result(ledger, reservation, {'model': selection['model'], 'request_fp': reservation['request_fp'],
            'http_class': 'http_200', 'usage': usage, 'error_kind': None,
            'answer_text_sha256_v1': hashlib.sha256(b'{}').hexdigest(), 'answer_finish_reason_v1': 'stop'})
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.input_binding'),content='{}' "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=?", (mid,))


def _approved(ledger, monkeypatch):
    old(ledger, 1)
    _paid(ledger, monkeypatch)
    plan = local_rebuild.preview(ledger)['plan']
    assert [unit['message_id'] for unit in plan['units']] == [1]
    receipt = mcs_requests.apply_command(ledger, {
        'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
        'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
        'reason': '完全合成の確認', 'plan': plan})
    assert receipt['outcome'] == 'applied'
    return local_rebuild.approved_units(ledger.db)[(1, 1)]


def _usage(ledger):
    return ledger.db.execute("SELECT artifact_id,kind,meta,content FROM artifacts WHERE kind LIKE 'cloud_usage_%' "
                             "ORDER BY artifact_id").fetchall()


def _current(ledger):
    return ledger.db.execute(
        "SELECT a.* FROM artifacts a JOIN messages m ON a.message_id=m.message_id WHERE a.kind=? "
        + mcs_queries.current_extract_pred() + " AND json_extract(a.meta,'$.extract_version')=?",
        (worker.KIND, worker.EXTRACT_VERSION)).fetchall()


def _no_cloud_unit(monkeypatch):
    def refuse(*args, **kwargs):
        pytest.fail('an approved local rebuild built a cloud unit')
    monkeypatch.setattr(cloud_inference, 'CloudInferenceUnit', refuse)


def test_an_approved_unit_runs_locally_is_tagged_adopted_and_kept_off_the_cloud(routed, monkeypatch):
    ledger, cfg, calls, reserved, results = routed
    unit = _approved(ledger, monkeypatch)
    before = [tuple(row) for row in _usage(ledger)]
    _no_cloud_unit(monkeypatch)
    prompts = []

    def local(prompt, **_kw):
        prompts.append(prompt)
        return {'symptoms': [], 'summary': '合成の要約'}
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report['done'] == 1 and prompts
    assert calls == [] and reserved == [] and results == []          # no cloud HTTP or reservation
    assert [tuple(row) for row in _usage(ledger)] == before            # paid history byte-for-byte
    final = _current(ledger)
    assert len(final) == 1 and json.loads(final[0]['meta'])['local_rebuild'] == local_rebuild.tag(unit)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    with legacy_cloud_routes():
        cloud, _ = worker._select_pending(ledger, 5, None, None, False, cloud_only=True)
    assert 1 not in [row['message_id'] for row in cloud]
    dispatched = len(prompts)
    second = worker.run_pending(ledger, limit=1, budget_s=180)       # adopted: never re-run
    assert second['done'] == 0 and len(prompts) == dispatched


def test_a_shadow_or_owned_configuration_still_prepares_it_local_only(routed, monkeypatch):
    ledger, cfg, *_ = routed
    _approved(ledger, monkeypatch)
    cfg['cloud_llm']['mode'] = 'shadow'
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    jobs, _skipped, blocked = worker._prepare_jobs(ledger, rows, ('', 'synthetic-model'), False,
                                                   load_checkpoints=False)
    assert blocked == 0 and len(jobs) == 1
    job = jobs[0]
    assert job['_cloud_mode'] == 'off' and job['_local_rebuild'] is not None
    assert not job.get('_cloud_owned') and not job.get('_superseded_local')


def test_a_new_paid_row_after_approval_holds_the_unit_without_any_dispatch(routed, monkeypatch):
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    _paid(ledger, monkeypatch, result=False)                           # an unresolved new reservation
    _no_cloud_unit(monkeypatch)

    def local(prompt, **_kw):
        pytest.fail('a held local rebuild dispatched')
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    reasons = {}
    jobs, _skipped, blocked = worker._prepare_jobs(ledger, rows, ('', 'synthetic-model'), False,
                                                   load_checkpoints=False, blocked_reasons=reasons)
    assert not jobs and reasons == {local_rebuild.PROOF_CHANGED: 1}
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 0
    assert not _current(ledger)


def test_every_checkpoint_of_the_local_run_carries_the_approval_tag(routed, monkeypatch):
    ledger, *_ = routed
    unit = _approved(ledger, monkeypatch)
    body = ledger.db.execute('SELECT body_text FROM messages WHERE message_id=1').fetchone()[0]

    def local(prompt, **_kw):
        return {'symptoms': [], 'patient_context': [{'category': 'living', 'subject': 'patient',
                                                     'text': body, 'evidence': body}]}
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    worker.run_pending(ledger, limit=1, budget_s=180)
    tags = [json.loads(meta).get('local_rebuild') for (meta,) in ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=1")]
    assert all(item == local_rebuild.tag(unit) for item in tags)


def test_a_checkpoint_write_after_a_new_reservation_is_refused_in_its_transaction(routed, monkeypatch):
    """_persist_chunks re-proves the permit inside its writer transaction."""
    import extract_llm_store
    ledger, *_ = routed
    unit = _approved(ledger, monkeypatch)
    _paid(ledger, monkeypatch, result=False)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    from inference_dependency_loss import source_binding
    row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=('', 'synthetic-model'),
               _local_rebuild=unit, _chunk_size=worker._CHUNK_SIZE)
    before = ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0]
    assert extract_llm_store._persist_chunks(ledger, row, {0: {'symptoms': []}}, None) is False
    assert ledger.db.execute("SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm_chunk'").fetchone()[0] == before
