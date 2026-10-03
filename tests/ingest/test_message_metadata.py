"""Synthetic metadata capture and shadow isolation contracts."""

import json
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from ledger import Ledger, valid_mcs_db
from mcs_adapter import MCSAdapter, MCSError, SchemaError, _norm_message
from mcs_signals import record_station_staff
from message_metadata import get_message_metadata
from run_check import stage_metadata_shadow
import run_check


def message(mid=1, **extra):
    return _norm_message(
        {
            "id": mid,
            "comment": "fictional text",
            "user": {"id": 11},
            "created_at": "2026-10-03T00:00:00+09:00",
            **extra,
        },
        1,
    )


def save(store, m):
    store.ensure_patient(1)
    return store.save_messages([m], project_id=1, notify=False)


def shadow_adapter(fetch):
    return SimpleNamespace(fetch_message_metadata=fetch, set_deadline=lambda deadline: None)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        [{"type": "viewed", "count": True, "self_reacted": False}],
        [{"type": "viewed", "count": -1, "self_reacted": False}],
        [{"type": "viewed", "count": 0, "self_reacted": True}],
        [{"type": "all", "count": 1, "self_reacted": False}],
        [{"type": "good", "count": 1, "self_reacted": "true"}],
    ],
)
def test_invalid_optional_metadata_keeps_chat(value):
    m = message(reactions=value, is_bookmarked=False)
    assert m.body_state == "full" and m.body_html == "fictional text"
    assert m.metadata_errors == ["reactions_invalid"]
    assert m.metadata == {"is_bookmarked": False}


@pytest.mark.parametrize("extra", [
    {"reactions": None},
    {"reactions": None, "mentions": {}},
    {"reactions": [{"type": "viewed", "count": True, "self_reacted": False}]},
])
def test_initial_invalid_reactions_are_not_unfetched(tmp_path, extra):
    db = Ledger(str(tmp_path / "ledger.db"))
    save(db, message(**extra))
    metadata = get_message_metadata(db.db, 1)
    assert metadata["reactions_status"] == "invalid"
    assert metadata["reactions"] is None
    assert metadata["last_error"] is not None
    db.close()


@pytest.mark.parametrize("mention", [
    {"type": "project"}, {"type": "project", "project": {"id": 1}},
])
def test_room_wide_mention_uses_current_project_id(mention):
    m = message(mentions=[mention])
    assert m.metadata == {"mentions": [{"type": "project", "id": 1}]}
    assert m.metadata_errors == []


@pytest.mark.parametrize("value", [True, 1.0, 0, 2])
def test_room_wide_mention_rejects_invalid_or_other_project_id(value):
    m = message(mentions=[{"type": "project", "project": {"id": value}}])
    assert "mentions" not in m.metadata
    assert m.metadata_errors == ["mentions_invalid"]


def test_capture_restart_and_hash_unchanged(tmp_path):
    path = tmp_path / "ledger.db"
    db = Ledger(str(path))
    save(db, message())
    assert get_message_metadata(db.db, 1)["reactions"] is None
    before = dict(db.db.execute("SELECT * FROM messages").fetchone())
    jobs = list(db.db.execute("SELECT * FROM fetch_jobs"))
    save(
        db,
        message(
            reactions=[],
            mentions=[{"type": "user", "user": {"id": 12, "name": "discard"}}],
            is_bookmarked=False,
            is_pinned=True,
        ),
    )
    assert get_message_metadata(db.db, 1)["reactions"] == []
    assert (
        db.db.execute("SELECT content_hash FROM messages").fetchone()[0]
        == before["content_hash"]
    )
    assert list(db.db.execute("SELECT * FROM fetch_jobs")) == jobs
    content = json.loads(
        db.db.execute("SELECT content FROM message_metadata").fetchone()[0]
    )
    assert content["mentions"]["value"] == [{"type": "user", "id": 12}]
    assert db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    save(
        db,
        message(reactions=[{"type": "future_kind", "count": 2, "self_reacted": True}]),
    )
    save(db, message())  # missing key must preserve last observation and age
    observed = get_message_metadata(db.db, 1)
    db.close()
    db = Ledger(str(path))
    assert get_message_metadata(db.db, 1) == observed
    db.close()


