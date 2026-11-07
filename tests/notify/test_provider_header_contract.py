"""Synthetic v4 header metadata and legacy compatibility."""
import copy

import pytest

from adapters.slack.cards import validate
from slack_card_testkit import _spec
from notify_testkit import led as led

LABELS = {"processing": "解析中", "preparing": "結果を準備中", "complete": "解析完了",
          "attention": "解析未完了・要確認", "unknown": "状況不明"}
NAMES = {"anthropic": "Anthropic", "openai": "OpenAI", "jev": "Jev", "local": "💻"}


def _v4(status="processing", sequence=None):
    spec = _spec()
    states = dict.fromkeys(NAMES, "unknown")
    visible = list(NAMES)
    ordered = list(sequence or [])
    remaining = [key for key in visible if key not in ordered]
    chunks = [f"{NAMES[key]} 不明" for key in ordered]
    if ordered and remaining:
        chunks.append("その他（順序不明）")
    chunks.extend(f"{NAMES[key]} 不明" for key in remaining)
    fallback = " · ".join(chunks)
    order = "採用結果の処理順: " + (" → ".join(NAMES[key] for key in sequence) if sequence else "不明") + " · "
    spec["parts"]["provider_states_version"] = 4
    spec["parts"]["containers"] = [
        {"type": "heading", "text": "合成カード"},
        {"type": "text", "provider_scope": "header", "provider_states": states.copy(),
         "visible_providers": visible, "processing_status": status, "provider_sequence": sequence,
         "text": "合成情報 · " + LABELS[status] + " · " + order + fallback},
        {"type": "text", "rule": True, "text": "合成投稿", "provider_states": states.copy()},
        {"type": "text", "text": "合成要約"},
    ]
    return spec


@pytest.mark.parametrize("status", list(LABELS))
@pytest.mark.parametrize("sequence", [None, ["jev", "openai"], ["local", "jev"]])
def test_v4_header_accepts_valid_status_and_proven_sequence(status, sequence):
    validate(_v4(status, sequence))


@pytest.mark.parametrize("fault", ["status", "status_text", "sequence_text", "hidden", "duplicate",
                                   "unknown", "length", "missing", "post", "footer", "old_version"])
def test_v4_header_rejects_inconsistent_metadata(fault):
    spec = copy.deepcopy(_v4(sequence=["jev", "openai"]))
    header = spec["parts"]["containers"][1]
    if fault == "status":
        header["processing_status"] = "invented"
    elif fault == "status_text":
        header["text"] = header["text"].replace("解析中", "解析完了")
    elif fault == "sequence_text":
        header["text"] = header["text"].replace("Jev → OpenAI", "OpenAI → Jev")
    elif fault == "hidden":
        header["visible_providers"].remove("openai")
    elif fault == "duplicate":
        header["provider_sequence"] = ["jev", "jev"]
    elif fault == "unknown":
        header["provider_sequence"] = ["jev", "other"]
    elif fault == "length":
        header["provider_sequence"] = ["jev"]
    elif fault == "missing":
        del header["provider_sequence"]
    elif fault == "post":
        spec["parts"]["containers"][2]["processing_status"] = "complete"
    elif fault == "footer":
        spec["parts"]["footer"][0]["provider_sequence"] = None
    else:
        spec["parts"]["provider_states_version"] = 3
    with pytest.raises(ValueError, match="bad_provider_states"):
        validate(spec)


@pytest.mark.parametrize("sequence,visible,posts,expected", [
    (["jev", "openai"], ("openai", "jev", "local"), (100,), ["jev", "openai"]),
    (["local", "jev"], ("jev", "local"), (100,), ["local", "jev"]),
    (["jev", "openai"], ("jev", "local"), (100,), None),
    (["jev", "openai"], ("openai", "jev", "local"), (100, 101), ["jev", "openai"]),
    (None, ("openai", "jev", "local"), (100,), None),
])
def test_writer_displays_only_proven_visible_single_post_sequence(led, monkeypatch, sequence, visible, posts, expected):
    import processing_route
    from notify_testkit import _dispatch, _intent, _seed_thread, _spec as written_spec
    from slack_testkit import SLACK

    monkeypatch.setattr(processing_route, "visible_providers", lambda: visible)

    def evidence(*args, sequence_out=None, **kwargs):
        if sequence_out is not None:
            sequence_out["sequence"] = sequence
        return dict.fromkeys(processing_route.PROVIDERS, "unknown")

    monkeypatch.setattr(processing_route, "provider_states", evidence)
    _seed_thread(led, mids=posts)
    _dispatch(led, _intent(led, payload={"message_ids": list(posts)}), SLACK)
    import notify_render
    from notify_testkit import _card
    raw = notify_render.card_content(led.db, _card(led), cfg=SLACK, visible_providers=visible)
    assert raw["containers"][1]["provider_sequence"] == expected
    spec = written_spec(led)
    validate(spec)
    header = spec["parts"]["containers"][1]
    assert spec["parts"]["processing_display_version"] == 3
    assert header["provider_sequence"] is None
    assert header["visible_providers"] == ["jev", "local"]
    assert header["context_progress"]["stage"] == "inputs"
    assert header["context_progress"]["percent"] == 0
    assert "👤 0%" in header["text"]
    assert "採用結果の処理順" not in header["text"]
    assert "OpenAI" not in header["text"] and "Anthropic" not in header["text"]
