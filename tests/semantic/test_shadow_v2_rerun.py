"""A shadow v2 comparison runs once per source + policy, not once per retry."""
import semantic_transport
import json
import time

import pytest

import semantic
import semantic_drain
import semantic_extraction
from semantic_testkit import NO_FACTS, _FakeJev, _cfg, _llm, _llm_v2, _seeded, _seeded_two


def _incomplete_v2(prompt):
    return "not a document" if "原子事実抽出器" in prompt else _llm(prompt)


def _passes(db, llm, configs=({}, {}), budget=300):
    """Run the job once per config (the summary fails, so it stays pending)
    and return the number of v2 extraction prompts sent after each pass."""
    calls = []

    def counted(prompt):
        if "原子事実抽出器" in prompt:
            calls.append(1)
        if "要約器" in prompt:
            return "not a summary"
        return llm(prompt)

    per_pass = []
    for extra in configs:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        semantic_drain.run_due(db, _cfg("shadow", fact_source="shadow", **extra),
                         {"errors": []}, time.monotonic() + budget,
                         jev_client=_FakeJev(), llm_fn=counted)
        per_pass.append(len(calls))
    return per_pass


def _receipts(db):
    return db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                         (semantic_drain.KIND_SHADOW_V2,)).fetchall()


def test_an_incomplete_comparison_is_not_rerun_on_retry(tmp_path):
    """2026-10-10: an incomplete shadow v2 document is never stored, so
    every retry re-extracted each finished target until a six-target job
    could no longer reach the next one and burned its attempts. The first
    attempt leaves an "incomplete" receipt; the retry skips the comparison."""
    db = _seeded(tmp_path)
    try:
        first, second = _passes(db, _incomplete_v2)
        assert first > 0 and second == first
        receipts = _receipts(db)     # one per target of the job
        assert len(receipts) == first
        assert all('"status": "incomplete"' in r[0] for r in receipts)
    finally:
        db.close()


def test_a_comparison_never_attempted_still_runs(tmp_path):
    """Stored legacy facts alone do not skip the comparison: without a
    receipt for this source + policy (facts stored before shadow v2 ran)
    the comparison runs again."""
    db = _seeded(tmp_path)
    try:
        targets = _passes(db, _incomplete_v2, configs=({},))[0]
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE kind=?",
                          (semantic_drain.KIND_SHADOW_V2,))
        assert _passes(db, _incomplete_v2, configs=({},)) == [targets]
        assert len(_receipts(db)) == targets
    finally:
        db.close()


def test_a_policy_change_runs_the_comparison_again(tmp_path):
    db = _seeded(tmp_path)
    try:
        first, second = _passes(db, _incomplete_v2,
                                configs=({}, {"match_threshold": 0.8}))
        assert first > 0 and second == 2 * first
        assert len(_receipts(db)) == 2 * first
    finally:
        db.close()


def test_a_complete_comparison_is_reused_without_a_receipt(tmp_path):
    db = _seeded(tmp_path)
    try:
        first, second = _passes(db, _llm_v2)
        assert first > 0 and second == first
        assert _receipts(db) == []
    finally:
        db.close()


@pytest.mark.parametrize("reason,reruns", [("model", False), ("deadline", True)])
def test_only_a_time_out_resumes_the_comparison(tmp_path, monkeypatch, reason, reruns):
    """A deterministic failure records the receipt; a time-out does not, so
    the next pass resumes from the persisted chunks."""
    calls = []

    def v2(*a, **k):
        calls.append(1)
        return {"extraction_complete": False, "failure_reason": reason,
                "chunks_completed": 0, "chunks_total": 1}

    monkeypatch.setattr(semantic_extraction, "extract_facts_v2", v2)
    db = _seeded(tmp_path)
    try:
        _passes(db, _llm, configs=({},))
        targets = len(calls)
        _passes(db, _llm, configs=({},))
        assert targets > 0 and len(calls) == (2 if reruns else 1) * targets
    finally:
        db.close()


