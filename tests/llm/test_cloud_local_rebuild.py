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
    ("UPDATE artifacts SET meta=json_set(meta,'$.input_binding',NULL) "
     "WHERE kind IN ('cloud_usage_reserve','cloud_usage_result') AND message_id=1", 'binding_present'),
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
    # an explicit null binding is not a key-absent candidate, so the dry run never lists it
    assert report['excluded'][reason] == (0 if reason == 'binding_present' else 1 if reason != 'history_pruned' else 2)


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
    monkeypatch.setattr(local_rebuild, 'unit_proof', busy)
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
