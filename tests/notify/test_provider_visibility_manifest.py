"""Provider visibility stays bound to each synthetic notification manifest."""
import json
from types import SimpleNamespace

import pytest

import notify_parts


class _Rows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _DB:
    def __init__(self, previous):
        self.previous = previous

    def execute(self, sql, _args):
        if "notification_renders" in sql:
            return _Rows((json.dumps(self.previous),))
        if "notification_view_manifests" in sql:
            return _Rows((json.dumps([100]),))
        raise AssertionError("unexpected synthetic query")


def _parts(version, visible=None, status="processing", sequence=None):
    header = {"type": "text", "provider_scope": "header", "text": "合成"}
    if visible is not None:
        header["visible_providers"] = visible
    if version == 4:
        header.update(processing_status=status, provider_sequence=sequence)
    return {"provider_states_version": version, "manifest_id": "synthetic",
            "containers": [header], "footer": [], "action_rows": []}


@pytest.mark.parametrize("old_version,old_visible,new_visible,reuse", [
    (2, None, ["anthropic", "openai", "jev", "local"], False),
    (3, ["anthropic", "jev", "local"], ["openai", "jev", "local"], False),
    (3, ["anthropic", "jev", "local"], ["anthropic", "jev", "local"], True),
    (3, ["local", "jev"], ["jev", "local"], False),
])
def test_lineworks_reuses_card_only_for_same_visibility(
        monkeypatch, old_version, old_visible, new_visible, reuse):
    old_parts = _parts(old_version, old_visible)
    old_parts["containers"][0]["text"] = "以前の合成カード"
    new_parts = _parts(3, new_visible)
    previous = {"parts": old_parts, "source_generation": "synthetic",
                "ui_revision": 1, "delivery": {}}
    spec = {"op": "update", "parts": new_parts, "source_generation": "synthetic",
            "ui_revision": 1, "delivery": {}}
    card = {"transport": "lineworks", "card_id": 1, "message_id": "synthetic-card"}
    monkeypatch.setattr(notify_parts, "scope_matches", lambda *_: True)
    monkeypatch.setattr(notify_parts, "stored_scope", lambda *_: {})
    parts, hold = notify_parts.build_manifest(_DB(previous), card, spec, {"shown": [100]}, False)
    assert hold is None
    assert bool(parts["manifest"][0].get("prior_remote_id")) is reuse
    assert parts["containers"][0]["text"] == ("以前の合成カード" if reuse else "合成")


@pytest.mark.parametrize("old_version,old_visible,old_status,old_sequence,new_visible,new_status,new_sequence,reuse", [
    (2, None, None, None, ["jev", "local"], "processing", None, False),
    (3, ["jev", "local"], None, None, ["jev", "local"], "processing", None, False),
    (4, ["jev", "local"], "processing", None, ["jev", "local"], "processing", None, True),
    (4, ["jev", "local"], "complete", ["local", "jev"], ["jev", "local"], "complete", ["local", "jev"], True),
    (4, ["jev", "local"], "processing", None, ["jev", "local"], "complete", None, False),
    (4, ["jev", "local"], "complete", ["local", "jev"], ["jev", "local"], "complete", ["jev", "local"], False),
    (4, ["openai", "jev", "local"], "processing", None, ["jev", "local"], "processing", None, False),
])
def test_lineworks_v4_reuse_requires_matching_header(
        monkeypatch, old_version, old_visible, old_status, old_sequence,
        new_visible, new_status, new_sequence, reuse):
    old_parts = _parts(old_version, old_visible, old_status, old_sequence)
    new_parts = _parts(4, new_visible, new_status, new_sequence)
    previous = {"parts": old_parts, "source_generation": "synthetic", "ui_revision": 1, "delivery": {}}
    spec = {"op": "update", "parts": new_parts, "source_generation": "synthetic", "ui_revision": 1, "delivery": {}}
    card = {"transport": "lineworks", "card_id": 1, "message_id": "synthetic-card"}
    monkeypatch.setattr(notify_parts, "scope_matches", lambda *_: True)
    monkeypatch.setattr(notify_parts, "stored_scope", lambda *_: {})
    parts, hold = notify_parts.build_manifest(_DB(previous), card, spec, {"shown": [100]}, False)
    assert hold is None
    assert bool(parts["manifest"][0].get("prior_remote_id")) is reuse
    assert parts["containers"][0] == (old_parts if reuse else new_parts)["containers"][0]


