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


def test_unrecorded_failure_carrying_patient_content_is_withheld(tmp_path, monkeypatch):
    settings = {"inbox": str(tmp_path / "missing" / "cmd")}
    carried = json.dumps({"ok": False, "error": "summary_not_adoptable",
                          "comparison": {"candidate": {"text": "合成 臨床内容"}}})
    monkeypatch.setattr(command_dispatch, "dispatch_operation", lambda *a, **k: carried)
    data = {"op": "control", "action": "adopt_summary", "phase": "preview", "project_id": 1}
    assert json.loads(command_dispatch.dispatch(data, settings, IDENTITY)) == {
        "ok": False, "error": "access_log_unavailable"}
    monkeypatch.setattr(command_dispatch, "dispatch_operation",
                        lambda *a, **k: deny("project_not_allowed"))
    assert json.loads(command_dispatch.dispatch(data, settings, IDENTITY))["error"] == "project_not_allowed"


def test_confirmation_is_attributed_to_the_patient_in_its_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(command_dispatch, "dispatch_operation",
                        lambda *a, **k: ok(operation="request", receipt="r1"))
    payload = {"cmd": "ops.adopt_summary", "project_id": 7, "actor": "slack:T1:U1"}
    command_dispatch.dispatch({"op": "control", "phase": "confirm", "payload": payload,
                               "payload_hash": "h", "origin": {}}, _settings(tmp_path), IDENTITY)
    monkeypatch.setattr(command_dispatch, "dispatch_operation",
                        lambda *a, **k: deny("payload_hash_mismatch"))
    command_dispatch.dispatch({"op": "request", "phase": "confirm",
                               "payload": {"cmd": "request.create", "project_id": 8}},
                              _settings(tmp_path), IDENTITY)
    done, refused = access_log.read(tmp_path)
    assert (done["op"], done["kind"], done["project_id"]) == ("control.confirm", "adopt_summary", 7)
    assert (refused["kind"], refused["project_id"]) == (None, None)


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
    assert rejected["kind"] == "invalid" and rejected["patients"] == []
    assert (named["op"], named["kind"]) == ("invalid.invalid", "invalid")


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


def test_native_summary_entries_share_the_recorded_answer(tmp_path, monkeypatch):
    from adapters.common import summary
    monkeypatch.setattr(summary, "answer", lambda *a, **k: {"text": "合成", "parts": {},
                                                           "project_ids": [3]})
    settings = {**_settings(tmp_path), "snapshot": str(tmp_path / "s.db"), "project_ids": frozenset({3})}
    who = {"transport": "lineworks", "user": "U9", "scope_id": "D1"}
    assert summary.answer_recorded(settings, "all", **who)["text"] == "合成"
    entry, = access_log.read(tmp_path)
    assert (entry["actor"], entry["op"], entry["patients"]) == ("lineworks:D1:U9", "summary", [3])
    missing = {**settings, "inbox": str(tmp_path / "gone" / "cmd")}
    assert summary.answer_recorded(missing, "all", **who) == {
        "error": summary.ACCESS_LOG_UNAVAILABLE}


def test_confirm_reply_without_an_access_record_carries_only_the_receipt(tmp_path):
    """A confirmed operation is queued before its record is attempted, so the
    reply stays, but it holds no patient information and a preview is withheld."""
    root = tmp_path
    settings = {**_settings(root), "allowed_user_ids": {"U1"}, "allowed_chat_ids": {"C1"}}
    identity = {**IDENTITY, "application_id": "A1", "route_epoch": 1}
    marker = "完全合成の確認理由"
    preview = json.loads(command_dispatch.dispatch(
        {"op": "control", "phase": "preview", "action": "update_rollback",
         "tag": "v1.0.11", "reason": marker}, settings, identity))
    assert preview["ok"] is True
    (root / access_log.DIR_NAME).chmod(0o755)   # judged untrusted: no record can be written
    confirm = {"op": "control", "phase": "confirm", "payload": preview["payload"],
               "payload_hash": preview["payload_hash"], "origin": preview["origin"]}
    reply = command_dispatch.dispatch(confirm, settings, identity)
    parsed = json.loads(reply)
    assert set(parsed) == {"ok", "operation", "phase", "receipt", "payload_hash"} and parsed["ok"] is True
    assert set(parsed["receipt"]) <= {"command_id", "payload_hash", "outcome", "error"}
    assert command_dispatch._returned_patients(parsed) == set()
    assert marker not in reply and "project_id" not in reply
    assert len(list((root / "cmd").glob("*.json"))) == 1
    withheld = command_dispatch.dispatch(
        {"op": "control", "phase": "preview", "action": "update_rollback",
         "tag": "v1.0.11", "reason": marker}, settings, identity)
    assert json.loads(withheld) == {"ok": False, "error": "access_log_unavailable"}
    assert len(list((root / "cmd").glob("*.json"))) == 1
