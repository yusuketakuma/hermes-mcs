"""Commands-worker publish retry (F3 r1): a failed background publish of the same change
waits SNAPSHOT_MIN_INTERVAL_S without copying; urgent changes, tick, force and every
malformed or replaced state publish as before. Synthetic ledgers and a stub copier only."""
import json
import math
import os
import time

import pytest

import maintenance
import snapshots
from extract_testkit import _ledger, _message


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    db = _ledger(tmp_path)
    db.save_messages([_message(1)])
    copies = []
    outcome = {"ok": True}

    def publish(path, **kwargs):
        copies.append(path)
        return outcome["ok"]

    monkeypatch.setattr(maintenance, "publish_snapshot", publish)
    yield db, str(tmp_path / "ledger.db"), copies, outcome
    db.close()


def _state():
    with open(maintenance._snapshot_sig_path(), encoding="utf-8") as f:
        return json.load(f)


def _first_publish(path, now):
    """A real published copy and success marker to fail against."""
    assert maintenance.refresh_snapshot(path, now=now) == "published"
    open(os.path.join(maintenance.SNAPSHOT_DIR, "ledger-snapshot.db"), "wb").close()


def _fail_then(env, now, *, change=None):
    db, path, copies, outcome = env
    _first_publish(path, now)
    with db.db:   # an urgent change that the commands worker must try to publish
        db.db.execute("INSERT INTO command_receipts VALUES('c1','h',1,NULL,'applied','{}',?)", (now + 1,))
    outcome["ok"] = False
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 2, commands_retry=True) is None
    outcome["ok"] = True
    if change is not None:
        change(db)
    return len(copies)


def test_same_change_waits_without_copying_and_keeps_the_success_marker(env):
    db, path, copies, outcome = env
    now = time.time()
    before = _fail_then(env, now)
    success = {key: _state()[key] for key in ("sig", "published_at")}
    assert set(_state()["commands_retry"]) == {"failed_at", "failed_sig", "source_identity"}
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, commands_retry=True) == "retry_deferred"
    assert len(copies) == before                       # no copy started
    timing = snapshots.last_timing()
    assert timing["retry_reason"] == "snapshot_retry_cooldown"
    assert 0 < timing["retry_after_s"] <= maintenance.SNAPSHOT_MIN_INTERVAL_S
    assert {key: _state()[key] for key in ("sig", "published_at")} == success


def test_appended_background_rows_keep_waiting_without_extending_the_failure(env):
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now, change=lambda db: db.save_messages([_message(2)]))
    failed_at = _state()["commands_retry"]["failed_at"]
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, commands_retry=True) == "retry_deferred"
    assert _state()["commands_retry"]["failed_at"] == failed_at


def test_a_new_urgent_change_is_tried_at_once(env):
    db, path, copies, outcome = env
    now = time.time()

    def urgent(db):
        with db.db:
            db.db.execute("INSERT INTO command_receipts VALUES('c2','h',1,NULL,'applied','{}',?)", (now + 3,))
    before = _fail_then(env, now, change=urgent)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, commands_retry=True) == "published"
    assert len(copies) == before + 1
    assert "commands_retry" not in _state()          # a success removes the failure record


@pytest.mark.parametrize("caller", [{}, {"force": True, "commands_retry": True}])
def test_tick_and_force_publish_through_the_same_failure(env, caller):
    db, path, copies, outcome = env
    now = time.time()
    before = _fail_then(env, now)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, **caller) == "published"
    assert len(copies) == before + 1 and "commands_retry" not in _state()


@pytest.mark.parametrize("offset,expected", [
    (maintenance.SNAPSHOT_MIN_INTERVAL_S - 0.001, "retry_deferred"),
    (maintenance.SNAPSHOT_MIN_INTERVAL_S, "published"),
])
def test_the_wait_ends_exactly_at_the_interval(env, offset, expected):
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now)
    failed_at = _state()["commands_retry"]["failed_at"]
    assert maintenance.refresh_snapshot(path, dirty=True, now=failed_at + offset, commands_retry=True) == expected


@pytest.mark.parametrize("damage", [
    lambda s: s["commands_retry"].update(failed_at=True),
    lambda s: s["commands_retry"].update(failed_at=float("inf")),
    lambda s: s["commands_retry"].update(failed_at=s["commands_retry"]["failed_at"] + 100),   # future
    lambda s: s["commands_retry"].update(source_identity=[True, 1]),
    lambda s: s["commands_retry"].update(source_identity=[1, 2]),                             # replaced
    lambda s: s["commands_retry"].update(failed_sig={key: True for key in s["commands_retry"]["failed_sig"]}),
    lambda s: s.update(commands_retry="broken"),
    # a marker of the wrong type for its key must not match by == (1.0 == 1)
    lambda s: s["commands_retry"]["failed_sig"].update(
        {key: float(value) for key, value in s["commands_retry"]["failed_sig"].items()
         if type(value) is int and key not in ("", "requests@updated_at")}),
    lambda s: s["commands_retry"]["failed_sig"].update({"": float(s["commands_retry"]["failed_sig"][""])}),
    lambda s: s["commands_retry"]["failed_sig"].update({"": "1"}),
    lambda s: s["commands_retry"]["failed_sig"].update({"patients@content": "not-a-digest"}),
    lambda s: s["commands_retry"]["failed_sig"].update({"requests@updated_at": float("nan")}),
    lambda s: s["commands_retry"]["failed_sig"].update({"synthetic_extra_table": 1}),
])
def test_malformed_future_or_replaced_states_never_wait(env, damage):
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now)
    state = _state()
    damage(state)
    with open(maintenance._snapshot_sig_path(), "w", encoding="utf-8") as f:
        json.dump(state, f)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, commands_retry=True) == "published"
    assert snapshots.last_timing().get("retry_state") == "retry_state_not_applied"


