"""Regression: med_change_no_followup evidence keeps the NEWEST mentions
so message_ids[-1] (group key / urgency) is the latest mention."""
import json

import pytest

import ledger as ledger_mod
import mcs_signals
from ops_testkit import DAY, NOW, _msg


@pytest.fixture
def led(tmp_path):
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    yield lg
    lg.db.close()


def _extract_llm(db, mid, chash, meds):
    db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_llm',?,?,?)",
        (mid, json.dumps({"meds": meds}), json.dumps({"hash": chash})))


def test_eleven_mentions_keep_latest_in_evidence(led):
    for mid in range(1, 12):
        _msg(led.db, mid, ts=NOW - (89 - 8 * (mid - 1)) * DAY,
             chash=f"h{mid}")
        meds = [{"name": "薬A", "action": "change"}]
        if mid == 11:
            meds.append({"name": "薬B", "action": "start"})
        _extract_llm(led.db, mid, f"h{mid}", meds)
    mcs_signals.evaluate(led, {}, now=NOW)
    sigs = {s["evidence"]["med"]: s
            for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "med_change_no_followup"}
    a = sigs["薬A"]["evidence"]
    assert a["mention_count"] == 11
    assert a["message_ids"] == list(range(2, 12))
    assert (mcs_signals.med_group_key(sigs["薬A"])
            == mcs_signals.med_group_key(sigs["薬B"]) == (1, 11))
