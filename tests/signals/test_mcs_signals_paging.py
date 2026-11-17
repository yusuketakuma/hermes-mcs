"""Pin that the remaining whole-result-set reads in mcs_signals and
mcs_queries stream through a cursor (bounded live rows) without changing
output or ordering (synthetic data only; no wall-clock or RSS checks)."""
import gc
import json

import pytest

import mcs_queries
import mcs_signals
import signal_policy
from ops_testkit import DAY, NOW, _extract_v1, _msg
from signal_detector_testkit import _extract_doc
from signal_detector_testkit import led as led  # noqa: F401

N = 300
ROOMS = 7
TODAY_END = "2026-09-21"          # NOW is 2026-09-21 JST


def _end(offset_days):
    from datetime import date, timedelta
    return (date.fromisoformat(TODAY_END) + timedelta(days=offset_days)).isoformat()


def _pid(mid):
    return 1 + mid % ROOMS


def _seed(db):
    """N posts over 7 rooms. Each carries a med period ending 0..19 days
    ahead (expiry candidates), an open request (overdue or aged) and a
    discharge event; every 4th post is a registered request source."""
    for mid in range(1, N + 1):
        _msg(db, mid, pid=_pid(mid), ts=NOW - (5 + mid % 4) * DAY,
             chash=f"h{mid}", body="退院しました。")
        _extract_v1(db, mid, f"h{mid}", [
            {"start": _end(-30), "end": _end(mid % 20), "raw": f"p{mid}"}])
        _extract_doc(db, mid, f"h{mid}", events=["discharge"])
        db.execute(
            "INSERT INTO requests(project_id,source_message_id,source_hash,"
            "title,status,due_date,revision,created_at,updated_at) "
            "VALUES (?,?,?,'t','open',?,1,?,?)",
            (_pid(mid), mid if mid % 4 == 0 else 10_000 + mid, "h" * 64,
             _end(-1 - mid % 5), NOW - (10 + mid % 60) * DAY, NOW))
    for k in range(3):                # lapsed-only rooms (period ended days ago)
        mid = 5000 + k
        _msg(db, mid, pid=50 + k, ts=NOW - 20 * DAY, chash=f"h{mid}")
        _extract_v1(db, mid, f"h{mid}", [
            {"start": _end(-40), "end": _end(-2 - k), "raw": f"q{mid}"}])
    db.commit()


@pytest.fixture
def seeded(led):  # noqa: F811
    _seed(led.db)
    return led, signal_policy.resolved_thresholds(led.db)


class _Rows(tuple):
    live = peak = 0

    def __new__(cls, cursor, values):
        row = super().__new__(cls, values)
        row._names = [d[0] for d in cursor.description]
        cls.live += 1
        cls.peak = max(cls.peak, cls.live)
        return row

    def __getitem__(self, key):
        if isinstance(key, str):
            key = self._names.index(key)
        return tuple.__getitem__(self, key)

    def keys(self):
        return list(self._names)

    def __del__(self):
        type(self).live -= 1


def _peak_rows(db, fn):
    gc.collect()
    _Rows.live = _Rows.peak = 0
    old = db.row_factory
    db.row_factory = _Rows
    try:
        out = fn()
    finally:
        db.row_factory = old
    gc.collect()
    return out, _Rows.peak


def _fetchall_period_rows(db):
    """Reference: the pre-change fetchall of the same statement."""
    return db.execute(
        "SELECT m.project_id, m.message_id, a.content "
        "FROM artifacts a JOIN messages m ON m.message_id=a.message_id "
        "WHERE a.kind='extract_v1'"
        f"{mcs_queries.current_extract_pred(error_check=False)} "
        f"AND json_array_length({mcs_queries.json_or_null('a.content')},"
        "'$.med_periods')>0").fetchall()


def test_med_period_artifacts_same_rows_and_order(seeded):
    led, _ = seeded
    got = [tuple(r) for r in mcs_queries.med_period_artifacts(led.db)]
    want = [tuple(r) for r in _fetchall_period_rows(led.db)]
    assert len(got) == N + 3 and got == want


def test_med_period_artifacts_holds_bounded_rows(seeded):
    led, _ = seeded
    n, peak = _peak_rows(led.db, lambda: sum(
        1 for _ in mcs_queries.med_period_artifacts(led.db)))
    assert n == N + 3 and peak <= 8, peak


@pytest.mark.parametrize("name", ["_rx_period_expiry", "_rx_period_lapsed",
                                  "_request_overdue", "_request_aging",
                                  "_discharge_notice"])
def test_detectors_hold_bounded_rows(seeded, name):
    led, th = seeded
    found, peak = _peak_rows(
        led.db, lambda: list(getattr(mcs_signals, name)(led.db, NOW, th, {})))
    assert found
    assert peak <= 8 + (256 if name == "_discharge_notice" else 0), peak


def test_discharge_notice_same_with_list_or_cursor_feed(seeded, monkeypatch):
    """The checker gets the cursor; a list feed gives the same signals in
    the same order (the error-order pin is in test_signal_response_queries)."""
    led, th = seeded
    got = list(mcs_signals._discharge_notice(led.db, NOW, th, {}))
    real = mcs_signals._engagement_checker
    monkeypatch.setattr(mcs_signals, "_engagement_checker",
                        lambda db, rows, p, o: real(db, list(rows), p, o))
    want = list(mcs_signals._discharge_notice(led.db, NOW, th, {}))
    assert got and got == want


def test_latest_signal_states_equivalent_and_cursor_read(seeded):
    led, th = seeded
    mcs_signals.evaluate(led, {}, now=NOW)
    mcs_signals.evaluate(led, {}, now=NOW + DAY)     # superseding/resolved rows
    got = mcs_signals.latest_signal_states(led.db)
    want = {}
    for pid, content_s, meta_s in led.db.execute(
            "SELECT project_id, content, meta FROM artifacts "
            "WHERE kind='signal_v1' ORDER BY artifact_id"):
        meta = json.loads(meta_s)
        want[meta["key"]] = (content_s, pid)
    want = {k: mcs_signals.signal_content(*v) for k, v in want.items()}
    assert got == want and len(got) > N
    _, peak = _peak_rows(led.db, lambda: mcs_signals.latest_signal_states(led.db))
    assert peak <= 8, peak
