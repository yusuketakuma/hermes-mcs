"""LINE WORKS MCS stamp DM form: numbered choices from the runner form. Synthetic only."""
import asyncio
import json

from adapters.common import envelopes, paths, text
from lineworks_testkit import SCOPE, TOKEN, command_files, event, world

ACTOR = "lineworks:40029600:operator"
FORM = {"card_id": 7, "token": "b" * 32, "mcs_user_id": 501, "project_id": 1,
        "form_hash": "f" * 64, "types": [["viewed", "見ました"], ["accepted", "承知"]],
        "posts": [{"opt": "p1", "message_id": 100, "source_hash": "1" * 64,
                   "label": "10-01 09:00 根元",
                   "own": {"type": "accepted", "state": "observed", "observed_at": 2.0}}]}


def _result(w, cid, value):
    paths.atomic_write(f"{w.dirs['cmd_results']}/{paths.safe_name(cid)}.json",
                       json.dumps(value).encode())


def _open_form(w, form=None):
    asyncio.run(w.actions.handle(event(postback="mcs:a:" + TOKEN)))
    [request] = command_files(w)
    _result(w, request["request_id"], {
        "request_id": request["request_id"], "outcome": "applied", "action": "stamp",
        "modal": True, "params": {}, **({"form": form} if form is not None else {})})
    asyncio.run(w.actions.sweep_followups())
    return request


def test_stamp_form_prompts_by_number_and_confirms_the_shown_target(tmp_path):
    w = world(tmp_path, action="stamp")
    request = _open_form(w, FORM)
    session = w.reg.modal("lw-form-" + envelopes.actor_hash(ACTOR))
    assert [d["id"] for d in session["definitions"]] == ["post", "op", "reason"]
    assert session["form"] == FORM
    assert "番号で返信" in w.client.calls[-1][1]["text"]
    for answer in ("1", "2", "誤って押した"):      # post p1, its own 承知 again = 取消, reason
        asyncio.run(w.actions._input("operator", ACTOR, answer))
    preview = str(w.client.calls[-1][1])          # a confirm-button template
    assert "取消（現在: 承知）" in preview and "収集アカウント" in preview
    assert command_files(w) == [request]  # No human command is sent before confirm.
    payload = next(iter(w.reg._data["pending_confirms"].values()))["payload"]
    assert (payload["cmd"], payload["op"], payload["message_id"]) == ("ops.mcs_reaction", "cancel", 100)
    assert "reaction_type" not in payload
    cid = next(iter(w.reg._data["pending_confirms"]))
    asyncio.run(w.actions._confirm(cid, False, "operator", ACTOR))
    assert len(command_files(w)) == 2
    w.client.calls.clear()
    _result(w, payload["command_id"], {"outcome": "applied", "stamp_state": "queued"})
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(payload["command_id"]) is not None and w.client.calls == []
    _result(w, payload["command_id"], {"outcome": "applied", "stamp_state": "unknown"})
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(payload["command_id"]) is None
    assert "再送しません" in str(w.client.calls[-1][1])


def test_stamp_form_without_runner_targets_is_never_prompted(tmp_path):
    w = world(tmp_path, action="stamp")
    _open_form(w)
    assert w.reg.modal("lw-form-" + envelopes.actor_hash(ACTOR)) is None
    assert w.client.calls[-1][1]["text"] == text.STAMP_FORM_PENDING


def test_obsolete_stamp_result_consumes_followup_without_an_empty_sdk_message(tmp_path):
    w = world(tmp_path, action='stamp')
    cid = 'obsolete-stamp'
    w.reg.put_followup(cid, {"kind": "form", "action": "stamp", "actor": ACTOR, "user": "operator",
                             "token": TOKEN, "origin": {**SCOPE, "message_id": "lw:" + "c" * 32},
                             "context": {"project_id": 1}})
    _result(w, cid, {'outcome': 'rejected', 'stamp_state': 'rejected', 'stamp_error': 'source_changed'})
    asyncio.run(w.actions._say('operator', ''))
    asyncio.run(w.actions.sweep_followups())
    asyncio.run(w.actions.sweep_followups())
    assert w.reg.followup(cid) is None and w.client.calls == []
