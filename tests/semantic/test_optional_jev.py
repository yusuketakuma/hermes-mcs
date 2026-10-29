"""Synthetic boundaries for optional Jev evaluation."""
import pytest

from semantic_audit import jev_skip_reason, optional_audit_status
from semantic_jev import JevError


@pytest.mark.parametrize("stop", ["rate_limited", "budget_exceeded"])
@pytest.mark.parametrize("chunked", [False, True])
def test_same_chunk_negative_survives_failed_answer_retry(stop, chunked):
    import semantic_jev as jev
    questions = {key: jev.noul_question("synthetic", "yes", "no") for key in ("a", "b")}

    class Partial:
        def __init__(self):
            self.calls = 0

        def evaluate(self, state, requested, deadline):
            self.calls += 1
            if self.calls == 1:
                error = JevError("protocol_error", "b:type_invalid")
                error.answers = {"a": {"type": "noul", "noul": 0.1}}
                error.failed = ["b"]
                raise error
            raise JevError(stop, "daily_cap" if stop == "budget_exceeded" else "http_429")

    with pytest.raises(JevError) as caught:
        if chunked:
            jev.evaluate_chunked(Partial(), {}, questions, 1, 2)
        else:
            jev.evaluate_once_more(Partial(), {}, questions, 1)
    assert caught.value.kind == stop
    assert caught.value.partial_answers == {"a": {"type": "noul", "noul": 0.1}}


@pytest.mark.parametrize("bad", [
    {"foreign": {"type": "noul", "noul": 0.1}},
    {"a": {"type": "noul", "noul": float("nan")}},
    {"a": {"type": "noul", "noul": 0.9}},
    ["not", "a", "mapping"],
])
def test_invalid_or_conflicting_retry_partial_never_authorizes_skip(bad):
    import semantic_jev as jev
    questions = {key: jev.noul_question("synthetic", "yes", "no") for key in ("a", "b")}

    class Partial:
        calls = 0

        def evaluate(self, state, requested, deadline):
            self.calls += 1
            if self.calls == 1:
                error = JevError("protocol_error", "b:type_invalid")
                error.answers, error.failed = {"a": {"type": "noul", "noul": 0.1}}, ["b"]
                raise error
            error = JevError("rate_limited", "http_429")
            error.partial_answers = bad
            raise error

    with pytest.raises(JevError) as caught:
        jev.evaluate_chunked(Partial(), {}, questions, 1, 2)
    assert caught.value.kind == "protocol_error"
    assert jev_skip_reason(caught.value) is None


