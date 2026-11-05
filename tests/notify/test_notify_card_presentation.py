"""Synthetic notification card content, action and pagination contracts."""
from __future__ import annotations

import notify_parts
import json
import pytest
import notify_cards
import notify_action_tokens
import notify_actions
import notify_render
import notify_transport
from notify_testkit import (
    _settle_bodies,
    CFG, NOW, ORIGIN, _begin, _card, _dispatch, _extract, _intent,
    _latest_render, _msg, _notif, _patient, _receipt, _seed_thread,
    _signal_row, _token_for, _uuid, led, pinned_clock,
)
from notify_testkit import delivered_source

from notify_card_test_support import (
    NO_THREAD_CFG, _deliverable, _delivered_card, _request, _tasks_click, _tasks_token
)

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def test_body_action_returns_full_text(led, tmp_path):
    """📄本文表示 is a view action: it answers the untruncated text of
    the shown set and mutates nothing — no render, no triage rows.
    The button ships only where no companion thread carries the body
    (card_thread off here), so exercise that surface."""
    long_body = "詳細な記録。" * 40           # ~200 chars > MAX_SNIPPET
    _patient(led, 1)
    _msg(led, 100, 1, body=long_body)
    _msg(led, 101, 1, parent=100, body="短い返信")
    ev = _intent(led)
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    tok = _token_for(spec, "body")
    n_renders = led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders").fetchone()["c"]
    req = _notif(tok)
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_actions.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    assert r["title"].endswith("本文")
    assert long_body in r["body"] and "短い返信" in r["body"]
    # view-only: no re-render is issued and nothing is mutated
    assert "delivery_id" not in r
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_renders"
    ).fetchone()["c"] == n_renders
    assert led.db.execute(
        "SELECT COUNT(*) c FROM notification_triage"
    ).fetchone()["c"] == 0


def test_card_renders_headers_not_bodies(led, tmp_path):
    """Thread cards show only per-message header lines (time + sender) —
    no body text at all; 📄本文表示 serves the untruncated set. Pages
    still pack by rendered length under the Components-V2 ceiling."""
    _patient(led, 1)
    body = "記録の本文です。" * 40
    _msg(led, 100, 1, body=body)
    for m in range(101, 114):                # 13 msgs > PAGE_THREAD
        _msg(led, m, 1, parent=100, body=body)
    ev = _intent(led, payload={"message_ids": list(range(100, 114))})
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    assert c["pages"] > 1                    # PAGE_THREAD count cap
    seen = set()
    for p in range(c["pages"]):
        card["ui_state"] = json.dumps({"page": p})
        c = notify_render.card_content(led.db, card)
        assert c["page"] == p
        total = (notify_render._blocks_len(c["containers"])
                 + notify_render._blocks_len(c["footer"]))
        assert total <= 4000, (p, total)     # hard Discord ceiling
        joined = "\n".join(b.get("text") or "" for b in c["containers"])
        assert "記録の本文です" not in joined  # no body text on the card
        assert "職員" in joined               # sender headers remain
        seen.update(c["shown"])
    assert seen == set(range(100, 114))      # nothing dropped


def test_card_body_oversized_headers_and_full_action(led, tmp_path):
    """A huge message renders as a bare header line on the card;
    📄本文表示 answers with the full body (bounded by BODY_MAX_CHARS
    with an explicit omission marker)."""
    _patient(led, 1)
    long_body = "長い記録。" * 1400            # ~7000 chars > BODY_MAX
    _msg(led, 100, 1, body=long_body)
    _msg(led, 101, 1, parent=100, body="短い")
    ev = _intent(led)
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    card = _card(led)
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    total = (notify_render._blocks_len(c["containers"])
             + notify_render._blocks_len(c["footer"]))
    assert total <= 4000
    assert set(c["shown"]) == {100, 101}
    assert "長い記録" not in joined and "短い" not in joined
    assert "職員" in joined                   # header lines only
    # the button still serves the untruncated set (BODY_MAX_CHARS cap)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    req = _notif(_token_for(spec, "body"))
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_actions.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    assert "（省略" in r["body"] and "長い記録。" in r["body"]
    assert len(r["body"]) <= notify_render.BODY_MAX_CHARS + 80


