"""Config change before chat_postMessage's request is not_sent, not unknown."""
import asyncio
from types import SimpleNamespace

import mcs_standalone.config as cfgmod
from adapters.slack import standalone as st


def test_config_change_before_post_is_not_sent(monkeypatch, tmp_path):
    calls = {"n": 0}

    def settings(root, t, require_interactive=False, target=None):
        calls["n"] += 1
        return {"channel_id": "C1", "team_id": "T1", "application_id": "A1",
                "v": calls["n"] == 1}

    monkeypatch.setattr(cfgmod, "connector_settings", settings)
    monkeypatch.setattr(cfgmod, "load_credentials", lambda r, t: {"bot_token": "x"})
    posted = []

    def fake_client(creds, verify):
        async def post(**kw):
            verify()  # Client._request -> session._verify() precedes HTTP
            posted.append(kw)

        async def close():
            pass
        return SimpleNamespace(chat_postMessage=post), SimpleNamespace(close=close)

    async def identity(client, s):
        return True

    monkeypatch.setattr(st, "_client", fake_client)
    monkeypatch.setattr(st, "_identity", identity)
    result = asyncio.run(st.send(str(tmp_path), "slack:C1", {"text": "hi"}))
    assert posted == []
    assert result == {"result": "not_sent",
                      "error_code": "slack_configuration_changed_restart_required"}
