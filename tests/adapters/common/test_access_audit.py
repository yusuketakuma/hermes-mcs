"""Synthetic checks that every native command leaves an access record before answering."""
import json

import access_log
import pytest
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


def test_unknown_command_words_are_recorded_as_other_not_as_typed(tmp_path, monkeypatch):
    monkeypatch.setattr(command_dispatch, "dispatch_operation", lambda *a, **k: deny("bad_read"))
    for data in ({"op": "read", "kind": "合成太郎"}, {"op": "合成花子", "kind": "search"},
                 {"op": "control", "phase": "合成次郎", "action": "合成三郎"},
                 {"op": "read", "kind": "synthetic_person"}):
        command_dispatch.dispatch(data, _settings(tmp_path), IDENTITY)
    entries = list(access_log.read(tmp_path))
    assert [(e["op"], e["kind"]) for e in entries] == [
        ("read", "other"), ("other", "search"), ("control.other", "other"), ("read", "other")]
    raw = b"".join(p.read_bytes() for p in (tmp_path / access_log.DIR_NAME).glob("*.jsonl"))
    assert "合成".encode() not in raw and b"synthetic_person" not in raw


def test_projectless_list_is_attributed_to_every_returned_patient(tmp_path, monkeypatch):
    reply = ok(operation="read", result={"items": [{"project_id": 4}, {"project_id": 9,
                                                   "nested": [{"project_id": 4}]}]})
    monkeypatch.setattr(command_dispatch, "dispatch_operation", lambda *a, **k: reply)
    command_dispatch.dispatch({"op": "read", "kind": "signals"}, _settings(tmp_path), IDENTITY)
    monkeypatch.setattr(command_dispatch, "dispatch_operation", lambda *a, **k: deny("bad_read"))
    command_dispatch.dispatch({"op": "read", "kind": "合成 患者 本文"}, _settings(tmp_path), IDENTITY)
    command_dispatch.dispatch({"op": "goukei.taro", "kind": "alice.smith", "phase": "x"},
                              _settings(tmp_path), IDENTITY)
    listed, rejected, named = access_log.read(tmp_path)
    assert listed["project_id"] is None and listed["patients"] == [4, 9]
    assert rejected["kind"] == "other" and rejected["patients"] == []
    assert (named["op"], named["kind"]) == ("other.other", "other")


def test_summary_text_records_shown_patients_and_its_failures(tmp_path, monkeypatch):
    from adapters.common import summary
    monkeypatch.setattr(command_dispatch, "authorize_system", lambda *a: None)
    answers = iter([{"text": "合成サマリー", "parts": {}, "project_ids": [2, 5]},
                    {"error": "合成の未作成メッセージ"}])
    monkeypatch.setattr(summary, "answer", lambda *a, **k: next(answers))
    settings = {**_settings(tmp_path), "snapshot": str(tmp_path / "s.db")}
    assert command_dispatch.dispatch({"op": "summary"}, settings, IDENTITY) == "合成サマリー"
    assert command_dispatch.dispatch({"op": "summary"}, settings, IDENTITY) == "合成の未作成メッセージ"
    shown, failed = access_log.read(tmp_path)
    assert (shown["outcome"], shown["patients"]) == ("ok", [2, 5])
    assert (failed["outcome"], failed["patients"]) == ("summary_unavailable", [])


@pytest.mark.parametrize(("transport", "scope_id", "expected_actor"), [
    ("slack", "T1", "slack:T1:U9"),
    ("discord", "G1", "discord:U9"),
    ("lineworks", "D1", "lineworks:D1:U9"),
])
def test_native_summary_entries_share_the_recorded_answer(
        tmp_path, monkeypatch, transport, scope_id, expected_actor):
    from adapters.common import summary
    monkeypatch.setattr(summary, "answer", lambda *a, **k: {"text": "合成", "parts": {},
                                                           "project_ids": [3]})
    settings = {**_settings(tmp_path), "snapshot": str(tmp_path / "s.db"), "project_ids": frozenset({3})}
    who = {"transport": transport, "user": "U9", "scope_id": scope_id}
    assert summary.answer_recorded(settings, "all", **who)["text"] == "合成"
    entry, = access_log.read(tmp_path)
    assert (entry["actor"], entry["op"], entry["patients"]) == (expected_actor, "summary", [3])
    missing = {**settings, "inbox": str(tmp_path / "gone" / "cmd")}
    assert summary.answer_recorded(missing, "all", **who) == {
        "error": summary.ACCESS_LOG_UNAVAILABLE}
