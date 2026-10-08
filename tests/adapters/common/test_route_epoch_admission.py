"""Synthetic grant and part recovery must respect the currently published route epoch."""
import asyncio
import copy
import json
from pathlib import Path

import notify_cards
import notify_cmds
import pytest

from adapters.common import paths, registry, worker
from notify_testkit import CFG, NOW, _dispatch, _intent, _seed_thread, led

__all__ = ["led"]


def _config(transport, epoch=1):
    cfg = copy.deepcopy(CFG)
    cfg["notify"].update(interactive=transport, route_epoch=epoch)
    if transport == "slack":
        cfg["notify"]["slack"] = {"profile": "mcs", "application_id": "A_SYNTHETIC",
                                  "team_id": "T_SYNTHETIC", "channel_id": "C_SYNTHETIC"}
    elif transport == "lineworks":
        cfg["notify"]["lineworks"] = {"profile": "mcs", "application_id": "123456",
                                      "team_id": "synthetic-domain", "channel_id": "synthetic-room"}
    return cfg


def _worker(root, cfg, reg=None):
    platform = cfg["notify"]["interactive"]
    scope = notify_cards.delivery_scope(cfg)

    def directories(data):
        dirs = paths.notify_dirs(data)
        dirs.update(render=str(Path(data) / (platform + "_render")),
                    state=str(Path(data) / (platform + "_state")))
        return dirs

    base = worker.WorkspaceDeliveryWorker if platform in ("slack", "lineworks") else worker.DeliveryWorker

    class RecordingWorker(base):
        transport = platform
        _notify_dirs = staticmethod(directories)

        def _validate_spec(self, spec):
            if platform == "slack":
                from adapters.slack.cards import validate
                validate(spec)
            elif platform == "lineworks":
                from adapters.lineworks.cards import validate
                validate(spec)
            else:
                super()._validate_spec(spec)

        async def _perform(self, claim):
            self.writes.append(("card", claim["spec"]["delivery_id"]))
            if platform == "lineworks":
                from adapters.lineworks.cards import logical_message_id
                mid = logical_message_id(claim["spec"])
            else:
                mid = "1000.000001"
            return {"result": "delivered", "message_id": mid}

        async def _perform_part(self, claim, part, ctx):
            self.writes.append((part["kind"], part["part_id"]))
            return {"result": "delivered", "remote_id": (
                ctx["card_message_id"] if platform == "lineworks" and part["kind"] == "thread"
                else f"1000.{part['index']:06d}")}

    reg = reg or registry.Registry(directories(root)["state"], scope=scope)
    sender = RecordingWorker(bot=None, settings=scope, root=root, reg=reg,
                             worker_id=registry.new_worker_id(), log=lambda *a, **k: None)
    sender.writes = []
    sender.events = []
    sender._log = lambda event, **fields: sender.events.append((event, fields))
    return sender, reg


def _claim(sender, reg):
    claims = reg.claims()
    assert claims, sender.events
    return next(iter(claims.values()))


def _drain(led, root, cfg):
    result = {"errors": []}
    notify_cmds.drain_int_commands(led, result, cfg, root)
    assert result["errors"] == []


async def _grant(led, root, cfg, sender, reg):
    await sender.tick()
    claim = _claim(sender, reg)
    assert claim["phase"] == "begin_sent" and sender.writes == []
    _drain(led, root, cfg)
    result = paths.read_result(str(Path(root) / "cmd_results"), claim["begin_cid"])
    assert result and result.get("granted") is True, (result, sender.events)
    assert sender._verify_grant(claim, result), result
    return claim


