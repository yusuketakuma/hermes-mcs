"""Regressions: multi-value profession/organization columns (joined
with ", " by mcs_adapter) match per element, and adherence_concern
honours the unverified fail-closed gate. Synthetic data only."""
import pytest

import ledger as ledger_mod
import mcs_signals
from ops_testkit import DAY, NOW, _msg
from test_mcs_signals import _extract_doc, _ev


@pytest.fixture
def led(tmp_path):
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    yield lg
    lg.db.close()


def _open_types(db):
    return {s["type"] for s in mcs_signals.current_open(db)["items"]}


def test_multi_profession_reply_counts_as_responder(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[{"to": "薬剤師", "action": "残薬確認"}])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2",
         prof="薬剤師, ケアマネジャー")
    _ev(led)
    assert "pharmacist_request_unanswered" not in _open_types(led.db)


def test_multi_station_reply_counts_as_responder(led):
    _msg(led.db, 1, ts=NOW - 4 * DAY)
    _extract_doc(led.db, 1, "h1",
                 requests=[{"to": "薬剤師", "action": "残薬確認"}])
    _msg(led.db, 2, ts=NOW - 3 * DAY, chash="h2",
         org="SYN第二薬局, SYN薬局")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"],
                          "self_professions": []}})
    assert "pharmacist_request_unanswered" not in _open_types(led.db)


def test_multi_station_self_post_excluded(led):
    _msg(led.db, 1, ts=NOW - DAY, body="飲み忘れが多い",
         org="SYN薬局, SYN第二薬局")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"]}})
    assert "adherence_concern" not in _open_types(led.db)
    # a station merely containing the name as a substring is not ours
    _msg(led.db, 2, pid=2, ts=NOW - DAY, chash="h2", body="飲み忘れが多い",
         org="SYN薬局東")
    _ev(led, {"signals": {"self_organizations": ["SYN薬局"]}})
    assert {s["project_id"] for s in mcs_signals.current_open(led.db)["items"]
            if s["type"] == "adherence_concern"} == {2}


@pytest.mark.parametrize("flag,opens", [
    (None, True), (False, True), (True, False), (1, False), ("", False)])
def test_adherence_meds_respect_unverified(led, flag, opens):
    med = {"name": "薬A", "action": None, "negated": True}
    if flag is not None:
        med["unverified"] = flag
    _msg(led.db, 1, ts=NOW - DAY, body="記録のみ")
    _extract_doc(led.db, 1, "h1", meds=[med])
    _ev(led)
    assert ("adherence_concern" in _open_types(led.db)) is opens
