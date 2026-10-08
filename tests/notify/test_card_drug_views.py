"""💊 薬剤を確認 / 薬剤を検索 — runner side card views over a synthetic
ledger and a fictional private dictionary. No live DB, network or LLM."""
from __future__ import annotations

import hashlib
import json

import pytest

import drug_map
import notify_cards
import notify_cmds
from notify_testkit import (
    CFG, CLICKER, NOW, ORIGIN, _deliver, _dispatch, _intent, _llm_extract,
    _seed_thread, _spec, _token_for, led, pinned_clock)
from test_drug_map import DOCUMENT

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")

MED = {"name": "キラナ", "dose": "5mg", "action": "start", "subject": "patient",
       "status": "current", "negated": False, "unverified": False,
       "evidence": "fictional quotation only"}


def _ids(spec):
    return [b["id"] for row in spec["parts"]["action_rows"] for b in row]


def _card(led, meds=(MED,), mid=101):
    _seed_thread(led)
    if meds is not None:
        _llm_extract(led, mid, {"meds": list(meds)})
    _dispatch(led, _intent(led))
    _deliver(led)
    return _spec(led)


def _dictionary(tmp_path, led, *, activate=True):
    raw = json.dumps(DOCUMENT, ensure_ascii=False).encode()
    path = tmp_path / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    if activate:
        drug_map.derive(led, drug_map.load(path, expected_sha256=sha))
    return {**CFG, "drug_map": {"path": str(path), "sha256": sha}}


def _click(led, spec, action, inputs=None, cfg=CFG, origin=None, token=None, actor=CLICKER):
    tok = token or _token_for(spec, action)
    req = {"version": 1, "op": "notification",
           "command_id": f"{tok}:{hashlib.sha256(actor.encode()).hexdigest()[:16]}", "actor": actor,
           "token": tok, "origin": origin or dict(ORIGIN, message_id="m-9")}
    if inputs:
        req["input"] = inputs
        req["command_id"] = f"{tok}:{hashlib.sha256(json.dumps(inputs).encode()).hexdigest()[:16]}"
    assert notify_cmds.validate_int(req) is None
    return notify_cards.apply_notification(led, req, cfg, now=NOW)


def test_no_medication_and_no_dictionary_emit_no_drug_buttons(led):
    ids = _ids(_card(led, meds=None))
    assert "meds" not in ids and "drugsearch" not in ids


def test_meds_view_lists_the_posts_medication_without_dictionary(led):
    spec = _card(led)
    assert "meds" in _ids(spec) and "drugsearch" not in _ids(spec)
    r = _click(led, spec, "meds")
    assert (r["outcome"], r["action"]) == ("applied", "list")
    view = r["list"]
    assert view["title"].startswith("💊 ") and view["title"].endswith("選択した投稿の薬剤")
    # one post, one page: no navigation or page lines
    assert view["head"][0].startswith("2026-09-24 ")
    assert view["head"][0].endswith("の投稿 — 1件")
    assert "有効化されていない" in view["head"][1]
    assert len(view["head"]) == 2
    assert view["page"] == 0 and view["pages"] == 1
    assert view["items"] == [{"project_id": 1,
                              "text": "・キラナ 5mg[開始]\n　→ 辞書候補なし"}]
    assert "参考情報" not in "".join(view["notes"])
    # ⚠ pins this same post, so the report hint is offered
    assert "抽出の誤りは「誤りを報告」→「薬」から報告できます。" in view["notes"]
    # the view reaches the clicker live — the receipt never stores it
    stored = led.db.execute("SELECT receipt_json FROM command_receipts "
                            "ORDER BY rowid DESC LIMIT 1").fetchone()[0]
    assert "キラナ" not in stored


def test_meds_view_shows_the_active_candidate_and_offers_search(led, tmp_path):
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED, {**MED, "name": "共通架空"},
                                     {**MED, "name": "キラナ", "subject": "family"}]})
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    assert {"meds", "drugsearch"} <= set(_ids(spec))
    view = _click(led, spec, "meds", cfg=cfg)["list"]
    assert view["head"][-1] == "辞書の候補はすべて未確認です"
    assert any(n.startswith("辞書 fictional-v1@") for n in view["notes"])
    texts = [i["text"] for i in view["items"]]
    # family mention stays out — same rule as the card's 薬剤 line
    assert texts == ["・キラナ 5mg[開始]\n　→ 候補: 架空成分甲（成分・fiction-i1）",
                     "・共通架空 5mg[開始]\n　→ 複数の候補（2件）: 用量・剤形を原文で確認してください"]
    assert "候補の名称・別名は「薬剤を検索」で確認できます。" in view["notes"]


