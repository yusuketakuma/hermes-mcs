"""Synthetic claimed no-signal adoption, evidence preservation and atomic rollback."""
from contextlib import closing
from datetime import datetime, timezone
import json
import threading
import time

import pytest

import anthropic_client
import clinical_allergies
import cloud_inference
import cloud_policy
import extract_llm as worker
import extract_llm_context
import extract_llm_errors
import extract_llm_prepare as prepare
import extract_llm_proofs as proofs
import extract_llm_runner as runner
import extract_llm_store as store
import inference_version as inference
import semantic_qc
from extract_testkit import _ledger, _message, _qc_job, _v2_artifact


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "on")
    for module in (worker, prepare, runner, store, extract_llm_context):
        monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(store, "_rerender_cards", lambda *_args: None)


def _source(ledger, body="ありがとうございます。", *, parent_id=None, mid=1):
    posted = datetime.fromtimestamp(time.time(), timezone.utc).isoformat()
    ledger.save_messages([_message(mid, body=body, parent_id=parent_id, posted_at=posted)])


def _rows(ledger, kind):
    return [dict(row) for row in ledger.db.execute(
        "SELECT * FROM artifacts WHERE kind=? ORDER BY artifact_id", (kind,))]


def _no_transport(monkeypatch):
    def refused(*_args, **_kwargs):
        pytest.fail("no-signal adoption reached a provider or transport")
    monkeypatch.setattr(runner, "llm_extract", refused)
    monkeypatch.setattr(runner, "_llm_call", refused)
    monkeypatch.setattr(cloud_inference, "CloudInferenceUnit", refused)
    monkeypatch.setattr(worker.local_llm, "chat", refused)


def _prepare(ledger):
    rows, _ = worker._select_pending(ledger, 1, None, None, False)
    jobs, skipped, blocked = prepare._prepare_jobs(
        ledger, rows, worker._resolved_llm(), True, allow_cloud=False, load_checkpoints=False)
    assert len(jobs) == 1 and skipped == blocked == 0
    return jobs[0]


def _claim_candidate(ledger):
    row, context, _saved, hints, _qc = _prepare(ledger)
    assert "_prefilter_prior" in row
    lease = extract_llm_errors._claim(ledger, row)
    assert lease is not None
    proofs._capture_checkpoint_state(ledger.db, row, context)
    return row, hints, lease


def _checkpoint(ledger, variant="pending"):
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
            "hash": ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]}
    # Deliberately old or malformed recovery evidence must not look absent.
    meta.update(ver=worker.EXTRACT_VERSION - 1, context="retired-context", chunk_size=1,
                chunk=0, quality_status=variant)
    aid = ledger.artifact_add("extract_llm_chunk", "{}", project_id=None if variant == "null_project" else 1,
                             message_id=1, model="synthetic-retired-model", meta=meta)
    with ledger.db:
        if variant in ("malformed", "null_meta"):
            ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?",
                              ("{" if variant == "malformed" else None, aid))
        elif variant == "other_hash":
            ledger.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.hash','other') WHERE artifact_id=?", (aid,))
    return aid


def _paid(ledger, state="reserved"):
    cfg = {"cloud_llm": {"mode": "on", "project_ids": [1]}}
    body = {"model": anthropic_client.MODEL, "max_tokens": 100, "system": "synthetic system",
            "messages": [{"role": "user", "content": "synthetic request"}]}
    reservation = cloud_policy.usage_reserver(ledger, cfg, route="reextract", project_id=1,
        message_id=1, unit_key="1")(body, 2)
    if state in ("unknown", "settled"):
        cloud_policy.record_result(ledger, reservation, {
            "model": reservation["model"], "request_fp": reservation["request_fp"],
            "usage": {"input_tokens": 10, "output_tokens": 2} if state == "settled" else None,
            "http_class": "http_200" if state == "settled" else "transport",
            "request_id": "synthetic-request", "error_kind": None if state == "settled" else "transport"})
    if state == "malformed":
        with ledger.db:
            ledger.db.execute("UPDATE artifacts SET meta='{' WHERE kind='cloud_usage_reserve'")
    assert cloud_policy.owns_request(ledger.db, route="reextract", project_id=1, message_id=1)


