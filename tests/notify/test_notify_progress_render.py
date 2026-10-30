"""共有通知の処理中表示は本文を公開せず表示世代のみを更新する。"""
import json

import pytest

import extraction_progress
import notify_cards
import notify_render
from inference_dependency_loss import source_binding
from notify_testkit import _card, _deliver, _dispatch, _intent, _seed_thread, led

__all__ = ["led"]


def test_processing_complete_attention_updates_both_face_and_body(led, monkeypatch):
    _seed_thread(led)
    assert _dispatch(led, _intent(led))["dispatched"]
    _deliver(led)
    card = _card(led)
    progress = {"state": "processing", "completed": 1, "total": 3}
    monkeypatch.setattr(extraction_progress, "message_progress", lambda *a, **k: dict(progress))
    led.db.execute("PRAGMA query_only=ON")
    original = notify_render.card_content(led.db, card)
    assert "解析更新中" not in notify_render.display_text(original)
    assert original["containers"][1]["processing_status"] == "unknown"
    processing = notify_render.card_content(led.db, card, cfg={})
    [header] = [c for c in processing["containers"] if c.get("provider_scope") == "header"]
    assert header["processing_status"] == "processing" and "解析中" in header["text"]
    post_indices = [i for i, c in enumerate(processing["containers"]) if c.get("rule")]
    assert len(post_indices) == len(processing["shown"]) == 2
    for index in post_indices:
        assert "解析更新中" not in processing["containers"][index + 1]["text"]
    body = notify_render.card_body_text(led.db, card, {"shown": json.dumps([100])}, cfg={})[1]
    assert "解析更新中" not in body    # Layout 2 keeps thread body behaviour unchanged.
    assert processing["source_fp"] == original["source_fp"]
    assert processing["source_fp"] == card["source_fp"]
    drift = notify_cards._generation_drift(card, processing)
    assert "source_generation" not in drift and "presentation_generation" in drift
    progress.update(state="complete", completed=3)
    complete = notify_render.card_content(led.db, card, cfg={})
    assert "解析中" not in notify_render.display_text(complete)
    assert complete["containers"][1]["processing_status"] == "complete"
    assert "解析完了" in complete["containers"][1]["text"]
    assert notify_render.content_fingerprint(complete) != notify_render.content_fingerprint(processing)
    progress.update(state="attention", completed=1)
    attention = notify_render.card_content(led.db, card, cfg={})
    [header] = [c for c in attention["containers"] if c.get("provider_scope") == "header"]
    assert header["processing_status"] == "attention"
    assert "解析未完了・要確認" in header["text"]
    for index in [i for i, c in enumerate(attention["containers"]) if c.get("rule")]:
        assert "解析要確認" not in attention["containers"][index + 1]["text"]
    assert "解析中" not in notify_render.display_text(attention)


def test_single_message_progress_strips_payload_and_uses_explicit_model(led, monkeypatch):
    import extract_llm
    _seed_thread(led)
    seen = []

    def read(db, row, *, model):
        seen.append((row["message_id"], model))
        return {"state": "processing", "completed": 0, "total": 2,
                "body": "CHECKPOINT-CANARY", "facts": ["FACT-CANARY"]}

    monkeypatch.setattr(extract_llm, "progress_for_message", read)
    row = led.db.execute("SELECT * FROM messages WHERE message_id=100").fetchone()
    led.db.execute("PRAGMA query_only=ON")
    result = extraction_progress.message_progress(led.db, row, cfg={"local_llm": {"model": "synthetic-model"}})
    assert result == {"state": "processing", "completed": 0, "total": 2}
    assert seen == [(100, "synthetic-model")]
    assert "CANARY" not in str(result)


def test_progress_off_visible_page_still_changes_presentation_fp(led, monkeypatch):
    _seed_thread(led, mids=tuple(range(100, 113)))
    assert _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 113))}))["dispatched"]
    card = _card(led)
    counts = {mid: 0 for mid in range(100, 113)}
    monkeypatch.setattr(extraction_progress, "message_progress", lambda db, row, **k: {
        "state": "processing", "completed": counts[row["message_id"]], "total": 3})
    before = notify_render.card_content(led.db, card, cfg={})
    hidden = next(mid for mid in counts if mid not in before["shown"])
    counts[hidden] = 1
    after = notify_render.card_content(led.db, card, cfg={})
    assert before["containers"] == after["containers"]
    assert before["source_fp"] == after["source_fp"]
    assert notify_render.content_fingerprint(before) != notify_render.content_fingerprint(after)


