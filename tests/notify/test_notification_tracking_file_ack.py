"""Historical file non-deliveries require their exact original approval and a successor."""
import copy
import json
from types import SimpleNamespace

import pytest

import notify_cards
import notify_recovery
import notify_scope
import notify_tracking_ack as tracking
import notify_transport
import present_slack
from adapters.common import envelopes
from notify_testkit import NOW, _latest_render, _uuid, led as led
from slack_testkit import ROOT_TS, SLACK
from test_card_preflight_file_groups import group_unknown
from test_file_group_dependency_refusal import _confirm_stage, _refused, _request
from test_notification_tracking_ack import originals

__all__ = ['led']


@pytest.fixture
def history(led, monkeypatch, request, tmp_path):
    monkeypatch.setattr(present_slack, 'BODY_ON_FACE', False)
    original_state = getattr(request, 'param', 'pending')
    if original_state == 'not_sent':
        _, old, _, stage, _ = _refused(led, tmp_path, monkeypatch)
        _confirm_stage(led, old, stage)
        req = _request(notify_recovery.card_part_resolve_plan(led, old['delivery_id'], 'file_group'))
    else:
        req, old, _ = group_unknown(led)
    if original_state == 'pending':
        with led.db:
            led.db.execute(
                "UPDATE notification_render_parts SET state='pending',attempt_id=NULL,error_code=NULL "
                "WHERE delivery_id=? AND part_id='file_group'",
                (old['delivery_id'],))
    plan = notify_recovery.card_part_resolve_plan(led, old['delivery_id'], 'file_group')
    resolve = {**req, **plan, 'result': 'mark_not_sent', 'evidence': {
        **req['evidence'], 'proof': 'dependency_rejected' if original_state == 'not_sent' else 'no_journal_started'}}
    resolve.pop('remote_id', None)
    receipt = notify_recovery.apply_card_part_resolve(led, resolve, SLACK, NOW + 1)
    assert receipt['outcome'] == 'applied', receipt.get('error')
    if original_state == 'not_sent':
        assert led.db.execute("SELECT error_code FROM notification_render_parts WHERE delivery_id=? "
                              "AND part_id='file_group'", (old['delivery_id'],)).fetchone()[0] == 'stage_receipt_missing'
    # A new source file changes the next group; no later exact-payload
    # receipt can falsely stand in for the old undelivered group.
    with led.db:
        led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,local_path,bytes,sha256,state,downloaded_at,created_at) "
            "VALUES(100,'synthetic-third','synthetic-third.pdf','/tmp/fake-third.txt',10,?,'downloaded',?,?)",
            ('ab' * 32, NOW, NOW))
    with led.db:
        assert notify_cards.issue_render(led.db, old['card_id'], SLACK, NOW + 2, [], force=True)
    successor = _latest_render(led)
    spec = json.loads(successor['spec_json'])
    claim = {'spec': spec, 'attempt_id': 'd' * 16, 'worker_id': 'e' * 16,
             'payload_hash': successor['payload_hash']}
    assert notify_transport.apply_transport_begin(led, envelopes.transport_begin(claim), SLACK, now=NOW + 3)['granted']
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, 'delivered', message_id=ROOT_TS), SLACK, now=NOW + 3)['applied']
    for part in spec['parts']['manifest'][1:]:
        remote = part.get('prior_remote_id') or (
            ROOT_TS if part['kind'] in ('thread', 'file_group') else
            'F_SYNTHETIC_' + str(part['attachment_id']) if part['kind'] == 'attachment_part' else
            f"1790000001.{part['index']:06d}")
        assert notify_transport.apply_part_receipt(
            led, envelopes.part_receipt(claim, part, 'delivered', remote_id=remote), SLACK, now=NOW + 3)['applied']
    plan = tracking.build_plan(led.db, old['delivery_id'], 'file_group', cfg=SLACK)
    assert 'error' not in plan, plan
    ack = {**plan, 'command_id': _uuid(940), 'actor': 'synthetic-operator', 'human_confirmed': True,
           'reason': '過去の未配送事実を残して追跡を終了。'}
    return SimpleNamespace(ledger=led, db=led.db, old=old, successor=successor,
                           resolution=resolve, ack=ack)


