"""Retained Discord files need sealed remote identity before a single surface write."""
import asyncio
from types import SimpleNamespace

import pytest
from adapters.common.spec import discord_file_name
from adapters.discord import delivery


def _fixture():
    member = {"part_id": "attach:0001", "name": "synthetic.pdf", "sha256": "a" * 64,
              "bytes": 7, "prior_remote_id": "101", "prior_remote_file_id": "201"}
    remote = SimpleNamespace(id=201, filename=discord_file_name(member), size=7)
    message = SimpleNamespace(id=101, attachments=[remote])
    instance = object.__new__(delivery.DeliveryWorker)
    return instance, member, message


@pytest.mark.parametrize("fault", ["none", "changed_hash", "wrong_file_id", "wrong_message", "extra_file"])
def test_retained_files_require_exact_identity_and_complete_remote_set(fault):
    instance, member, message = _fixture()
    if fault == "changed_hash":
        member["sha256"] = "b" * 64
    elif fault == "wrong_file_id":
        message.attachments[0].id = 202
    elif fault == "wrong_message":
        message.id = 102
    elif fault == "extra_file":
        message.attachments.append(SimpleNamespace(id=203, filename="foreign.pdf", size=7))
    async def no_upload(members):
        assert not members, "proven old files must never be uploaded again"
        return []
    instance._member_files = no_upload
    result = asyncio.run(instance._edit_group_files(message, [member]))
    assert (result == message.attachments) if fault == "none" else result is None


@pytest.mark.parametrize("response_fault", [None, "wrong_message", "wrong_channel"])
def test_card_group_update_uses_one_edit_and_retains_only_proven_file(monkeypatch, response_fault):
    instance, member, message = _fixture()
    writes = []
    async def edit(**kwargs):
        writes.append(kwargs)
        if response_fault == "wrong_message":
            return SimpleNamespace(id=102, attachments=message.attachments)
        if response_fault == "wrong_channel":
            return SimpleNamespace(id=101, channel=SimpleNamespace(id=99), attachments=message.attachments)
        return message
    async def fetch(_):
        return message
    async def channel(_):
        return SimpleNamespace(id=42, fetch_message=fetch)
    async def no_upload(members):
        assert members == []
        return []
    message.edit = edit
    instance._channel = channel
    instance._member_files = no_upload
    monkeypatch.setattr(delivery.cards, "message_payload", lambda *_a, **_kw: {"view": "synthetic-layout"})
    monkeypatch.setattr(delivery.cards, "no_pings", lambda: "none")
    spec = {"op": "update", "delivery": {"channel_id": "42", "message_id": "101"},
            "parts": {"chat_format_version": 1, "manifest": [member,
                {"part_id": "file_group:card", "kind": "file_group", "surface": "card", "members": [member["part_id"]]}]}}
    out = asyncio.run(instance._perform_card({"spec": spec}))
    if response_fault:
        assert out == {"result": "unknown", "error_code": "file_group_unverified"}
    else:
        assert out["result"] == "delivered"
        assert out["files"][0]["remote_file_id"] == "201"
    assert len(writes) == 1
    assert writes[0]["attachments"] == message.attachments
