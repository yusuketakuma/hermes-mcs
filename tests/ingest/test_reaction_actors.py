"""Synthetic 22-E publish and 22-F reaction-actor contracts (no real MCS)."""

import copy
import json
import time
from types import SimpleNamespace

import pytest

import ledger as ledger_mod
import run_check
from ledger import SCHEMA_VERSION, Ledger, LedgerReader, reaction_actor_summary
from mcs_adapter import MCSAdapter, MCSError, SessionExpired, _norm_message
from mcs_signals import record_station_staff
from message_metadata import get_message_metadata
from run_check import stage_metadata_shadow

SELF_ID = 11
TS = 987654321
NAME = "synthetic actor name canary"


def message(mid=1, project_id=1, **extra):
    return _norm_message({"id": mid, "comment": "fictional text", "user": {"id": SELF_ID},
                          "created_at": "2026-10-03T00:00:00+09:00", **extra}, project_id)


def reactions(**counts):
    return [{"type": k, "count": v, "self_reacted": False} for k, v in counts.items()]


@pytest.fixture
def db(tmp_path):
    store = Ledger(str(tmp_path / "ledger.db"))
    with store.db:
        record_station_staff(store.db, [{"staff_id": SELF_ID, "is_self": True}])
    yield store
    store.close()


def save(store, m, project_id=1):
    store.ensure_patient(project_id)
    store.save_messages([m], project_id=project_id, notify=False)
    with store.db:
        store.db.execute("UPDATE messages SET posted_at_ts=?", (time.time(),))


def actor_rows(n, kind="viewed", start=1000, profession="看護師"):
    return [{"reaction_type": kind,
             "user": {"id": start + i, "last_name": NAME, "icon_url": "x",
                      "stations": [{"name": NAME}],
                      "specialist_categories": [{"name": profession}]}}
            for i in range(n)]


