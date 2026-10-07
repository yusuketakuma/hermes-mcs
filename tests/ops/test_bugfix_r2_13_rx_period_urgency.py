"""rx_period signals carry their source post as evidence.message_id; a
high-urgency extraction on that post must escalate them to immediate."""
import json

import ledger as ledger_mod
import mcs_signals
import pytest
from ops_testkit import NOW, _msg


@pytest.fixture
def led(tmp_path):
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    yield lg
    lg.db.close()


def test_rx_period_expiry_high_urgency_escalates(led):
    _msg(led.db, 1, body="本人の内服期間9/1-9/24。急変につき至急ご連絡ください。")
    led.db.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',1,?,?)",
        (json.dumps({"urgency": "high", "med_periods": [
            {"start": "2026-09-01", "end": "2026-09-24",
             "raw": "9/1-9/24"}]}), json.dumps({"hash": "h1"})))
    mcs_signals.evaluate(led, {"signals": {"notify": True}}, now=NOW)
    items = mcs_signals.current_open(led.db)["items"]
    assert [s["type"] for s in items] == ["rx_period_expiry"]
    pls = [json.loads(r[0]) for r in led.db.execute(
        "SELECT payload FROM notify_outbox ORDER BY event_id")]
    assert pls and all(p.get("urgent") for p in pls)
    assert not any(p.get("digest") for p in pls)