@pytest.mark.parametrize("prior", ["bound", "missing_binding", "corrupt", "corrupt_findings", "older_corrupt"])
def test_pending_negative_survives_next_run_with_jev_disabled(tmp_path, monkeypatch, prior):
    import json
    import time
    import semantic_audit
    import semantic_drain
    from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

    db = _seeded(tmp_path)
    count = [0]

    def claim_audit(client, bundle, summary, deadline, *args):
        count[0] += 1
        if client is not None:
            client.last_error = JevError("protocol_error", "claim_2:type_invalid", retryable=True)
            return [{"code": "claim_contradicts", "claim": "claim_1", "confidence": 0.9},
                    {"code": "support_unevaluated", "claim": "claim_2"}], False
        return [{"code": "support_unevaluated"}], False

    real_audit = semantic_audit.audit_claims
    monkeypatch.setattr(semantic_audit, "audit_claims", claim_audit)
    try:
        first = semantic_drain.run_due(db, _cfg("assist", budget=100), {"errors": []},
                                       time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert first["done"] == 0
        [old] = db.artifacts("semantic_audit", message_id=1)
        assert json.loads(old["meta"])["audit_status"] == "PENDING"
        if prior == "missing_binding":
            db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.claim_input_binding') "
                          "WHERE kind='semantic_audit' AND message_id=1")
        elif prior == "corrupt":
            db.db.execute("UPDATE artifacts SET content='not-json' WHERE kind='semantic_audit' AND message_id=1")
        elif prior == "corrupt_findings":
            db.db.execute("UPDATE artifacts SET content=json_set(content,'$.findings',json(?)) "
                          "WHERE kind='semantic_audit' AND message_id=1",
                          ('[{"code":"support_unevaluated"}]',))
        elif prior == "older_corrupt":
            db.db.execute("UPDATE artifacts SET content='not-json' WHERE kind='semantic_audit' AND message_id=1")
            db.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "SELECT kind,project_id,message_id,'{}',model,json_set(meta,'$.policy_fingerprint',?),created_at "
                          "FROM artifacts WHERE kind='semantic_audit' AND message_id=1 ORDER BY artifact_id DESC LIMIT 1",
                          ("b" * 64,))
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        attempts = db.db.execute("SELECT attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
        second = semantic_drain.run_due(db, _cfg("assist", budget=0), {"errors": []},
                                        time.monotonic() + 300, llm_fn=_llm)
        assert second["done"] == 0
        current = json.loads(db.artifacts("semantic_audit", message_id=1)[-1]["content"])
        assert current["status"] == "PENDING"
        codes = {f["code"] for f in current["findings"]}
        assert ("claim_contradicts" if prior == "bound" else "prior_audit_unverified") in codes
        if prior != "bound":
            assert "claim_contradicts" not in codes
        for _ in range(2):
            db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
            db.db.commit()
            semantic_drain.run_due(db, _cfg("assist", budget=0), {"errors": []},
                                   time.monotonic() + 300, llm_fn=_llm)
        job = db.db.execute("SELECT state,attempts FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] == "pending" and job["attempts"] == attempts
        monkeypatch.setattr(semantic_audit, "audit_claims", real_audit)
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        recovered = semantic_drain.run_due(db, _cfg("assist", budget=100), {"errors": []},
                                           time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert recovered["done"] == 1
    finally:
        db.close()


def test_repaired_summary_stays_pending_if_jev_cannot_recheck(tmp_path, monkeypatch):
    import json
    import time
    import semantic_audit
    import semantic_drain
    from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

    db = _seeded(tmp_path)
    calls = [0]

    def claim_audit(client, bundle, summary, deadline, *args):
        calls[0] += 1
        if calls[0] == 1:
            return [{"code": "claim_not_supported", "claim": "claim_1", "confidence": 0.9}], True
        if client is not None:
            client.last_error = JevError("rate_limited", "http_429", retryable=True)
        return [{"code": "support_unevaluated"}], False

    monkeypatch.setattr(semantic_audit, "audit_claims", claim_audit)
    try:
        out = semantic_drain.run_due(db, _cfg("assist", budget=100), {"errors": []},
                                     time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)
        assert out["done"] == 0 and calls[0] >= 2
        [row] = db.artifacts("semantic_audit", message_id=1)
        audit = json.loads(row["content"])
        assert audit["status"] == "PENDING"
        assert {f["code"] for f in audit["findings"]} >= {"prior_audit_unverified", "support_unevaluated"}
        assert all(f["code"] != "claim_not_supported" for f in audit["findings"])
    finally:
        db.close()


def test_legacy_pending_without_any_jev_answer_can_skip_after_recheck(tmp_path, monkeypatch):
    import json
    import time
    import semantic_audit
    import semantic_drain
    from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

    db = _seeded(tmp_path)

    def claim_audit(client, bundle, summary, deadline, *args):
        if client is not None:
            client.last_error = JevError("protocol_error", "claim_1:type_invalid", retryable=True)
        return [{"code": "support_unevaluated"}], False

    monkeypatch.setattr(semantic_audit, "audit_claims", claim_audit)
    try:
        assert semantic_drain.run_due(db, _cfg("assist", budget=100), {"errors": []},
                time.monotonic() + 300, jev_client=_FakeJev(), llm_fn=_llm)["done"] == 0
        db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.summary_artifact_id',"
                      "'$.claim_input_binding','$.summary_hash','$.target_revision','$.audit_content_hash') "
                      "WHERE kind='semantic_audit'")
        db.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
        db.db.commit()
        out = semantic_drain.run_due(db, _cfg("assist", budget=0), {"errors": []},
                                     time.monotonic() + 300, llm_fn=_llm)
        assert out["done"] == 1
        assert all(json.loads(row["content"])["status"] == "SKIPPED"
                   for row in db.artifacts("semantic_audit")[-2:])
    finally:
        db.close()


def test_prior_audit_scan_limit_fails_closed(tmp_path):
    import json
    import semantic_drain
    from semantic_testkit import _seeded

    db = _seeded(tmp_path)
    try:
        meta = json.dumps({"fingerprint": "c" * 64, "policy_fingerprint": "d" * 64})
        with db.db:
            db.db.executemany("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                              "VALUES('semantic_audit',1,1,'{}','synthetic',?,0)", [(meta,)] * 1025)
        args = (db.db, 1, 1, "a" * 64, "b" * 64, "revision", "shadow", False, None, {}, "binding")
        assert semantic_drain._prior_pending_findings(*args) == ([], True)
        with db.db:
            db.db.execute("DELETE FROM artifacts WHERE artifact_id=(SELECT MAX(artifact_id) FROM artifacts "
                          "WHERE kind='semantic_audit')")
        assert semantic_drain._prior_pending_findings(*args) == ([], False)
    finally:
        db.close()


@pytest.mark.parametrize(("kind", "detail", "expected"), [
    ("no_api_key", "unset", "not_configured"),
    ("budget_exceeded", "daily_cap", "daily_budget"),
    ("rate_limited", "http_429", "service_unavailable"),
    ("transport", "synthetic", "service_unavailable"),
    ("timeout", "synthetic", "service_unavailable"),
    ("timeout", "job_deadline_exceeded", None),
    ("auth_error", "http_401", "service_unavailable"),
    ("payment_required", "http_402", "service_unavailable"),
    ("budget_exceeded", "semantic_config_unreadable", None),
    ("budget_exceeded", "semantic_until_expired", None),
    ("budget_exceeded", "semantic_usage_invalid", None),
    ("budget_exceeded", "usage_ledger_unavailable", None),
    ("budget_exceeded", "reservation_day_changed", None),
    ("budget_exceeded", "job_budget", None),
    ("protocol_error", "invalid_answer", None),
    ("contract_error", "synthetic", None),
])
def test_only_explicit_unavailability_can_skip(kind, detail, expected):
    assert jev_skip_reason(JevError(kind, detail)) == expected


def test_unrelated_exception_cannot_claim_unavailability():
    class UnexpectedError(Exception):
        kind = "no_api_key"

    assert jev_skip_reason(UnexpectedError()) is None


@pytest.mark.parametrize(("code", "jev", "reason", "expected"), [
    ([], [{"code": "support_unevaluated"}], "daily_budget", "SKIPPED"),
    ([], [{"code": "support_unevaluated"}], None, "PENDING"),
    ([{"code": "input_incomplete"}], [], "daily_budget", "PENDING"),
    ([{"code": "evidence_span_mismatch"}], [], "daily_budget", "NEEDS_REVIEW"),
    ([], [{"code": "claim_contradicts"}, {"code": "support_unevaluated"}],
     "daily_budget", "NEEDS_REVIEW"),
    ([], [{"code": "claim_not_supported"}, {"code": "support_unevaluated"}],
     "daily_budget", "REPAIR_REQUIRED"),
    ([], [{"code": "unexpected"}], "daily_budget", "NEEDS_REVIEW"),
    ([{"code": "unexpected_code_failure"}], [], "daily_budget", "NEEDS_REVIEW"),
])
def test_skip_never_discards_verified_findings(code, jev, reason, expected):
    assert optional_audit_status(code, jev, reason, False) == expected


def test_canonical_work_can_finish_without_jev_but_does_not_claim_pass(tmp_path, monkeypatch):
    import json
    import time
    import semantic_drain
    import semantic_jev
    import semantic_send_gate
    import semantic_store
    from semantic_testkit import _canonical_cfg, _llm_v2, _seeded_two

    db = _seeded_two(tmp_path)
    cfg = _canonical_cfg()
    cfg["semantic"]["daily_request_budget"] = 0
    monkeypatch.setattr(semantic_jev, "api_key", lambda *args, **kwargs: None)
    try:
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     llm_fn=_llm_v2)
        assert out["done"] == 1
        rows = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='semantic_audit'").fetchall()
        assert len(rows) == 2
        for row in rows:
            content, meta = json.loads(row["content"]), json.loads(row["meta"])
            assert content["status"] == "SKIPPED"
            assert meta["audit_status"] == "SKIPPED"
            assert meta["jev_skip_reason"] in {"not_configured", "disabled"}
        scfg, _ = semantic_drain.runtime_config(cfg)
        source = db.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m "
                               "JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
        assert semantic_drain._reprocess_target_state(db, source, scfg) == 'current_skipped'
        bundle = semantic_store.thread_bundle(db.db, 1, 1, [1])
        policy = semantic_drain.policy_fingerprint(scfg)
        assert semantic_send_gate.current_summary(db.db, 1, 1, bundle, policy) is not None
        assert len(db.artifacts('semantic_facts_v4_skipped')) == 2
        assert not db.artifacts('semantic_facts_v4')
        assert not db.artifacts('canonical_projection')
        db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.jev_evaluated',json('true')) "
                      "WHERE kind='semantic_audit' AND message_id=1")
        db.db.commit()
        assert semantic_drain._reprocess_target_state(db, source, scfg) != 'current_skipped'
        assert semantic_send_gate.current_summary(db.db, 1, 1, bundle, policy) is None
    finally:
        db.close()


@pytest.mark.parametrize('budget, expected', [(0, 'disabled'), (50, 'not_configured')])
def test_shadow_work_can_finish_without_jev_but_does_not_claim_pass(tmp_path, monkeypatch, budget, expected):
    import json
    import time
    import semantic_drain
    import semantic_jev
    from semantic_testkit import _cfg, _llm, _seeded

    db = _seeded(tmp_path)
    cfg = _cfg("assist", fact_source="shadow", summary_mode="shadow",
               loop_mode="shadow", budget=budget)
    monkeypatch.setattr(semantic_jev, "api_key", lambda *args, **kwargs: None)
    try:
        out = semantic_drain.run_due(db, cfg, {"errors": []}, time.monotonic() + 300,
                                     llm_fn=_llm)
        assert out["done"] == 1
        rows = db.db.execute("SELECT content,meta FROM artifacts WHERE kind='semantic_audit'").fetchall()
        assert rows
        for row in rows:
            content, meta = json.loads(row["content"]), json.loads(row["meta"])
            assert content["status"] == "SKIPPED"
            assert meta["audit_status"] == "SKIPPED"
            assert meta["jev_skip_reason"] == expected
        scfg, _ = semantic_drain.runtime_config(cfg)
        source = db.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m "
                               "JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1").fetchone()
        assert semantic_drain._reprocess_target_state(db, source, scfg) == 'current_skipped'
    finally:
        db.close()


