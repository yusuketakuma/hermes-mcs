"""chat_format v1 drug controls: every medication post keeps a reachable
💊 on the surface that carries it — beside its face original, or through
the companion-thread view for posts left in the thread."""
from __future__ import annotations

import pytest

from notify_testkit import (
    MED, ORIGIN, _deliver, _dispatch, _drug_click, _intent, _llm_extract,
    _seed_thread, _spec, led, pinned_clock)
from notify_testkit import _button_ids as _ids

__all__ = ["led", "pinned_clock"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _rule_indexes(spec):
    """Container indexes of the post lines, in shown order."""
    return [i for i, c in enumerate(spec["parts"]["containers"])
            if c.get("rule") and c.get("type") == "text"]


def _post_actions(spec):
    """The face's per-post 💊 buttons: [(container index, token)]."""
    return [(a["at"], a["button"]["token"])
            for a in (spec["parts"].get("discord") or {}).get("post_actions") or []]


def _face_card(led, meds=((100, None), (101, MED))):
    """A delivered v1 card whose short originals both fit the face."""
    _seed_thread(led)
    for mid, med in meds:
        if med is not None:
            _llm_extract(led, mid, {"meds": [med]})
    _dispatch(led, _intent(led))
    _deliver(led)
    return _spec(led)


def test_face_posts_keep_their_pill_without_the_thread_flag(led):
    spec = _face_card(led)
    parts = spec["parts"]
    assert parts["chat_format_version"] == 1
    # nothing overflowed, so no thread body exists to hold the drug view
    assert "thread_drug_actions" not in parts
    own = parts["discord"]
    assert len(own["face_posts"]) == 2
    # only the medication post's 💊 sits beside its own face post
    at100, at101 = _rule_indexes(spec)
    assert _post_actions(spec) == [(at101, _post_actions(spec)[0][1])]
    token = _post_actions(spec)[0][1]
    # the card-level drug row stays on the face and still opens the view
    assert "meds" in _ids(spec)
    card_level = _drug_click(led, spec, "meds")
    assert card_level["outcome"] == "applied" and card_level["action"] == "list"
    per_post = _drug_click(led, spec, "meds", token=token)
    assert per_post["outcome"] == "applied"
    assert any("キラナ" in i["text"] for i in per_post["list"]["items"])


def test_mixed_posts_split_pills_between_face_and_thread(led):
    _seed_thread(led)
    with led.db:
        led.db.execute("UPDATE messages SET body_text=? WHERE message_id=101",
                       ("合成の長文" * 1000,))
    _llm_extract(led, 100, {"meds": [{**MED, "name": "面薬"}]})
    _llm_extract(led, 101, {"meds": [{**MED, "name": "糸薬"}]})
    _dispatch(led, _intent(led))
    _deliver(led)
    spec = _spec(led)
    parts = spec["parts"]
    assert parts["chat_format_version"] == 1
    assert parts["thread_drug_actions"] is True
    own = parts["discord"]
    at100, at101 = _rule_indexes(spec)
    # post 100's original is whole on the face; 101's stays in the thread
    assert [p["at"] for p in own["face_posts"]] == [at100]
    assert any("合成の長文" in p for p in parts["thread_body_parts"])
    actions = _post_actions(spec)
    assert [a for a, _t in actions] == [at100]
    # the card-level drug row rides the first thread body for the rest
    assert "meds" in _ids(spec)
    first = _drug_click(led, spec, "meds", token=actions[0][1])
    assert first["outcome"] == "applied"
    assert any("面薬" in i["text"] for i in first["list"]["items"])
    # the face 💊 view still reaches the thread post's medication
    newer = next(b["token"] for b in first["navigation"]
                 if b["label"] == "新しい投稿")
    second = _drug_click(led, spec, "meds", token=newer)
    assert any("糸薬" in i["text"] for i in second["list"]["items"])


def test_face_pill_rejects_a_foreign_origin(led):
    spec = _face_card(led)
    token = _post_actions(spec)[0][1]
    answer = _drug_click(led, spec, "meds", token=token,
                         origin=dict(ORIGIN, message_id="other-message"))
    assert answer["error"] == "origin_mismatch"
