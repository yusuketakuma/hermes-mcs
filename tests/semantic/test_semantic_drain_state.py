"""Nested jobs and later passes own independent diagnostics and shadow accounting."""
import semantic_due_selection
import llm_availability
import json
import time

from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_util import payload_hash
import semantic_drain as drain
import semantic_runtime as runtime
from semantic_testkit import _FakeJev, _cfg, _llm, _seeded


def _run(db, max_jobs=2):
    return drain.run_due(db, _cfg("shadow"), {"errors": []}, time.monotonic() + 300,
                         jev_client=_FakeJev(), llm_fn=_llm, lane="backlog", max_jobs=max_jobs)


def test_nested_jobs_and_next_pass_cannot_replace_outer_reason_or_shadow_totals(tmp_path, monkeypatch):
    outer_path, inner_path = tmp_path / "outer", tmp_path / "inner"
    outer_path.mkdir()
    inner_path.mkdir()
    outer, inner = _seeded(outer_path), _seeded(inner_path)
    monkeypatch.setattr(llm_availability, 'disk_free_mb', lambda *args: 100_000.0)
    payload = json.loads(outer.db.execute(
        "SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()[0])
    outer.job_add("semantic", 1, 2, payload=payload)
    outer.db.commit()
    seen, nested = [], []
    phase = [1]

    def process(ledger, scfg, job, client, model, deadline, *, diagnostics, shadow_stats, **kwargs):
        assert diagnostics == {"code": None, "summary_validation_error": None,
                               "loops_rejected": None, "not_sent": None}
        if ledger is inner:
            assert diagnostics is not seen[0][0] and shadow_stats is not seen[0][1]
            drain._why(diagnostics, "inner_first")
            diagnostics.update(summary_validation_error="inner_summary", loops_rejected="inner_loop",
                               not_sent="llm_turn_busy")
            shadow_stats.update(calls=1, incomplete=1, s=7.0)
            return "deferred"
        if phase[0] == 2:
            assert shadow_stats == {"calls": 0, "incomplete": 0, "s": 0.0}
            shadow_stats.update(calls=1, incomplete=0, s=1.0)
            return "deferred"
        seen.append((diagnostics, shadow_stats))
        if len(seen) == 1:
            drain._why(diagnostics, "outer_first")
            diagnostics.update(summary_validation_error="outer_summary", loops_rejected="outer_loop")
            shadow_stats.update(calls=1, incomplete=0, s=2.0)
            nested.append(_run(inner, max_jobs=1))
            drain._why(diagnostics, "outer_second")
            assert diagnostics["code"] == "outer_first"
            assert diagnostics["summary_validation_error"] == "outer_summary"
            assert diagnostics["loops_rejected"] == "outer_loop" and diagnostics["not_sent"] is None
            assert shadow_stats == {"calls": 1, "incomplete": 0, "s": 2.0}
            return "retry"
        assert diagnostics is not seen[0][0] and shadow_stats is seen[0][1]
        shadow_stats["calls"] += 1
        shadow_stats["s"] += 3.0
        return "deferred"

    monkeypatch.setattr(drain, "_process_job", process)
    try:
        result = _run(outer)
        metrics = result["job_metrics"]
        assert [m["retry_reason"] for m in metrics] == ["outer_first", None]
        assert [m["summary_validation_error"] for m in metrics] == ["outer_summary", None]
        assert [m["loops_rejected"] for m in metrics] == ["outer_loop", None]
        assert result["shadow_v2"] == {"calls": 2, "incomplete": 0, "s": 5.0}
        assert nested[0]["shadow_v2"] == {"calls": 1, "incomplete": 1, "s": 7.0}
        assert nested[0]["job_metrics"][0]["retry_reason"] == "inner_first"
        before = dict(result["shadow_v2"])
        outer.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        outer.db.commit()
        phase[0] = 2
        again = _run(outer, max_jobs=1)
        assert again["shadow_v2"] == {"calls": 1, "incomplete": 0, "s": 1.0}
        assert again["job_metrics"][0]["retry_reason"] is None
        assert again["job_metrics"][0]["summary_validation_error"] is None
        assert again["job_metrics"][0]["loops_rejected"] is None
        assert result["shadow_v2"] == before
    finally:
        outer.close()
        inner.close()


def test_exception_first_cause_is_job_local_and_qc_gets_empty_diagnostics(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    monkeypatch.setattr(llm_availability, 'disk_free_mb', lambda *args: 100_000.0)
    db.job_add(drain.QC_JOB_KIND, 1, 1, payload={VERSION_KEY: CURRENT_VERSION, "generation": "synthetic-qc"})
    db.db.commit()
    semantic = db.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (drain.JOB_KIND,)).fetchone()
    qc = db.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (drain.QC_JOB_KIND,)).fetchone()
    # Exact served sequence exercises one normal exception then a QC call;
    # queue selection/fairness remain covered by their existing independent tests.
    monkeypatch.setattr(drain, "_select_due", lambda *args: (
        [semantic, qc], [qc], [semantic], [], {"turn": None}))
    monkeypatch.setattr(semantic_due_selection, '_select_due', drain._select_due)
    monkeypatch.setattr(drain.semantic_qc, "_qc_seed", lambda *args: None)

    def broken(*args, diagnostics, **kwargs):
        drain._why(diagnostics, "before_exception")
        diagnostics.update(summary_validation_error="summary_bad", loops_rejected="loop_bad")
        raise OSError("synthetic")

    def qc_done(ledger, scfg, job, *args, **kwargs):
        assert runtime.transition(ledger, runtime.JobToken.from_row(job), "done")
        return "done"

    monkeypatch.setattr(drain, "_process_job", broken)
    monkeypatch.setattr(drain.semantic_qc, "_process_qc_job", qc_done)
    try:
        config = _cfg("shadow", extract_qc="annotate")
        errors = {"errors": []}
        result = drain.run_due(db, config, errors, time.monotonic() + 300,
                              jev_client=_FakeJev(), llm_fn=_llm, max_jobs=2)
        first, second = result["job_metrics"]
        assert first["payload_digest"] == payload_hash(json.loads(semantic["payload"]))
        assert first["attempts"] == semantic["attempts"]
        assert first["status"] == "error" and first["retry_reason"] == "before_exception"
        assert first["summary_validation_error"] == "summary_bad" and first["loops_rejected"] == "loop_bad"
        assert second["kind"] == drain.QC_JOB_KIND and second["status"] == "done"
        assert second["retry_reason"] is second["summary_validation_error"] is second["loops_rejected"] is None
        assert result["failed"] == 1 and result["done"] == 1
        assert any("OSError" in error for error in errors["errors"])
    finally:
        db.close()
