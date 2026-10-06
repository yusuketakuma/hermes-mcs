"""Registry delta journal: per-card durability without a full rewrite (synthetic only)."""
import asyncio
import os
import shutil

import pytest
from hermes_plugin.mcs_delivery import registry
from test_lineworks_adapter import TOKEN, overflow_spec, world
from test_lineworks_adapter import _worker as lw_worker


def _count_rewrites(monkeypatch, reg):
    writes = []
    real = registry.paths.atomic_write

    def counted(path, raw, **kw):
        if path == reg._path:
            writes.append(path)
        return real(path, raw, **kw)

    monkeypatch.setattr(registry.paths, "atomic_write", counted)
    return writes


def test_durable_tokens_in_a_batch_append_and_survive_a_crash(tmp_path, monkeypatch):
    reg = registry.Registry(str(tmp_path))
    writes = _count_rewrites(monkeypatch, reg)
    with reg.batch():
        for i in range(20):
            reg.put_tokens({f"t{i}": {"action": "ack", "card_key": f"c{i}"}}, durable=True)
        assert writes == []
        crashed = registry.Registry(str(tmp_path))   # crash before the flush
        assert all(crashed.token(f"t{i}")["card_key"] == f"c{i}" for i in range(20))
    assert len(writes) == 1
    assert not os.path.exists(reg._delta_path)
    assert registry.Registry(str(tmp_path)).token("t19") is not None


def test_retirement_is_durable_before_flush_and_replays_in_order(tmp_path):
    reg = registry.Registry(str(tmp_path))
    reg.put_tokens({"old": {"action": "ack", "card_key": "k"}})
    with reg.batch():
        reg.retire_card_tokens("k")
        reg.put_tokens({"new": {"action": "ack", "card_key": "k"}}, durable=True)
        crashed = registry.Registry(str(tmp_path))
        assert crashed.token("old") is None
        assert crashed.token("new") is not None


def test_folded_delta_left_by_a_crash_is_not_replayed(tmp_path):
    reg = registry.Registry(str(tmp_path))
    with reg.batch():
        reg.put_tokens({"t": {"action": "ack", "card_key": "k", "message_id": "m"}}, durable=True)
        shutil.copy(reg._delta_path, str(tmp_path / "kept"))
        del reg._data["tokens"]["t"]    # later in-memory removal, folded at flush
    # crash between the main rewrite and the delta unlink
    shutil.copy(str(tmp_path / "kept"), reg._delta_path)
    assert registry.Registry(str(tmp_path)).token("t") is None


def test_torn_delta_tail_is_skipped_and_truncated_on_next_append(tmp_path):
    reg = registry.Registry(str(tmp_path))
    with reg.batch():
        reg.put_tokens({"a": {"action": "ack"}}, durable=True)
        with open(reg._delta_path, "ab") as handle:
            handle.write(b'{"op":"put","seq":99,"tokens":{"torn"')
        assert registry.Registry(str(tmp_path)).token("a") is not None
        reg.put_tokens({"b": {"action": "ack"}}, durable=True)
        crashed = registry.Registry(str(tmp_path))
        assert crashed.token("a") is not None and crashed.token("b") is not None


@pytest.mark.parametrize("line", [b"not-json\n", b"[]\n", b'{"op":"put","seq":1}\n',
                                  b'{"op":"drop","seq":1}\n',
                                  b'{"op":"put","seq":1,"tokens":{"x":1}}\n'])
def test_committed_malformed_delta_row_fails_closed(tmp_path, line):
    (tmp_path / "registry.json.delta").write_bytes(line)
    with pytest.raises(ValueError, match="registry_corrupt"):
        registry.Registry(str(tmp_path))


def test_lineworks_burst_rewrites_registry_once_and_retires_before_send(tmp_path, monkeypatch):
    w = world(tmp_path)
    worker = lw_worker(w)
    writes = _count_rewrites(monkeypatch, w.reg)
    base = overflow_spec()
    w.reg.put_tokens({TOKEN: {**w.reg.token(TOKEN), "card_key": "card-0"}})
    seen = []
    send = w.client.send_message

    def checked(content, **targets):
        # the durable state at HTTP time: the old pin is already gone
        seen.append(registry.Registry(w.dirs["state"], scope=w.settings).token(TOKEN))
        return send(content, **targets)

    monkeypatch.setattr(w.client, "send_message", checked)
    writes.clear()

    async def burst():
        with w.reg.batch():
            for i in range(5):
                spec = {**base, "card_key": f"card-{i}"}
                assert (await worker._perform({"spec": spec}))["result"] == "delivered"
            assert writes == []
    asyncio.run(burst())
    assert len(writes) == 1
    assert seen and seen[0] is None
    restored = registry.Registry(w.dirs["state"], scope=w.settings)
    assert any(c.get("card_key") == "card-4" for c in restored._data["tokens"].values())


def _older_release_rewrite(reg, **tokens):
    """An older release (no delta support) rewrites the main file after a
    rollback: it keeps unknown keys such as delta_seq and never folds or
    removes the delta."""
    import json
    with open(reg._path) as handle:
        data = json.load(handle)
    data["tokens"].update(tokens)
    with open(reg._path, "w") as handle:
        json.dump(data, handle)


def test_delta_left_over_a_rollback_is_not_replayed_after_reupgrade(tmp_path):
    reg = registry.Registry(str(tmp_path))
    reg.put_tokens({"old": {"action": "ack", "card_key": "k"}})
    main = open(reg._path, "rb").read()
    with reg.batch():
        reg.retire_card_tokens("k")
        reg.put_tokens({"gone": {"action": "ack", "card_key": "x"}}, durable=True)
        stale = open(reg._delta_path, "rb").read()
    # crash before the flush (the main file never saw the batch), then a
    # rollback whose older release mints a newer token for the same card
    with open(reg._path, "wb") as handle:
        handle.write(main)
    with open(reg._delta_path, "wb") as handle:
        handle.write(stale)
    assert registry.Registry(str(tmp_path)).token("gone") is not None
    _older_release_rewrite(reg, newer={"action": "ack", "card_key": "k"})
    upgraded = registry.Registry(str(tmp_path))
    assert upgraded.token("newer") is not None     # stale retire skipped
    assert upgraded.token("gone") is None          # stale put skipped
    # rows appended on top of the rewritten file still replay
    with upgraded.batch():
        upgraded.put_tokens({"fresh": {"action": "ack", "card_key": "k"}}, durable=True)
        crashed = registry.Registry(str(tmp_path))
        assert crashed.token("fresh") is not None
        assert crashed.token("newer") is not None and crashed.token("gone") is None


def test_unfolded_delta_on_an_unchanged_main_file_replays(tmp_path):
    reg = registry.Registry(str(tmp_path))
    reg.put_tokens({"old": {"action": "ack", "card_key": "k"}})
    with reg.batch():
        reg.retire_card_tokens("k")
        assert registry.Registry(str(tmp_path)).token("old") is None
