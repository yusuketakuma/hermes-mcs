"""job_ops drain coverage — history/reply/discovery/trickle/reconcile
jobs, archived-patient handling (Oracle F1-F10, F01-F07), history
floors, drain fairness."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import job_ops
import ledger
import mcs_adapter
import notify_flush
from ingest_testkit import _ledger, _message, _unread_patient


def test_reply_merge_result_is_per_call():
    class Adapter:
        def __init__(self):
            self.fail = True

        def fetch_thread(self, *_):
            if self.fail:
                raise mcs_adapter.MCSError("broken")
            return [_message(mid=20, parent_id=10)]

    adapter = Adapter()
    stats = {"errors": [], "threads": 0}
    first = _message(mid=10)
    first.reply_count = 1
    assert not job_ops.merge_full_replies(
        adapter, [first], 0, time.monotonic() + 5, stats).checkpoint_safe

    adapter.fail = False
    second = _message(mid=11)
    second.replies = [_message(mid=20, state="snippet", parent_id=11)]
    assert job_ops.merge_full_replies(
        adapter, [second], 0, time.monotonic() + 5, stats).checkpoint_safe


def test_explicit_command_promotes_existing_trickle_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history", 1, payload={
        "since": 0, "page": 7, "trickle": True
    })
    cmd_dir = tmp_path / "cmd"
    cmd_dir.mkdir()
    (cmd_dir / "request.json").write_text(json.dumps({
        "cmd": "import", "project_id": 1, "days": 14, "pages": 20
    }), encoding="utf-8")

    job_ops.drain_commands(db, {"errors": []}, str(cmd_dir))

    payload = json.loads(db.history_job(1)["payload"])
    assert payload["page"] == 7
    assert payload["since"] == 0
    assert payload["pages"] == 20
    assert payload["trickle"] is False
    db.close()


def test_trickle_revives_done_but_not_failed_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    job_id = db.job_add("history", 1, payload={"since": 10})
    db.job_done(job_id)
    assert job_ops.seed_trickle(db, [1]) == 1
    pending = db.history_job(1)
    db.job_fail(pending["job_id"])
    assert job_ops.seed_trickle(db, [1]) == 0
    assert db.job_state("history", 1) == "failed"
    db.close()


def test_one_page_history_job_advances_cursor(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("history", 1, payload={"since": 0, "page": 1, "pages": 1})

    class Adapter:
        def fetch_history(self, *args, **kwargs):
            return mcs_adapter.MessageBatch([], pages=1, reached=False)

    result = {"errors": []}
    job_ops.run_history_jobs(
        Adapter(), db, result, time.monotonic() + 100, trickle=False)
    payload = json.loads(db.history_job(1)["payload"])
    assert payload["page"] == 2
    db.close()


def test_complete_embedded_replies_need_no_thread_refetch():
    parent = _message(mid=10)
    parent.reply_count = 1
    parent.replies = [_message(mid=20, parent_id=10)]

    class Adapter:
        def fetch_thread(self, *_):
            pytest.fail("complete embedded replies must not be refetched")

    result = job_ops.merge_full_replies(
        Adapter(), [parent], 0, time.monotonic() + 5,
        {"errors": [], "threads": 0})

    assert result.checkpoint_safe
    assert result.reply_jobs == 0


# ---------- archived patients (Oracle F1-F10) ----------


class _KartesAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        assert path == "/kartes"
        self.calls.append(dict(params))
        return self.pages[params["page"] - 1]


def _karte(pid, name="x"):
    return {"id": pid + 1000, "last_name": "a", "first_name": name,
            "user": {"id": 1},
            "station": {"id": 1, "name": "st"}, "disease": "d",
            "medical_project": {"id": pid, "is_unread": False}}


def test_archived_kartes_pagination_dedup_and_contract():
    pages = [
        {"kartes": [_karte(10), _karte(11), {"id": 99}],
         "paginate": {"has_next": True}},
        {"kartes": [_karte(11), _karte(12)],
         "paginate": {"has_next": False}},
    ]
    adapter = _KartesAdapter(pages)
    out = adapter.list_archived_kartes()
    # 99 has no medical_project -> skipped; 11 duplicated -> deduped
    assert [p.project_id for p in out] == [10, 11, 12]
    assert out[0].patient_name == "a x"
    assert adapter.calls[0]["is_archived"] == 1
    assert adapter.calls[0]["page"] == 1 and adapter.calls[1]["page"] == 2


def test_archived_kartes_malformed_and_page_cap():
    bad = _KartesAdapter([
        {"kartes": [{"medical_project": {"id": "x"}}],
         "paginate": {"has_next": False}}])
    with pytest.raises(mcs_adapter.SchemaError):
        bad.list_archived_kartes()

    capped = _KartesAdapter([
        {"kartes": [], "paginate": {"has_next": True}}])
    with pytest.raises(mcs_adapter.MCSError) as e:
        capped.list_archived_kartes(max_pages=1)
    assert e.value.kind == "pages_exceeded"


def test_archived_registration_atomic_and_transition(tmp_path):
    db = _ledger(tmp_path)
    p = _unread_patient(50)
    created, transitioned = db.upsert_patient_info(p, is_archived=True)
    assert created and transitioned
    row = db.db.execute(
        "SELECT is_archived,fetch_state FROM patients WHERE project_id=50"
    ).fetchone()
    assert row["is_archived"] == 1 and row["fetch_state"] == "pending"

    # re-discovery without the flag preserves it — no half-registered
    # intermediate state exists to observe (F1)
    created, transitioned = db.upsert_patient_info(p)
    assert not created and not transitioned
    assert db.is_archived(50)

    # live-list reappearance unarchives through the same atomic path (F4)
    created, transitioned = db.upsert_patient_info(p, is_archived=False)
    assert not created and not transitioned
    assert not db.is_archived(50)
    db.close()


def test_archived_save_suppresses_notify_intent(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(60), is_archived=True)
    new_ids = db.save_messages([_message(mid=1, project_id=60)],
                               project_id=60, notify={"source": "t"})
    assert new_ids == [1]
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 0
    db.close()


def test_outbox_event_suppressed_after_archival(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.ensure_patient(61)
    db.save_messages([_message(mid=55, project_id=61)], project_id=61)
    db.db.execute(
        "INSERT INTO notify_outbox(kind,project_id,payload,state,next_try,"
        "created_at,updated_at)"
        " VALUES('new_messages',60,'{}','pending',0,0,0),"
        "       ('new_messages',60,'{}','failed',0,0,0),"
        "       ('new_messages',61,'{\"message_ids\":[55]}','failed',0,0,0),"
        "       ('run_failed',NULL,'{}','pending',0,0,0)")
    db.db.commit()
    db.upsert_patient_info(_unread_patient(60), is_archived=True)

    sent = []
    monkeypatch.setattr(notify_flush, "_hermes_exe", lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_flush, "_target", lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_flush, "_send",
                        lambda *a, **k: sent.append(a) or None)

    res = notify_flush.flush(db)
    # BOTH of archived pid 60's events — pending AND already-failed —
    # are dropped terminally; pid 61's retryable event and the system
    # event still send normally (F2)
    assert res["suppressed"] == 2 and res["sent"] == 2
    assert len(sent) == 2
    rows = db.db.execute(
        "SELECT project_id,state FROM notify_outbox ORDER BY event_id"
    ).fetchall()
    assert [r["state"] for r in rows] == [
        "suppressed", "suppressed", "accepted", "accepted"]
    db.close()


def test_unread_reappearance_unarchives_and_notifies(tmp_path):
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(61), is_archived=True)
    p = _unread_patient(61)
    p.messages = [_message(mid=2, project_id=61)]
    p.fetch_state = "complete"
    new_ids = db.save_patient(p, notify={"source": "unread"})
    # positive reappearance in the unread path clears the flag in the
    # same transaction as the save, so the notify intent lands (F4)
    assert new_ids == [2]
    assert not db.is_archived(61)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 1
    db.close()


def test_frontier_patients_excludes_archived(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(62)
    db.upsert_patient_info(_unread_patient(63), is_archived=True)
    assert [r["project_id"] for r in db.frontier_patients()] == [62]
    assert {r["project_id"] for r in db.known_patients()} == {62, 63}
    db.close()


def test_history_floor_withheld_for_nonterminal_parent(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(70)
    db.job_add("history", 70,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=5, project_id=70,
                                   state="snippet")],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # a parent whose body never completed must not certify the floor (F9)
    assert db.history_floor(70) == 0
    assert db.job_state("history", 70) == "pending"
    db.close()


def test_history_stalled_window_fails_visibly(tmp_path):
    # a 'snippet' parent can never be upgraded (no API surface returns
    # its full body), so a checkpoint-unsafe window re-walked forever
    # must eventually fail instead of looping silently (P-2)
    db = _ledger(tmp_path)
    db.ensure_patient(72)
    db.job_add("history", 72,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=5, project_id=72,
                                   state="snippet")],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(job_ops.HISTORY_STALL_LIMIT):
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    assert db.job_state("history", 72) == "failed"
    assert "import 72: window_stalled" in result["errors"]
    assert db.history_floor(72) == 0
    db.close()


def test_history_batch_error_consumes_attempts(tmp_path):
    """AUDIT-J03: an embedded batch.error must consume job attempts like
    a raised MCSError — a permanent mid-walk failure (gone project, lost
    permission) has to reach 'failed', not defer every interval forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(73)
    db.job_add("history", 73,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                [], pages=1, reached=False,
                error=mcs_adapter.MCSError("gone", retryable=False))

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(8):   # job_retry's default max_attempts
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    assert db.job_state("history", 73) == "failed"
    assert any("import 73: gone" in e for e in result["errors"])
    db.close()


