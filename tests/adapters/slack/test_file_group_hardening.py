"""Synthetic private-stage and visible-group safety boundaries."""
import asyncio
import json

import pytest
import notify_cards
import notify_parts
from adapters.common import journal
from adapters.common import spec as spec_module
from adapters.slack import delivery
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import ROOT_TS, SLACK, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led

__all__ = ["led"]


def _world(store, tmp_path, monkeypatch, count=2):
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)
    _seed_thread(store, mids=(100,))
    for index in range(count):
        _attach(store, tmp_path, name=f"synthetic-{index}." + ("jpg" if index == 0 else "pdf"), blob=f"synthetic-{index}".encode())
    assert _dispatch(store, _intent(store), SLACK)["dispatched"]
    world = _mkworld(store)
    return world


def test_private_files_share_once_as_complete_group(led, tmp_path, monkeypatch):
    world = _world(led, tmp_path, monkeypatch)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == 2
    assert all("channel" not in call and "thread_ts" not in call
               and "initial_comment" not in call for call in world.client.upload_calls)
    groups = [kw for kind, kw in world.client.calls if kind == "update" and "file_ids" in kw]
    assert groups == [{"channel": SLACK["notify"]["slack"]["channel_id"], "ts": ROOT_TS,
                       "file_ids": list(world.client._files)}]
    spec = json.loads(_latest_render(led)["spec_json"])
    asyncio.run(world.worker._resume_parts(spec))
    assert len(world.client.upload_calls) == 2
    assert len([1 for kind, kw in world.client.calls if kind == "update" and "file_ids" in kw]) == 1
    card = led.db.execute("SELECT * FROM notification_cards").fetchone()
    identities = notify_parts._same_thread_delivered_parts(led.db, card)[2]
    assert {p[5] for proofs in identities.values() for p in proofs} == {"private_stage"}


