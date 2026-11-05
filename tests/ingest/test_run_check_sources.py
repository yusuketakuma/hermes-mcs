"""Latest-post probes, self identity and station roster refresh contracts."""
import collection_stages
import self_identity
import mcs_contract
import json
import time
from types import SimpleNamespace
import pytest
import job_ops
import mcs_adapter
import run_check
from ingest_testkit import _ledger, _unread_patient, _msg_at, _iso
from run_check_stage_testkit import _no_disk_guard as _no_disk_guard


# ---------- self-post / latest probe ----------

def test_self_probe_fetches_and_notifies_own_post(tmp_path):
    """A positive probe triggers a bounded history fetch and the newly
    stored OWN post (is_unread=0 by definition) still lands in a notify
    intent — notify_all_new widens eligibility past the unread flag."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90))
    db.save_messages([_msg_at(100, 90, _iso(1), unread=False)])
    wm = db.high_watermark(90)
    own = _msg_at(101, 90, _iso(0), unread=False)

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            assert pid == 90
            return {"message_id": 101, "is_self_only": True}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            assert since == wm - collection_stages.BACKFILL_OVERLAP_S
            self.history_calls += 1
            return mcs_contract.MessageBatch([own], pages=1, reached=True)

    adapter = Adapter()
    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(adapter, db, result,
                               time.monotonic() + 300, run_id=1)

    assert adapter.history_calls == 1
    assert result["self_probe"] == 1
    assert result["self_probe_fetched"] == [90]
    assert result["new_messages"] == 1
    row = db.db.execute(
        "SELECT kind,payload FROM notify_outbox").fetchone()
    assert row["kind"] == "new_messages"
    payload = json.loads(row["payload"])
    assert payload["source"] == "self"
    assert payload["message_ids"] == [101]
    db.close()


def test_stage_fetch_probe_notifies_own_post_backfill_would_absorb(
        tmp_path, monkeypatch):
    """With self_posts on, the probe runs before backfill: an own post
    (never unread) on backfill's first page is stored by the probe with a
    source=self notification instead of silently by backfill."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90))
    db.save_messages([_msg_at(100, 90, _iso(1), unread=False)])
    own = _msg_at(101, 90, _iso(0), unread=False)

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 101, "is_self_only": True}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_contract.MessageBatch([own], pages=1, reached=True)

    monkeypatch.setattr(collection_stages, 'stage_unread', lambda *a, **k: None)
    result = {"errors": [], "new_messages": 0, "backfilled": 0}
    args = SimpleNamespace(mark_read=False, jobs_only=False, no_backfill=False)
    run_check._stage_fetch(Adapter(), db, args, {"self_posts": True},
                           result, time.monotonic() + 300, 1, False, None)

    assert result["errors"] == []
    assert result["self_probe_fetched"] == [90]
    assert result["new_messages"] == 1 and result["backfilled"] == 0
    rows = db.db.execute(
        "SELECT kind,payload FROM notify_outbox").fetchall()
    assert [r["kind"] for r in rows] == ["new_messages"]
    payload = json.loads(rows[0]["payload"])
    assert payload["source"] == "self" and payload["message_ids"] == [101]
    # no pending reply job, so backfill still certifies coverage this tick
    assert db.coverage_ts(90) == db.high_watermark(90)
    db.close()


