"""💊 薬剤を確認 / 薬剤を検索 — runner side card views over a synthetic
ledger and a fictional private dictionary. No live DB, network or LLM."""
from __future__ import annotations
import extract_llm_engine
import extract_llm_runner

import hashlib
import json

import pytest

import drug_map
import notify_cards
import notify_actions
from notify_testkit import (
    CFG, CLICKER, MED, NOW, ORIGIN, _deliver, _dispatch, _drug_card, _drug_click, _extract,
    _intent, _llm_extract, _seed_thread, _spec, led, pinned_clock)
from notify_testkit import _button_ids as _ids
from medications_testkit import DOCUMENT

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")

def _dictionary(tmp_path, led, *, activate=True, document=None):
    raw = json.dumps(DOCUMENT if document is None else document, ensure_ascii=False).encode()
    path = tmp_path / "fictional-drug-map.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    sha = hashlib.sha256(raw).hexdigest()
    if activate:
        drug_map.derive(led, drug_map.load(path, expected_sha256=sha))
    return {**CFG, "drug_map": {"path": str(path), "sha256": sha}}


def test_no_medication_and_no_dictionary_emit_no_drug_buttons(led):
    ids = _ids(_drug_card(led, meds=None))
    assert "meds" not in ids and "drugsearch" not in ids


def test_meds_view_lists_the_posts_medication_without_dictionary(led):
    spec = _drug_card(led)
    assert "meds" in _ids(spec) and "drugsearch" not in _ids(spec)
    r = _drug_click(led, spec, "meds")
    assert (r["outcome"], r["action"]) == ("applied", "list")
    view = r["list"]
    assert view["title"].startswith("💊 ") and view["title"].endswith("選択した投稿の薬剤")
    # one post, one page: no navigation or page lines
    assert view["head"][0].startswith("09-24 ")
    assert view["head"][0].endswith("の投稿 — 1件")
    assert "設定されていない" in view["head"][1]   # the real reason: no dictionary configured
    assert len(view["head"]) == 2
    assert view["page"] == 0 and view["pages"] == 1
    assert view["items"] == [{"project_id": 1,
                              "text": "・キラナ 5mg[開始]"}]
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
    view = _drug_click(led, spec, "meds", cfg=cfg)["list"]
    assert view["head"][-1] == "辞書の候補はすべて未確認です"
    assert any(n.startswith("辞書 fictional-v1@") for n in view["notes"])
    texts = [i["text"] for i in view["items"]]
    # family mention stays out — same rule as the card's 薬剤 line
    assert texts == ["・キラナ 5mg[開始]\n　→ 候補: 架空成分甲（成分・fiction-i1）",
                     "・共通架空 5mg[開始]\n　→ 複数の候補（2件）: 用量・剤形を原文で確認してください"
                     "\n　辞書の一致候補・未確認: 2件\n　・架空成分甲（成分）\n　・架空成分乙（成分）"]
    assert "候補の名称・別名は「薬剤を検索」で確認できます。" in view["notes"]


def test_drug_search_opens_modal_then_answers_reference_hits(led, tmp_path):
    _seed_thread(led)
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    first = _drug_click(led, spec, "drugsearch", cfg=cfg)
    assert first["modal"] is True and first["action"] == "drugsearch"
    view = _drug_click(led, spec, "drugsearch", {"query": "ｷﾗﾅ"}, cfg=cfg)["list"]
    # half-width kana folds; キラナタール (乙) is a substring hit too
    assert view["head"][0] == "2件（名称・別名・コードの部分一致）"
    assert [i["text"] for i in view["items"]] == [
        "・架空成分甲（成分・fiction-i1）\n　一致した別名: キラナ、別キラナ",
        "・架空成分乙（成分・fiction-i2）\n　一致した別名: キラナタール"]
    none = _drug_click(led, spec, "drugsearch", {"query": "存在しない薬"}, cfg=cfg)["list"]
    assert none["items"] == [] and none["empty"].startswith("一致する候補はありません")