@pytest.mark.parametrize("fault", ["id", "user", "size", "sha256", "name", "scope"])
def test_private_prior_needs_exact_owned_sealed_identity(led, tmp_path, monkeypatch, fault):
    world = _world(led, tmp_path, monkeypatch, count=1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    spec = json.loads(_latest_render(led)["spec_json"])
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    fid = next(iter(world.client._files))
    part["prior_remote_id"] = fid
    if fault == "scope":
        spec["delivery"]["channel_id"] = "C_OTHER"
    else:
        world.client._files[fid][fault] = 999 if fault == "size" else "foreign"
    result = asyncio.run(world.worker._verify_private_fid(spec, part, {}))
    assert result["result"] == "unknown"
    assert len(world.client.upload_calls) == 1


@pytest.mark.parametrize("mutation", ["flag", "kind", "shape", "order", "hash", "stage"])
def test_group_cannot_escape_sealed_contract(led, tmp_path, monkeypatch, mutation):
    world = _world(led, tmp_path, monkeypatch, count=1)
    spec = json.loads(_latest_render(led)["spec_json"])
    group = spec["parts"]["manifest"][-1]
    if mutation == "flag":
        spec["parts"]["slack"].pop("file_group")
    elif mutation == "kind":
        spec["kind"] = "digest"
    elif mutation == "shape":
        group["prior_remote_id"] = ROOT_TS
    elif mutation == "hash":
        group["sha256"] = "0" * 64
    elif mutation == "stage":
        spec["parts"]["manifest"][-2]["sha256"] = "0" * 64
    else:
        spec["parts"]["manifest"][-2:] = list(reversed(spec["parts"]["manifest"][-2:]))
    with pytest.raises(ValueError):
        spec_module.validate(spec)
    assert not world.client.upload_calls


def test_unknown_stage_does_not_attempt_group_or_repeat_upload(led, tmp_path, monkeypatch):
    world = _world(led, tmp_path, monkeypatch)
    world.client.upload_fail_at = {0}
    asyncio.run(_granted_card(world.worker, led, world.root))
    spec = json.loads(_latest_render(led)["spec_json"])
    asyncio.run(world.worker._resume_parts(spec))
    assert not any(kind == "update" and "file_ids" in kw for kind, kw in world.client.calls)
    rows = [r for rs in journal.scan(world.dirs["state"]).values() for r in rs
            if r.get("phase") == "started" and r.get("kind") == "attachment_part"]
    assert len(rows) == 2


def test_sdk_clone_checks_each_request_without_mutating_original():
    class Client:
        retry_handlers = ["original"]
        async def api_call(self, method):
            return method
    original = Client()
    clone = delivery.single_attempt(original)
    async def denied():
        return False
    async def run():
        token = delivery.RETRY_ALLOWED.set(denied)
        try:
            with pytest.raises(RuntimeError, match="send_authority_revoked"):
                await clone.api_call("files.completeUploadExternal")
            assert await original.api_call("unchanged") == "unchanged"
        finally:
            delivery.RETRY_ALLOWED.reset(token)
    asyncio.run(run())
    assert original.retry_handlers == ["original"] and clone.retry_handlers == []


def test_sdk_upload_stops_between_requests_when_authority_changes():
    calls = []
    class Client:
        retry_handlers = []
        async def api_call(self, method):
            calls.append(method)
            return {"ok": True}
        async def files_upload_v2(self):
            await self.api_call("files.getUploadURLExternal")
            await self.api_call("files.completeUploadExternal")
    async def allowed():
        return not calls
    async def run():
        token = delivery.RETRY_ALLOWED.set(allowed)
        try:
            with pytest.raises(RuntimeError, match="send_authority_revoked"):
                await delivery.single_attempt(Client()).files_upload_v2()
        finally:
            delivery.RETRY_ALLOWED.reset(token)
    asyncio.run(run())
    assert calls == ["files.getUploadURLExternal"]


def test_retry_gate_rejects_changed_sealed_file(led, tmp_path, monkeypatch):
    world = _world(led, tmp_path, monkeypatch, count=1)
    spec = json.loads(_latest_render(led)["spec_json"])
    # Use the runner's actual spool directory; no fabricated authorization.
    from pathlib import Path
    path = Path(world.worker._dirs["render"]) / (spec["delivery_id"] + ".json")
    async def run():
        assert await world.worker._retry_gate(spec)()
        changed = json.loads(path.read_text())
        changed["render_rev"] += 1
        path.write_text(json.dumps(changed))
        assert not await world.worker._retry_gate(spec)()
    asyncio.run(run())


def test_normal_update_reuses_private_stages_and_gc_preserves_kind(led, tmp_path, monkeypatch):
    import notify_spool
    world = _world(led, tmp_path, monkeypatch)
    asyncio.run(_granted_card(world.worker, led, world.root))
    first = _latest_render(led)
    witness = notify_parts.attachment_identity_witness(led.db, first["delivery_id"])
    assert witness and len(witness["proofs"]) == 2
    led.db.execute("UPDATE notification_renders SET spec_json=? WHERE delivery_id=?",
                   (json.dumps({"attachment_identity": witness}), first["delivery_id"]))
    card = led.db.execute("SELECT * FROM notification_cards").fetchone()
    proofs = notify_parts._same_thread_delivered_parts(led.db, card)[2]
    assert {p[5] for rows in proofs.values() for p in rows} == {"private_stage"}
    specs = []
    with led.db:
        notify_cards.issue_render(led.db, card["card_id"], SLACK, NOW + 1, specs, force=True)
    assert len(specs) == 1
    notify_spool.publish_specs(led.db, notify_spool.notify_dirs(str(world.root)), specs, NOW + 1)
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert len(world.client.upload_calls) == 2
    groups = [kw for kind, kw in world.client.calls if kind == "update" and "file_ids" in kw]
    assert len(groups) == 2 and groups[0] == groups[1]
    asyncio.run(world.worker._resume_parts(specs[0]))
    assert len([kw for kind, kw in world.client.calls if kind == "update" and "file_ids" in kw]) == 2
