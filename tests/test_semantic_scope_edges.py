"""Scope and question-correlation contracts for AT-011/AT-019."""
import json
import time

import pytest

import semantic
import semantic_jev as jev
from test_mcs_semantic import _message, _patient, _seeded, _cfg, _FakeJev, _llm


def test_jev_question_ids_are_correlation_only():
    questions = {
        "renamed-target-check": jev.noul_question(
            "Evaluate only state.target.text.", "yes", "no"),
        "choice-after-rename": jev.choice_question(
            "Evaluate only state.target.text.", {"a": "A", "b": "B"}),
    }
    raw = {
        "model": jev.JEV_MODEL,
        "usage": {"input_tokens": 1, "output_tokens": 1},
        "answers": {
            "renamed-target-check": {"type": "noul", "noul": 0.9},
            "choice-after-rename": {
                "type": "choice", "choice": "b", "confidence": 0.8,
                "probabilities": {"a": 0.2, "b": 0.8},
            },
        },
    }
    out = jev.validate_answers(raw, questions, jev.JEV_MODEL)
    assert out["answers"]["renamed-target-check"]["noul"] == 0.9
    assert out["answers"]["choice-after-rename"]["choice"] == "b"
    assert all("state.target.text" in q["instructions"]
               for q in questions.values())


def test_thread_bundle_rejects_unrelated_targets_instead_of_dropping_them(
        tmp_path):
    db = _seeded(tmp_path)
    try:
        _patient(db, pid=2)
        db.save_messages([
            _message(3, pid=1, body="同じ患者だが別threadの投稿。"),
            _message(4, pid=2, body="別患者の投稿。"),
        ])
        for target_ids in ([3], [4], [2, 3], [2, 4], [True], [1.0], "1"):
            with pytest.raises(ValueError, match="semantic_target_scope"):
                semantic.thread_bundle(db, 1, 1, target_ids)
        row = db.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
        payload = json.loads(row['payload'])
        payload['targets'] = [2, 4]
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'",
                          (json.dumps(payload),))
        client = _FakeJev()
        semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 300,
                         jev_client=client, llm_fn=_llm)
        assert not client.calls
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 'failed'
    finally:
        db.close()


@pytest.mark.parametrize('raw', ['{', '[]', 'null', '{"targets":[]}', '{"targets":null}'])
def test_invalid_job_payload_is_quarantined_before_dispatch(tmp_path, raw):
    db = _seeded(tmp_path)
    try:
        with db.db:
            db.db.execute("UPDATE fetch_jobs SET payload=? WHERE kind='semantic'", (raw,))
        client = _FakeJev()
        semantic.run_due(db, _cfg(), {'errors': []}, time.monotonic() + 300,
                         jev_client=client, llm_fn=_llm)
        assert not client.calls
        assert db.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()[0] == 'failed'
    finally:
        db.close()
