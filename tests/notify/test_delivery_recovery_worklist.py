"""Health worklist surfaces bounded, plan-verified recovery candidates."""
import json

import notify_recovery
import notify_scope
import notify_spool
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


def test_page_parameters_are_strictly_validated(stored):
    import pytest
    for bad in ({"limit": -1}, {"limit": 0}, {"limit": 21}, {"limit": "20"},
                {"limit": True}, {"offset": -1}, {"offset": "0"}, {"offset": True}):
        with pytest.raises(ValueError):
            notify_recovery.recovery_worklist(stored, SLACK, **bad)


def test_offset_pages_cover_the_tail_without_overlap(stored):
    for _ in range(21):
        _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
        _attempts(stored, delivery)
    first = notify_scope.health_cards(stored, SLACK)['recovery_parts']
    second = notify_recovery.recovery_worklist(stored, SLACK, offset=20)
    assert first['total'] == second['total'] == 21
    assert first['truncated'] is True and second['truncated'] is False
    assert len(second['items']) == 1 and second['offset'] == 20
    seen = {i['delivery_id'] for i in first['items']} | {i['delivery_id'] for i in second['items']}
    assert len(seen) == 21


def _live_data_dir(tmp_path):
    root = tmp_path / 'live-data'
    for state in ('discord_state', 'slack_state', 'lineworks_state'):
        (root / state).mkdir(parents=True)
    return root


def _journal(root, state, name, rows):
    (root / state / name).write_text(''.join(
        row if row.endswith('\n') else row + '\n' for row in rows))


def test_check_passes_on_clean_journal_without_contradiction(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': 'synthetic-attempt', 'delivery_id': delivery,
                    'part_id': 'part-0', 'phase': 'begin'})])
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert 'error' not in result
    assert result['journal_check'] == 'passed'
    assert result['journal']['state'] == 'clean'
    assert result['journal']['reason'] is None
    assert result['journal']['scope_rows'] == 1
    assert result['journal']['has_started'] is False
    assert result['journal_checks'] == {
        'no_journal_started': 'passed', 'api_rejected': 'blocked',
        'remote_absent': 'passed'}
    assert result['journal_blocked_reasons'] == ['api_rejection_unproven']
    assert result['proofs_checked'] == ['no_journal_started', 'api_rejected', 'remote_absent']
    assert result['required_external_checks'] == [
        'worker_stop_evidence', 'human_approval',
        'remote_identity_or_absence', 'apply_revalidation']
    assert result['expected_state'] == 'unknown' and result['attempt_id'] == 'synthetic-attempt'


def test_check_blocked_when_journal_records_delivery(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': 'synthetic-attempt', 'delivery_id': delivery,
                    'part_id': 'part-0', 'phase': 'result', 'result': 'delivered',
                    'remote_id': 'synthetic-post'})])
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert result['journal_check'] == 'blocked'
    assert result['journal_checks'] == {
        'no_journal_started': 'blocked', 'api_rejected': 'blocked',
        'remote_absent': 'blocked'}
    assert result['journal_blocked_reasons'] == ['journal_contradicts_proof']
    assert result['journal']['scope_rows'] == 1
    assert result['journal']['has_delivered_result'] is True
    assert result['journal']['remote_id_recorded'] is True
    assert result['journal']['phases'] == ['result']


def test_check_is_unknown_when_journal_unreadable_or_incomplete(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    missing = notify_recovery.card_part_check(
        stored, str(tmp_path / 'absent'), delivery, 'part-0')
    assert missing['journal_check'] == 'unknown'
    assert missing['journal']['state'] == 'unreadable'
    assert missing['journal']['reason'] is None
    assert missing['journal_checks'] == {
        'no_journal_started': 'unknown', 'api_rejected': 'unknown',
        'remote_absent': 'unknown'}
    for key in ('scope_rows', 'phases', 'has_started',
                'has_delivered_result', 'remote_id_recorded'):
        assert missing['journal'][key] is None
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-bad.jsonl', ['{"phase":"started"'])
    tainted = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert tainted['journal_check'] == 'unknown'
    assert tainted['journal']['state'] == 'incomplete'
    assert tainted['journal']['scope_rows'] is None
    assert tainted['journal_checks']['no_journal_started'] == 'unknown'


def test_check_is_unknown_when_journal_read_limited(stored, tmp_path, monkeypatch):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': 'synthetic-attempt', 'phase': 'begin'})])
    monkeypatch.setattr(notify_recovery, '_CARD_PART_CHECK_MAX_BYTES', 4)
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert result['journal_check'] == 'unknown'
    assert result['journal']['state'] == 'read_limited'
    assert result['journal']['reason'] == 'journal_read_limit'
    assert result['journal']['scope_rows'] is None
    assert result['journal_checks']['remote_absent'] == 'unknown'


def test_check_is_unknown_when_scope_has_no_journal_rows(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert result['journal_check'] == 'unknown'
    assert result['journal']['state'] == 'absent'
    assert result['journal']['scope_rows'] is None
    assert result['journal_checks'] == {
        'no_journal_started': 'unknown', 'api_rejected': 'unknown',
        'remote_absent': 'unknown'}


def test_check_scope_ignores_other_delivery_and_part(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    other = '123e4567-e89b-42d3-a456-426614174999'
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': 'synthetic-attempt', 'delivery_id': other,
                    'part_id': 'part-0', 'phase': 'result', 'result': 'delivered'}),
        json.dumps({'attempt_id': 'synthetic-attempt', 'delivery_id': delivery,
                    'part_id': 'other-part', 'phase': 'result', 'result': 'delivered'})])
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert result['journal_check'] == 'unknown'
    assert result['journal']['state'] == 'absent'
    assert result['journal']['has_delivered_result'] is None


