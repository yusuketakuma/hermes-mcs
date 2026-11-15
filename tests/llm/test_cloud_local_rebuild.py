"""A paid reextract unit with no stored answer is proven, bundle-approved and permitted for local
rebuild only; the proof, the approval and the permit never send, reserve or change accounting."""
from contextlib import closing
import json
import time
import uuid

import pytest

import cloud_inference
import extract_llm_local_rebuild as local_rebuild
import mcs_requests
from ledger import Ledger
from mcs_util import payload_hash
from semantic_testkit import _message
from test_cloud_response_recovery import legacy_cloud_routes, paid as paid

__all__ = ["paid"]


def _paid_unit(ledger, cfg, message_id, *, drop_fp=False):
    """One real paid send through the routed client, then the legacy shape of
    test_cloud_response_recovery's 'legacy' damage: neither row carries input binding and
    the result kept no answer. ``drop_fp`` also removes the logical fingerprint."""
    provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
        message_id=message_id, unit_key=str(message_id), deadline=time.monotonic() + 120,
        current=lambda: True, dispatch=lambda fn, **_kw: fn(), owner={'lease': 'synthetic'},
        input_binding=payload_hash('synthetic-input'))
    with legacy_cloud_routes():
        provider('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
    removed = "'$.input_binding','$.logical_request_fp'" if drop_fp else "'$.input_binding'"
    with ledger.db:
        ledger.db.execute(f"UPDATE artifacts SET meta=json_remove(meta,{removed}),content='{{}}' "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=?", (message_id,))


@pytest.fixture
def legacy(paid):
    path, cfg, calls, *_ = paid
    ledger = Ledger(path)
    ledger.save_messages([_message(2)])
    for message_id in (1, 2):
        _paid_unit(ledger, cfg, message_id, drop_fp=message_id == 2)   # a missing fp never excludes
    assert len(calls) == 2
    yield ledger, calls
    ledger.close()


def _usage(ledger):
    return ledger.db.execute("SELECT kind,COUNT(*) FROM artifacts WHERE kind LIKE 'cloud_usage_%' "
                             "GROUP BY kind ORDER BY kind").fetchall()


def _approve(ledger, plan, command_id=None):
    req = {'version': 1, 'cmd': local_rebuild.COMMAND, 'command_id': command_id or str(uuid.uuid4()),
           'actor': 'synthetic-human', 'human_confirmed': True, 'project_id': None,
           'reason': '完全合成の確認', 'plan': plan}
    return req, mcs_requests.apply_command(ledger, req)


def test_a_bindingless_answerless_success_is_eligible_and_the_bundle_lists_it(legacy):
    ledger, calls = legacy
    before = _usage(ledger)
    report = local_rebuild.preview(ledger)
    assert report['state'] == 'complete' and report['candidates'] == report['eligible'] == 2
    units = report['plan']['units']
    assert [unit['message_id'] for unit in units] == [2, 1]
    assert all(unit['contract'] == local_rebuild._context_contract() for unit in units)
    assert local_rebuild.valid_plan(report['plan'])
    assert _usage(ledger) == before and len(calls) == 2


@pytest.mark.parametrize('damage,reason', [
    ("UPDATE artifacts SET meta=json_set(meta,'$.input_binding','" + 'a' * 64 + "') "
     "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=1", 'binding_present'),
    # an explicit null binding fails the reservation's own validation before any key test
    ("UPDATE artifacts SET meta=json_set(meta,'$.input_binding',NULL) "
     "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=1", 'receipt_inconsistent'),
    ("UPDATE artifacts SET content='{\"text\":\"synthetic\",\"finish_reason\":\"stop\"}' "
     "WHERE kind='cloud_usage_result' AND message_id=1", 'answer_stored'),
    ("UPDATE artifacts SET content='null' WHERE kind='cloud_usage_result' AND message_id=1",
     'receipt_inconsistent'),
    ("DELETE FROM artifacts WHERE kind='cloud_usage_result' AND message_id=1", 'result_missing'),
    ("INSERT INTO artifacts(kind,project_id,message_id,meta,created_at) SELECT 'cloud_usage_disposition',1,1,"
     "json_object('reservation_id',json_extract(meta,'$.reservation_id')),0 FROM artifacts "
     "WHERE kind='cloud_usage_reserve' AND message_id=1", 'disposition'),
    ("INSERT INTO artifacts(kind,content,created_at) VALUES('cloud_usage_pruned_before','1e300',0)",
     'history_pruned'),
    # an explicit null logical fingerprint is not an absent one
    ("UPDATE artifacts SET meta=json_set(meta,'$.logical_request_fp',NULL) "
     "WHERE kind='cloud_usage_reserve' AND message_id=1", 'receipt_inconsistent'),
    # an orphan result (no reservation of any route) and a reservation without a route fail closed
    ("INSERT INTO artifacts(kind,project_id,message_id,meta,content,created_at) "
     "VALUES('cloud_usage_result',1,1,'{\"reservation_id\":\"" + 'a' * 32 + "\"}','{}',0)", 'receipt_inconsistent'),
    ("UPDATE artifacts SET meta=json_remove(meta,'$.route') WHERE kind='cloud_usage_reserve' AND message_id=1",
     'receipt_inconsistent'),
    # an unknown route is never treated as another valid route's row
    ("INSERT INTO artifacts(kind,project_id,message_id,model,meta,content,created_at) SELECT kind,project_id,"
     "message_id,model,json_set(meta,'$.reservation_id','" + 'c' * 32 + "','$.route','synthetic_unknown'),content,"
     "created_at FROM artifacts WHERE kind='cloud_usage_reserve' AND message_id=1", 'receipt_inconsistent'),
    # a disposition with a malformed reservation id is never silently ignored
    ("INSERT INTO artifacts(kind,project_id,message_id,meta,created_at) "
     "VALUES('cloud_usage_disposition',1,1,'{\"reservation_id\":\"not-hex\"}',0)", 'receipt_inconsistent'),
])
def test_each_unproven_history_is_excluded_with_its_fixed_reason(legacy, damage, reason):
    ledger, _ = legacy
    with ledger.db:
        ledger.db.execute(damage)
    with pytest.raises(ValueError, match=f'^{reason}$'):
        local_rebuild.unit_proof(ledger, 1, 1)
    report = local_rebuild.preview(ledger)
    planned = [unit['message_id'] for unit in (report['plan'] or {'units': []})['units']]
    assert 1 not in planned
    # a present binding (null or a hash) is not a key-absent candidate, so the dry run never lists it
    present = 'input_binding' in damage and 'json_set' in damage
    # a broken usage row can also void the month tally, which the dry run checks first (fail closed)
    counted = report['excluded'][reason] + (report['excluded']['tally_invalid'] if reason != 'history_pruned' else 0)
    assert counted == (0 if present else 2 if reason == 'history_pruned' else 1)


def test_one_bundle_approval_is_idempotent_and_permits_local_work_only(legacy):
    ledger, calls = legacy
    plan = local_rebuild.preview(ledger)['plan']
    before = _usage(ledger)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.UNAPPROVED
    req, receipt = _approve(ledger, plan)
    assert receipt['outcome'] == 'applied' and receipt['approved'] == 2
    assert receipt['kind'] == local_rebuild.RECEIPT_KIND and receipt['units'] == plan['units']
    _, replay = _approve(ledger, plan, command_id=req['command_id'])
    assert replay == receipt
    _, conflict = _approve(ledger, {**plan, 'units': plan['units'][:1],
                                    'bundle_digest': payload_hash({k: plan[k] if k != 'units' else plan['units'][:1]
                                                                   for k in ('schema', 'database', 'units')})},
                           command_id=req['command_id'])
    assert conflict == {'outcome': 'rejected', 'error': 'command_id_conflict'}
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.permit(ledger, 1, 2) == local_rebuild.ACTIVE
    assert local_rebuild.preview(ledger)['plan'] is None        # approved units are not offered again
    _, again = _approve(ledger, plan)
    assert again['outcome'] == 'rejected' and again['error'] == 'local_rebuild_already_approved'
    assert _usage(ledger) == before and len(calls) == 2


def test_a_single_changed_unit_rejects_the_whole_bundle(legacy):
    ledger, _ = legacy
    plan = local_rebuild.preview(ledger)['plan']
    with ledger.db:
        ledger.db.execute("UPDATE messages SET body_text='完全合成の変更後の本文' WHERE message_id=1")
        ledger.db.execute("UPDATE messages SET content_hash='synthetic-changed' WHERE message_id=1")
    _, receipt = _approve(ledger, plan)
    assert receipt['outcome'] == 'rejected' and receipt['error'] == 'local_rebuild_bundle_changed'
    assert local_rebuild.approved_units(ledger.db) == {}
    assert local_rebuild.permit(ledger, 1, 2) == local_rebuild.UNAPPROVED


@pytest.mark.parametrize('change', ['source', 'history'])
def test_a_changed_source_or_history_turns_the_permit_into_proof_changed(legacy, change):
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    with ledger.db:
        if change == 'source':
            ledger.db.execute("UPDATE messages SET content_hash='synthetic-changed' WHERE message_id=1")
        else:
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.synthetic_note','x') "
                              "WHERE kind='cloud_usage_result' AND message_id=1")
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED
    assert local_rebuild.permit(ledger, 1, 2) == local_rebuild.ACTIVE


def test_a_broken_receipt_grants_nothing(legacy):
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    with ledger.db:
        ledger.db.execute("UPDATE command_receipts SET receipt_json=json_set(receipt_json,'$.units','broken')")
    assert local_rebuild.approved_units(ledger.db) == {}
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.UNAPPROVED


def test_an_unreadable_permit_is_unverified_never_a_grant(legacy, monkeypatch):
    import sqlite3
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])

    def busy(*_args, **_kwargs):
        raise sqlite3.OperationalError('synthetic interrupt')
    monkeypatch.setattr(local_rebuild, '_history_proof', busy)   # the permit's own history re-proof
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.UNVERIFIED


