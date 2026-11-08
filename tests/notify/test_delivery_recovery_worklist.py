"""Health worklist surfaces bounded, plan-verified recovery candidates."""
import json

import notify_recovery
import notify_scope
from notify_testkit import CFG, led as led
from test_discord_file_group_settlement import _body_render, _groups
from test_incomplete_part_health import SLACK, _render, stored as stored


def _attempts(db, delivery):
    db.execute("UPDATE notification_render_parts SET attempt_id='synthetic-attempt' "
               "WHERE delivery_id=?", (delivery,))


def test_unknown_part_with_attempt_is_plannable(stored):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    report = notify_scope.health_cards(stored, SLACK)
    item, = report['recovery_parts']['items']
    assert report['recovery_parts']['total'] == 1
    assert not report['recovery_parts']['truncated']
    assert item['flow'] == 'card_part_resolve' and item['status'] == 'plannable'
    assert item['delivery_id'] == delivery and item['part_id'] == 'part-0'
    assert item['kind'] == 'body_part' and item['state'] == 'unknown'
    assert item['transport'] == 'slack' and item['error_code'] == 'timeouterror'
    assert item['attempt_id'] == 'synthetic-attempt' and item['age_s'] == 60
    assert 'blocker' not in item and 'approval_part_id' not in item


def test_unknown_part_without_attempt_is_blocked_with_fixed_reason(stored):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    item, = notify_scope.health_cards(stored, SLACK)['recovery_parts']['items']
    assert item['status'] == 'blocked' and item['blocker'] == 'part_not_resolvable'
    assert item['attempt_id'] is None


def test_settled_not_sent_part_is_not_a_candidate(stored):
    _render(stored, [('body_part', 'not_sent', 'worker_crash')])
    _render(stored, [('file_group', 'not_sent', 'some_other_code')])
    report = notify_scope.health_cards(stored, SLACK)['recovery_parts']
    assert report['total'] == 0 and report['items'] == []


def test_dependency_rejected_group_is_plannable(stored):
    _card, delivery = _render(stored, [('file_group', 'not_sent', 'stage_receipt_missing')])
    attempt = f"p:{delivery.replace('-', '')}:file_group"
    stored.execute("UPDATE notification_render_parts SET part_id='file_group',attempt_id=? "
                   "WHERE delivery_id=?", (attempt, delivery))
    item, = notify_scope.health_cards(stored, SLACK)['recovery_parts']['items']
    assert item['state'] == 'not_sent' and item['part_id'] == 'file_group'
    assert item['flow'] == 'card_part_resolve' and item['status'] == 'plannable'
    assert item['error_code'] == 'stage_receipt_missing'


def test_held_identity_part_uses_confirm_flow(led, tmp_path, monkeypatch):
    from slack_testkit import legacy_attachment_parts
    from test_attachment_upload_identity import _held_collected, _uploaded
    legacy_attachment_parts(monkeypatch)
    uploaded = _uploaded(led, tmp_path, monkeypatch)
    _world, _prior, latest, held, _part, _attachment_id = _held_collected(led, uploaded)
    item, = [i for i in notify_recovery.recovery_worklist(led.db, SLACK)['items']
             if i['delivery_id'] == latest['delivery_id'] and i['part_id'] == held['part_id']]
    assert item['flow'] == 'attachment_identity_confirm' and item['status'] == 'plannable'
    assert item['part_id'] == held['part_id'] and item['state'] == 'held'
    assert item['error_code'] == 'attachment_upload_identity_unverified'
    assert item['attempt_id'] is None


def test_discord_member_names_group_anchor(led):
    render, spec = _body_render(led)
    group = _groups(spec)[0]
    attempt = "c" * 16
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET attempt_id=? WHERE delivery_id=?",
                       (attempt, render["delivery_id"]))
        led.db.execute("UPDATE notification_render_parts SET state='unknown' "
                       "WHERE delivery_id=?", (render["delivery_id"],))
    items = notify_recovery.recovery_worklist(led.db, CFG)['items']
    member = next(i for i in items if i['part_id'] == group['members'][0])
    assert member['status'] == 'plannable' and member['flow'] == 'card_part_resolve'
    assert member['approval_part_id'] == group['part_id'] and member['group_approval'] is True
    anchor = next(i for i in items if i['part_id'] == group['part_id'])
    assert anchor['status'] == 'plannable' and 'approval_part_id' not in anchor


def test_cancelled_and_retired_are_excluded(stored):
    _render(stored, [('body_part', 'unknown', 'timeouterror')], render_state='cancelled')
    _render(stored, [('body_part', 'unknown', 'timeouterror')], transport='discord')
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    report = notify_scope.health_cards(stored, SLACK)['recovery_parts']
    assert report['total'] == 1
    assert [i['delivery_id'] for i in report['items']] == [delivery]


def test_worklist_is_bounded_with_total_and_truncation(stored):
    for _ in range(21):
        _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
        _attempts(stored, delivery)
    report = notify_scope.health_cards(stored, SLACK)['recovery_parts']
    assert report['total'] == 21 and len(report['items']) == 20
    assert report['truncated'] is True


def test_fixed_producer_codes_are_preserved(stored):
    _card, delivery = _render(stored, [('file_group', 'unknown', 'file_group_unverified')])
    _attempts(stored, delivery)
    item, = notify_scope.health_cards(stored, SLACK)['recovery_parts']['items']
    assert item['error_code'] == 'file_group_unverified'
    assert notify_recovery.recovery_guidance(
        'file_group', 'discord', 'file_group_unverified', 'a')['error_code'] == 'file_group_unverified'
    assert notify_recovery.part_hold_report(stored)['parts']['unknown']['file_group_unverified'] == 1


def test_unrecognized_code_still_collapses_without_leaking(stored):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'synthetic-private-text')])
    _attempts(stored, delivery)
    report = notify_scope.health_cards(stored, SLACK)
    item, = report['recovery_parts']['items']
    assert item['error_code'] == 'unknown'
    assert 'synthetic-private-text' not in json.dumps(report['recovery_parts'])


def test_worklist_reads_without_writes(stored):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    before = [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')]
    changes, in_transaction = stored.total_changes, stored.in_transaction
    notify_scope.health_cards(stored, SLACK)
    assert [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')] == before
    assert stored.total_changes == changes and stored.in_transaction == in_transaction
