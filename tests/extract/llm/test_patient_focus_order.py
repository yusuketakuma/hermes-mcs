"""Re-extraction order: patients nearest completion finish first.

Patient context waits for a patient's last pending post, so the few
patients with the fewest pending posts re-extract before the rest, after
new arrivals. Synthetic temp DB only."""
import json

import extract_llm
from extract_llm import EXTRACT_VERSION, KIND
from extract_testkit import _hash, _ledger, _message


def _old(db, mid, pid):
    db.artifact_add(KIND, json.dumps({"summary": "s"}), project_id=pid, message_id=mid,
                    meta={"hash": _hash(db, mid), "extract_version": EXTRACT_VERSION - 1})


def _corpus(tmp_path):
    db = _ledger(tmp_path)
    # patient 1: three pending re-extractions; patient 2: one, posted earliest
    db.save_messages([_message(mid=mid, body=f"体温は37度です。経過観察 {mid}",
                               posted_at=f"2026-09-0{mid}T00:00:00+09:00") for mid in (2, 3, 4)]
                     + [_message(mid=1, project_id=2, body="体温は36度です。経過観察 1",
                                 posted_at="2026-08-01T00:00:00+09:00")])
    for mid in (2, 3, 4):
        _old(db, mid, 1)
    _old(db, 1, 2)
    return db


def _order(db, limit=10):
    rows, fresh = extract_llm._select_pending(db, limit, None, None, False)
    return [r["message_id"] for r in rows], fresh


def test_the_patient_nearest_completion_reextracts_first_after_arrivals(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_FOCUS_PATIENTS", 1)
    db = _corpus(tmp_path)
    db.save_messages([_message(mid=5, body="体温は38度です。経過観察 5",
                               posted_at="2026-09-09T00:00:00+09:00")])
    # new arrival first; then patient 2 despite its older post; then newest first
    assert _order(db) == ([5, 1, 4, 3, 2], 1)
    assert _order(db, limit=2) == ([5, 1], 1)


def test_the_ranking_is_cached_and_rows_are_never_duplicated(tmp_path, monkeypatch):
    db = _corpus(tmp_path)
    key = extract_llm._left_estimate_key(db)
    first, _ = _order(db)
    ranked = extract_llm._FOCUS[key]
    second, _ = _order(db)
    assert first == second == [1, 4, 3, 2]        # both patients focused, each row once
    assert extract_llm._FOCUS[key] is ranked      # no grouped scan per selection
    monkeypatch.setattr(extract_llm, "_LEFT_ESTIMATE_S", 0)
    _order(db)
    assert extract_llm._FOCUS[key] is not ranked  # refreshed once the period passes


def test_oldest_first_keeps_its_chronological_walk(tmp_path):
    db = _corpus(tmp_path)
    rows, fresh = extract_llm._select_pending(db, 10, None, None, True)
    assert [r["message_id"] for r in rows] == [1, 2, 3, 4] and fresh is None


def test_arrivals_still_take_the_whole_limit_first(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_FOCUS_PATIENTS", 1)
    db = _corpus(tmp_path)
    db.save_messages([_message(mid=mid, body=f"体温は38度です。経過観察 {mid}",
                               posted_at="2026-09-09T00:00:00+09:00") for mid in (5, 6)])
    # never-extracted posts keep their place: focus only orders re-extraction
    assert _order(db, limit=2) == ([6, 5], 2)


def test_each_shard_keeps_the_focus_order(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_FOCUS_PATIENTS", 1)
    db = _corpus(tmp_path)
    rows, _ = extract_llm._select_pending(db, 10, None, (1, 2), False)   # odd ids
    assert [r["message_id"] for r in rows] == [1, 3]


def test_a_patient_archived_while_cached_moves_behind_active_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(extract_llm, "_FOCUS_PATIENTS", 1)
    db = _corpus(tmp_path)
    assert _order(db)[0][0] == 1                   # patient 2 is focused and cached
    db.db.execute("INSERT OR REPLACE INTO patients(project_id, patient_name, is_archived)"
                  " VALUES (2, 'synthetic-2', 1)")
    db.db.commit()
    assert _order(db)[0] == [4, 3, 2, 1]
