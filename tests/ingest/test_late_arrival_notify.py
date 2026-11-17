"""Quiet history walks still announce a post newer than everything stored (2026-11-15)."""
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from types import SimpleNamespace

import job_ops
import ledger
import mcs_contract
import run_check
from ingest_testkit import _ledger, _message

JST = timezone(timedelta(hours=9))
DAY = 86400


def _iso(ts):
    return datetime.fromtimestamp(ts, JST).isoformat(timespec="seconds")


def _events(db):
    return [json.loads(r[0]) for r in db.db.execute(
        "SELECT payload FROM notify_outbox WHERE kind='new_messages' ORDER BY event_id")]


def _reconcile(db, messages, notify_max_age_s):
    class Adapter:
        def fetch_history(self, pid, since, **kwargs):
            return mcs_contract.MessageBatch(list(messages), pages=1, reached=True)

        def fetch_thread(self, pid, parent_id):
            return [r for m in messages for r in m.replies if r.parent_id == parent_id]

        def fetch_thread_window(self, pid, parent_id, start_page=1):
            return mcs_contract.MessageBatch(self.fetch_thread(pid, parent_id), pages=1, reached=True)

    db.job_add("reconcile", 1, payload={"page": 1})
    job_ops.run_reconcile_jobs(Adapter(), db, {"errors": []}, time.monotonic() + 100,
                               notify_max_age_s=notify_max_age_s)


@pytest.mark.parametrize("max_age", [DAY, None])
def test_reconcile_announces_a_read_post_newer_than_everything_stored(tmp_path, max_age):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    now = time.time()
    db.save_messages([_message(mid=10, posted_at=_iso(now - 2 * 3600))], project_id=1)
    assert _events(db) == []  # a first import stays quiet

    _reconcile(db, [_message(mid=11, posted_at=_iso(now - 600)),
                    _message(mid=10, posted_at=_iso(now - 2 * 3600))], max_age)

    assert [(e["source"], e["message_ids"]) for e in _events(db)] == [("history", [11])]
    db.save_messages([_message(mid=11, posted_at=_iso(now - 600))], project_id=1,
                     notify={"source": "history"}, notify_max_age_s=DAY, notify_all_new=True)
    assert len(_events(db)) == 1  # a later notifying walk does not repeat it
    db.close()


def test_late_root_does_not_carry_an_old_new_reply_with_notify_all_replies(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"), notify_all_replies=True)
    db.ensure_patient(1)
    now = time.time()
    db.save_messages([_message(mid=10, posted_at=_iso(now - 90 * DAY)),
                      _message(mid=20, posted_at=_iso(now - 3600))], project_id=1)
    old_root = _message(mid=10, posted_at=_iso(now - 90 * DAY))
    old_root.reply_count = 1
    old_root.replies = [_message(mid=12, parent_id=10, posted_at=_iso(now - 60 * DAY))]

    _reconcile(db, [_message(mid=21, posted_at=_iso(now - 300)), old_root], DAY)

    assert [e["message_ids"] for e in _events(db)] == [[21]]
    assert db.has_message(12)
    db.close()


def test_quiet_save_keeps_a_late_post_older_than_the_age_bound_quiet(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    now = time.time()
    db.save_messages([_message(mid=10, posted_at=_iso(now - 10 * DAY))], project_id=1)

    db.save_messages([_message(mid=12, posted_at=_iso(now - 3 * DAY))], project_id=1,
                     notify_max_age_s=DAY, notify_late=True)
    assert _events(db) == []  # newer than everything stored, but past the age bound

    db.save_messages([_message(mid=13, posted_at=_iso(now - 600))], project_id=1,
                     notify_max_age_s=DAY, notify_late=True)
    assert [e["message_ids"] for e in _events(db)] == [[13]]  # positive control
    db.close()


def test_late_path_notifies_only_rows_newer_than_the_stored_newest(tmp_path):
    db = _ledger(tmp_path)
    db.ensure_patient(1)
    now = time.time()
    db.save_messages([_message(mid=10, posted_at=_iso(now - 1000))], project_id=1)

    db.save_messages([_message(mid=11, posted_at=_iso(now - 500)),
                      _message(mid=12, posted_at=_iso(now - 3000))], project_id=1,
                     notify_max_age_s=DAY, notify_late=True)
    assert [e["message_ids"] for e in _events(db)] == [[11]]
    assert db.has_message(12)  # stored, but older than the newest stored post

    db.save_messages([_message(mid=13, posted_at=_iso(now - 4000))], project_id=1,
                     notify_max_age_s=DAY, notify_late=True)
    assert len(_events(db)) == 1  # the same kind of row stays quiet alone
    db.close()


def test_every_quiet_history_drain_gets_the_notify_age_bound(tmp_path, monkeypatch):
    import notify_cmds
    import notify_spool
    db = _ledger(tmp_path)
    seen = []
    for name in ("drain_commands", "seed_discovery", "run_discovery",
                 "run_reply_jobs", "seed_trickle"):
        monkeypatch.setattr(job_ops, name, lambda *a, **k: None)
    for name in ("run_history_jobs", "run_reconcile_jobs"):
        monkeypatch.setattr(job_ops, name, lambda *a, _n=name, **k: seen.append(
            (_n, k.get("trickle", False), k.get("notify_max_age_s"))))
    monkeypatch.setattr(run_check, "HOME", str(tmp_path))
    monkeypatch.setattr(notify_spool, "ensure_dirs", lambda *a: None)
    monkeypatch.setattr(notify_spool, "restore_pending", lambda *a: None)
    monkeypatch.setattr(notify_cmds, "drain_int_commands", lambda *a, **k: None)

    class Adapter:
        def self_profile(self):
            return {"sender_id": 1, "name": "synthetic", "professions": [],
                    "organizations": []}

        def station_staffs(self, stations):
            return []

    result = {"errors": []}
    run_check._run_jobs(Adapter(), db, SimpleNamespace(jobs_only=True, download_files=False),
                        {}, result, time.monotonic() + 300, False, 3600.0)
    assert result["errors"] == []
    assert sorted(seen) == [("run_history_jobs", False, 3600.0),
                            ("run_history_jobs", True, 3600.0),
                            ("run_reconcile_jobs", False, 3600.0)]
    db.close()
