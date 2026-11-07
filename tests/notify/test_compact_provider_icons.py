"""Reuse existing provider images with concise status and no unused inventory."""
from adapters.slack.presentation import _provider_context, _PROVIDERS, render_parts

_JEV_URL = next(url for key, _name, url in _PROVIDERS if key == "jev")


def _v3_parts(states, context_progress, text):
    header = {"type": "text", "provider_scope": "header", "provider_states": states,
              "visible_providers": ["jev", "local"], "context_progress": context_progress,
              "text": text}
    return {"provider_states_version": 4, "processing_display_version": 3,
            "containers": [header], "footer": [], "action_rows": []}


def test_local_indicator_is_compact_without_changing_legacy_text():
    states = {"anthropic": "unused", "openai": "unused", "jev": "unused", "local": "done"}
    compact = _provider_context(states, visible=["local"], compact=True)
    legacy = _provider_context(states, visible=["local"])
    assert compact["elements"] == [{"type": "plain_text", "text": "💻 ✓"}]
    assert legacy["elements"] == [{"type": "plain_text", "text": "💻 ✓ 実行済み"}]


def test_compact_icons_keep_alt_text_and_do_not_claim_running():
    states = {"anthropic": "unused", "openai": "done", "jev": "pending", "local": "unused"}
    block = _provider_context(states, "1投稿 · 解析中 · 採用結果の処理順: OpenAI → Jev",
                              ["anthropic", "openai", "jev", "local"], ["openai", "jev"], compact=True)
    assert block["elements"][0]["text"] == "1投稿 · ◐ 未完了"
    images = [e for e in block["elements"] if e["type"] == "image"]
    assert [e["image_url"] for e in images] == [row[2] for row in _PROVIDERS if row[0] in ("openai", "jev")]
    assert [e["alt_text"] for e in images] == ["OpenAI 実行済み", "Jev 処理待ち"]
    assert [e["text"] for e in block["elements"] if e["type"] == "plain_text"] == ["1投稿 · ◐ 未完了", "✓", "…"]


def test_compact_text_and_discord_share_status_without_mutating_legacy_spec():
    import copy
    import notify_render
    import processing_route
    from adapters.discord.cards import _zones

    states = {"anthropic": "unused", "openai": "done", "jev": "pending", "local": "unknown"}
    visible = ["openai", "jev", "local"]
    header = {"type": "text", "provider_scope": "header", "provider_states": states,
              "visible_providers": visible, "provider_sequence": ["openai", "jev"],
              "text": "1投稿 · 解析中 · 採用結果の処理順: OpenAI → Jev · "
                      + processing_route.provider_text(states, visible, ["openai", "jev"])}
    parts = {"containers": [header], "footer": [], "processing_display_version": 1}
    before = copy.deepcopy(parts)
    expected = "1投稿 · ◐ 未完了 · OpenAI ✓  Jev …"
    assert notify_render.display_text(parts) == expected
    assert _zones({"kind": "thread", "parts": parts}, str)[0] == [[expected]]
    assert parts == before
    legacy = {k: v for k, v in parts.items() if k != "processing_display_version"}
    assert notify_render.display_text(legacy) == header["text"]


def test_manifest_binds_compact_marker_without_changing_legacy_hash():
    import hashlib
    import pytest
    from adapters.common.envelopes import canonical
    from adapters.common.spec import _check_part_payloads

    visible = {"containers": [], "footer": [], "action_rows": []}
    old = {"kind": "card", "sha256": hashlib.sha256(canonical(visible)).hexdigest()}
    _check_part_payloads(visible, [old], None)
    marked = {**visible, "processing_display_version": 1}
    new = {"kind": "card", "sha256": hashlib.sha256(canonical(marked)).hexdigest()}
    _check_part_payloads(marked, [new], None)
    with pytest.raises(ValueError, match="card_part_sha256"):
        _check_part_payloads(visible, [new], None)
    with pytest.raises(ValueError, match="card_part_sha256"):
        _check_part_payloads(marked, [old], None)


def test_aggregate_used_is_not_shown_as_completed():
    import processing_route
    states = {"anthropic": "unknown", "openai": "used", "jev": "used", "local": "unknown"}
    visible = ["openai", "jev", "local"]
    header = {"provider_states": states, "visible_providers": visible, "provider_sequence": None,
              "text": "解析完了 · 採用結果の処理順: 不明 · " + processing_route.provider_text(states, visible)}
    assert "OpenAI ●" in processing_route.compact_header(header)
    block = _provider_context(states, "✓ 完了", visible, compact=True)
    marks = [element["text"] for element in block["elements"] if element["type"] == "plain_text"]
    assert marks == ["✓ 完了", "●", "●"]


