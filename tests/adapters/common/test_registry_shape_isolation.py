"""Malformed synthetic persisted rows cannot starve healthy token/claim cleanup."""
import asyncio
import json

import pytest

from adapters.common.registry import Registry
from adapters.common.worker import DeliveryWorker


@pytest.mark.parametrize("action", [[], {}])
def test_unhashable_old_token_action_does_not_block_proven_edit_pruning(tmp_path, action):
    reg = Registry(str(tmp_path))
    base = {"card_key": "synthetic", "channel_id": "42", "message_id": None}
    reg._data["tokens"] = {
        "bad": {**base, "action": action, "at": 1},
        "old": {**base, "action": "ack", "at": 1},
        "new": {**base, "action": "ack", "at": 2},
        "other": {**base, "card_key": "other", "action": "ack", "at": 1}}
    reg.save()
    reg = Registry(str(tmp_path))
    reg.prune_card_tokens({"new": {**base, "action": "ack"}})
    assert reg.token("old") is None and reg.token("new") is not None
    assert reg.token("bad")["action"] == action and reg.token("other") is not None
    assert Registry(str(tmp_path)).token("old") is None


@pytest.mark.parametrize("bad", [{}, {"phase": []}, {"phase": {}}, {"phase": "unknown"},
                                  {"phase": "begin_sent", "spec": []}])
def test_damaged_orphan_claim_is_retained_without_blocking_next_claim(tmp_path, bad):
    reg = Registry(str(tmp_path))
    reg.claim("bad", bad)
    reg.claim("good", {"phase": "started"})
    reg = Registry(str(tmp_path))
    worker = object.__new__(DeliveryWorker)
    worker._reg = reg
    calls, logs = [], []
    worker._log = lambda event, **fields: logs.append((event, fields))

    async def step(claim, **kwargs):
        assert claim == {"phase": "started"} and kwargs == {"allow_parts": False}
        calls.append("healthy")

    async def drop(claim):
        raise AssertionError("damaged state must never be discarded or granted a resend")

    worker._step_claim, worker._drop_claim = step, drop
    worker._worker_id = "synthetic"
    asyncio.run(worker._settle_orphan_claims(set()))
    assert calls == ["healthy"] and reg.claimed("bad") == bad
    assert logs and logs[0][0] == "orphan_claim_error"
    assert logs[0][1]["delivery_id"] == "bad"


def test_orphan_cancellation_is_propagated_without_mutating_other_claims(tmp_path):
    reg = Registry(str(tmp_path))
    reg.claim("cancelled", {"phase": "started"})
    reg.claim("later", {"phase": "started"})
    worker = object.__new__(DeliveryWorker)
    worker._reg = reg
    calls = []

    async def settle(delivery_id, claim):
        calls.append(delivery_id)
        raise asyncio.CancelledError()

    worker._settle_orphan_claim = settle
    worker._log = lambda *_args, **_kwargs: pytest.fail("cancellation is not a row error")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker._settle_orphan_claims(set()))
    assert calls == ["cancelled"] and set(reg.claims()) == {"cancelled", "later"}


def test_repeated_registry_restart_expiry_keeps_published_unknown_protection(tmp_path):
    protected = {"phase": "result", "outcome": {"result": "unknown"}}
    for n in range(100):
        reg = Registry(str(tmp_path))
        reg.claim("published", protected)
        reg._data["dead"]["published"] = {"damaged_timestamp": n}
        reg._data["parts"]["published"] = "done"
        reg._data["pending_modals"][f"expired-{n}"] = {"expires": []}
        reg._data["pending_confirms"][f"expired-{n}"] = {"expires": {}}
        reg._data["followups"][f"expired-{n}"] = {"expires": "damaged"}
        reg.save()
        reg.expire(keep={"published"})
        assert reg.claimed("published") == protected and reg.is_dead("published")
        assert reg.parts_done("published")
        assert not any(reg._data[table] for table in ("pending_modals", "pending_confirms", "followups"))
    assert Registry(str(tmp_path)).claimed("published") == protected


@pytest.mark.parametrize("origin", [{"transport": "lineworks"},
                                    {"profile": "\ud800"}])
def test_malformed_legacy_origin_is_not_imported_or_blocks_owned_claim(tmp_path, origin):
    scope = {"transport": "lineworks", "team_id": "synthetic-team", "profile": "mcs", "application_id": "1", "channel_id": "42"}
    legacy = {"claims": {"bad": {"spec": {"delivery": origin}},
                         "owned": {"spec": {"delivery": scope}, "phase": "started"}}}
    path = tmp_path / "registry.json"
    raw = json.dumps(legacy).encode()
    path.write_bytes(raw)
    reg = Registry(str(tmp_path), scope=scope)
    assert reg.claimed("bad") is None and reg.claimed("owned") == legacy["claims"]["owned"]
    assert path.read_bytes() == raw
