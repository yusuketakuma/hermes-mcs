"""Loop-only readiness is proven by the synthetic worker, not a done job."""
import semantic_job_processor
import json
import time

import pytest

from inference_version import CURRENT_VERSION, VERSION_KEY
from mcs_util import payload_hash
import patient_context_refresh
import semantic_assessment
import semantic_drain as drain
import semantic_policy
import semantic_runtime
import semantic_store
from semantic_testkit import BODY, _FakeJev, _PassJev, _cfg, _llm_v2, _message
from test_patient_context_refresh import _raw_proof as _summary_raw_proof, seed
from test_patient_context_refresh import runtime as runtime


def _config(*, budget=50, fact_source="legacy"):
    return _cfg("assist", summary_mode="off", loop_mode="assist",
                budget=budget, fact_source=fact_source)


def _empty_llm(_prompt):
    return json.dumps({"facts": []})


def _request_llm(_prompt):
    return json.dumps({"facts": [{"statement": "確認依頼の記載がある",
        "kind": "explicit_request", "status": "not_stated", "polarity": "affirmed",
        "time_text": None, "quantity": None, "evidence_quote": "確認お願いします"}]})


def _raw_proof(led, pid, mid):
    # The generic patient-context fixture only stores a summary. These loop
    # sources contain clinical/request hints, so a resolved raw proof must
    # actually retain them rather than merely claim zero missing items in meta.
    import extract_llm
    import extract_validation

    _summary_raw_proof(led, pid, mid)
    row = dict(led.db.execute("SELECT * FROM messages WHERE project_id=? AND message_id=?",
                              (pid, mid)).fetchone())
    body = row["body_text"]
    if body == BODY:
        document = {"summary": "合成の処方変更と確認依頼", "meds": [{
            "name": "カロナール", "dose": None, "freq": "1日3回", "action": "change",
            "status": "planned", "subject": "patient", "negated": False,
            "evidence": body.split("。", 1)[0]}], "requests": [{
                "to": None, "from": None, "kind": "request", "action": "確認お願いします",
                "evidence": "確認お願いします"}]}
    else:
        assert body == "アムロジピン5mgを継続します。"
        document = {"summary": "合成の継続処方", "meds": [{
            "name": "アムロジピン", "dose": None, "action": "none", "status": "current",
            "subject": "patient", "negated": False, "evidence": body}]}
    valid = extract_validation.validate(document, body)
    assert valid is not None and not valid.get("_items_dropped") and not valid.get("_evidence_dropped")
    assert extract_llm._hint_coverage(valid, extract_llm._rule_hints(row), body) == {
        "state": "hint_gaps_resolved", "remaining_hint_candidates": 0}
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=("
                       "SELECT MAX(artifact_id) FROM artifacts WHERE kind='extract_llm' "
                       "AND project_id=? AND message_id=?)", (json.dumps(document), pid, mid))
    assert patient_context_refresh._raw_extraction_current(led.db, row, _config())[0]


def _seed(runtime, *, reply=False, targets=None):
    led, _, _ = runtime
    seed(led, 1, body=BODY, proof=False)
    if reply:
        led.save_messages([_message(2, parent=1)], project_id=1)
    for mid in (1, 2) if reply else (1,):
        _raw_proof(led, 1, mid)
    led.semantic_seed(1, targets or ([1, 2] if reply else [1]),
                      {"source": "synthetic-history", "notification_free": True})
    return led


def _run(led, cfg, *, llm=_empty_llm, client=None):
    return drain.run_due(led, cfg, {"errors": []}, time.monotonic() + 300,
                         jev_client=client, llm_fn=llm)


def _state(led, cfg, mid=1):
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    row = led.db.execute("SELECT m.*,p.project_id AS patient_id,p.fetch_reason "
                         "FROM messages m JOIN patients p ON p.project_id=m.project_id "
                         "WHERE m.message_id=?", (mid,)).fetchone()
    return drain._reprocess_target_state(led, row, scfg, cfg=cfg)