def test_a_new_v2_extraction_generation_runs_the_comparison_again(tmp_path, monkeypatch):
    """A receipt speaks only for the model + prompt + chunk plan that ran;
    an updated extractor compares again."""
    db = _seeded(tmp_path)
    try:
        first = _passes(db, _incomplete_v2, configs=({},))[0]
        monkeypatch.setattr(semantic_extraction, "v2_generation", lambda: "next")
        assert _passes(db, _incomplete_v2, configs=({},)) == [first]
    finally:
        db.close()


def test_a_budget_stop_keeps_finished_targets_and_moves_on(tmp_path, monkeypatch):
    """Review condition: target A's comparison stays incomplete and its
    legacy facts are stored; the 300 s reserve then stops the pass before
    target B's first comparison call — B gets no (false) receipt. The next
    pass does not re-run A and reaches B."""
    clock = [1000.0]
    monkeypatch.setattr(semantic.time, "monotonic", lambda: clock[0])
    sent = []

    def model(prompt):
        if "原子事実抽出器" in prompt or "事実候補抽出器" in prompt:
            clock[0] += 320                   # one long generation each
        if "原子事実抽出器" in prompt:
            sent.append(prompt)
        return _incomplete_v2(prompt)

    db = _seeded(tmp_path)
    try:
        first = _passes(db, model, configs=({"job_budget_seconds": 900},),
                        budget=930)[0]
        assert first == 1 and len(_receipts(db)) == 1
        clock[0] += 1000
        _passes(db, model, configs=({"job_budget_seconds": 900},), budget=930)
        # one more comparison (B's) — A was not re-run — and one receipt each
        assert len(sent) == 2
        targets = db.db.execute("SELECT COUNT(DISTINCT message_id) FROM artifacts "
                                "WHERE kind=?", (semantic_drain.KIND_SHADOW_V2,)).fetchone()[0]
        assert len(_receipts(db)) == 2 and targets == 2
    finally:
        db.close()


def test_canonical_never_writes_a_shadow_receipt(tmp_path):
    db = _seeded(tmp_path)
    try:
        for _ in range(2):
            with db.db:
                db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            semantic_drain.run_due(db, _cfg("shadow", fact_source="canonical"),
                             {"errors": []}, time.monotonic() + 300,
                             jev_client=_FakeJev(), llm_fn=_incomplete_v2)
        assert _receipts(db) == []
    finally:
        db.close()


@pytest.mark.parametrize("content", ["{}", '{"status": "unknown"}'])
def test_a_malformed_receipt_does_not_skip_the_comparison(tmp_path, content):
    """Review condition: only a well-formed "incomplete" receipt skips the
    comparison; a newer malformed row (kept as history) makes it run again."""
    db = _seeded(tmp_path)
    try:
        targets = _passes(db, _incomplete_v2, configs=({},))[0]
        with db.db:
            db.db.execute(
                "INSERT INTO artifacts(kind, project_id, message_id, content, model, "
                "meta, created_at) SELECT kind, project_id, message_id, ?, model, "
                "meta, created_at FROM artifacts WHERE kind=?",
                (content, semantic_drain.KIND_SHADOW_V2))
        assert _passes(db, _incomplete_v2, configs=({},)) == [targets]
        assert len(_receipts(db)) == 3 * targets      # earlier rows are kept
    finally:
        db.close()


def _length_stop(prompt):
    if "原子事実抽出器" in prompt:
        return semantic_transport._failed("length_stop")
    return _llm(prompt)


def _one_invalid_fact(prompt):
    if "原子事実抽出器" in prompt:
        return json.dumps({"facts": [{"kind": "not_a_kind"}],
                           "category_presence": dict(NO_FACTS)})
    return _llm(prompt)


def _two_invalid_facts(prompt):
    if "原子事実抽出器" in prompt:
        return json.dumps({"facts": [{"kind": "not_a_kind", "statement": "合成"},
                                     {"kind": "medication_event", "statement": ""}],
                           "category_presence": dict(NO_FACTS)})
    return _llm(prompt)


