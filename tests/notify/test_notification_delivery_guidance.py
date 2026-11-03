"""Delivery evidence and recovery guidance never imply approval or source validity."""
import json

import pytest

import notify_recovery
import notify_scope
from test_incomplete_part_health import NOW, SLACK, _render, stored as stored
from notify_testkit import (CFG, NOW as EVENT_NOW, _dispatch, _intent, _latest_render,
                            _seed_thread, led as led)


def _state(db, delivery, state):
    db.execute("UPDATE notification_renders SET parts_state=? WHERE delivery_id=?", (state, delivery))


def test_primary_delivery_is_distinct_from_complete_and_unknown_parts(stored):
    _, full = _render(stored, [('card', 'delivered', None), ('body_part', 'delivered', None)])
    _state(stored, full, 'complete')
    _, uncertain = _render(stored, [('card', 'delivered', None), ('attachment_part', 'unknown', 'timeouterror')])
    _state(stored, uncertain, 'incomplete')
    _, pending = _render(stored, [('card', 'delivered', None), ('body_part', 'pending', None)])
    _state(stored, pending, 'pending')
    _render(stored, [('card', 'unknown', None)], render_state='unknown')
    _render(stored, [])
    before = stored.total_changes
    report = notify_scope.health_cards(stored, SLACK)['completion']
    assert report['total'] == 5
    assert report['primary_delivered'] == 4
    assert report['all_parts_delivered'] == 1
    assert report['primary_only_delivered'] == 3
    assert report['unknown'] == 2 and report['incomplete'] == 4
    assert report['parts_unobserved'] == 1 and report['reason'] == 'part_plan_unobserved'
    assert report['status'] == 'recorded' and report['observed_at'] == NOW
    assert 'not_current_clinical_validity_or_readership' in report['basis']
    assert stored.total_changes == before and stored.in_transaction


def test_completion_does_not_reuse_old_success_after_pending_update(stored):
    card, first = _render(stored, [('card', 'delivered', None), ('body_part', 'delivered', None)])
    _state(stored, first, 'complete')
    _, latest = _render(stored, [('card', 'pending', None)], card=card, revision=2, render_state='queued')
    report = notify_scope.health_cards(stored, SLACK)['completion']
    assert report['total'] == 1 and report['incomplete'] == 1
    assert report['primary_delivered'] == report['all_parts_delivered'] == 0
    _state(stored, latest, 'complete')  # cached completion alone cannot prove the part plan
    assert notify_scope.health_cards(stored, SLACK)['completion']['all_parts_delivered'] == 0


def test_completion_excludes_retired_transports_and_unassigned_notices(stored):
    _, slack = _render(stored, [('card', 'delivered', None)])
    _state(stored, slack, 'complete')
    _, discord = _render(stored, [('card', 'delivered', None)], transport='discord')
    _state(stored, discord, 'complete')
    _, notice = _render(stored, [('card', 'delivered', None)])
    _state(stored, notice, 'complete')
    stored.execute("UPDATE notification_renders SET card_id=NULL,op='notice' WHERE delivery_id=?", (notice,))
    report = notify_scope.health_cards(stored, SLACK)['completion']
    assert report['total'] == report['all_parts_delivered'] == 1


@pytest.mark.parametrize('kind,command,preview', [
    ('card', 'ops.card_resolve', None),
    ('thread', 'ops.card_part_resolve', 'card_part_plan'),
    ('body_part', 'ops.card_part_resolve', 'card_part_plan'),
    ('attachment_part', 'ops.card_part_resolve', 'card_part_plan'),
])
def test_unknown_worklist_routes_to_existing_recovery_with_required_evidence(stored, kind, command, preview):
    _card, delivery = _render(stored, [(kind, 'unknown', 'timeouterror')])
    stored.execute("UPDATE notification_render_parts SET attempt_id='synthetic-attempt' WHERE delivery_id=?", (delivery,))
    item = notify_scope.health_cards(stored, SLACK)['unknown_parts'][0]
    assert item['kind'] == kind and item['state'] == 'unknown'
    assert item['attempt_id'] == 'synthetic-attempt' and item['age_s'] == 60
    assert item['error_code'] == 'timeouterror'
    guide = item['recovery']
    assert guide['available'] and guide['command'] == command and guide['preview'] == preview
    assert guide['requirements']['human_confirmation']
    assert guide['requirements']['evidence_fields'] == ['method', 'ref']
    assert guide['requirements']['mark_delivered']['remote_identity'] == ('message_id' if kind == 'card' else 'remote_id')
    assert guide['requirements']['worker_stopped_for_all_results'] == (kind != 'card')
    assert guide['requirements']['mark_not_sent']['worker_stopped']
    assert guide['final_scope_and_journal_validation_required'] and not guide['automatic_resend']