def test_unchanged_recapture_keeps_observed_at(tmp_path, monkeypatch):
    """Regression: every unread re-capture refreshed observed_at, so the
    card footer changed each tick and digests recounted old reactions."""
    import ledger
    clock = [1_000_000.0]
    monkeypatch.setattr(ledger.time, "time", lambda: clock[0])
    db = Ledger(str(tmp_path / "ledger.db"))
    seen = [{"type": "viewed", "count": 1, "self_reacted": True}]
    save(db, message(reactions=seen, is_pinned=False))
    first = get_message_metadata(db.db, 1)["reactions_observed_at"]
    clock[0] += 300
    save(db, message(reactions=seen, is_pinned=False))
    assert get_message_metadata(db.db, 1)["reactions_observed_at"] == first
    clock[0] += 300
    save(db, message(reactions=seen + [
        {"type": "good", "count": 1, "self_reacted": False}]))
    assert get_message_metadata(db.db, 1)["reactions_observed_at"] == first + 600
    db.close()


def test_shadow_never_changes_source_or_capture(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    save(db, message(reactions=[]))
    before = list(db.db.execute("SELECT * FROM messages"))
    db.save_metadata_shadow(
        message(reactions=[{"type": "accepted", "count": 1, "self_reacted": True}])
    )
    assert list(db.db.execute("SELECT * FROM messages")) == before
    assert get_message_metadata(db.db, 1)["reactions"] == []
    db.save_metadata_shadow(message(), error="network_error")
    row = db.db.execute(
        "SELECT * FROM message_metadata WHERE source='shadow'"
    ).fetchone()
    assert row["last_error"] == "network_error"
    assert json.loads(row["content"])["reactions"]["value"][0]["count"] == 1
    with pytest.raises(ValueError, match="message_project_mismatch"):
        db.save_metadata_shadow(_norm_message({"id": 1}, 2))
    db.close()


def test_additive_migration_snapshot_and_recovery(tmp_path):
    path = tmp_path / "ledger.db"
    db = Ledger(str(path))
    save(db, message())
    db.close()
    old = sqlite3.connect(path)
    old.execute("DROP TABLE message_metadata")
    old.execute("PRAGMA user_version=7")
    old.commit()
    old.close()
    db = Ledger(str(path))
    save(db, message(reactions=[]))
    snapshot = tmp_path / "snapshot.db"
    db.db.backup(con := sqlite3.connect(snapshot))
    con.execute("PRAGMA journal_mode=DELETE")
    con.close()
    assert valid_mcs_db(str(snapshot))
    con = sqlite3.connect(snapshot)
    con.execute("DROP TABLE message_metadata")
    con.commit()
    con.close()
    assert not valid_mcs_db(str(snapshot))
    assert (
        db.db.execute("SELECT body_html FROM messages").fetchone()[0]
        == "fictional text"
    )
    db.close()


def test_exact_refresh_is_get_only_and_preserves_read_and_session():
    adapter = MCSAdapter()
    seen = []

    def get(path, params=None, extend_session=True):
        seen.append((path, params, extend_session))
        return {"messages": [{"id": 1, "reactions": []}]}

    adapter._get = get
    assert adapter.fetch_message_metadata(1, 1).metadata == {"reactions": []}
    assert seen == [
        (
            "/projects/1/messages",
            {"message_id": 1, "per_page": 1, "keep_read_status": 1},
            False,
        )
    ]
    adapter._get = lambda *a, **kw: {"messages": [{"id": 2}]}
    with pytest.raises(SchemaError, match="target mismatch"):
        adapter.fetch_message_metadata(1, 1)


def test_exact_reply_refresh_uses_thread_route_and_preserves_parent():
    adapter = MCSAdapter()
    seen = []

    def get(path, params=None, extend_session=True):
        seen.append((path, params, extend_session))
        if path == "/projects/1/messages":
            raise MCSError("http_error", status=422)
        return {"messages": [{"id": 2, "project_id": 1, "reactions": []}]}

    adapter._get = get
    with pytest.raises(MCSError) as error:
        adapter.fetch_message_metadata(1, 2)
    assert error.value.status == 422
    reply = adapter.fetch_message_metadata(1, 2, parent_id=1)
    assert reply.message_id == 2 and reply.project_id == 1 and reply.parent_id == 1
    assert reply.metadata == {"reactions": []}
    assert seen[-1] == (
        "/projects/1/messages/1/messages",
        {"message_id": 2, "per_page": 1, "keep_read_status": 1}, False)


@pytest.mark.parametrize("parent_id", [True, 1.0, 0, -1, 2])
def test_exact_reply_refresh_rejects_invalid_parent_before_get(parent_id):
    adapter = MCSAdapter()
    adapter._get = lambda *args, **kwargs: pytest.fail("invalid parent caused a GET")
    with pytest.raises(SchemaError, match="invalid parent id"):
        adapter.fetch_message_metadata(1, 2, parent_id=parent_id)


@pytest.mark.parametrize("extra", [{"id": 3}, {"project_id": 2}])
def test_exact_reply_refresh_rejects_wrong_message_or_project(extra):
    adapter = MCSAdapter()
    item = {"id": 2, "project_id": 1, "reactions": [], **extra}
    adapter._get = lambda *args, **kwargs: {"messages": [item]}
    with pytest.raises(SchemaError, match="target mismatch"):
        adapter.fetch_message_metadata(1, 2, parent_id=1)


@pytest.mark.parametrize(("field", "value"), [
    ("id", True), ("id", 1.0), ("project_id", True), ("project_id", 1.0),
])
def test_exact_refresh_rejects_non_integer_target(field, value):
    adapter = MCSAdapter()
    item = {"id": 1, "project_id": 1, "reactions": [], field: value}
    adapter._get = lambda *args, **kwargs: {"messages": [item]}
    with pytest.raises(SchemaError, match="target mismatch"):
        adapter.fetch_message_metadata(1, 1)


def test_watch_set_is_bounded_fair_and_failure_backs_off(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in range(1, 8):
        save(db, message(mid))
    now = time.time()
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (now,))
    assert len(db.metadata_watch_targets()) == 5
    calls = []

    def fetch(pid, mid):
        calls.append(mid)
        if mid == 1:
            raise MCSError("network_error", retryable=True)
        return message(mid, reactions=[])

    result = {}
    stage_metadata_shadow(
        shadow_adapter(fetch), db, result, time.monotonic() + 60
    )
    assert calls == [1, 2, 3, 4, 5]
    assert result["metadata_shadow"]["due"] == 7
    assert result["metadata_shadow"]["deferred"] == 2
    assert result["metadata_shadow"]["fetched"] == 4
    assert result["metadata_shadow"]["errors"] == [
        {"message_id": 1, "kind": "network_error"}
    ]
    assert [r["message_id"] for r in db.metadata_watch_targets()] == [6, 7]
    calls.clear()
    stage_metadata_shadow(
        shadow_adapter(fetch), db, {}, time.monotonic() + 10
    )
    assert calls == []
    db.close()


def test_missing_shadow_fields_do_not_starve_later_targets(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in range(1, 8):
        save(db, message(mid))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
    stage_metadata_shadow(
        shadow_adapter(lambda pid, mid: message(mid)),
        db,
        {},
        time.monotonic() + 60,
    )
    assert [r["message_id"] for r in db.metadata_watch_targets()] == [6, 7]
    assert get_message_metadata(db.db, 1)["reactions"] is None
    db.close()


def test_shadow_expiry_stops_further_optional_gets(tmp_path):
    from mcs_adapter import SessionExpired

    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in range(1, 4):
        save(db, message(mid))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
    calls = []

    def fetch(pid, mid):
        calls.append(mid)
        raise SessionExpired()

    result = {}
    stage_metadata_shadow(
        shadow_adapter(fetch), db, result, time.monotonic() + 60
    )
    assert calls == [1]
    assert result["metadata_shadow"]["deferred"] == 2
    db.close()


def test_shadow_schema_error_then_expiry_counts_remaining_targets(tmp_path):
    from mcs_adapter import SessionExpired

    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in range(1, 6):
        save(db, message(mid))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
    calls = []

    def fetch(pid, mid):
        calls.append(mid)
        if mid == 1:
            return message(mid, reactions=None)
        raise SessionExpired()

    result = {}
    stage_metadata_shadow(
        shadow_adapter(fetch), db, result, time.monotonic() + 60
    )
    assert calls == [1, 2]
    stats = result["metadata_shadow"]
    assert (stats["due"], stats["fetched"], stats["deferred"]) == (5, 1, 3)
    assert stats["errors"] == [{"message_id": 1, "kind": "schema_error"},
                               {"message_id": 2, "kind": "session_expired"}]
    assert stats["deferred_reasons"] == {"session_expired": 3}
    db.close()


def test_capture_metadata_preserves_semantic_generation_and_projections(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    db.ensure_patient(1)
    db.save_messages([message()], project_id=1, semantic=True)
    chash = db.db.execute("SELECT content_hash FROM messages").fetchone()[0]
    db.artifact_add("canonical_projection", '{"requests":[]}', project_id=1,
                    message_id=1, meta={"hash": chash})
    generation = db._semantic_source_generation(1, 1)
    jobs = [dict(row) for row in db.db.execute("SELECT * FROM fetch_jobs")]
    artifacts = [dict(row) for row in db.db.execute("SELECT * FROM artifacts")]
    db.save_messages([message(reactions=[])], project_id=1, semantic=True)
    assert db._semantic_source_generation(1, 1) == generation
    assert [dict(row) for row in db.db.execute("SELECT * FROM fetch_jobs")] == jobs
    assert [dict(row) for row in db.db.execute("SELECT * FROM artifacts")] == artifacts
    assert db.db.execute("SELECT COUNT(*) FROM notify_outbox").fetchone()[0] == 0
    db.close()


def test_watch_signal_latest_state_and_patient_message_boundaries(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in range(1, 7):
        save(db, message(mid))
    now = time.time()
    with db.db:
        db.db.execute("UPDATE messages SET sender_id=12,posted_at_ts=?", (now - 8 * 86400,))
        db.db.execute("UPDATE messages SET parent_id=3 WHERE message_id=1")
        db.db.execute("UPDATE messages SET sender_id=11,posted_at_ts=? WHERE message_id=3",
                      (now - 7 * 86400,))
        db.db.execute("UPDATE messages SET sender_id=11,posted_at_ts=? WHERE message_id=4",
                      (now - 7 * 86400 - 1,))
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=5")
        db.ensure_patient(2)
        db.db.execute("UPDATE patients SET is_archived=1 WHERE project_id=2")
        db.db.execute("UPDATE messages SET project_id=2 WHERE message_id=6")
    for mid in (1, 2, 5, 6):
        db.artifact_add("signal_v1", json.dumps({
            "type": "pharmacist_request_unanswered", "state": "open",
            "evidence": {"message_ids": [mid]}}),
            project_id=2 if mid == 6 else 1, meta={"key": f"synthetic:{mid}"})
    db.artifact_add("signal_v1", '{"state":"resolved"}', project_id=1,
                    meta={"key": "synthetic:2"})
    assert [(row["message_id"], row["parent_id"])
            for row in db.metadata_watch_targets(now=now)] == [(1, 3), (3, None)]
    db.close()


def test_shadow_routes_root_and_signal_reply_by_parent_id(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in (1, 2):
        save(db, message(mid))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
        db.db.execute("UPDATE messages SET sender_id=12,parent_id=1 WHERE message_id=2")
    db.artifact_add("signal_v1", json.dumps({
        "type": "pharmacist_request_unanswered", "state": "open",
        "evidence": {"message_ids": [2]}}), project_id=1, meta={"key": "synthetic:reply"})
    before = [dict(row) for row in db.db.execute("SELECT * FROM messages ORDER BY message_id")]
    calls = []

    def fetch(pid, mid, **kwargs):
        calls.append((pid, mid, kwargs))
        return message(mid, reactions=[])

    result = {}
    stage_metadata_shadow(shadow_adapter(fetch), db, result, time.monotonic() + 60)
    assert calls == [(1, 1, {}), (1, 2, {"parent_id": 1})]
    assert result["metadata_shadow"]["fetched"] == 2
    assert [dict(row) for row in db.db.execute("SELECT * FROM messages ORDER BY message_id")] == before
    assert [row[0] for row in db.db.execute(
        "SELECT message_id FROM message_metadata WHERE source='shadow' ORDER BY message_id")] == [1, 2]
    db.close()


def test_shadow_success_poll_and_failure_backoff_are_distinct(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in (1, 2):
        save(db, message(mid))
    now = time.time()
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (now,))
        db.db.executemany(
            "INSERT INTO message_metadata(message_id,source,content,checked_at,last_error) "
            "VALUES(?,'shadow','{}',?,?)",
            [(1, now, None), (2, now, "network_error")])
    assert db.metadata_watch_targets(now=now + 1799) == []
    assert [row["message_id"] for row in db.metadata_watch_targets(now=now + 1800)] == [1]
    assert [row["message_id"] for row in db.metadata_watch_targets(now=now + 21599)] == [1]
    assert [row["message_id"] for row in db.metadata_watch_targets(now=now + 21600)] == [1, 2]
    db.close()


@pytest.mark.parametrize(("remaining", "budget", "reason"), [
    (60, 25, "budget_exhausted"), (35, 5, "deadline_margin"), (20, 0, "deadline_margin"),
])
def test_shadow_uses_bounded_deadline_and_restores_run_deadline(
        tmp_path, monkeypatch, remaining, budget, reason):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    for mid in (1, 2):
        save(db, message(mid))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
    clock = [100.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])
    deadlines, calls = [], []

    def fetch(pid, mid):
        calls.append(mid)
        assert deadlines[-1] == min(100 + remaining - 30, 125)
        clock[0] += budget
        return message(mid, reactions=[])

    adapter = SimpleNamespace(fetch_message_metadata=fetch, set_deadline=deadlines.append)
    result = {}
    stage_metadata_shadow(adapter, db, result, 100 + remaining)
    stats = result["metadata_shadow"]
    assert deadlines == [min(100 + remaining - 30, 125), 100 + remaining]
    assert calls == ([1] if budget else [])
    assert stats["budget_s"] == stats["elapsed_s"] == budget
    assert stats["deferred_reasons"] == {reason: 1 if budget else 2}
    db.close()


def test_shadow_restores_deadline_after_unexpected_failure(tmp_path):
    db = Ledger(str(tmp_path / "ledger.db"))
    with db.db:
        record_station_staff(db.db, [{"staff_id": 11, "is_self": True}])
    save(db, message())
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))
    deadlines = []

    def fetch(pid, mid):
        raise RuntimeError("synthetic failure")

    deadline = time.monotonic() + 60
    adapter = SimpleNamespace(fetch_message_metadata=fetch, set_deadline=deadlines.append)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        stage_metadata_shadow(adapter, db, {}, deadline)
    assert deadlines[-1] == deadline
    db.close()


@pytest.mark.parametrize(("cfg", "manual", "expected"), [
    ({}, False, "disabled"), ({}, True, "shadow"),
    ({"metadata_shadow": True}, False, "shadow"),
    ({"metadata_shadow": "true"}, False, "config_invalid"),
    ({"metadata_shadow": 1}, True, "config_invalid"),
])
def test_shadow_config_and_manual_selection(monkeypatch, cfg, manual, expected):
    monkeypatch.setattr(run_check, "_code_changed", lambda result: False)
    monkeypatch.setattr(run_check, "stage_metadata_shadow", lambda adapter, store, result, deadline,
                        **kwargs: result.update(metadata_shadow={"mode": "shadow"}))
    result = {"errors": []}
    store = SimpleNamespace()
    run_check._run_metadata_shadow(None, store, result, 100, cfg, manual=manual)
    assert result["metadata_shadow"] == ({"mode": "shadow"} if expected == "shadow"
                                         else {"mode": "off", "reason": expected})


@pytest.mark.parametrize("jobs_only", [False, True])
@pytest.mark.parametrize("enabled", [False, True])
def test_scheduled_tick_and_deep_share_shadow_after_semantic(
        tmp_path, monkeypatch, capsys, jobs_only, enabled):
    monkeypatch.setattr(sys, "argv", ["run_check"] + (["--jobs-only"] if jobs_only else []))
    for key, value in {"HOME": str(tmp_path), "DB": str(tmp_path / "data/ledger.db"),
                       "ATTACH_DIR": str(tmp_path / "data/attachments"),
                       "LOCKFILE": str(tmp_path / "data/run.lock")}.items():
        monkeypatch.setattr(run_check, key, value)
    adapter = shadow_adapter(lambda pid, mid: message(mid))
    monkeypatch.setattr(run_check, "MCSAdapter", lambda **kwargs: adapter)
    monkeypatch.setattr(run_check, "_config", lambda: {"metadata_shadow": enabled})
    monkeypatch.setattr(run_check, "_semantic_enabled", lambda *args: False)
    monkeypatch.setattr(run_check, "_code_changed", lambda result: False)
    events = []
    for name in ("_stage_fetch", "_run_jobs", "stage_derive", "_with_relogin",
                 "_housekeeping", "_write_health"):
        monkeypatch.setattr(run_check, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(run_check, "_deliver", lambda *args: events.append("delivery"))
    monkeypatch.setattr(run_check, "_run_semantic", lambda *args, **kwargs: events.append("semantic"))

    def shadow(adapter, store, result, deadline, **kwargs):
        events.append("shadow")
        result["metadata_shadow"] = {"mode": "shadow"}

    monkeypatch.setattr(run_check, "stage_metadata_shadow", shadow)
    monkeypatch.setattr(run_check, "_finish_run", lambda *args: "ok")
    assert run_check._main() == 0
    result = json.loads(capsys.readouterr().out)
    assert events == ["delivery", "semantic"] + (["shadow"] if enabled else [])
    assert result["metadata_shadow"]["mode"] == ("shadow" if enabled else "off")


def test_health_shadow_contains_safe_counts_without_message_ids(tmp_path, monkeypatch):
    db = Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(run_check, "_free_mb", lambda: 10000)
    shadow = {"mode": "shadow", "due": 5, "fetched": 1, "deferred": 3,
              "budget_s": 25, "elapsed_s": 5, "deferred_reasons": {"session_expired": 3},
              "errors": [{"message_id": 123, "kind": "session_expired"}],
              "message_ids": [123], "project_id": 1}
    health = run_check._health(db, {"errors": [], "metadata_shadow": shadow}, "ok")
    summary = health["metadata_shadow"]
    assert summary["error_count"] == 1
    assert summary["budget_s"] == 25 and summary["elapsed_s"] == 5
    assert "123" not in json.dumps(summary)
    assert not {"errors", "message_ids", "project_id"} & summary.keys()
    db.close()


@pytest.mark.parametrize("target_unread", [True, False, None])
def test_probe_outputs_only_shape_and_read_state(target_unread):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "metadata_probe",
        Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def get(path, params, **kwargs):
        if path == "/projects":
            return {
                "projects": [{"id": 1, "is_unread": True}],
                "paginate": {"has_next": False, "timestamp": 12345},
            }
        # A room flag or the exact response alone cannot establish the target baseline.
        if params.get("unread") == 1:
            return {"messages": [{"id": 1}],
                    "paginate": {"has_next": False, "total_entries": 1}}
        return {
            "messages": [
                {
                    "id": 1,
                    "is_unread": target_unread,
                    "comment": "fictional private body",
                    "reactions": [{"type": "viewed", "count": 1, "self_reacted": True}],
                }
            ]
        }

    adapter = SimpleNamespace(
        _get=get,
        fetch_message_metadata=lambda pid, mid: message(reactions=[], is_unread=target_unread),
    )
    report = mod.probe(adapter, 1, 1)
    encoded = json.dumps(report)
    assert "fictional private body" not in encoded and '"viewed"' not in encoded
    assert report["read_state_unchanged"] is True
    assert report["unread_at_start"] is True
    assert report["target_unread_at_start"] is None
    assert report["target_read_state_unchanged"] is None
    assert report["unread_preservation_proven"] is False
    assert report["viewed_unchanged_between_gets"] is False


def test_usage_probe_deduplicates_patients_and_keeps_failures_unknown():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "metadata_probe_usage", Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def get(path, params, *, extend_session):
        calls.append((path, params, extend_session))
        assert extend_session is False
        if path == "/projects":
            return {"projects": [{"id": 1, "type": "medical", "karte": {"id": 11}},
                                 {"id": 2, "type": "medical", "karte": {"id": 11}},
                                 {"id": 3, "type": "medical", "karte": {"id": 12}},
                                 {"id": 4, "type": "group"}, {"id": 5, "type": "group"}],
                    "paginate": {"has_next": True}}
        if path.endswith("/medication_periods"):
            return {"medication_periods": [{"medicine_name": "private fiction"}]}
        if path.endswith("/observation_items"):
            return {"observation_items": []}
        if path == "/projects/4/consultations":
            raise MCSError("http_error", status=404)
        assert path == "/projects/5/consultations"
        return {"consultations": [{}]}

    report = mod.usage_counts(SimpleNamespace(_get=get), max_pages=1)
    assert report["inventory_complete"] is False
    assert report["patients_in_inventory"] == 2
    assert report["datasets"]["medication_periods"]["patients_with_records"] == 2
    assert report["datasets"]["consultations"]["groups_with_records"] == 1
    assert report["datasets"]["consultations"]["groups_unknown"] == 1
    assert report["datasets"]["consultations"]["patient_count_known"] is False
    assert report["errors"] == {"consultations": {"http_error:404": 1}}
    assert not report["datasets"]["consultations"]["complete"]
    assert sum(path.endswith("/medication_periods") for path, _, _ in calls) == 2
    assert not any(path == f"/projects/{pid}/consultations" for path, _, _ in calls
                   for pid in (1, 2, 3))
    assert "private fiction" not in json.dumps(report)


def test_usage_probe_does_not_count_schema_or_access_errors_as_empty():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "metadata_probe_unknown", Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def get(path, params, *, extend_session):
        if path == "/projects":
            return {"projects": [{"id": 1, "type": "medical", "karte": {"id": 11}},
                                 {"id": 2, "type": "group"}],
                    "paginate": {"has_next": False}}
        if path.endswith("/medication_periods"):
            return {"medication_periods": {}}
        if path.endswith("/consultations"):
            raise MCSError("http_error", status=404)
        return {"observation_items": []}

    report = mod.usage_counts(SimpleNamespace(_get=get))
    assert report["inventory_complete"] is True
    assert report["datasets"]["medication_periods"] == {
        "unit": "patients", "patients_with_records": 0,
        "patients_unknown": 1, "complete": False}
    assert report["datasets"]["consultations"]["groups_unknown"] == 1
    assert report["datasets"]["observation_items"]["complete"] is True


def test_extended_probe_only_shape_and_no_unread_route_calls():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "metadata_probe_routes", Path(__file__).resolve().parents[2]
        / "scripts/development/probe_message_metadata.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls = []

    def get(path, params, *, extend_session):
        calls.append((path, params))
        assert extend_session is False
        assert "timestamp" not in params and "increment_count" not in params
        assert "keep_read_status" not in params or path.startswith("/projects/")
        if path.endswith("/user_reactions"):
            return {"reactions": [{"reaction_type": "viewed", "user": {
                "id": 12345, "name": "fictional private actor"}}],
                    "paginate": {"has_next": False, "timestamp": 12345}}
        if path.endswith("/reactions"):
            return {"users": [{"id": 12345, "name": "fictional private actor"}]}
        return {"messages": [{"id": 12345, "comment": "fictional private body",
                              "reactions": []}]}

    adapter = SimpleNamespace(_get=get)
    assert mod.probe_routes(adapter, 1, 12345, unread_at_start=True)["state"] == "deferred"
    assert calls == []
    report = mod.probe_routes(adapter, 1, 12345, unread_at_start=False)
    assert report["state"] == "observed"
    assert report["unread_preservation_proven"] is False
    assert report["routes"]["batch"]["target_returned"] is True
    text = json.dumps(report)
    assert "fictional private" not in text and "12345" not in text