def test_history_batch_session_expired_stays_attempt_free(tmp_path):
    """An embedded SessionExpired aborts the run like the raised path —
    auth failure is not a per-job failure and must not consume attempts."""
    db = _ledger(tmp_path)
    db.ensure_patient(74)
    db.job_add("history", 74,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, *a, **k):
            return mcs_adapter.MessageBatch(
                [], pages=0, reached=False,
                error=mcs_adapter.SessionExpired(status=401))

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    with pytest.raises(mcs_adapter.SessionExpired):
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    row = db.db.execute(
        "SELECT attempts, state FROM fetch_jobs "
        "WHERE kind='history'").fetchone()
    assert row["attempts"] == 0 and row["state"] == "pending"
    db.close()


def test_history_stall_counter_resets_on_progress(tmp_path):
    # stalls count consecutive unsafe windows only — once the cursor
    # advances (checkpoint safe) the counter clears (P-2)
    db = _ledger(tmp_path)
    db.ensure_patient(73)
    db.job_add("history", 73,
               payload={"since": 0, "page": 1, "pages": 1,
                        "trickle": True})
    calls = {"n": 0}

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            calls["n"] += 1
            state = "snippet" if calls["n"] == 1 else "full"
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=calls["n"], project_id=73,
                                   state=state)],
                pages=1, reached=calls["n"] > 1)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    for _ in range(2):
        db.db.execute(
            "UPDATE fetch_jobs SET next_try=0 WHERE kind='history'")
        db.db.commit()
        job_ops.run_history_jobs(Adapter(), db, result,
                                 time.monotonic() + 300, trickle=True)
    # first pass stalled once (snippet), second advanced + floored
    assert db.job_state("history", 73) == "done"
    assert db.history_floor(73) == -1
    db.close()