@pytest.mark.parametrize("body", ["こんにちは。", "ありがとうございます。"])
@pytest.mark.parametrize("circuit", [False, True])
def test_clean_marker_is_claimed_provider_free_and_excluded_from_real_qc(tmp_path, monkeypatch, body, circuit):
    _no_transport(monkeypatch)
    monkeypatch.setattr(runner, "circuit_open_s", lambda _db: 30 if circuit else 0)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger, body)
        result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        assert result["skipped"] == 1 and result["done"] == result["held_prior"] == 0
        assert len(_rows(ledger, "extract_v1")) == len(_rows(ledger, worker.KIND)) == 1
        row = _rows(ledger, worker.KIND)[0]
        meta, content = json.loads(row["meta"]), json.loads(row["content"])
        assert meta["prefilter"] == "no_signal" and meta["extract_version"] == worker.EXTRACT_VERSION
        assert meta["provider_provenance"]["providers"] == []
        assert meta["provider_provenance"]["complete"] is False
        assert content["meds"] == [] and "summary" not in content
        assert not ledger.db.execute("SELECT 1 FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
        assert worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)["selected"] == 0
        # Positive QC control: the date and inference generation are eligible.
        _source(ledger, "完全合成の報告です。", mid=2)
        current_hash = ledger.db.execute("SELECT content_hash FROM messages WHERE message_id=2").fetchone()[0]
        _v2_artifact(ledger, 2, current_hash)
        assert semantic_qc._qc_seed(ledger, time.time()) == 1
        assert _qc_job(ledger, 1) is None and _qc_job(ledger, 2) is not None


@pytest.mark.parametrize("body", [
    "承知しました。卵アレルギーはありません。", "了解です。合成薬ABで発疹が出ました。",
    "ありがとうございます。体温38度です。", "ありがとうございます", "了解です。"])
def test_clinical_or_acknowledgement_text_stays_on_normal_path(body):
    assert not prepare._low_signal(body, {"v": 1})


def test_shared_allergy_cues_and_failed_hints_refuse_empty(monkeypatch):
    assert not prepare._low_signal("ありがとうございます。", None)
    assert not prepare._low_signal("ありがとうございます。", {"v": 1, "symptoms": ["synthetic"]})
    monkeypatch.setattr(clinical_allergies, "cue_spans", lambda _body: [(0, 1, "allergy")])
    assert not prepare._low_signal("ありがとうございます。", {"v": 1})


@pytest.mark.parametrize("body", ["こんにちは。", "ありがとうございます。", "承知しました。"])
def test_real_thread_reply_is_not_an_empty_candidate(tmp_path, body):
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger, "完全合成の依頼です。明日の訪問をお願いします。", mid=10)
        _source(ledger, body, parent_id=10)
        jobs, skipped, blocked = prepare._prepare_jobs(ledger,
            [dict(ledger.db.execute("SELECT m.*,NULL AS qc_src,NULL AS thin_src,NULL AS human_src "
                                   "FROM messages m WHERE message_id=1").fetchone())],
            worker._resolved_llm(), True, allow_cloud=False, load_checkpoints=False)
        assert len(jobs) == 1 and skipped == blocked == 0
        assert "_prefilter_prior" not in jobs[0][0]
        assert not _rows(ledger, worker.KIND)


@pytest.mark.parametrize("variant", ["pending", "started", "malformed", "null_meta", "null_project", "other_hash"])
def test_raw_old_or_damaged_checkpoint_is_not_hidden_by_empty(tmp_path, variant):
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        _checkpoint(ledger, variant)
        before = _rows(ledger, "extract_llm_chunk")
        row, _context, saved, _hints, _qc = _prepare(ledger)
        assert saved == {}  # production prepares without loading checkpoints
        assert "_prefilter_prior" not in row
        assert not prepare._mark_prefiltered(ledger, row)
        assert _rows(ledger, "extract_llm_chunk") == before
        assert not _rows(ledger, worker.KIND)


@pytest.mark.parametrize("state", ["reserved", "unknown", "settled", "malformed"])
def test_paid_ownership_is_not_a_prefilter_candidate(tmp_path, monkeypatch, state):
    monkeypatch.setattr(cloud_policy, "send_route", lambda _route: True)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *_args, **_kwargs: "synthetic-key")
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        _paid(ledger, state)
        before = _rows(ledger, "cloud_usage_reserve") + _rows(ledger, "cloud_usage_result")
        row, _context, _saved, _hints, _qc = _prepare(ledger)
        assert "_cloud_owned" not in row  # ownership is not this optional preparation flag
        assert "_prefilter_prior" not in row
        assert not prepare._mark_prefiltered(ledger, row)
        assert before == _rows(ledger, "cloud_usage_reserve") + _rows(ledger, "cloud_usage_result")
        assert not _rows(ledger, worker.KIND)


