"""Synthetic reference validation, frozen checkpoints and worker progress."""
import extract_validation
import copy
import json

import pytest

import extract_llm as llm
import patient_state_context as state
import inference_dependency_loss as dependency_loss
from test_patient_state_context import seed, target
from test_pharmacy_state import context
from extract_testkit import _ledger, _message

BODY = "本人の合成薬Aの残薬は2錠になりました。"


def document(body=BODY, *, reference="S1", names=("合成薬A",), subject="patient", value="2錠"):
    item = context(body, values=(value,), names=names, subject=subject)
    item["details"][-1]["refers_to"] = reference
    return {"patient_context": [item], "summary": "合成の残薬報告", "urgency": "routine"}


def refs(led, row):
    return state.prepare(led, row, model=llm.MODEL, contract="synthetic/1", attempt={},
                         enabled=True, base_context=None, version=llm.EXTRACT_VERSION)


def test_known_reference_resolves_but_never_replaces_body_only_values(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        frame = refs(led, row)
        out = extract_validation.validate(document(), BODY, ctx=True, references=frame["refs"])
        detail = out["patient_context"][0]["details"][-1]
        assert detail["value"] == "2錠" and detail["refers_to"]["value"] == "3錠"
        assert detail["refers_to"]["message_id"] == 1
        lifted = extract_validation.validate(document(value="3錠"), BODY, ctx=True, references=frame["refs"])
        assert all(d["key"] != "residual_quantity" for d in lifted["patient_context"][0]["details"])
        assert lifted["_items_dropped"] >= 1
    finally:
        led.close()


@pytest.mark.parametrize("bad", ["S999", [], {}, None, True])
def test_unknown_or_malformed_reference_is_counted_without_losing_its_grounded_value(tmp_path, bad):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        out = extract_validation.validate(document(reference=bad), BODY, references=refs(led, row)["refs"])
        detail = out["patient_context"][0]["details"][-1]
        assert detail["value"] == "2錠" and "refers_to" not in detail
        assert out["_items_dropped"] == 1
    finally:
        led.close()


def test_other_drug_or_subject_cannot_borrow_a_known_reference(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        mapping = refs(led, row)["refs"]
        for body, names, subject in [(BODY.replace("合成薬A", "合成薬B"), ("合成薬B",), "patient"),
                                     (BODY, ("合成薬A",), "family")]:
            out = extract_validation.validate(document(body, names=names, subject=subject), body, references=mapping)
            assert "refers_to" not in out["patient_context"][0]["details"][-1]
            assert out["_items_dropped"] == 1
    finally:
        led.close()


def test_forged_resolved_object_and_unsupplied_reference_are_rejected(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        mapping = refs(led, row)["refs"]
        forged = {**mapping["S1"], "token": "S1", "project_id": 2}
        for doc, supplied in [(document(reference=forged), mapping), (document(), None)]:
            out = extract_validation.validate(doc, BODY, references=supplied)
            assert out["_items_dropped"] == 1
            assert "refers_to" not in out["patient_context"][0]["details"][-1]
    finally:
        led.close()


def test_resolved_reference_survives_piece_merge_and_durable_checkpoint(tmp_path):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        led.save_messages([_message(mid=999, body=BODY, posted_at=row["posted_at"])])
        row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=999").fetchone())
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        frame = refs(led, row)
        row.update(_frame=frame, _target=("", llm.MODEL))
        out = extract_validation.validate_piece(document(), BODY, BODY, references=frame["refs"])
        merged = extract_validation.merge([out])
        assert merged["patient_context"][0]["details"][-1]["refers_to"]["token"] == "S1"
        assert llm._persist_chunks(led, row, {0: out}, frame["text"])
        _, saved = llm._saved_chunk_state(led.db, row, frame["text"])
        assert saved[0]["patient_context"][0]["details"][-1]["refers_to"]["token"] == "S1"
        changed = {**row, "_frame": {**frame, "digest": "f" * 64}}
        assert llm._saved_chunk_state(led.db, changed, frame["text"])[1] == {}
    finally:
        led.close()


def test_worker_saves_frame_before_call_and_completed_progress_ignores_background_updates(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        led.save_messages([_message(mid=999, body=BODY, posted_at=row["posted_at"])])
        row = dict(led.db.execute("SELECT * FROM messages WHERE message_id=999").fetchone())
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"patient_state_context": True}})
        seen = []

        def infer(body, **kwargs):
            frames = led.artifacts(state.KIND, message_id=999)
            assert len(frames) == 1 and kwargs["references"]["S1"]["value"] == "3錠"
            seen.append(copy.deepcopy(kwargs["references"]))
            kwargs["meta_out"].update(completeness_chunks=1,
                completeness={"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}})
            return extract_validation.validate(document(), body, references=kwargs["references"])

        monkeypatch.setattr(llm, "llm_extract", infer)
        result = llm.run_pending(led, admitted_ids={999}, limit=1, budget_s=10)
        assert result["done"] == 1 and len(seen) == 1
        final = led.artifacts(llm.KIND, message_id=999)[0]
        meta = json.loads(final["meta"])
        assert meta["patient_state_context"] is True and type(meta["context_snapshot"]) is int
        assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == "complete"
        seed(led, 2, body="本人の合成薬Aの残薬は8錠。", value="8錠", date="2026-10-02T10:00:00+09:00")
        monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"patient_state_context": False}})
        assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == "complete"
    finally:
        led.close()


