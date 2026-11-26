"""A time-limited notification read keeps the last complete measurement, never a false all-clear."""
import json

import pytest

import health_report
import health_watch
import notify_scope
from ingest_testkit import _ledger


NOW = 200_000.0
SLACK = {'notify': {'interactive': 'slack'}}
SCOPE = ('synthetic-profile', 'synthetic-application', None, 'synthetic-team', 'synthetic-channel')
REASONS = ('notification_cards_unavailable', 'notification_parts_unavailable')


@pytest.fixture
def stored(tmp_path, monkeypatch):
    ledger = _ledger(tmp_path)
    monkeypatch.setattr(health_report.time, 'time', lambda: NOW)
    monkeypatch.setattr(health_report, 'HEALTH_FILE', str(tmp_path / 'health.json'))
    monkeypatch.setattr(health_report, 'attachment_free_mb', lambda path=None: 10_000)
    monkeypatch.setattr(health_report.local_backup, 'local_backup_status',
                        lambda **_: {'state': 'verified', 'newest_age_s': 50, 'observed_at': NOW})
    try:
        yield ledger.db
    finally:
        ledger.close()


def _parts(total=0, unknown=0, observed_at=NOW - 60, status='recorded'):
    states = {'pending': total - unknown, 'unknown': unknown, 'not_sent': 0, 'held': 0}
    plans = {'current': total, 'superseded': 0}
    return {'active_total': total, 'active_cards': int(bool(total)),
            'acknowledged_unresolved_total': 0, 'unknown_parts_total': unknown,
            'active_by_state': states, 'active_by_plan': plans, 'active_oldest_age_s': None,
            'total': total, 'cards': int(bool(total)), 'by_state': dict(states),
            'by_plan': dict(plans), 'oldest_age_s': None, 'observed_at': observed_at, 'status': status}


CARD_KEYS = ('pending', 'delivered', 'update_failed', 'delivery_unknown', 'message_deleted', 'revoked',
             'oldest_pending_age_s', 'layout_inventory', 'unknown_parts', 'recovery_parts',
             'unknown_part_resolution', 'renders_queued', 'renders_unknown', 'attempts_unsettled',
             'retired_unsettled', 'retired_items', 'oldest_unsettled_age_s', 'unsettled', 'last_delivered_at')


def _previous(path, observed_at, *, card_status=None, parts=None, partial=False):
    cards = {**dict.fromkeys(CARD_KEYS, 0), 'pending': 2, 'delivered': 97,
             'completion': {'status': 'recorded'}, 'observation': {'stop': 'complete'},
             'observed_at': observed_at}
    if partial:
        del cards['completion']
        parts = {key: value for key, value in _parts(observed_at=observed_at).items() if key != 'by_state'}
    if card_status:
        cards['status'] = card_status
    path.write_text(json.dumps({'at': observed_at, 'overall': 'ok', 'cards': cards,
                                'notify': {'incomplete_parts': parts or _parts(observed_at=observed_at)}}))


def _exhaust(monkeypatch):
    """Both notification reads run past their 2.0 s bound, as on the swapping host."""
    clock = [100.0]
    monkeypatch.setattr(health_report.time, 'monotonic', lambda: clock[0])

    def slow(*_args, **_kwargs):
        clock[0] += health_report.NOTIFICATION_OBSERVATION_S + 0.1
        return {}

    monkeypatch.setattr(notify_scope, 'health_cards', slow)
    monkeypatch.setattr(notify_scope, 'incomplete_parts_health', slow)


def _build(db, cfg=None):
    return health_report.build_health(db, {'errors': [], 'notify': {}}, 'ok',
                                      cfg={} if cfg is None else cfg)


def _watch(tmp_path, report, now):
    path = tmp_path / 'watched.json'
    path.write_text(json.dumps(report))
    return health_watch.classify_health(str(path), now, 3600)