@pytest.mark.parametrize('plan', [
    None, {}, {'schema': 'other'},
])
def test_an_invalid_plan_is_rejected_before_any_write(legacy, plan):
    ledger, _ = legacy
    _, receipt = _approve(ledger, plan)
    assert receipt['outcome'] == 'rejected' and receipt['error'] == 'local_rebuild_plan_invalid'
    assert local_rebuild.approved_units(ledger.db) == {}


def test_the_bundle_limit_is_a_fixed_constant():
    assert local_rebuild.BUNDLE_LIMIT == 20
    with pytest.raises(ValueError, match='local_rebuild_limit_invalid'):
        local_rebuild.preview(None, limit=21)
    units = [{'project_id': 1, 'message_id': mid, 'source_binding': 's', 'contract': 'b' * 64,
              'proof': 'a' * 64} for mid in range(1, 22)]
    plan = {'schema': local_rebuild.PLAN_SCHEMA, 'database': [1, 2], 'units': units}
    plan['bundle_digest'] = payload_hash({k: plan[k] for k in ('schema', 'database', 'units')})
    assert not local_rebuild.valid_plan(plan)
    assert json.dumps(plan)   # the plan file format is plain JSON


def test_a_settled_but_unsuccessful_result_is_not_success(legacy, monkeypatch):
    """A reextract reservation whose only result is a timeout is not a proven success."""
    import cloud_policy
    from test_cloud_resend_guard import _reserve, _result
    ledger, _ = legacy
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    ledger.save_messages([_message(3)])
    with legacy_cloud_routes():   # reextract sends are closed in the live policy, like the fixture's seed
        reservation = _reserve(ledger, 'synthetic prompt', route='reextract', unit='3', message_id=3)
    cloud_policy.record_result(ledger, reservation, _result(reservation, 'timeout', 'timeout'))
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.input_binding') "
                          "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=3")
    with pytest.raises(ValueError, match='^not_success$'):
        local_rebuild.unit_proof(ledger, 1, 3)