def test_card_page_indicator_shows_position(led, tmp_path):
    """F05: a multi-page card must display order and count, not just
    nav buttons — the reader must see which page they are on."""
    _patient(led, 1)
    body = "記録の本文です。" * 40
    _msg(led, 100, 1, body=body)
    for m in range(101, 114):
        _msg(led, m, 1, parent=100, body=body)
    ev = _intent(led, payload={"message_ids": list(range(100, 114))})
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    assert c["pages"] > 1
    # the context line under the heading: post count, then the page
    ctx = c["containers"][1]["text"]
    assert "14投稿" in ctx and f"1/{c['pages']}ページ" in ctx
    # last page shows its position
    card["ui_state"] = json.dumps({"page": c["pages"] - 1})
    c = notify_render.card_content(led.db, card)
    assert f"{c['pages']}/{c['pages']}ページ" in c["containers"][1]["text"]
    # single-page card carries no page line at all
    _patient(led, 2)
    _msg(led, 200, 2)
    ev2 = _intent(led, pid=2, payload={"message_ids": [200]})
    _dispatch(led, ev2)
    card2 = dict(led.db.execute(
        "SELECT * FROM notification_cards ORDER BY card_id DESC LIMIT 1"
        ).fetchone())
    c2 = notify_render.card_content(led.db, card2)
    assert c2["pages"] == 1
    assert "ページ" not in "\n".join(
        b.get("text") or "" for b in c2["containers"] + c2["footer"])


def test_fit_item_field_shrinks_as_last_resort(led):
    """Pathological input (huge field value, no shrinkable text) still
    fits the page budget — an over-budget item must never make the
    whole spec fail validation and render no card at all."""
    blocks = [{"type": "field", "name": "患者",
               "value": "名前" * 4000}]
    out = notify_render._fit_item(blocks)
    assert notify_render._blocks_len(out) <= notify_render.PAGE_TEXT_BUDGET
    assert "省略" in out[0]["value"]


def test_card_thread_shows_structured_lines(led, tmp_path):
    """A message renders its structured block under a header, not its body."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛", "悪寒"],
                        "rx_actions": [{"action": "start",
                                        "ctx": "オキシコドン"}]})
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    texts = [b.get("text") or "" for b in c["containers"]]
    # layout 2: the post line heads the summary, no 📋 label on the face
    struct = [t for t in texts if t.startswith("・")]
    assert struct and "症状" in struct[0] and "疼痛" in struct[0]
    # the header line remains alongside the structured block; the raw
    # body itself stays off the card (📄本文表示 serves it)
    assert any("職員" in t for t in texts) and not any(
        "本文" in t for t in texts)


def test_card_thread_structured_per_message(led, tmp_path):
    """Structured data binds to its own message — a second message's
    extraction must not bleed into the first message's block."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]})
    _extract(led, 101, {"v": 1, "symptoms": ["悪寒"]})
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    for p in range(9):
        card["ui_state"] = json.dumps({"page": p})
        c = notify_render.card_content(led.db, card)
        joined = "\n".join(b.get("text") or "" for b in c["containers"])
        if 100 in c["shown"]:
            assert "疼痛" in joined
        if 101 in c["shown"]:
            assert "悪寒" in joined
        if c["pages"] == p + 1:
            break