@pytest.mark.parametrize('chained', [False, True])
def test_fresh_previous_measurement_is_carried_without_unavailable_alerts(stored, tmp_path, monkeypatch, chained):
    _previous(tmp_path / 'health.json', NOW - 60, card_status='stale_measurement' if chained else None,
              parts=_parts(status='stale_measurement' if chained else 'recorded'))
    _exhaust(monkeypatch)
    report = _build(stored)
    cards, parts = report['cards'], report['notify']['incomplete_parts']
    for block in (cards, parts):
        assert block['status'] == 'stale_measurement' and block['reason'] == 'observation_limit'
        # the original time is kept, so a chain of carries still expires
        assert block['observed_at'] == NOW - 60 and block['age_s'] == 60
    assert cards['pending'] == 2 and cards['delivered'] == 97
    assert cards['observation']['stop'] == 'time_limit'
    assert parts['active_total'] == 0 and parts['total'] == 0
    assert not set(REASONS) & set(report['state_reasons'])
    assert report['notify']['state'] == 'ok'
    watched = _watch(tmp_path, report, NOW + 1)
    assert not set(REASONS) & set(watched['state_reasons'] or ())


@pytest.mark.parametrize('previous', ['none', 'too_old', 'unknown', 'partial'])
def test_missing_or_old_previous_measurement_still_alerts(stored, tmp_path, monkeypatch, previous):
    if previous == 'too_old':
        _previous(tmp_path / 'health.json', NOW - health_report.CARD_STALL_S - 1)
    elif previous == 'partial':
        _previous(tmp_path / 'health.json', NOW - 60, partial=True)
    elif previous == 'unknown':
        _previous(tmp_path / 'health.json', NOW - 60, card_status='unknown',
                  parts=_parts(status='unknown'))
    _exhaust(monkeypatch)
    report = _build(stored)
    assert report['cards']['status'] == report['notify']['incomplete_parts']['status'] == 'unknown'
    assert report['cards']['pending'] is None
    assert set(REASONS) <= set(report['state_reasons'])
    assert report['overall'] == 'degraded'


def test_carried_real_gap_still_alerts(stored, tmp_path, monkeypatch):
    _previous(tmp_path / 'health.json', NOW - 60, parts=_parts(total=1, unknown=1))
    _exhaust(monkeypatch)
    report = _build(stored)
    assert report['notify']['incomplete_parts']['status'] == 'stale_measurement'
    assert 'notification_parts_incomplete' in report['state_reasons']
    assert 'notification_cards_unavailable' not in report['state_reasons']


def test_complete_measurement_with_unknown_part_alerts_despite_fresh_previous(stored, tmp_path):
    _previous(tmp_path / 'health.json', NOW - 60)
    card = stored.execute(
        "INSERT INTO notification_cards(card_key,kind,anchor_key,transport,delivery_state,profile,"
        "application_id,guild_id,team_id,channel_id,created_at,updated_at) "
        "VALUES('synthetic-card','thread','synthetic','slack','delivered',?,?,?,?,?,?,?)",
        (*SCOPE, NOW - 100, NOW)).lastrowid
    stored.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,transport,"
        "payload_hash,correlation,state,profile,application_id,guild_id,team_id,channel_id,"
        "created_at,updated_at) VALUES('synthetic-d',?,'update',1,1,'slack','synthetic-hash',"
        "'synthetic-d','delivered',?,?,?,?,?,?,?)", (card, *SCOPE, NOW - 100, NOW))
    stored.execute(
        "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,name,"
        "payload_sha256,updated_at) VALUES('synthetic-d','part-1','body_part',1,'unknown','m:1#1',?,?)",
        ('1' * 64, NOW - 60))
    report = _build(stored, SLACK)
    parts = report['notify']['incomplete_parts']
    assert parts['status'] == 'recorded' and parts['unknown_parts_total'] == 1
    assert 'notification_parts_incomplete' in report['state_reasons']


@pytest.mark.parametrize('observed_at,unavailable', [(NOW - 1700, False), (NOW - 1700 - 200, True)])
def test_watcher_expires_carried_measurement_at_the_same_age(tmp_path, observed_at, unavailable):
    block = {'status': 'stale_measurement', 'observed_at': observed_at, 'reason': 'observation_limit'}
    report = {'at': NOW, 'overall': 'ok', 'state_reasons': [], 'cards': dict(block),
              'notify': {'incomplete_parts': {**_parts(observed_at=observed_at), **block}}}
    watched = _watch(tmp_path, report, NOW + 100)
    assert (watched['status'] == 'degraded') is unavailable
    assert (set(REASONS) <= set(watched['state_reasons'])) is unavailable