def test_qc_skip_is_bound_and_reseeded_only_after_jev_recovers(tmp_path):
    import json
    import time
    import semantic_policy
    import semantic_qc
    from extract_testkit import _extract_artifact, _hash, _ledger, _message
    from semantic_testkit import _cfg

    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body="合成の観察。", posted_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))])
        source_id = _extract_artifact(db, 1, {"symptoms": [{"text": "観察"}]}, _hash(db))
        assert semantic_qc._qc_seed(db, time.time(), skip_mode=True) == 1
        job = db.db.execute("SELECT * FROM fetch_jobs WHERE kind=?", (semantic_qc.QC_JOB_KIND,)).fetchone()
        cfg, _ = semantic_policy.semantic_config(_cfg(extract_qc="annotate", budget=0))
        assert semantic_qc._process_qc_job(db, cfg, job, None, time.monotonic() + 60,
                                            skip_reason="disabled") == "done"
        assert not db.artifacts(semantic_qc.QC_ARTIFACT)
        [row] = db.artifacts("extract_qc_skipped")
        assert json.loads(row["content"]) == {"status": "SKIPPED", "reason": "disabled"}
        assert json.loads(row["meta"])["source_artifact_id"] == source_id
        assert semantic_qc._qc_seed(db, time.time(), skip_mode=True) == 0
        assert semantic_qc._qc_seed(db, time.time(), skip_mode=False) == 1
    finally:
        db.close()


