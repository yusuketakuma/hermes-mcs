"""Card latency and card-less notice parts are observed on their own delivery evidence."""
import notify_scope
from notify_testkit import CFG, NOW as EVENT_NOW, led as led
from test_incomplete_part_health import NOW, _render, stored as stored
from test_notification_delivery_guidance import _complete_render

LINEWORKS = {'notify': {'interactive': 'lineworks'}}


def test_latency_after_a_later_update_is_measured_on_the_first_delivery(led, monkeypatch):  # noqa: F811 - pytest injects the imported fixture
    create = _complete_render(led)
    db = led.db
    # Hours later an ack/extraction update bumps the generations and becomes the latest render.
    db.execute("UPDATE notification_cards SET source_generation=source_generation+1,"
               "presentation_generation=presentation_generation+1 WHERE card_id=?", (create['card_id'],))
    card = db.execute("SELECT * FROM notification_cards WHERE card_id=?", (create['card_id'],)).fetchone()
    manifest = db.execute(
        "INSERT INTO notification_view_manifests(card_id,render_rev,source_generation,presentation_generation,"
        "digest,shown,created_at) SELECT card_id,?,?,?,digest,shown,? FROM notification_view_manifests "
        "WHERE manifest_id=?", (create['render_rev'] + 1, card['source_generation'],
                                card['presentation_generation'], EVENT_NOW + 3500, create['manifest_id'])).lastrowid
    db.execute(
        "INSERT INTO notification_renders(delivery_id,card_id,op,render_rev,manifest_id,route_epoch,profile,"
        "application_id,guild_id,channel_id,transport,team_id,payload_hash,correlation,state,parts_state,"
        "created_at,updated_at) SELECT 'synthetic-update',card_id,'update',render_rev+1,?,route_epoch,profile,"
        "application_id,guild_id,channel_id,transport,team_id,payload_hash,'synthetic-update-correlation',"
        "'delivered','complete',?,? FROM notification_renders WHERE delivery_id=?",
        (manifest, EVENT_NOW + 3500, EVENT_NOW + 3600, create['delivery_id']))
    db.execute(
        "INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,payload_sha256,state,remote_id,updated_at) "
        "SELECT 'synthetic-update',part_id,kind,idx,payload_sha256,'delivered','synthetic-remote',? "
        "FROM notification_render_parts WHERE delivery_id=?", (EVENT_NOW + 3600, create['delivery_id']))
    monkeypatch.setattr(notify_scope.time, 'time', lambda: EVENT_NOW + 7200)
    latency = notify_scope.health_cards(db, CFG, latency=True)['completion']['latency']
    assert latency['status'] == 'recorded' and latency['examined'] == latency['count'] == 1
    assert latency['min_s'] == latency['max_s'] == latency['p50_s'] == 30
    assert latency['unavailable_by_reason'] == {}


def _notice(db, states, transport):
    card, delivery = _render(db, states, transport=transport)
    db.execute("UPDATE notification_renders SET card_id=NULL,op='notice' WHERE delivery_id=?", (delivery,))
    db.execute("DELETE FROM notification_cards WHERE card_id=?", (card,))
    return delivery


def test_cardless_notice_not_sent_overflow_part_is_counted(stored):  # noqa: F811 - pytest injects the imported fixture
    _notice(stored, [('card', 'delivered', None), ('body_part', 'not_sent', 'synthetic_failure')], 'lineworks')
    report = notify_scope.incomplete_parts_health(stored, NOW, LINEWORKS)
    assert report['total'] == report['active_total'] == 1
    assert report['by_state']['not_sent'] == report['active_by_state']['not_sent'] == 1
    assert report['cards'] == report['active_cards'] == 0
    assert report['oldest_age_s'] == 60


def test_cardless_notice_length_rejection_does_not_retain_its_spec(stored):  # noqa: F811 - pytest injects the imported fixture
    delivery = _notice(stored, [('card', 'delivered', None), ('body_part', 'not_sent', 'msg_too_long')], 'slack')
    assert notify_scope.incomplete_parts_health(stored, NOW, {'notify': {'interactive': 'slack'}})['total'] == 1
    assert notify_scope.unresolved_length_deliveries(stored, [delivery]) == set()
