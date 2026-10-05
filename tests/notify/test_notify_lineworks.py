"""Synthetic LINE WORKS render, grant, receipt, action and restore isolation."""
import copy
import hashlib
import json
import uuid

import pytest

import mcs_requests
import notify_cards as cards
import notify_cmds as cmds
import notify_reconcile as reconcile
import notify_transport as transport
from hermes_plugin.mcs_delivery import envelopes, registry
from hermes_plugin.mcs_delivery.spec import validate as validate_v1
from notify_render import display_text, lineworks_card_split
from notify_testkit import (
    CFG, NOW, _card, _dispatch, _intent, _latest_render, _seed_thread,
    _token_for, led as led,
)

SCOPE = {"transport": "lineworks", "profile": "mcs",
         "application_id": "2000001", "team_id": "40029600",
         "channel_id": "12345678-1234-4321-abcd-123456789abc"}
LINEWORKS = {"notify": {"interactive": "lineworks", "route_epoch": 1,
                        "card_thread": False,
                        "lineworks": {k: v for k, v in SCOPE.items()
                                      if k != "transport"}},
             "signals": {"notify": True}}
ACTOR = "lineworks:40029600:c72af563-0f21-4736-11e4-045237113344"
MID = "lw:" + "c" * 32


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch):
    monkeypatch.setattr(cards.time, "time", lambda: NOW)


def _render(led):
    _seed_thread(led)
    assert _dispatch(led, _intent(led), LINEWORKS)["dispatched"]
    return _latest_render(led)


def _claim(render, attempt="a" * 16):
    return {"spec": json.loads(render["spec_json"]),
            "attempt_id": attempt, "worker_id": "b" * 16,
            "payload_hash": render["payload_hash"]}


def _apply(led, req, cfg=LINEWORKS):
    assert cmds.validate_int(req) is None
    return cmds.dispatch(led, req, cfg, cards.data_root(led), now=NOW)


def _deliver(led, render):
    claim = _claim(render)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    for part in claim["spec"]["parts"]["manifest"][1:]:
        assert _apply(led, envelopes.part_receipt(
            claim, part, "delivered", remote_id="lw:part:" + part["part_id"]))["applied"]
    return claim["spec"]


def test_lineworks_render_full_body_and_durable_scope_without_thread_api(led, tmp_path):
    render = _render(led)
    spec = json.loads(render["spec_json"])
    assert spec["schema"] == cards.LINEWORKS_RENDER_SCHEMA
    assert spec["delivery"].items() >= SCOPE.items()
    assert "guild_id" not in spec["delivery"] and render["guild_id"] is None
    assert spec["card_key"].startswith("v3|lineworks|")
    assert render["team_id"] == SCOPE["team_id"]
    validate_v1({**spec, "schema": cards.RENDER_SCHEMA})
    # LINE WORKS always carries the complete content as durable channel
    # parts: card_thread=False cannot silently remove the body/files.
    assert "本文" in "".join(spec["parts"]["thread_body_parts"])
    assert all(len(chunk) <= 1900 for chunk in spec["parts"]["thread_body_parts"])
    assert all(b["id"] != "body" for row in spec["parts"]["action_rows"] for b in row)
    path = tmp_path / "data" / "lineworks_render" / (render["delivery_id"] + ".json")
    assert path.exists() and not list((tmp_path / "data" / "slack_render").iterdir())
    raw = path.read_bytes()
    path.unlink()
    assert cards.recover(led, LINEWORKS, {})["republished"] == 1
    assert path.read_bytes() == raw
    flags = json.loads((tmp_path / "data" / "flags" / "notify.json").read_text())
    assert flags["transport"] == "lineworks" and flags["interactive"]


def test_lineworks_visible_text_overflow_is_lossless_durable_content(led, monkeypatch):
    original = cards._card_content

    def long_display(*args, **kwargs):
        content = original(*args, **kwargs)
        content["containers"] = [{"type": "text", "text": "合成内容" * 400}]
        return content

    monkeypatch.setattr(cards, "_card_content", long_display)
    spec = json.loads(_render(led)["spec_json"])
    text = display_text(spec["parts"])
    display = [(p, c) for p, c in zip(
        [p for p in spec["parts"]["manifest"] if p["kind"] == "body_part"],
        spec["parts"]["thread_body_parts"], strict=True)
        if p["name"].startswith("display#")]
    head, rest = lineworks_card_split(text)
    # the card breaks at a line end with ↓ 続き; the rest rides as parts
    assert len(text) > 1000 and display and len(head) <= 1000
    assert head.endswith("\n↓ 続き") and "".join(c for _, c in display) == rest
    assert text.startswith(head.removesuffix("\n↓ 続き"))
    validate_v1({**spec, "schema": cards.RENDER_SCHEMA})


