"""Long-lived Slack workers retain a bounded member-name lookup cache."""
import asyncio
from types import SimpleNamespace

import pytest

from adapters.slack import delivery


@pytest.mark.parametrize("known", [True, False])
def test_member_cache_is_bounded_and_keeps_recent_hits(monkeypatch, known):
    monkeypatch.setattr(delivery, "NAME_CACHE_MAX", 16)
    monkeypatch.setattr(delivery.time, "monotonic", lambda: 1000)
    calls = 0

    async def users_info(*, user):
        nonlocal calls
        calls += 1
        if not known:
            raise RuntimeError("synthetic_lookup_failure")
        return {"user": {"profile": {"display_name": "Synthetic " + user}}}

    adapter = delivery.SlackCardAdapter(
        SimpleNamespace(client=SimpleNamespace(users_info=users_info)),
        team_id="T_SYNTHETIC", application_id="A_SYNTHETIC",
        channel_id="C_SYNTHETIC", profile=None, allowed_user_ids=set())

    async def scenario():
        hot = await adapter.display_name("U_HOT")
        for i in range(1024):
            await adapter.display_name(f"U_{i}")
            assert await adapter.display_name("U_HOT") == hot
            assert len(adapter._names) <= delivery.NAME_CACHE_MAX
        assert calls == 1025
        assert "U_HOT" in adapter._names and "U_0" not in adapter._names
        await adapter.display_name("U_0")
        assert calls == 1026 and len(adapter._names) == delivery.NAME_CACHE_MAX

    asyncio.run(scenario())
