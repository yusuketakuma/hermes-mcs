"""Synthetic resource holds stay private across recovery and direct publication."""
import notify_parts
import json
from pathlib import Path

import pytest

import notify_spool
import notify_recovery
from notify_testkit import (CFG, NOW, _begin, _dispatch, _intent, _latest_render,

                            _seed_thread, led as led)

pytestmark = pytest.mark.usefixtures("v1_thread_bodies")


@pytest.mark.parametrize("error_code", ["part_budget_exceeded", "thread_cleanup_budget_exceeded",
                                        "attachment_upload_identity_unverified"])
def test_resource_hold_is_never_published_or_granted(led, monkeypatch, error_code):
    _seed_thread(led, mids=(100,))
    monkeypatch.setattr(notify_parts, "MAX_PARTS", 3)
    monkeypatch.setattr(notify_parts, "card_body_text", lambda *a, **k: ("合成", "x" * 4000))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    held = _latest_render(led)
    assert held["state"] == "held" and held["parts_state"] == "held"
    rows = led.db.execute("SELECT state,error_code FROM notification_render_parts WHERE delivery_id=?",
                          (held["delivery_id"],)).fetchall()
    assert rows and all(row["state"] == "held" and row["error_code"] == "part_budget_exceeded" for row in rows)
    if error_code != "part_budget_exceeded":
        with led.db:
            led.db.execute("UPDATE notification_render_parts SET error_code=? WHERE delivery_id=?",
                           (error_code, held["delivery_id"]))
    assert notify_recovery.part_hold_report(led)["parts"]["held"][error_code] > 0
    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
    path = Path(dirs["discord_render"]) / (held["delivery_id"] + ".json")
    assert not path.exists()
    assert notify_spool.recover(led, CFG, {})["republished"] == 0
    assert notify_spool.publish_specs(led.db, dirs, [json.loads(held["spec_json"])], NOW + 1) == []
    assert not path.exists()
    assert _latest_render(led)["spec_published"] == 0
    assert not _begin(led, held, n=1600)["granted"]
    assert _latest_render(led)["state"] == "held"


def test_unknown_beyond_horizon_part_is_never_republished(led):
    """A part a restore beyond the journal horizon marked unknown keeps its
    delivery off the spool even while a sibling part stays pending: the
    compacted journal cannot prove that part never ran."""
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}))
    render = _latest_render(led)
    parts = led.db.execute(
        "SELECT part_id FROM notification_render_parts WHERE delivery_id=? "
        "ORDER BY idx", (render["delivery_id"],)).fetchall()
    assert len(parts) >= 2
    with led.db:
        led.db.execute("UPDATE notification_renders SET state='delivered' "
                       "WHERE delivery_id=?", (render["delivery_id"],))
        led.db.execute(
            "UPDATE notification_render_parts SET state='unknown',error_code=?,"
            "attempt_id='aa' WHERE delivery_id=? AND part_id=?",
            (notify_recovery.BEYOND_HORIZON, render["delivery_id"], parts[0]["part_id"]))
        led.db.execute(
            "UPDATE notification_render_parts SET state='pending' "
            "WHERE delivery_id=? AND part_id=?",
            (render["delivery_id"], parts[1]["part_id"]))
    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
    path = Path(dirs["discord_render"]) / (render["delivery_id"] + ".json")
    path.unlink(missing_ok=True)
    assert notify_spool.recover(led, CFG, {})["republished"] == 0
    assert notify_spool.publish_specs(led.db, dirs,
                                      [json.loads(render["spec_json"])], NOW + 1) == []
    assert not path.exists()