def test_drug_search_refuses_a_dictionary_that_is_not_the_active_generation(led, tmp_path):
    spec = _drug_card(led)
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
    answer = _drug_click(led, spec, action, {"query": "キラナ"} if action == "drugsearch" else None,
                    cfg=cfg, origin=origin)
    if fault:
        assert answer["error"] == "origin_mismatch"
    else:
        assert answer["outcome"] == "applied" and answer["action"] == "list"


def test_write_action_cannot_use_thread_drug_view_origin(led):
    spec = _drug_card(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
    answer = _drug_click(led, spec, "ack",
                    origin=dict(ORIGIN, thread_id="synthetic-thread", message_id="r-body:0001"))
    assert answer["error"] == "origin_mismatch"
    assert led.db.execute("SELECT COUNT(*) FROM notification_acknowledgements").fetchone()[0] == 0


def test_drug_thread_flag_is_rejected_by_an_old_worker(led, monkeypatch):
    from adapters.common import spec as contract
    spec = _drug_card(led)
    assert contract.validate(spec) is spec
    monkeypatch.setattr(contract, "PARTS_KEYS", contract.PARTS_KEYS - {"thread_drug_actions"})
    with pytest.raises(ValueError, match="unsupported_parts_key"):
        contract.validate(spec)


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_drug_thread_flag_is_strict(led, value):
    from adapters.common import spec as contract
    spec = _drug_card(led)
    spec["parts"]["thread_drug_actions"] = value
    with pytest.raises(ValueError, match="bad_thread_drug_actions"):
        contract.validate(spec)


def test_dictionary_activation_refreshes_a_card_without_medication(led, tmp_path):
    _drug_card(led, meds=None)
    cfg = _dictionary(tmp_path, led)
    assert notify_cards.rerender_message_cards(led, cfg, 1, 100, now=NOW)
    assert "drugsearch" in _ids(_spec(led))


@pytest.mark.parametrize("fault,error", [("expired", "token_expired"),
                                         ("revoked", "card_revoked"),
                                         ("foreign_scope", "scope_mismatch")])
def test_thread_drug_view_preserves_existing_gates(led, fault, error):
    spec = _drug_card(led)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread'")
        if fault == "expired":
            led.db.execute("UPDATE notification_action_tokens SET expires_at=?", (NOW - 1,))
        elif fault == "revoked":
            led.db.execute("UPDATE notification_cards SET delivery_state='revoked'")
    origin = dict(ORIGIN, thread_id="synthetic-thread", message_id="r-body:0001")
    if fault == "foreign_scope":
        origin["channel_id"] = "another-channel"
    assert _drug_click(led, spec, "meds", origin=origin)["error"] == error


def test_another_cards_delivered_body_cannot_open_drug_view(led):
    spec = _drug_card(led)
    _seed_thread(led, pid=2, root=200, mids=(200, 201))
    _dispatch(led, _intent(led, pid=2, payload={"message_ids": [200, 201]}))
    _deliver(led)
    other = _spec(led, card_id=2)
    with led.db:
        led.db.execute("UPDATE notification_cards SET thread_id='synthetic-thread' WHERE card_id=1")
        led.db.execute("UPDATE notification_render_parts SET remote_id='foreign-body' "
                       "WHERE delivery_id=? AND part_id='body:0001'", (other["delivery_id"],))
    answer = _drug_click(led, spec, "meds",
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
    first = _drug_click(led, spec, "meds", cfg=cfg, origin=original)
    current, seen = first, []
    for page in range(4):
        assert current["list"]["page"] == page and current["list"]["pages"] == 4
        assert len(current["list"]["items"]) <= 5 and current["list"]["more"] == 0
        seen.extend(row["text"] for row in current["list"]["items"])
        if page < 3:
            current = _drug_click(led, spec, "meds", cfg=cfg, origin=private,
                             token=_navigation(current, "次の5件"))
    assert len(seen) == 16 and len(set(seen)) == 16
    assert all(any(f"合成薬{i:02}" in row for row in seen) for i in range(1, 17))
    rendered = "\n".join(value for value, _ in text.view_answer(current, lambda pid: pid == 1))
    assert "合成薬16" in rendered and "他1件" not in rendered
    old = _drug_click(led, spec, "meds", cfg=cfg, origin=private,
                 token=_navigation(current, "古い投稿"))
    assert old["list"]["head"][0] == "薬剤記載のある投稿 2/2（新しい順）"
    assert "キラナ" in old["list"]["items"][0]["text"]
    assert "架空成分甲" in old["list"]["items"][0]["text"]
    newer = _drug_click(led, spec, "meds", cfg=cfg, origin=private,
                   token=_navigation(old, "新しい投稿"))
    assert newer["list"]["page"] == 0 and "合成薬01" in newer["list"]["items"][0]["text"]
    wrong = _drug_click(led, spec, "meds", cfg=cfg, origin=private,
                   token=_navigation(first, "次の5件"), actor="discord:1002")
    assert wrong["error"] == "actor_mismatch"
    stored = [json.loads(row[0]) for row in led.db.execute("SELECT receipt_json FROM command_receipts")]
    assert all("navigation" not in row and "token_ctx" not in row and "list" not in row for row in stored)


@pytest.mark.parametrize("state", ["failed", "deleted"])
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
            notify_cards.issue_render(led.db, 1, cfg, NOW, [], force=True)
    ids = _ids(_spec(led))
    assert "meds" not in ids and "drugsearch" not in ids and "body" in ids


@pytest.mark.parametrize("fault,error", [("wrong_thread", "card_not_found"),
    ("unknown_post", "card_not_found"), ("unknown_delivery", "card_not_found"),
    ("foreign_channel", "card_not_found"), ("foreign_guild", "scope_mismatch"),
    ("revoked", "card_revoked")])
def test_refresh_from_thread_requires_trusted_body_and_existing_scope(led, fault, error):
    _drug_card(led)
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
    result = notify_actions.apply_refresh(led, {"version": 1, "op": "refresh",
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
    result = _drug_click(led, spec, "meds")
    assert not result["list"]["head"][0].startswith("薬剤記載のある投稿")
    assert "キラナ" in result["list"]["items"][0]["text"]


def test_unverified_medication_says_unverified_without_a_source_label(led):
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": []})       # the LLM saw no medication
    _extract(led, 101, {"medications": [{"name": "ソラノ", "dose": "1錠"}]})
    _dispatch(led, _intent(led))
    _deliver(led)
    view = _drug_click(led, _spec(led), "meds")["list"]
    firsts = [item["text"].split("\n", 1)[0] for item in view["items"]]
    assert firsts == ["・ソラノ 1錠（未確認）"]


def test_identical_search_input_from_another_member_is_not_a_replay(led, tmp_path):
    _seed_thread(led)
    cfg = _dictionary(tmp_path, led)
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    hits = _drug_click(led, spec, "drugsearch", {"query": "ｷﾗﾅ"}, cfg=cfg)
    assert hits["list"]["items"]
    # another member sending the same input is its own command, not a replay
    # of the first member's stored receipt
    other = _drug_click(led, spec, "drugsearch", {"query": "ｷﾗﾅ"}, cfg=cfg, actor="discord:2002")
    assert other["command_id"] != hits["command_id"]
    assert other["actor"] == "discord:2002" and hits["actor"] == CLICKER


def test_reference_suggestions_only_search_visible_page_and_never_write(led, tmp_path, monkeypatch):
    import notify_views
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": name} for name in
                                   ("キラ", "ｷﾗ", "キラナタ", "共通架空", "存在しない架空", "キラーナ未一致")]})
    cfg = _dictionary(tmp_path, led)
    calls = []
    original = drug_map.DrugMap.search
    def search(self, query, *, limit=20):
        calls.append((query, limit))
        return original(self, query, limit=limit)
    monkeypatch.setattr(drug_map.DrugMap, "search", search)
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg, page=0)
    assert view["pages"] == 2 and len(view["items"]) == 5
    assert len(calls) == 4 and all(limit == 3 for _, limit in calls)
    text = "\n".join(i["text"] for i in view["items"])
    assert "参考候補・未確認・部分一致: 2件" in text
    assert "参考候補・未確認・部分一致: 0件" in text
    calls.clear()
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg, page=99)
    assert view["page"] == 1 and len(view["items"]) == 1 and len(calls) == 1
    assert led.db.total_changes == before