def _ready(runtime, cfg):
    led, clock, _ = runtime
    # The shared fixture controls the patient-refresh clock separately;
    # the loop manifest consumes the same real monotonic deadline as the worker.
    clock.mono = time.monotonic()
    return patient_context_refresh._inference_ready(led, 1, cfg, deadline=clock.mono + 600)


def _receipts(led):
    return led.artifacts(drain.KIND_LOOP_COMPLETION, project_id=1)


@pytest.mark.parametrize("field", ["meds", "requests"])
def test_resolved_metadata_cannot_hide_missing_raw_source_hints(runtime, field):
    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, client=_FakeJev())["done"] == 1
    assert _state(led, cfg) == "current"
    raw = led.artifacts("extract_llm", project_id=1, message_id=1)[-1]
    document = json.loads(raw["content"])
    document[field] = []
    with led.db:
        led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                       (json.dumps(document), raw["artifact_id"]))
    row = led.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=1").fetchone()
    assert not patient_context_refresh._raw_extraction_current(led.db, row, cfg)[0]
    assert _state(led, cfg) == "held"
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("fact_source", ["legacy", "shadow"])
def test_worker_proves_zero_candidates_and_readiness(runtime, fact_source):
    led = _seed(runtime)
    cfg = _config(fact_source=fact_source)
    out = _run(led, cfg, client=_FakeJev())
    assert out["done"] == 1
    assert not led.artifacts("loop_candidate", project_id=1)
    assert not led.artifacts("loop_event", project_id=1)
    receipts = _receipts(led)
    assert receipts
    content = json.loads(receipts[-1]["content"])
    assert content["status"] == "DONE" and content["target_ids"] == [1]
    assert content["loop_mode"] == "assist"
    assert _state(led, cfg) == "current"
    assert _ready(runtime, cfg)