def _seed_history(db, cards=300):
    """Many old unfinished parts: revoked cards, later proofs, and a few real gaps."""
    rows = {'cards': [], 'renders': [], 'parts': []}
    seq = 0
    for card in range(1, cards + 1):
        rows['cards'].append((card, f'synthetic-{card}', 'thread', 'synthetic', 'slack',
                              'revoked' if card % 3 else 'delivered', *SCOPE, float(card), float(card)))
        for rev in range(1, 5):
            seq += 1
            delivery = f'synthetic-{seq}'
            rows['renders'].append((delivery, card, 'update', rev, 1, 'slack', 'synthetic-hash', delivery,
                                    'delivered', *SCOPE, float(seq), float(seq)))
            for part in range(4):
                old = rev < 4 and part == 1
                # every 17th card's latest plan never confirms its part: a real gap
                gap = rev == 4 and part == 2 and card % 17 == 0
                state = ('pending', 'held', 'not_sent', 'unknown')[seq % 4] if old or gap else 'delivered'
                rows['parts'].append((delivery, f'part-{part}', 'body_part', part, state,
                                      f'm:{part}', f'{part + 1:064x}',
                                      f'synthetic-remote-{seq}-{part}' if state == 'delivered' else None,
                                      float(seq)))
    with db:
        db.executemany(
            'INSERT INTO notification_cards(card_id,card_key,kind,anchor_key,transport,delivery_state,'
            'profile,application_id,guild_id,team_id,channel_id,created_at,updated_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', rows['cards'])
        db.executemany(
            'INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,route_epoch,transport,'
            'payload_hash,correlation,state,profile,application_id,guild_id,team_id,channel_id,'
            'created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', rows['renders'])
        db.executemany(
            'INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,name,'
            'payload_sha256,remote_id,updated_at) VALUES(?,?,?,?,?,?,?,?,?)', rows['parts'])


class OriginalProof:
    """Replay the proof correlated on r.card_id, as before the card filter moved first."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        if 'done.card_id=c.card_id' in sql:
            sql = sql.replace('done.card_id=c.card_id', 'done.card_id=r.card_id', 1)
        return self.db.execute(sql, *args)


def _operations(db, read):
    steps = [0]

    def progress():
        steps[0] += 1
        return 0

    db.set_progress_handler(progress, 100)
    try:
        return read(), steps[0]
    finally:
        db.set_progress_handler(None, 0)


def test_cheaper_part_query_keeps_verdicts_on_old_history(stored):
    _seed_history(stored)
    assert stored.execute("SELECT COUNT(*) FROM notification_render_parts "
                          "WHERE state!='delivered'").fetchone()[0] > 900
    actual, steps = _operations(stored, lambda: notify_scope.incomplete_parts_health(stored, NOW, SLACK))
    expected, original_steps = _operations(
        stored, lambda: notify_scope.incomplete_parts_health(OriginalProof(stored), NOW, SLACK))
    assert actual == expected
    # cards 51, 102, 153, 204 and 255: live, divisible by 17, one unconfirmed latest part each
    assert actual['active_total'] == actual['total'] == 5 and actual['cards'] == 5
    assert steps < original_steps


def test_card_counts_without_latency_match_the_full_card_read(stored):
    # the fixture pins time.time for every module, so both reads share one clock
    _seed_history(stored)
    full = notify_scope.health_cards(stored, SLACK, latency=True)
    latency = full['completion'].pop('latency')
    assert notify_scope.health_cards(stored, SLACK) == full
    assert latency == notify_scope._delivery_latency(stored, notify_scope.retired_transports(SLACK), NOW)


@pytest.mark.parametrize('age,reason,carried', [
    (health_report.CARD_STALL_S, 'observation_limit', True),
    (health_report.CARD_STALL_S + 1, 'observation_limit', False),
    (60, 'observation_unavailable', False)])
def test_card_carry_keeps_its_own_age_and_reason_despite_the_longer_context_carry(age, reason, carried):
    """The patient-context carry bound and reasons are opt-in; the card defaults are unchanged."""
    assert health_report.PATIENT_CONTEXT_CARRY_S > health_report.CARD_STALL_S + 1
    current = {'status': 'unknown', 'observed_at': NOW, 'reason': reason}
    result = health_report.carried_measurement(current, _parts(observed_at=NOW - age), NOW)
    assert (result['status'] == 'stale_measurement') is carried
    if carried:
        assert result['observed_at'] == NOW - age and result['age_s'] == age
    else:
        assert result is current