def test_lineworks_grant_receipt_action_updates_and_retires_old_buttons(led):
    render = _render(led)
    spec = _deliver(led, render)
    old_token = _token_for(spec, "ack")
    old_request = envelopes.notification(old_token, ACTOR, {**SCOPE, "message_id": MID})
    assert _apply(led, old_request)["outcome"] == "applied"
    update = _latest_render(led)
    assert update["op"] == "update" and update["render_rev"] > render["render_rev"]
    new_spec = json.loads(update["spec_json"])
    assert new_spec["delivery"]["message_id"] == MID
    assert _token_for(new_spec, "ack") != old_token
    assert _card(led)["message_id"] == MID
    # A replay on an undeletable older LINE WORKS post must reauthorize,
    # rather than returning a cached success from its earlier click.
    assert _apply(led, old_request)["error"] == "token_expired"
    claim = _claim(update, attempt="d" * 16)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "delivered", message_id=MID))["applied"]
    assert _card(led)["delivery_state"] == "delivered"
    assert _card(led)["applied_render_rev"] == update["render_rev"]


@pytest.mark.parametrize("field", ["profile", "application_id", "team_id", "channel_id"])
def test_lineworks_scope_is_exact_at_grant_receipt_and_action(led, field):
    render = _render(led)
    claim = _claim(render)
    begin = envelopes.transport_begin(claim)
    foreign = {**begin, field: "FOREIGN"}
    assert _apply(led, foreign)["error"] == "denied_scope_mismatch"
    assert _apply(led, begin)["error"] == "denied_scope_mismatch"  # denied attempt replay
    claim = _claim(render, attempt="d" * 16)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    receipt = envelopes.transport_receipt(claim, "delivered", message_id=MID)
    assert not _apply(led, {**receipt, field: "FOREIGN"})["applied"]
    assert _card(led)["message_id"] is None
    assert _apply(led, receipt)["applied"]
    spec = claim["spec"]
    req = envelopes.notification(_token_for(spec, "ack"), ACTOR,
                                 {**SCOPE, "message_id": MID, field: "FOREIGN"})
    if field == "team_id":
        assert cmds.validate_int(req) == "bad_actor"
    else:
        assert _apply(led, req)["error"] == "scope_mismatch"
    assert not led.db.execute("SELECT 1 FROM notification_acknowledgements").fetchone()


@pytest.mark.parametrize("mutation", [
    {"version": 1}, {"version": 2}, {"version": 4}, {"version": True},
    {"transport": "slack"}, {"guild_id": "40029600"}, {"team_id": None},
])
def test_lineworks_version_and_scope_never_alias_other_transports(led, mutation):
    claim = _claim(_render(led))
    for req in (envelopes.transport_begin(claim),
                envelopes.transport_receipt(claim, "delivered", message_id=MID)):
        assert cmds.validate_int({**req, **mutation}) is not None


def test_lineworks_scope_switch_does_not_retarget_old_card_or_grant(led):
    render = _render(led)
    other = copy.deepcopy(LINEWORKS)
    other["notify"]["lineworks"]["team_id"] = "99999999"
    ev = led.db.execute("SELECT * FROM notify_outbox").fetchone()
    assert _dispatch(led, ev, other)["error"] == "scope_mismatch"
    cards.sweep(led, other, now=NOW)
    assert _latest_render(led)["spec_json"] == render["spec_json"]
    assert _apply(led, envelopes.transport_begin(_claim(render)), other)["error"] == "denied_scope_mismatch"
    assert _dispatch(led, _intent(led, payload={"message_ids": [101]}), other)["dispatched"]
    assert _card(led, 2)["team_id"] == "99999999"
    assert _card(led, 2)["card_key"] != _card(led)["card_key"]
    assert _dispatch(led, ev, CFG)["error"] == "transport_mismatch"


