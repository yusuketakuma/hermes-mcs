"""Durable daily reservations fence interleaved clients before POST."""
import json
import time

import pytest

import semantic
import semantic_drain
import semantic_jev as jev
import semantic_runtime as runtime
import semantic_store
from semantic_testkit import _cfg, _ledger, _message, _patient, _seeded


def _reserver(db, limit):
    row = db.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic' LIMIT 1").fetchone()
    return runtime.usage_reserver(db, runtime.JobToken.from_row(row), kind="semantic_usage",
                                  model=jev.JEV_MODEL, project_id=row["project_id"],
                                  message_id=row["message_id"], daily_request_budget=limit)


def test_interleaved_clients_stop_at_the_durable_cap_without_charging_an_attempt(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    person = _patient(db)
    person.messages = [_message(1, body="synthetic source alpha"), _message(3, body="synthetic source beta")]
    db.save_patient(person, notify={"source": "unread"}, semantic=True)
    config = _cfg("shadow", budget=2, job_budget_seconds=450)
    deadline = time.monotonic() + 480
    calls, nesting = [], [False]
    monkeypatch.setattr(semantic_drain, "_invalidated_at", None)
    monkeypatch.setattr(semantic_drain.v4, "reproject_stale", lambda *_: {})

    def response(body):
        return 200, {}, json.dumps({"model": jev.JEV_MODEL,
            "answers": {key: {"type": "noul", "noul": 0.9} for key in body["questions"]},
            "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()

    def post_b(body, _timeout):
        calls.append("B")
        return response(body)

    client_b = jev.JevClient(api_key="synthetic-unused-key", post_fn=post_b)

    def post_a(body, _timeout):
        calls.append("A")
        if calls == ["A"]:
            nesting[0] = True
            try:
                assert semantic_drain.run_due(
                    db, config, {"errors": []}, deadline, lane="backlog", max_jobs=1,
                    jev_client=client_b, llm_fn=lambda *_: None)["done"] == 1
            finally:
                nesting[0] = False
        return response(body)

    client_a = jev.JevClient(api_key="synthetic-unused-key", post_fn=post_a)
    question = {"opaque": jev.noul_question("Synthetic token test", "yes", "no")}

    def process(ledger, scfg, job, client, _llm, end, **kwargs):
        token = runtime.JobToken.from_row(job)

        def guard(stage):
            runtime.guard(ledger, token, deadline=end, expected_config_generation=None,
                          expected_mode=scfg["mode"], stage=stage)

        hooked, _ = runtime.bind_jev(client, guard, kwargs["reserve_fn"])
        try:
            for _ in range(1 if nesting[0] else 2):
                hooked.evaluate({"target": {"id": "opaque", "text": "synthetic token"}}, question, end)
        except jev.JevError as error:
            # Same resource classification used by the real per-stage handlers.
            assert semantic_drain._jev_failure_class(error) == "resource"
            return "deferred"
        assert runtime.transition(ledger, token, "done")
        return "done"

    monkeypatch.setattr(semantic, "_process_job", process)
    try:
        out = semantic_drain.run_due(db, config, {"errors": []}, deadline, lane="backlog", max_jobs=1,
                                     jev_client=client_a, llm_fn=lambda *_: None)
        assert calls == ["A", "B"]
        assert semantic_store.jev_usage_today(db) == 2
        assert (client_a.requests_made, client_b.requests_made) == (1, 1)
        assert client_a.last_error.kind == "budget_exceeded"
        assert out["deferred"] == 1 and out["done"] == out["failed"] == 0
        row = db.db.execute("SELECT state,attempts,next_try FROM fetch_jobs WHERE message_id=1 "
                            "AND kind='semantic'").fetchone()
        assert row["state"] == "pending" and row["attempts"] == 0 and row["next_try"] > time.time()
        again = semantic_drain.run_due(db, config, {"errors": []}, deadline, lane="backlog", max_jobs=1,
                                       jev_client=client_a, llm_fn=lambda *_: None)
        assert again["done"] == 0 and calls == ["A", "B"]
    finally:
        db.close()


def test_normal_reservations_allow_the_full_cap_then_hold_and_pick_up_new_limit(tmp_path):
    db = _seeded(tmp_path)
    try:
        reserve = _reserver(db, 2)
        for _ in range(2):
            reserve({"synthetic": True}, 1)
        assert semantic_store.jev_usage_today(db) == 2
        for limit in (2, 1, 0):
            with pytest.raises(jev.JevError) as error:
                _reserver(db, limit)({"synthetic": True}, 1)
            assert error.value.kind == "budget_exceeded"
            assert semantic_store.jev_usage_today(db) == 2
        _reserver(db, 3)({"synthetic": True}, 1)
        assert semantic_store.jev_usage_today(db) == 3
        assert not db.db.in_transaction
    finally:
        db.close()


def test_previous_jst_day_does_not_spend_today_and_declined_reservations_stay_absent(tmp_path, monkeypatch):
    db = _seeded(tmp_path)
    noon = 1_800_000_000
    monkeypatch.setattr(runtime.time, "time", lambda: noon)
    try:
        day = semantic_store._jst_day_start(noon)
        aid = db.artifact_add("semantic_usage", "{}", meta={"jev_requests": 100})
        with db.db:
            db.db.execute("UPDATE artifacts SET created_at=? WHERE artifact_id=?", (day - 1, aid))
        assert semantic_store.jev_usage_today(db) == 0
        _reserver(db, 1)({}, 1)
        assert semantic_store.jev_usage_today(db) == 1
        with pytest.raises(jev.JevError):
            _reserver(db, 1)({}, 1)
        assert len(db.artifacts("semantic_usage")) == 2
    finally:
        db.close()


@pytest.mark.parametrize("bad", ["{}", '{"jev_requests":true}', '{"jev_requests":-1}',
                                 '{"jev_requests":1.5}', "{broken", "[" * 1100 + "0" + "]" * 1100])
def test_unknown_usage_fails_closed_before_reservation(tmp_path, bad):
    db = _seeded(tmp_path)
    try:
        aid = db.artifact_add("semantic_usage", "{}")
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (bad, aid))
        before = len(db.artifacts("semantic_usage"))
        with pytest.raises(jev.JevError) as error:
            _reserver(db, 2)({}, 1)
        assert error.value.kind == "budget_exceeded" and error.value.detail == "semantic_usage_invalid"
        assert len(db.artifacts("semantic_usage")) == before
        assert not db.db.in_transaction
    finally:
        db.close()