def test_expired_scope_is_not_bypassed_when_jev_budget_is_zero(tmp_path, monkeypatch):
    import json
    import time
    import semantic_drain
    import semantic_jev
    from semantic_testkit import _cfg, _llm, _seeded

    db = _seeded(tmp_path)
    cfg = _cfg('assist', fact_source='shadow', summary_mode='shadow', loop_mode='shadow', budget=0,
               until='2000-01-01T00:00:00+00:00')
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(cfg), encoding='utf-8')
    monkeypatch.setattr(semantic_jev, 'api_key', lambda *args, **kwargs: None)
    try:
        result = {'errors': []}
        out = semantic_drain.run_due(db, cfg, result, time.monotonic() + 300,
                                     llm_fn=_llm, cfg_path=str(path))
        assert out['done'] == 0
        assert 'semantic: jev_skip_unverified' in result['errors']
        assert not db.artifacts('semantic_audit')
        assert not db.artifacts('semantic_loop_skipped')
    finally:
        db.close()


@pytest.mark.parametrize('corrupt', [
    '{"consecutive_failures":3,"open_until":9999999999,"failure_class":"http_429","failure_class":"http_429"}',
    '{"consecutive_failures":3,"open_until":9999999999,"failure_class":"http_429","unused":NaN}',
])
def test_malformed_jev_circuit_never_authorizes_skip(tmp_path, corrupt):
    import time
    import semantic_drain
    from semantic_testkit import _cfg, _llm, _seeded

    db = _seeded(tmp_path)
    try:
        db.artifact_add('semantic_circuit', corrupt)
        out = semantic_drain.run_due(db, _cfg('assist', budget=0), {'errors': []},
                                     time.monotonic() + 300, llm_fn=_llm)
        assert out['circuit_open'] is True and out['done'] == 0
        assert not db.artifacts('semantic_audit')
        assert not db.artifacts('semantic_loop_skipped')
    finally:
        db.close()