def test_a_current_local_result_is_already_current_and_not_offered(legacy):
    from cloud_selection_progress_testkit import _complete_local_tx
    ledger, _ = legacy
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        _complete_local_tx(ledger, {'project_id': 1, 'message_id': 1})
    with pytest.raises(ValueError, match='^already_current$'):
        local_rebuild.unit_proof(ledger, 1, 1)
    report = local_rebuild.preview(ledger)
    assert report['excluded']['already_current'] == 1
    assert [unit['message_id'] for unit in report['plan']['units']] == [2]


def test_the_cli_requires_a_human_confirmation_before_any_write(legacy, capsys):
    import argparse
    import cloud_cli
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest='command'))
    args = parser.parse_args(['cloud-llm', 'local-rebuild-approve', '--plan', 'synthetic-plan.json',
                              '--reason', '完全合成の確認'])
    assert args.cloud_action == 'local-rebuild-approve' and args.confirm_human is False
    assert args.fn(args) == 1
    assert '--confirm-human' in capsys.readouterr().out
    ledger, _ = legacy
    assert local_rebuild.approved_units(ledger.db) == {}
    plan = parser.parse_args(['cloud-llm', 'local-rebuild-plan', '--limit', '5'])
    assert plan.cloud_action == 'local-rebuild-plan' and plan.limit == 5 and plan.out is None


