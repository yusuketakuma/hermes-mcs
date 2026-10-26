"""Discord thread cards show each post's stamp rows on the card face (stamps
only — no folded original) and drop them, not the card, over budget
(synthetic fixtures only)."""
import sys

import pytest

from adapters.common import spec as spec_mod
from adapters.common.spec import MAX_COMPONENTS, MAX_TEXT, MAX_TOTAL_TEXT, validate
from adapters.discord import cards
from discord_delivery_testkit import _spec
from discord_testkit import _fake_discord

STAMP = "スタンプ 👀2 合成 太郎・合成 花子 · 観測 10-01 10:00"


@pytest.fixture(autouse=True)
def _stub_sdk(monkeypatch):
    monkeypatch.setitem(sys.modules, "discord", _fake_discord())


def _card(n, stamps=(STAMP,)):
    spec = _spec([])
    spec["parts"].pop("manifest")  # Supported legacy pre-plan card.
    containers, posts = [], []
    for i in range(n):
        containers.append({"type": "text", "rule": True, "text": f"10-01 09:{i:02d} 合成職員{i}"})
        containers.append({"type": "text", "text": f"・合成の要約{i}"})
        posts.append({"at": 2 * i, "stamps": list(stamps)})
    spec["parts"]["containers"] = containers
    spec["parts"]["discord"] = {"face_posts": posts}
    return spec


def _view_texts(view):
    (container,) = view.items
    return [t.content for c in container.children
            for t in (c, *getattr(c, "children", ())) if hasattr(t, "content")]


def _components(view):
    (container,) = view.items
    return 1 + sum(1 + len(getattr(c, "children", ())) for c in container.children)


def test_v2_face_shows_each_posts_stamp_rows_under_its_summary():
    spec = validate(_card(2))
    text = "\n".join(_view_texts(cards.build_view(spec)))
    assert text.count(f"-# {STAMP}") == 2
    assert (text.index("合成の要約0") < text.index(f"-# {STAMP}")
            < text.index("合成職員1") < text.rindex(f"-# {STAMP}"))


def test_many_posts_keep_their_rows_in_one_zone():
    # eight rule zones overflow the Container's 10 children: the zones
    # collapse first and the stamp rows stay
    spec = validate(_card(spec_mod.MAX_FACE_POSTS))
    view = cards.build_view(spec)
    assert "\n".join(_view_texts(view)).count(f"-# {STAMP}") == spec_mod.MAX_FACE_POSTS
    assert len(view.items[0].children) <= cards._CONTAINER_MAX


def test_legacy_embed_carries_the_rows():
    payload = cards.message_payload(validate(_card(2)))
    assert payload["embed"].description.count(f"-# {STAMP}") == 2


def test_card_without_face_posts_is_unchanged():
    spec = _card(2)
    plain = _card(2)
    del plain["parts"]["discord"]
    face = _view_texts(cards.build_view(validate(spec)))
    assert [t.replace(f"\n-# {STAMP}", "") for t in face] == \
        _view_texts(cards.build_view(validate(plain)))
    assert cards.message_payload(plain)["embed"].description == \
        cards.message_payload(spec)["embed"].description.replace(f"\n-# {STAMP}", "")


def test_worst_case_rows_over_budget_fall_back_without_rejecting():
    # eight posts, four maximal rows each: far over 4000 characters
    spec = validate(_card(spec_mod.MAX_FACE_POSTS, stamps=["合" * MAX_TEXT] * 4))
    view = cards.build_view(spec)
    texts = _view_texts(view)
    assert not any("合合合" in t for t in texts)
    assert "合成職員7" in "\n".join(texts) and "合成の要約7" in "\n".join(texts)
    assert sum(map(len, texts)) <= MAX_TOTAL_TEXT
    assert _components(view) <= MAX_COMPONENTS
    description = cards.message_payload(spec)["embed"].description
    assert "合合合" not in description and "合成の要約7" in description


def test_rows_that_fit_one_level_only_are_kept_where_they_fit():
    # rows sized so the face just fits the text budget: none are dropped
    spec = validate(_card(2, stamps=["合" * 1500]))
    texts = _view_texts(cards.build_view(spec))
    assert sum(t.count("合" * 1500) for t in texts) == 2
    assert sum(map(len, texts)) <= MAX_TOTAL_TEXT


@pytest.mark.parametrize("post", [
    {"at": 0, "stamps": [STAMP], "original": "合成の原文"},   # no fold on Discord
    {"at": 0, "stamps": []},                                 # nothing to show
    {"at": 0, "stamps": [STAMP] * 5},
    {"at": 0, "stamps": [""]},
    {"at": 0, "stamps": ["合" * (MAX_TEXT + 1)]},
    {"at": 1, "stamps": [STAMP]},                            # not a post line
    {"at": True, "stamps": [STAMP]},
    {"at": 0, "stamps": STAMP},
    {"at": 0},
])
def test_validator_rejects_bad_discord_face_posts(post):
    spec = _card(1)
    spec["parts"]["discord"]["face_posts"] = [post]
    with pytest.raises(ValueError):
        validate(spec)


def test_validator_rejects_duplicates_overflow_and_non_thread_cards():
    spec = _card(1)
    spec["parts"]["discord"]["face_posts"] *= 2
    with pytest.raises(ValueError):
        validate(spec)
    spec = _card(spec_mod.MAX_FACE_POSTS + 1)
    with pytest.raises(ValueError):
        validate(spec)
    spec = _card(1)
    spec["kind"] = "signal"
    with pytest.raises(ValueError):
        validate(spec)
    spec = _card(1)
    spec["parts"]["discord"]["face_posts"] = []
    with pytest.raises(ValueError):
        validate(spec)


def test_slack_face_post_shape_is_unchanged():
    parts = _card(1)["parts"]
    spec = {"kind": "thread"}
    spec_mod._validate_face_posts(spec, parts, [{"at": 0, "stamps": [], "original": "合成"}], "slack")
    for bad in ({"at": 0, "stamps": [STAMP]}, {"at": 0, "stamps": [STAMP], "original": ""}):
        with pytest.raises(ValueError):
            spec_mod._validate_face_posts(spec, parts, [bad], "slack")