def test_self_post_burst_past_probe_pages_notifies_every_new_post(
        tmp_path, monkeypatch):
    """A self_posts burst larger than SELF_PROBE_PAGES: the probe stores
    the newest pages and the same tick's backfill stores the rest — that
    remainder must notify too, or the next tick sees the latest id stored
    and never probes again. Rows at/below the pre-probe cutoff stay quiet."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90))
    db.save_messages([_msg_at(100, 90, _iso(1), unread=False)])
    # lagging coverage: backfill walks below the watermark and newly
    # stores an old gap row, which must not be announced
    db.set_coverage(90, int(time.time()) - 3 * 86400)
    # newest-first timeline: 21 new read/own posts, then the old gap row
    items = [{"id": i, "comment": "synthetic own post",
              "created_at": _iso(0), "is_unread": False}
             for i in range(121, 100, -1)]
    items.append({"id": 50, "comment": "synthetic old gap",
                  "created_at": _iso(2), "is_unread": False})

    class Adapter(mcs_adapter.MCSAdapter):
        def __init__(self):
            self.calls = []

        def fetch_latest(self, pid):
            return {"message_id": 121, "is_self_only": True}

        def _get(self, path, params=None, **kwargs):
            assert path == "/projects/90/messages"
            self.calls.append(params["page"])
            start = (params["page"] - 1) * params["per_page"]
            return {"messages": items[start:start + params["per_page"]],
                    "paginate": {"has_next":
                                 start + params["per_page"] < len(items)}}

    monkeypatch.setattr(collection_stages, 'stage_unread', lambda *a, **k: None)
    monkeypatch.setattr(collection_stages, 'outage_since', lambda: None)
    args = SimpleNamespace(mark_read=False, jobs_only=False, no_backfill=False)
    adapter = Adapter()
    for run_id in (1, 2):
        result = {"errors": [], "new_messages": 0, "backfilled": 0}
        run_check._stage_fetch(adapter, db, args, {"self_posts": True},
                               result, time.monotonic() + 300, run_id,
                               False, None)
        assert result["errors"] == []
    rows = db.db.execute(
        "SELECT kind,payload FROM notify_outbox").fetchall()
    notified = sorted({i for r in rows if r["kind"] == "new_messages"
                       for i in json.loads(r["payload"])["message_ids"]})
    assert db.has_message(50)
    assert notified == list(range(101, 122))
    db.close()


def test_self_probe_skips_when_latest_already_stored(tmp_path):
    """The latest endpoint returns no timestamp — freshness is decided
    by whether the returned id is already in the ledger."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(91))
    db.save_messages([_msg_at(100, 91, _iso(0), unread=False)])

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 100, "is_self_only": False}

        def fetch_history(self, *a, **k):
            raise AssertionError("must not fetch")

    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 1
    assert "self_probe_fetched" not in result
    assert db.db.execute("SELECT COUNT(*) c FROM notify_outbox"
                         ).fetchone()["c"] == 0
    db.close()


def test_self_probe_null_message_means_nothing_new(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(92))
    db.save_messages([_msg_at(100, 92, _iso(0), unread=False)])

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": None, "is_self_only": False}

        def fetch_history(self, *a, **k):
            raise AssertionError("must not fetch")

    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 1
    assert result["errors"] == []
    db.close()


def test_self_probe_never_imported_patient_skipped(tmp_path):
    """No high watermark -> the durable deep-import jobs own the backlog;
    probing them would only re-trigger full history fetches."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(93))

    class Adapter:
        def fetch_latest(self, pid):
            raise AssertionError("must not probe")

    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert result["self_probe"] == 0
    db.close()


def test_self_probe_missed_post_uses_probe_source(tmp_path):
    """is_self_only=False means others' already-read posts were missed —
    they notify under the 'probe' source, not 'self'."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(94))
    db.save_messages([_msg_at(100, 94, _iso(1), unread=False)])
    missed = _msg_at(101, 94, _iso(0), unread=False)

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 101, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_contract.MessageBatch([missed], pages=1,
                                            reached=True)

    result = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    payload = json.loads(db.db.execute(
        "SELECT payload FROM notify_outbox").fetchone()["payload"])
    assert payload["source"] == "probe"
    assert payload["message_ids"] == [101]
    db.close()