def test_llm_extraction_to_source_bound_reference_view(led, tmp_path, monkeypatch):
    import extract_llm
    import notify_views

    body = "本人はキラを1日2回服用しています。"
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=101", (body,))
    prompts = []

    def stub(prompt, **_kwargs):
        prompts.append(prompt)
        return {"symptoms": [], "meds": [{"name": "キラ", "freq": "1日2回",
                "action": "none", "status": "current", "subject": "patient",
                "negated": False, "evidence": body}]}

    monkeypatch.setattr(extract_llm, "_llm_call", stub)
    monkeypatch.setattr(extract_llm_engine, '_llm_call', extract_llm._llm_call)
    monkeypatch.setattr(extract_llm_runner, '_llm_call', extract_llm._llm_call)
    extracted = extract_llm.llm_extract(body)
    assert prompts and extracted["meds"][0]["name"] == "キラ"
    assert extracted["meds"][0]["evidence"] == body
    _llm_extract(led, 101, extracted)
    cfg = _dictionary(tmp_path, led)
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    assert len(view["items"]) == 1
    text = view["items"][0]["text"]
    assert "キラ" in text and "1日2回" in text
    assert "参考候補・未確認・部分一致: 2件" in text
    assert "架空成分甲" in text and "架空成分乙" in text
    assert led.db.total_changes == before


