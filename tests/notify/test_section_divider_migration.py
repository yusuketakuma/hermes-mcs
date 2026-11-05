"""Prior LINE WORKS parts keep their identity across the divider upgrade."""
import json

import notify_cards
import notify_parts
import notify_render
from adapters.lineworks.cards import validate
from notify_lineworks_testkit import LINEWORKS, MID, _deliver
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread, led

import pytest

pytestmark = pytest.mark.usefixtures("legacy_chat_format")


__all__ = ["led"]


def test_old_delivered_original_is_not_reposted_when_divider_is_added(led, monkeypatch):
    _seed_thread(led, mids=(100,))
    real = notify_parts.build_manifest
    def old_builder(db, card, spec, *args, **kwargs):
        spec["parts"].pop("section_divider_version", None)
        return real(db, card, spec, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(notify_parts, "build_manifest", old_builder)
        assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)["dispatched"]
    old = _deliver(led, _latest_render(led))
    old_body = next(p for p in old["parts"]["manifest"] if p.get("name") == "m:100#1")
    with led.db:
        updates = []
        notify_cards.issue_render(led.db, 1, LINEWORKS, NOW + 1, updates, force=True)
    new = updates[0]
    assert new["parts"]["section_divider_version"] == 1
    assert new["parts"]["manifest"][0]["prior_remote_id"] == MID
    body = next(p for p in new["parts"]["manifest"] if p.get("name") == "m:100#1")
    assert body["sha256"] == old_body["sha256"]
    assert body["prior_remote_id"] == "lw:part:" + old_body["part_id"]
    header = next(p for p in new["parts"]["manifest"] if p.get("name") == "m:100:header#1")
    chunk = new["parts"]["thread_body_parts"][int(header["part_id"].split(":")[1]) - 1]
    assert chunk.endswith("\n" + notify_render.SECTION_RULE)
    assert "prior_remote_id" not in header
    assert validate(new) is new
    assert "section_divider_version" not in json.loads(
        led.db.execute("SELECT spec_json FROM notification_renders WHERE delivery_id=?",
                       (old["delivery_id"],)).fetchone()[0])["parts"]
