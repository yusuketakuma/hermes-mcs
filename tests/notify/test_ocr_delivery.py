"""Current attachment OCR stays scoped to its post and behind existing body controls."""
import json

import pytest

import attachment_ocr as ocr
import notify_render
from mcs_requests import payload_hash
from notify_testkit import _attach, _msg, _patient, led

__all__ = ["led"]


def _save(led, aid, *, text="合成OCR本文 <@U123>\nOpenAI 完了", state="complete"):
    source = ocr._source(led.db, aid)
    pages = [{"page": 1, "engine": "yomitoku", "text": text, "text_hash": ocr._hash(text)}]
    doc = {"schema": ocr.SCHEMA, "runtime_contract": ocr.RUNTIME_CONTRACT,
           "attachment_id": aid, "input_sha256": source["sha256"], "input_bytes": source["bytes"],
           "state": state, "engine": "yomitoku", "engine_version": ocr.ENGINE_VERSION,
           "pages": pages, "text_hash": ocr._hash(json.dumps(pages, ensure_ascii=False, sort_keys=True))}
    with led.db:
        led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,meta,created_at) "
                       "VALUES(?,?,?,?,?,0)", (ocr.KIND, source["project_id"], source["message_id"],
                                               json.dumps(doc), json.dumps({"attachment_id": aid})))


def _card(transport="slack"):
    return {"kind": "thread", "project_id": 1, "root_message_id": 100,
            "layout": 4, "transport": transport, "anchor_key": "{}"}


def test_no_ocr_preserves_fingerprint_payload(led, monkeypatch):
    _patient(led)
    _msg(led, 100)
    _attach(led, 100)
    actual = notify_render.source_fingerprint(led.db, _card())
    monkeypatch.setattr(notify_render, "_ocr_generations", lambda *args: {})
    assert notify_render.source_fingerprint(led.db, _card()) == actual
    assert notify_render.attachment_ocr_records(led.db, 100) == []


@pytest.mark.parametrize("transport", ["slack", "discord", "lineworks"])
def test_current_ocr_body_is_separate_scoped_and_not_metadata(led, transport):
    _patient(led)
    _patient(led, 2)
    _msg(led, 100, body="原文をそのまま保持\n二行目")
    _msg(led, 200, pid=2, parent=100)
    own = _attach(led, 100, name="合成資料.pdf")
    other = _attach(led, 200)
    before = notify_render.source_fingerprint(led.db, _card(transport))
    _save(led, other, text="OTHER-PATIENT-OCR")
    assert notify_render.source_fingerprint(led.db, _card(transport)) == before
    _save(led, own)
    assert notify_render.source_fingerprint(led.db, _card(transport)) != before
    _, body = notify_render.card_body_text(
        led.db, _card(transport), {"shown": "[100,200]"}, max_chars=None,
        stamps=False, include_summary=False)
    assert "原文をそのまま保持\n二行目\n" + notify_render.SECTION_RULE in body
    assert "📎 添付の文字起こし\n合成資料.pdf · 完了 🖥 0/1 · ☁ 待ち\n1ページ\n合成OCR本文 <@U123>" in body
    assert "OTHER-PATIENT-OCR" not in body
    summary = notify_render._summary_block(led.db, 100)["text"]
    assert "📎 文字起こし 完了 1ページ" in summary
    assert "合成OCR本文" not in summary and "<@U123>" not in summary
    assert led.db.execute("SELECT body_text FROM messages WHERE message_id=100").fetchone()[0] == "原文をそのまま保持\n二行目"


@pytest.mark.parametrize(('analysis', 'label'), [
    ([None], '🖥 0/1 · ☁ 待ち'),
    ([(1, 'hash', 'local', True, False)], '🖥 ✓ · ☁ 0/1'),
    ([(2, 'hash', 'cloud', True, True)], '🖥 ✓ · ☁ ✓'),
    ([(1, 'hash', 'local', False, True)], '🖥 ✓ · ☁ 対象外'),
    ([(1, 'hash', 'local', None, False)], '🖥 ✓ · ☁ ?'),
])
def test_ocr_compact_stage_icons_do_not_conflate_local_and_cloud(analysis, label):
    assert notify_render._ocr_analysis_label({'analysis': analysis}) == label


def test_stale_and_corrupt_ocr_cannot_render_or_hold_generation(led):
    _patient(led)
    _msg(led, 100)
    aid = _attach(led, 100)
    baseline = notify_render.source_fingerprint(led.db, _card())
    _save(led, aid)
    first = notify_render.source_fingerprint(led.db, _card())
    _save(led, aid, text="改訂した合成OCR")
    assert notify_render.source_fingerprint(led.db, _card()) != first
    with led.db:
        led.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts)")
    assert notify_render.source_fingerprint(led.db, _card()) == baseline
    assert notify_render.original_post_text(led.db, 100, {"body_text": "本文", "body_state": "full"}) == "本文"
    _save(led, aid)
    with led.db:
        led.db.execute("UPDATE attachments SET sha256=? WHERE attachment_id=?", (payload_hash("changed"), aid))
    assert notify_render.source_fingerprint(led.db, _card()) == baseline


def test_legacy_null_body_state_keeps_valid_ocr_visible(led):
    _patient(led)
    _msg(led, 100)
    aid = _attach(led, 100)
    _save(led, aid)
    with led.db:
        led.db.execute('UPDATE messages SET body_state=NULL WHERE message_id=100')
    assert notify_render.attachment_ocr_records(led.db, 100)[0]['artifact_id']


def test_slack_ocr_refresh_reuses_exact_file_id_and_collapsed_original(led, tmp_path):
    import asyncio
    import notify_cards
    from adapters.slack.presentation import thread_part_blocks
    from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
    from slack_testkit import SLACK, _attach as slack_attach, _granted_card, _mkworld

    _seed_thread(led, mids=(100,))
    slack_attach(led, tmp_path, name="synthetic.pdf", blob=b"synthetic file")
    assert _dispatch(led, _intent(led, payload={"message_ids": [100]}), SLACK)["dispatched"]
    world = _mkworld(led)
    asyncio.run(_granted_card(world.worker, led, world.root))
    prior_fid = next(iter(world.client._files))
    uploads = len(world.client.upload_calls)
    aid = led.db.execute("SELECT attachment_id FROM attachments").fetchone()[0]
    _save(led, aid)
    assert notify_cards.sweep(led, SLACK, now=NOW + 1)["updated"] == 1
    spec = json.loads(_latest_render(led)["spec_json"])
    attachment = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    assert attachment["prior_remote_id"] == prior_fid
    original = spec["parts"]["slack"]["face_posts"][0]["original"]
    assert "📎 添付の文字起こし" in original
    assert not spec["parts"].get("thread_body_parts")
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == uploads
    assert set(world.client._files) == {prior_fid}
    # The same labelled text folds when the existing face budget sends it to the thread.
    blocks = thread_part_blocks({"parts": {"manifest": [
        {"kind": "body_part", "name": "m:100:header#1"}]}},
        {"name": "m:100#1"}, original)
    assert blocks[0]["default_collapsed"] is True
    assert "<@U123>" in str(blocks[0]["child_blocks"])
    assert all(b["text"]["type"] == "plain_text" for b in blocks[0]["child_blocks"])