def test_unknown_worklist_is_bounded_and_never_exposes_unrecognized_error_text(stored):
    for _ in range(21):
        _render(stored, [('body_part', 'unknown', 'synthetic-private-error-text')])
    report = notify_scope.health_cards(stored, SLACK)
    assert len(report['unknown_parts']) == 20
    assert all(item['error_code'] == 'unknown' for item in report['unknown_parts'])
    assert all(not item['recovery']['available'] for item in report['unknown_parts'])
    assert 'synthetic-private-error-text' not in json.dumps(report)


def test_guidance_retains_lineworks_and_preflight_proof_conditions():
    lw = notify_recovery.recovery_guidance('attachment_part', 'lineworks', None, 'synthetic-attempt')
    proof = lw['requirements']['mark_not_sent']
    assert proof['remote_absent_requires_duplicate_risk_accepted']
    assert proof['remote_absence_is_not_delivery_proof']
    preflight = notify_recovery.recovery_guidance('attachment_part', 'slack', 'edit_target_unverified', 'a')
    assert preflight['requirements']['mark_not_sent']['conditional_proof'] == 'local_preflight_rejected'
    assert preflight['requirements']['mark_not_sent']['conditional_proof_requires_current_sealed_preflight']


def _complete_render(led):
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    led.db.execute("UPDATE messages SET first_seen=? WHERE message_id=100", (EVENT_NOW - 10,))
    led.db.execute("UPDATE notification_renders SET state='delivered',parts_state='complete',updated_at=? "
                   "WHERE delivery_id=?", (EVENT_NOW + 20, render['delivery_id']))
    led.db.execute("UPDATE notification_render_parts SET state='delivered',remote_id='synthetic-remote',updated_at=? "
                   "WHERE delivery_id=?", (EVENT_NOW + 20, render['delivery_id']))
    return render


def test_latency_samples_bound_root_to_full_plan_receipt_confirmation(led, monkeypatch):
    _complete_render(led)
    monkeypatch.setattr(notify_scope.time, 'time', lambda: EVENT_NOW + 30)
    before = led.db.total_changes
    latency = notify_scope.health_cards(led.db, CFG)['completion']['latency']
    assert latency['status'] == 'recorded'
    assert latency['examined'] == latency['count'] == 1
    assert latency['min_s'] == latency['max_s'] == latency['p50_s'] == latency['p95_s'] == 30
    assert latency['unavailable_by_reason'] == {}
    assert 'not_remote_arrival_or_readership' in latency['basis']
    assert led.db.total_changes == before


@pytest.mark.parametrize('damage,reason', [
    ('missing_first_seen', 'timestamps_unavailable'),
    ('future_receipt', 'timestamps_unavailable'),
    ('reversed_receipt', 'timestamps_reversed'),
    ('source_changed', 'source_changed'),
    ('missing_manifest', 'source_binding_unverified'),
])
def test_latency_unknowns_are_not_zero_or_success(led, monkeypatch, damage, reason):
    render = _complete_render(led)
    if damage == 'missing_first_seen':
        led.db.execute("UPDATE messages SET first_seen=NULL WHERE message_id=100")
    elif damage in ('future_receipt', 'reversed_receipt'):
        at = EVENT_NOW + 60 if damage == 'future_receipt' else EVENT_NOW - 1
        led.db.execute("UPDATE notification_render_parts SET updated_at=? WHERE delivery_id=?",
                       (at, render['delivery_id']))
    elif damage == 'source_changed':
        led.db.execute("UPDATE messages SET content_hash=? WHERE message_id=100", ('f' * 64,))
    else:
        led.db.execute("UPDATE notification_renders SET manifest_id=NULL WHERE delivery_id=?", (render['delivery_id'],))
    monkeypatch.setattr(notify_scope.time, 'time', lambda: EVENT_NOW + 30)
    report = notify_scope.health_cards(led.db, CFG)['completion']
    assert report['all_parts_delivered'] == 1  # recorded delivery is independent of current source validity
    latency = report['latency']
    assert latency['status'] == 'unavailable' and latency['count'] == 0
    assert latency['unavailable_by_reason'] == {reason: 1}
    assert all(latency[key] is None for key in ('min_s', 'max_s', 'p50_s', 'p95_s'))


def test_latency_examines_at_most_twenty_current_complete_renders(stored):
    for _ in range(21):
        _, delivery = _render(stored, [('card', 'delivered', None)])
        _state(stored, delivery, 'complete')
    latency = notify_scope.health_cards(stored, SLACK)['completion']['latency']
    assert latency['examined'] == latency['sample_limit'] == 20
    assert latency['count'] == 0 and latency['p50_s'] is None
    assert latency['unavailable_by_reason'] == {'source_binding_unverified': 20}