def test_history_floor_set_when_all_parents_terminal(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(71)
    db.job_add("history", 71,
               payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=7, project_id=71)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.history_floor(71) == -1
    assert db.job_state("history", 71) == "done"
    db.close()


def test_failed_thread_with_partial_replies_not_checkpoint_safe():
    # embedded reply present but count unmet + thread fetch fails —
    # the walk must not advance coverage over an unverified thread (F3)
    class Adapter:
        def fetch_thread(self, *_):
            raise mcs_adapter.MCSError("broken")

    m = _message(mid=10)
    m.reply_count = 2
    m.replies = [_message(mid=20, state="full", parent_id=10)]
    stats = {"errors": [], "threads": 0}
    merged = job_ops.merge_full_replies(
        Adapter(), [m], 0, time.monotonic() + 5, stats)
    assert not merged.checkpoint_safe


def test_reply_job_saves_thread_siblings(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(80)
    db.job_add("reply", 80, message_id=21, parent_id=20)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=21, project_id=80),
                    _message(mid=22, project_id=80)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    saved = {r["message_id"]: r["parent_id"] for r in db.db.execute(
        "SELECT message_id,parent_id FROM messages")}
    assert saved == {21: 20, 22: 20}
    assert db.job_state("reply", 80, message_id=21) == "done"
    db.close()


def test_reply_job_excludes_thread_root(tmp_path):
    """C2: a thread response that embeds its own root must not store the
    parent as a self-referencing reply row."""
    db = _ledger(tmp_path)
    db.ensure_patient(81)
    db.job_add("reply", 81, message_id=31, parent_id=30)

    class Adapter:
        def fetch_thread(self, pid, mid):
            # API contract violation defence: the parent is in the list
            return [_message(mid=30, project_id=81),
                    _message(mid=31, project_id=81)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    rows = {r["message_id"]: r["parent_id"] for r in db.db.execute(
        "SELECT message_id,parent_id FROM messages")}
    assert rows == {31: 30}          # no self-referencing row for 30
    db.close()


def test_reply_job_unread_sibling_notifies_once(tmp_path):
    """R4: a brand-new sibling reply stored by a reply-job drain must
    still produce exactly one notify intent — 'stored' and 'notified'
    are separate facts."""
    db = _ledger(tmp_path)
    db.ensure_patient(82)
    db.job_add("reply", 82, message_id=41, parent_id=40)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=41, project_id=82, unread=True),
                    _message(mid=42, project_id=82, unread=True)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    assert len(rows) == 1
    assert set(json.loads(rows[0]["payload"])["message_ids"]) == {41, 42}
    # a second identical drain produces no duplicate
    db.job_add("reply", 82, message_id=43, parent_id=40)

    class Adapter2:
        def fetch_thread(self, pid, mid):
            return [_message(mid=41, project_id=82, unread=True),
                    _message(mid=42, project_id=82, unread=True),
                    _message(mid=43, project_id=82, unread=False)]

    job_ops.run_reply_jobs(Adapter2(), db, result, time.monotonic() + 60)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 1
    db.close()


def test_unread_save_notifies_pre_stored_unread(tmp_path):
    """R4 other direction: a reply stored silently (e.g. as read-context)
    but still unread must join the notify intent when the unread path
    re-observes it."""
    db = _ledger(tmp_path)
    db.ensure_patient(83)
    db.save_messages([_message(mid=51, project_id=83, unread=True)],
                     project_id=83)                       # stored, unnotified
    p = _unread_patient(83)
    m = _message(mid=50, project_id=83, unread=True)
    m.replies = [_message(mid=51, project_id=83, unread=True,
                         parent_id=50)]
    p.messages = [m]
    p.fetch_state = "complete"
    db.save_patient(p, notify={"source": "unread"})
    ids = set(json.loads(db.db.execute(
        "SELECT payload FROM notify_outbox").fetchone()["payload"]
        )["message_ids"])
    assert ids == {50, 51}
    db.close()


def test_old_history_reply_does_not_notify(tmp_path):
    """Reply-job saves of already-read history must stay silent — the
    unread-only intent gate keeps deep imports quiet."""
    db = _ledger(tmp_path)
    db.ensure_patient(84)
    db.job_add("reply", 84, message_id=61, parent_id=60)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=61, project_id=84, unread=False),
                    _message(mid=62, project_id=84, unread=False)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.db.execute(
        "SELECT count(*) c FROM notify_outbox").fetchone()["c"] == 0
    db.close()