def test_card_stale_and_bad_extraction_not_shown(led, tmp_path):
    """An artifact bound to an older body revision (hash mismatch) or
    malformed content is never rendered as current structured data."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]}, stale=True)
    _extract(led, 101, None)                      # malformed JSON
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    # no current extraction: each post says so instead of a stale block
    assert joined.count("要約 処理待ち") == 2 and "疼痛" not in joined
    # the card still renders the message headers (bodies stay off-card)
    assert "職員" in joined and "本文" not in joined


def test_card_deleted_message_hides_structured_data(led, tmp_path):
    """A deleted message shows （削除済み） — its extraction must never
    leak through the 📋 block even if the artifact's hash still matches
    (the body_state gate lives in the artifact SQL itself)."""
    _seed_thread(led)
    _extract(led, 100, {"v": 1, "symptoms": ["疼痛"]})
    led.db.execute(
        "UPDATE messages SET body_state='deleted', body_text='' "
        "WHERE message_id=100")        # hash unchanged — worst case
    led.db.commit()
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "（削除された投稿）" in joined
    # only the live reply carries a summary line; the deleted post none
    assert joined.count("要約 処理待ち") == 1 and "疼痛" not in joined


def test_card_sender_tag_shows_time_profession_org(led, tmp_path):
    """Thread card header lines carry write date+time plus the sender's
    profession/organization; missing fields leave no dangling parens."""
    _patient(led, 1)
    _msg(led, 100, 1, prof="看護師", org="訪問看護ステーションX")
    _msg(led, 101, 1, parent=100)
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    card["ui_state"] = json.dumps({"page": 0})
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "09-24 08:" in joined
    assert "職員（看護師・訪問看護ステーションX）" in joined
    assert "職員（）" not in joined and "（・" not in joined


def test_body_manifest_shows_sender_metadata(led, tmp_path, monkeypatch):
    """The 📄本文表示 manifest header carries the same sender tag."""
    import processing_route

    monkeypatch.setattr(processing_route, "visible_providers", lambda: ("jev", "local"))
    _patient(led, 1)
    _msg(led, 100, 1, prof="薬剤師", org="薬局Y")
    _msg(led, 101, 1, parent=100)
    ev = _intent(led)
    _dispatch(led, ev)
    card = _card(led)
    title, text = notify_render.card_body_text(
        led.db, card, {"shown": "[100, 101]"})
    assert "09-24 08:" in text
    # layout 2: header -> summary -> stamps -> body
    rule = notify_render.SECTION_RULE
    states = "Jev 不明 · 💻 不明"
    assert (f"↳ 09-24 08:40 職員（薬剤師・薬局Y）\n{states}\n要約 処理待ち\nスタンプ 未取得\n{rule}\n"
            f"本文") in text
    assert f"08:41 職員\n{states}\n要約 処理待ち\nスタンプ 未取得\n{rule}\n本文" in text


def test_provider_bundle_cache_is_scoped_to_one_render(led, monkeypatch):
    import processing_route

    _patient(led, 1)
    _msg(led, 100, 1)
    _msg(led, 101, 1, parent=100)
    _dispatch(led, _intent(led))
    card = {**_card(led), "layout": 3}
    caches = []

    def states(_db, _pid, _mid, *, cfg=None, bundle_cache=None, sequence_out=None, followup=False):
        caches.append(bundle_cache)
        return dict.fromkeys(("anthropic", "openai", "jev", "local"), "unknown")

    monkeypatch.setattr(processing_route, "provider_states", states)
    notify_render.card_content(led.db, card, cfg=CFG)
    assert len(caches) == 2 and isinstance(caches[0], dict) and caches[0] is caches[1]
    card_cache = caches[0]
    caches.clear()
    notify_render.card_body_text(led.db, card, {"shown": "[100, 101]"}, cfg=CFG)
    assert len(caches) == 2 and caches[0] is caches[1] and caches[0] is not card_cache
    body_cache = caches[0]
    caches.clear()
    notify_render.card_content(led.db, card, cfg=CFG)
    assert len(caches) == 2 and caches[0] is caches[1] and caches[0] is not card_cache
    assert caches[0] is not body_cache


def test_signal_quote_shows_sender_metadata(led, tmp_path):
    """The signal's 最新言及 evidence quote carries the sender tag in
    the thread/body view — the card face itself is compact."""
    _patient(led, 1)
    _msg(led, 100, 1, body="退院後フォローの記録", prof="薬剤師",
         org="薬局Y")
    _signal_row(led, "sig-meta", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-meta"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    card = _card(led)
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "薬剤師・薬局Y" not in joined
    _, body = notify_render.card_body_text(
        led.db, card, {"shown": json.dumps(c["shown"])})
    assert "薬剤師・薬局Y" in body


def test_card_signal_structured_evidence(led, tmp_path):
    """The evidence message's structured block rides the companion
    thread (LLM summary labelled as such via the shared formatter);
    the card face shows only the key point."""
    _patient(led, 1)
    _msg(led, 100, 1, body="退院後フォローの記録")
    _signal_row(led, "sig-x", mids=[100])
    _extract(led, 100, {"summary": "状態安定", "points": ["経過観察"]},
             kind="extract_llm")
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-x"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    card = _card(led)
    c = notify_render.card_content(led.db, card)
    joined = "\n".join(b.get("text") or "" for b in c["containers"])
    assert "note sig-x" in joined
    assert "📋 要約" not in joined and "退院後フォローの記録" not in joined
    _, body = notify_render.card_body_text(
        led.db, card, {"shown": json.dumps(c["shown"])})
    assert "職員\n・状態安定\n・要点: 経過観察\n" in body
    assert "退院後フォローの記録" in body        # raw body still there


def test_digest_face_groups_signals_per_patient(led):
    """Digest card shows one item per patient — a patient with several
    signals still occupies a single face block, and ``shown`` keeps the
    underlying signal keys so thread bodies and actions still resolve
    per signal."""
    _patient(led, 1, "患者A")
    _patient(led, 2, "患者B")
    _signal_row(led, "sig-a1", pid=1)
    _signal_row(led, "sig-a2", pid=1, stype="adherence_concern")
    _signal_row(led, "sig-b1", pid=2)
    ev = _intent(led, kind="signal", pid=None,
                 payload={"digest": True, "type": "signal_digest",
                          "signal_keys": ["sig-a1", "sig-a2", "sig-b1"]})
    _dispatch(led, ev)
    card = _card(led)
    c = notify_render.card_content(led.db, card)
    texts = [b.get("text") or "" for b in c["containers"]
             if b["type"] == "text"]
    assert texts.count("患者A") == 1 and texts.count("患者B") == 1
    a_block = texts[texts.index("患者A") + 1]
    assert "note sig-a1" in a_block and "note sig-a2" in a_block
    assert c["shown"] == ["sig-a1", "sig-a2", "sig-b1"]


def test_body_action_signal_full_evidence(led, tmp_path):
    long_body = "退院後フォローの経過記録。" * 30
    _patient(led, 1)
    _msg(led, 100, 1, body=long_body)
    _signal_row(led, "sig-body", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-body"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    delivered_source(led, card_id=2, mids=(100,))
    _dispatch(led, ev, cfg=NO_THREAD_CFG)
    render = _latest_render(led)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    tok = _token_for(spec, "body")
    req = _notif(tok)
    req["origin"] = dict(ORIGIN, message_id="m-9")
    r = notify_actions.apply_notification(led, req, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "body"
    # signal notice + the FULL evidence body — the card shows only a
    # 120-char snippet of the quote
    assert "note sig-body" in r["body"]
    assert long_body in r["body"]
    assert "患者A" in r["body"]


def test_action_scope_and_stale_source_rejected(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    tok = _token_for(spec, "assign")
    bad = _notif(tok)
    bad["origin"] = dict(ORIGIN, channel_id="other-ch")
    bad["command_id"] = f"{tok}:{'cd' * 8}"
    r = notify_actions.apply_notification(led, bad, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "scope_mismatch"
    # mutate source -> token's need_source_gen goes stale
    _msg(led, 103, 1, parent=100)
    notify_cards.sweep(led, CFG)
    good = _notif(tok)
    good["origin"] = dict(ORIGIN, message_id="m-9")
    good["command_id"] = f"{tok}:{'ef' * 8}"
    r = notify_actions.apply_notification(led, good, CFG, now=NOW)
    assert r["outcome"] == "rejected" and r["error"] == "stale_source"
    # a fresh token still works
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (_latest_render(led)["delivery_id"] + ".json")).read_text())
    r = notify_actions.apply_notification(
        led, {**_notif(_token_for(spec2, "assign")),
              "command_id": f"{_token_for(spec2, 'assign')}:{'ef' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")},
        CFG, now=NOW)
    assert r["outcome"] == "applied"


def test_page_nav_issues_new_render_and_tokens(led, tmp_path):
    """prev/next must produce a new revision/token/render — the plan's
    view-op contract (a nav that only bumps ui_revision leaves the
    card frozen and every other button dead on stale_ui)."""
    card, spec = _delivered_card(led, tmp_path)
    # single-page card mints no nav buttons — grow it to two pages
    assert all(b["id"] not in ("prev", "next")
               for row in spec["parts"]["action_rows"] for b in row)
    for m in range(110, 120):
        _msg(led, m, 1, parent=100)
    notify_cards.sweep(led, CFG)
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (_latest_render(led)["delivery_id"] + ".json")).read_text())
    assert spec2["parts"]["pages"] > 1
    # a card opens on its newest page — only 'prev' is actionable there
    tok = _token_for(spec2, "prev")
    r = notify_actions.apply_notification(
        led, {**_notif(tok), "command_id": f"{tok}:{'ab' * 8}",
              "origin": dict(ORIGIN, message_id="m-9")}, CFG, now=NOW)
    assert r["outcome"] == "applied" and r["action"] == "page"
    # a successor UPDATE render carrying the new page + fresh tokens
    nxt = _latest_render(led)
    assert nxt["render_rev"] == spec2["render_rev"] + 1
    assert nxt["state"] == "queued" and nxt["op"] == "update"
    spec3 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (nxt["delivery_id"] + ".json")).read_text())
    assert spec3["parts"]["page"] == spec2["parts"]["page"] - 1
    assert spec3["ui_revision"] == spec2["ui_revision"] + 1
    # fresh tokens are minted — the consumed ones never recur
    nav3 = [b["token"] for row in spec3["parts"]["action_rows"]
            for b in row if b["id"] in ("prev", "next")]
    assert nav3 and tok not in nav3


def test_request_and_dismiss_tokens_authorize_modal(led, tmp_path):
    """request/dismiss tokens authorize opening the modal — applied,
    modal flag, stored params echoed — while the actual human command
    arrives separately."""
    _patient(led, 1)
    _msg(led, 100, 1)
    _signal_row(led, "sig-modal", mids=[100])
    ev = _intent(led, kind="signal", pid=1,
                 payload={"signal_keys": ["sig-modal"], "project_id": 1,
                          "type": "med_followup"})
    _dispatch(led, ev)
    delivered_source(led, card_id=2, mids=(100,))
    _dispatch(led, ev)
    render = _latest_render(led)
    assert _begin(led, render)["granted"]
    assert _receipt(led, render, f"{1:016x}", message_id="signal-reply")["applied"]
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    # the render pins modal context — the plugin confirms against what
    # was shown, never a snapshot that may lag the render
    ctx = spec["parts"]["context"]
    assert ctx["project_id"] == 1
    assert ctx["source_message_id"] == 100
    assert ctx["source_hash"] == f"{100:064x}"
    assert ctx["signals"]["sig-modal"]["artifact_id"] > 0
    for i, action in enumerate(("request", "dismiss")):
        tok = _token_for(spec, action)
        r = notify_actions.apply_notification(
            led, {**_notif(tok, n=70 + i),
                  "command_id": f"{tok}:{(70 + i):016x}",
                  "origin": dict(ORIGIN, message_id="signal-reply", thread_id="original-thread")},
            CFG, now=NOW)
        assert r["outcome"] == "applied", (action, r)
        assert r["action"] == action and r["modal"] is True
        assert isinstance(r["params"], dict)
    # nothing was mutated — no request row, signal still open
    assert led.db.execute(
        "SELECT COUNT(*) c FROM requests").fetchone()["c"] == 0


def test_gc_deletes_expired_tokens_and_old_specs(led, tmp_path):
    render = _deliverable(led, tmp_path)
    spec_path = (tmp_path / "data" / "discord_render"
                 / (render["delivery_id"] + ".json"))
    assert spec_path.exists()
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    # the card settled but its durable parts are pending — the spec
    # file is the only copy a restart-resume can replay, so gc keeps
    # it until the part plan reaches a terminal mix
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["spec_files"] == 0 and spec_path.exists()
    led.db.execute(
        "UPDATE notification_render_parts SET state='delivered' "
        "WHERE delivery_id=?", (render["delivery_id"],))
    notify_parts.update_state(
        led.db, render["delivery_id"], NOW)
    led.db.commit()
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["spec_files"] >= 1 and not spec_path.exists()
    led.db.execute("UPDATE notification_action_tokens SET expires_at=?",
                   (NOW - 1,))
    led.db.commit()
    out = notify_cards.gc(led, CFG, now=NOW)
    assert out["tokens"] >= 1


def test_failed_thread_keeps_body_button(led, tmp_path):
    """A card whose companion thread could not be created keeps the
    📄 button on the next render — the body has nowhere else to live."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "error_code": "http_403"}, CFG, now=NOW)
    assert r["thread_state"] == "failed"
    _msg(led, 103, 1, parent=100)          # drift -> update render
    notify_cards.sweep(led, CFG)
    upd = _latest_render(led)
    assert upd["op"] == "update"
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (upd["delivery_id"] + ".json")).read_text())
    ids = {b["id"] for row in spec["parts"]["action_rows"]
           for b in row}
    assert "body" in ids
    assert "thread_body" not in spec["parts"]


