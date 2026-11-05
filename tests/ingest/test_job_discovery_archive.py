"""Archived-patient discovery, registration and active/archive transitions."""
import mcs_contract
import json
import time
import pytest
import job_ops
import ledger
import mcs_adapter
import notify_flush
import notify_sender
import mcs_util
from ingest_testkit import _ledger, _message, _unread_patient


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
    with pytest.raises(mcs_contract.SchemaError):
        bad.list_archived_kartes()

    capped = _KartesAdapter([
        {"kartes": [], "paginate": {"has_next": True}}])
    with pytest.raises(mcs_contract.MCSError) as e:
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
    monkeypatch.setattr(mcs_util, 'hermes_exe', lambda *a: "/bin/sh")
    monkeypatch.setattr(notify_sender, 'target', lambda cfg, kind: "chan")
    monkeypatch.setattr(notify_sender, 'send',
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


class _DiscoveryAdapter(mcs_adapter.MCSAdapter):
    def __init__(self, projects=None, kartes=None, fail=False):
        self._projects = projects or []
        self._kartes = kartes or []
        self.fail = fail

    def list_projects(self):
        if self.fail:
            raise mcs_contract.MCSError("broken")
        return list(self._projects)

    def list_archived_kartes(self):
        if self.fail:
            raise mcs_contract.MCSError("broken")
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
            raise mcs_contract.MCSError("broken")

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
            return mcs_contract.MessageBatch(
                messages=[_message(mid=pid, project_id=pid)],
                pages=1, reached=True)

        def fetch_thread(self, *a):
            return []
        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return mcs_contract.MessageBatch(self.fetch_thread(pid, parent_id), pages=1, reached=True)

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
