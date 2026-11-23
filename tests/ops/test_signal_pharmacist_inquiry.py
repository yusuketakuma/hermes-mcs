"""Synthetic pharmacy-to-physician inquiries awaiting a reply on the record."""
import json

import pytest

from common_testkit import ledger_fixture
import mcs_signals
from ops_testkit import DAY, NOW, _msg
import signal_policy
from inference_version import CURRENT_VERSION, VERSION_KEY

CFG = {"self_professions": ["薬剤師"], "self_organizations": ["合成薬局"]}

store = ledger_fixture(name="store")


def _inquiry(db, mid, *, pid=1, to="主治医", action="処方変更の提案", org="合成薬局", age=5, parent=None):
    _msg(db, mid, pid=pid, ts=NOW - age * DAY, chash=f"h{mid}", body="合成の照会です。",
         prof="薬剤師", org=org, parent=parent)
    db.execute("INSERT INTO artifacts(kind,message_id,content,meta) VALUES('extract_llm',?,?,?)",
               (mid, json.dumps({"requests": [{"to": to, "action": action}]}),
                json.dumps({VERSION_KEY: CURRENT_VERSION, "hash": f"h{mid}"})))
    db.commit()


def _run(db, cfg=CFG):
    return list(mcs_signals._pharmacist_inquiry(db, NOW, signal_policy.resolved_thresholds(db), cfg))


def test_own_inquiry_without_reply_is_a_candidate(store):
    _inquiry(store.db, 1)
    [(key, sig)] = _run(store.db)
    assert sig["type"] == "pharmacist_inquiry_unanswered"
    assert sig["evidence"] == {"message_ids": [1], "request_actions": ["処方変更の提案"]}
    assert "記録上の返答を確認できませんでした" in sig["note"] and "記録外" in sig["note"]


@pytest.mark.parametrize("reply", [
    {"prof": "看護師", "org": "合成訪看", "parent": 1},       # someone else answered in the thread
    {"prof": "医師", "org": "合成医院", "parent": None},       # a physician posted in the room
    {"prof": "医師", "org": "合成薬局", "parent": 1},          # a physician sharing our organization
])
def test_later_reply_suppresses(store, reply):
    _inquiry(store.db, 1)
    _msg(store.db, 2, pid=1, ts=NOW - 4 * DAY, chash="h2", **reply)
    store.db.commit()
    assert _run(store.db) == []


@pytest.mark.parametrize("reply", [
    {"prof": "薬剤師", "org": "合成薬局", "parent": 1},        # only our own follow-up
    {"prof": "看護師", "org": "合成訪看", "parent": None},     # an unrelated post in the room
])
def test_own_follow_up_or_unrelated_post_is_not_a_reply(store, reply):
    _inquiry(store.db, 1)
    _msg(store.db, 2, pid=1, ts=NOW - 4 * DAY, chash="h2", **reply)
    store.db.commit()
    assert len(_run(store.db)) == 1


@pytest.mark.parametrize("kwargs", [
    {"org": "他の薬局"}, {"to": "薬剤師"}, {"to": "ケアマネ"},
    {"action": "訪問日の調整"}, {"action": "訪問日の変更"}, {"action": "次回訪問の提案"},
    {"age": 1}, {"age": 200},
])
def test_out_of_scope_inquiries_do_not_fire(store, kwargs):
    _inquiry(store.db, 1, **kwargs)
    assert _run(store.db) == []


def test_needs_own_organization_config(store):
    _inquiry(store.db, 1)
    assert _run(store.db, {"self_professions": ["薬剤師"]}) == []


def test_own_inquiry_is_not_also_a_visibility_signal(store):
    _inquiry(store.db, 1)
    _inquiry(store.db, 2, pid=2, org="合成訪看")
    found = mcs_signals._rx_request_visibility(store.db, NOW, signal_policy.resolved_thresholds(store.db), CFG)
    assert [sig["evidence"]["message_ids"] for _, sig in found] == [[2]]


def test_resolution_records_the_reply(store):
    _inquiry(store.db, 1)
    [(_, sig)] = _run(store.db)
    _msg(store.db, 2, pid=1, ts=NOW - 1 * DAY, chash="h2", prof="医師", org="合成医院")
    store.db.commit()
    resolved = mcs_signals._resolution(store.db, {**sig, "state": "open"}, NOW, signal_policy.resolved_thresholds(store.db), CFG)
    assert resolved["cause"] == "reply_observed"