@pytest.mark.parametrize('borrowed', [False, True])
@pytest.mark.parametrize('damaged', [False, True])
def test_store_readiness_preserves_transaction_and_rejects_invalid_proof(runtime, monkeypatch, borrowed, damaged):
    import builtins
    import socket

    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, client=_FakeJev())['done'] == 1
    if damaged:
        with led.db:
            led.db.execute("UPDATE artifacts SET content='{}' WHERE kind=?", (drain.KIND_LOOP_COMPLETION,))
    scfg, errors = semantic_policy.runtime_config(cfg)
    assert not errors
    row = led.db.execute('SELECT m.*,p.project_id AS patient_id,p.fetch_reason FROM messages m '
                         'JOIN patients p ON p.project_id=m.project_id WHERE m.message_id=1').fetchone()
    before = [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')]
    if borrowed:
        led.db.execute('BEGIN')
    importer = builtins.__import__

    def read_only_import(name, *args, **kwargs):
        if name == 'semantic_drain':
            pytest.fail('reader imported semantic worker')
        return importer(name, *args, **kwargs)

    def no_network(*args, **kwargs):
        pytest.fail('reader attempted network access')

    monkeypatch.setattr(builtins, '__import__', read_only_import)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    assert semantic_store.reprocess_target_state(led, row, scfg, cfg=cfg) == ('held' if damaged else 'current')
    assert led.db.in_transaction is borrowed
    assert [tuple(r) for r in led.db.execute('SELECT * FROM artifacts ORDER BY artifact_id')] == before
    if borrowed:
        led.db.rollback()


def test_store_import_does_not_load_semantic_worker():
    from pathlib import Path
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    code = '''import sys
sys.path.insert(0, sys.argv[1])
import _mcs_path
class NoWorker:
    def find_spec(self, fullname, *args):
        if fullname == 'semantic_drain':
            raise AssertionError('reader imported semantic worker')
sys.meta_path.insert(0, NoWorker())
import semantic_store
assert 'semantic_drain' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(root / 'mcs')],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_done_job_without_worker_receipt_is_not_completion(runtime):
    led = _seed(runtime)
    cfg = _config()
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='done' WHERE kind='semantic'")
    assert not _receipts(led)
    assert _state(led, cfg) == "held"
    assert not _ready(runtime, cfg)


def test_disabled_jev_produces_explicit_skip_not_done(runtime):
    led = _seed(runtime)
    cfg = _config(budget=0)
    assert _run(led, cfg)["done"] == 1
    receipts = _receipts(led)
    assert receipts and json.loads(receipts[-1]["content"])["status"] == "SKIPPED"
    assert _state(led, cfg) == "current_skipped"
    assert _ready(runtime, cfg)


@pytest.mark.parametrize('damage', ['invalidated', 'malformed', 'replacement'])
def test_newer_unverified_loop_skip_never_reuses_old_receipt(runtime, damage):
    led = _seed(runtime)
    cfg = _config(budget=0)
    assert _run(led, cfg)['done'] == 1
    assert _state(led, cfg) == 'current_skipped'
    marker = led.artifacts('semantic_loop_skipped', project_id=1)[-1]
    with led.db:
        led.db.execute('INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) '
                       'SELECT kind,project_id,message_id,content,model,meta,created_at '
                       'FROM artifacts WHERE artifact_id=?', (marker['artifact_id'],))
        newest = led.db.execute('SELECT MAX(artifact_id) FROM artifacts WHERE kind=?',
                                ('semantic_loop_skipped',)).fetchone()[0]
        if damage == 'invalidated':
            led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true')) WHERE artifact_id=?",
                           (newest,))
        elif damage == 'malformed':
            led.db.execute("UPDATE artifacts SET meta='{' WHERE artifact_id=?", (newest,))
    assert _state(led, cfg) == 'held'
    assert not _ready(runtime, cfg)


def test_completed_relations_are_bound_to_actual_candidates(runtime):
    led = _seed(runtime, reply=True)
    cfg = _config()
    assert _run(led, cfg, llm=_request_llm, client=_FakeJev())["done"] == 1
    assert led.artifacts("loop_candidate", project_id=1)
    assert led.artifacts("loop_event", project_id=1)
    assert _state(led, cfg, 1) == _state(led, cfg, 2) == "current"
    event = led.artifacts("loop_event", project_id=1)[0]
    with led.db:
        led.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (event["artifact_id"],))
    assert _state(led, cfg, 1) != "current"
    assert not _ready(runtime, cfg)


def test_partial_relation_pass_never_publishes_completion(runtime, monkeypatch):
    led = _seed(runtime)
    cfg = _config()
    monkeypatch.setattr(drain, "_promote_loops", lambda *args, **kw: (False, False, False, True, False))
    monkeypatch.setattr(semantic_job_processor, '_promote_loops', drain._promote_loops)
    assert _run(led, cfg, client=_FakeJev())["done"] == 0
    assert not _receipts(led)
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


def test_invalid_medication_details_cannot_be_completion(runtime, monkeypatch):
    led = _seed(runtime)
    cfg = _config()
    monkeypatch.setattr(semantic_assessment, "evaluate_medication_events", lambda *a, **k: {
        "complete": False, "failure_reason": "invalid", "findings": [{"code": "medication_detail_fact_invalid"}]})
    _run(led, cfg, client=_FakeJev())
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("damage", ["delete", "malformed", "status", "mode", "targets"])
def test_missing_or_tampered_receipt_cannot_prove_readiness(runtime, damage):
    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, client=_FakeJev())["done"] == 1
    assert _state(led, cfg) == "current"
    ids = [row["artifact_id"] for row in _receipts(led)]
    assert ids
    with led.db:
        if damage == "delete":
            led.db.execute("DELETE FROM artifacts WHERE kind=?", (drain.KIND_LOOP_COMPLETION,))
        elif damage == "malformed":
            led.db.execute("UPDATE artifacts SET content='{' WHERE artifact_id=?", (ids[-1],))
        else:
            key, value = {"status": ("status", "SKIPPED"), "mode": ("loop_mode", "off"),
                          "targets": ("target_ids", [])}[damage]
            led.db.execute("UPDATE artifacts SET content=json_set(content,?,json(?)) WHERE artifact_id=?",
                           ("$." + key, json.dumps(value), ids[-1]))
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("change", ["body", "aba", "attachment", "policy", "loop_mode"])
def test_receipt_is_current_only_for_its_live_source_and_mode(runtime, change):
    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, client=_FakeJev())["done"] == 1
    assert _state(led, cfg) == "current"
    if change in ("body", "aba"):
        led.save_messages([_message(1, body="完全合成の変更後の投稿。")], project_id=1)
        if change == "aba":
            led.save_messages([_message(1, body=BODY)], project_id=1)
    elif change == "attachment":
        with led.db:
            led.db.execute("INSERT INTO attachments(message_id,file_id,name,state,created_at) "
                           "VALUES(1,'synthetic-file','synthetic','pending',0)")
    elif change == "policy":
        cfg["semantic"]["calibration_version"] = "synthetic-other-policy"
    else:
        cfg["semantic"]["loop_mode"] = "shadow"
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


def test_receipt_for_one_target_does_not_cover_a_sibling(runtime, monkeypatch):
    led = _seed(runtime, reply=True, targets=[1])
    cfg = _config()
    # The separate reprocess preamble normally seeds every missing sibling;
    # this test observes the worker's existing, narrower target set.
    monkeypatch.setattr(drain, 'reprocess_step', lambda *args, **kwargs: {})
    payload = led.db.execute("SELECT payload FROM fetch_jobs WHERE kind='semantic'").fetchone()[0]
    assert json.loads(payload)['targets'] == [1]
    assert _run(led, cfg, client=_FakeJev())["done"] == 1
    assert [row['message_id'] for row in _receipts(led)] == [1]
    assert _state(led, cfg, 1) == "current"
    assert _state(led, cfg, 2) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


def test_current_negative_audit_is_not_cleared_by_completion_receipt(runtime):
    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, client=_FakeJev())["done"] == 1
    scfg, _ = semantic_policy.runtime_config(cfg)
    fp = semantic_store.thread_bundle(led.db, 1, 1, [1])["source_fingerprint"]
    led.artifact_add("semantic_audit", json.dumps({"status": "NEEDS_REVIEW",
                     "findings": [{"code": "claim_contradicts"}], "target_message_id": 1}),
        project_id=1, message_id=1, meta={VERSION_KEY: CURRENT_VERSION, "fingerprint": fp,
        "policy_fingerprint": semantic_policy.policy_fingerprint(scfg), "audit_status": "NEEDS_REVIEW"})
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("kind", ["semantic_facts", "semantic_assess"])
@pytest.mark.parametrize("damage", ["alter", "delete"])
def test_completion_does_not_survive_changed_or_missing_worker_evidence(runtime, kind, damage):
    led = _seed(runtime)
    cfg = _config()
    assert _run(led, cfg, llm=_request_llm, client=_FakeJev())["done"] == 1
    assert _state(led, cfg) == "current"
    row = led.artifacts(kind, project_id=1, message_id=1)[-1]
    content = json.loads(row["content"])
    if damage == "alter":
        if kind == "semantic_facts":
            assert content["facts"]
            content["facts"][0]["statement"] = "完全合成の異なる内容"
        else:
            name = next(iter(content["verdicts"]))
            content["verdicts"][name] = {"noul": 0.1, "verdict": "NO_MATCH"}
    with led.db:
        if damage == "delete":
            led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (row["artifact_id"],))
        else:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                           (json.dumps(content, ensure_ascii=False), row["artifact_id"]))
    assert _state(led, cfg) == "held"
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("damage", ["invalidated", "duplicate", "bad_metadata"])
def test_bad_relation_present_before_worker_commit_cannot_get_receipt(runtime, monkeypatch, damage):
    led = _seed(runtime, reply=True)
    cfg = _config()
    original = drain._promote_loops
    observed = []

    def poison_before_commit(*args, **kwargs):
        result = original(*args, **kwargs)
        assert result[0] is True
        event = led.artifacts("loop_event", project_id=1)[0]
        with led.db:
            if damage == "invalidated":
                led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.invalidated',json('true')) "
                               "WHERE artifact_id=?", (event["artifact_id"],))
            elif damage == "duplicate":
                led.db.execute("INSERT INTO artifacts(kind,project_id,message_id,model,content,meta,created_at) "
                               "SELECT kind,project_id,message_id,model,content,meta,created_at "
                               "FROM artifacts WHERE artifact_id=?", (event["artifact_id"],))
            else:
                led.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.registry','invalid') "
                               "WHERE artifact_id=?", (event["artifact_id"],))
        observed.append(event["artifact_id"])
        return result

    monkeypatch.setattr(drain, "_promote_loops", poison_before_commit)
    monkeypatch.setattr(semantic_job_processor, '_promote_loops', drain._promote_loops)
    _run(led, cfg, llm=_request_llm, client=_FakeJev())
    assert observed
    assert not _receipts(led)
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


def test_failed_done_transition_rolls_back_completion_receipt(runtime, monkeypatch):
    led = _seed(runtime)
    cfg = _config()
    original = semantic_runtime.transition_tx
    observed = []

    def refuse_done(ledger, token, action, *args, **kwargs):
        if action == "done" and token.kind == drain.JOB_KIND:
            assert ledger.db.in_transaction
            assert _receipts(ledger)
            observed.append(True)
            return False
        return original(ledger, token, action, *args, **kwargs)

    monkeypatch.setattr(semantic_runtime, "transition_tx", refuse_done)
    _run(led, cfg, client=_FakeJev())
    assert observed and not led.db.in_transaction
    assert not _receipts(led)
    row = led.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert row["state"] != "done"
    assert _state(led, cfg) not in ("current", "current_skipped")
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("damage", ["other_reason", "invalid_reason", "invalid_status", "forged_done"])
def test_skip_status_and_reason_must_match_the_worker_skip(runtime, damage):
    led = _seed(runtime)
    cfg = _config(budget=0)
    assert _run(led, cfg)["done"] == 1
    assert _state(led, cfg) == "current_skipped"
    receipt = _receipts(led)[-1]
    content, meta = json.loads(receipt["content"]), json.loads(receipt["meta"])
    if damage == "other_reason":
        content["skip_reason"] = "daily_budget" if content["skip_reason"] != "daily_budget" else "disabled"
    elif damage == "invalid_reason":
        content["skip_reason"] = "synthetic_unknown"
    elif damage == "invalid_status":
        content["status"] = "UNKNOWN"
    else:
        content["status"], content["skip_reason"] = "DONE", None
    # Keep the receipt hash consistent: the independent skip evidence must
    # still prevent an unexecuted relation pass from being claimed as DONE.
    meta["content_hash"] = payload_hash(content)
    meta["loop_status"] = content["status"]
    meta["skip_reason"] = content["skip_reason"]
    with led.db:
        led.db.execute("UPDATE artifacts SET content=?,meta=? WHERE artifact_id=?",
                       (json.dumps(content), json.dumps(meta), receipt["artifact_id"]))
    assert _state(led, cfg) == "held"
    assert not _ready(runtime, cfg)


@pytest.mark.parametrize("damage", ["delete", "alter"])
def test_canonical_worker_completion_requires_its_actual_fact_audit(runtime, damage):
    led, _, _ = runtime
    seed(led, 1, body="アムロジピン5mgを継続します。", proof=False)
    _raw_proof(led, 1, 1)
    # _cfg builds the existing synthetic approved_config for these exact modes.
    cfg = _cfg("enforce", summary_mode="off", loop_mode="assist",
               fact_source="canonical", fact_source_gate="g6-v1:test")
    led.semantic_seed(1, [1], {"source": "synthetic-history", "notification_free": True})
    assert _run(led, cfg, llm=_llm_v2, client=_PassJev())["done"] == 1
    facts = led.artifacts("semantic_facts_v2", project_id=1, message_id=1)
    audits = led.artifacts("semantic_facts_audit", project_id=1, message_id=1)
    assert facts and audits and _receipts(led)
    doc = json.loads(facts[-1]["content"])
    audit = json.loads(audits[-1]["content"])
    assert doc["facts"] and audit["status"] == "PASS" and audit["evaluated"] is True
    assert audit["fact_verdicts"]
    assert _state(led, cfg) == "current"
    assert _ready(runtime, cfg)
    with led.db:
        if damage == "delete":
            led.db.execute("DELETE FROM artifacts WHERE artifact_id=?", (audits[-1]["artifact_id"],))
        else:
            # Retain PASS/evaluated/findings while changing the actual evidence;
            # a status-only check cannot establish the same audited outcome.
            audit["fact_verdicts"] = {}
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                           (json.dumps(audit), audits[-1]["artifact_id"]))
    assert _state(led, cfg) == "held"
    assert not _ready(runtime, cfg)


def test_new_human_retry_completes_with_old_rejection_preserved(runtime):
    import mcs_requests
    import semantic_loops
    from test_loop_relation_rejected import _RejectingJev

    class RelationRejectingJev(_RejectingJev):
        def evaluate(self, state, questions, deadline):
            if 'relation' in questions:
                return super().evaluate(state, questions, deadline)
            return _FakeJev.evaluate(self, state, questions, deadline)

    led = _seed(runtime, reply=True)
    cfg = _config()
    # The first attempt actually records an incomplete pair; it is history
    # belonging to the original retry scope, not a fabricated rejection.
    assert _run(led, cfg, llm=_request_llm, client=RelationRejectingJev())["done"] == 0
    rejected = led.artifacts(semantic_loops.KIND_LOOP_REJECTED, project_id=1)
    assert rejected and json.loads(rejected[-1]["meta"])["retry_scope"] == ""
    assert not _receipts(led)
    assert _state(led, cfg, 1) in ("held", "pending")
    assert _state(led, cfg, 2) in ("held", "pending")
    assert not _ready(runtime, cfg)

    # Simulate exhaustion and exercise the actual human-authorized retry
    # command, which assigns a new retry scope without erasing history.
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET state='failed',attempts=6,next_try=0 WHERE kind='semantic'")
    job = led.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
    command = {"version": 1, "cmd": "ops.retry",
               "command_id": "10000000-0000-4000-8000-000000000001",
               "actor": "synthetic-reviewer", "human_confirmed": True,
               "project_id": 1, "job_id": job["job_id"],
               "expected_payload_hash": mcs_requests.payload_hash(json.loads(job["payload"])),
               "additional_attempts": 2, "reason": "合成の再試行"}
    assert mcs_requests.apply_command(led, command)["outcome"] == "applied"
    job = led.db.execute("SELECT * FROM fetch_jobs WHERE kind='semantic'").fetchone()
    assert json.loads(job["payload"])["retry_command_id"] == command["command_id"]
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE job_id=?", (job["job_id"],))
    assert _run(led, cfg, llm=_request_llm, client=_FakeJev())["done"] == 1
    after = led.artifacts(semantic_loops.KIND_LOOP_REJECTED, project_id=1)
    assert [(r["artifact_id"], r["content"], r["meta"]) for r in after] == [
        (r["artifact_id"], r["content"], r["meta"]) for r in rejected]
    events = [json.loads(row["content"]) for row in led.artifacts("loop_event", project_id=1)]
    for rejection in rejected:
        item = json.loads(rejection["content"])
        assert any(event["loop_artifact_id"] == item["loop_artifact_id"]
                   and event["trigger_message_id"] == item["trigger_message_id"]
                   and event["trigger_revision"] == item["trigger_revision"] for event in events)
    assert {row["message_id"] for row in _receipts(led)} == {1, 2}
    assert _state(led, cfg, 1) == _state(led, cfg, 2) == "current"
    assert _ready(runtime, cfg)


def test_current_scope_unresolved_rejection_cannot_get_completion(runtime, monkeypatch):
    import semantic_loops
    from test_loop_relation_rejected import _RejectingJev

    class RelationRejectingJev(_RejectingJev):
        def evaluate(self, state, questions, deadline):
            if 'relation' in questions:
                return super().evaluate(state, questions, deadline)
            return _FakeJev.evaluate(self, state, questions, deadline)

    led = _seed(runtime, reply=True)
    cfg = _config()
    assert _run(led, cfg, llm=_request_llm, client=RelationRejectingJev())["done"] == 0
    rejected = led.artifacts(semantic_loops.KIND_LOOP_REJECTED, project_id=1)
    assert rejected and json.loads(rejected[-1]["meta"])["retry_scope"] == ""
    item = json.loads(rejected[-1]["content"])
    events = [json.loads(row["content"]) for row in led.artifacts("loop_event", project_id=1)]
    assert not any(event["loop_artifact_id"] == item["loop_artifact_id"]
                   and event["trigger_message_id"] == item["trigger_message_id"] for event in events)
    # Even a falsely completed loop call cannot turn this unresolved receipt
    # into a completion proof; the real manifest must inspect the history.
    monkeypatch.setattr(drain, "_promote_loops", lambda *args, **kwargs: (True, False, False, False, False))
    monkeypatch.setattr(semantic_job_processor, '_promote_loops', drain._promote_loops)
    with led.db:
        led.db.execute("UPDATE fetch_jobs SET next_try=0 WHERE kind='semantic'")
    _run(led, cfg, llm=_request_llm, client=_FakeJev())
    assert not _receipts(led)
    assert _state(led, cfg, 1) == _state(led, cfg, 2) == "held"
    assert not _ready(runtime, cfg)


def test_loop_manifest_deadline_rolls_back_and_preserves_outer_progress_handler(runtime, monkeypatch):
    from types import SimpleNamespace

    led = _seed(runtime)
    cfg = _config()
    outer_deadline = time.monotonic() + 300
    original_manifest, original_json, original_time = drain._loop_completion_manifest, drain.strict_json, drain.time
    observed, expired, progress = [], [False], []

    def manifest(*args, **kwargs):
        deadline = kwargs.get("deadline")
        assert deadline is not None and deadline <= outer_deadline
        assert led.db.in_transaction
        observed.append(deadline)
        fake_time = SimpleNamespace(**vars(original_time))
        fake_time.monotonic = lambda: deadline + 1 if expired[0] else deadline - 1

        def parse(value):
            result = original_json(value)
            expired[0] = True
            return result

        with monkeypatch.context() as scope:
            scope.setattr(semantic_store, "time", fake_time)
            scope.setattr(semantic_store, "strict_json", parse)
            return original_manifest(*args, **kwargs)

    monkeypatch.setattr(semantic_store, "_loop_completion_manifest", manifest)
    led.db.set_progress_handler(lambda: progress.append(True) and 0, 1)
    try:
        result = drain.run_due(led, cfg, {"errors": []}, outer_deadline,
                               jev_client=_FakeJev(), llm_fn=_empty_llm)
        assert observed and expired[0]
        assert result["done"] == 0
        assert not led.db.in_transaction
        assert not _receipts(led)
        job = led.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] != "done"
        before = len(progress)
        led.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        assert len(progress) > before
        assert _state(led, cfg) in ("held", "pending")
    finally:
        led.db.set_progress_handler(None, 0)


def test_loop_manifest_sqlite_interrupt_rolls_back_and_keeps_outer_handler(runtime, monkeypatch):
    led = _seed(runtime)
    cfg = _config()
    original_manifest = drain._loop_completion_manifest
    armed, interruptions, progress, manifest_calls = [False], [], [], []

    def progress_handler():
        progress.append(True)
        if armed[0] and not interruptions:
            interruptions.append(True)
            return 1  # SQLITE_INTERRUPT, exactly once on this borrowed connection
        return 0

    def manifest(*args, **kwargs):
        assert led.db.in_transaction
        assert kwargs.get("deadline") is not None
        manifest_calls.append(True)
        armed[0] = True
        return original_manifest(*args, **kwargs)

    monkeypatch.setattr(semantic_store, "_loop_completion_manifest", manifest)
    led.db.set_progress_handler(progress_handler, 1)
    try:
        result = _run(led, cfg, client=_FakeJev())
        assert manifest_calls and interruptions == [True]
        assert result["done"] == 0
        assert not led.db.in_transaction
        assert not _receipts(led)
        job = led.db.execute("SELECT state FROM fetch_jobs WHERE kind='semantic'").fetchone()
        assert job["state"] != "done"
        before = len(progress)
        led.db.execute("SELECT COUNT(*) FROM artifacts").fetchone()
        assert len(progress) > before and interruptions == [True]
        assert _state(led, cfg) in ("held", "pending")
    finally:
        led.db.set_progress_handler(None, 0)