def test_lineworks_registry_namespace_and_restore_journals_are_isolated(led, tmp_path):
    _render(led)
    dirs = cards.notify_dirs(cards.data_root(led))
    assert registry.scope_key(SCOPE) != registry.scope_key({**SCOPE, "transport": "slack"})
    assert registry.scope_key(SCOPE) != registry.scope_key({**SCOPE, "team_id": "99999999"})
    journal = tmp_path / "data" / "lineworks_state" / "journal-synthetic.jsonl"
    journal.write_text(json.dumps({"attempt_id": "a" * 16, "phase": "started"}) + "\n")
    rows, tainted = reconcile.scan_journals(dirs)
    assert "a" * 16 in rows and not tainted
    journal.write_text(journal.read_text() + "invalid-json\n")
    _, tainted = reconcile.scan_journals(dirs)
    assert tainted


@pytest.mark.parametrize("transport_name,version", [("slack", 2), ("lineworks", 3)])
def test_operator_resolve_delegates_exact_version_to_scope_validator(transport_name, version):
    req = {"version": version, "cmd": "ops.card_resolve", "transport": transport_name,
           "command_id": str(uuid.uuid4()), "actor": "operator", "human_confirmed": True,
           "reason": "合成検証の未送信確認", "delivery_id": str(uuid.uuid4()),
           "attempt_id": "a" * 16, "result": "mark_not_sent",
           **{k: v for k, v in SCOPE.items() if k != "transport"},
           "evidence": {"method": "synthetic", "ref": "synthetic-receipt",
                        "worker_stopped": True, "proof": "api_rejected"}}
    assert transport.validate_card_resolve(req) is None
    assert mcs_requests.validate(req) is None
    assert mcs_requests.validate({**req, "human_confirmed": False}) == "human_confirmation_required"
    assert mcs_requests.validate({**req, "guild_id": "40029600"}) == "unknown_field"



def test_lineworks_downloaded_and_unavailable_attachments_remain_in_sealed_plan(led, tmp_path):
    _seed_thread(led)
    blob = tmp_path / "synthetic.txt"
    raw = b"fully synthetic attachment"
    blob.write_bytes(raw)
    for file_id, state in (("synthetic-ready", "downloaded"), ("synthetic-failed", "failed")):
        led.db.execute(
            "INSERT INTO attachments(message_id,file_id,name,local_path,bytes,"
            "sha256,state,downloaded_at,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (100, file_id, "合成添付.txt", str(blob), len(raw),
             hashlib.sha256(raw).hexdigest(), state, NOW, NOW))
    led.db.commit()
    assert _dispatch(led, _intent(led), LINEWORKS)["dispatched"]
    spec = json.loads(_latest_render(led)["spec_json"])
    attachments = [p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part"]
    assert len(attachments) == 2
    assert attachments[0]["path"] == str(blob)
    assert attachments[0]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert attachments[1]["unavailable"] is True
    validate_v1({**spec, "schema": cards.RENDER_SCHEMA})


def test_lineworks_unknown_delivery_never_reissues_until_factual_resolution(led):
    render = _render(led)
    claim = _claim(render)
    assert _apply(led, envelopes.transport_begin(claim))["granted"]
    assert _apply(led, envelopes.transport_receipt(
        claim, "unknown", error_code="synthetic_connection_lost"))["applied"]
    cards.sweep(led, LINEWORKS, now=NOW + 3600)
    assert _latest_render(led)["delivery_id"] == render["delivery_id"]
    assert _card(led)["delivery_state"] == "delivery_unknown"
    assert _apply(led, envelopes.transport_begin(_claim(
        render, attempt="e" * 16)))["granted"] is False


def test_lineworks_revoke_invalidates_tokens_without_remote_delete(led):
    render = _render(led)
    spec = _deliver(led, render)
    led.db.execute("UPDATE notification_cards SET delivery_state='revoked' WHERE card_id=1")
    specs = []
    cards._issue_render(led.db, 1, LINEWORKS, NOW, specs, force=True)
    led.db.commit()
    assert specs[0]["op"] == "revoke"
    assert specs[0]["delivery"]["message_id"] == MID
    assert [p["kind"] for p in specs[0]["parts"]["manifest"]] == ["card"]
    req = envelopes.notification(_token_for(spec, "ack"), ACTOR,
                                 {**SCOPE, "message_id": MID})
    assert _apply(led, req)["outcome"] == "rejected"
    assert _card(led)["delivery_state"] == "revoked"
