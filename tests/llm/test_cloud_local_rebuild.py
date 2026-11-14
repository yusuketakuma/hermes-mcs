"""A paid reextract unit with no stored answer is proven, bundle-approved and permitted for local
rebuild only; the proof, the approval and the permit never send, reserve or change accounting."""
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


def _paid_unit(ledger, cfg, message_id):
    """One real paid send through the routed client, then the legacy shape: the reservation
    predates input binding and the result kept no answer."""
    provider = cloud_inference.CloudInferenceUnit(ledger, cfg, route='reextract', project_id=1,
        message_id=message_id, unit_key=str(message_id), deadline=time.monotonic() + 120,
        current=lambda: True, dispatch=lambda fn, **_kw: fn(), owner={'lease': 'synthetic'},
        input_binding=payload_hash('synthetic-input'))
    with legacy_cloud_routes():
        provider('synthetic prompt', deadline=time.monotonic() + 60, max_tokens=100)
    with ledger.db:
        ledger.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.input_binding','$.logical_request_fp') "
                          "WHERE kind='cloud_usage_reserve' AND message_id=?", (message_id,))
        ledger.db.execute("UPDATE artifacts SET content='{}' WHERE kind='cloud_usage_result' AND message_id=?",
                          (message_id,))


@pytest.fixture
def legacy(paid):
    path, cfg, calls, *_ = paid
    ledger = Ledger(path)
    ledger.save_messages([_message(2)])
    for message_id in (1, 2):
        _paid_unit(ledger, cfg, message_id)
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
    assert all(unit['extract_version'] == local_rebuild.EXTRACT_VERSION for unit in units)
    assert local_rebuild.valid_plan(report['plan'])
    assert _usage(ledger) == before and len(calls) == 2


@pytest.mark.parametrize('damage,reason', [
    ("UPDATE artifacts SET meta=json_set(meta,'$.input_binding',NULL) WHERE kind='cloud_usage_reserve' "
     "AND message_id=1", 'binding_present'),
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
    units = [{'project_id': 1, 'message_id': mid, 'source_binding': 's', 'extract_version': 1,
              'proof': 'a' * 64} for mid in range(1, 22)]
    plan = {'schema': local_rebuild.PLAN_SCHEMA, 'database': [1, 2], 'units': units}
    plan['bundle_digest'] = payload_hash({k: plan[k] for k in ('schema', 'database', 'units')})
    assert not local_rebuild.valid_plan(plan)
    assert json.dumps(plan)   # the plan file format is plain JSON