def test_created_thread_update_carries_body_and_detail_button(led, tmp_path):
    """The thread carries body parts while the private body view stays available."""
    render = _deliverable(led, tmp_path)
    _begin(led, render)
    _receipt(led, render, "0" * 15 + "1", message_id="m-9")
    _settle_bodies(led, render)
    r = notify_transport.apply_thread_receipt(led, {
        "version": 1, "op": "thread_receipt", "command_id": _uuid(5),
        "delivery_id": render["delivery_id"], "message_id": "m-9",
        "thread_id": "th-1"}, CFG, now=NOW)
    assert r["thread_state"] == "created"
    _msg(led, 103, 1, parent=100)
    notify_cards.sweep(led, CFG)
    upd = _latest_render(led)
    spec = json.loads(
        (tmp_path / "data" / "discord_render"
         / (upd["delivery_id"] + ".json")).read_text())
    assert spec["delivery"]["thread_id"] == "th-1"
    assert spec["parts"]["thread_body_parts"]   # durable part text (T7)
    assert [p for p in spec["parts"]["manifest"]
            if p["kind"] == "body_part"]
    ids = {b["id"] for row in spec["parts"]["action_rows"]
           for b in row}
    assert "body" in ids


def test_multipage_intent_plans_every_covered_body(led):
    """U03-F03: the card face opens on its last page, but the durable
    thread plan must carry the body of every message the intent covers —
    earlier pages included — in thread order."""
    _patient(led, 1)
    _msg(led, 100, 1, body="BODY-100")
    for m in range(101, 114):
        _msg(led, m, 1, parent=100, body=f"BODY-{m}")
    _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 114))}))
    spec = json.loads(_latest_render(led)["spec_json"])
    assert spec["parts"]["pages"] > 1
    assert spec["parts"]["page"] == spec["parts"]["pages"] - 1
    body = "".join(spec["parts"]["thread_body_parts"])
    positions = [body.index(f"BODY-{m}") for m in range(100, 114)]
    assert positions == sorted(positions)
    body_parts = [p for p in spec["parts"]["manifest"]
                  if p["kind"] == "body_part"]
    assert len(body_parts) == len(spec["parts"]["thread_body_parts"])


