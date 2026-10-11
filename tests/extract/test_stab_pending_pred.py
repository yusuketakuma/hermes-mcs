"""M5/M12: the guarded pending predicate and the streamed two-pass
selection pick exactly the rows the former unguarded predicate and
grouped query picked; only the order changes (never-extracted first).

Synthetic fixtures + temp DB only."""
import json
import time

import extract_llm
from extract_llm import (EXTRACT_VERSION, KIND, _human_flagged_sql,
                         _qc_flagged_sql, _thin_pending_sql)
from extract_testkit import _hash, _ledger, _message
from mcs_queries import (ELIGIBLE_MESSAGE, V4_PROJECTION_KIND,
                         current_extract_pred, current_v4_id, json_or_null)


def _reference_pending_pred() -> str:
    """The composition before M5 (no guards, qc_fix tested first)."""
    return f"""{ELIGIBLE_MESSAGE}
        AND {current_v4_id()} IS NULL
        AND ((NOT EXISTS (SELECT 1 FROM artifacts f
                        WHERE f.kind='{KIND}' AND f.message_id=m.message_id
                          {current_extract_pred('f')}
                          AND json_extract({json_or_null('f.meta')},'$.extract_version')={EXTRACT_VERSION}
                          AND json_extract({json_or_null('f.meta')},'$.qc_fix') IS NOT NULL)
        AND (NOT EXISTS (SELECT 1 FROM artifacts a
                        WHERE a.kind='{KIND}' AND a.message_id=m.message_id
                          {current_extract_pred()}
                          AND json_extract({json_or_null('a.meta')},'$.extract_version')={EXTRACT_VERSION})
             OR EXISTS ({_qc_flagged_sql(val="1")})
             OR EXISTS ({_thin_pending_sql()})))
             OR EXISTS ({_human_flagged_sql(val="1")}))"""


def _reference_selection(db, now, limit=10_000):
    """The grouped selection query before M5 (no admission, no shard)."""
    return [r["message_id"] for r in db.db.execute(f"""
      SELECT m.message_id,
             MAX(COALESCE(json_extract(e.meta,'$.attempts'),0)) AS attempts
      FROM messages m
      LEFT JOIN artifacts e ON e.message_id=m.message_id AND e.kind=?
        AND CASE WHEN json_valid(e.meta)
                 THEN json_extract(e.meta,'$.error')=1
                  AND COALESCE(json_extract(e.meta,'$.extract_version'),0)=?
                  AND json_extract(e.meta,'$.hash')=m.content_hash
                 ELSE 0 END
      WHERE {ELIGIBLE_MESSAGE}
        AND NOT EXISTS (SELECT 1 FROM fetch_jobs claim
                        WHERE claim.kind='extract_claim'
                          AND claim.project_id=m.project_id
                          AND claim.message_id=m.message_id
                          AND claim.next_try > ?)
        AND NOT EXISTS (SELECT 1 FROM artifacts bad
                        WHERE bad.kind=? AND bad.message_id=m.message_id
                          AND NOT json_valid(bad.meta))
        AND {_reference_pending_pred()}
      GROUP BY m.message_id
      HAVING attempts < 5
         AND COALESCE(MAX(json_extract(e.meta,'$.next_try')),0) <= ?
      ORDER BY m.posted_at_ts DESC LIMIT ?""",
        (KIND, EXTRACT_VERSION, now, KIND, now, limit)).fetchall()]


def _new_selection(db, now, fresh, limit=10_000):
    return [r["message_id"] for r in db.db.execute(
        extract_llm._selection_sql("DESC", "", fresh),
        (KIND, EXTRACT_VERSION, None, 1, 0, now, KIND, KIND,
         EXTRACT_VERSION, now, limit)).fetchall()]


_LONG = "体温は37度です。" + "経過観察を継続します。" * 40   # >= 300 chars


def _add(db, mid, meta=None, content=None, kind=KIND, raw_meta=None):
    meta = {"hash": _hash(db, mid), "extract_version": EXTRACT_VERSION,
            **(meta or {})}
    content = {"summary": "s", "symptoms": [{"text": "a"}, {"text": "b"}]} \
        if content is None else content
    if raw_meta is not None:
        with db.db:
            return db.db.execute(
                "INSERT INTO artifacts(kind,project_id,message_id,content,"
                "model,meta,created_at) VALUES(?,?,?,?,?,?,?)",
                (kind, 1, mid, json.dumps(content), "m", raw_meta,
                 time.time())).lastrowid
    return db.artifact_add(kind, json.dumps(content, ensure_ascii=False),
                           project_id=1, message_id=mid, meta=meta)


