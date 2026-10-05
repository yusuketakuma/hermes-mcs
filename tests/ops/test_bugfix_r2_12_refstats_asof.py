"""Capture at a fractional generated_at must replay identically on verify."""
from datetime import datetime, timedelta, timezone

import ledger
import mcs_adapter
import mcs_refstats
import mcs_requests as requests
from test_mcs_refstats import _approve_req, _db, _pending_hash

GEN = 1790000000.6


def test_capture_pins_integer_as_of_so_verify_passes(tmp_path, monkeypatch, capsys):
    # post exactly on the 7d lower bound at int(GEN): excluded at the
    # float as_of, included at the replayed whole-second as_of
    ts = int(GEN) - 7 * 86400
    iso = datetime.fromtimestamp(ts, timezone(timedelta(hours=9))).isoformat()
    db = _db(tmp_path)
    db.save_messages([mcs_adapter.Message(
        message_id=1, project_id=1, parent_id=None, sender_id=1,
        sender_name="s", sender_type="user", profession="看護師",
        organization="", posted_at=iso, body_html="x", body_state="full",
        is_unread=False, reply_count=0)])
    monkeypatch.setattr(ledger.time, "time", lambda: GEN)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    monkeypatch.undo()
    assert mcs_refstats.main(["capture", "--name", "b", "--preset", "operational",
                              "--snapshot", str(snap), "--data-dir", str(tmp_path)]) == 0
    r = requests.apply_command(db, _approve_req("b", _pending_hash(tmp_path, "b")))
    assert r["outcome"] == "applied", r
    # republish so the snapshot carries the approval artifact; the
    # pinned as_of freezes the window, so same data must still match
    monkeypatch.setattr(ledger.time, "time", lambda: GEN + 60)
    snap = ledger.publish_snapshot(str(tmp_path / "ledger.db"), str(tmp_path / "snap"))
    monkeypatch.undo()
    capsys.readouterr()
    rc = mcs_refstats.main(["verify", "--name", "b", "--snapshot", str(snap),
                            "--data-dir", str(tmp_path)])
    out = capsys.readouterr().out
    db.close()
    assert rc == 0, out
