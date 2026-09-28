"""Artifact JSON readers survive malformed meta/content under any join order."""
import json
import sqlite3

import pytest

import extract_llm
import ledger as ledger_mod
import mcs_signals
import mcs_stats

MALFORMED = ["{bad", "[1,2]", '"s"', "5", None]
PLANNERS = ["default", "skewed"]
MED_STATS = ["meds", "med_mentions", "med_change_burden", "rx_expiry",
             "med_change_followup", "canonical_facts"]


def _skew(conn):
    """Planner stats claiming artifacts are tiny and messages are huge,
    which makes SQLite scan the artifact side first."""
    conn.execute("ANALYZE")
    conn.execute("DELETE FROM sqlite_stat1")
    conn.executemany("INSERT INTO sqlite_stat1 VALUES (?,?,?)", [
        ("artifacts", "idx_artifacts_kind_msg", "10 10 1"),
        ("artifacts", None, "10"),
        ("messages", None, "1000000")])
    conn.commit()
    conn.execute("ANALYZE sqlite_schema")


def _add_raw(conn, kind, mid, content, meta):
    conn.execute(
        "INSERT INTO artifacts(kind,project_id,message_id,content,meta) "
        "VALUES (?,1,?,?,?)", (kind, mid, content, meta))


def _bad_content_rows(conn, raw, mid=2, chash="h2"):
    for kind in ("extract_v1", "extract_llm", "canonical_projection",
                 "semantic_facts_v4"):
        _add_raw(conn, kind, mid, raw, json.dumps({"hash": chash}))


def _stats_db(bad, planner):
    from test_mcs_stats import SCHEMA, SNAP_TS, _extract, _msg
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO snapshot_meta VALUES (1,'g',?)", (SNAP_TS,))
    _msg(conn, 1, chash="h1", ts=1789000000)
    _msg(conn, 2, chash="h2", ts=1789000000)
    _extract(conn, 1, "h1", [{"name": "合成薬A", "action": "start"}])
    conn.execute(
        "INSERT INTO artifacts(kind,message_id,content,meta) "
        "VALUES ('extract_v1',1,?,?)",
        (json.dumps({"med_periods": [
            {"start": "2026-09-01", "end": "2026-09-30", "raw": "9/1-30"}]}),
         json.dumps({"hash": "h1"})))
    if bad:
        for raw in MALFORMED:
            _bad_content_rows(conn, raw)
    if planner == "skewed":
        _skew(conn)
    return conn


@pytest.mark.parametrize("planner", PLANNERS)
def test_med_stats_ignore_malformed_content(planner):
    from test_mcs_stats import SNAP_TS
    clean, dirty = _stats_db(False, planner), _stats_db(True, planner)
    try:
        for name in MED_STATS:
            args = {"stat": name, "limit": 20}
            want = mcs_stats.run_stats(clean, SNAP_TS, args)["stats"][name]
            got = mcs_stats.run_stats(dirty, SNAP_TS, args)["stats"][name]
            assert want["status"] != "unavailable", (name, want)
            assert got == want, name
    finally:
        clean.close()
        dirty.close()


@pytest.mark.parametrize("planner", PLANNERS)
def test_rx_period_expiry_ignores_malformed_content(tmp_path, planner):
    from test_mcs_signals import NOW, _extract_v1, _msg
    lg = ledger_mod.Ledger(str(tmp_path / "ledger.db"))
    try:
        _msg(lg.db, 1)
        _msg(lg.db, 2, chash="h2")
        _extract_v1(lg.db, 1, "h1", [
            {"start": "2026-09-01", "end": "2026-09-30", "raw": "9/1-9/30"}])
        for raw in MALFORMED:
            _bad_content_rows(lg.db, raw)
        lg.db.commit()
        if planner == "skewed":
            _skew(lg.db)
        first = mcs_signals.evaluate(lg, {}, now=NOW)
        assert first["errors"] == []
        assert (first["open"], first["opened"]) == (1, 1)
        second = mcs_signals.evaluate(lg, {}, now=NOW)
        assert second["errors"] == []
        assert (second["open"], second["opened"]) == (1, 0)
        [sig] = mcs_signals.current_open(lg.db)["items"]
        assert sig["type"] == "rx_period_expiry"
        assert sig["context"]["days_left"] == 9
    finally:
        lg.db.close()


@pytest.mark.parametrize("raw", MALFORMED)
@pytest.mark.parametrize("column", ["meta", "content"])
def test_qc_readers_ignore_malformed_rows_under_skewed_stats(
        tmp_path, raw, column):
    from extract_testkit import _hash, _ledger
    from test_extract_llm_v2 import _seed_qc_flagged
    db = _ledger(tmp_path)
    try:
        _seed_qc_flagged(db)
        chash = _hash(db, 1)
        src = db.artifacts("extract_llm", message_id=1)[-1]["artifact_id"]
        want = extract_llm._qc_feedback(db, src)
        assert want is not None and "裏付け" in want["notes"][0]
        good_meta = json.dumps({
            "hash": chash, "extract_version": extract_llm.EXTRACT_VERSION,
            "source_artifact_id": src, "qc": "done"})
        for kind in ("extract_qc", "extract_llm"):
            for _ in range(3):
                if column == "meta":
                    _add_raw(db.db, kind, 1, "{}", raw)
                else:
                    _add_raw(db.db, kind, 1, raw, good_meta)
        db.db.commit()
        _skew(db.db)
        for _ in range(2):
            assert extract_llm._qc_feedback(db, src) == want
            assert extract_llm._current(db, 1, chash) is True
    finally:
        db.close()


@pytest.mark.parametrize("raw", MALFORMED)
def test_qc_view_ignores_malformed_content_under_skewed_stats(tmp_path, raw):
    from test_qc_view import _qc, _seeded, _view
    db = _seeded(tmp_path)
    try:
        _qc(db, 1, {"qc": "done", "items": [
            {"section": "meds", "index": 0, "item": {"name": "A"},
             "verdict": "NO_MATCH", "noul": 0.1}]})
        _qc(db, 2, {"qc": "unevaluated", "reason": "invalid_argument"})
        _skew(db.db)
        view = _view(db, tmp_path)
        try:
            want = view.read("qc", project=1)
        finally:
            view.close()
        for mid in (3, 4):
            _qc(db, mid, {"qc": "done", "items": []})
            db.db.execute("UPDATE artifacts SET content=? WHERE "
                          "artifact_id=last_insert_rowid()", (raw,))
        db.db.commit()
        _skew(db.db)
        view = _view(db, tmp_path)
        try:
            got = view.read("qc", project=1)
        finally:
            view.close()
        assert got["summary"] == want["summary"]
        assert got["items"] == want["items"]
    finally:
        db.close()
