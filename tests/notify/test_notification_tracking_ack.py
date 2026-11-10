"""Historical operational closure preserves factual parts, grants and every delivery witness."""
import asyncio
import copy
import hashlib
import io
import json
from types import SimpleNamespace

import pytest

import health_watch
import job_ops
import mcs_requests
import mcs_util
import mcs_view
import notify_cards
import notify_scope
import notify_tracking_ack as tracking
import present_slack
from notify_testkit import NOW, _latest_render, _uuid
from slack_testkit import SLACK, _granted_card
from slack_testkit import isolated_slack_ledger as led
from test_card_part_cli import snapshot
from test_slack_body_limits_hotfix import MULTIBYTE, _old_failed_update

__all__ = ["led"]
TABLES = ('notification_cards', 'notification_renders', 'notification_render_parts',
          'notification_delivery_attempts', 'notify_outbox', 'artifacts', 'fetch_jobs')


@pytest.fixture(autouse=True)
def legacy_thread_bodies(monkeypatch):
    monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)


def originals(db):
    return {name: [tuple(row) for row in db.execute(f'SELECT * FROM {name} ORDER BY rowid')]
            for name in TABLES}


@pytest.fixture
def past(led, monkeypatch):
    world, _, failed, _, native = _old_failed_update(led, monkeypatch, MULTIBYTE)
    rejection = world.client.chat_update
    world.client.chat_update = native
    body = '完全合成の新しい本文。'
    with led.db:
        led.db.execute('UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100',
                       (body, hashlib.sha256(body.encode()).hexdigest()))
    notify_cards.sweep(led, SLACK, now=NOW + 2)
    asyncio.run(_granted_card(world.worker, led, world.root))
    successor = _latest_render(led)
    assert successor['render_rev'] > failed['render_rev'] and successor['state'] == 'delivered'
    part = led.db.execute("SELECT * FROM notification_render_parts WHERE delivery_id=? "
                          "AND kind='body_part' AND state='not_sent' ORDER BY idx LIMIT 1",
                          (failed['delivery_id'],)).fetchone()
    assert part is not None
    plan = tracking.build_plan(led.db, failed['delivery_id'], part['part_id'], cfg=SLACK)
    assert 'error' not in plan, plan
    req = {**plan, 'command_id': _uuid(920), 'actor': 'synthetic-operator',
           'human_confirmed': True, 'reason': '合成の過去の未配送事実は残して運用追跡を終了。'}
    return SimpleNamespace(ledger=led, db=led.db, world=world, failed=failed,
                           successor=successor, part=dict(part), req=req, rejection=rejection)


def test_ack_is_receipt_only_and_keeps_undelivered_totals_and_unknown_facts(past, monkeypatch):
    before = originals(past.db)
    wire_calls = len(past.world.client.calls)
    def forbidden(*args, **kwargs):
        pytest.fail('tracking acknowledgement attempted to create a send plan')
    monkeypatch.setattr(notify_cards, 'issue_render', forbidden)
    assert mcs_requests.validate(past.req) is None
    receipt = tracking.apply(past.ledger, past.req, SLACK, NOW + 3)
    assert receipt['outcome'] == 'applied' and receipt['delivery_outcome'] == 'not_sent'
    assert receipt['residual'] == 'unrecovered' and receipt['followup'] == 'none'
    assert originals(past.db) == before and len(past.world.client.calls) == wire_calls
    health = notify_scope.incomplete_parts_health(past.db, NOW + 4, SLACK)
    assert health['total'] == health['by_state']['not_sent'] == 1
    assert health['active_total'] == 0 and health['acknowledged_unresolved_total'] == 1
    assert health['unknown_parts_total'] == 0
    assert tracking.apply(past.ledger, past.req, SLACK, NOW + 5) == receipt
    conflict = tracking.apply(past.ledger, {**past.req, 'reason': 'changed'}, SLACK, NOW + 6)
    assert conflict == {'outcome': 'rejected', 'error': 'command_id_conflict'}
    assert originals(past.db) == before


def test_duplicate_approvals_count_targets_once_and_survive_sweep_time_and_spec_gc(past):
    assert tracking.apply(past.ledger, past.req, SLACK, NOW + 3)['outcome'] == 'applied'
    with past.db:
        past.db.execute('UPDATE notification_cards SET updated_at=updated_at+100')
        past.db.execute('UPDATE notification_renders SET spec_json=NULL')
    second = {**past.req, 'command_id': _uuid(921)}
    assert tracking.apply(past.ledger, second, SLACK, NOW + 4)['outcome'] == 'applied'
    health = notify_scope.incomplete_parts_health(past.db, NOW + 5, SLACK)
    assert health['total'] == health['acknowledged_unresolved_total'] == 1
    assert health['active_total'] == 0