def test_snippet_parent_blocks_cursor_advance(tmp_path):
    """R3: a non-terminal parent must freeze the walk cursor, not just
    withhold the floor — otherwise later batches certify around it."""
    db = _ledger(tmp_path)
    db.ensure_patient(85)
    db.job_add("history", 85,
               payload={"since": 0, "page": 1, "trickle": True})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            calls.append(start_page)
            state = "snippet" if start_page == 1 else "full"
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=start_page, project_id=85,
                                   state=state)],
                pages=1, reached=start_page == 1)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # batch reached the end but the snippet parent blocked the checkpoint
    assert db.history_floor(85) == 0
    job = db.job_pending("history", 85)
    assert json.loads(job["payload"])["page"] == 1   # cursor NOT advanced
    db.close()


def test_history_drain_rotates_fairly(tmp_path):
    db = _ledger(tmp_path)
    for pid in (95, 96):
        db.ensure_patient(pid)
        db.job_add("history", pid,
                   payload={"since": 0, "page": 1, "trickle": True})

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=pid, project_id=pid)],
                pages=1, reached=False)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True,
                             max_jobs=1)
    # 95 was worked and deferred — its updated_at bump moves it behind
    # 96, so a chronically incomplete patient cannot starve others (F8)
    assert db.history_jobs_due()[0]["project_id"] == 96
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True,
                             max_jobs=1)
    assert db.history_jobs_due()[0]["project_id"] == 95
    db.close()


class _DiscoveryAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, projects=None, kartes=None, fail=False):
        self._projects = projects or []
        self._kartes = kartes or []
        self.fail = fail

    def list_projects(self):
        if self.fail:
            raise mcs_adapter.MCSError("broken")
        return list(self._projects)

    def list_archived_kartes(self):
        if self.fail:
            raise mcs_adapter.MCSError("broken")
        return list(self._kartes)


def _force_due(db):
    db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='discovery'")
    db.db.commit()


def test_discovery_never_dies_and_recovers(tmp_path):
    db = _ledger(tmp_path)
    job_ops.seed_discovery(db)
    adapter = _DiscoveryAdapter(fail=True)
    result = {"errors": []}
    for _ in range(10):   # far beyond the old 8-attempt burnout limit
        _force_due(db)
        job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    job = db.job_pending("discovery", 0)
    assert job is not None                       # still alive (F7)
    assert result["errors"].count("discovery: broken") == 10

    adapter.fail = False
    adapter._projects = [_unread_patient(90)]
    _force_due(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=90"
                         ).fetchone()
    assert result["discovery"]["projects"] == 1

    # and it survives another failure after success
    adapter.fail = True
    _force_due(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600)
    assert db.job_pending("discovery", 0) is not None
    db.close()


