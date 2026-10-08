"""Card attachment delivery, live views, health alerts and transport gate — synthetic temp ledger only."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

import notify_cards
import notify_cmds
import notify_flush
import notify_render
import notify_transport
from mcs_requests import payload_hash
from semantic_projection import PROJECTION_VERSION
from notify_testkit import (
    CFG, CLICKER, NOW, ORIGIN, SCOPE, _begin, _card, _click, _delivered_card,
    _dispatch, _intent, _latest_render, _llm_extract, _receipt,
    _seed_thread, _spec, _token_for, _uuid, led, pinned_clock,
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _attachment(led, tmp_path, mid=100, name="syn.bin"):
    blob = b"synthetic-file"
    f = tmp_path / f"blob-{mid}.bin"
    f.write_bytes(blob)
    aid = led.db.execute(
        "INSERT INTO attachments(message_id,file_id,name,local_path,"
        "bytes,sha256,state) VALUES(?,?,?,?,?,?,'downloaded')",
        (mid, f"file-{mid}", name, str(f), len(blob),
         hashlib.sha256(blob).hexdigest())).lastrowid
    led.db.commit()
    return aid


def _followups(led):
    return [json.loads(r["payload"])["attachment_id"] for r in led.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='attachment_followup' "
        "AND state!='suppressed'")]


# 1 ---------------------------------------------------------------------

@pytest.mark.parametrize("transport", ["slack", "discord"])
def test_earlier_native_content_fp_drifts_layout_once_and_keeps_ack_source(led, transport):
    spec = _delivered_card(led)
    assert _click(led, spec, "ack")["outcome"] == "applied"
    card = _card(led)
    card["transport"] = transport
    content = notify_cards._card_content(led.db, card)
    old_fp = payload_hash({"c": content["containers"], "f": content["footer"],
                           "s": content["shown"], "p": content["page"],
                           "a": content.get("actor_fp")})
    card.update(content_fp=old_fp, source_fp=content["source_fp"])
    source_generation = card["source_generation"]
    acknowledged = notify_render.current_ackers(led.db, card["card_id"], source_generation, content["shown"])
    assert acknowledged == [CLICKER]
    drift = notify_cards._generation_drift(card, content)
    assert drift["presentation_generation"] == card["presentation_generation"] + 1
    assert "source_generation" not in drift and "source_fp" not in drift
    card.update(drift)
    assert card["source_generation"] == source_generation
    assert notify_render.current_ackers(led.db, card["card_id"], source_generation, content["shown"]) == acknowledged
    assert notify_cards._generation_drift(card, content) == {}
    changed = {**content, "preview_text": "別のプレビュー"}
    assert notify_render._content_fp(changed) == card["content_fp"]


@pytest.mark.parametrize("transport", ["lineworks", None])
def test_earlier_non_native_content_fp_stays_compatible(led, transport):
    _delivered_card(led)
    card = _card(led)
    card["layout"] = 1                    # a card created before layout 2
    if transport is None:
        card.pop("transport")
    else:
        card["transport"] = transport
    content = notify_cards._card_content(led.db, card)
    assert "thread_layout" not in content and "layout" not in content
    old_fp = payload_hash({"c": content["containers"], "f": content["footer"],
                           "s": content["shown"], "p": content["page"],
                           "a": content.get("actor_fp")})
    card.update(content_fp=old_fp, source_fp=content["source_fp"])
    assert notify_cards._generation_drift(card, content) == {}
    changed = {**content, "preview_text": "別のプレビュー"}
    assert notify_render._content_fp(changed) == old_fp


def test_layout_one_card_keeps_legacy_face_and_new_cards_use_layout_two(led):
    _delivered_card(led)
    card = _card(led)
    assert card["layout"] == notify_cards.CARD_LAYOUT == 2
    new = notify_cards._card_content(led.db, card)
    legacy = notify_cards._card_content(led.db, {**card, "layout": 1})
    assert " · 起点 " in legacy["containers"][0]["text"]
    assert " · 起点 " not in new["containers"][0]["text"]
    assert notify_render._content_fp(legacy) != notify_render._content_fp(new)


# 2 ---------------------------------------------------------------------

def test_pending_text_followup_owns_the_file_not_the_part(led, tmp_path):
    _seed_thread(led)
    aid = _attachment(led, tmp_path)
    assert [a["attachment_id"] for a in notify_cards._plan_attachments(led.db, [100])] == [aid]
    led.outbox_add("attachment_followup", 1,
                   {"attachment_id": aid, "message_id": 100})
    assert notify_cards._plan_attachments(led.db, [100]) == []
    led.db.execute("UPDATE notify_outbox SET state='suppressed' "
                   "WHERE kind='attachment_followup'")
    assert [a["attachment_id"] for a in notify_cards._plan_attachments(led.db, [100])] == [aid]


@pytest.mark.parametrize("state", ["pending", "delivered", "unknown"])
def test_followup_aborts_while_a_card_part_owns_the_file(led, tmp_path, state):
    _seed_thread(led)
    aid = _attachment(led, tmp_path)
    _dispatch(led, _intent(led))
    led.db.execute("UPDATE notification_render_parts SET state=? "
                   "WHERE kind='attachment_part'", (state,))
    with pytest.raises(notify_flush._StaleSend):
        notify_flush._followup_files(
            led, {"attachment_id": aid, "message_id": 100}, 1)


# 3 ---------------------------------------------------------------------

def _part_receipt(led, render, part_id, **fields):
    req = {"version": 1, "op": "part_receipt", "command_id": _uuid(77),
           "attempt_id": f"p:{render['delivery_id'].replace('-', '')}:{part_id}",
           "delivery_id": render["delivery_id"],
           "render_rev": render["render_rev"],
           "payload_hash": render["payload_hash"], "route_epoch": 1,
           "correlation": render["correlation"], **SCOPE,
           "part_id": part_id, **fields}
    return notify_transport.apply_part_receipt(led, req, CFG, now=NOW)


def test_failed_thread_hands_held_files_to_the_text_followup(led, tmp_path):
    _seed_thread(led)
    aid = _attachment(led, tmp_path)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], message_id="m-9")
    out = _part_receipt(led, render, "thread", result="not_sent",
                        error_code="http_403")
    assert out["applied"] is True
    assert led.db.execute(
        "SELECT state FROM notification_render_parts WHERE attachment_id=?",
        (aid,)).fetchone()["state"] == "held"
    assert _followups(led) == [aid]
    # the held part never blocks the followup it was handed to
    text, files = notify_flush._followup_files(
        led, {"attachment_id": aid, "message_id": 100}, 1)
    assert files and "添付ファイル（後送）" in text


def test_unknown_thread_keeps_files_for_the_next_render(led, tmp_path):
    _seed_thread(led)
    _attachment(led, tmp_path)
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    grant = _begin(led, render)
    _receipt(led, render, grant["attempt_id"], message_id="m-9")
    _part_receipt(led, render, "thread", result="unknown")
    assert _followups(led) == []


# 4 ---------------------------------------------------------------------

def test_tasks_view_is_live_and_not_persisted(led):
    import notify_testkit as kit
    spec = _delivered_card(led)
    kit._add_request(led, title="合成タスク1")
    notify_cards.sweep(led, CFG, now=NOW + 1)
    kit._deliver(led)
    spec = _spec(led)
    tok = _token_for(spec, "tasks")
    req = {"version": 1, "op": "notification",
           "command_id": f"{tok}:{'ab' * 8}", "actor": CLICKER,
           "token": tok, "origin": dict(ORIGIN, message_id="m-9")}
    first = notify_cards.apply_notification(led, dict(req), CFG, now=NOW + 2)
    assert [t["title"] for t in first["tasks"]] == ["合成タスク1"]
    kit._add_request(led, title="合成タスク2")
    again = notify_cards.apply_notification(led, dict(req), CFG, now=NOW + 3)
    assert sorted(t["title"] for t in again["tasks"]) == ["合成タスク1", "合成タスク2"]
    stored = led.db.execute("SELECT receipt_json FROM command_receipts "
                            "WHERE command_id=?", (req["command_id"],)).fetchone()
    assert "合成タスク" not in stored["receipt_json"]
    assert "token_ctx" not in json.loads(stored["receipt_json"])


# 6 ---------------------------------------------------------------------

def test_extract_ref_skips_message_shown_from_canonical_projection(led):
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": []})
    assert notify_cards._extract_ref(led.db, 1, root=100)["message_id"] == 101
    h = led.db.execute("SELECT content_hash FROM messages WHERE message_id=101"
                       ).fetchone()[0]
    led.db.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,"
        "created_at) VALUES('canonical_projection',1,101,'{}','test',?,?)",
        (json.dumps({"hash": h, "projection_version": PROJECTION_VERSION}), NOW))
    assert notify_cards._extract_ref(led.db, 1, root=100) is None


# 7 ---------------------------------------------------------------------

def test_long_attachment_name_keeps_its_extension(led, tmp_path):
    _seed_thread(led)
    _attachment(led, tmp_path, name="写" * 300 + ".png")
    (entry,) = notify_cards._plan_attachments(led.db, [100])
    assert len(entry["name"]) == 200 and entry["name"].endswith("….png")


# 8 ---------------------------------------------------------------------

def test_health_write_failed_alert_is_sent(led, monkeypatch):
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda cfg: sys.executable)
    monkeypatch.setattr(notify_flush, "_target", lambda *a: "synthetic")
    monkeypatch.setattr(notify_flush, "_send_argv", lambda *a: ["hermes"])
    monkeypatch.setattr(notify_flush, "_config", lambda: {})
    sent = []
    monkeypatch.setattr(notify_flush, "_send",
                        lambda argv, content, *a, **k: sent.append(content))
    led.outbox_add("health_write_failed", None,
                   {"detail": "OSError", "run_id": 7})
    notify_flush.flush(led)
    assert any("health.json" in s and "run 7: OSError" in s for s in sent)
    assert led.db.execute("SELECT state FROM notify_outbox WHERE "
                          "kind='health_write_failed'").fetchone()[0] == "accepted"


# 9 ---------------------------------------------------------------------

@pytest.mark.parametrize("actor", ["slack:T1:U1", "lineworks:T1:U1", "unknown"])
def test_human_command_from_another_transport_is_refused(led, tmp_path, actor):
    _delivered_card(led)
    req = {"version": 1, "cmd": "request.create",
           "command_id": "11111111-2222-4333-8444-555555555558",
           "actor": actor, "human_confirmed": True, "project_id": 1,
           "source_message_id": 101, "source_hash": f"{101:064x}",
           "title": "合成タスク", "reason": "合成理由"}
    out = notify_cmds.dispatch(led, req, CFG, str(tmp_path / "data"))
    assert out["error"] == "interactive_off"
    assert led.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0


# 10 --------------------------------------------------------------------

def test_view_results_expire_after_an_hour(led):
    _delivered_card(led)
    root = notify_cards.data_root(led)
    dirs = notify_cards.notify_dirs(root)
    spec = _spec(led)
    tok = _token_for(spec, "summary")
    env = {"version": 1, "op": "notification", "command_id": f"{tok}:{'cd' * 8}",
           "actor": CLICKER, "token": tok, "origin": dict(ORIGIN, message_id="m-9"),
           "request_id": _uuid(91)}
    notify_cards.publish_file(dirs["cmd_int"], "a.json", json.dumps(env).encode())
    notify_cmds.drain_int_commands(led, {}, CFG, root)
    path = os.path.join(dirs["cmd_results"], _uuid(91) + ".json")
    assert "body" in json.loads(open(path, encoding="utf-8").read())
    import time
    age = time.time() - os.stat(path).st_mtime
    assert notify_cards.TOKEN_WRITE_S - notify_cards.LIVE_RESULT_S - 60 < age
    notify_cards.gc(led, CFG, now=time.time() + notify_cards.LIVE_RESULT_S + 1)
    assert not os.path.exists(path)


# 11 --------------------------------------------------------------------

def test_discord_digest_text_is_literal():
    from adapters.discord.cards import escape_md
    staff = "[link](https://x.example) ||spoiler|| **b** <@123>\n# 見出し\n> 引用\n1. 項目"
    parts = {"containers": [{"type": "heading", "text": "# 題"},
                            {"type": "text", "text": staff}],
             "footer": [{"type": "text", "text": "- 注記 _x_"}]}
    out = notify_render.parts_text(parts, "discord")
    assert "## ＃ 題" in out and escape_md(staff).replace("<@123>", "メンバー") in out
    assert "-# － 注記 ＿x＿" in out
    assert notify_render._discord_literal(staff) == escape_md(staff)
    assert "[link]" in notify_render.parts_text(parts, "plain")
