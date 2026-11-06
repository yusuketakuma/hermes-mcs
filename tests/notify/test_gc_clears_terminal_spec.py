"""Keep recoverable Slack caption plans; unrelated terminal specs still lose their text."""
import json
from pathlib import Path

import pytest

from adapters.common import envelopes
import notify_cards
import notify_parts
import notify_spool
import notify_transport
from notify_testkit import (CFG, NOW, _attach, _dispatch, _intent, _latest_render,
                            _seed_thread, _surface_file_echo, _wire_parts, led as led)
from slack_testkit import SLACK


def settle(led, cfg, render, *, error=None, kind="attachment_part"):
    spec = json.loads(render["spec_json"])
    claim = {"spec": spec, "attempt_id": f"{render['render_rev']:016x}",
             "worker_id": "b" * 16, "payload_hash": render["payload_hash"]}
    assert notify_transport.apply_transport_begin(
        led, envelopes.transport_begin(claim), cfg, now=NOW)["granted"]
    assert notify_transport.apply_transport_receipt(
        led, envelopes.transport_receipt(claim, "delivered", message_id="1790000000.000001",
                                         files=_surface_file_echo(spec, "card")),
        cfg, now=NOW)["applied"]
    target = next(part for part in spec["parts"]["manifest"] if part["kind"] == kind)
    group = next((p for p in spec["parts"]["manifest"]
                  if p["kind"] == "file_group" and target["part_id"] in p.get("members", ())), None)
    if group is not None and spec["delivery"].get("transport", "discord") == "discord":
        target = next(p for p in spec["parts"]["manifest"] if p["part_id"] == group["surface"])
        assert target["kind"] == "body_part"
    for part in _wire_parts(spec)[1:]:
        unknown = error is not None and part == target
        remote_id = "1790000000.000001" if part["kind"] == "thread" else "synthetic-" + part["part_id"]
        receipt = envelopes.part_receipt(claim, part, "unknown" if unknown else "delivered",
                                         remote_id=None if unknown else remote_id,
                                         error_code=error if unknown else None,
                                         files=None if unknown else _surface_file_echo(spec, part["part_id"]))
        assert notify_transport.apply_part_receipt(led, receipt, cfg, now=NOW)["applied"]
    return claim, target


def caption_update(led, monkeypatch, cfg, error, kind="attachment_part"):
    if cfg is CFG:
        import present_discord
        monkeypatch.setattr(present_discord, "FACE_ORIGINAL_MAX", 1)
    if kind == "body_part":
        # Keep a real thread-body part instead of moving its synthetic text to the face.
        import present_slack
        monkeypatch.setattr(present_slack, "BODY_ON_FACE", False)
    _seed_thread(led)
    _attach(led, 100, name="synthetic.pdf")
    monkeypatch.setattr(notify_parts, "_attachment_captions",
                        lambda db, card, attachments: {a["attachment_id"]: "synthetic before" for a in attachments})
    assert _dispatch(led, _intent(led), cfg)["dispatched"]
    settle(led, cfg, _latest_render(led))
    monkeypatch.setattr(notify_parts, "_attachment_captions",
                        lambda db, card, attachments: {a["attachment_id"]: "synthetic after" for a in attachments})
    specs = []
    with led.db:
        notify_cards.issue_render(led.db, 1, cfg, NOW + 1, specs, force=True)
    dirs = notify_spool.notify_dirs(notify_spool.data_root(led))
    notify_spool.publish_specs(led.db, dirs, specs, NOW + 1)
    render = _latest_render(led)
    assert render["op"] == "update"
    claim, target = settle(led, cfg, render, error=error, kind=kind)
    attachment = next(p for p in claim["spec"]["parts"]["manifest"] if p["kind"] == "attachment_part")
    assert attachment["edit_only"] is True
    path = Path(dirs[render["transport"] + "_render"]) / (render["delivery_id"] + ".json")
    assert path.is_file()
    return render, claim, target, path


@pytest.mark.parametrize("error", ["edit_target_unverified", "edit_history_unverified"])
@pytest.mark.parametrize("resolved", ["delivered", "not_sent"])
def test_gc_retains_slack_caption_specs_until_unknown_is_resolved(led, monkeypatch, error, resolved):
    render, claim, target, path = caption_update(led, monkeypatch, SLACK, error)
    sealed = path.read_bytes()
    notify_cards.gc(led, SLACK, now=NOW + 2)
    current = _latest_render(led)
    assert current["spec_json"] == render["spec_json"]
    assert current["payload_hash"] == render["payload_hash"]
    assert current["spec_published"] == 1 and path.read_bytes() == sealed
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (render["delivery_id"], target["part_id"])).fetchone()[0] == "unknown"
    # A later verified synthetic outcome settles the edit; GC does not resolve it.
    receipt = envelopes.part_receipt(claim, target, resolved,
                                     remote_id=target["prior_remote_id"] if resolved == "delivered" else None,
                                     error_code="synthetic_rejection" if resolved == "not_sent" else None)
    assert notify_transport.apply_part_receipt(led, receipt, SLACK, now=NOW + 3)["applied"]
    result = notify_cards.gc(led, SLACK, now=NOW + 4)
    assert result["spec_json_cleared"] == 1 and result["spec_files"] == 1
    current = _latest_render(led)
    if resolved == "delivered":
        assert "attachment_identity" in json.loads(current["spec_json"])
        assert "synthetic.pdf" not in current["spec_json"]
    else:
        assert current["spec_json"] is None
    assert current["spec_published"] == 0
    assert current["payload_hash"] == render["payload_hash"]
    assert not path.exists()


@pytest.mark.parametrize("cfg,error,kind", [
    (SLACK, "timeouterror", "attachment_part"),
    (SLACK, "bad_slack_response", "attachment_part"),
    (SLACK, "edit_target_unverified", "body_part"),
    (SLACK, "edit_history_unverified", "body_part"),
    (CFG, "edit_target_unverified", "attachment_part"),
    (CFG, "edit_history_unverified", "attachment_part"),
])
def test_gc_still_clears_other_terminal_unknown_specs(led, monkeypatch, cfg, error, kind):
    render, _, target, path = caption_update(led, monkeypatch, cfg, error, kind)
    notify_cards.gc(led, cfg, now=NOW + 2)
    current = _latest_render(led)
    if cfg is SLACK and kind == "body_part":
        assert "attachment_identity" in json.loads(current["spec_json"])
        assert "synthetic.pdf" not in current["spec_json"]
    else:
        assert current["spec_json"] is None
    assert current["spec_published"] == 0
    assert current["payload_hash"] == render["payload_hash"]
    assert not path.exists()
    assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? AND part_id=?",
                          (render["delivery_id"], target["part_id"])).fetchone()[0] == "unknown"
