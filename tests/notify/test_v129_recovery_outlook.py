"""v1.0.29 recovery outlook: held/unknown parts counted by fixed reason and
specs GC keeps, read-only and counts only; health shows the same report and
an over-budget read as unknown. Synthetic temp DB only."""
import json

import health_report
import ledger
import notify_recovery


def _render(con, did, state, *, transport="discord", spec=None, published=0):
    con.execute(
        "INSERT INTO notification_renders(delivery_id,op,render_rev,route_epoch,payload_hash,"
        "correlation,state,transport,spec_json,spec_published,created_at,updated_at)"
        " VALUES(?,'create',1,1,'h',?,?,?,?,?,1,1)",
        (did, "c" + did, state, transport, spec, published))


def _part(con, did, pid, state, code=None, kind="body_part", attempt=None):
    con.execute("INSERT INTO notification_render_parts(delivery_id,part_id,kind,idx,state,error_code,"
                "attempt_id) VALUES(?,?,?,1,?,?,?)", (did, pid, kind, state, code, attempt))


def _seeded(tmp_path):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    con = db.db
    _render(con, "d1", "held", spec='{"synthetic": 1}', published=1)
    _render(con, "d2", "delivered", transport="slack", spec='{"synthetic": 2}', published=1)
    _render(con, "d3", "delivered")
    _part(con, "d1", "p1", "held", "part_budget_exceeded")
    _part(con, "d1", "p2", "held")                                   # blocked by its parent
    _part(con, "d1", "p3", "held", "synthetic_worker_code")          # not a fixed reason
    _part(con, "d2", "p1", "unknown", "edit_target_unverified", "attachment_part", "a1")
    _part(con, "d2", "p2", "unknown", None, "thread")                # no attempt: not resolvable
    _part(con, "d3", "card", "unknown", "synthetic", "card", "a2")   # cards resolve elsewhere
    _part(con, "d3", "p1", "pending")
    con.commit()
    return db


def test_report_counts_every_fixed_reason_and_reads_only(tmp_path):
    db = _seeded(tmp_path)
    before = db.db.total_changes
    out = notify_recovery.part_hold_report(db)
    assert db.db.total_changes == before and not db.db.in_transaction
    held, unknown = out["parts"]["held"], out["parts"]["unknown"]
    assert set(held) == set(notify_recovery.PART_HOLD_REASONS) | {"not_recorded", "unknown"}
    assert held["part_budget_exceeded"] == 1 and held["not_recorded"] == 1 and held["unknown"] == 1
    assert held["card_revoked"] == 0                                 # observed zero, present
    assert unknown["edit_target_unverified"] == 1 and unknown["not_recorded"] == 1
    assert sum(unknown.values()) == 2                                # the card part is excluded
    assert out["resolvable"] == 1
    assert notify_recovery.card_part_resolve_plan(db, "d2", "p1").get("error") is None
    assert notify_recovery.card_part_resolve_plan(db, "d2", "p2")["error"] == "part_not_resolvable"
    assert out["specs"] == {"stored_json": 2, "published_files": 2, "kept_for_edit_check": 1}
    assert "synthetic_worker_code" not in json.dumps(out)
    db.close()


def test_health_shows_the_report_and_an_over_budget_read_is_unknown(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    out = health_report.part_holds_health(db.db, 5.0)
    assert out["status"] == "recorded" and out["observed_at"] == 5.0
    assert out["parts"]["held"]["part_budget_exceeded"] == 1
    monkeypatch.setattr(notify_recovery, "part_hold_report",
                        lambda _db: (_ for _ in ()).throw(RuntimeError("observation_limit")))
    assert health_report.part_holds_health(db.db, 6.0) == {
        "status": "unknown", "observed_at": 6.0, "reason": "observation_limit"}
    db.close()