def test_discovery_registers_archived_and_delta_job(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(91)
    db.save_messages([_message(mid=9, project_id=91)], project_id=91)
    wm = db.high_watermark(91)
    db.set_coverage(91, wm)                      # verified upper boundary
    db.set_history_floor(91, -1)                 # fully walked, then archived
    # a later unread-path store bumps the watermark past the verified
    # boundary — the head anchor must stay at coverage, not hwm (F01)
    db.save_messages([_message(mid=10, project_id=91)], project_id=91)

    adapter = _DiscoveryAdapter(
        projects=[], kartes=[_unread_patient(91), _unread_patient(92)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)

    assert db.is_archived(91) and db.is_archived(92)
    # 91 transitioned live->archived: a 'history_head' reservation was
    # committed in the SAME tx as the flag (R1); floored so the anchor
    # is the verified newest message (R6)
    j91 = json.loads(db.job_pending("history_head", 91)["payload"])
    assert j91["since"] == wm - ledger.HEAD_SYNC_OVERLAP_S
    assert j91["trickle"] is True
    # 92 is brand-new: no verified boundary -> conservative full walk
    j92 = json.loads(db.job_pending("history_head", 92)["payload"])
    assert j92["since"] == 0 and j92["trickle"] is True
    # the generic trickle seeder never touches archived patients
    assert db.job_pending("history", 91) is None
    assert db.job_pending("history", 92) is None
    assert result["discovery"]["archived"] == 2
    db.close()


def test_head_job_coexists_with_pending_history(tmp_path):
    """R2: a mid-walk deep-import job must not absorb the final head
    reconciliation — they coexist as different job kinds."""
    db = _ledger(tmp_path)
    db.ensure_patient(95)
    db.job_add("history", 95,
               payload={"since": 0, "page": 7, "trickle": True})
    db.upsert_patient_info(_unread_patient(95), is_archived=True)
    # the in-flight deep walk keeps its cursor; the head sync is separate
    assert json.loads(db.job_pending("history", 95)["payload"])["page"] == 7
    assert db.job_pending("history_head", 95) is not None
    db.close()


def test_head_job_done_preserves_floor(tmp_path):
    """R5: completing a bounded head sync must never regress floor=-1."""
    db = _ledger(tmp_path)
    db.ensure_patient(96)
    db.set_history_floor(96, -1)
    db.upsert_patient_info(_unread_patient(96), is_archived=True)

    class Adapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=50, project_id=96)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(Adapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.history_floor(96) == -1          # not overwritten
    assert db.job_state("history_head", 96) == "done"
    db.close()


def test_history_floor_is_monotonic(tmp_path):
    """R5 belt-and-suspenders: a shallower completion can never rewrite
    a deeper floor."""
    db = _ledger(tmp_path)
    db.ensure_patient(97)
    db.set_history_floor(97, -1)
    db.set_history_floor(97, 500)              # shallow -> ignored
    assert db.history_floor(97) == -1
    db.ensure_patient(98)
    db.set_history_floor(98, 800)
    db.set_history_floor(98, 900)              # shallower -> ignored
    assert db.history_floor(98) == 800
    db.set_history_floor(98, 300)              # deeper -> accepted
    assert db.history_floor(98) == 300
    db.close()


def test_archive_head_since_uses_verified_boundary(tmp_path):
    """R6/F01: the head anchor is always the VERIFIED boundary
    (coverage_ts) — never the high watermark, which any unread-path
    store bumps past verified coverage without fetching the gap."""
    db = _ledger(tmp_path)
    db.ensure_patient(99)
    db.save_messages([_message(mid=1, project_id=99)], project_id=99)
    db.set_coverage(99, db.high_watermark(99) - 1000)
    assert db.archive_head_since(99) == \
        db.coverage_ts(99) - ledger.HEAD_SYNC_OVERLAP_S
    # floor=-1 does NOT upgrade the anchor to the current watermark —
    # a post-coverage store only proves the new post exists
    db.set_history_floor(99, -1)
    db.save_messages([_message(mid=2, project_id=99)], project_id=99)
    assert db.high_watermark(99) > db.coverage_ts(99)
    assert db.archive_head_since(99) == \
        db.coverage_ts(99) - ledger.HEAD_SYNC_OVERLAP_S
    db.close()


def test_discovery_kartes_failure_preserves_active(tmp_path):
    """R7: a /kartes failure must not discard already-fetched active
    registrations or unarchive-on-reappearance work."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(97), is_archived=True)
    assert db.is_archived(97)

    class Adapter(_DiscoveryAdapter):
        def list_archived_kartes(self):
            raise mcs_adapter.MCSError("broken")

    adapter = Adapter(projects=[_unread_patient(97), _unread_patient(98)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)
    # active-side work landed anyway: reactivation + new registration
    assert not db.is_archived(97)
    assert db.db.execute("SELECT 1 FROM patients WHERE project_id=98"
                         ).fetchone()
    # and the job stays pending on a short retry, not burnt or done
    job = db.job_pending("discovery", 0)
    assert job is not None
    assert any("archived" in e for e in result["errors"])
    db.close()


def test_discovery_archived_off_skips_kartes(tmp_path):
    """C1: discover_archived=False gates only NEW archived enumeration;
    a pending head job still drains."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(90), is_archived=True)
    assert db.job_pending("history_head", 90) is not None

    class Adapter(_DiscoveryAdapter):
        def list_archived_kartes(self):
            raise AssertionError("kartes must not be called")

    adapter = Adapter(projects=[_unread_patient(95)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=False)
    assert result["discovery"]["archived"] == 0

    class HAdapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=1, project_id=pid)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []

    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    # in-flight archived work drains regardless of the switch
    assert db.job_state("history_head", 90) == "done"
    db.close()


def test_seed_trickle_never_seeds_archived(tmp_path):
    """Archived patients ride 'history_head' reservations, never the
    generic deep-import seeder."""
    db = _ledger(tmp_path)
    db.upsert_patient_info(_unread_patient(93), is_archived=True)
    db.ensure_patient(94)
    n = job_ops.seed_trickle(db)
    assert n == 1
    assert db.job_state("history", 94) == "pending"
    assert db.job_state("history", 93) is None
    db.close()


# ---------- second-round review regressions (Oracle F01-F07) ----------


def test_reply_drain_retires_failed_sibling_job(tmp_path):
    """F05-A: a burnt-out reply job must be retired when a later thread
    fetch returns that reply's full body — a stale failed job would
    otherwise block floor certification forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=11, parent_id=10)
    j11 = db.job_pending("reply", 1, message_id=11)["job_id"]
    db.job_fail(j11)                       # burnt out
    db.job_add("reply", 1, message_id=12, parent_id=10)

    class Adapter:
        def fetch_thread(self, pid, mid):
            # fetching for 12 returns the whole thread: 11 is full too
            return [_message(mid=11, project_id=1),
                    _message(mid=12, project_id=1)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=11) == "done"
    assert db.job_state("reply", 1, message_id=12) == "done"
    assert db.pending_reply_jobs(1) == 0
    db.close()


def test_reply_drain_enqueues_incomplete_sibling(tmp_path):
    """F05-C: a newly returned still-incomplete sibling must get a
    durable retry job in the same commit as its save."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=11, parent_id=10)

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=11, project_id=1),
                    _message(mid=13, project_id=1, state="snippet")]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=11) == "done"
    j13 = db.job_pending("reply", 1, message_id=13)
    assert j13 is not None                 # durable retry reserved
    assert j13["parent_id"] == 10
    db.close()