def test_drug_search_opens_modal_then_answers_reference_hits(led, tmp_path):
    _seed_thread(led)
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    first = _click(led, spec, "drugsearch", cfg=cfg)
    assert first["modal"] is True and first["action"] == "drugsearch"
    view = _click(led, spec, "drugsearch", {"query": "ｷﾗﾅ"}, cfg=cfg)["list"]
    # half-width kana folds; キラナタール (乙) is a substring hit too
    assert view["head"][0] == "2件（名称・別名・コードの部分一致）"
    assert [i["text"] for i in view["items"]] == [
        "・架空成分甲（成分・fiction-i1）\n　一致した別名: キラナ、別キラナ",
        "・架空成分乙（成分・fiction-i2）\n　一致した別名: キラナタール"]
    none = _click(led, spec, "drugsearch", {"query": "存在しない薬"}, cfg=cfg)["list"]
    assert none["items"] == [] and none["empty"].startswith("一致する候補はありません")


def test_drug_search_refuses_a_dictionary_that_is_not_the_active_generation(led, tmp_path):
    spec = _card(led)
    cfg = _dictionary(tmp_path, led, activate=False)
    assert drug_map.active_dictionary(led.db, cfg)[1] == "inactive"
    assert drug_map.active_dictionary(led.db, CFG)[1] == "unconfigured"
    bad = {**CFG, "drug_map": {"path": "relative", "sha256": "0" * 64}}
    assert drug_map.active_dictionary(led.db, bad)[1] == "invalid"
    assert "drugsearch" not in _ids(spec)


@pytest.mark.parametrize("action", ["meds", "drugsearch"])
@pytest.mark.parametrize("fault", [None, "wrong_thread", "unknown_post", "pending", "unknown"])
def test_thread_drug_views_require_this_cards_delivered_body(led, tmp_path, action, fault):
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED]})
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    assert spec["parts"]["thread_drug_actions"] is True
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
        if fault in ("pending", "unknown"):
            led.db.execute("UPDATE notification_render_parts SET state=? WHERE kind='body_part'",
                           (fault,))
    origin = dict(ORIGIN, thread_id="other-thread" if fault == "wrong_thread" else "synthetic-thread",
                  message_id="unknown-body" if fault == "unknown_post" else "r-body:0001")
    answer = _click(led, spec, action, {"query": "キラナ"} if action == "drugsearch" else None,
                    cfg=cfg, origin=origin)
    if fault:
        assert answer["error"] == "origin_mismatch"
    else:
        assert answer["outcome"] == "applied" and answer["action"] == "list"


