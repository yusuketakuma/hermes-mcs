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


def _approved(ledger, monkeypatch, mid=1, body=None):
    if body is None:
        old(ledger, mid)
    else:
        from extract_testkit import _hash, _message
        ledger.save_messages([_message(mid, body)])
        ledger.artifact_add(worker.KIND, "{}", project_id=1, message_id=mid,
            meta={"hash": _hash(ledger, mid), "extract_version": worker.EXTRACT_VERSION - 1})
    _paid(ledger, monkeypatch, mid)
    plan = local_rebuild.preview(ledger)['plan']
    assert [unit['message_id'] for unit in plan['units']] == [mid]
    receipt = mcs_requests.apply_command(ledger, {
        'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
        'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
        'reason': '完全合成の確認', 'plan': plan})
    assert receipt['outcome'] == 'applied'
    return local_rebuild.approved_units(ledger.db)[(1, mid)]


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
    job = jobs[0][0]                                    # (row, context, saved, hints, qc)
    assert job['_cloud_mode'] == 'off' and job['_local_rebuild'] is not None
    assert not job.get('_cloud_owned') and not job.get('_superseded_local')
    calls, reserved, results = routed[2], routed[3], routed[4]
    _no_cloud_unit(monkeypatch)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1     # shadow cfg, adopted locally
    assert calls == [] and reserved == [] and results == []                   # no shadow follow-up send


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


def test_a_multi_piece_body_with_a_quality_repair_stays_local_and_tagged(routed, monkeypatch):
    """Pieces (completeness_quality_testkit's small _CHUNK_SIZE) and one local quality repair,
    whose started/origin checkpoints are written: no cloud unit, reservation or HTTP, and every
    checkpoint and the final carry the approval tag."""
    import extract_llm_context
    import extract_llm_prepare
    import extract_llm_proofs
    import extract_llm_store
    from extract_testkit import _hash, _message
    ledger, cfg, calls, reserved, results = routed
    pieces = [f"本人に合成{tag}の発熱と咳があります。\n" for tag in "ABC"]
    ledger.save_messages([_message(1, "".join(pieces))])
    ledger.artifact_add(worker.KIND, "{}", project_id=1, message_id=1,
        meta={"hash": _hash(ledger, 1), "extract_version": worker.EXTRACT_VERSION - 1})
    for module in (worker, extract_llm_context, extract_llm_engine, extract_llm_prepare,
                   extract_llm_proofs, extract_llm_store):
        monkeypatch.setattr(module, '_CHUNK_SIZE', len(pieces[0]))
    _paid(ledger, monkeypatch)
    plan = local_rebuild.preview(ledger)['plan']
    receipt = mcs_requests.apply_command(ledger, {
        'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': str(uuid.uuid4()),
        'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
        'reason': '完全合成の確認', 'plan': plan})
    assert receipt['outcome'] == 'applied'
    unit = local_rebuild.approved_units(ledger.db)[(1, 1)]
    _no_cloud_unit(monkeypatch)
    kinds, issued = [], []

    def local(prompt, **kwargs):
        kinds.append(kwargs.get('kind', 'first'))
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        return {"symptoms": [{"text": text, "evidence": piece} for text in ("発熱", "咳")]}
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    completeness = worker.extract_quality.completeness_issues

    def once(*args):
        if not issued:
            issued.append(True)
            return ["synthetic-missing"]
        return completeness(*args)
    monkeypatch.setattr(worker.extract_quality, "completeness_issues", once)
    done = 0
    for _ in range(4):
        # the repair needs its own call budget after the remaining pieces' (_MIN_CALL_S each)
        done = worker.run_pending(ledger, limit=1, budget_s=600)["done"]
        if done:
            break
    assert done == 1 and len(_current(ledger)) == 1                   # the run completed and was adopted
    assert issued and 'repair' in kinds and kinds.count('first') >= len(pieces)   # every piece plus the repair
    assert calls == [] and reserved == [] and results == []
    tags = [json.loads(meta).get('local_rebuild') for (meta,) in ledger.db.execute(
        "SELECT meta FROM artifacts WHERE kind IN ('extract_llm_chunk',?) AND message_id=1", (worker.KIND,))
        if json.loads(meta).get('extract_version', worker.EXTRACT_VERSION) == worker.EXTRACT_VERSION]
    assert tags and all(item == local_rebuild.tag(unit) for item in tags)



