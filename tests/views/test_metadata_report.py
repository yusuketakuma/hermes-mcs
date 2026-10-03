"""Count-only capture/shadow comparison report over a synthetic ledger."""
import hashlib
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

import metadata_report
from ledger import Ledger, LedgerReader
from mcs_signals import record_station_staff

NOW = 1_800_000_000.0
SCRIPT = Path(metadata_report.__file__)


def _stamp(type_, count=1, self_reacted=False):
    return {"type": type_, "count": count, "self_reacted": self_reacted}


def _row(db, mid, source, reactions=None, checked=NOW, observed=None,
         error=None, raw=None):
    content = raw if raw is not None else json.dumps(
        {} if reactions is None else
        {"reactions": {"value": reactions,
                       "observed_at": observed or checked}})
    db.execute("INSERT INTO message_metadata VALUES(?,?,?,?,?)",
               (mid, source, content, checked, error))


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "ledger.db"
    led = Ledger(str(path))
    with led.db:
        record_station_staff(led.db, [{"staff_id": 11, "is_self": True}])
        led.db.execute("INSERT INTO patients(project_id,is_archived,created_at,"
                       "last_seen) VALUES(1,0,?,?)", (NOW, NOW))
        for mid in range(1, 12):
            led.db.execute(
                "INSERT INTO messages(message_id,project_id,sender_id,"
                "posted_at_ts,body_text,body_state,content_hash,first_seen,"
                "updated_seen) VALUES(?,1,11,?,'synthetic','full','h',?,?)",
                (mid, NOW - 3600, NOW, NOW))
        a = [_stamp("accepted", 1, True)]
        _row(led.db, 1, "capture", a, checked=NOW - 7200)        # match,
        _row(led.db, 1, "shadow", a, checked=NOW - 3600)         # shadow newer
        _row(led.db, 2, "capture", a, checked=NOW - 60)          # mismatch,
        _row(led.db, 2, "shadow", [_stamp("accepted", 2, True)],  # capture newer
             checked=NOW - 120, observed=NOW - 120)
        _row(led.db, 3, "capture", a, checked=NOW - 900)         # self flag diff
        _row(led.db, 3, "shadow", [_stamp("accepted", 1, False)],
             checked=NOW - 600)
        _row(led.db, 4, "shadow", [_stamp("viewed")], checked=NOW - 600)
        _row(led.db, 5, "capture", a, checked=NOW - 900)         # not attempted
        _row(led.db, 6, "capture", a, checked=NOW - 900)
        _row(led.db, 6, "shadow", None, checked=NOW - 30 * 86400,
             error="network_error")
        _row(led.db, 7, "capture", a, checked=NOW - 900)
        _row(led.db, 7, "shadow", None, checked=NOW - 600,
             error="reactions_invalid,schema_error")
        _row(led.db, 8, "shadow", None, raw="{broken", checked=NOW - 600)
    led.close()
    return path


def _report(path):
    reader = LedgerReader(str(path))
    try:
        return metadata_report.build_report(reader, now=NOW)
    finally:
        reader.close()


def test_report_classifies_and_orients_time(db_path):
    rep = _report(db_path)
    assert rep["outcomes"] == {"match": 1, "mismatch": 2,
                               "shadow_failed": 2, "shadow_invalid": 1,
                               "shadow_not_attempted": 1, "shadow_only": 1}
    assert rep["count_diff_by_type"] == {"accepted": 1}
    assert rep["self_flag_diff_by_type"] == {"accepted": 1}
    assert rep["shadow_failures_by_code"] == {
        "network_error": 1, "reactions_invalid": 1, "schema_error": 1}
    assert rep["time_direction"] == {"match_shadow_newer": 1,
                                     "mismatch_capture_newer": 1,
                                     "mismatch_shadow_newer": 1}
    assert rep["capture_to_shadow_lag"] == {"1h_6h": 1, "lt_1h": 1,
                                             "shadow_before_capture": 1}
    # due: 5 and 9-11 never shadowed, 1 past the interval, 6 past backoff
    assert rep["watch"] == {"available": True, "due": 6,
                            "due_never_attempted": 4,
                            "oldest_shadow_check_age_s": 30 * 86400}
    assert "未読保持の証明ではありません" in rep["note"]


def test_report_has_no_identifiers_or_text(db_path):
    out = json.dumps(_report(db_path), ensure_ascii=False)
    assert "synthetic" not in out and "message_id" not in out
    assert "project" not in out
    assert "未読保持" in metadata_report.render_text(_report(db_path))


def test_report_never_writes(db_path):
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    _report(db_path)
    reader = LedgerReader(str(db_path))
    with pytest.raises(sqlite3.OperationalError):
        reader.db.execute("DELETE FROM message_metadata")
    reader.close()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before


def test_cli_runs_against_db(db_path):
    for flags in (["--json"], []):
        res = subprocess.run([sys.executable, str(SCRIPT), "--db", str(db_path),
                              *flags], capture_output=True, text=True, check=True)
        assert "未読保持の証明ではありません" in res.stdout
    data = json.loads(subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(db_path), "--json"],
        capture_output=True, text=True, check=True).stdout)
    assert data["messages"] == 8 and data["as_of"] <= time.time() + 1