def test_write_action_cannot_use_thread_drug_view_origin(led):
    spec = _card(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
    answer = _click(led, spec, "ack",
                    origin=dict(ORIGIN, thread_id="synthetic-thread", message_id="r-body:0001"))
    assert answer["error"] == "origin_mismatch"
    assert led.db.execute("SELECT COUNT(*) FROM notification_acknowledgements").fetchone()[0] == 0


def test_drug_thread_flag_is_rejected_by_an_old_worker(led, monkeypatch):
    from adapters.common import spec as contract
    spec = _card(led)
    assert contract.validate(spec) is spec
    monkeypatch.setattr(contract, "PARTS_KEYS", contract.PARTS_KEYS - {"thread_drug_actions"})
    with pytest.raises(ValueError, match="unsupported_parts_key"):
        contract.validate(spec)


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_drug_thread_flag_is_strict(led, value):
    from adapters.common import spec as contract
    spec = _card(led)
    spec["parts"]["thread_drug_actions"] = value
    with pytest.raises(ValueError, match="bad_thread_drug_actions"):
        contract.validate(spec)


def test_dictionary_activation_refreshes_a_card_without_medication(led, tmp_path):
    _card(led, meds=None)
    cfg = _dictionary(tmp_path, led)
    assert notify_cards.rerender_message_cards(led, cfg, 1, 100, now=NOW)
    assert "drugsearch" in _ids(_spec(led))


@pytest.mark.parametrize("fault,error", [("expired", "token_expired"),
                                         ("revoked", "card_revoked"),
                                         ("foreign_scope", "scope_mismatch")])
def test_thread_drug_view_preserves_existing_gates(led, fault, error):
    spec = _card(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
        if fault == "expired":
            led.db.execute("UPDATE notification_action_tokens SET expires_at=?", (NOW - 1,))
        elif fault == "revoked":
            led.db.execute("UPDATE notification_cards SET delivery_state='revoked'")
    origin = dict(ORIGIN, thread_id="synthetic-thread", message_id="r-body:0001")
    if fault == "foreign_scope":
        origin["channel_id"] = "another-channel"
    assert _click(led, spec, "meds", origin=origin)["error"] == error


def test_another_cards_delivered_body_cannot_open_drug_view(led):
    spec = _card(led)
    _seed_thread(led, pid=2, root=200, mids=(200, 201))
    _dispatch(led, _intent(led, pid=2, payload={"message_ids": [200, 201]}))
    _deliver(led)
    other = _spec(led, card_id=2)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread' WHERE card_id=1")
        led.db.execute("UPDATE notification_render_parts SET remote_id='foreign-body' "
                       "WHERE delivery_id=? AND part_id='body:0001'", (other["delivery_id"],))
    answer = _click(led, spec, "meds",
                    origin=dict(ORIGIN, thread_id="synthetic-thread", message_id="foreign-body"))
    assert answer["error"] == "origin_mismatch"


def _navigation(result, label):
    return next(button["token"] for button in result["navigation"] if button["label"] == label)


def test_all_medications_and_old_post_candidates_are_reachable_privately(led, tmp_path):
    from adapters.common import text
    _seed_thread(led)
    _llm_extract(led, 100, {"meds": [MED]})
    _llm_extract(led, 101, {"meds": [{**MED, "name": f"合成薬{i:02}"} for i in range(1, 17)]})
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
    original = dict(ORIGIN, thread_id="synthetic-thread", message_id="r-body:0001")
    private = dict(original, message_id="private-answer")
    first = _click(led, spec, "meds", cfg=cfg, origin=original)
    current, seen = first, []
    for page in range(4):
        assert current["list"]["page"] == page and current["list"]["pages"] == 4
        assert len(current["list"]["items"]) <= 5 and current["list"]["more"] == 0
        seen.extend(row["text"] for row in current["list"]["items"])
        if page < 3:
            current = _click(led, spec, "meds", cfg=cfg, origin=private,
                             token=_navigation(current, "次の5件"))
    assert len(seen) == 16 and len(set(seen)) == 16
    assert all(any(f"合成薬{i:02}" in row for row in seen) for i in range(1, 17))
    rendered = "\n".join(value for value, _ in text.view_answer(current, lambda pid: pid == 1))
    assert "合成薬16" in rendered and "他1件" not in rendered
    old = _click(led, spec, "meds", cfg=cfg, origin=private,
                 token=_navigation(current, "古い投稿"))
    assert old["list"]["head"][0] == "薬剤記載のある投稿 2/2（新しい順）"
    assert "キラナ" in old["list"]["items"][0]["text"]
    assert "架空成分甲" in old["list"]["items"][0]["text"]
    newer = _click(led, spec, "meds", cfg=cfg, origin=private,
                   token=_navigation(old, "新しい投稿"))
    assert newer["list"]["page"] == 0 and "合成薬01" in newer["list"]["items"][0]["text"]
    wrong = _click(led, spec, "meds", cfg=cfg, origin=private,
                   token=_navigation(first, "次の5件"), actor="discord:1002")
    assert wrong["error"] == "actor_mismatch"
    stored = [json.loads(row[0]) for row in led.db.execute("SELECT receipt_json FROM command_receipts")]
    assert all("navigation" not in row and "token_ctx" not in row and "list" not in row for row in stored)


@pytest.mark.parametrize("state", ["off", "failed", "deleted"])
def test_slack_without_a_usable_thread_hides_drug_actions(led, tmp_path, state):
    from slack_testkit import SLACK
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED]})
    dictionary = _dictionary(tmp_path, led)
    cfg = {**SLACK, "drug_map": dictionary["drug_map"],
           "notify": {**SLACK["notify"], "card_thread": state != "off"}}
    _dispatch(led, _intent(led), cfg)
    if state != "off":
        with led.db:
            led.db.execute("UPDATE notification_cards SET thread_state=?", (state,))
            notify_cards._issue_render(led.db, 1, cfg, NOW, [], force=True)
    ids = _ids(_spec(led))
    assert "meds" not in ids and "drugsearch" not in ids and "body" in ids


@pytest.mark.parametrize("fault,error", [("wrong_thread", "card_not_found"),
    ("unknown_post", "card_not_found"), ("unknown_delivery", "card_not_found"),
    ("foreign_channel", "card_not_found"), ("foreign_guild", "scope_mismatch"),
    ("revoked", "card_revoked")])
def test_refresh_from_thread_requires_trusted_body_and_existing_scope(led, fault, error):
    _card(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
        if fault == "unknown_delivery":
            led.db.execute("UPDATE notification_render_parts SET state='unknown' WHERE kind='body_part'")
        elif fault == "revoked":
            led.db.execute("UPDATE notification_cards SET delivery_state='revoked'")
    origin = dict(ORIGIN, message_id="r-body:0001", thread_id="synthetic-thread")
    if fault == "wrong_thread":
        origin["thread_id"] = "other-thread"
    elif fault == "unknown_post":
        origin["message_id"] = "other-post"
    elif fault == "foreign_channel":
        origin["channel_id"] = "other-channel"
    elif fault == "foreign_guild":
        origin["guild_id"] = "other-guild"
    result = notify_cards.apply_refresh(led, {"version": 1, "op": "refresh",
        "command_id": "11111111-1111-4111-8111-111111111111", "actor": CLICKER,
        "origin": origin}, CFG, now=NOW)
    assert result["error"] == error


def test_drug_post_before_twenty_medication_free_replies_remains_available(led):
    _seed_thread(led, mids=tuple(range(100, 125)))
    _llm_extract(led, 100, {"meds": [MED]})
    _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 125))}))
    _deliver(led)
    spec = _spec(led)
    assert "meds" in _ids(spec)
    result = _click(led, spec, "meds")
    assert not result["list"]["head"][0].startswith("薬剤記載のある投稿")
    assert "キラナ" in result["list"]["items"][0]["text"]