def test_a_held_head_keeps_a_bounded_claim_and_the_pass_reaches_the_next_row(routed, monkeypatch):
    """The real local prepare loop (runner refill over prepare refusals): a lapsed approval at the
    head is claimed for the prepare hold and the healthy row behind it is extracted."""
    ledger, *_ = routed
    _approved(ledger, monkeypatch, mid=2)                              # newest first: the head
    _paid(ledger, monkeypatch, 2, result=False)                        # lapses the approval
    old(ledger, 1)                                                     # a healthy, unapproved row
    seen = []

    def local(prompt, **_kw):
        seen.append(prompt)
        return {'symptoms': [], 'summary': '合成の要約'}
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    report = worker.run_pending(ledger, limit=1, budget_s=180)
    assert report['done'] == 1 and seen
    assert report['defer_reasons'].get(local_rebuild.PROOF_CHANGED) == 1
    assert ledger.db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE kind='extract_claim' AND message_id=2 "
                             "AND next_try>strftime('%s','now')").fetchone()[0] == 1


def test_an_adopted_unit_stays_adopted_when_later_review_evidence_turns_adverse(routed, monkeypatch):
    """Adoption is the authentic past result (material proof without the adverse review probe);
    publication readers still see the adverse evidence and refuse the material."""
    import extract_llm_progress
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    [final] = _current(ledger)
    probe = extract_llm_progress._progress_probe

    def adverse(db, item):
        return True if item[0] == 'review_material' else probe(db, item)
    monkeypatch.setattr(extract_llm_progress, '_progress_probe', adverse)
    assert not mcs_queries.selected_artifact_proof_valid(ledger.db, final['artifact_id'], material=True)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED


def test_a_local_length_stop_splits_the_piece_and_every_split_stays_local_and_tagged(routed, monkeypatch):
    """The real length-split path (engine: a None answer whose last note stopped at length):
    the split plan and its children run locally, no cloud unit/reservation/HTTP, and the split
    checkpoints carry the approval tag."""
    ledger, cfg, calls, reserved, results = routed
    body = "".join(f"完全合成の区間{i}の居住記録です。" for i in range(6))   # splittable, as in the checkpoint tests
    unit = _approved(ledger, monkeypatch, body=body)
    _no_cloud_unit(monkeypatch)
    stopped = []

    def local(prompt, **kwargs):
        if not stopped:
            stopped.append(True)
            worker._integrity_note({"status": "ok", "finish_reason": "length"}, kwargs.get("kind", "first"))
            return None
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        return {"symptoms": [], "patient_context": [{"category": "living", "subject": "patient",
                                                     "text": piece, "evidence": piece}]}
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    import extract_llm_runner
    import extract_llm_store
    written = {}                                       # checkpoint key -> (split plan?, tag) at write time
    persist = extract_llm_store._persist_chunks

    def observe(ledger, r, chunks, context=None, **kwargs):
        accepted = persist(ledger, r, chunks, context, **kwargs)
        if accepted:
            for key in chunks:
                written[str(key)] = (kwargs.get('split_size') is not None, json.loads(ledger.db.execute(
                    "SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' AND message_id=1 "
                    "ORDER BY artifact_id DESC LIMIT 1").fetchone()[0]).get('local_rebuild'))
        return accepted
    monkeypatch.setattr(worker, '_persist_chunks', observe)
    monkeypatch.setattr(extract_llm_runner, '_persist_chunks', worker._persist_chunks)
    done = 0
    for _ in range(3):
        done = worker.run_pending(ledger, limit=1, budget_s=180)['done']
        if done:
            break
    assert done == 1 and len(_current(ledger)) == 1                   # the split run completed
    assert stopped and calls == [] and reserved == [] and results == []
    assert written.get('0', (False,))[0]                               # the split plan, before cleanup
    assert {'0:0', '0:1'} <= set(written)                              # both children were answered
    assert all(item[1] == local_rebuild.tag(unit) for item in written.values())