def test_real_current_checkpoint_updates_shared_summary_without_partial_facts(led):
    import extract_llm
    _seed_thread(led)
    body = "完全合成自由文 SOURCE-CANARY。\n" * 200
    with led.db:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=100", (body,))
    row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=100").fetchone())
    row["_source_binding"] = source_binding(led.db, row["project_id"], row["message_id"])
    model = "synthetic-shared-progress"
    row["_target"] = ("", model)
    context = extract_llm._thread_context(led.db, row)
    roots = extract_llm.plan_chunks(body, extract_llm._CHUNK_SIZE)
    assert len(roots) > 1
    assert extract_llm._persist_chunks(led, row, {0: {}}, context)
    led.db.execute("PRAGMA query_only=ON")
    cfg = {"local_llm": {"model": model}}
    block = notify_render._summary_block(led.db, 100, cfg=cfg)
    assert f"解析更新中（{len(roots)}区間中1区間完了）" in block["text"]
    assert notify_render.progress_state(led.db, 100, cfg) == {
        "state": "processing", "completed": 1, "total": len(roots)}
    assert "SOURCE-CANARY" not in block["text"]
    changed_model = {"local_llm": {"model": "different-synthetic"}}
    changed = notify_render.progress_state(led.db, 100, changed_model)
    assert changed["completed"] == 0 and changed["total"] == len(roots)


def test_card_reads_each_current_message_once_and_reuses_even_unknown_progress(led, monkeypatch):
    _seed_thread(led, mids=(100, 101, 102))
    assert _dispatch(led, _intent(led, payload={"message_ids": [100, 101, 102]}))["dispatched"]
    card = _card(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=102")
    calls = []

    def read(db, row, **kwargs):
        calls.append(row["message_id"])
        return {"state": "processing", "completed": 1, "total": 3} if row["message_id"] == 100 else None

    monkeypatch.setattr(extraction_progress, "message_progress", read)
    led.db.execute("PRAGMA query_only=ON")
    content = notify_render.card_content(led.db, card, cfg={})
    assert calls == [100, 101]
    [header] = [c for c in content["containers"] if c.get("provider_scope") == "header"]
    assert header["processing_status"] == "unknown" and "状況不明" in header["text"]
    assert "解析更新中" not in notify_render.display_text(content)
    assert content["progress_fp"] == notify_render.payload_hash([
        (100, {"state": "processing", "completed": 1, "total": 3}), (101, None)])
    calls.clear()
    notify_render.card_content(led.db, card)
    assert calls == []  # cfg-unknown path keeps its prior display and reader boundary


@pytest.mark.parametrize("platform", ["slack", "discord", "lineworks"])
def test_thread_body_includes_summary_and_attachment_caption_keeps_source(led, tmp_path, monkeypatch, platform):
    import hashlib
    from notify_testkit import CFG, _latest_render
    from slack_testkit import SLACK
    from notify_lineworks_testkit import LINEWORKS
    from notify_testkit import _attach
    _seed_thread(led, mids=(100,))
    raw = "原文の要約という単語を削除しません。"
    with led.db:
        led.db.execute("UPDATE messages SET body_text=?,organization='合成所属' WHERE message_id=100", (raw,))
    monkeypatch.setattr(notify_render, "structured_block", lambda *_args, **_kw: {
        "type": "text", "text": "📋 要約\nGENERATED-SUMMARY-CANARY"})
    monkeypatch.setattr(notify_render, "progress_state", lambda *_args: {
        "state": "processing", "completed": 1, "total": 3})
    file = tmp_path / "synthetic.pdf"
    file.write_bytes(b"synthetic attachment")
    _attach(led, 100, local_path=str(file), sha256=hashlib.sha256(file.read_bytes()).hexdigest(), nbytes=file.stat().st_size)
    cfg = {"slack": SLACK, "discord": CFG, "lineworks": LINEWORKS}[platform]
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), cfg)
    spec = json.loads(_latest_render(led)["spec_json"])
    face = notify_render.display_text(spec["parts"])
    assert "GENERATED-SUMMARY-CANARY" in face
    [header] = [c for c in spec["parts"]["containers"] if c.get("provider_scope") == "header"]
    assert header["processing_status"] == "processing" and "解析中" in header["text"]
    assert "解析更新中" not in face
    body = "".join(spec["parts"]["thread_body_parts"])
    caption = next(part["caption"] for part in spec["parts"]["manifest"] if part["kind"] == "attachment_part")
    assert "合成所属" in caption
    if platform == "slack":
        # Slack keeps summary, stamps and the folded original on the card face;
        # only the attachment goes to the thread.
        assert body == ""
        post = spec["parts"]["slack"]["face_posts"][0]
        assert post["original"] == raw and post["stamps"] == ["スタンプ 未取得"]
        body = "\n".join(post["stamps"]) + "\n" + raw
    else:
        assert raw in body and "📄 本文" not in body
        assert body.index("GENERATED-SUMMARY-CANARY") < body.index("スタンプ 未取得") < body.index(raw)
    assert "解析更新中" not in body
    assert "📋 要約" not in body
    if platform == "lineworks":
        assert "GENERATED-SUMMARY-CANARY" in caption
    else:
        assert "スタンプ 未取得" in body
        assert "GENERATED-SUMMARY-CANARY" not in caption and "解析更新中" not in caption
        assert raw not in caption
    private = notify_render.card_body_text(led.db, _card(led), {"shown": "[100]"}, cfg=cfg)[1]
    assert "GENERATED-SUMMARY-CANARY" in private and "解析更新中" not in private