def test_a_copied_database_never_inherits_the_approval(legacy, tmp_path):
    import sqlite3
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ACTIVE
    copy_path = str(tmp_path / 'synthetic-copy.db')
    with sqlite3.connect(copy_path) as target:
        ledger.db.backup(target)
    with closing(Ledger(copy_path)) as copied:
        assert local_rebuild.approved_units(copied.db)        # the receipt itself was copied
        assert local_rebuild.permit(copied, 1, 1) == local_rebuild.PROOF_CHANGED


@pytest.mark.parametrize('field,value', [
    ('payload_hash', 'f' * 64), ('receipt_reason', '別の理由'), ('receipt_units', []),
    ('receipt_bundle', 'e' * 64),
])
def test_a_receipt_that_does_not_reproduce_its_envelope_grants_nothing(legacy, field, value):
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    assert len(local_rebuild.approved_units(ledger.db)) == 2
    with ledger.db:
        if field == 'payload_hash':
            ledger.db.execute("UPDATE command_receipts SET payload_hash=?", (value,))
        else:
            path = {'receipt_reason': '$.reason', 'receipt_units': '$.units', 'receipt_bundle': '$.bundle_digest'}[field]
            ledger.db.execute(f"UPDATE command_receipts SET receipt_json=json_set(receipt_json,'{path}',json(?))",
                              (json.dumps(value),))
    assert local_rebuild.approved_units(ledger.db) == {}          # one broken part voids the whole bundle
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.UNAPPROVED


def test_an_approval_that_cannot_reverify_in_time_rejects_the_whole_bundle(legacy, monkeypatch):
    ledger, _ = legacy
    plan = local_rebuild.preview(ledger)['plan']
    monkeypatch.setattr(local_rebuild, 'APPROVE_SECONDS', 0.0)
    _, receipt = _approve(ledger, plan)
    assert receipt['outcome'] == 'rejected' and receipt['error'] == local_rebuild.UNVERIFIED
    assert local_rebuild.approved_units(ledger.db) == {}


def test_a_changed_extraction_contract_turns_the_permit_into_proof_changed(legacy, monkeypatch):
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    monkeypatch.setattr(local_rebuild, '_context_contract', lambda: 'c' * 64)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED


