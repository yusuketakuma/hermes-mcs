"""Slack card-face originals retain proven thread actions and lossless fallback."""
import asyncio
import json

import pytest

from notify_testkit import _dispatch, _intent, _seed_thread
from slack_testkit import SLACK, _granted_card, _mkworld, isolated_slack_ledger as led

__all__ = ["led"]


@pytest.mark.face_posts
def test_card_face_carries_the_fold_and_the_thread_stays_empty(led):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_text='合成の元投稿' WHERE message_id=100")
    assert _dispatch(led, _intent(led), SLACK)["dispatched"]
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    card = next(kw for kind, kw in w.client.calls if kind == "create" and "thread_ts" not in kw)
    folds = [b for b in card["blocks"] if b.get("type") == "container"]
    assert [b["title"]["text"] for b in folds] == ["原文（6字）", "原文（2字）"]
    assert folds[0]["child_blocks"][0]["text"]["text"] == "合成の元投稿"
    # the fallback text may say 「原文・要約処理待ち」; the fold title and body must not leak into it
    assert "原文（" not in str(card.get("text") or "") and "合成の元投稿" not in str(card.get("text") or "")
    assert w.client.thread_posts == []


@pytest.mark.face_posts
@pytest.mark.parametrize("initial_state", ["full", "deleted"])
def test_face_post_growing_past_limit_keeps_its_full_thread_body(led, monkeypatch, initial_state):
    import notify_cards
    import notify_spool
    import present_slack
    from notify_testkit import NOW, _latest_render
    monkeypatch.setattr(present_slack, "FACE_ORIGINAL_MAX", 20)
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_state=? WHERE message_id=100", (initial_state,))
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=101", ("OVERSIZED_REPLY" * 5,))
    _dispatch(led, _intent(led), SLACK)
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    assert w.client.thread_posts
    before = len(w.client.thread_posts)
    issued = []
    original = "GROWN_ORIGINAL" * 5
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,content_hash=?,body_state='full' WHERE message_id=100", (original, "c" * 64))
        notify_cards.issue_render(led.db, 1, SLACK, NOW + 1, issued, force=True)
    current = json.loads(_latest_render(led)["spec_json"])
    body = "".join(current["parts"]["thread_body_parts"][int(p["part_id"].split(":")[1]) - 1]
                   for p in current["parts"]["manifest"]
                   if p["kind"] == "body_part" and p["name"].startswith("m:100#"))
    assert body == original
    assert not current["delivery"]["intent_event_ids"]
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(w.root)), issued, NOW + 1)
    asyncio.run(_granted_card(w.worker, led, w.root))
    added = w.client.thread_posts[before:]
    assert any(p["text"] == original for p in added)
    assert not any(p.get("reply_broadcast") for p in added)
    asyncio.run(_granted_card(w.worker, led, w.root))
    assert len(w.client.thread_posts) == before + len(added)


@pytest.mark.parametrize("fault", [None, "card_missing", "card_unknown", "thread_unknown", "wrong_root", "scope"])
def test_face_drug_thread_binding_requires_proven_same_card_and_thread(led, fault):
    from adapters.common import registry, envelopes
    from adapters.common.spec import token_map
    from adapters.slack.delivery import DeliveryWorker
    from notify_testkit import _latest_render
    from slack_testkit import SCOPE
    from notify_testkit import _llm_extract
    _seed_thread(led)
    _llm_extract(led, 100, {"meds": [{"name": "合成薬", "dose": "5mg", "action": "start",
                                   "subject": "patient", "status": "current", "negated": False,
                                   "unverified": False, "evidence": "合成の薬剤記録"}]})
    _dispatch(led, _intent(led), SLACK)
    w = _mkworld(led)
    asyncio.run(_granted_card(w.worker, led, w.root))
    spec = json.loads(_latest_render(led)["spec_json"])
    assert not spec["parts"]["thread_body_parts"]
    tokens = [t for t, c in token_map(spec).items() if c["action"] in ("meds", "drugsearch")]
    assert tokens
    root = "1790000000.000001"
    # a face with no reply under it: proven card, no thread for the answers
    assert all(w.reg.token(t).get("verified_card_message_id") == root for t in tokens)
    assert all(w.reg.token(t).get("verified_thread_id") is None for t in tokens)
    restored = registry.Registry(w.dirs["state"], scope=SCOPE)
    for token in tokens:
        restored._data["tokens"][token].pop("verified_thread_id", None)
        restored._data["tokens"][token].pop("verified_card_message_id", None)
    records = {aid: [dict(r) for r in rows] for aid, rows in w.worker._jview.refresh().items()}
    thread_aid = envelopes.part_attempt_id(spec["delivery_id"], "thread")
    for aid, rows in records.items():
        for row in rows:
            if row.get("phase") != "result":
                continue
            if aid == thread_aid and fault in ("thread_unknown", "wrong_root"):
                row.update(result="unknown" if fault == "thread_unknown" else "delivered", remote_id="1790000000.999999")
            elif not row.get("part_id") and fault in ("card_missing", "card_unknown"):
                row["result"] = "unknown"
                if fault == "card_missing":
                    row["phase"] = "started"
    if fault == "scope":
        spec["delivery"]["channel_id"] = "C_OTHER"
    worker = DeliveryWorker(sender=w.sender, settings=SCOPE, root=str(w.root), reg=restored,
                            worker_id=registry.new_worker_id(), log=lambda *_a, **_k: None)
    asyncio.run(worker._resume_parts(spec, records))
    assert all(restored.token(t).get("verified_card_message_id") == (root if fault is None else None) for t in tokens)
    assert all(restored.token(t).get("verified_thread_id") is None for t in tokens)
    assert not w.client.thread_posts
