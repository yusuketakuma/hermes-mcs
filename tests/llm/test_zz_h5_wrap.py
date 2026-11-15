"""H5 measurement only (not committed): result-preserving wrappers split one reservation's writer hold."""
import hashlib
import json
import random
import sqlite3
import sys
import time
import tracemalloc
from contextlib import closing

import pytest

import cloud_policy as c
from anthropic_client import MODEL
from ledger import Ledger
from test_cloud_policy import _body

CFG = {"cloud_llm": {"mode": "on", "project_ids": [1], "daily_cost_limit_usd": 100000,
                     "monthly_cost_limit_usd": 100000}}
PAIRS = 35000


def _reserve(ledger, unit):
    return c.usage_reserver(ledger, CFG, route="patient_context", project_id=1, unit_key=unit)(_body(unit), 2)


def _history(ledger):
    """About 1KB of metadata per row; created_at and insertion order differ from artifact_id order."""
    template = _reserve(ledger, "template")
    rng = random.Random(20261114)
    now = time.time()
    rows = []
    for index in range(PAIRS):
        unit = f"h{index}-" + "x" * 560          # widens the row to about 1KB of JSON
        reserve = {**template, "reservation_id": f"{index + 100000:032x}", "unit_key": unit}
        result = {key: reserve.get(key) for key in c._BINDING_KEYS if key not in ("effort", "profile") or key in reserve}
        result.update(usage=None, cost_usd=reserve["max_cost_usd"], http_class="transport", error_kind="transport")
        at = now - rng.uniform(600, 7200)
        rows.append(("cloud_usage_reserve", 1, MODEL, json.dumps(reserve), "{}", at))
        rows.append(("cloud_usage_result", 1, MODEL, json.dumps(result), "{}", at + 1))
    rng.shuffle(rows)
    ledger.db.executemany("INSERT INTO artifacts(kind,project_id,model,meta,content,created_at) VALUES(?,?,?,?,?,?)", rows)
    ledger.db.commit()
    digest = hashlib.sha256(json.dumps([row[:4] for row in rows]).encode()).hexdigest()[:16]
    widths = [len(row[3]) for row in rows]
    return digest, sum(widths) / len(widths)


def _wrap(monkeypatch, owner, name, events):
    original = getattr(owner, name)

    def timed(*args, **kwargs):
        events.append(("enter", name, time.perf_counter()))
        try:
            return original(*args, **kwargs)
        finally:
            events.append(("exit", name, time.perf_counter()))
    monkeypatch.setattr(owner, name, timed)


def _exclusive(events):
    """Exclusive seconds per call, in call order: a nested call's time is not counted twice."""
    stack, calls = [], []
    for kind, name, at in events:
        if kind == "enter":
            stack.append([name, at, 0.0])
        else:
            entered = stack.pop()
            total = at - entered[1]
            calls.append((entered[1], name, total - entered[2], total))
            if stack:
                stack[-1][2] += total
    return sorted(calls)


@pytest.mark.parametrize("memory", [False, True])
@pytest.mark.parametrize("label", ["cold", "warm"])
def test_h5_wrapped_phases(tmp_path, monkeypatch, label, memory):
    print(f"\nH5W python={sys.version.split()[0]} sqlite={sqlite3.sqlite_version}")
    with closing(Ledger(str(tmp_path / "synthetic.db"))) as ledger:
        digest, width = _history(ledger)
        if label == "warm":
            _reserve(ledger, "warmup")
        events, marks = [], []
        _wrap(monkeypatch, c, "_verified_request_history", events)
        _wrap(monkeypatch, c._Tally, "fingerprint", events)
        _wrap(monkeypatch, c._Tally, "update", events)
        ledger.db.set_trace_callback(lambda sql: marks.append((time.perf_counter(), sql[:20])))
        if memory:
            tracemalloc.start()
        started = time.perf_counter()
        try:
            _reserve(ledger, f"measure-{label}")
        finally:
            ended = time.perf_counter()
            peak = tracemalloc.get_traced_memory()[1] if memory else 0
            if memory:
                tracemalloc.stop()
            ledger.db.set_trace_callback(None)
        begin = next(at for at, sql in marks if sql.startswith("BEGIN IMMEDIATE"))
        commit = [at for at, sql in marks if sql.startswith("COMMIT")][-1]
        print(f"H5W {label} memory_tracing={memory} input={digest} rows={2 * PAIRS} meta_avg={width:.0f}B "
              f"reserve_total={ended - started:.3f}s begin_to_commit_start={commit - begin:.3f}s "
              f"begin_to_return={ended - begin:.3f}s peak_python_mem={peak / 1e6:.1f}MB")
        for at, name, exclusive, total in _exclusive(events):
            where = "in_tx" if begin <= at <= commit else "outside_tx"
            print(f"  +{at - begin:7.3f}s {where:<10} {name:<26} exclusive={exclusive:.3f}s inclusive={total:.3f}s")
