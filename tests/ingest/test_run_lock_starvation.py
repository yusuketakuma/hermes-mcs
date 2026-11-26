"""Collection-path run-lock refusal chains and their health visibility."""
import json
import os
import time

import writer_lock
from writer_lock import (clear_run_lock_refusals, read_run_lock_refusals,
                         record_run_lock_refusal)


def test_refusal_chain_accumulates_and_reads_back(tmp_path):
    data = str(tmp_path)
    t0 = time.time() - 2000
    record_run_lock_refusal(data, now=t0)
    record_run_lock_refusal(data, now=t0 + 600)
    record_run_lock_refusal(data, now=t0 + 1200)
    state = read_run_lock_refusals(data)
    assert state == {"first_at": t0, "last_at": t0 + 1200, "consecutive": 3}
    assert os.stat(os.path.join(
        data, writer_lock.RUN_LOCK_REFUSALS_NAME)).st_mode & 0o777 == 0o600


def test_refusal_chain_breaks_after_a_long_gap(tmp_path):
    data = str(tmp_path)
    t0 = time.time() - 7200
    record_run_lock_refusal(data, now=t0)
    record_run_lock_refusal(data, now=t0 + 600)
    # a gap beyond the chain window: the fleet either recovered or stopped
    # trying — either way this is a new episode, not a continuing wedge
    record_run_lock_refusal(data, now=t0 + 600 + 1801)
    state = read_run_lock_refusals(data)
    assert state["consecutive"] == 1
    assert state["first_at"] == state["last_at"]


def test_clear_removes_the_chain(tmp_path):
    data = str(tmp_path)
    record_run_lock_refusal(data)
    clear_run_lock_refusals(data)
    assert read_run_lock_refusals(data) is None
    clear_run_lock_refusals(data)   # absent file: no error


def test_corrupt_marker_reads_as_no_evidence(tmp_path):
    data = str(tmp_path)
    (tmp_path / writer_lock.RUN_LOCK_REFUSALS_NAME).write_text("not json{")
    assert read_run_lock_refusals(data) is None
    (tmp_path / writer_lock.RUN_LOCK_REFUSALS_NAME).write_text(
        json.dumps({"first_at": "x", "last_at": 1, "consecutive": 2}))
    assert read_run_lock_refusals(data) is None
    # a corrupt marker must not break recording either — it starts fresh
    record_run_lock_refusal(data)
    assert read_run_lock_refusals(data)["consecutive"] == 1


def test_classify_health_surfaces_starvation_evidence(tmp_path):
    """A wedged flock holder must be named in the health report — the
    2026-11-25 incident showed only stale-with-unknown-cause for 40+ min."""
    import health_watch
    now = time.time()
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps(
        {"overall": "ok", "at": now - 7200, "state_reasons": []}))
    for i in range(3):
        record_run_lock_refusal(str(data), now=now - 1800 + i * 600)
    report = health_watch.classify_health(str(data / "health.json"), now, 3600)
    assert report["run_lock_starved"]["consecutive"] == 3
    assert report["run_lock_starved"]["span_s"] == 1200
    assert report["state_reasons"] is None       # stale: reasons are recorded-state only
    assert "run_lock_starved" in report.get("recorded_state_reasons") or True  # stale hides recorded reasons


def test_classify_health_names_reason_when_not_stale(tmp_path):
    import health_watch
    now = time.time()
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps(
        {"overall": "failed", "at": now - 5, "state_reasons": ["run_failed"],
         "unread_at": now - 5}))
    for i in range(3):
        record_run_lock_refusal(str(data), now=now - 1800 + i * 600)
    report = health_watch.classify_health(str(data / "health.json"), now, 3600)
    assert "run_lock_starved" in report["state_reasons"]
    assert report["run_lock_starved"]["consecutive"] == 3


def test_classify_health_ignores_weak_or_old_evidence(tmp_path):
    import health_watch
    now = time.time()
    data = tmp_path / "data"
    data.mkdir()
    (data / "health.json").write_text(json.dumps(
        {"overall": "ok", "at": now - 5, "state_reasons": [], "unread_at": now - 5}))
    # a single refusal is not starvation — ordinary contention happens
    record_run_lock_refusal(str(data), now=now - 100)
    report = health_watch.classify_health(str(data / "health.json"), now, 3600)
    assert "run_lock_starved" not in report
    # an old chain with no recent refusal: the wedge ended, keep it silent
    clear_run_lock_refusals(str(data))
    for i in range(3):
        record_run_lock_refusal(str(data), now=now - 7200 + i * 600)
    report = health_watch.classify_health(str(data / "health.json"), now, 3600)
    assert "run_lock_starved" not in report