def pages(rows, *, mid=1, pid=1, per_page=50, ts=TS):
    counts = {}
    for r in rows:
        counts[r["reaction_type"]] = counts.get(r["reaction_type"], 0) + 1
    total = max(1, -(-len(rows) // per_page))
    out = []
    for page in range(1, total + 1):
        resp = {"reactions": copy.deepcopy(rows[(page - 1) * per_page:page * per_page]),
                "paginate": {"current_page": page, "per_page": per_page,
                             "has_next": page < total, "timestamp": ts}}
        if page == 1:
            resp["message"] = {"id": mid, "project_id": pid,
                               "reactions": reactions(**counts)}
        out.append(resp)
    return out


def scripted_adapter(responses):
    adapter = MCSAdapter()
    adapter.calls = []

    def get(path, params=None, extend_session=True):
        adapter.calls.append((path, dict(params or {}), extend_session))
        assert extend_session is False and "keep_read_status" not in params
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    adapter._get = get
    return adapter


# ---------- adapter walk ----------

@pytest.mark.parametrize("count", [0, 1, 49, 50, 51, 100, 101])
def test_walk_boundaries_and_snapshot_params(count):
    rows = actor_rows(count)
    responses = pages(rows)
    if count == 0:
        responses[0]["message"]["reactions"] = []
    adapter = scripted_adapter(responses)
    walk = adapter.fetch_reaction_actors(1, 1)
    assert walk["complete"] is True and len(walk["actors"]) == count
    assert walk["pages"] == max(1, -(-count // 50))
    for i, (path, params, _) in enumerate(adapter.calls):
        assert path == "/messages/1/user_reactions"
        assert ("timestamp" in params) is (i > 0)
        assert params.get("timestamp", TS) == TS and params["page"] == i + 1
    for a in walk["actors"]:
        # #22-D2: name and facility are kept for display; the icon never
        assert set(a) == {"actor_id", "reaction_type", "profession",
                          "name", "organization"}
        assert a["name"] == NAME and a["organization"] == NAME
    assert "icon" not in json.dumps(walk)


def test_walk_multiple_people_and_kinds():
    rows = actor_rows(2, "viewed") + actor_rows(1, "good", start=1000, profession="医師")
    walk = scripted_adapter(pages(rows)).fetch_reaction_actors(1, 1)
    assert walk["complete"] is True
    assert sorted((a["actor_id"], a["reaction_type"]) for a in walk["actors"]) == [
        (1000, "good"), (1000, "viewed"), (1001, "viewed")]


@pytest.mark.parametrize("defect", ["timestamp", "cycle", "other_message", "other_project",
                                    "duplicate", "count"])
def test_walk_rejects_inconsistent_pages(defect):
    responses = pages(actor_rows(51))
    if defect == "timestamp":
        responses[1]["paginate"]["timestamp"] += 1
    elif defect == "cycle":
        responses[1]["paginate"]["current_page"] = 1
    elif defect == "other_message":
        responses[0]["message"]["id"] = 2
    elif defect == "other_project":
        responses[0]["message"]["project_id"] = 2
    elif defect == "duplicate":
        responses[1]["reactions"][0] = copy.deepcopy(responses[0]["reactions"][0])
    else:
        responses[0]["message"]["reactions"][0]["count"] = 52
    walk = scripted_adapter(responses).fetch_reaction_actors(1, 1)
    assert walk["complete"] is False
    assert walk["error"] == ("count_mismatch" if defect == "count" else "schema_error")


# ---------- ledger storage ----------

def test_reaction_tables_use_current_schema_version(db):
    assert db.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    tables = {r[0] for r in db.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"message_reaction_actors", "message_reaction_actor_fetch"} <= tables


def test_complete_replaces_and_incomplete_keeps_old_set(db):
    save(db, message(reactions=reactions(viewed=2)))
    db.save_reaction_actors(1, [{"actor_id": 5, "reaction_type": "viewed", "profession": "医師"},
                                {"actor_id": SELF_ID, "reaction_type": "viewed",
                                 "profession": None}], True)
    s = reaction_actor_summary(db.db, 1)
    assert s["state"] == "complete" and s["self_included"] is True
    assert s["counts"] == {"医師": {"viewed": 1}, "": {"viewed": 1}}
    first = s["complete_at"]
    db.save_reaction_actors(1, [{"actor_id": 6, "reaction_type": "viewed"}], False,
                            error="http_error")
    s = reaction_actor_summary(db.db, 1)
    assert s["state"] == "failed" and s["complete_at"] == first
    assert sum(sum(v.values()) for v in s["counts"].values()) == 2
    cols = {r[1] for r in db.db.execute("PRAGMA table_info(message_reaction_actors)")}
    assert {"actor_name", "organization", "removed_at"} <= cols
    assert not {"icon_url", "medium_icon_url"} & cols   # the icon is never kept
    assert reaction_actor_summary(db.db, 99)["state"] == "not_fetched"



@pytest.mark.parametrize("upgrade", [False, True])
def test_actor_columns_upgrade_preserves_old_snapshot_observations(tmp_path, upgrade):
    """Older actor tables remain readable and migrate without erasing observations."""
    path = tmp_path / "old-actors.db"
    store = Ledger(str(path))
    save(store, message(reactions=reactions(viewed=1)))
    observed = time.time()
    store.save_reaction_actors(1, [{"actor_id": 5, "reaction_type": "viewed",
                                   "profession": "医師"}], True, now=observed)
    with store.db:
        store.db.execute("CREATE TABLE legacy_actors(message_id INTEGER NOT NULL,"
                         "actor_id INTEGER NOT NULL,reaction_type TEXT NOT NULL,"
                         "profession TEXT,observed_at REAL NOT NULL,"
                         "PRIMARY KEY(message_id,actor_id,reaction_type))")
        store.db.execute("INSERT INTO legacy_actors SELECT message_id,actor_id,"
                         "reaction_type,profession,observed_at FROM message_reaction_actors")
        store.db.execute("DROP TABLE message_reaction_actors")
        store.db.execute("ALTER TABLE legacy_actors RENAME TO message_reaction_actors")
    store.close()
    reader = Ledger(str(path)) if upgrade else LedgerReader(str(path))
    try:
        summary = reaction_actor_summary(reader.db, 1, now=observed + 1)
        assert summary["state"] == "complete"
        assert summary["counts"] == {"医師": {"viewed": 1}}
        assert summary["actors"] == [{"reaction_type": "viewed", "name": None,
                                      "profession": "医師", "organization": None,
                                      "self": False}]
        columns = {r[1] for r in reader.db.execute("PRAGMA table_info(message_reaction_actors)")}
        assert ({"actor_name", "organization", "removed_at"} <= columns) is upgrade
        assert reader.db.execute("SELECT observed_at FROM message_reaction_actors").fetchone()[0] == observed
    finally:
        reader.close()

def test_summary_stale_on_expiry_or_changed_counts(db):
    save(db, message(reactions=reactions(viewed=1)))
    now = time.time() + 10
    db.save_reaction_actors(1, [{"actor_id": 5, "reaction_type": "viewed"}], True, now=now)
    assert reaction_actor_summary(db.db, 1, now=now)["state"] == "complete"
    assert reaction_actor_summary(db.db, 1, now=now + 86400)["state"] == "stale"
    with db.db:
        db.db.execute("UPDATE message_metadata SET content=json_set(content,"
                      "'$.reactions.observed_at',?) WHERE source='capture'", (now + 1,))
    assert reaction_actor_summary(db.db, 1, now=now + 2)["state"] == "stale"


def test_targets_change_expiry_backoff_and_retention(db, monkeypatch):
    for mid in (1, 2, 3):
        save(db, message(mid, reactions=reactions(viewed=1) if mid != 3 else []))
    now = time.time() + 10
    assert [r["message_id"] for r in db.reaction_actor_targets(now=now)] == [1, 2]
    db.save_reaction_actors(1, [], True, now=now)
    db.save_reaction_actors(2, [], False, error="http_error", now=now)
    assert db.reaction_actor_targets(now=now + 1) == []
    # unchanged counts still refetch after 24h; failures after the backoff
    assert [r["message_id"] for r in db.reaction_actor_targets(now=now + 6 * 3600)] == [2]
    assert [r["message_id"] for r in db.reaction_actor_targets(now=now + 86400)] == [2, 1]
    monkeypatch.setattr(ledger_mod.time, "time", lambda: now + 2)   # a later capture change
    save(db, message(1, reactions=reactions(viewed=1, good=1)))
    assert [r["message_id"] for r in db.reaction_actor_targets(now=now + 3)] == [1]
    with db.db:
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=1")
    # retention is unlimited (#22-D3): leaving the watch set never deletes
    assert db.db.execute("SELECT COUNT(*) FROM message_reaction_actor_fetch").fetchone()[0] == 2



@pytest.mark.parametrize("reply_state", ["future", "deleted"])
def test_actor_watch_activity_excludes_future_and_deleted_replies(db, reply_state):
    now = time.time()
    save(db, message(1, reactions=reactions(viewed=1)))
    save(db, message(2, reactions=reactions(viewed=1)))
    with db.db:
        db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1", (now - 8 * 86400,))
        db.db.execute("UPDATE messages SET parent_id=1,posted_at_ts=?,body_state=? WHERE message_id=2",
                      (now + 1 if reply_state == "future" else now,
                       "deleted" if reply_state == "deleted" else "full"))
    assert db.reaction_actor_targets(now=now) == []
    if reply_state == "future":
        with db.db:
            db.db.execute("UPDATE messages SET posted_at_ts=? WHERE message_id=1", (now,))
        assert [r["message_id"] for r in db.reaction_actor_targets(now=now)] == [1]
        assert [r["message_id"] for r in db.reaction_actor_targets(now=now + 1)] == [1, 2]


def test_thread_activity_index_is_added_on_reopen_without_changing_targets(db):
    for mid in range(1, 41):
        save(db, message(mid, reactions=reactions(viewed=1)))
    now = time.time() + 1
    with db.db:
        db.db.execute("UPDATE messages SET parent_id=message_id-1 WHERE message_id%2=0")
        db.db.execute("DROP INDEX idx_messages_thread_time")
    before = [tuple(r) for r in db.reaction_actor_targets(now=now)]
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    reopened = Ledger(path)
    try:
        statements = []
        reopened.db.set_trace_callback(statements.append)
        after = [tuple(r) for r in reopened.reaction_actor_targets(now=now)]
        reopened.db.set_trace_callback(None)
        assert after == before
        assert len(after) == 4
        query = next(sql for sql in statements
                     if "SELECT m.message_id,m.project_id FROM messages m" in sql)
        plan = [r[3] for r in reopened.db.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("SEARCH x USING INDEX idx_messages_thread_time" in step
                   and "<expr>=?" in step and "posted_at_ts<?" in step for step in plan)
    finally:
        reopened.close()

def test_actor_history_keeps_names_and_marks_cancellations(db):
    """#22-D2/D3 (2026-10-03): names are kept and shown; nothing is
    deleted — a cancelled stamp is marked removed_at, a re-press clears
    it, and an incomplete walk never marks anything removed."""
    save(db, message(reactions=reactions(viewed=2)))
    a = {"actor_id": 5, "reaction_type": "viewed", "profession": "医師",
         "name": "合成 一郎", "organization": "合成クリニック"}
    b = {"actor_id": 6, "reaction_type": "viewed", "name": "合成 花子"}
    db.save_reaction_actors(1, [a, b], True, now=100.0)
    names = [x["name"] for x in reaction_actor_summary(db.db, 1, now=101)["actors"]]
    assert names == ["合成 一郎", "合成 花子"]
    db.save_reaction_actors(1, [a], False, error="http_error", now=200.0)
    assert len(reaction_actor_summary(db.db, 1, now=201)["actors"]) == 2
    db.save_reaction_actors(1, [a], True, now=300.0)
    rows = db.db.execute("SELECT actor_id,observed_at,removed_at,actor_name FROM "
                         "message_reaction_actors ORDER BY actor_id").fetchall()
    assert [tuple(r) for r in rows] == [(5, 100.0, None, "合成 一郎"),
                                        (6, 100.0, 300.0, "合成 花子")]
    assert [x["name"] for x in reaction_actor_summary(db.db, 1, now=301)["actors"]] == ["合成 一郎"]
    db.save_reaction_actors(1, [a, b], True, now=400.0)    # pressed again
    assert db.db.execute("SELECT observed_at,removed_at FROM message_reaction_actors "
                         "WHERE actor_id=6").fetchone()[:] == (400.0, None)


def test_shadow_detects_first_actor_and_changes_without_publishing(db, monkeypatch):
    save(db, message(reactions=[]))
    assert not db.reaction_actor_targets()
    now = time.time() + 10
    monkeypatch.setattr(ledger_mod.time, "time", lambda: now)
    db.save_metadata_shadow(message(reactions=reactions(viewed=1)), publish=False)
    assert get_message_metadata(db.db, 1)["reactions"] == []
    assert [r["message_id"] for r in db.reaction_actor_targets()] == [1]
    db.save_reaction_actors(1, ok(5)["actors"], True)
    now += 1
    db.save_metadata_shadow(message(reactions=reactions(viewed=2)), publish=False)
    assert [r["message_id"] for r in db.reaction_actor_targets()] == [1]
    assert reaction_actor_summary(db.db, 1)["state"] == "stale"



# ---------- 22-E publish ----------

def dump_except_metadata(store):
    tables = [r[0] for r in store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'messages_fts%' "
        "AND name!='message_metadata' ORDER BY name")]
    return {t: [tuple(r) for r in store.db.execute(f"SELECT * FROM {t} ORDER BY 1")]
            for t in tables}


def test_publish_writes_capture_failure_does_not(db):
    save(db, message(reactions=[]))
    before = dump_except_metadata(db)
    db.save_metadata_shadow(message(reactions=reactions(good=1)))
    assert get_message_metadata(db.db, 1)["reactions"] == []        # publish off
    db.save_metadata_shadow(message(reactions=reactions(good=2)), error="network_error",
                            publish=True)
    assert get_message_metadata(db.db, 1)["reactions"] == []        # failure
    # the stored shadow value is never promoted later; only a fresh success is
    db.save_metadata_shadow(message(reactions=reactions(good=3)), publish=True)
    assert get_message_metadata(db.db, 1)["reactions"][0]["count"] == 3
    observed = get_message_metadata(db.db, 1)["reactions_observed_at"]
    db.save_metadata_shadow(message(reactions=reactions(good=3)), publish=True)
    assert get_message_metadata(db.db, 1)["reactions_observed_at"] == observed
    # body/hash, coverage, outbox, semantic jobs and read marks: all untouched
    assert dump_except_metadata(db) == before


@pytest.mark.parametrize(("cfg", "publish", "actors"), [
    ({"metadata_shadow": True}, False, False),
    ({"metadata_shadow": True, "metadata_refresh_publish": True}, True, False),
    ({"metadata_shadow": True, "metadata_refresh_publish": "true",
      "metadata_actors": 1}, False, False),
    ({"metadata_shadow": True, "metadata_actors": True}, False, True),
])
def test_config_gates(monkeypatch, cfg, publish, actors):
    monkeypatch.setattr(run_check, "_code_changed", lambda result: False)
    seen = {}
    monkeypatch.setattr(run_check, "stage_metadata_shadow",
                        lambda *a, **kw: seen.update(kw))
    run_check._run_metadata_shadow(None, SimpleNamespace(),
                                   {"errors": []}, 100, cfg)
    assert seen == {"publish": publish, "actors": actors, "cfg": cfg}


def test_publish_stage_stats_and_health(db):
    save(db, message(reactions=[]))
    adapter = SimpleNamespace(set_deadline=lambda d: None,
                              fetch_message_metadata=lambda pid, mid: message(
                                  mid, reactions=reactions(good=1)))
    result = {"errors": []}
    stage_metadata_shadow(adapter, db, result, time.monotonic() + 60, publish=True)
    assert result["metadata_shadow"]["mode"] == "publish"
    assert result["metadata_shadow"]["published"] == 1
    assert get_message_metadata(db.db, 1)["reactions"][0]["count"] == 1
    health = run_check._health(db, result, "ok")["metadata_shadow"]
    assert health["published"] == 1 and health["mode"] == "publish"


# ---------- 22-F stage ----------

def actor_stage_adapter(walks, shadow=None):
    calls = []

    def fetch_actors(pid, mid):
        calls.append(mid)
        w = walks.pop(0)
        return w(mid) if callable(w) else w

    return calls, SimpleNamespace(
        set_deadline=lambda d: None, fetch_reaction_actors=fetch_actors,
        fetch_message_metadata=shadow or (lambda pid, mid: message(
            mid, reactions=reactions(viewed=1))))


def ok(*ids):
    return {"complete": True, "actors": [
        {"actor_id": i, "reaction_type": "viewed", "profession": None} for i in ids]}


def test_stage_caps_four_per_tick_and_detects_swap_on_expiry(db, monkeypatch):
    for mid in (1, 2, 3, 4, 5):
        save(db, message(mid, reactions=reactions(viewed=1)))
    calls, adapter = actor_stage_adapter([ok(5)] * 4)
    result = {}
    stage_metadata_shadow(adapter, db, result, time.monotonic() + 60, actors=True)
    assert calls == [1, 2, 3, 4] and result["metadata_shadow"]["actors"]["complete"] == 4
    # same counts, different actor: only the 24h expiry refetch can see it
    later = time.time() + 86400 + 60
    monkeypatch.setattr(ledger_mod.time, "time", lambda: later)
    calls, adapter = actor_stage_adapter([ok(6), ok(7), ok(7), ok(7)])
    stage_metadata_shadow(adapter, db, {}, time.monotonic() + 60, actors=True)
    assert calls == [5, 1, 2, 3]
    # the swap is seen: 7 is current, 5 stays in the history as removed
    assert db.db.execute("SELECT actor_id FROM message_reaction_actors WHERE message_id=1 "
                         "AND removed_at IS NULL").fetchall()[0][0] == 7
    assert db.db.execute("SELECT removed_at IS NOT NULL FROM message_reaction_actors "
                         "WHERE message_id=1 AND actor_id=5").fetchone()[0] == 1


@pytest.mark.parametrize("error", ["http_error", "session_expired"])
def test_actor_failure_keeps_old_set_and_layers_0_1(db, error):
    save(db, message(1, reactions=reactions(viewed=1)))
    save(db, message(2, reactions=reactions(viewed=1)))
    db.save_reaction_actors(1, ok(5)["actors"], True, now=time.time() - 86400 - 10)
    db.save_reaction_actors(2, ok(5)["actors"], True, now=time.time() - 86400 - 5)
    capture = db.db.execute("SELECT content FROM message_metadata WHERE source='capture' "
                            "ORDER BY message_id").fetchall()
    partial = {"complete": False, "actors": ok(9)["actors"], "error": error}
    calls, adapter = actor_stage_adapter([partial, ok(8)])
    result = {}
    stage_metadata_shadow(adapter, db, result, time.monotonic() + 60, actors=True)
    stats = result["metadata_shadow"]
    assert stats["fetched"] == 2 and not stats["errors"]          # layer 1 unaffected
    assert db.db.execute("SELECT content FROM message_metadata WHERE source='capture' "
                         "ORDER BY message_id").fetchall() == capture
    assert [r[0] for r in db.db.execute(
        "SELECT actor_id FROM message_reaction_actors WHERE message_id=1")] == [5]
    assert reaction_actor_summary(db.db, 1)["state"] == "failed"
    assert calls == ([1] if error == "session_expired" else [1, 2])


def test_actors_skipped_when_shadow_stopped_or_budget_spent(db, monkeypatch):
    save(db, message(1, reactions=reactions(viewed=1)))

    def expired(pid, mid):
        raise SessionExpired()

    calls, adapter = actor_stage_adapter([], shadow=expired)
    result = {}
    stage_metadata_shadow(adapter, db, result, time.monotonic() + 60, actors=True)
    assert calls == [] and result["metadata_shadow"]["actors"]["deferred"] == 1
    clock = [100.0]
    monkeypatch.setattr(run_check.time, "monotonic", lambda: clock[0])

    def slow(pid, mid):
        clock[0] += 25
        return message(mid, reactions=reactions(viewed=1))

    with db.db:
        db.db.execute("DELETE FROM message_metadata WHERE source='shadow'")
    calls, adapter = actor_stage_adapter([], shadow=slow)
    result = {}
    stage_metadata_shadow(adapter, db, result, 200, actors=True)
    assert calls == [] and result["metadata_shadow"]["actors"]["deferred"] == 1


def test_actor_walk_error_from_adapter_is_not_raised(db):
    """A transport error inside the walk is data, not a crashed tick."""
    save(db, message(1, reactions=reactions(viewed=1)))
    adapter = scripted_adapter([MCSError("http_error", status=429, retryable=True)])
    adapter.set_deadline = lambda d: None
    adapter.fetch_message_metadata = lambda pid, mid: message(mid, reactions=reactions(viewed=1))
    result = {}
    stage_metadata_shadow(adapter, db, result, time.monotonic() + 60, actors=True)
    assert result["metadata_shadow"]["actors"]["errors"] == [
        {"message_id": 1, "kind": "http_error"}]
    assert reaction_actor_summary(db.db, 1)["state"] == "failed"


def test_complete_walk_rerenders_thread_card_only_with_cfg(db, monkeypatch):
    import notify_cards
    rerendered = []
    monkeypatch.setattr(notify_cards, "rerender_message_cards",
                        lambda ledger, cfg, pid, mid: rerendered.append((pid, mid)))
    for mid in (1, 2):
        save(db, message(mid, reactions=reactions(viewed=1)))
    partial = {"complete": False, "actors": [], "error": "http_error"}
    _calls, adapter = actor_stage_adapter([ok(5), partial])
    stage_metadata_shadow(adapter, db, {}, time.monotonic() + 60, actors=True, cfg={})
    assert rerendered == [(1, 1)]           # the incomplete walk changes nothing
    db.save_reaction_actors(1, ok(5)["actors"], True, now=time.time() - 86400 - 10)
    _calls, adapter = actor_stage_adapter([ok(6), ok(6)])
    stage_metadata_shadow(adapter, db, {}, time.monotonic() + 60, actors=True)
    assert rerendered == [(1, 1)]           # no cfg: no card side effects