def test_regular_jev_circuit_can_skip_without_clearing_it(tmp_path):
    import json
    import time
    import semantic_drain
    from semantic_testkit import _cfg, _llm, _seeded

    db = _seeded(tmp_path)
    try:
        db.artifact_add('semantic_circuit', json.dumps({'consecutive_failures': 3,
                        'open_until': time.time() + 300, 'failure_class': 'http_429'}))
        out = semantic_drain.run_due(db, _cfg('assist', budget=0), {'errors': []},
                                     time.monotonic() + 300, llm_fn=_llm)
        assert out['done'] == 1 and out['jev_skipped']['circuit_open'] == 1
        assert len(db.artifacts('semantic_circuit')) == 1
    finally:
        db.close()


def test_budget_exhausted_during_assessment_resumes_without_more_jev(tmp_path):
    import time
    import semantic_drain
    from semantic_testkit import _cfg, _FakeJev, _llm, _seeded

    ledger = _seeded(tmp_path)

    class Exhausted(_FakeJev):
        def evaluate(self, state, questions, deadline):
            if self.requests_made:
                raise JevError('budget_exceeded', 'daily_cap', retryable=True)
            self.requests_made += 1
            ledger.artifact_add('semantic_usage', '{}', meta={'jev_requests': 1})
            return {'answers': {next(iter(questions)): {'type': 'noul', 'noul': 0.1}}}

    try:
        client = Exhausted()
        cfg = _cfg('assist', fact_source='shadow', summary_mode='shadow', loop_mode='shadow', budget=1,
                   max_questions_per_request=1)
        result = semantic_drain.run_due(ledger, cfg, {'errors': []}, time.monotonic() + 300,
                                        jev_client=client, llm_fn=_llm)
        assert result['done'] == 1
        assert client.requests_made == 1
        assert any('daily_budget' in row['meta'] for row in ledger.artifacts('semantic_audit'))
        import json
        partial = [json.loads(row['content'])['verdicts'] for row in ledger.artifacts('semantic_assess')
                   if json.loads(row['meta']).get('technical_status') == 'skipped']
        assert any(value['verdict'] == 'NO_MATCH' for row in partial for value in row.values())
    finally:
        ledger.close()


def test_qc_keeps_a_negative_answer_when_a_later_chunk_hits_budget(tmp_path):
    import json
    import time
    import semantic_policy
    import semantic_qc
    from extract_testkit import _extract_artifact, _hash, _ledger, _message
    from semantic_testkit import _cfg, _FakeJev

    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(body='合成の観察。', posted_at=time.strftime('%Y-%m-%dT%H:%M:%S+00:00', time.gmtime()))])
        _extract_artifact(db, 1, {'symptoms': [{'text': '観察1'}, {'text': '観察2'}]}, _hash(db))
        assert semantic_qc._qc_seed(db, time.time()) == 1
        job = db.db.execute('SELECT * FROM fetch_jobs WHERE kind=?', (semantic_qc.QC_JOB_KIND,)).fetchone()
        cfg, _ = semantic_policy.semantic_config(_cfg(extract_qc='annotate', budget=1, max_questions_per_request=1))

        class Partial(_FakeJev):
            def evaluate(self, state, questions, deadline):
                if self.requests_made:
                    raise JevError('budget_exceeded', 'daily_cap', retryable=True)
                self.requests_made += 1
                db.artifact_add('semantic_usage', '{}', meta={'jev_requests': 1})
                return {'answers': {next(iter(questions)): {'type': 'noul', 'noul': 0.1}}}

        assert semantic_qc._process_qc_job(db, cfg, job, Partial(), time.monotonic() + 60) == 'done'
        [row] = db.artifacts('extract_qc_skipped')
        content = json.loads(row['content'])
        assert content['status'] == 'SKIPPED'
        assert list(content['partial_answers'].values()) == [{'type': 'noul', 'noul': 0.1}]
        assert not db.artifacts(semantic_qc.QC_ARTIFACT)
        import extract_llm
        assert not semantic_qc.skipped_current(db.db, 1, 1, json.loads(row['meta'])['source_artifact_id'],
                                               _hash(db), extract_llm.EXTRACT_VERSION, cfg)
    finally:
        db.close()