def test_a_reader_limit_leaves_the_rest_unverified_and_the_preview_partial(legacy, monkeypatch):
    class SyntheticLimit(ValueError):
        pass
    ledger, _ = legacy
    proof = local_rebuild.unit_proof

    def limited(ledger, project_id, message_id, stop=()):
        if message_id == 1:
            raise SyntheticLimit('read_deadline')
        return proof(ledger, project_id, message_id, stop)
    monkeypatch.setattr(local_rebuild, 'unit_proof', limited)
    report = local_rebuild.preview(ledger, stop=(SyntheticLimit,))
    assert report['state'] == 'partial' and report['excluded']['unverified'] == 1
    assert report['excluded']['receipt_inconsistent'] == 0 and report['eligible'] == 1


def test_an_unreadable_tally_is_a_fixed_reason_for_every_candidate(legacy, monkeypatch):
    import cloud_policy
    ledger, _ = legacy

    def broken(*_args, **_kwargs):
        raise cloud_policy.AnthropicError('pricing_unverified', 'synthetic')
    monkeypatch.setattr(cloud_policy, '_tally', broken)
    report = local_rebuild.preview(ledger)
    assert report['excluded']['tally_invalid'] == 2 and report['plan'] is None


def _frame_and_started_checkpoint(ledger, monkeypatch, *, checkpoint):
    """The canonical writers of tests/extract/llm/test_sealed_checkpoint_loss_replay's
    `uncertain` fixture: a frozen frame for message 1 and, optionally, a started quality
    repair checkpoint persisted under its claim."""
    import extract_llm as worker
    import extract_llm_carry
    import extract_llm_errors
    import extract_llm_resident
    import extract_llm_runner
    import extract_llm_selection
    import extract_llm_source_contract
    import extract_llm_store
    import extraction_context as frames
    from inference_dependency_loss import source_binding
    for module in (worker, extract_llm_carry, extract_llm_errors, extract_llm_resident, extract_llm_runner,
                   extract_llm_selection, extract_llm_source_contract):
        monkeypatch.setattr(module, '_writer_current', lambda **kwargs: True)
    monkeypatch.setattr(worker, '_rerender_cards', lambda *args: None)
    monkeypatch.setattr(extract_llm_store, '_rerender_cards', worker._rerender_cards)
    row = dict(ledger.db.execute('SELECT * FROM messages WHERE message_id=1').fetchone())
    row.update(_source_binding=source_binding(ledger.db, 1, 1), _target=('', 'synthetic-model'),
               qc_src=None, thin_src=None, human_src=None)
    row['_frame'] = frames.prepare(ledger, row, model='synthetic-model', contract=worker._context_contract(),
        attempt={'qc_src': None, 'thin_src': None, 'human_src': None, 'auto_retry': 0},
        enabled=False, base_context=None, version=worker.EXTRACT_VERSION)
    if checkpoint:
        lease = worker._claim(ledger, row)
        assert lease is not None
        worker._capture_checkpoint_state(ledger.db, row, None)
        body = row['body_text']
        candidate = worker.extract_validation.validate_piece({'symptoms': [], 'patient_context': [
            {'category': 'living', 'subject': 'patient', 'text': body, 'evidence': body}]}, body, body)
        value = worker._EvidenceLeaf({**candidate, '_quality_status': 'started'},
                                     worker._provider_evidence(['local'], True))
        assert worker._persist_chunks(ledger, row, {0: value}, None, chunk_size=row['_chunk_size'],
                                      lease=lease, guard=row['_checkpoint_guard'])


def test_a_frame_alone_is_an_input_and_keeps_the_unit_eligible(legacy, monkeypatch):
    """A stalled unit leaves its frozen frame on every prepare; the frame is no work record."""
    ledger, _ = legacy
    _frame_and_started_checkpoint(ledger, monkeypatch, checkpoint=False)
    assert local_rebuild.unit_proof(ledger, 1, 1)['message_id'] == 1
    assert sorted(u['message_id'] for u in local_rebuild.preview(ledger)['plan']['units']) == [1, 2]