def test_head_job_blocked_only_by_live_reply_work(tmp_path):
    """Floor/head certification waits on LIVE reply work. A burnt-out
    'failed' job is a bounded give-up — it stays recorded for audit
    but cannot stall the walk forever (permanently body-less replies
    exist: stamps/system posts). Merge revives failed jobs whenever
    the reply is re-encountered, so transient failures still heal."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    db.job_add("reply", 1, message_id=11, parent_id=10)

    class HAdapter:
        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            return mcs_adapter.MessageBatch(
                messages=[_message(mid=10, project_id=1)], pages=1,
                reached=True)

        def fetch_thread(self, *a):
            return []

    result = {"errors": []}
    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.job_state("history_head", 1) == "pending"  # live job blocks

    # burn the job out -> it stops blocking and the head completes;
    # the failed row remains as the give-up receipt
    j11 = db.job_pending("reply", 1, message_id=11)["job_id"]
    db.job_fail(j11)
    db.db.execute("UPDATE fetch_jobs SET next_try=0 "
                  "WHERE kind='history_head'")
    db.db.commit()
    job_ops.run_history_jobs(HAdapter(), db, result,
                             time.monotonic() + 300, trickle=True)
    assert db.job_state("history_head", 1) == "done"
    db.close()


def test_orphan_reply_gets_durable_job(tmp_path):
    """Replies stored outside a merge (unread path, sibling saves) with
    no fetch_job in any state get a durable reservation — otherwise
    they stay 'snippet'/'unknown' forever."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=21, project_id=1, parent_id=20,
                               state="snippet")], project_id=1)
    assert db.job_state("reply", 1, message_id=21) is None

    class Adapter:
        def fetch_thread(self, pid, mid):
            return [_message(mid=21, project_id=1)]

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result, time.monotonic() + 60)
    assert db.job_state("reply", 1, message_id=21) == "done"
    # resolved rows are never re-seeded
    assert db.replies_without_job() == []
    db.close()


class _PagedThreadAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def _get(self, path, params=None, extend_session=True):
        page = (params or {}).get("page", 1)
        self.calls.append(page)
        msgs, has_next = self.pages.get(page, ([], False))
        return {"messages": [
            {"id": mid, "user": {"id": 1, "type": "medical"},
             "created_at": "2026-09-19T00:00:00+09:00",
             "comment": "body", "count": {}}
            for mid in msgs],
            "paginate": {"has_next": has_next}}


def test_fetch_thread_paginates_to_completion():
    """Threads beyond one page were previously truncated/failed
    forever — every page is now walked until has_next is false."""
    a = _PagedThreadAdapter({1: ([11, 12], True), 2: ([13], False)})
    out = a.fetch_thread(1, 10)
    assert [m.message_id for m in out] == [11, 12, 13]
    assert a.calls == [1, 2]

    # a thread that never terminates raises rather than certify
    b = _PagedThreadAdapter({p: ([p], True) for p in range(1, 12)})
    with pytest.raises(mcs_adapter.MCSError) as e:
        b.fetch_thread(1, 10, max_pages=5)
    assert e.value.kind == "thread_incomplete"
    assert b.calls == [1, 2, 3, 4, 5]