@pytest.mark.parametrize("mutation", ["checkpoint", "claim", "paid", "prior"])
def test_second_connection_change_after_capture_refuses_empty(tmp_path, monkeypatch, mutation):
    _no_transport(monkeypatch)
    monkeypatch.setattr(cloud_policy, "send_route", lambda _route: True)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *_args, **_kwargs: "synthetic-key")
    barriers = threading.Barrier(2, timeout=10)
    errors = []
    before = []
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        capture = runner._capture_checkpoint_state

        def writer():
            try:
                with closing(_ledger(tmp_path)) as other:
                    barriers.wait()
                    if mutation == "checkpoint":
                        _checkpoint(other, "malformed")
                    elif mutation == "claim":
                        with other.db:
                            other.db.execute("UPDATE fetch_jobs SET next_try=?,payload=? WHERE kind='extract_claim'",
                                (time.time() + 1500, json.dumps({"owner_token": "synthetic-new-owner"})))
                    elif mutation == "paid":
                        _paid(other)
                    else:
                        other.artifact_add(worker.KIND, '{"summary":"合成の新prior"}', project_id=1, message_id=1,
                            meta={inference.VERSION_KEY: inference.CURRENT_VERSION,
                                  "hash": other.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0],
                                  "extract_version": worker.EXTRACT_VERSION - 1})
                    before.extend(_rows(other, "extract_llm_chunk") + _rows(other, "cloud_usage_reserve")
                                  + _rows(other, worker.KIND))
                    barriers.wait()
            except BaseException as error:
                errors.append(error)
                barriers.abort()

        def paused_capture(*args):
            saved = capture(*args)
            barriers.wait()
            barriers.wait()
            return saved

        monkeypatch.setattr(runner, "_capture_checkpoint_state", paused_capture)
        thread = threading.Thread(target=writer)
        thread.start()
        try:
            result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        finally:
            thread.join(timeout=15)
        assert not thread.is_alive() and not errors
        assert result["skipped"] == result["done"] == 0 and result["deferred"] == 1
        assert not _rows(ledger, "extract_v1")
        assert before == (_rows(ledger, "extract_llm_chunk") + _rows(ledger, "cloud_usage_reserve")
                          + _rows(ledger, worker.KIND))
        if mutation == "claim":
            claim = ledger.db.execute("SELECT payload FROM fetch_jobs WHERE kind='extract_claim'").fetchone()
            assert json.loads(claim[0])["owner_token"] == "synthetic-new-owner"


