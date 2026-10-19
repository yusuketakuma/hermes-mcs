"""Synthetic extraction regressions for bounded context and linear medication merging."""
import copy
import sqlite3

import pytest

import extract_llm


@pytest.mark.parametrize("target_is_root", [False, True])
def test_thread_context_fetches_only_existing_selection(target_is_root):
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE messages(message_id INTEGER,project_id INTEGER,"
               "parent_id INTEGER,posted_at_ts REAL,body_text TEXT,"
               "profession TEXT,sender_type TEXT,body_state TEXT)")
    data = [(i, 1, None if i == 1 else 1, i, f"SYNTH-{i}", "", "user", "full")
            for i in range(1, 301)]
    # Root priority, timestamp ties/unknowns, discarded bodies, other scope.
    data[0] = (1, 1, None, None, "SYNTH-root\0still-body", "", "user", "full")
    data.extend([
        (302, 1, 1, 300, "SYNTH-tie", "", "user", "full"),
        (303, 1, 1, None, "SYNTH-unknown", "", "user", "full"),
        (304, 1, 1, 399, "SYNTH-thin", "", "user", "thin"),
        (305, 2, 1, 399, "SYNTH-other", "", "user", "full"),
        (306, 1, 1, 401, "SYNTH-future", "", "user", "full")])
    db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", data)
    fetched = []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            rows = self.cursor.fetchall()
            fetched.extend(rows)
            return rows

    class DB:
        def execute(self, sql, args):
            return Cursor(db.execute(sql, args))

    class Ledger:
        def karte_summary_current(self, project_id):
            return None

    ledger = Ledger()
    ledger.db = DB()
    target = {"project_id": 1, "message_id": 1 if target_is_root else 400,
              "parent_id": None if target_is_root else 1,
              "posted_at_ts": 400, "posted_at": "2026-01-01T00:00:00+09:00"}
    candidates = db.execute(
        "SELECT message_id,body_text,posted_at_ts,sender_type AS who FROM messages "
        "WHERE project_id=1 AND (message_id=1 OR (parent_id=1 AND posted_at_ts<400)) "
        "AND body_state='full' AND message_id!=?", (target["message_id"],)).fetchall()
    expected = "\n".join(extract_llm._ctx_lines(candidates, 1))
    try:
        assert extract_llm._thread_context(ledger, target) == expected
        assert len(fetched) <= 4
    finally:
        db.close()


def test_medication_merge_preserves_last_occurrence_with_linear_work():
    reads = []

    class Medication(dict):
        def __getitem__(self, key):
            if key == "name":
                reads.append(key)
            return super().__getitem__(key)

    meds = [Medication(name=f"SYNTH-{i}", subject="patient", action="start", dose=str(i))
            for i in range(120)]
    meds.extend([Medication(name="SYNTH-0", subject="patient", action="stop"),
                 Medication(name="SYNTH-0", subject="patient", action="start", dose="new"),
                 Medication(name="SYNTH-1", subject="family", action="start")])
    original = copy.deepcopy(meds)
    expected = []
    for med in original:
        key = (med["name"], med.get("subject"), med.get("action"))
        expected = [old for old in expected if (
            old["name"], old.get("subject"), old.get("action")) != key]
        expected.append(med)
    reads.clear()
    result = extract_llm._merge([{"meds": meds[:80]}, {"meds": meds[80:]}])
    assert result["meds"] == expected
    assert meds == original
    assert len(reads) == len(meds)


def test_parallel_drain_releases_handled_future_results(tmp_path, monkeypatch):
    from concurrent import futures
    import weakref
    from extract_testkit import _ledger, _message

    ledger = _ledger(tmp_path)
    ledger.save_messages([_message(mid=i, body=f"SYNTH-{i}") for i in range(1, 9)])
    references, retained = [], []

    class Pool:
        def __init__(self, max_workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, fn, *args):
            future = futures.Future()
            future.sequence = len(references)
            future.handled = False
            future.set_result(fn(*args))
            references.append(weakref.ref(future))
            return future

    def wait(pending, **kwargs):
        retained.append(sum(ref() is not None and ref().handled for ref in references))
        future = min(pending, key=lambda item: item.sequence)
        future.handled = True
        return {future}, pending - {future}

    monkeypatch.setattr(futures, "ThreadPoolExecutor", Pool)
    monkeypatch.setattr(futures, "wait", wait)
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: None)
    monkeypatch.setattr(extract_llm, "llm_extract", lambda *args, **kwargs: {"summary": "SYNTH"})
    try:
        result = extract_llm.run_pending(ledger, limit=8, workers=2, budget_s=60,
                                         admitted_ids=set(range(1, 9)))
        assert result["done"] == 8
        assert max(retained) <= 1  # the current loop iteration may hold its last future
        assert all(ref() is None for ref in references)
    finally:
        ledger.close()