def test_a_removed_row_is_not_a_background_append(env):
    db, path, copies, outcome = env
    now = time.time()
    with db.db:
        db.db.execute("INSERT INTO command_receipts VALUES('baseline','h',1,NULL,'applied','{}',?)", (now,))
    baseline = db.db.execute("SELECT MAX(rowid) FROM command_receipts").fetchone()[0]

    def removed(db):
        with db.db:
            db.db.execute("DELETE FROM command_receipts")   # MAX(rowid) falls
    before = _fail_then(env, now, change=removed)
    state = _state()
    assert state["sig"]["command_receipts"] == baseline
    assert state["commands_retry"]["failed_sig"]["command_receipts"] > baseline
    assert maintenance._content_signature(path)["command_receipts"] is None
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10, commands_retry=True) == "published"
    assert snapshots.last_timing()["retry_state"] == "retry_state_not_applied"
    assert len(copies) == before + 1


def test_a_failure_without_a_signature_or_the_fence_records_nothing(env, monkeypatch):
    db, path, copies, outcome = env
    now = time.time()
    _first_publish(path, now)
    with db.db:
        db.db.execute("INSERT INTO command_receipts VALUES('c1','h',1,NULL,'applied','{}',?)", (now + 1,))
    outcome["ok"] = False
    monkeypatch.setattr(snapshots, "_verified_publication_lock", lambda *a: False)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 2, commands_retry=True) is None
    assert "commands_retry" not in _state()
    monkeypatch.setattr(maintenance, "_content_signature", lambda *a, **k: None)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 3, commands_retry=True) is None
    assert "commands_retry" not in _state()


def test_a_failed_tick_publish_records_no_commands_wait(env):
    db, path, copies, outcome = env
    now = time.time()
    _first_publish(path, now)
    with db.db:
        db.db.execute("INSERT INTO command_receipts VALUES('c1','h',1,NULL,'applied','{}',?)", (now + 1,))
    outcome["ok"] = False
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 2) is None
    assert "commands_retry" not in _state()


def test_a_retry_record_write_failure_keeps_the_original_failure(env, monkeypatch):
    db, path, copies, outcome = env
    now = time.time()
    _first_publish(path, now)
    with db.db:
        db.db.execute("INSERT INTO command_receipts VALUES('c1','h',1,NULL,'applied','{}',?)", (now + 1,))
    outcome["ok"] = False

    def broken(*args, **kwargs):
        raise OSError("synthetic write failure")
    snapshots._reset_status()
    monkeypatch.setattr(maintenance, "atomic_write", broken)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 2, commands_retry=True) is None
    assert snapshots.last_failure().get("retry_state") == "retry_state_write_failed"
    assert "commands_retry" not in _state()


@pytest.mark.parametrize("left", [0.0001, 0.4999, None])   # None: the last representable instant
def test_a_deferral_close_to_the_end_still_reports_a_positive_wait(env, left):
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now)
    failed_at = _state()["commands_retry"]["failed_at"]
    end = failed_at + maintenance.SNAPSHOT_MIN_INTERVAL_S
    # an epoch near 1.8e9 cannot hold a sub-microsecond offset: take the float just before the end
    at = math.nextafter(end, -math.inf) if left is None else end - left
    assert maintenance.refresh_snapshot(path, dirty=True, now=at, commands_retry=True) == "retry_deferred"
    assert 0 < snapshots.last_timing()["retry_after_s"] <= maintenance.SNAPSHOT_MIN_INTERVAL_S


def test_stage_order_commands_fail_defer_then_tick_publishes_and_clears_the_record(env):
    """The order seen on 11-15: commands fails, commands defers, the tick then publishes;
    afterwards the commands worker follows the ordinary decisions again."""
    db, path, copies, outcome = env
    now = time.time()
    before = _fail_then(env, now)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 5, commands_retry=True) == "retry_deferred"
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10) == "published"     # the tick
    assert len(copies) == before + 1 and "commands_retry" not in _state()
    assert maintenance.refresh_snapshot(path, now=now + 15, commands_retry=True) == "unchanged"
    assert "retry_reason" not in snapshots.last_timing()        # the status was reset per call


@pytest.mark.parametrize("dirty", [False, True])
def test_code_without_the_option_decides_as_if_the_record_were_absent(env, dirty):
    """An old worker never passes commands_retry: the extra key changes none of its decisions."""
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now)
    with_record = _state()
    db.save_messages([_message(3)])
    stripped = {key: value for key, value in with_record.items() if key != "commands_retry"}
    decisions = []
    for state in (stripped, with_record):
        with open(maintenance._snapshot_sig_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
        decisions.append(maintenance.refresh_snapshot(path, dirty=dirty, now=now + 20))
        outcome["ok"] = True
    assert decisions[0] == decisions[1]


def test_a_failed_success_marker_reports_that_the_old_record_may_remain(env, monkeypatch):
    db, path, copies, outcome = env
    now = time.time()
    _fail_then(env, now)

    def broken(*args, **kwargs):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(maintenance, "atomic_write", broken)
    assert maintenance.refresh_snapshot(path, dirty=True, now=now + 10) == "published"
    timing = snapshots.last_timing()
    assert timing["marker_state"] == "unavailable" and timing["retry_state"] == "retry_state_not_cleared"
    assert "commands_retry" in _state()          # the publish result is not falsified
