#!/usr/bin/env python3
"""Benchmark synthetic pending-notification lookup before and after its index."""
from __future__ import annotations

from collections import Counter
import json
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mcs"))
import _mcs_path  # noqa: E402,F401
from ledger import Ledger  # noqa: E402

HISTORICAL_EVENTS = 25_000
UNRELATED_ACTIVE_EVENTS = 5_000
ELIGIBLE_EVENTS = 50
WARMUPS = 30
SAMPLES = 501
INDEX_NAME = "idx_outbox_pending_new_message_due"
INDEX_SQL = f"""CREATE INDEX {INDEX_NAME}
  ON notify_outbox(kind,next_try)
  WHERE kind='new_messages'
    AND state IN ('pending','failed')
    AND next_try IS NOT NULL"""
LOOKUP_SQL = ("SELECT payload FROM notify_outbox WHERE kind='new_messages' "
              "AND state IN ('pending','failed') AND next_try IS NOT NULL")


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    return ordered[math.ceil(percentile * len(ordered)) - 1]


def _plan(db) -> str:
    return " | ".join(row[3] for row in db.execute(
        "EXPLAIN QUERY PLAN " + LOOKUP_SQL))


def _vm_callbacks(ledger: Ledger) -> int:
    callbacks = [0]
    ledger.db.set_progress_handler(
        lambda: callbacks.__setitem__(0, callbacks[0] + 1) or 0, 100)
    try:
        ledger.pending_notify_message_ids()
    finally:
        ledger.db.set_progress_handler(None, 0)
    return callbacks[0]


def _measure(ledger: Ledger) -> tuple[Counter, tuple[float, float, float]]:
    expected = Counter(ledger.pending_notify_message_ids())
    for _ in range(WARMUPS):
        if Counter(ledger.pending_notify_message_ids()) != expected:
            raise RuntimeError("warmup_result_changed")
    samples = []
    for _ in range(SAMPLES):
        started = time.perf_counter_ns()
        message_ids = ledger.pending_notify_message_ids()
        elapsed_ns = time.perf_counter_ns() - started
        samples.append(elapsed_ns / 1_000_000)
        if Counter(message_ids) != expected:
            raise RuntimeError("measured_result_changed")
    return expected, tuple(_percentile(samples, p) for p in (.50, .95, .99))


def _index_bytes(db) -> int | None:
    try:
        row = db.execute(
            "SELECT COALESCE(SUM(pgsize),0) FROM dbstat WHERE name=?",
            (INDEX_NAME,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row[0])


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mcs-notify-index-bench-") as tmp:
        ledger = Ledger(os.path.join(tmp, "synthetic.db"))
        db = ledger.db
        future = time.time() + 86400
        payload = json.dumps({"message_ids": [1]}, separators=(",", ":"))
        try:
            with db:
                db.executemany(
                    "INSERT INTO notify_outbox(kind,project_id,payload,state,"
                    "next_try,created_at,updated_at) VALUES(?,?,?,'accepted',"
                    "NULL,?,?)",
                    (("new_messages", i, payload, i, i)
                     for i in range(1, HISTORICAL_EVENTS + 1)))
                db.executemany(
                    "INSERT INTO notify_outbox(kind,project_id,payload,state,"
                    "next_try,created_at,updated_at) VALUES(?,?,?, ?,?,?,?)",
                    (("attachment_followup", i, payload,
                      "pending" if i % 2 else "failed", future, i, i)
                     for i in range(1, UNRELATED_ACTIVE_EVENTS + 1)))
                db.executemany(
                    "INSERT INTO notify_outbox(kind,project_id,payload,state,"
                    "next_try,created_at,updated_at) VALUES(?,?,?, ?,?,?,?)",
                    (("new_messages", HISTORICAL_EVENTS + i,
                      json.dumps({"message_ids": [HISTORICAL_EVENTS + i]},
                                 separators=(",", ":")),
                      "pending" if i % 2 else "failed", future, i, i)
                     for i in range(1, ELIGIBLE_EVENTS + 1)))

            db.execute(f"DROP INDEX {INDEX_NAME}")
            db.commit()
            before_plan = _plan(db)
            before_callbacks = _vm_callbacks(ledger)
            before_result, before_times = _measure(ledger)

            started = time.perf_counter_ns()
            db.execute(INDEX_SQL)
            db.commit()
            index_build_ms = (time.perf_counter_ns() - started) / 1_000_000
            index_size = _index_bytes(db)
            after_plan = _plan(db)
            after_callbacks = _vm_callbacks(ledger)
            after_result, after_times = _measure(ledger)
            if before_result != after_result:
                raise RuntimeError("before_after_multiset_mismatch")

            print(f"Python {sys.version.split()[0]}")
            print(f"SQLite {sqlite3.sqlite_version}")
            print(f"synthetic rows: {HISTORICAL_EVENTS} settled new_messages, "
                  f"{UNRELATED_ACTIVE_EVENTS} active unrelated, "
                  f"{ELIGIBLE_EVENTS} eligible new_messages")
            print(f"warmups={WARMUPS} samples={SAMPLES}; percentiles=nearest-rank")
            print(f"before plan: {before_plan}")
            print(f"after plan: {after_plan}")
            print(f"multiset result equivalent: {before_result == after_result}; "
                  f"message IDs={sum(after_result.values())}")
            print("warm-call latency ms: "
                  f"before p50={before_times[0]:.3f} p95={before_times[1]:.3f} "
                  f"p99={before_times[2]:.3f}; "
                  f"after p50={after_times[0]:.3f} p95={after_times[1]:.3f} "
                  f"p99={after_times[2]:.3f}")
            print("SQLite VM progress callbacks per 100 opcodes: "
                  f"before={before_callbacks} after={after_callbacks}")
            size_text = (f"{index_size} bytes" if index_size is not None
                         else "unavailable (SQLite dbstat not compiled in)")
            print(f"index build={index_build_ms:.3f} ms; index size={size_text}")
            print("write/update cost: qualitative; one index entry per matching "
                  "active row, maintained when its kind/state/next_try changes; "
                  "payload is not copied")
        finally:
            ledger.close()


if __name__ == "__main__":
    main()
