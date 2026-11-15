"""H5 measurement only (not committed): what one reservation holds the writer for on a large ledger."""
from collections import defaultdict
from contextlib import closing
import json
import re
import time

import pytest

import cloud_policy as c
from anthropic_client import MODEL
from ledger import Ledger
from test_cloud_policy import _body

CFG = {"cloud_llm": {"mode": "on", "project_ids": [1], "daily_cost_limit_usd": 100000,
                     "monthly_cost_limit_usd": 100000}}


def _reserve(ledger, unit):
    return c.usage_reserver(ledger, CFG, route="patient_context", project_id=1, unit_key=unit)(_body(unit), 2)


def _history(ledger, pairs):
    template = _reserve(ledger, "template")
    now = time.time() - 600
    rows = []
    for index in range(pairs):
        reserve = {**template, "reservation_id": f"{index + 100000:032x}", "unit_key": f"h{index}"}
        result = {key: reserve.get(key) for key in c._BINDING_KEYS if key not in ("effort", "profile") or key in reserve}
        result.update(usage=None, cost_usd=reserve["max_cost_usd"], http_class="transport", error_kind="transport")
        rows.append(("cloud_usage_reserve", 1, MODEL, json.dumps(reserve), "{}", now))
        rows.append(("cloud_usage_result", 1, MODEL, json.dumps(result), "{}", now))
    ledger.db.executemany("INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) VALUES(?,?,?,?,?,?)", rows)
    ledger.db.commit()


def _shape(sql):
    sql = re.sub(r"'[0-9a-f]{32}'", "'<rid>'", sql)
    sql = re.sub(r"\b\d+(\.\d+)?\b", "<n>", sql)
    return sql[:110]


def _measure(ledger, unit):
    marks = []
    ledger.db.set_trace_callback(lambda sql: marks.append((time.perf_counter(), sql)))
    started = time.perf_counter()
    try:
        _reserve(ledger, unit)
    finally:
        ended = time.perf_counter()
        ledger.db.set_trace_callback(None)
    spans = defaultdict(lambda: [0, 0.0])
    tx_start = tx_end = None
    for index, (at, sql) in enumerate(marks):
        following = marks[index + 1][0] if index + 1 < len(marks) else ended
        spans[_shape(sql)][0] += 1
        spans[_shape(sql)][1] += following - at
        if sql.startswith("BEGIN IMMEDIATE") and tx_start is None:
            tx_start = at
        if sql.startswith("COMMIT"):
            tx_end = at
    order = []
    for index, (at, sql) in enumerate(marks):
        following = marks[index + 1][0] if index + 1 < len(marks) else ended
        if sql.startswith(("BEGIN", "COMMIT")) or "FROM artifacts WHERE kind IN ('cloud_usage" in sql:
            tail = re.findall(r"(artifact_id[<>]=?\d+|artifact_id>\?|created_at>=[\d.]+)", sql)
            order.append(f"{following - at:7.3f}s {sql[:16]:<16} {' '.join(tail)}")
    print("  order:\n    " + "\n    ".join(order))
    return ended - started, (tx_end - tx_start) if tx_start and tx_end else None, spans


@pytest.mark.parametrize("pairs", [35000])
def test_h5_reservation_hold(tmp_path, pairs):
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        _history(ledger, pairs)
        for label in ("first", "second"):
            total, held, spans = _measure(ledger, f"measure-{label}")
            print(f"\nH5 pairs={pairs} rows={2 * pairs} {label}: total={total:.3f}s writer_tx={held if held is None else round(held, 3)}s")
            for shape, (count, seconds) in sorted(spans.items(), key=lambda item: -item[1][1])[:8]:
                print(f"  {seconds:8.3f}s x{count:<3} {shape}")