def test_check_discord_member_uses_anchor_plan_scope(led, tmp_path):
    from pathlib import Path
    render, spec = _body_render(led)
    group = _groups(spec)[0]
    attempt = 'c' * 16
    with led.db:
        led.db.execute("UPDATE notification_render_parts SET attempt_id=? WHERE delivery_id=?",
                       (attempt, render['delivery_id']))
        led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE delivery_id=?",
                       (render['delivery_id'],))
    root = Path(notify_spool.data_root(led))
    notify_spool.ensure_dirs(str(root))
    _journal(root, 'discord_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': attempt, 'delivery_id': render['delivery_id'],
                    'part_id': group['surface'], 'phase': 'begin'})])
    member = group['members'][0]
    result = notify_recovery.card_part_check(led, str(root), render['delivery_id'], member)
    assert 'error' not in result
    assert result['part_id'] == group['part_id']
    assert result['journal_check'] == 'passed'
    assert result['journal_checks']['no_journal_started'] == 'passed'
    assert result['journal']['scope_rows'] == 1
    assert result['journal']['has_started'] is False


def test_check_returns_plan_error_for_ineligible_part(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    result = notify_recovery.card_part_check(
        stored, str(_live_data_dir(tmp_path)), delivery, 'missing-part')
    assert result == {'error': 'unknown_part'}


def test_check_dependency_group_uses_fixed_proof(stored, tmp_path):
    _card, delivery = _render(stored, [('file_group', 'not_sent', 'stage_receipt_missing')])
    attempt = f"p:{delivery.replace('-', '')}:file_group"
    stored.execute("UPDATE notification_render_parts SET part_id='file_group',attempt_id=? "
                   "WHERE delivery_id=?", (attempt, delivery))
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': attempt, 'delivery_id': delivery,
                    'part_id': 'file_group', 'phase': 'begin'})])
    result = notify_recovery.card_part_check(stored, str(root), delivery, 'file_group')
    assert 'error' not in result
    assert result['expected_state'] == 'not_sent'
    assert result['proofs_checked'] == ['dependency_rejected']
    assert result['journal_checks'] == {'dependency_rejected': 'blocked'}
    assert result['journal_check'] == 'blocked'
    assert 'dependency_rejection_unproven' in result['journal_blocked_reasons']
    assert result['journal']['scope_rows'] == 1


def test_check_reads_without_writes(stored, tmp_path):
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    root = _live_data_dir(tmp_path)
    _journal(root, 'slack_state', 'journal-w1.jsonl', [
        json.dumps({'attempt_id': 'synthetic-attempt', 'delivery_id': delivery,
                    'part_id': 'part-0', 'phase': 'begin'})])
    before = [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')]
    changes, in_transaction = stored.total_changes, stored.in_transaction
    notify_recovery.card_part_check(stored, str(root), delivery, 'part-0')
    assert [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')] == before
    assert stored.total_changes == changes and stored.in_transaction == in_transaction
    _card, delivery = _render(stored, [('body_part', 'unknown', 'timeouterror')])
    _attempts(stored, delivery)
    before = [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')]
    changes, in_transaction = stored.total_changes, stored.in_transaction
    notify_scope.health_cards(stored, SLACK)
    assert [tuple(row) for row in stored.execute('SELECT * FROM notification_render_parts')] == before
    assert stored.total_changes == changes and stored.in_transaction == in_transaction