@pytest.mark.parametrize("version,visible,expected", [
    (4, ["openai", "jev", "local"], ("openai", "jev", "local")),
    (3, ["openai", "jev", "local"], ("openai", "jev", "local")),
    (2, None, ("anthropic", "openai", "jev", "local")),
    (1, None, ("anthropic", "openai", "jev", "local")),
])
def test_body_parts_reuse_the_manifest_visibility(monkeypatch, version, visible, expected):
    parts = _parts(version, visible)
    spec = {"op": "create", "parts": parts, "delivery": {"intent_event_ids": []}}
    card = {"transport": "slack", "card_id": 1, "card_key": "synthetic", "kind": "thread", "layout": 2}
    calls = []
    monkeypatch.setattr(notify_parts, "_thread_plan_ids", lambda *_: [100, 101])
    monkeypatch.setattr(notify_parts, "_body_groups", lambda *_: [("m:100", [100]), ("m:101", [101])])
    monkeypatch.setattr(notify_parts, "_plan_attachments", lambda *_: [])
    monkeypatch.setattr(notify_parts, "_attachment_captions", lambda *_: {})
    monkeypatch.setattr(notify_parts, "presenter", lambda *_: SimpleNamespace(EDITABLE=True))
    monkeypatch.setattr(notify_parts, "card_body_text",
                        lambda *a, **kw: (calls.append(kw["visible_providers"]) or "合成", "合成本文"))
    result, hold = notify_parts.build_manifest(None, card, spec, {"shown": [100, 101]}, True)
    assert hold is None and calls == [expected, expected]
    assert result["thread_body_parts"] == ["合成本文", "合成本文"]


from notify_testkit import led as led  # noqa: E402


@pytest.mark.parametrize("legacy", [True, False])
def test_lineworks_writer_rebuilds_card_when_registered_keys_change(led, monkeypatch, legacy):
    import notify_cards
    import processing_route
    from adapters.lineworks.cards import validate
    from notify_lineworks_testkit import LINEWORKS, _deliver
    from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread

    registered = {"MCS_ANTHROPIC_API_KEY"}
    monkeypatch.setattr(processing_route.mcs_util, "private_env_problem",
                        lambda key, root=None: None if key in registered else "missing")
    original_content = notify_cards.card_content

    if legacy:
        def legacy_content(*args, **kwargs):
            kwargs["visible_providers"] = ("anthropic", "openai", "jev", "local")
            content = original_content(*args, **kwargs)
            for item in content["containers"]:
                item.pop("visible_providers", None)
                item.pop("processing_status", None)
                item.pop("provider_sequence", None)
            return content

        monkeypatch.setattr(notify_cards, "card_content", legacy_content)
    _seed_thread(led, mids=(100,))
    _dispatch(led, _intent(led, payload={"message_ids": [100]}), LINEWORKS)
    first = _latest_render(led)
    original = json.loads(first["spec_json"])
    assert original["parts"]["provider_states_version"] == (2 if legacy else 4)
    assert validate(original) is original
    _deliver(led, first)

    monkeypatch.setattr(notify_cards, "card_content", original_content)
    registered.clear()
    registered.add("MCS_OPENAI_API_KEY")
    with led.db:
        specs = []
        assert notify_cards.issue_render(led.db, 1, LINEWORKS, NOW + 1, specs, force=True)
    current = specs[0]
    assert current["parts"]["provider_states_version"] == 4
    header = next(item for item in current["parts"]["containers"]
                  if item.get("provider_scope") == "header")
    assert header["visible_providers"] == ["openai", "jev", "local"]
    assert "Anthropic" not in header["text"] and "OpenAI" in header["text"]
    assert "prior_remote_id" not in current["parts"]["manifest"][0]
    assert validate(current) is current