def test_a_started_repair_checkpoint_keeps_an_all_success_unit_out(legacy, monkeypatch):
    """The usage history alone qualifies; a raw checkpoint row (a started quality repair here)
    is left work and excludes the unit."""
    ledger, calls = legacy
    assert local_rebuild.unit_proof(ledger, 1, 1)['message_id'] == 1
    _frame_and_started_checkpoint(ledger, monkeypatch, checkpoint=True)
    with pytest.raises(ValueError, match='^checkpoint_present$'):
        local_rebuild.unit_proof(ledger, 1, 1)
    report = local_rebuild.preview(ledger)
    assert report['excluded']['checkpoint_present'] == 1 and [u['message_id'] for u in report['plan']['units']] == [2]
    assert len(calls) == 2


def test_after_approval_only_this_approvals_tagged_checkpoints_keep_it_active(legacy, monkeypatch):
    """A checkpoint written after approval without this approval's tag (another writer, another
    approval, a hand-made tag) invalidates the permit; the same row with the tag keeps it."""
    ledger, _ = legacy
    req, _receipt = _approve(ledger, local_rebuild.preview(ledger)['plan'])
    unit = local_rebuild.approved_units(ledger.db)[(1, 1)]
    _frame_and_started_checkpoint(ledger, monkeypatch, checkpoint=True)
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED      # untagged
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.local_rebuild',json(?)) "
                          "WHERE kind='extract_llm_chunk' AND message_id=1", (json.dumps(local_rebuild.tag(unit)),))
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ACTIVE
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.local_rebuild.command_id',?) "
                          "WHERE kind='extract_llm_chunk' AND message_id=1", (str(uuid.uuid4()),))
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED      # another approval


def test_the_cli_dry_run_reads_through_the_bounded_read_only_reader(legacy, tmp_path, monkeypatch, capsys):
    """A selected final exists, so the material proof (with its SAVEPOINT) runs on the CLI's
    query_only reader; stdout carries counts only and the plan file is a valid bundle."""
    import argparse
    import sqlite3
    import cloud_cli
    import mcs_util
    from cloud_selection_progress_testkit import _complete_local_tx
    ledger, _ = legacy
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        _complete_local_tx(ledger, {'project_id': 1, 'message_id': 1})
    root = tmp_path / 'synthetic-root'
    (root / 'data').mkdir(parents=True)
    with sqlite3.connect(str(root / 'data' / 'ledger.db')) as target:
        ledger.db.backup(target)
    monkeypatch.setattr(mcs_util, 'HOME', str(root))
    parser = argparse.ArgumentParser()
    cloud_cli.add_parsers(parser.add_subparsers(dest='command'))
    out = tmp_path / 'synthetic-plan.json'
    args = parser.parse_args(['cloud-llm', 'local-rebuild-plan', '--out', str(out)])
    assert args.fn(args) == 0
    printed = json.loads(capsys.readouterr().out.split('\n件数')[0])
    assert printed['state'] == 'complete' and printed['planned'] == 1
    assert printed['excluded']['already_current'] == 1 and 'plan' not in printed
    plan = json.loads(out.read_text())
    assert local_rebuild.valid_plan(plan) and [u['message_id'] for u in plan['units']] == [2]


def test_an_expired_approval_records_its_rejection_and_clears_the_deadline(legacy, monkeypatch):
    ledger, _ = legacy
    plan = local_rebuild.preview(ledger)['plan']
    monkeypatch.setattr(local_rebuild, 'APPROVE_SECONDS', 0.0)
    req, receipt = _approve(ledger, plan)
    assert receipt['outcome'] == 'rejected' and receipt['error'] == local_rebuild.UNVERIFIED
    stored = ledger.db.execute('SELECT outcome,payload_hash FROM command_receipts WHERE command_id=?',
                               (req['command_id'],)).fetchone()
    assert tuple(stored) == ('rejected', payload_hash(req))
    # the approval's progress handler is gone: a later long read on the same connection completes
    assert ledger.db.execute('WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<200000) '
                             'SELECT COUNT(*) FROM n').fetchone()[0] == 200000


