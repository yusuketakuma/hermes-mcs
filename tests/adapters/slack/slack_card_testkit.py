"""Neutral v2 Slack card spec for tests — import-light on purpose (no
sys.path changes, no mcs imports), so the pinned-Hermes integration lane
can share it without shadowing Hermes modules."""


def _spec(text="合成の確認項目", label="本文表示"):
    return {
        "schema": "mcs-card-render/v2",
        "delivery_id": "00000000-0000-4000-8000-000000000001",
        "card_key": "synthetic-thread",
        "kind": "thread",
        "op": "create",
        "render_rev": 1,
        "source_generation": 1,
        "presentation_generation": 1,
        "ui_revision": 1,
        "delivery": {
            "profile": "cco",
            "transport": "slack",
            "application_id": "A_SYNTHETIC",
            "team_id": "T_SYNTHETIC",
            "guild_id": None,
            "channel_id": "C_SYNTHETIC",
            "route_epoch": 1,
            "correlation": "a" * 32,
            "intent_event_ids": [1],
        },
        "parts": {
            "containers": [
                {"type": "heading", "text": "合成カード"},
                {"type": "text", "text": text},
                {"type": "meta", "correlation": "a" * 32},
            ],
            "footer": [{"type": "text", "text": "合成フッター"}],
            "action_rows": [[
                {"id": "body", "ui": "button", "style": "secondary",
                 "label": label, "token": "b" * 32},
                {"id": "ack", "ui": "button", "style": "success",
                 "label": "確認", "token": "c" * 32},
            ]],
            "context": {"body": "非公開の合成本体"},
        },
    }
