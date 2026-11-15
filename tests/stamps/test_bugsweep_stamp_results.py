"""Synthetic stamp results keep their real reason and reach the click that asked for them."""
import json
from pathlib import Path

import pytest

from adapters.common import text
from mcs_contract import MCSError
import notify_spool
import stamp_write
from notify_testkit import _uuid
from test_stamp_write import (
    _apply, _command, _drain, _press_buttons, _receipt, led, pinned_clock, scene,
)

__all__ = ["led", "pinned_clock", "scene"]
pytestmark = pytest.mark.usefixtures("pinned_clock")


def _results(scene):
    return Path(notify_spool.notify_dirs(scene[2])["cmd_results"])


def _scope(scene):
    return scene[2] / "stamp_operations" / "1-100-77"


@pytest.mark.parametrize("legacy", [False, True])
def test_restored_server_rejection_replays_its_reason(scene, legacy):
    request = _command(scene)
    assert _apply(scene, request)["stamp_state"] == "queued"
    db = scene[0].db
    row = db.execute("SELECT artifact_id,content FROM artifacts WHERE kind=?", (stamp_write.KIND,)).fetchone()
    receipt = db.execute("SELECT receipt_json FROM command_receipts WHERE command_id=?",
                         (request["command_id"],)).fetchone()[0]
    scene[3].failure = MCSError("http_error", status=409)
    assert _drain(scene)["rejected"] == 1
    witness = _scope(scene) / (request["command_id"] + ".json")
    if legacy:                        # a record written before the reason was kept
        value = json.loads(witness.read_bytes())
        del value["error"]
        stamp_write.atomic_write(str(witness), lambda f: f.write(json.dumps(value)), mode=0o600, makedirs=False)
    # The DB goes back to before the attempt; the external witness survives.
    db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (row["content"], row["artifact_id"]))
    db.execute("UPDATE command_receipts SET receipt_json=? WHERE command_id=?", (receipt, request["command_id"]))
    db.commit()
    scene[3].failure = None
    assert _drain(scene)["rejected"] == 1
    assert scene[3].posts == [(100, "viewed", False)]          # never sent again
    assert _receipt(scene, request)["stamp_error"] == "stamp_server_rejected"
    result = json.loads((_results(scene) / (request["command_id"] + ".json")).read_bytes())
    assert result["error"] == "stamp_server_rejected"
    assert text.ERR_JA["stamp_server_rejected"] in text.ja(result)


@pytest.mark.parametrize("name,raw,code", [
    (_uuid(9001) + ".json", b"{not json", "stamp_witness_invalid"),
    ("not-a-uuid.json", b"{}", "stamp_witness_invalid"),
    ("stray.txt", b"synthetic", "stamp_witness_capacity"),
])
def test_bad_witness_records_are_named_not_called_a_restore(scene, name, raw, code):
    assert _apply(scene, _command(scene))["stamp_state"] == "queued"
    assert _drain(scene)["confirmed"] == 1
    path = _scope(scene) / name
    path.write_bytes(raw)
    path.chmod(0o600)
    result = _apply(scene, _command(scene, serial=701, kind="accepted"))
    assert (result["outcome"], result["error"]) == ("rejected", code)


def test_form_confirm_behind_a_queued_operation_is_result_unknown(scene):
    later = _command(scene, serial=701, kind="accepted")     # the form was opened first
    assert _apply(scene, _command(scene))["stamp_state"] == "queued"
    result = _apply(scene, later)
    assert (result["outcome"], result["error"]) == ("rejected", "stamp_result_unknown")


def _click(scene, button, request_id=None):
    import notify_cmds
    from notify_testkit import CLICKER, NOW, ORIGIN
    ledger, cfg, root, _, _ = scene
    req = {"version": 1, "op": "notification", "command_id": f"{button['token']}:{1:016x}",
           "actor": CLICKER, "token": button["token"],
           "origin": ORIGIN, "clicked_at": NOW}
    if request_id:
        req["request_id"] = request_id
    assert notify_cmds.validate_int(req) is None
    return notify_cmds.dispatch(ledger, req, cfg, str(root), now=NOW)


def _button(scene, prefix):
    return next(b for b in _press_buttons(scene) if b["label"].startswith(prefix))


def test_a_repress_after_an_execution_rejection_answers_the_rejection(scene):
    button = _button(scene, "👀")
    assert _click(scene, button)["outcome"] == "applied"
    scene[3].account = 78                     # refused before any post is read or written
    assert _drain(scene)["rejected"] == 1
    again = _click(scene, button)
    assert (again["outcome"], again["error"], again["stamp_state"]) == (
        "rejected", "stamp_account_mismatch", "rejected")
    answer = text.ja(again)
    assert "受け付けました" not in answer and text.ERR_JA["stamp_account_mismatch"] in answer
    assert scene[3].posts == []


def test_a_press_followup_waits_for_and_reads_the_final_result(scene):
    click = _uuid(5000)
    pressed = _click(scene, _button(scene, "👀"), request_id=click)
    assert pressed["stamp_state"] == "queued" and text.followup_waiting(pressed)
    assert _drain(scene)["confirmed"] == 1
    final = json.loads((_results(scene) / (click + ".json")).read_bytes())
    assert final["request_id"] == click and final["stamp_state"] == "confirmed"
    assert not text.followup_waiting(final) and text.ja(final) == text.STAMP_STATE_TEXT["confirmed"]