def test_self_probe_unfetchable_id_not_retried(tmp_path):
    """If a completed fetch cannot store the probed id (e.g. it is a
    reply only reachable via its own thread), the probe marker stops the
    same id from re-triggering a fetch on every subsequent tick."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(96))
    db.save_messages([_msg_at(100, 96, _iso(0), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            return mcs_contract.MessageBatch([], pages=1, reached=True)

    adapter = Adapter()
    for _ in range(2):
        result = {"errors": [], "new_messages": 0}
        collection_stages.stage_self_probe(adapter, db, result,
                                   time.monotonic() + 300, run_id=1)
        assert result["self_probe"] == 1
    assert adapter.history_calls == 1
    db.close()


def test_self_probe_incomplete_walk_retries_latest_id(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(97))
    db.save_messages([_msg_at(100, 97, _iso(0), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            if self.history_calls == 1:
                return mcs_contract.MessageBatch([], pages=2, reached=False)
            return mcs_contract.MessageBatch(
                [_msg_at(555, 97, _iso(1), unread=False)],
                pages=1, reached=True)

    adapter = Adapter()
    first = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(adapter, db, first,
                               time.monotonic() + 300, run_id=1)
    assert db.probe_marker(97) is None
    assert "probe 97: history_incomplete" in first["errors"]

    second = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(adapter, db, second,
                               time.monotonic() + 300, run_id=2)
    assert adapter.history_calls == 2
    assert db.has_message(555)
    assert second["new_messages"] == 1
    db.close()


def test_self_probe_incomplete_body_does_not_mark_latest(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(98))
    db.save_messages([_msg_at(100, 98, _iso(1), unread=False)])

    class Adapter:
        def __init__(self):
            self.history_calls = 0

        def fetch_latest(self, pid):
            return {"message_id": 555, "is_self_only": False}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            self.history_calls += 1
            if self.history_calls == 1:
                partial = _msg_at(101, 98, _iso(0), unread=False)
                partial.body_state = "snippet"
                return mcs_contract.MessageBatch(
                    [partial], pages=1, reached=True)
            return mcs_contract.MessageBatch(
                [_msg_at(555, 98, _iso(0), unread=False)],
                pages=1, reached=True)

    adapter = Adapter()
    first = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(
        adapter, db, first, time.monotonic() + 300, run_id=1)
    assert db.probe_marker(98) is None
    assert "probe 98: history_incomplete" in first["errors"]

    second = {"errors": [], "new_messages": 0}
    collection_stages.stage_self_probe(
        adapter, db, second, time.monotonic() + 300, run_id=2)
    assert adapter.history_calls == 2
    assert db.has_message(555)
    db.close()


@pytest.mark.parametrize("jobs_only,has_profile,fetched", [
    (False, True, False),   # tick with a stored profile: no GET
    (False, False, True),   # tick before the first profile: fetch it
    (True, True, True),     # deep run always refreshes
])
def test_self_profile_fetch_gate(tmp_path, monkeypatch, jobs_only,
                                 has_profile, fetched):
    import notify_spool
    import notify_cmds
    db = _ledger(tmp_path)
    prof = {"sender_id": 1, "name": "synthetic", "professions": [],
            "organizations": []}
    if has_profile:
        with db.db:
            self_identity.record_self_profile(db.db, prof)
    calls = []

    class Adapter:
        def self_profile(self):
            calls.append(1)
            return prof

        def station_staffs(self, stations):
            return []

    monkeypatch.setattr(run_check, "HOME", str(tmp_path))
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "run_history_jobs", "run_reconcile_jobs",
                 "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, "ensure_dirs", lambda *a: None)
    monkeypatch.setattr(notify_spool, "restore_pending", lambda *a: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands",
                        lambda *a, **k: None)
    result = {"errors": []}
    args = SimpleNamespace(jobs_only=jobs_only, download_files=False)
    run_check._run_jobs(Adapter(), db, args, {}, result,
                        time.monotonic() + 300, False, None)
    assert result["errors"] == []
    assert bool(calls) is fetched
    assert self_identity.latest_self_profile(db.db)["name"] == "synthetic"
    db.close()


def test_station_roster_follows_profile_cadence(tmp_path, monkeypatch):
    """The own-station roster is fetched right after the self profile
    and stored replace-on-change; a failure keeps the stored roster and
    is reported outside errors."""
    import notify_spool
    import notify_cmds
    db = _ledger(tmp_path)
    prof = {"sender_id": 1, "name": "synthetic", "professions": [],
            "organizations": ["みどり薬局"],
            "stations": [{"id": 7, "name": "みどり薬局"}]}
    roster = [{"staff_id": 3, "name": "佐藤 一郎", "professions": [],
               "station": "みどり薬局", "is_self": False}]
    seen = []

    class Adapter:
        fail = False

        def self_profile(self):
            return prof

        def station_staffs(self, stations):
            seen.append(stations)
            if self.fail:
                raise RuntimeError("synthetic")
            return roster

    monkeypatch.setattr(run_check, "HOME", str(tmp_path))
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "run_history_jobs", "run_reconcile_jobs",
                 "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    monkeypatch.setattr(notify_spool, "ensure_dirs", lambda *a: None)
    monkeypatch.setattr(notify_spool, "restore_pending", lambda *a: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands",
                        lambda *a, **k: None)
    args = SimpleNamespace(jobs_only=True, download_files=False)
    result = {"errors": []}
    run_check._run_jobs(Adapter(), db, args, {}, result,
                        time.monotonic() + 300, False, None)
    assert seen == [prof["stations"]]
    assert result["station_staff"] == "updated" and result["errors"] == []
    assert self_identity.latest_station_staff(db.db) == roster
    failing = Adapter()
    failing.fail = True
    result = {"errors": []}
    run_check._run_jobs(failing, db, args, {}, result,
                        time.monotonic() + 300, False, None)
    # optional roster data: reported, never a health-degrading error
    assert result["errors"] == []
    assert result["station_staff"] == "failed: RuntimeError"
    assert self_identity.latest_station_staff(db.db) == roster
    db.close()