def test_an_adopted_unit_stays_adopted_only_while_its_usage_history_is_unchanged(legacy, monkeypatch):
    """ADOPTED suppresses the cloud follow-up; a new unresolved reservation after the local
    final re-proves the history first and lifts it."""
    import cloud_policy
    from cloud_selection_progress_testkit import _complete_local_tx
    from test_cloud_resend_guard import _reserve
    ledger, calls = legacy
    req, receipt = _approve(ledger, local_rebuild.preview(ledger)['plan'])
    unit = local_rebuild.approved_units(ledger.db)[(1, 1)]
    with ledger.db:
        ledger.db.execute('BEGIN IMMEDIATE')
        _complete_local_tx(ledger, {'project_id': 1, 'message_id': 1})
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.local_rebuild',json(?)) "
                          "WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
                          "AND message_id=1)", (json.dumps({'command_id': req['command_id'], 'proof': unit['proof']}),))
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ADOPTED
    monkeypatch.setattr(cloud_policy, 'pricing_valid', lambda *args, **kwargs: True)
    with legacy_cloud_routes():
        _reserve(ledger, 'synthetic later prompt', route='reextract', unit='1', message_id=1)   # no result yet
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED
    assert len(calls) == 2



def test_a_whitespace_empty_answer_is_the_empty_object(legacy):
    ledger, _ = legacy
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET content='{ }' WHERE kind='cloud_usage_result' AND message_id=1")
    assert local_rebuild.unit_proof(ledger, 1, 1)['message_id'] == 1



def test_another_routes_linked_usage_rows_neither_change_the_proof_nor_lift_the_approval(legacy):
    """A semantic_backlog reservation of the same message, with its linked result, is outside the
    reextract proof: the unit keeps its proof and its permit."""
    ledger, _ = legacy
    before = local_rebuild.unit_proof(ledger, 1, 1)['proof']
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    with ledger.db:
        for kind in ('cloud_usage_reserve', 'cloud_usage_result'):
            ledger.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,model,meta,content,created_at) "
                "SELECT kind,project_id,message_id,model,json_set(meta,'$.reservation_id',?,'$.route',"
                "'semantic_backlog'),content,created_at FROM artifacts WHERE kind=? AND message_id=1",
                ('b' * 32, kind))
    assert local_rebuild.unit_proof(ledger, 1, 1)['proof'] == before
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ACTIVE



def test_moving_another_routes_row_to_reextract_lifts_the_approval(legacy):
    """Rows outside the proof only while they link to another valid route: re-routed to
    reextract they join the proof and the approval lapses."""
    ledger, _ = legacy
    _approve(ledger, local_rebuild.preview(ledger)['plan'])
    with ledger.db:
        for kind in ('cloud_usage_reserve', 'cloud_usage_result'):
            ledger.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,model,meta,content,created_at) "
                "SELECT kind,project_id,message_id,model,json_set(meta,'$.reservation_id',?,'$.route',"
                "'semantic_backlog'),content,created_at FROM artifacts WHERE kind=? AND message_id=1",
                ('d' * 32, kind))
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.ACTIVE
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.route','reextract') WHERE "
                          "json_extract(meta,'$.reservation_id')=?", ('d' * 32,))
    assert local_rebuild.permit(ledger, 1, 1) == local_rebuild.PROOF_CHANGED



def test_a_broken_other_route_reservation_is_not_left_out_of_the_proof(legacy):
    """Another route's rows are excluded only once they verify as that route's accounting."""
    ledger, _ = legacy
    with ledger.db:
        ledger.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,model,meta,content,created_at) "
            "SELECT kind,project_id,message_id,model,json_remove(json_set(meta,'$.reservation_id',?,'$.route',"
            "'semantic_backlog'),'$.max_cost_usd','$.model'),content,created_at FROM artifacts "
            "WHERE kind='cloud_usage_reserve' AND message_id=1", ('e' * 32,))
    with pytest.raises(ValueError, match='^receipt_inconsistent$'):
        local_rebuild.unit_proof(ledger, 1, 1)