@pytest.mark.parametrize("fault", ["no_cfg", "unapproved", "generation", "invalid_name", "stale_ref"])
def test_reference_suggestions_require_current_approved_dictionary(led, tmp_path, monkeypatch, fault):
    import notify_views
    from dataclasses import replace
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "キラ"}]})
    cfg = _dictionary(tmp_path, led)
    dictionary, state = drug_map.active_dictionary(led.db, cfg)
    if fault == "no_cfg":
        cfg = CFG
    elif fault == "generation":
        with led.db:
            led.db.execute("DELETE FROM artifacts WHERE kind=?", (drug_map.PROGRESS_KIND,))
    elif fault == "unapproved":
        dictionary = replace(dictionary, source=replace(dictionary.source, approved_by=""))
        monkeypatch.setattr(drug_map, "active_dictionary", lambda *a: (dictionary, state))
    else:
        real = notify_views.structured_view.medication_entries
        def entries(*a, **kwargs):
            groups = real(*a, **kwargs)
            return tuple([(text, {**ref, "name": ""} if fault == "invalid_name" else
                                  {**ref, "dict_sha256": "0" * 64}) for text, ref in group] for group in groups)
        monkeypatch.setattr(notify_views.structured_view, "medication_entries", entries)
    monkeypatch.setattr(drug_map.DrugMap, "search", lambda *a, **kw: pytest.fail("unexpected reference search"))
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    assert not any("参考候補" in item["text"] for item in view["items"])


