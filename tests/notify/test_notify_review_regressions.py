import json
import notify_cards
import notify_render
from test_notify_cards import (led, _seed_thread, _extract, _dispatch, _intent, _card, _latest_render, _begin, _receipt, _notif, _token_for, CFG, NOW)


__all__ = ["led"]

def test_changed_extraction_refreshes_display_and_source(led):
    _seed_thread(led)
    _extract(led, 100, {"summary": "合成の古い要約"}, kind="extract_llm")
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    attempt = _begin(led, render)
    _receipt(led, render, attempt["attempt_id"], message_id="mid-1")
    before = _card(led)
    _extract(led, 100, {"summary": "合成の訂正済み要約"}, kind="extract_llm")
    assert notify_render._source_fp(led.db, before) != before["source_fp"]
    notify_cards.sweep(led, CFG, now=NOW+1)
    after = _card(led)
    assert after["presentation_generation"] > before["presentation_generation"]
    assert after["source_generation"] > before["source_generation"]


def test_old_write_token_rejects_changed_extraction(led):
    _seed_thread(led)
    _extract(led, 100, {"summary": "合成の古い要約"}, kind="extract_llm")
    _dispatch(led, _intent(led))
    render = _latest_render(led)
    spec = json.loads(render["spec_json"])
    attempt = _begin(led, render)
    _receipt(led, render, attempt["attempt_id"], message_id="mid-1")
    _extract(led, 100, {"summary": "合成の訂正済み要約"}, kind="extract_llm")
    result = notify_cards.apply_notification(led, _notif(_token_for(spec, "assign")), CFG, now=NOW+1)
    assert result["outcome"] == "rejected" and result["error"] == "stale_source"