def test_tasks_button_only_on_thread_cards(led, tmp_path):
    """☑ pins a task list to a thread — only thread cards with open
    tasks carry it."""
    card, spec = _delivered_card(led, tmp_path)
    ids = {b["id"] for row in spec["parts"]["action_rows"] for b in row}
    assert "tasks" not in ids                 # no open task yet
    _request(led, src_mid=101, title="返信タスク")
    notify_cards.sweep(led, CFG, now=NOW)
    render = _latest_render(led)
    spec1 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    button = next(b for row in spec1["parts"]["action_rows"] for b in row
                  if b["id"] == "tasks")
    assert button["label"] == "タスク一覧"
    _patient(led, 2)
    _msg(led, 200, 2)
    _signal_row(led, "sig-nt", pid=2, mids=[200])
    ev = _intent(led, kind="signal", pid=2,
                 payload={"signal_keys": ["sig-nt"], "project_id": 2,
                          "type": "med_followup"})
    _dispatch(led, ev)
    delivered_source(led, card_id=3, mids=(200,))
    _dispatch(led, ev)
    render = _latest_render(led, 2)
    spec2 = json.loads(
        (tmp_path / "data" / "discord_render"
         / (render["delivery_id"] + ".json")).read_text())
    ids = {b["id"] for row in spec2["parts"]["action_rows"] for b in row}
    assert "tasks" not in ids