def test_rearchive_resets_pending_head_cursor(tmp_path):
    """F02: re-archiving while a head job is still queued must restart
    it at page 1 (posts arrived during reactivation) and keep the
    deeper of the two since anchors."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1)], project_id=1)
    db.set_coverage(1, db.high_watermark(1))   # verified boundary
    db.set_history_floor(1, -1)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    # pretend the head walk made progress since reservation
    db.db.execute(
        "UPDATE fetch_jobs SET payload=? WHERE kind='history_head'",
        (json.dumps({"since": 500, "page": 4, "trickle": True}),))
    db.db.commit()
    db.upsert_patient_info(_unread_patient(1), is_archived=False)
    db.upsert_patient_info(_unread_patient(1), is_archived=True)
    j = json.loads(db.job_pending("history_head", 1)["payload"])
    assert j["page"] == 1                  # cursor restarted
    # new anchor = coverage-120 (deep), old pending anchor = 500
    # -> the deeper of the two wins
    assert j["since"] == min(500, db.coverage_ts(1)
                           - ledger.HEAD_SYNC_OVERLAP_S)
    db.close()


def test_interrupted_v7_migration_reruns_backfill(tmp_path):
    """F03: a DB stopped between the notified_at column add and the
    version fence must complete the idempotent backfill on next open —
    column presence must not be mistaken for migration completion."""
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=False),
                      _message(mid=2, project_id=1, unread=True)],
                     project_id=1)
    # simulated crash point: column exists, backfill never ran, old fence
    db.db.execute("UPDATE messages SET notified_at=NULL")
    db.db.execute("PRAGMA user_version=6")
    db.db.commit()
    db.close()

    db2 = ledger.Ledger(path)
    rows = {r["message_id"]: r["notified_at"] for r in db2.db.execute(
        "SELECT message_id,notified_at FROM messages")}
    assert rows[1] is not None             # read message backfilled
    assert rows[2] is None                 # unread w/o intent stays NULL
    assert db2.db.execute(
        "PRAGMA user_version").fetchone()[0] == 7
    db2.close()


def test_migration_tolerates_malformed_outbox_payload(tmp_path):
    """F07: non-object / non-list legacy outbox payloads must not abort
    the migration — they carry no usable message ids."""
    path = str(tmp_path / "ledger.db")
    db = ledger.Ledger(path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=False),
                      _message(mid=2, project_id=1, unread=True)],
                     project_id=1)
    db.db.execute("UPDATE messages SET notified_at=NULL")
    db.db.execute("PRAGMA user_version=6")
    for bad in ("[1,2,3]", "not json", '"text"',
                '{"message_ids":"oops"}', '{"message_ids":[2,"x",-3]}'):
        db.db.execute(
            "INSERT INTO notify_outbox(kind,project_id,payload,state,"
            "next_try,created_at,updated_at) "
            "VALUES('new_messages',1,?,'pending',0,0,0)", (bad,))
    db.db.commit()
    db.close()

    db2 = ledger.Ledger(path)              # must not raise
    rows = {r["message_id"]: r["notified_at"] for r in db2.db.execute(
        "SELECT message_id,notified_at FROM messages")}
    assert rows[1] is not None
    assert rows[2] is not None             # valid id 2 inside the mixed
                                           # list still counts as intented
    assert db2.db.execute(
        "PRAGMA user_version").fetchone()[0] == 7
    db2.close()


def test_stored_unread_flag_does_not_notify_after_read(tmp_path):
    """F04: the stored is_unread column is sticky (MAX = ever-unread).
    A message stored unread but reported READ by the next fetch must not
    notify — the fetch's own flag is the authority for 'unread now'."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.save_messages([_message(mid=1, project_id=1, unread=True)],
                     project_id=1)         # stored unread, unnotified
    p = _unread_patient(1)
    m = _message(mid=1, project_id=1, unread=False)
    p.messages = [m]
    db.save_patient(p, notify={"source": "unread"})
    rows = db.db.execute(
        "SELECT payload FROM notify_outbox").fetchall()
    ids = {i for r in rows
           for i in json.loads(r["payload"]).get("message_ids", [])}
    assert 1 not in ids                    # read at fetch time: silent
    db.close()


def test_discovery_repairs_archived_without_work(tmp_path):
    """F06: an already-archived patient with neither in-flight jobs nor
    a certified floor gets a full re-walk reservation; floored or
    actively-walking patients are left alone."""
    db = _ledger(tmp_path)
    for pid in (1, 2, 3, 4):
        db.upsert_patient_info(_unread_patient(pid), is_archived=True)
        db.db.execute("DELETE FROM fetch_jobs")   # drop the reservation
        db.db.commit()
    db.save_messages([_message(mid=3, project_id=3)], project_id=3)
    db.set_coverage(3, db.high_watermark(3))     # verified boundary
    db.set_history_floor(3, -1)                  # fully walked
    db.job_add("history", 4, payload={"since": 0, "page": 2,
                                      "trickle": True})

    adapter = _DiscoveryAdapter(projects=[],
        kartes=[_unread_patient(1), _unread_patient(2),
                _unread_patient(3), _unread_patient(4)])
    result = {"errors": []}
    job_ops.seed_discovery(db)
    job_ops.run_discovery(adapter, db, result, time.monotonic() + 600,
                          include_archived=True)

    # uncovered archived patients get a bounded full re-walk head job
    for pid in (1, 2):
        j = db.job_pending("history_head", pid)
        assert j is not None
        assert json.loads(j["payload"])["since"] == 0
    # floored patient still gets a bounded head job — its floor predates
    # archival possibly, so a post-floor gap may exist (F06). The anchor
    # is the VERIFIED boundary (coverage), never the watermark (F01)
    j3 = db.job_pending("history_head", 3)
    assert j3 is not None
    assert json.loads(j3["payload"])["since"] == \
        db.coverage_ts(3) - ledger.HEAD_SYNC_OVERLAP_S
    # pending deep walk already covers patient 4 — no duplicate head
    assert db.job_pending("history_head", 4) is None
    assert json.loads(db.job_pending("history", 4)["payload"])["page"] == 2
    db.close()


def test_reply_job_window_resumes_across_pages(tmp_path):
    """F03: a thread longer than one window resumes at its durable
    cursor instead of restarting at page 1."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, message_id=30, parent_id=10)
    calls = []

    class Adapter:
        def fetch_thread_window(self, pid, mid, start_page=1,
                                max_pages=10):
            calls.append(start_page)
            if start_page == 1:
                return mcs_adapter.MessageBatch(
                    [_message(mid=20, parent_id=10)], pages=10)
            return mcs_adapter.MessageBatch(
                [_message(mid=30, parent_id=10)], pages=1, reached=True)

    result = {"errors": []}
    job_ops.run_reply_jobs(Adapter(), db, result,
                           time.monotonic() + 100)

    assert calls == [1]
    assert db.job_state("reply", 1, 30) == "pending"
    payload = json.loads(db.job_pending("reply", 1, 30)["payload"])
    assert payload["page"] == 11
    assert db.db.execute(
        "SELECT body_state FROM messages WHERE message_id=20"
    ).fetchone()[0] == "full"
    assert result["reply_windows"] == [{"mid": 30, "next_page": 11}]

    job_ops.run_reply_jobs(Adapter(), db, {"errors": []},
                           time.monotonic() + 100)
    assert calls == [1, 11]
    assert db.job_state("reply", 1, 30) == "done"
    db.close()


def test_reconcile_job_rewalks_history_without_since(tmp_path):
    """F04: reconcile is the only surface that refetches below the
    since-cutoff — edits/deletions on old posts become visible."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 1})
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, max_pages=1,
                          start_page=None):
            calls.append((pid, since, start_page))
            return mcs_adapter.MessageBatch(
                [_message(mid=50)], pages=2, reached=False)

    result = {"errors": []}
    job_ops.run_reconcile_jobs(Adapter(), db, result,
                               time.monotonic() + 100)

    assert calls == [(1, 0, 1)]
    payload = json.loads(db.job_pending("reconcile", 1)["payload"])
    assert payload["page"] == 3
    assert db.db.execute(
        "SELECT count(*) FROM messages WHERE message_id=50"
    ).fetchone()[0] == 1
    db.close()