def _setup(led, tmp_path, monkeypatch, transport):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    root = str(tmp_path / "data")
    cfg = _config(transport)
    notify_cards.ensure_dirs(root)
    notify_cards.publish_flags(cfg, root)
    _seed_thread(led)
    assert _dispatch(led, _intent(led), cfg)["dispatched"]
    sender, reg = _worker(root, cfg)
    render = led.db.execute("SELECT spec_json FROM notification_renders").fetchone()
    sender._validate_spec(json.loads(render["spec_json"]))
    assert sender.acquire_scope_lock()
    return root, cfg, sender, reg


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
@pytest.mark.parametrize("epoch", [2, None, True, "1"])
def test_granted_send_stays_held_until_its_exact_epoch_returns(
        led, tmp_path, monkeypatch, transport, epoch):
    root, cfg, sender, reg = _setup(led, tmp_path, monkeypatch, transport)

    async def scenario():
        claim = await _grant(led, root, cfg, sender, reg)
        attempt = claim["attempt_id"]
        flag_path = Path(root) / "flags/notify.json"
        flags = json.loads(flag_path.read_text())
        if epoch is None:
            flags.pop("route_epoch")
        else:
            flags["route_epoch"] = epoch
        flag_path.write_text(json.dumps(flags))
        for _ in range(2):
            await sender.tick()
        assert sender.writes == []
        assert claim["phase"] == "granted" and claim["attempt_id"] == attempt
        rows = led.db.execute("SELECT attempt_id,state FROM notification_delivery_attempts").fetchall()
        assert [(row["attempt_id"], row["state"]) for row in rows] == [(attempt, "granted")]
        notify_cards.publish_flags(cfg, root)
        await sender.tick()
        _drain(led, root, cfg)
        await sender.tick()
        assert sum(kind == "card" for kind, _ in sender.writes) == 1
        assert reg.claims() == {}
        assert led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                              (attempt,)).fetchone()["state"] == "delivered"

    try:
        asyncio.run(scenario())
    finally:
        sender.release_scope_lock()


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
@pytest.mark.parametrize("result", ["delivered", "unknown"])
def test_epoch_change_after_wire_preserves_the_factual_result(
        led, tmp_path, monkeypatch, transport, result):
    root, cfg, sender, reg = _setup(led, tmp_path, monkeypatch, transport)
    newer = _config(transport, 2)

    async def past_wire(claim):
        sender.writes.append(("card", claim["spec"]["delivery_id"]))
        notify_cards.publish_flags(newer, root)
        return ({"result": "delivered", "message_id": "1000.000001"}
                if result == "delivered" else {"result": "unknown", "error_code": "synthetic_timeout"})

    monkeypatch.setattr(sender, "_perform", past_wire)

    async def scenario():
        claim = await _grant(led, root, cfg, sender, reg)
        attempt = claim["attempt_id"]
        await sender.tick()
        _drain(led, root, newer)
        await sender.tick()
        assert len(sender.writes) == 1
        assert led.db.execute("SELECT state FROM notification_delivery_attempts WHERE attempt_id=?",
                              (attempt,)).fetchone()["state"] == result
        assert led.db.execute("SELECT state FROM notification_renders WHERE delivery_id=?",
                              (claim["spec"]["delivery_id"],)).fetchone()["state"] == result

    try:
        asyncio.run(scenario())
    finally:
        sender.release_scope_lock()


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
def test_clean_unsent_grant_restart_can_recover_into_the_new_epoch(
        led, tmp_path, monkeypatch, transport):
    root, cfg, sender, reg = _setup(led, tmp_path, monkeypatch, transport)
    newer = _config(transport, 2)

    async def scenario():
        await _grant(led, root, cfg, sender, reg)
        notify_cards.publish_flags(newer, root)
        await sender.tick()
        assert sender.writes == []
        reg.save()
        sender.release_scope_lock()
        replacement, fresh_reg = _worker(root, newer)
        assert replacement.acquire_scope_lock()
        try:
            stats = await replacement.reconcile()
            assert stats["not_sent"] == 1 and stats["unknown"] == 0
            _drain(led, root, newer)
            for _ in range(3):
                await replacement.tick()
                _drain(led, root, newer)
            assert sum(kind == "card" for kind, _ in replacement.writes) == 1
            assert fresh_reg.claims() == {}
            latest = led.db.execute("SELECT route_epoch,state FROM notification_renders "
                                    "ORDER BY render_rev DESC LIMIT 1").fetchone()
            assert (latest["route_epoch"], latest["state"]) == (2, "delivered")
        finally:
            replacement.release_scope_lock()

    try:
        asyncio.run(scenario())
    finally:
        sender.release_scope_lock()


