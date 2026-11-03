"""Synthetic upload provenance survives receipt publication failure without a resend."""
import asyncio
import copy
import hashlib
import json

import pytest
from adapters.common import envelopes, journal
from adapters.common.spec import post_scoped
import notify_cmds
from notify_testkit import NOW, _dispatch, _intent, _latest_render, _seed_thread
from slack_testkit import ROOT_TS, SLACK, _attach, _granted_card, _mkworld
from slack_testkit import isolated_slack_ledger as led


__all__ = ["led"]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    import notify_cards
    monkeypatch.setattr(notify_cards.time, "time", lambda: NOW)


def _rows(world):
    return [row for rows in journal.scan(world.dirs["state"]).values() for row in rows
            if row.get("phase") == "result" and row.get("kind") == "attachment_part"]


def _world(store, tmp_path, legacy=None):
    if legacy is not None:
        import notify_parts
        build = notify_parts.build_manifest
        def legacy_manifest(*args, **kwargs):
            parts, hold = build(*args, **kwargs)
            parts["manifest"] = [p for p in parts["manifest"] if p["kind"] != "file_group"]
            parts.get("slack", {}).pop("file_group", None)
            return parts, hold
        legacy.setattr(notify_parts, "build_manifest", legacy_manifest)
    _seed_thread(store)
    aid, _, blob = _attach(store, tmp_path)
    assert _dispatch(store, _intent(store), SLACK)["dispatched"]
    return _mkworld(store), aid, blob


def test_upload_origin_is_bound_to_sealed_part_and_remote_identity(led, tmp_path, monkeypatch):
    world, aid, blob = _world(led, tmp_path, legacy=monkeypatch)
    asyncio.run(_granted_card(world.worker, led, world.root))
    row, = _rows(world)
    assert row["attachment_origin"] == {
        "method": "upload", "attachment_id": aid,
        "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
        "thread_id": ROOT_TS}
    env = row["receipt_envelope"]
    spec = json.loads(_latest_render(led)["spec_json"])
    assert row["remote_id"] == env["remote_id"]
    assert row["part_id"] == env["part_id"] == f"attach:{aid:04d}"
    assert row["delivery_id"] == env["delivery_id"] == spec["delivery_id"]
    assert env["payload_hash"] == envelopes.payload_hash(spec)
    assert env["team_id"] == spec["delivery"]["team_id"]
    assert "path" not in row["attachment_origin"] and "caption" not in row["attachment_origin"]
    uploads = len(world.client.upload_calls)
    update = copy.deepcopy(spec)
    update.update(op="update", delivery_id="00000000-0000-4000-8000-000000000222")
    part = next(p for p in update["parts"]["manifest"] if p["kind"] == "attachment_part")
    part["prior_remote_id"] = row["remote_id"]
    asyncio.run(world.worker._attempt_part({"spec": update,
        "payload_hash": envelopes.payload_hash(update)}, part,
        {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    latest = _rows(world)[-1]
    assert latest["attachment_origin"]["method"] == "remote_match"
    assert latest["remote_id"] == row["remote_id"] and len(world.client.upload_calls) == uploads
    assert row["attachment_origin"]["method"] == "upload"  # old fact is not overwritten


def test_upload_result_is_durable_before_receipt_and_resume_does_not_upload(led, tmp_path, monkeypatch):
    world, _, _ = _world(led, tmp_path)
    publish = envelopes.publish_command
    fail = [True]

    def interrupted(directory, env):
        if env.get("op") == "part_receipt" and env.get("kind") == "attachment_part" and fail[0]:
            fail[0] = False
            raise OSError("synthetic publication failure")
        return publish(directory, env)

    monkeypatch.setattr(envelopes, "publish_command", interrupted)
    logs = []
    world.worker._log = lambda event, **fields: logs.append((event, fields))
    asyncio.run(_granted_card(world.worker, led, world.root))
    assert fail == [False]  # tick isolates the publication error and preserves the journal
    assert any(fields.get("error") == "OSError" for _, fields in logs)
    row, = _rows(world)
    assert row["attachment_origin"]["method"] == "upload"
    assert len(world.client.upload_calls) == 1
    spec = json.loads(_latest_render(led)["spec_json"])
    asyncio.run(world.worker._resume_parts(spec))
    notify_cmds.drain_int_commands(led, {"errors": []}, SLACK, str(world.root))
    assert len(world.client.upload_calls) == 1
    assert _rows(world) == [row]
    assert any(r.get("republish") for rows in journal.scan(world.dirs["state"]).values()
               for r in rows if r.get("part_id") == row["part_id"] and r.get("phase") == "receipt")


def test_ambiguous_upload_has_no_claim_of_upload_provenance(led, tmp_path):
    world, _, _ = _world(led, tmp_path)
    world.client.upload_fail_at = {0}
    world.client.upload_failure = TimeoutError("synthetic")
    asyncio.run(_granted_card(world.worker, led, world.root))
    row, = _rows(world)
    assert row["result"] == "unknown" and "attachment_origin" not in row
    assert sum(kind == "files_upload_v2" for kind, _ in world.client.calls) == 1
    assert world.client.upload_calls == []  # fake records successful uploads separately


@pytest.mark.parametrize("missing", [404, 410, "message_not_found"])
@pytest.mark.parametrize("scoped", [False, True])
def test_missing_file_caption_target_is_not_recorded_as_a_successful_edit(led, tmp_path, missing, scoped, monkeypatch):
    from types import SimpleNamespace
    world, _, _ = _world(led, tmp_path, legacy=monkeypatch)
    asyncio.run(_granted_card(world.worker, led, world.root))
    spec = json.loads(_latest_render(led)["spec_json"])
    if not scoped:
        spec["parts"].pop("post_source_id", None)
    assert post_scoped(spec) is scoped
    part = next(p for p in spec["parts"]["manifest"] if p["kind"] == "attachment_part")
    fid = _rows(world)[0]["remote_id"]

    async def gone(**kwargs):
        if missing == "message_not_found":
            return {"ok": False, "error": "message_not_found"}
        error = RuntimeError("synthetic")
        error.response = SimpleNamespace(status_code=missing)
        raise error

    world.client.chat_update = gone
    out = asyncio.run(world.worker._attachment_part(
        {**spec, "op": "update", "delivery": {**spec["delivery"], "thread_id": ROOT_TS}},
        {**part, "edit_only": True, "prior_remote_id": fid, "caption": "changed"},
        {"thread_id": ROOT_TS, "history": None, "consumed": set()}))
    assert out == ({"result": "unknown", "error_code": "edit_target_unverified"} if scoped
                   else {"result": "delivered", "remote_id": fid})
    assert "attachment_origin" not in out
    assert len(world.client.upload_calls) == 1