def test_v3_header_shows_jev_logo_and_frozen_context_as_text():
    states = {"anthropic": "unknown", "openai": "unknown", "jev": "done", "local": "done"}
    progress = {"proof_version": 1, "percent": None, "stage": "unknown", "held": False,
                "source_sig": None, "observed_at": 1.0}
    parts = _v3_parts(states, progress, "解析完了 · 👤 — · Jev 実行済み · 💻 実行済み")
    blocks = render_parts(parts, card_kind="thread")
    assert [block["type"] for block in blocks] == ["context"]
    elements = blocks[0]["elements"]
    assert elements[0] == {"type": "plain_text", "text": "✓ 完了 · 👤 —"}
    images = [e for e in elements if e["type"] == "image"]
    assert [e["image_url"] for e in images] == [_JEV_URL]
    assert images[0]["alt_text"] == "Jev 実行済み"
    assert [e["text"] for e in elements if e["type"] == "plain_text"] == ["✓ 完了 · 👤 —", "✓", "💻 ✓"]
    assert "Jev" not in " ".join(e.get("text", "") for e in elements if e["type"] == "plain_text")
    assert not any("Anthropic" in e.get("alt_text", "") or "OpenAI" in e.get("alt_text", "")
                   for e in elements)


def test_v3_header_keeps_frozen_percent_hold_and_filters_states():
    states = {"anthropic": "unknown", "openai": "unknown", "jev": "attention", "local": "unknown"}
    progress = {"proof_version": 1, "percent": 45, "stage": "sources", "held": True,
                "source_sig": "a" * 64, "observed_at": 1.0}
    parts = _v3_parts(states, progress, "解析中 · 👤 45% 保留 · Jev 要確認 · 💻 不明")
    elements = render_parts(parts, card_kind="thread")[0]["elements"]
    assert elements[0]["text"] == "◐ 未完了 · 👤 45% 保留"
    images = [e for e in elements if e["type"] == "image"]
    assert [e["image_url"] for e in images] == [_JEV_URL]
    assert images[0]["alt_text"] == "Jev 要確認"
    assert [e["text"] for e in elements if e["type"] == "plain_text"] == ["◐ 未完了 · 👤 45% 保留", "!"]


def test_v3_header_matches_text_compact_header_except_the_jev_name():
    import processing_route

    states = {"anthropic": "unknown", "openai": "unknown", "jev": "partial", "local": "unverified"}
    progress = {"proof_version": 1, "percent": 10, "stage": "sources", "held": False,
                "source_sig": "a" * 64, "observed_at": 1.0}
    header = {"type": "text", "provider_scope": "header", "provider_states": states,
              "visible_providers": ["jev", "local"], "context_progress": progress,
              "text": "状況不明 · 👤 10% · Jev 一部実行・一部省略 · 💻 状況未確認"}
    blocks = render_parts(_v3_parts(states, progress, header["text"]), card_kind="thread")
    elements = blocks[0]["elements"]
    text = processing_route.compact_header(header)
    assert text == "? 状況未取得 · 👤 10% · Jev ◐  💻 ?"
    assert [e["text"] for e in elements if e["type"] == "plain_text"] == [
        "? 状況未取得 · 👤 10%", "◐", "💻 ?"]
    assert elements[0]["text"] + " · " + "  ".join(
        ["Jev " + elements[2]["text"], elements[3]["text"]]) == text
    assert elements[1] == {"type": "image", "image_url": _JEV_URL, "alt_text": "Jev 一部実行・一部省略"}


def test_partial_mark_matches_for_slack_and_text():
    import processing_route
    states = {'anthropic': 'unused', 'openai': 'partial', 'jev': 'done', 'local': 'unknown'}
    visible = ['openai', 'jev', 'local']
    header = {'provider_states': states, 'visible_providers': visible, 'provider_sequence': None,
              'text': '解析中 · 採用結果の処理順: 不明 · ' + processing_route.provider_text(states, visible)}
    block = _provider_context(states, '解析中', visible, compact=True)
    assert 'OpenAI ◐' in processing_route.compact_header(header)
    assert '◐' in [item.get('text') for item in block['elements']]