@pytest.mark.parametrize('history', ['unknown', 'pending', 'not_sent'], indirect=True)
def test_exact_original_negative_approval_allows_only_receipt_closure(history, monkeypatch):
    item = history.ack['targets'][0]
    assert item['resolution_command_id'] == history.resolution['command_id']
    before = originals(history.db)
    raw = notify_scope.incomplete_parts_health(history.db, NOW + 4, SLACK)
    assert raw['active_total'] >= 1
    monkeypatch.setattr(notify_cards, 'issue_render', lambda *args, **kwargs: pytest.fail('tracking attempted resend'))
    receipt = tracking.apply(history.ledger, history.ack, SLACK, NOW + 4)
    assert receipt['outcome'] == 'applied'
    assert receipt['delivery_outcome'] == 'not_sent' and receipt['residual'] == 'unrecovered'
    assert originals(history.db) == before
    after = notify_scope.incomplete_parts_health(history.db, NOW + 5, SLACK)
    assert after['total'] == raw['total']
    assert after['active_total'] == raw['active_total'] - 1
    assert after['acknowledged_unresolved_total'] == 1
    assert tracking.apply(history.ledger, history.ack, SLACK, NOW + 6) == receipt
    with history.db:
        history.db.execute('UPDATE notification_renders SET spec_json=NULL')
    assert notify_scope.incomplete_parts_health(history.db, NOW + 7, SLACK)['acknowledged_unresolved_total'] == 1


@pytest.mark.parametrize('fault', ['missing', 'receipt', 'digest', 'part', 'attempt', 'scope', 'successor', 'resolution_id'])
def test_changed_original_proof_or_scope_cannot_close_history(history, fault):
    req = copy.deepcopy(history.ack)
    with history.db:
        if fault == 'missing':
            history.db.execute('DELETE FROM command_receipts WHERE command_id=?', (history.resolution['command_id'],))
        elif fault == 'receipt':
            row = history.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                                     (history.resolution['command_id'],)).fetchone()
            receipt = json.loads(row[0])
            receipt['actor'] = 'synthetic-forged'
            history.db.execute('UPDATE command_receipts SET receipt_json=? WHERE command_id=?',
                               (json.dumps(receipt), history.resolution['command_id']))
        elif fault == 'digest':
            history.db.execute('UPDATE command_receipts SET payload_hash=? WHERE command_id=?',
                               ('b' * 64, history.resolution['command_id']))
        elif fault == 'part':
            history.db.execute("UPDATE notification_render_parts SET payload_sha256=? "
                               "WHERE delivery_id=? AND part_id='file_group'", ('c' * 64, history.old['delivery_id']))
        elif fault == 'attempt':
            history.db.execute("UPDATE notification_delivery_attempts SET state='unknown' WHERE delivery_id=?",
                               (history.old['delivery_id'],))
        elif fault == 'scope':
            history.db.execute("UPDATE notification_cards SET channel_id='C_OTHER_SYNTHETIC'")
        elif fault == 'successor':
            history.db.execute("UPDATE notification_renders SET state='cancelled' WHERE delivery_id=?",
                               (history.successor['delivery_id'],))
        else:
            req['targets'][0]['resolution_command_id'] = _uuid(999)
    before = originals(history.db)
    result = tracking.apply(history.ledger, req, SLACK, NOW + 4)
    assert result['outcome'] == 'rejected' and result['error'] == 'tracking_ack_stale_plan'
    assert originals(history.db) == before


def test_current_or_non_operator_file_failure_never_becomes_history_ack(history):
    with history.db:
        history.db.execute('DELETE FROM command_receipts WHERE command_id=?', (history.resolution['command_id'],))
        history.db.execute("UPDATE notification_render_parts SET error_code='stage_receipt_missing' "
                           "WHERE delivery_id=? AND part_id='file_group'", (history.old['delivery_id'],))
    assert 'error' in tracking.build_plan(history.db, history.old['delivery_id'], 'file_group', cfg=SLACK)
    assert 'error' in tracking.build_plan(history.db, history.successor['delivery_id'], 'file_group', cfg=SLACK)