@pytest.mark.parametrize('fault', ['part_hash', 'attempt_unknown', 'successor_scope', 'card_scope', 'restore_hold'])
def test_changed_plan_is_rejected_without_mutating_transport_facts(past, fault):
    with past.db:
        if fault == 'part_hash':
            past.db.execute('UPDATE notification_render_parts SET payload_sha256=? WHERE delivery_id=? AND part_id=?',
                            ('b' * 64, past.part['delivery_id'], past.part['part_id']))
        elif fault == 'attempt_unknown':
            past.db.execute("UPDATE notification_delivery_attempts SET state='unknown' WHERE attempt_id=?",
                            (past.part['attempt_id'],))
        elif fault == 'successor_scope':
            past.db.execute('UPDATE notification_renders SET channel_id=? WHERE delivery_id=?',
                            ('C_OTHER_SYNTHETIC', past.successor['delivery_id']))
        elif fault == 'card_scope':
            past.db.execute('UPDATE notification_cards SET channel_id=?', ('C_OTHER_SYNTHETIC',))
        else:
            past.db.execute('INSERT INTO notification_restore_holds(card_id,reason,held_at) VALUES(?,?,?)',
                            (past.failed['card_id'], 'synthetic_restore', NOW))
    before = originals(past.db)
    result = tracking.apply(past.ledger, past.req, SLACK, NOW + 3)
    assert result['outcome'] == 'rejected' and result['error'] == 'tracking_ack_stale_plan'
    assert originals(past.db) == before


@pytest.mark.parametrize('state', ['unknown', 'pending', 'held'])
def test_unsettled_parts_cannot_be_closed_and_unknown_count_survives_card_revocation(past, state):
    with past.db:
        past.db.execute('UPDATE notification_render_parts SET state=? WHERE delivery_id=? AND part_id=?',
                        (state, past.part['delivery_id'], past.part['part_id']))
    assert 'error' in tracking.build_plan(past.db, past.part['delivery_id'], past.part['part_id'], cfg=SLACK)
    assert tracking.apply(past.ledger, past.req, SLACK, NOW + 3)['outcome'] == 'rejected'
    health = notify_scope.incomplete_parts_health(past.db, NOW + 4, SLACK)
    assert health['active_total'] == health['total'] == 1
    if state == 'unknown':
        with past.db:
            past.db.execute('UPDATE notification_cards SET revoked_at=?,delivery_state=?', (NOW, 'revoked'))
        health = notify_scope.incomplete_parts_health(past.db, NOW + 5, SLACK)
        assert health['total'] == 0 and health['unknown_parts_total'] == 1


def test_current_failure_without_a_delivered_successor_is_not_a_history_ack(led, monkeypatch):
    _, _, failed, _, _ = _old_failed_update(led, monkeypatch, MULTIBYTE)
    result = tracking.build_plan(led.db, failed['delivery_id'], 'body:0001', cfg=SLACK)
    assert 'error' in result


def test_scope_change_invalidates_old_applied_ack_even_when_replay_returns_old_receipt(past):
    receipt = tracking.apply(past.ledger, past.req, SLACK, NOW + 3)
    with past.db:
        past.db.execute('UPDATE notification_renders SET channel_id=? WHERE delivery_id=?',
                        ('C_OTHER_SYNTHETIC', past.successor['delivery_id']))
    assert tracking.apply(past.ledger, past.req, SLACK, NOW + 4) == receipt
    health = notify_scope.incomplete_parts_health(past.db, NOW + 5, SLACK)
    assert health['active_total'] == health['total'] == 1
    assert health['acknowledged_unresolved_total'] == 0


def test_forged_receipt_cannot_quiet_the_failure(past):
    receipt = tracking.apply(past.ledger, past.req, SLACK, NOW + 3)
    receipt['human_confirmed'] = False
    with past.db:
        past.db.execute('UPDATE command_receipts SET receipt_json=? WHERE command_id=?',
                        (json.dumps(receipt), past.req['command_id']))
    with pytest.raises(ValueError, match='tracking_ack_receipt_unverified'):
        notify_scope.incomplete_parts_health(past.db, NOW + 4, SLACK)


