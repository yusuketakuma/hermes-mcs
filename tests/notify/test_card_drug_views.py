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


def _click(led, spec, action, inputs=None, cfg=CFG):
    tok = _token_for(spec, action)
    req = {"version": 1, "op": "notification",
           "command_id": f"{tok}:{'c' * 16}", "actor": CLICKER,
           "token": tok, "origin": dict(ORIGIN, message_id="m-9")}
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
    assert view["title"].startswith("💊 ") and view["title"].endswith("この投稿の薬剤")
    assert view["head"][0].endswith("の投稿 — 1件")
    assert "有効化されていない" in view["head"][1]
    assert view["items"] == [{"project_id": 1,
                              "text": "・キラナ 5mg[開始]\n　→ 辞書候補なし"}]
    assert view["notes"][0].startswith("※ 辞書候補は名称の一致による参考情報")
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
    assert view["head"][1].startswith("辞書 fictional-v1@")
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