def test_tasks_view_lists_thread_requests(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)   # root 100 + reply 101
    rid1 = _request(led, src_mid=100, title="経過確認")
    rid2 = _request(led, src_mid=101, title="返信タスク",
                    status="in_progress", assignee="山田",
                    due="2026-10-01")
    _request(led, src_mid=100, title="取り下げ済", status="cancelled")
    _msg(led, 300, 1)
    _request(led, src_mid=300, title="別スレッド")
    _patient(led, 2)
    _msg(led, 400, 2)
    _request(led, pid=2, src_mid=400, title="別患者")
    r = _tasks_click(led, spec)
    assert r["outcome"] == "applied" and r["action"] == "tasks"
    ids = [t["request_id"] for t in r["tasks"]]
    assert ids == [rid1, rid2]          # open first; cancelled/other gone
    t1, t2 = r["tasks"]
    assert set(t1["transitions"]) == {"in_progress", "done"}
    assert set(t2["transitions"]) == {"done"}
    assert t2["assignee"] == "山田" and t2["due_date"] == "2026-10-01"
    for t in r["tasks"]:
        for tr in t["transitions"].values():
            ctx = r["token_ctx"].get(tr["token"])
            assert ctx and ctx["action"] == "task_status" \
                and ctx["project_id"] == 1