@pytest.mark.parametrize(("values", "expected"), [
    ([], "unknown"),
    ([None], "unknown"),
    ([{"state": "complete", "completed": 1, "total": 1}], "complete"),
    ([{"state": "processing", "completed": 0, "total": 0}], "processing"),
    ([{"state": "processing", "completed": 0, "total": 1}], "processing"),
    ([{"state": "processing", "completed": 1, "total": 1}], "preparing"),
    ([{"state": "attention", "completed": 1, "total": 1}], "attention"),
    ([{"state": "attention", "completed": 1, "total": 1},
      {"state": "processing", "completed": 1, "total": 1}], "attention"),
    ([{"state": "complete", "completed": 1, "total": 1}, None], "unknown"),
    ([{"state": "processing", "completed": 0, "total": 1},
      {"state": "processing", "completed": 1, "total": 1}], "processing"),
    ([{"state": "processing", "completed": 1, "total": 1},
      {"state": "complete", "completed": 1, "total": 1}], "preparing"),
    ([{"state": "complete", "completed": 0, "total": 1}], "unknown"),
    ([{"state": "complete", "completed": True, "total": 1}], "unknown"),
])
def test_page_processing_status_never_promotes_unfinished_or_unknown(values, expected):
    assert notify_render._page_processing_status(values) == expected


@pytest.mark.parametrize("layout", [1, 2, 3])
def test_header_status_uses_only_visible_nondeleted_posts(led, monkeypatch, layout):
    mids = (100, 101, 102, 103, 104)
    _seed_thread(led, mids=mids)
    assert _dispatch(led, _intent(led, payload={"message_ids": list(mids)}))["dispatched"]
    card = {**_card(led), "layout": layout}
    with led.db:
        led.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=104")
    progress = {100: {"state": "complete", "completed": 1, "total": 1},
                101: {"state": "complete", "completed": 1, "total": 1},
                102: {"state": "attention", "completed": 1, "total": 1},
                103: {"state": "processing", "completed": 0, "total": 1}}
    monkeypatch.setattr(notify_render, "PAGE_THREAD", 2)
    monkeypatch.setattr(notify_render, "progress_state", lambda db, mid, cfg: progress[mid])
    monkeypatch.setattr(notify_render, "structured_block", lambda *_args, **_kwargs:
                        {"type": "text", "text": "📋 要約\n・合成の臨床要約"})
    for page, expected, ids in [(0, "complete", [100, 101]),
                                (1, "attention", [102, 103]),
                                (2, "unknown", [104])]:
        content = notify_render.card_content(led.db, {**card, "ui_state": json.dumps({"page": page})}, cfg={})
        header = content["containers"][1]
        assert content["shown"] == ids
        assert header["processing_status"] == expected
        assert header["provider_sequence"] is None
        assert notify_render.PROCESSING_STATUS_LABELS[expected] in header["text"]
        assert "解析更新中" not in notify_render.display_text(content)
        if page < 2 and layout >= 3:
            assert "合成の臨床要約" in notify_render.display_text(content)