@pytest.mark.parametrize("transport", ["discord", "slack", "lineworks"])
def test_epoch_change_between_parts_holds_only_the_unsent_remainder(
        led, tmp_path, monkeypatch, transport):
    root, cfg, sender, reg = _setup(led, tmp_path, monkeypatch, transport)
    attempt_part = sender._attempt_part

    async def stop_after_thread(claim, part, ctx):
        await attempt_part(claim, part, ctx)
        if part["kind"] == "thread":
            notify_cards.publish_flags(_config(transport, 2), root)

    monkeypatch.setattr(sender, "_attempt_part", stop_after_thread)

    async def scenario():
        await _grant(led, root, cfg, sender, reg)
        await sender.tick()
        assert [kind for kind, _ in sender.writes] == ["card", "thread"]
        _drain(led, root, cfg)
        await sender.tick()
        assert [kind for kind, _ in sender.writes] == ["card", "thread"]
        latest = led.db.execute("SELECT delivery_id,state FROM notification_renders "
                                "ORDER BY render_rev DESC LIMIT 1").fetchone()
        assert latest["state"] == "delivered" and not reg.parts_done(latest["delivery_id"])
        assert led.db.execute("SELECT state FROM notification_render_parts WHERE delivery_id=? "
                              "AND kind='body_part' LIMIT 1", (latest["delivery_id"],)).fetchone()[0] == "pending"
        notify_cards.publish_flags(cfg, root)
        await sender.tick()
        _drain(led, root, cfg)
        await sender.tick()
        assert reg.parts_done(latest["delivery_id"])
        assert sum(kind == "card" for kind, _ in sender.writes) == 1
        assert sum(kind == "thread" for kind, _ in sender.writes) == 1
        assert led.db.execute("SELECT parts_state FROM notification_renders WHERE delivery_id=?",
                              (latest["delivery_id"],)).fetchone()[0] == "complete"

    try:
        asyncio.run(scenario())
    finally:
        sender.release_scope_lock()


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_publisher_repairs_raw_epoch_types_before_releasing_a_hold(tmp_path, value):
    root = str(tmp_path)
    cfg = _config("discord")
    notify_cards.ensure_dirs(root)
    assert notify_cards.publish_flags(cfg, root)
    flag_path = tmp_path / "flags/notify.json"
    flags = json.loads(flag_path.read_text())
    flags["route_epoch"] = value
    flag_path.write_text(json.dumps(flags))
    assert notify_cards.publish_flags(cfg, root)
    restored = json.loads(flag_path.read_text())
    assert type(restored["route_epoch"]) is int and restored["route_epoch"] == 1
    assert not notify_cards.publish_flags(cfg, root)


def test_publisher_keeps_normal_flags_unchanged_for_at_only_drift(tmp_path, monkeypatch):
    cfg = _config("discord")
    notify_cards.ensure_dirs(str(tmp_path))
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    assert notify_cards.publish_flags(cfg, str(tmp_path))
    flag_path = tmp_path / "flags/notify.json"
    before = flag_path.read_bytes()
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW + 60)
    assert not notify_cards.publish_flags(cfg, str(tmp_path))
    assert flag_path.read_bytes() == before


@pytest.mark.parametrize("raw", ["{\"route_epoch\":NaN}", "[" * 20000 + "]" * 20000])
def test_publisher_replaces_unserializable_or_deep_raw_flags(tmp_path, raw):
    cfg = _config("discord")
    notify_cards.ensure_dirs(str(tmp_path))
    (tmp_path / "flags/notify.json").write_text(raw)
    assert notify_cards.publish_flags(cfg, str(tmp_path))
    assert json.loads((tmp_path / "flags/notify.json").read_text())["route_epoch"] == 1


def test_bad_config_boolean_keeps_the_existing_epoch_normalization(tmp_path):
    cfg = _config("discord", True)
    notify_cards.ensure_dirs(str(tmp_path))
    assert notify_cards.publish_flags(cfg, str(tmp_path))
    flags = json.loads((tmp_path / "flags/notify.json").read_text())
    assert type(flags["route_epoch"]) is int and flags["route_epoch"] == 1