def test_malformed_saved_context_defers_without_any_model_call(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        target(led)
        calls = []
        monkeypatch.setattr(llm, "load_config", lambda: {})
        rows, _ = llm._select_pending(led, 1, {999}, None, False)
        jobs, _, _ = llm._prepare_jobs(led, rows, ("", llm.MODEL), False)
        assert len(jobs) == 1
        with led.db:
            led.db.execute("UPDATE artifacts SET content='not json' WHERE kind=?", (state.KIND,))
        monkeypatch.setattr(llm, "llm_extract", lambda *a, **k: calls.append(1))
        result = llm.run_pending(led, admitted_ids={999}, limit=1, budget_s=10)
        assert result["done"] == 0 and result["deferred"] == result["context_blocked"] == 1
        assert calls == []
    finally:
        led.close()


def test_interrupted_worker_resumes_the_first_snapshot_after_history_changes(tmp_path, monkeypatch):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        led.save_messages([_message(mid=999, body=BODY, posted_at=row["posted_at"])])
        monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"patient_state_context": True}})
        calls = []

        def infer(body, **kwargs):
            calls.append(copy.deepcopy(kwargs["references"]))
            return llm._DEFERRED if len(calls) == 1 else extract_validation.validate(
                document(), body, references=kwargs["references"])

        monkeypatch.setattr(llm, "llm_extract", infer)
        assert llm.run_pending(led, admitted_ids={999}, limit=1, budget_s=10)["deferred"] == 1
        seed(led, 2, body="本人の合成薬Aの残薬は8錠。", value="8錠", date="2026-10-02T10:00:00+09:00")
        assert llm.run_pending(led, admitted_ids={999}, limit=1, budget_s=10)["done"] == 1
        assert calls[0] == calls[1] and calls[1]["S1"]["value"] == "3錠"
        assert len(led.artifacts(state.KIND, message_id=999)) == 1
    finally:
        led.close()


@pytest.mark.parametrize("route", ["qc", "human", "thin"])
def test_failed_same_generation_feedback_retains_the_prior_model_and_context_origin(tmp_path, route):
    led = _ledger(tmp_path)
    try:
        seed(led)
        row = target(led)
        row["_source_binding"] = dependency_loss.source_binding(led.db, row["project_id"], row["message_id"])
        first = refs(led, row)
        r = {**row, "_frame": first, "_context": first["text"], "_target": ("", llm.MODEL)}
        content = json.dumps({"summary": "合成の元結果"})
        assert llm._replace_current(led, r, content,
                                  integrity={"validation": {"items_dropped": 0, "evidence_dropped": 0},
                                             "completeness_chunks": 1,
                                             "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}})
        second = state.prepare(led, row, model=llm.MODEL, contract="synthetic/2", attempt={},
                               enabled=False, base_context=None, version=llm.EXTRACT_VERSION)
        r = {**row, "_frame": second, "_context": None, "_target": ("", llm.MODEL)}
        kwargs = {}
        if route == "qc":
            kwargs["qc_fix"] = {"qc": 123, "applied": False}
        elif route == "human":
            kwargs["extra_meta"] = {"human_fix": {"feedback_id": 123, "applied": False}}
        else:
            r["_kept_prior"] = True
            kwargs["extra_meta"] = {"thin_retried": True}
        assert llm._replace_current(led, r, content, **kwargs)
        out = led.artifacts(llm.KIND, message_id=999)[0]
        meta = json.loads(out["meta"])
        assert out["model"] == meta["actual_model"] == llm.MODEL
        assert meta["context_snapshot"] == first["artifact_id"] and meta["patient_state_context"] is True
        assert meta["integrity"]["validation"] == {"items_dropped": 0, "evidence_dropped": 0}
        assert llm.progress_for_message(led.db, row, model=llm.MODEL)["state"] == (
            "complete" if route == "thin" else "attention")
    finally:
        led.close()


@pytest.mark.parametrize("total", [1199, 1200, 1201, 1202])
def test_context_lines_count_their_joining_newlines_within_the_budget(total):
    head = len("[投稿者] ")
    sizes = [400, 400, total - 800]
    rows = [{"message_id": i + 1, "posted_at_ts": i, "who": None, "body_text": "合" * (size - head)}
            for i, size in enumerate(sizes)]
    lines = llm.thread_context_lines(rows, 1, 1200)
    assert len("\n".join(lines)) <= 1200
    assert len(lines) == (3 if total + 2 <= 1200 else 2)


@pytest.mark.parametrize("enabled", [False, True])
def test_a_full_thread_never_blocks_preparation(tmp_path, monkeypatch, enabled):
    led = _ledger(tmp_path)
    try:
        body = "合成の経過" * 100
        led.save_messages([_message(mid=1, body=body, posted_at="2026-09-19T00:00:00+09:00")]
                          + [_message(mid=mid, body=body, parent_id=1,
                                      posted_at=f"2026-09-19T00:0{mid}:00+09:00") for mid in range(2, 6)])
        monkeypatch.setattr(llm, "load_config", lambda: {"extract": {"patient_state_context": enabled}})
        rows, _ = llm._select_pending(led, 10, {5}, None, False)
        jobs, _, blocked = llm._prepare_jobs(led, rows, llm._resolved_llm(), False)
        assert blocked == 0 and len(jobs) == 1
        assert len(jobs[0][1]) <= llm._CTX_TOTAL_MAX
    finally:
        led.close()