def test_reference_many_candidates_are_bounded_without_strength_inference(led, tmp_path):
    import notify_views
    from copy import deepcopy
    document = deepcopy(DOCUMENT)
    document["entries"] = [{"id": f"fiction-{i}", "kind": "ingredient",
        "display": f"架空薬{i}錠{5 + i}mg", "aliases": [f"架空候補{i}"], "codes": {}, "forms": []}
        for i in range(8)]
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "架空候補"}, {**MED, "name": "架空候補9液50mg"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = view["items"][0]["text"]
    assert "参考候補・未確認・部分一致: 8件" in text and "他5件は省略" in text
    assert text.count("\n　・") == 3 and "候補: " not in text
    assert "部分一致: 0件" in view["items"][1]["text"]
    assert all(len(item["text"]) < 2000 for item in view["items"])


def test_exact_official_candidate_keeps_product_name_strength_and_unit(led, tmp_path):
    import notify_views
    from medications_testkit import _source_document
    document = _source_document()
    entry = document["entries"][0]
    entry["products"] = [{"medicine": "900000001", "name": "架空製品錠5mg", "unit": "錠", "form_code": "1"}]
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "架空製品"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    assert "辞書の製品情報（未確認）:" in view["items"][0]["text"]
    assert "架空製品錠5mg（単位: 錠）" in view["items"][0]["text"]
    assert led.db.total_changes == before


def test_reference_long_names_keep_tail_and_mark_omission(led, tmp_path, monkeypatch):
    import notify_views
    from copy import deepcopy
    document = deepcopy(DOCUMENT)
    document["entries"][0]["display"] = "架空" + "長い製品名" * 30 + "徐放錠10mg"
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "ｷﾗ"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = view["items"][0]["text"]
    assert "参考候補・未確認・部分一致" in text
    assert "徐放錠10mg（名称省略）" in text
    assert "薬剤を検索で絞り込んでください" in text
    assert all(len(line) <= 80 for line in text.splitlines() if line.startswith("　・"))
    before = led.db.total_changes
    monkeypatch.setattr(drug_map.DrugMap, "search", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic")))
    failed = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    assert "ｷﾗ" in failed["items"][0]["text"] and "参考候補" not in failed["items"][0]["text"]
    assert led.db.total_changes == before


def test_slack_thread_option_off_keeps_drug_search_available(led, tmp_path):
    from slack_testkit import SLACK
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED]})
    cfg = {**SLACK, "drug_map": _dictionary(tmp_path, led)["drug_map"],
           "notify": {**SLACK["notify"], "card_thread": False}}
    _dispatch(led, _intent(led), cfg)
    ids = _ids(_spec(led))
    assert "drugsearch" in ids and "body" in ids


@pytest.mark.parametrize("fault", ["unset", "bad_pin", "changed_config", "ref_id", "ref_sha", "ref_generation"])
def test_meds_view_never_displays_old_reference_after_config_change(led, tmp_path, monkeypatch, fault):
    import notify_views
    from copy import deepcopy
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED]})
    cfg = _dictionary(tmp_path, led)
    original = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    assert "架空成分甲" in original["items"][0]["text"]
    if fault == "unset":
        cfg = CFG
    elif fault == "bad_pin":
        cfg = {**cfg, "drug_map": {**cfg["drug_map"], "sha256": "0" * 64}}
    elif fault == "changed_config":
        document = deepcopy(DOCUMENT)
        document["dict_id"] = "fictional-v2"
        cfg = _dictionary(tmp_path, led, activate=False, document=document)
    else:
        real = notify_views.structured_view.medication_entries
        key, value = {"ref_id": ("dict_id", "wrong-id"), "ref_sha": ("dict_sha256", "0" * 64),
                      "ref_generation": ("resolver_version", "old-resolver")}[fault]
        def entries(*args, **kwargs):
            return tuple([(text, {**ref, key: value}) for text, ref in group] for group in real(*args, **kwargs))
        monkeypatch.setattr(notify_views.structured_view, "medication_entries", entries)
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = json.dumps(view, ensure_ascii=False)
    assert "キラナ 5mg[開始]" in text
    assert "架空成分甲" not in text and "fictional-v1@" not in text
    assert "参考候補" not in text and "→ 候補:" not in text
    if fault.startswith("ref_"):
        assert "辞書照合はまだありません" in text
    else:
        assert "候補は表示できません" in text
    assert led.db.total_changes == before


def test_no_config_compatibility_uses_source_bound_refs_without_reference_search(led, tmp_path, monkeypatch):
    import notify_views
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [MED]})
    _dictionary(tmp_path, led)
    monkeypatch.setattr(drug_map.DrugMap, "search", lambda *a, **kw: pytest.fail("no live pin"))
    view = notify_views.meds_view(led.db, 1, 101)
    assert "候補: 架空成分甲" in view["items"][0]["text"]
    assert "参考候補" not in view["items"][0]["text"]