def test_protected_prior_is_held_and_not_counted_as_skipped(tmp_path, monkeypatch):
    _no_transport(monkeypatch)
    # Fault the classifier, not the source-grounded protection contract.
    monkeypatch.setattr(prepare, "_low_signal", lambda *_args: True)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger, "本人に発熱はありません。")
        source = dict(ledger.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        prior = {"symptoms": [{"text": "発熱", "subject": "patient", "status": "ongoing", "negated": True,
                               "evidence": source["body_text"]}]}
        aid = ledger.artifact_add(worker.KIND, json.dumps(prior, ensure_ascii=False), project_id=1, message_id=1,
            meta={inference.VERSION_KEY: inference.CURRENT_VERSION, "hash": source["content_hash"],
                  "extract_version": worker.EXTRACT_VERSION - 1})
        before = dict(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone())
        result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        assert result["held_prior"] == 1 and result["skipped"] == result["done"] == 0
        assert dict(ledger.db.execute("SELECT * FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()) == before
        receipt = json.loads(_rows(ledger, worker.UPGRADE_KIND)[0]["content"])
        assert receipt["reason"] == "protected_loss" and receipt["prior_artifact_id"] == aid
        assert len(_rows(ledger, "extract_v1")) == 1
        assert not ledger.db.execute("SELECT 1 FROM messages m WHERE " + worker.pending_pred(ledger.db)).fetchone()


@pytest.mark.parametrize("failure", ["exception", "rejected", "source", "frame", "claim", "checkpoint", "paid", "prior"])
def test_auxiliary_write_rolls_back_with_failed_adoption(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(cloud_policy, "send_route", lambda _route: True)
    monkeypatch.setattr(cloud_policy, "api_key", lambda *_args, **_kwargs: "synthetic-key")
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        row, hints, lease = _claim_candidate(ledger)
        ensure = prepare._ensure_v1
        before = [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]

        def injected(*args):
            ensure(*args)
            assert _rows(ledger, "extract_v1")  # real SQL, still inside the adoption transaction
            if failure == "exception":
                raise RuntimeError("synthetic-aux-stop")
            if failure == "source":
                ledger.db.execute("UPDATE messages SET sender_name='synthetic-changed' WHERE message_id=1")
            elif failure == "frame":
                ledger.db.execute("UPDATE artifacts SET content='{}' WHERE artifact_id=?", (row["_frame"]["artifact_id"],))
            elif failure == "claim":
                ledger.db.execute("UPDATE fetch_jobs SET payload='{}' WHERE kind='extract_claim'")
            elif failure == "checkpoint":
                ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                                  "VALUES('extract_llm_chunk',1,1,'{}','synthetic','{',?)", (time.time(),))
            elif failure == "paid":
                ledger.db.execute("INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                                  "VALUES('cloud_usage_reserve',1,1,'{}','synthetic','{',?)", (time.time(),))
            elif failure == "prior":
                ledger.artifact_add_tx(worker.KIND, '{"summary":"合成の割込みprior"}', project_id=1, message_id=1,
                    meta={inference.VERSION_KEY: inference.CURRENT_VERSION, "hash": row["content_hash"],
                          "extract_version": worker.EXTRACT_VERSION - 1})

        monkeypatch.setattr(prepare, "_ensure_v1", injected)
        if failure == "rejected":
            monkeypatch.setattr(store, "_replace_current_tx", lambda *_args, **_kwargs: "rejected")
        try:
            if failure == "exception":
                with pytest.raises(RuntimeError, match="synthetic-aux-stop"):
                    prepare._settle_prefiltered(ledger, row, hints=hints)
            else:
                assert prepare._settle_prefiltered(ledger, row, hints=hints) == "rejected"
            assert before == [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
            assert not _rows(ledger, "extract_v1") and not _rows(ledger, worker.KIND)
            assert not _rows(ledger, "extract_llm_chunk") and not _rows(ledger, "cloud_usage_reserve")
            assert ledger.db.execute("SELECT sender_name FROM messages WHERE message_id=1").fetchone()[0] == "sender"
        finally:
            extract_llm_errors._release(ledger, row, lease)


def test_commit_then_interruption_does_not_repeat_empty_adoption(tmp_path, monkeypatch):
    _no_transport(monkeypatch)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        row, hints, lease = _claim_candidate(ledger)
        original = store._rerender_cards
        monkeypatch.setattr(store, "_rerender_cards", lambda *_args: (_ for _ in ()).throw(RuntimeError("after-commit")))
        try:
            with pytest.raises(RuntimeError, match="after-commit"):
                prepare._settle_prefiltered(ledger, row, hints=hints)
        finally:
            extract_llm_errors._release(ledger, row, lease)
        monkeypatch.setattr(store, "_rerender_cards", original)
        before = _rows(ledger, worker.KIND)
        result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        assert result["selected"] == 0 and _rows(ledger, worker.KIND) == before
        assert len(_rows(ledger, "extract_v1")) == 1


def test_marker_reopens_only_after_a_real_edit(tmp_path, monkeypatch):
    _no_transport(monkeypatch)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        assert worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)["skipped"] == 1
        _source(ledger, "本人に息切れがあります。")
        calls = []
        monkeypatch.setattr(runner, "llm_extract", lambda body, **_kwargs:
                            calls.append(body) or {"summary": "完全合成の結果"})
        result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        assert result["done"] == 1 and result["skipped"] == 0 and calls == ["本人に息切れがあります。"]


@pytest.mark.parametrize("mode", ["off", "manifest", "cloud_only", "thin", "human", "qc", "rebuild"])
def test_existing_explicit_exclusions_do_not_gain_an_empty_candidate(tmp_path, monkeypatch, mode):
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        if mode == "off":
            monkeypatch.setenv("MCS_EXTRACT_PREFILTER", "off")
            calls = []
            monkeypatch.setattr(runner, "llm_extract", lambda body, **_kwargs:
                                calls.append(body) or {"summary": "完全合成の結果"})
            assert worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)["skipped"] == 0
            assert calls == ["ありがとうございます。"]
            return
        rows, _ = worker._select_pending(ledger, 1, None, None, False)
        row = dict(rows[0])
        if mode == "qc":
            row["qc_src"] = 123
            monkeypatch.setattr(prepare, "_qc_feedback", lambda *_args: {"qc": 123})
        elif mode in ("thin", "human"):
            row[mode + "_src"] = 123
        elif mode == "rebuild":
            monkeypatch.setattr(prepare.local_rebuild, "approved_units", lambda _db: {(1, 1): {}})
            monkeypatch.setattr(prepare.local_rebuild, "approval", lambda *_args, **_kwargs: ("permitted", {}))
            monkeypatch.setattr(prepare.local_rebuild, "RUNNABLE", {"permitted"})
        jobs, skipped, _blocked = prepare._prepare_jobs(ledger, [row], worker._resolved_llm(),
            mode != "manifest", allow_cloud=False, cloud_only=mode == "cloud_only", load_checkpoints=False)
        assert skipped == 0 and all("_prefilter_prior" not in job[0] for job in jobs)
        assert not _rows(ledger, worker.KIND)


@pytest.mark.parametrize("failure", ["checkpoint", "paid", "claim"])
def test_final_insert_trigger_cannot_leave_empty_or_auxiliary_write(tmp_path, failure):
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        row, hints, lease = _claim_candidate(ledger)
        before = [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
        changes = {
            "checkpoint": "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                          "VALUES('extract_llm_chunk',1,1,'{}','synthetic','{',NEW.created_at);",
            "paid": "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
                    "VALUES('cloud_usage_reserve',1,1,'{}','synthetic','{',NEW.created_at);",
            "claim": "UPDATE fetch_jobs SET payload='{}' WHERE kind='extract_claim';",
        }
        with ledger.db:
            ledger.db.execute("CREATE TRIGGER synthetic_prefilter_interference AFTER INSERT ON artifacts "
                "WHEN NEW.kind='extract_llm' BEGIN " + changes[failure] + " END")
        try:
            assert prepare._settle_prefiltered(ledger, row, hints=hints) == "rejected"
            assert before == [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
            assert extract_llm_errors._owns_current_source(ledger, row, lease)
        finally:
            extract_llm_errors._release(ledger, row, lease)


@pytest.mark.parametrize("refusal", ["_resume_refused", "_checkpoint_conflict", "promotion"])
def test_shared_capture_refusal_precedes_any_auxiliary_or_empty_write(tmp_path, monkeypatch, refusal):
    _no_transport(monkeypatch)
    capture = runner._capture_checkpoint_state
    if refusal == "promotion":
        monkeypatch.setattr(prepare, "_low_signal", lambda *_args: True)

    def refused(db, row, context):
        saved = capture(db, row, context)
        if refusal == "promotion":
            saved[0] = {"summary": "合成の不完全な候補", "_resume_promote_conflict": "protected_basis_conflict"}
        else:
            row[refusal] = True
        return saved

    monkeypatch.setattr(runner, "_capture_checkpoint_state", refused)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger, "本人に発熱があります。" if refusal == "promotion" else "ありがとうございます。")
        result = worker.run_pending(ledger, limit=1, budget_s=30, allow_cloud=False)
        assert result["skipped"] == result["done"] == 0 and result["deferred"] == 1
        assert result["defer_reasons"]["quality_checkpoint_unverified"] == 1
        assert not _rows(ledger, "extract_v1") and not _rows(ledger, worker.KIND)


def test_stale_prepared_candidate_returns_current_without_repeating_auxiliary_write(tmp_path, monkeypatch):
    _no_transport(monkeypatch)
    with closing(_ledger(tmp_path)) as ledger:
        _source(ledger)
        stale, context, _saved, hints, _qc = _prepare(ledger)
        fresh, fresh_hints, first = _claim_candidate(ledger)
        try:
            assert prepare._settle_prefiltered(ledger, fresh, hints=fresh_hints) == "written"
        finally:
            extract_llm_errors._release(ledger, fresh, first)
        before = [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
        later = extract_llm_errors._claim(ledger, stale)
        assert later is not None  # local claim admission is not itself a current-result proof
        try:
            proofs._capture_checkpoint_state(ledger.db, stale, context)
            monkeypatch.setattr(prepare, "_ensure_v1", lambda *_args: pytest.fail("noop wrote auxiliary coverage"))
            assert prepare._settle_prefiltered(ledger, stale, hints=hints) == "current"
            assert before == [dict(item) for item in ledger.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]
        finally:
            extract_llm_errors._release(ledger, stale, later)
