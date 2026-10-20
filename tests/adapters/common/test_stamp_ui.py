"""MCS stamp modal, payload and wording: target only from the runner form. Synthetic only."""
from __future__ import annotations

import pytest

from adapters.common import envelopes, text

TOKEN = "a" * 32
FORM = {
    "card_id": 7, "token": TOKEN, "mcs_user_id": 501, "project_id": 3,
    "form_hash": "f" * 64,
    "types": [["viewed", "見ました"], ["accepted", "承知"], ["completed", "完了"]],
    "posts": [
        {"opt": "p1", "message_id": 100, "source_hash": "1" * 64,
         "label": "10-01 09:00 根元",
         "own": {"type": "accepted", "state": "observed", "observed_at": 1790000000.0}},
        {"opt": "p2", "message_id": 101, "source_hash": "2" * 64,
         "label": "10-01 09:05 返信（添付あり）",
         "own": {"type": None, "state": "observed", "observed_at": 1790000001.0}},
        {"opt": "p3", "message_id": 102, "source_hash": "3" * 64,
         "label": "10-01 09:10 返信",
         "own": {"type": None, "state": "unknown", "observed_at": None}},
        {"opt": "p4", "message_id": 103, "source_hash": "4" * 64,
         "label": "10-01 09:15 返信",
         "own": {"type": "viewed", "state": "pending", "observed_at": 1790000002.0}},
    ],
}
ACTOR = "slack:T1:U1"


def _payload(fields, form=FORM):
    return envelopes.human_payload("stamp", ACTOR, {"project_id": 3}, {}, fields, form=form)


def test_modal_offers_only_runner_posts_and_types_with_required_reason():
    fields = {f["id"]: f for f in text.modal_fields("stamp", FORM)}
    assert [v for v, _ in fields["post"]["options"]] == ["p1", "p2", "p3", "p4"]
    assert "承知" in dict(fields["post"]["options"])["p1"]
    ops = dict(fields["op"]["options"])
    # MCS's own row: the stamps in runner order, no separate cancel choice
    assert list(ops) == ["add:viewed", "add:accepted", "add:completed"]
    assert fields["reason"]["required"] and fields["reason"]["max"] == 200
    assert "stamp" in text.MODAL_ACTIONS and text.MODAL_TITLES["stamp"]


def test_add_builds_exact_contract_fields_from_the_shown_form():
    env = _payload({"post": "p2", "op": "add:completed", "reason": "確認しました"})
    assert set(env) == {"version", "cmd", "command_id", "actor", "human_confirmed",
                        "project_id", "card_id", "token", "message_id", "source_hash",
                        "form_hash", "expected_own", "reason", "mcs_user_id", "op", "confirmed_at",
                        "reaction_type"}
    assert env["cmd"] == "ops.mcs_reaction" and env["human_confirmed"] is True
    assert (env["message_id"], env["source_hash"]) == (101, "2" * 64)
    assert (env["card_id"], env["token"], env["mcs_user_id"], env["project_id"]) == (7, TOKEN, 501, 3)
    assert env["op"] == "add" and env["reaction_type"] == "completed"
    assert env["expected_own"] == {"type": None, "observed_at": 1790000001.0}


def test_cancel_requires_a_known_own_stamp_and_carries_no_kind():
    env = _payload({"post": "p1", "op": "cancel", "reason": "誤って押した"})
    assert env["op"] == "cancel" and "reaction_type" not in env
    assert env["expected_own"] == {"type": "accepted", "observed_at": 1790000000.0}
    assert isinstance(_payload({"post": "p2", "op": "cancel", "reason": "x"}), str)


def test_choosing_the_own_stamp_takes_it_back_like_mcs():
    env = _payload({"post": "p1", "op": "add:accepted", "reason": "取り消す"})
    assert env["op"] == "cancel" and "reaction_type" not in env
    assert env["expected_own"]["type"] == "accepted"


def test_forms_opened_before_the_mcs_layout_still_accept_cancel():
    env = _payload({"post": "p1", "op": "cancel", "reason": "取り消す"})
    assert env["op"] == "cancel"


@pytest.mark.parametrize("fields", [
    {"post": "p3", "op": "add:viewed", "reason": "x"},       # own unknown
    {"post": "p4", "op": "add:viewed", "reason": "x"},       # already pending
    {"post": "p9", "op": "add:viewed", "reason": "x"},       # not offered
    {"post": "p2", "op": "add:good", "reason": "x"},         # kind not offered
    {"post": "p2", "op": "add:viewed", "reason": ""},        # reason required
    {"post": "p2", "op": "add:viewed", "reason": "あ" * 201},
    {"post": "p2", "op": "delete", "reason": "x"},
])
def test_invalid_selection_is_a_message_not_a_command(fields):
    out = _payload(fields)
    assert isinstance(out, str) and out


def test_missing_form_never_builds_a_command():
    assert isinstance(_payload({"post": "p2", "op": "add:viewed", "reason": "x"}, form=None), str)
    broken = {**FORM, "token": "not-a-token"}
    assert isinstance(_payload({"post": "p2", "op": "add:viewed", "reason": "x"}, form=broken), str)


def test_preview_names_target_operation_reason_and_shared_account():
    env = _payload({"post": "p1", "op": "cancel", "reason": "誤って押した"})
    out = text.preview_text("stamp", env, markdown=False, target="患者A · 10-01 09:00の投稿",
                            form=FORM)
    assert "取消" in out and "承知" in out and "誤って押した" in out
    assert "収集アカウント" in out
    env = _payload({"post": "p2", "op": "add:completed", "reason": "確認しました"})
    assert "完了" in text.preview_text("stamp", env, markdown=True, form=FORM)


@pytest.mark.parametrize("state,expect,forbid", [
    ("queued", "確認中", "反映しました"),
    ("unknown", "再送しません", "反映しました"),
    ("confirmed", "MCSで確認済み", None),
])
def test_receipt_wording_never_claims_completion_before_readback(state, expect, forbid):
    out = text.ja({"outcome": "applied", "action": "stamp", "stamp_state": state})
    assert expect in out
    if forbid:
        assert forbid not in out and "押しました" not in out


def test_only_a_queued_stamp_keeps_its_followup_waiting():
    assert text.followup_waiting({"outcome": "applied", "stamp_state": "queued"})
    for state in ("confirmed", "unknown", "rejected", None):
        assert not text.followup_waiting({"outcome": "applied", "stamp_state": state})
    assert not text.followup_waiting(None)


def test_terminal_rejection_names_the_stamp_without_internal_codes():
    out = text.ja({"outcome": "rejected", "error": "stamp_rejected", "stamp_state": "rejected"})
    assert "MCSスタンプ" in out and "stamp_rejected" not in out and "反映しました" not in out


def test_preview_names_the_selected_post_not_only_the_card_target():
    target = "患者A · 10-01 09:00の投稿"
    root = text.preview_text("stamp", _payload({"post": "p1", "op": "cancel", "reason": "x"}),
                             markdown=False, target=target, form=FORM)
    reply = text.preview_text("stamp", _payload({"post": "p2", "op": "add:viewed", "reason": "x"}),
                              markdown=False, target=target, form=FORM)
    assert "投稿: 10-01 09:00 根元" in root
    assert "投稿: 10-01 09:05 返信（添付あり）" in reply and "根元" not in reply
    assert target in root and target in reply
    tampered = {**_payload({"post": "p2", "op": "add:viewed", "reason": "x"}), "source_hash": "9" * 64}
    assert "投稿: 不明" in text.preview_text("stamp", tampered, markdown=False, form=FORM)