def test_reconcile_full_pass_parks_then_rotates(tmp_path):
    """F04: after a complete pass the job idles for the rotation
    interval and restarts at page 1."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reconcile", 1, payload={"page": 5})

    class Adapter:
        def fetch_history(self, *a, **k):
            return mcs_adapter.MessageBatch([], pages=0, reached=True)

    job_ops.run_reconcile_jobs(Adapter(), db, {"errors": []},
                               time.monotonic() + 100)
    job = db.job_pending("reconcile", 1)
    payload = json.loads(job["payload"])
    assert payload["page"] == 1
    assert job["next_try"] > \
        time.time() + job_ops.RECONCILE_INTERVAL_S - 60
    db.close()


def test_seed_reconcile_only_floored_patients(tmp_path):
    """F04: only a completed deep import earns a reconcile job —
    mid-import patients are still covered by their own cursor."""
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.ensure_patient(2)
    db.ensure_patient(3)
    db.set_history_floor(1, 100)
    db.set_history_floor(3, 0)
    job_ops.seed_reconcile(db)
    assert db.job_pending("reconcile", 1) is not None
    assert db.job_pending("reconcile", 2) is None
    assert db.job_pending("reconcile", 3) is not None
    db.close()


def test_reconcile_hydrates_changed_reply_and_rotates_patients(tmp_path):
    db = _ledger(tmp_path)
    for pid in (1, 2, 3):
        db.ensure_patient(pid)
        db.job_add("reconcile", pid, payload={"page": 1})
    root = _message(mid=10)
    root.reply_count = 1
    root.replies = [_message(mid=11, parent_id=10, body="previous")]
    db.save_messages([root])
    calls = []

    class Adapter:
        def fetch_history(self, pid, since, **kwargs):
            calls.append(pid)
            if pid != 1:
                return mcs_adapter.MessageBatch([], pages=2, reached=False)
            post = _message(mid=10)
            post.reply_count = 1
            post.replies = [_message(mid=11, parent_id=10, state="snippet")]
            return mcs_adapter.MessageBatch([post], pages=2, reached=False)

        def fetch_thread(self, *args):
            return [_message(mid=11, parent_id=10, body="corrected")]

    adapter = Adapter()
    job_ops.run_reconcile_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    job_ops.run_reconcile_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert calls[:3] == [1, 2, 3]
    assert db.db.execute("SELECT body_text FROM messages WHERE message_id=11").fetchone()[0] == "corrected"
    db.close()


def test_reply_target_found_early_keeps_thread_tail_work(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, 11, parent_id=10)

    class Adapter:
        def fetch_thread_window(self, pid, root, start_page=1):
            if start_page == 1:
                return mcs_adapter.MessageBatch(
                    [_message(mid=11, parent_id=10)], pages=10)
            assert start_page == 11
            return mcs_adapter.MessageBatch(
                [_message(mid=111, parent_id=10)], pages=1, reached=True)

    adapter = Adapter()
    job_ops.run_reply_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert db.job_state("reply", 1, 11) == "done"
    assert db.pending_reply_jobs(1) == 1
    job_ops.run_reply_jobs(adapter, db, {"errors": []}, time.monotonic() + 300)
    assert db.db.execute("SELECT 1 FROM messages WHERE message_id=111").fetchone()
    assert db.pending_reply_jobs(1) == 0
    db.close()


def test_thread_partial_pages_survive_session_loss(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    db.job_add("reply", 1, 11, parent_id=10)

    class Adapter(mcs_adapter.MCSAdapter):
        def _get(self, path, params=None, **kwargs):
            if params["page"] == 2:
                raise mcs_adapter.SessionExpired(status=401)
            return {"messages": [{"id": 11, "comment": "synthetic"}],
                    "paginate": {"has_next": True}}

    with pytest.raises(mcs_adapter.SessionExpired):
        job_ops.run_reply_jobs(Adapter(), db, {"errors": []}, time.monotonic() + 300)
    assert db.db.execute("SELECT body_state FROM messages WHERE message_id=11").fetchone()[0] == "full"
    pending = db.job_pending("thread", 1, 10)
    assert json.loads(pending["payload"])["page"] == 2
    assert pending["attempts"] == 0
    db.close()