def test_new_failures_are_active_after_historical_ack(past, monkeypatch):
    assert tracking.apply(past.ledger, past.req, SLACK, NOW + 3)['outcome'] == 'applied'
    from adapters.common import present_slack
    from adapters.slack import delivery
    import notify_spool
    body = MULTIBYTE + '完全合成の新しい失敗。'
    with past.db:
        past.db.execute('UPDATE messages SET body_text=?,content_hash=? WHERE message_id=100',
                        (body, hashlib.sha256(body.encode()).hexdigest()))
    past.world.client.chat_update = past.rejection
    with monkeypatch.context() as old:
        old.setattr(present_slack, 'THREAD_PART_WIRE_LIMIT', None)
        old.setattr(delivery, 'BODY_WIRE_MAX', 100000)
        with past.db:
            notify_cards.issue_render(past.db, past.failed['card_id'], SLACK, NOW + 4, specs := [])
        notify_spool.publish_specs(past.db, notify_spool.notify_dirs(str(past.world.root)), specs, NOW + 4)
        asyncio.run(_granted_card(past.world.worker, past.ledger, past.world.root))
    health = notify_scope.incomplete_parts_health(past.db, NOW + 5, SLACK)
    assert health['acknowledged_unresolved_total'] == 1 and health['active_total'] >= 1
    assert health['total'] == health['active_total'] + 1


def test_cli_plan_enqueue_and_existing_command_drain_are_one_receipted_path(past, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mcs_util, 'load_config_checked', lambda: SLACK)
    monkeypatch.setattr(mcs_util, 'load_config', lambda *args: SLACK)
    snap = snapshot(past.ledger, tmp_path)
    inbox = tmp_path / 'cmd'
    inbox.mkdir()
    common = ['--snapshot', snap, '--cmd-dir', str(inbox)]
    before = originals(past.db)
    assert mcs_view.main(common + ['card_part_tracking_plan', '--delivery-id', past.part['delivery_id'],
                                 '--part-id', past.part['part_id']]) == 0
    assert json.loads(capsys.readouterr().out)['targets'] == past.req['targets']
    envelope = {k: v for k, v in past.req.items() if k not in ('cmd', 'version', 'human_confirmed')}
    monkeypatch.setattr(mcs_view.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps(envelope).encode())))
    assert mcs_view.main(common + ['control', 'card_part_tracking_ack', '--confirm-human']) == 0
    assert json.loads(capsys.readouterr().out)['outcome'] == 'queued'
    assert originals(past.db) == before
    result = {'errors': []}
    job_ops.drain_commands(past.ledger, result, str(inbox))
    assert not result['errors']
    receipt = json.loads(past.db.execute('SELECT receipt_json FROM command_receipts WHERE command_id=?',
                                       (past.req['command_id'],)).fetchone()[0])
    assert receipt['outcome'] == 'applied' and receipt['kind'] == tracking.COMMAND
    assert originals(past.db) == before


def test_observer_never_accepts_arbitrary_active_count_reductions(past):
    raw = notify_scope.incomplete_parts_health(past.db, NOW + 4, SLACK)
    assert health_watch._tracking_parts(raw)['total'] == 1
    bad = copy.deepcopy(raw)
    bad.update(active_total=0, acknowledged_unresolved_total=1)
    bad['active_by_state']['not_sent'] = 0
    bad['active_by_plan']['current'] = 0
    bad['active_by_plan']['superseded'] = 0
    # This is a past fact, so its reduction is structurally valid; current or unknown reductions are not.
    assert health_watch._tracking_parts(bad)['total'] == 0
    bad['by_plan'] = {'current': 1, 'superseded': 0}
    assert health_watch._tracking_parts(bad) is None
    bad = copy.deepcopy(raw)
    bad['by_state'] = {'pending': 0, 'unknown': 1, 'not_sent': 0, 'held': 0}
    bad['active_by_state'] = {'pending': 0, 'unknown': 0, 'not_sent': 0, 'held': 0}
    bad.update(active_total=0, acknowledged_unresolved_total=1, unknown_parts_total=1)
    bad['active_by_plan'] = {'current': 0, 'superseded': 0}
    assert health_watch._tracking_parts(bad) is None


def test_observer_global_unknown_cannot_be_hidden_by_zero_current_parts(tmp_path):
    states = dict.fromkeys(('pending', 'unknown', 'not_sent', 'held'), 0)
    plans = {'current': 0, 'superseded': 0}
    parts = {'total': 0, 'cards': 0, 'by_state': states, 'by_plan': plans,
             'active_total': 0, 'active_cards': 0, 'active_by_state': states,
             'active_by_plan': plans, 'acknowledged_unresolved_total': 0, 'unknown_parts_total': 1}
    path = tmp_path / 'health.json'
    path.write_text(json.dumps({'at': NOW, 'overall': 'ok', 'state_reasons': [],
                               'notify': {'incomplete_parts': parts}}))
    report = health_watch.classify_health(str(path), now=NOW, deadline_s=900)
    assert report['status'] == 'degraded'
    assert 'notification_parts_incomplete' in report['state_reasons']
    assert report['notify_unknown_part_outcomes'] == 1
