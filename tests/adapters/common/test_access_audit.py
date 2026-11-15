"""Synthetic checks that every native command leaves an access record before answering."""
import json

import access_log
from adapters.common import dispatch as command_dispatch
from adapters.common.mcs_api import deny, ok

IDENTITY = {"transport": "slack", "user_id": "U1", "chat_id": "C1", "scope_id": "T1",
            "profile": None, "message_id": None}


def _settings(tmp_path):
    (tmp_path / "cmd").mkdir(exist_ok=True)
    return {"inbox": str(tmp_path / "cmd")}


def test_read_and_denial_are_recorded_without_query_text(tmp_path, monkeypatch):
    replies = iter([ok(operation="read", result={"rows": []}), deny("project_not_allowed")])
    monkeypatch.setattr(command_dispatch, "dispatch_operation", lambda *a, **k: next(replies))
    data = {"op": "read", "kind": "search", "project_id": 3, "query": "合成 患者名"}
    assert json.loads(command_dispatch.dispatch(data, _settings(tmp_path), IDENTITY))["ok"] is True
    command_dispatch.dispatch(data, _settings(tmp_path), IDENTITY)
    entries = list(access_log.read(tmp_path))
    assert [(e["actor"], e["kind"], e["project_id"], e["outcome"]) for e in entries] == [
        ("slack:T1:U1", "search", 3, "ok"), ("slack:T1:U1", "search", 3, "project_not_allowed")]
    raw = b"".join(p.read_bytes() for p in (tmp_path / access_log.DIR_NAME).glob("*.jsonl"))
    assert "合成".encode() not in raw and entries[0]["query"] is True


def test_unrecorded_read_is_withheld_but_confirmed_write_is_not(tmp_path, monkeypatch):
    monkeypatch.setattr(command_dispatch, "dispatch_operation",
                        lambda *a, **k: ok(operation="read", result={"secret": 1}))
    settings = {"inbox": str(tmp_path / "missing" / "cmd")}
    read = command_dispatch.dispatch({"op": "read", "kind": "timeline", "project_id": 1},
                                     settings, IDENTITY)
    assert json.loads(read) == {"ok": False, "error": "access_log_unavailable"}
    write = command_dispatch.dispatch({"op": "request", "phase": "confirm"}, settings, IDENTITY)
    assert json.loads(write)["ok"] is True
