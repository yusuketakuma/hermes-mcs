"""Multi-page card renders read each page's footer once per render, and every
page shows the footer its own shown set produces (synthetic ledger only)."""
import json
from collections import Counter

import notify_render
from notify_testkit import _card, _dispatch, _intent, _msg, _patient, led

__all__ = ["led"]


def _multipage_card(led):
    _patient(led, 1)
    body = "記録の本文です。" * 40
    _msg(led, 100, 1, body=body)
    for mid in range(101, 114):
        _msg(led, mid, 1, parent=100, body=body)
    _dispatch(led, _intent(led, payload={"message_ids": list(range(100, 114))}))
    return _card(led)


def test_each_page_footer_is_read_once_and_matches_its_shown_set(led, monkeypatch):
    card = _multipage_card(led)
    real_footer = notify_render._footer
    calls = Counter()

    def counted(db, card_row, shown, generation, *, stamp_details):
        calls[tuple(shown)] += 1
        return real_footer(db, card_row, shown, generation, stamp_details=stamp_details)
    monkeypatch.setattr(notify_render, "_footer", counted)
    card["ui_state"] = json.dumps({"page": 0})
    first = notify_render.card_content(led.db, card)
    assert first["pages"] > 1
    assert calls and max(calls.values()) == 1        # repacking reuses a page's footer
    monkeypatch.undo()

    seen, fingerprints = [], []
    for page in range(first["pages"]):
        card["ui_state"] = json.dumps({"page": page})
        content = notify_render.card_content(led.db, card)
        footer, toggles = real_footer(
            led.db, card, content["shown"], notify_render.render_source_generation(
                card, content["source_fp"]), stamp_details=False)
        assert content["page"] == page and content["pages"] == first["pages"]
        assert (content["footer"], content["toggles"]) == (footer, toggles)
        seen.extend(content["shown"])
        fingerprints.append(notify_render.content_fingerprint(content))
        again = notify_render.card_content(led.db, card)
        assert notify_render.content_fingerprint(again) == fingerprints[-1]
    assert len(seen) == len(set(seen)) and set(seen) == set(range(100, 114))  # pages are disjoint
    assert len(set(fingerprints)) == first["pages"]
