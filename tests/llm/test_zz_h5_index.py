"""H5 candidate C evaluation only (not committed): an artifact_id-ordered partial index for accounting rows."""
import sqlite3
import sys
import time
import tracemalloc
from contextlib import closing

import pytest

import cloud_policy as c
from ledger import Ledger
from test_zz_h5_wrap import _exclusive, _history, _reserve, _wrap

KINDS_SQL = "kind IN ('cloud_usage_reserve','cloud_usage_result')"
VARIANTS = {
    "none": None,
    "C1": f"CREATE INDEX h5_candidate ON artifacts(artifact_id) WHERE {KINDS_SQL}",
    "C2": f"CREATE INDEX h5_candidate ON artifacts(artifact_id, created_at) WHERE {KINDS_SQL}",
}
QUERIES = {
    "fingerprint": f"SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
                   f"WHERE {KINDS_SQL} AND created_at>=? AND artifact_id<=? ORDER BY artifact_id",
    "update": f"SELECT artifact_id,kind,project_id,message_id,model,meta,created_at FROM artifacts "
              f"WHERE {KINDS_SQL} AND created_at>=? AND artifact_id>? ORDER BY artifact_id",
}


def _size(db):
    return db.execute("PRAGMA page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0]


def _answer(ledger, now):
    """A fresh tally from a full read (no cached tally): what the budget decision sees."""
    ledger._cloud_usage_tally = None
    ledger._cloud_window_tally = None
    tally = c._tally(ledger, now)
    return (str(tally.total), tally.last_id, bool(tally.invalid), sorted(tally.orphans), tally.prefix)


def _older_months(ledger, count):
    """Accounting rows from earlier months: outside the tally period, still inside the partial index."""
    import json
    rows = ledger.db.execute("SELECT kind,project_id,model,meta,content FROM artifacts "
                             f"WHERE {KINDS_SQL} ORDER BY artifact_id LIMIT ?", (count,)).fetchall()
    old = time.time() - 70 * 86400
    copies = []
    for index, (kind, project, model, meta, content) in enumerate(rows):
        meta = json.loads(meta)
        meta["reservation_id"] = f"{int(meta['reservation_id'], 16) + 10**9:032x}"   # unique per older copy
        copies.append((kind, project, model, json.dumps(meta), content, old + index))
    ledger.db.executemany("INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) VALUES(?,?,?,?,?,?)",
                          copies)
    ledger.db.commit()


@pytest.mark.parametrize("older", [0, 70000])
@pytest.mark.parametrize("variant", list(VARIANTS))
def test_h5_candidate_index(tmp_path, monkeypatch, variant, older):
    print(f"\nH5I python={sys.version.split()[0]} sqlite={sqlite3.sqlite_version} variant={variant} ddl={VARIANTS[variant]}")
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        digest, width = _history(ledger)
        if older:
            _older_months(ledger, older)
        print(f"H5I older_rows={older}")
        now = time.time()
        baseline = _answer(ledger, now)          # independent full read before any index
        before = _size(ledger.db)
        if VARIANTS[variant]:
            started = time.perf_counter()
            ledger.db.execute(VARIANTS[variant])
            ledger.db.commit()
            print(f"H5I build={time.perf_counter() - started:.3f}s added_bytes={_size(ledger.db) - before}")
        ledger.db.execute("ANALYZE artifacts")
        ledger.db.commit()
        month = c._periods(now)[1]
        last = ledger.db.execute("SELECT MAX(artifact_id) FROM artifacts").fetchone()[0]
        for name, sql in QUERIES.items():
            params = (month, last if name == "fingerprint" else 0)
            plan = " | ".join(row[3] for row in ledger.db.execute("EXPLAIN QUERY PLAN " + sql, params))
            started = time.perf_counter()
            count = sum(1 for _ in ledger.db.execute(sql, params))
            print(f"H5I {name}: rows={count} read={time.perf_counter() - started:.3f}s temp_btree={'TEMP B-TREE' in plan} plan={plan}")
            if VARIANTS[variant]:
                forced = sql.replace("FROM artifacts ", "FROM artifacts INDEXED BY h5_candidate ", 1)
                plan = " | ".join(row[3] for row in ledger.db.execute("EXPLAIN QUERY PLAN " + forced, params))
                started = time.perf_counter()
                assert [tuple(r) for r in ledger.db.execute(forced, params)] == [tuple(r) for r in ledger.db.execute(sql, params)]
                print(f"H5I {name} forced: read+compare={time.perf_counter() - started:.3f}s temp_btree={'TEMP B-TREE' in plan} plan={plan}")
        assert _answer(ledger, now) == baseline   # same tally, last id, invalid flag, orphans and fingerprint
        _reserve(ledger, "warmup")
        events, marks = [], []
        _wrap(monkeypatch, c, "_verified_request_history", events)
        _wrap(monkeypatch, c._Tally, "fingerprint", events)
        _wrap(monkeypatch, c._Tally, "update", events)
        ledger.db.set_trace_callback(lambda sql: marks.append((time.perf_counter(), sql[:20])))
        started = time.perf_counter()
        _reserve(ledger, "measure")
        ended = time.perf_counter()
        ledger.db.set_trace_callback(None)
        begin = next(at for at, sql in marks if sql.startswith("BEGIN IMMEDIATE"))
        phases = ", ".join(f"{name}={exclusive:.3f}s" for _, name, exclusive, _ in _exclusive(events))
        print(f"H5I warm_reserve input={digest} meta_avg={width:.0f}B begin_to_return={ended - begin:.3f}s {phases}")
        tracemalloc.start()
        _reserve(ledger, "memory")
        print(f"H5I peak_python_mem={tracemalloc.get_traced_memory()[1] / 1e6:.1f}MB")
        tracemalloc.stop()