def test_ambiguous_exact_candidates_precede_misleading_partial_names(led, tmp_path, monkeypatch):
    import notify_views
    from copy import deepcopy
    from medications_testkit import _source_document
    document = _source_document()
    for entry in document["entries"]:
        entry["aliases"] = ["共通架空名"]
    exact_names = [e["display"] for e in document["entries"]]
    template = document["entries"][0]
    for i in range(3):
        extra = deepcopy(template)
        medicine = str(800000001 + i)
        extra.update(id="mhlw:medicine:" + medicine, display=f"紛らわしい架空製品{i}",
                     aliases=[f"共通架空名の別製品{i}"], codes={"medicine": medicine})
        extra["source_codes"][0]["medicine"] = medicine
        document["entries"].insert(0, extra)
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "共通架空名"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    monkeypatch.setattr(drug_map.DrugMap, "search", lambda *a, **kw: pytest.fail("exact ambiguity must not substring-search"))
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = view["items"][0]["text"]
    assert "辞書の一致候補・未確認: 2件" in text
    assert all(name in text for name in exact_names)
    assert "紛らわしい" not in text and "部分一致" not in text
    assert "（製品）" in text and "（一般名処方）" in text
    assert led.db.total_changes == before


def test_ambiguous_exact_candidates_have_three_item_limit(led, tmp_path):
    import notify_views
    from copy import deepcopy
    document = deepcopy(DOCUMENT)
    document["entries"] = [{"id": f"fiction-{i}", "kind": "ingredient", "display": f"架空成分{i}",
                            "aliases": ["共通架空名"], "codes": {}, "forms": []} for i in range(5)]
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "共通架空名"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = view["items"][0]["text"]
    assert "辞書の一致候補・未確認: 5件" in text and "他2件は省略" in text
    assert text.count("\n　・") == 3 and "部分一致" not in text


def test_ambiguous_stem_candidates_are_labeled_by_match_method(led, tmp_path):
    import notify_views
    from copy import deepcopy
    document = deepcopy(DOCUMENT)
    document["entries"] = [{"id": f"fiction-{i}", "kind": "ingredient", "display": f"架空成分{i}",
                            "aliases": ["共通架空名"], "codes": {}, "forms": []} for i in range(2)]
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "共通架空名錠5mg"}]})
    cfg = _dictionary(tmp_path, led, document=document)
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = view["items"][0]["text"]
    assert "辞書の基底名による候補・未確認: 2件" in text
    assert "辞書の一致候補・未確認" not in text
    assert "架空成分0" in text and "架空成分1" in text

    dictionary, _ = drug_map.active_dictionary(led.db, cfg)
    ref = dictionary.resolve("共通架空名錠5mg")
    ref.update(dict_id=dictionary.dict_id, dict_sha256=dictionary.sha256,
               resolver_version=drug_map.RESOLVER_VERSION)
    ref["cands"][0]["method"] = "alias"
    mixed = notify_views._medication_reference_lines(ref, dictionary)
    assert "辞書の候補（照合方法混在）・未確認: 2件" in mixed
    assert "辞書の一致候補・未確認" not in mixed


@pytest.mark.parametrize("fault", ["display", "code", "kind", "method", "order", "shape", "resolve_error"])
def test_live_meds_revalidates_annotation_against_exact_dictionary(led, tmp_path, monkeypatch, fault):
    import notify_views
    from copy import deepcopy
    _seed_thread(led)
    _llm_extract(led, 101, {"meds": [{**MED, "name": "共通架空"}]})
    cfg = _dictionary(tmp_path, led)
    real = notify_views.structured_view.medication_entries
    def entries(*args, **kwargs):
        groups = deepcopy(real(*args, **kwargs))
        for group in groups:
            for _, ref in group:
                if fault in ("display", "code", "kind", "method"):
                    ref["cands"][0][fault] = "改変した架空値"
                elif fault == "order":
                    ref["cands"].reverse()
                elif fault == "shape":
                    ref["cands"] = {"invalid": True}
        return groups
    monkeypatch.setattr(notify_views.structured_view, "medication_entries", entries)
    if fault == "resolve_error":
        monkeypatch.setattr(drug_map.DrugMap, "resolve", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic")))
    before = led.db.total_changes
    view = notify_views.meds_view(led.db, 1, 101, cfg=cfg)
    text = json.dumps(view, ensure_ascii=False)
    assert "共通架空" in text and "辞書照合はまだありません" in text
    assert "架空成分" not in text and "改変した架空値" not in text and "fictional-v1@" not in text
    assert "辞書の一致候補" not in text
    assert led.db.total_changes == before
