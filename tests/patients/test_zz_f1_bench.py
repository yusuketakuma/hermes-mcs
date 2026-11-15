"""F1 measurement only (not committed): synthetic per-patient rollup rebuild cost."""
import cProfile
import io
import pstats
import time

import pytest

import extract
import rollup
from semantic_testkit import _ledger, _message, _patient

BODIES = ["明日からカロナール300mgを1日3回に変更します。確認お願いします。",
          "本日訪問しました。BT36.8 SpO2 97%。痛みはありません。",
          "次回10/5訪問予定。残薬を確認してください。",
          "家族より食欲低下の報告あり。様子を見ます。"]


def _corpus(db, pid, count, start):
    db.save_messages([_message(mid=start + i, pid=pid, parent=None if i % 4 == 0 else start + i - i % 4,
                               body=BODIES[i % 4]) for i in range(count)], project_id=pid)


@pytest.mark.parametrize("own,others", [(50, 0), (200, 0), (800, 0), (200, 4000)])
def test_f1_rebuild_cost(tmp_path, own, others):
    db = _ledger(tmp_path)
    try:
        _patient(db, 1)
        _corpus(db, 1, own, 1)
        for other in range(others // 200):
            _patient(db, 2 + other)
            _corpus(db, 2 + other, 200, 100000 + other * 1000)
        while extract.run_pending(db).get("done"):
            pass
        statements = []
        db.db.set_trace_callback(statements.append)
        profile = cProfile.Profile()
        started = time.perf_counter()
        profile.enable()
        rollup.rebuild(db, 1)
        profile.disable()
        elapsed = time.perf_counter() - started
        db.db.set_trace_callback(None)
        out = io.StringIO()
        pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats(18)
        print(f"\nF1 own={own} others={others} elapsed={elapsed:.3f}s statements={len(statements)}")
        print("\n".join(line for line in out.getvalue().splitlines() if "rollup" in line or "mcs/" in line)[:4000])
    finally:
        db.close()