def test_a_failed_reprocessing_of_an_adopted_unit_settles_with_the_tag_and_stays_adopted(routed, monkeypatch):
    """A human report re-pends the adopted unit; the local re-extract fails (no answer), so the
    report settles by re-minting the prior content with human_fix.applied=false. That settling
    write carries the approval tag inside its re-proved transaction, and the unit stays ADOPTED
    (applied=false is quality, not authenticity)."""
    ledger, calls, reserved, results = routed[0], routed[2], routed[3], routed[4]
    unit = _approved(ledger, monkeypatch)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1
    [final] = _current(ledger)
    with ledger.db:
        ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,created_at) "
                          "VALUES('extract_feedback_v1',1,1,?,0)", (json.dumps({'artifact_id': final['artifact_id']}),))
    _no_cloud_unit(monkeypatch)
    import extract_llm_runner
    monkeypatch.setattr(worker, '_llm_up', lambda **kwargs: True)              # reach the settling branch
    monkeypatch.setattr(extract_llm_runner, '_llm_up', worker._llm_up)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: None)
    worker.run_pending(ledger, limit=1, budget_s=180)
    [settled] = _current(ledger)
    meta = json.loads(settled['meta'])
    assert settled['artifact_id'] != final['artifact_id']
    assert meta['human_fix']['applied'] is False and meta['local_rebuild'] == local_rebuild.tag(unit)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    assert calls == [] and reserved == [] and results == []



def test_health_and_dry_run_count_approved_units_by_state(routed, monkeypatch):
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    assert local_rebuild.health_counts(ledger.db) == {'approved': 1, 'written': 0, 'exhausted': 0}
    report = local_rebuild.preview(ledger)
    assert report['approved'][local_rebuild.ACTIVE] == 1 and report['plan'] is None
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1
    assert local_rebuild.health_counts(ledger.db) == {'approved': 1, 'written': 1, 'exhausted': 0}
    assert local_rebuild.preview(ledger)['approved'][local_rebuild.ADOPTED] == 1


def test_a_reservation_landing_during_the_local_call_stops_the_adoption_write(routed, monkeypatch):
    """The permit is re-proved inside the adoption transaction: a new reservation that appears
    while the local model answers lapses the approval and nothing is adopted."""
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    _no_cloud_unit(monkeypatch)

    def local(prompt, **_kw):
        if not local.lapsed:
            local.lapsed = True
            _paid(ledger, monkeypatch, result=False)
        return {'symptoms': [], 'summary': '合成'}
    local.lapsed = False
    monkeypatch.setattr(extract_llm_engine, '_llm_call', local)
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 0
    assert local.lapsed and not _current(ledger)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED



@pytest.mark.parametrize('tamper', [
    "json_remove(meta,'$.integrity')",
    "json_remove(meta,'$.integrity.completeness','$.integrity.completeness_chunks')",
    "json_remove(meta,'$.integrity.completeness_chunks')",
    "json_set(meta,'$.integrity.completeness_chunks',99)",
])
def test_a_tampered_completeness_record_is_never_an_authentic_adoption(routed, monkeypatch, tamper):
    """The authenticity reader waives only the resolution: missing, one-sided or mismatched
    completeness evidence on the tagged final is rejected (the tag never stands in for it)."""
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    [final] = _current(ledger)
    with ledger.db:
        ledger.db.execute(f"UPDATE artifacts SET meta={tamper} WHERE artifact_id=?", (final['artifact_id'],))
    assert local_rebuild.permit(ledger, 1, 1) != local_rebuild.ADOPTED


def test_tampered_final_content_is_never_an_authentic_adoption(routed, monkeypatch):
    ledger, *_ = routed
    _approved(ledger, monkeypatch)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', lambda prompt, **_kw: {'symptoms': [], 'summary': '合成'})
    assert worker.run_pending(ledger, limit=1, budget_s=180)['done'] == 1
    [final] = _current(ledger)
    content = json.loads(final['content'])
    content['summary'] = '改ざんした合成'
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                          (json.dumps(content, ensure_ascii=False), final['artifact_id']))
    assert local_rebuild.permit(ledger, 1, 1) != local_rebuild.ADOPTED
