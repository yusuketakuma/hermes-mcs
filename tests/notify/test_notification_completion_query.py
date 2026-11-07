"""Latest-render completion reuses its population without changing receipt counts."""
from itertools import count

import pytest

import health_report
import notify_scope
from test_incomplete_part_health import NOW, SLACK, _render, stored as stored
from test_notification_health_indexes import _seed


class OriginalQuery:
    """Replay the original SQL through the same reader and result assembly."""

    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        sql = sql.replace('WITH latest AS MATERIALIZED (', 'WITH latest AS (')
        sql = sql.replace(
            'FROM latest l CROSS JOIN notification_render_parts p ON p.delivery_id=l.delivery_id',
            'FROM notification_render_parts p JOIN latest l ON l.delivery_id=p.delivery_id')
        return self.db.execute(sql, *args)


def _complete(db, states, **kwargs):
    card, delivery = _render(db, states, **kwargs)
    db.execute("UPDATE notification_renders SET parts_state='complete' WHERE delivery_id=?", (delivery,))
    return card, delivery


def _history(db):
    delivered = [('card', 'delivered', None), ('body_part', 'delivered', None)]
    _complete(db, delivered)
    _complete(db, [('card', 'delivered', None), ('body_part', 'unknown', None)])
    _complete(db, [('card', 'delivered', None), ('attachment_part', 'pending', None)])
    _complete(db, [('card', 'unknown', None)], render_state='unknown')
    _complete(db, [])  # a cached complete flag cannot invent missing receipts
    for op in ('revoke', 'notice'):
        card, _ = _complete(db, delivered)
        _, latest = _complete(db, delivered, card=card, revision=2)
        db.execute('UPDATE notification_renders SET op=? WHERE delivery_id=?', (op, latest))
    card, _ = _complete(db, delivered)
    _complete(db, delivered, card=card, revision=2, transport='discord')
    card, _ = _complete(db, [('card', 'unknown', None)], render_state='unknown')
    _complete(db, delivered, card=card, revision=2, render_state='queued')
    _, unassigned = _complete(db, delivered)
    db.execute('UPDATE notification_renders SET card_id=NULL WHERE delivery_id=?', (unassigned,))
    db.commit()


@pytest.mark.parametrize('cfg,retired', [(None, False), (SLACK, True)])
@pytest.mark.parametrize('populated', [False, True])
def test_completion_keeps_original_counts_and_latest_revision_boundaries(stored, cfg, retired, populated):
    if populated:
        _history(stored)
    before = stored.total_changes
    observation = {}
    actual = notify_scope._delivery_completion(stored, cfg, NOW, observation)
    assert actual == notify_scope._delivery_completion(OriginalQuery(stored), cfg, NOW)
    expected = {
        'total': 6 if retired else 7,
        'primary_delivered': 4 if retired else 5,
        'all_parts_delivered': 1 if retired else 2,
        'primary_only_delivered': 3,
        'unknown': 2,
        'incomplete': 5,
        'parts_unobserved': 1,
    }
    assert {key: actual[key] for key in expected} == (expected if populated else dict.fromkeys(expected, 0))
    assert actual['status'] == 'recorded'
    assert actual['reason'] == ('part_plan_unobserved' if populated else None)
    assert observation == {'phase': 'delivery_latency'}
    assert stored.total_changes == before and not stored.in_transaction


@pytest.mark.parametrize('old_parts', [0, 80], ids=['original-history', 'more-old-parts'])
def test_completion_history_fits_existing_bounded_reader(stored, monkeypatch, old_parts):
    _seed(stored)  # 160 cards, 8 revisions each, 12,800 fully synthetic parts
    latest_parts = ("SELECT COUNT(*) FROM notification_render_parts WHERE delivery_id IN "
                    "(SELECT delivery_id FROM notification_renders WHERE render_rev=8)")
    assert stored.execute(latest_parts).fetchone()[0] == 1600
    prior = [r[0] for r in stored.execute(
        'SELECT delivery_id FROM notification_renders WHERE render_rev<8')]
    with stored:
        stored.executemany(
            'INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,name,updated_at) '
            "VALUES(?,?,'body_part',?,'delivered',?,1)",
            ((delivery, f'old-part-{i}', i + 10, f'synthetic-old-{i}')
             for delivery in prior for i in range(old_parts)))
    # Only historical parts grow: the population and current part plans are fixed.
    assert stored.execute('SELECT COUNT(*) FROM notification_renders').fetchone()[0] == 1280
    assert stored.execute(latest_parts).fetchone()[0] == 1600
    assert stored.execute('SELECT COUNT(*) FROM notification_render_parts').fetchone()[0] == (
        12800 + 1120 * old_parts)
    before = stored.total_changes
    original_wait = stored.execute('PRAGMA busy_timeout').fetchone()[0]
    # The reader polls this clock every 1,000 SQLite VM operations. A fixed
    # synthetic cost exposes repeated latest lookups without wall-time flakiness.
    ticks = count()
    monkeypatch.setattr(health_report.time, 'monotonic', lambda: next(ticks) * 0.005)
    observation = {}
    report = health_report.bounded_health_read(
        stored, lambda _check: notify_scope._delivery_completion(stored, None, NOW, observation),
        seconds=health_report.NOTIFICATION_OBSERVATION_S, observation=observation)
    assert report['total'] == report['primary_delivered'] == 160
    assert report['all_parts_delivered'] == 0
    assert observation['stop'] == 'complete' and observation['budget_s'] == 2.0
    assert observation['remaining_s'] > 0
    assert stored.total_changes == before and not stored.in_transaction
    assert stored.execute('PRAGMA busy_timeout').fetchone()[0] == original_wait