@pytest.mark.parametrize("llm,failure,dropped,reasons", [
    (_incomplete_v2, None, 0, {}),          # unparsable document
    (_length_stop, "length_stop", 0, {}),   # the LLM call itself failed
    (_one_invalid_fact, None, 1, {"statement_invalid": 1}),
    (_two_invalid_facts, None, 2, {"kind_invalid": 1, "statement_invalid": 1}),
])
def test_the_receipt_says_which_model_stop_it_was(tmp_path, monkeypatch, llm, failure,
                                                  dropped, reasons):
    monkeypatch.setattr(semantic_transport, '_LLM_FAILURE', None)   # llm_chat resets it per call
    db = _seeded(tmp_path)
    try:
        _passes(db, llm, configs=({},))
        receipts = [json.loads(r[0]) for r in _receipts(db)]
        assert receipts and all(r["failure_reason"] == "model"
                                and r["llm_failure"] == failure
                                and r["dropped_in_failed_chunk"] == dropped
                                and r["dropped_reasons"] == reasons
                                for r in receipts)
    finally:
        db.close()


def _with_context(value):
    """The synthetic v2 documents with every fact carrying patient_context=value."""
    def llm(prompt):
        if "原子事実抽出器" not in prompt:
            return _llm(prompt)
        doc = json.loads(_llm_v2(prompt))
        for fact in doc["facts"]:
            fact["patient_context"] = value
        return json.dumps(doc, ensure_ascii=False)
    return llm


def test_a_null_patient_context_is_left_out_not_a_failed_chunk(tmp_path):
    db = _seeded_two(tmp_path)    # bodies the v2 stub returns facts for
    try:
        _passes(db, _with_context(None), configs=({},))
        assert _receipts(db) == []
        rows = db.db.execute("SELECT content, meta FROM artifacts "
                             "WHERE kind='semantic_facts_v2'").fetchall()
        assert rows
        for content, meta in rows:
            facts = json.loads(content)["facts"]
            assert facts and all("patient_context" not in f for f in facts)
            # the stub's one LLM fact per body; hint facts are not LLM output
            assert json.loads(meta)["null_contexts_omitted"] == 1
    finally:
        db.close()


def test_a_null_beside_an_invalid_context_is_counted_apart(tmp_path):
    def mixed(prompt):
        if "原子事実抽出器" not in prompt:
            return _llm(prompt)
        doc = json.loads(_llm_v2(prompt))
        if doc["facts"]:
            null_fact = dict(doc["facts"][0], patient_context=None)
            bad_fact = dict(doc["facts"][0], patient_context=[],
                            statement=doc["facts"][0]["statement"] + "（合成）")
            doc["facts"] = [null_fact, bad_fact]
        return json.dumps(doc, ensure_ascii=False)

    db = _seeded_two(tmp_path)
    try:
        _passes(db, mixed, configs=({},))
        receipts = [json.loads(r[0]) for r in _receipts(db)]
        assert receipts and all(r["null_contexts_omitted"] >= 1
                                and r["dropped_reasons"] == {"context_shape": 1}
                                and sum(r["dropped_reasons"].values())
                                == r["dropped_in_failed_chunk"]
                                for r in receipts)
    finally:
        db.close()


@pytest.mark.parametrize("value,reason", [
    ([], "context_shape"), ("", "context_shape"), ({}, "context_ungrounded"),
])
def test_only_null_is_left_out(tmp_path, value, reason):
    """Empty but non-null values still fail the chunk as before."""
    db = _seeded_two(tmp_path)    # bodies the v2 stub returns facts for
    try:
        _passes(db, _with_context(value), configs=({},))
        receipts = [json.loads(r[0]) for r in _receipts(db)]
        assert receipts and all(r["dropped_in_failed_chunk"] > 0
                                and set(r["dropped_reasons"]) == {reason}
                                and r["null_contexts_omitted"] == 0
                                for r in receipts)
    finally:
        db.close()
