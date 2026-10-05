"""Regressions: attachment 401 on an expired session, and the self-probe
dropping a latest post in the same second as the stored watermark."""
import time
from types import SimpleNamespace

import pytest

import mcs_adapter
import run_check
from datetime import datetime

from ingest_testkit import _ledger, _message, _att, _msg_at, _unread_patient


def _states(db):
    return [tuple(r) for r in db.db.execute(
        "SELECT state,attempts FROM attachments")]


def _raise_401(*a):
    raise mcs_adapter.MCSError("http_error", status=401)


def test_expired_session_401_keeps_attachment_pending(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    m = _message()
    m.attachments = [_att("file0")]
    db.save_messages([m])
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    adapter = SimpleNamespace(download=_raise_401,
                              _probe_session_ok=lambda: False)
    with pytest.raises(mcs_adapter.SessionExpired):
        run_check.stage_attachments(adapter, db, {"errors": []},
                                    time.monotonic() + 100)
    assert _states(db) == [("pending", 0)]
    db.close()


def test_401_with_live_session_is_recorded(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    m = _message()
    m.attachments = [_att("file0")]
    db.save_messages([m])
    monkeypatch.setattr(run_check, "ATTACH_DIR", str(tmp_path))
    adapter = SimpleNamespace(download=_raise_401,
                              _probe_session_ok=lambda: True)
    result = {"errors": []}
    run_check.stage_attachments(adapter, db, result, time.monotonic() + 100)
    assert _states(db) == [("failed", 1)]
    db.close()


def test_self_probe_stores_same_second_latest_post(tmp_path):
    db = _ledger(tmp_path)
    ts = "2026-10-04T00:00:00+09:00"
    db.upsert_patient_info(_unread_patient(90))
    db.save_messages([_msg_at(100, 90, ts, unread=False)])
    own = _msg_at(101, 90, ts, unread=False)

    class Adapter:
        def fetch_latest(self, pid):
            return {"message_id": 101, "is_self_only": True}

        def fetch_history(self, pid, since, max_pages=10, start_page=1):
            # mirror MCSAdapter.fetch_history: items with ts <= since drop
            keep = [x for x in [own] if datetime.fromisoformat(x.posted_at).timestamp() > since]
            return mcs_adapter.MessageBatch(keep, pages=1, reached=True)

    result = {"errors": [], "new_messages": 0}
    run_check.stage_self_probe(Adapter(), db, result,
                               time.monotonic() + 300, run_id=1)
    assert db.has_message(101)
    assert result["new_messages"] == 1
    assert db.probe_marker(90) in (None, 101)
    db.close()
