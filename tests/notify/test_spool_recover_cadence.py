"""recover() cadence: the scan cost must not be paid every commands pass,
and the interval state must survive a restart (it lives in the ledger).
All fixtures synthetic."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import notify_spool
from notify_testkit import CFG, led as led  # noqa: F401  (fixture)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _last_run(led):
    row = led.db.execute(
        "SELECT last_run_at FROM notify_recovery_state WHERE singleton=1").fetchone()
    return row[0] if row else None


def test_first_recover_run_records_the_cadence_row(led):
    assert _last_run(led) is None
    fixed = notify_spool.recover(led, CFG, {})
    assert "deferred" not in fixed
    assert _last_run(led) is not None


def test_immediate_second_run_is_deferred_without_scanning(led):
    notify_spool.recover(led, CFG, {})
    fixed = notify_spool.recover(led, CFG, {})
    assert fixed["deferred"] == "interval"
    assert fixed["republished"] == 0


def test_cadence_state_survives_a_restart(led):
    notify_spool.recover(led, CFG, {})
    path = led.db.execute("PRAGMA database_list").fetchone()[2]
    led.close()
    import ledger as ledger_mod
    reopened = ledger_mod.Ledger(path)
    try:
        fixed = notify_spool.recover(reopened, CFG, {})
        assert fixed["deferred"] == "interval"
    finally:
        reopened.close()


def test_backdated_run_executes_again(led):
    notify_spool.recover(led, CFG, {})
    with led.db:
        led.db.execute(
            "UPDATE notify_recovery_state SET last_run_at=? WHERE singleton=1",
            (time.time() - notify_spool.RECOVER_INTERVAL_S - 1,))
    fixed = notify_spool.recover(led, CFG, {})
    assert "deferred" not in fixed
