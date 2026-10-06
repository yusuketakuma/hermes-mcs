"""A mixed incomplete plan still needs its worker spec while any part remains pending."""
from pathlib import Path

import notify_cards
from notify_testkit import CFG, NOW, _begin, _dispatch, _intent, _latest_render, _receipt, _seed_thread
from test_notify_parts import _part_receipt, led as led


def test_incomplete_render_keeps_spec_until_all_pending_parts_settle(led):
    _seed_thread(led)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    _receipt(led, render, f"{1:016x}")
    body = led.db.execute("SELECT part_id FROM notification_render_parts WHERE delivery_id=? "
                          "AND kind='body_part' ORDER BY idx LIMIT 1", (render["delivery_id"],)).fetchone()[0]
    _part_receipt(led, render, body, result="unknown", remote_id=None)
    assert led.db.execute("SELECT parts_state FROM notification_renders WHERE delivery_id=?",
                          (render["delivery_id"],)).fetchone()[0] == "incomplete"
    spec = Path(notify_cards.notify_dirs(notify_cards.data_root(led))["discord_render"]) / (
        render["delivery_id"] + ".json")
    assert spec.is_file()
    assert notify_cards.gc(led, CFG, now=NOW + 1)["spec_files"] == 0
    assert spec.is_file()
    assert notify_cards.recover(led, CFG, {})["republished"] == 0
    sealed = spec.read_bytes()
    spec.unlink()  # simulate a spec removed by an older GC
    assert notify_cards.recover(led, CFG, {})["republished"] == 1
    assert spec.read_bytes() == sealed
    pending = led.db.execute("SELECT part_id FROM notification_render_parts WHERE delivery_id=? "
                             "AND state='pending' ORDER BY idx", (render["delivery_id"],)).fetchall()
    assert pending
    for row in pending:
        assert _part_receipt(led, render, row[0])["applied"]
    assert notify_cards.gc(led, CFG, now=NOW + 2)["spec_files"] == 1
    assert not spec.exists()