def _corpus(tmp_path):
    db = _ledger(tmp_path)
    now = time.time()
    bodies = {mid: ("短い本文です" if mid % 2 else _LONG) + f" {mid}"
              for mid in range(1, 41)}
    db.save_messages([
        _message(mid=mid, body=body,
                 posted_at=f"2026-09-{1 + mid % 28:02d}T00:00:00+09:00",
                 state="partial" if mid == 40 else "full")
        for mid, body in bodies.items()])
    # 1-4 never extracted (fresh)
    _add(db, 5)                                              # settled
    _add(db, 6, {"extract_version": EXTRACT_VERSION - 1})    # older version
    _add(db, 7, {"error": True, "attempts": 1, "next_try": now - 5},
         content={"_error": True})                           # retry due
    _add(db, 8, {"error": True, "attempts": 2, "next_try": now + 900},
         content={"_error": True})                           # backing off
    _add(db, 9, {"error": True, "attempts": 5, "next_try": now - 5},
         content={"_error": True})                           # exhausted
    _add(db, 10, content={"summary": "s"})                   # thin, long body
    _add(db, 11, content={"summary": "s"})                   # short body: not thin
    _add(db, 12, {"thin_retried": True}, content={"summary": "s"})
    _add(db, 14, {"prefilter": "no_signal"}, content={"summary": "s"})
    src = _add(db, 15)                                       # QC flagged
    _add(db, 15, kind="extract_qc", content={
        "qc": "done", "items": [{"section": "symptoms", "verdict": "NO_MATCH",
                                 "item": {"text": "a"}}]},
         meta={"source_artifact_id": src, "qc": "done"})
    src = _add(db, 16)                                       # QC clean
    _add(db, 16, kind="extract_qc", content={"qc": "done", "items": []},
         meta={"source_artifact_id": src, "qc": "done"})
    src = _add(db, 17, {"qc_fix": {"qc": 1, "applied": True}})
    _add(db, 17, kind="extract_qc", content={
        "qc": "done", "items": [{"section": "symptoms", "verdict": "NO_MATCH",
                                 "item": {"text": "a"}}]},
         meta={"source_artifact_id": src, "qc": "done"})
    cur = _add(db, 18)                                       # human report
    _add(db, 18, kind="extract_feedback_v1", content={"artifact_id": cur})
    _add(db, 19, raw_meta="{not json")                       # bad meta
    _add(db, 20, kind=V4_PROJECTION_KIND, content={"facts": []}, meta={
        "engine_version": 4, "projection_version": 5})       # current v4
    _add(db, 21, kind=V4_PROJECTION_KIND, content={"facts": []}, meta={
        "engine_version": 4, "projection_version": 5, "invalidated": True})
    _add(db, 22, {"hash": "stale-body"})                     # other body
    with db.db:                                              # live claim
        db.db.execute(
            "INSERT INTO fetch_jobs(kind,project_id,message_id,state,"
            "next_try,created_at,updated_at) VALUES('extract_claim',1,23,"
            "'pending',?,?,?)", (now + 600, now, now))
        db.db.execute("UPDATE messages SET body_state='deleted' WHERE message_id=24")
    return db, now


def test_guarded_pending_pred_matches_reference(tmp_path):
    db, _now = _corpus(tmp_path)
    try:
        new = {r[0] for r in db.db.execute(
            f"SELECT m.message_id FROM messages m WHERE {extract_llm.pending_pred()}")}
        ref = {r[0] for r in db.db.execute(
            f"SELECT m.message_id FROM messages m WHERE {_reference_pending_pred()}")}
        assert new == ref
        # the fixture exercises each branch, not an empty corpus
        assert {1, 6, 7, 10, 15, 18, 21}.issubset(ref)
        assert not {5, 11, 12, 14, 16, 17, 20, 40} & ref
    finally:
        db.close()


def test_two_pass_selection_equals_grouped_query(tmp_path):
    db, now = _corpus(tmp_path)
    try:
        ref = _reference_selection(db, now)
        fresh = _new_selection(db, now, True)
        rest = _new_selection(db, now, False)
        assert not set(fresh) & set(rest)
        assert sorted(fresh + rest) == sorted(ref)
        assert sorted(_new_selection(db, now, None)) == sorted(ref)
        assert set(fresh) == {m for m in range(1, 41) if m in ref
                              and not db.db.execute(
                                  "SELECT 1 FROM artifacts WHERE kind=? AND message_id=?",
                                  (KIND, m)).fetchone()}
        assert 9 not in ref and 8 not in ref and 23 not in ref and 19 not in ref
    finally:
        db.close()


def test_run_pending_serves_never_extracted_before_reextraction(tmp_path, monkeypatch):
    """M12: a version bump re-pends every older row; a new arrival with an
    older posted_at is still taken first."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([
            _message(mid=1, body="体温37度", posted_at="2026-09-01T00:00:00+09:00"),
            _message(mid=2, body="体温38度", posted_at="2026-09-20T00:00:00+09:00"),
            _message(mid=3, body="体温39度", posted_at="2026-09-21T00:00:00+09:00")])
        for mid in (2, 3):
            _add(db, mid, {"extract_version": EXTRACT_VERSION - 1})
        seen = []
        monkeypatch.setattr(extract_llm, "llm_extract",
                            lambda body, **_: seen.append(body) or {"summary": "ok"})
        res = extract_llm.run_pending(db, limit=1, budget_s=30)
        assert res["done"] == 1 and res["fresh_selected"] == 1
        assert seen == ["体温37度"]
        res = extract_llm.run_pending(db, limit=1, budget_s=30)
        assert seen[-1] == "体温39度" and res["fresh_selected"] == 0
    finally:
        db.close()


def test_full_selection_reports_left_estimate(tmp_path, monkeypatch):
    """M12: a full selection keeps left=None (no per-job O(backlog) scan)
    but carries a periodically refreshed estimate."""
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=m, body=f"体温3{m}度") for m in range(1, 6)])
        monkeypatch.setattr(extract_llm, "llm_extract",
                            lambda body, **_: {"summary": "ok"})
        extract_llm._LEFT_ESTIMATE.clear()
        res = extract_llm.run_pending(db, limit=1, budget_s=30)
        # counted after this call settled one row
        assert res["left"] is None and res["left_estimate"] == 4
        res = extract_llm.run_pending(db, limit=1, budget_s=30)
        # cached count minus what this process settled since
        assert res["left"] is None and res["left_estimate"] == 3
        assert res["left_estimate_age_s"] >= 0
        res = extract_llm.run_pending(db, limit=20, budget_s=30)
        assert res["left"] == 0 and res["left_estimate"] == 0
    finally:
        db.close()