def test_task_status_transition_and_stale_reject(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rid = _request(led, src_mid=100, title="T")
    r = _tasks_click(led, spec)
    tr = r["tasks"][0]["transitions"]["in_progress"]
    # the click arrives on the ephemeral list message — not the card's
    # bound message — which the token's "ephemeral" flag legitimizes
    out = notify_actions.apply_notification(
        led, {**_notif(tr["token"]),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "applied" and out["status"] == "in_progress"
    row = led.db.execute(
        "SELECT status,revision FROM requests WHERE request_id=?",
        (rid,)).fetchone()
    assert row["status"] == "in_progress" and row["revision"] == 2
    # the same view token is now stale — rejected, never double-applied
    out2 = notify_actions.apply_notification(
        led, {**_notif(tr["token"], n=21),
              "command_id": f"{tr['token']}:{'cd' * 8}",
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out2["outcome"] == "rejected" and out2["error"] == "stale_task"


def test_task_status_done_absorbs_and_terminal_rejected(led, tmp_path):
    card, spec = _delivered_card(led, tmp_path)
    rid = _request(led, src_mid=100)
    r = _tasks_click(led, spec)
    done_tok = r["tasks"][0]["transitions"]["done"]["token"]
    out = notify_actions.apply_notification(
        led, {**_notif(done_tok),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "applied" and out["status"] == "done"
    # a fresh list offers no transition for a done task
    r2 = _tasks_click(led, spec, suffix="cd")
    t = next(t for t in r2["tasks"] if t["request_id"] == rid)
    assert t["transitions"] == {}
    # defensive: a hand-minted token pinning the live revision still
    # refuses to move a terminal row (the table is the authority)
    rid2 = _request(led, src_mid=101, status="cancelled")
    tok = notify_action_tokens.mint(
        led.db, card["card_id"], "task_status",
        {"request_id": rid2, "status": "done", "ephemeral": True},
        {"request_rev": 1}, NOW)
    led.db.commit()
    out2 = notify_actions.apply_notification(
        led, {**_notif(tok),
              "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out2["outcome"] == "rejected" \
        and out2["error"] == "request_not_open"


def test_task_status_rejects_non_ephemeral_mismatch(led, tmp_path):
    """Card-bound tokens still require the bound message — only tokens
    minted for an ephemeral surface may arrive on another message."""
    card, spec = _delivered_card(led, tmp_path)
    tok = _tasks_token(led, spec)
    out = notify_actions.apply_notification(
        led, {**_notif(tok), "origin": dict(ORIGIN, message_id="eph-1")},
        CFG, now=NOW)
    assert out["outcome"] == "rejected" \
        and out["error"] == "origin_mismatch"
