"""Invented completeness candidates reuse one repair and the absolute caller budget."""
import copy
import json
import sqlite3
import threading
import time

import pytest

import extract_llm
from extract_testkit import _hash, _ledger, _message


OBSERVATIONS = {"symptoms": [
    {"text": "発熱", "status": "ongoing", "subject": "patient", "evidence": "発熱"},
    {"text": "咳", "status": "ongoing", "subject": "patient", "evidence": "咳"}]}
PREFIX = "本人に発熱と咳があります。"


def hints(body):
    return extract_llm._rule_hints({"body_text": body, "posted_at": "2032-06-01"})


@pytest.mark.parametrize("tail,addition", [
    ("本人の血圧120/80です。", {"vitals": {"sbp": 120, "dbp": 80}}),
    ("本人は架空薬A5mgを内服中です。", {"meds": [{
        "name": "架空薬A", "dose": "5mg", "status": "current", "subject": "patient",
        "action": "none", "negated": False, "evidence": "架空薬A5mgを内服中"}]}),
    ("本人の記録を確認をお願いします。", {"requests": [{
        "to": "不明", "kind": "request", "action": "記録確認",
        "evidence": "本人の記録を確認をお願いします"}]}),
])
def test_clean_first_candidate_recovers_a_supported_missing_hint(monkeypatch, tail, addition):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else {**OBSERVATIONS, **addition})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_llm._facts(extract_llm._validate(OBSERVATIONS, body)) <= extract_llm._facts(result)
    assert set(addition) <= result.keys()
    assert extract_llm.EXTRACT_VERSION == 7


@pytest.mark.parametrize("tail", [
    "母の血圧120/80です。", "本人の以前の血圧120/80です。",
    "母は架空薬A5mgを内服中です。", "本人は以前に架空薬A5mgを内服していました。",
    "本人は架空薬A5mgを使用していません。", "ご確認ありがとうございました。",
    "本日は情報を共有します。", "本人のCRP1.2mg/dLです。",
])
def test_weak_other_person_past_negative_or_unsupported_hints_do_not_add_a_call(monkeypatch, tail):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(OBSERVATIONS))
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1 and result["symptoms"]


@pytest.mark.parametrize("repair", [
    {"vitals": {"sbp": 120, "dbp": 80}},
    {**OBSERVATIONS, "meds": [{"name": "原文に存在しない架空薬"}]},
])
def test_optional_repair_cannot_erase_prior_facts_or_win_with_an_ungrounded_gain(monkeypatch, repair):
    body = PREFIX + "本人の血圧120/80です。"
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else repair)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert result == extract_llm._validate(OBSERVATIONS, body)


def test_optional_piece_repair_reserves_the_later_initial_call(monkeypatch):
    first = PREFIX + "本人の血圧120/80です。\n"
    body = first + "本人には頭痛があります。"
    clock, calls = [100.0], []
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])
    outputs = [OBSERVATIONS, {**OBSERVATIONS, "vitals": {"sbp": 120, "dbp": 80}},
               {"symptoms": [{"text": "頭痛", "evidence": "頭痛"}]}]

    def model(prompt, **kwargs):
        calls.append(kwargs)
        clock[0] += 10
        return copy.deepcopy(outputs[len(calls) - 1])

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body), deadline=300.0,
                                     chunk_size=len(first))
    assert [call["deadline"] for call in calls] == [300.0, 300.0 - extract_llm._MIN_CALL_S, 300.0]
    assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
    assert any(item["text"] == "頭痛" for item in result["symptoms"])


def test_pending_quality_checkpoint_reuses_the_first_inference_after_restart(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    clock, calls = [100.0], []
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])

    def model(prompt, **kwargs):
        calls.append(prompt)
        clock[0] += 20
        return copy.deepcopy(OBSERVATIONS if len(calls) == 1 else {
            **OBSERVATIONS, "vitals": {"sbp": 120, "dbp": 80}})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        first = extract_llm.run_pending(db, budget_s=80)
        assert first["done"] == 0 and first["deferred"] == 1
        assert len(calls) == 1 and not db.artifacts("extract_llm")
        assert db.artifacts("extract_llm_chunk")
        db.close()
        db = _ledger(tmp_path)
        resumed = extract_llm.run_pending(db, budget_s=300)
        assert resumed["done"] == 1 and len(calls) == 2
        result = json.loads(db.artifacts("extract_llm")[0]["content"])
        assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
        assert not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_clean_batch_candidate_is_repaired_without_repeating_first_inference(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in (1, 2)])
    calls = []

    def model(prompt, **kwargs):
        calls.append(kwargs.get("schema"))
        if kwargs.get("schema") is extract_llm._SCHEMA_BATCH:
            return {"items": [{"i": i, **copy.deepcopy(OBSERVATIONS)} for i in range(2)]}
        return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        result = extract_llm.run_pending(db, budget_s=300, batch_k=2)
        assert result["done"] == 2
        assert len(calls) == 3 and calls[0] is extract_llm._SCHEMA_BATCH
        assert all(json.loads(row["content"])["vitals"] == {"sbp": 120.0, "dbp": 80.0}
                   for row in db.artifacts("extract_llm"))
    finally:
        db.close()


def test_parallel_optional_dispatch_waits_for_its_durable_first_candidate(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body="合成" + label + "。" + body)
                      for i, label in ((1, "A"), (2, "B"))])
    path = db.db.execute("PRAGMA database_list").fetchone()[2]
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    checks = []

    def model(prompt, **kwargs):
        if "未収載" not in prompt:
            return copy.deepcopy(OBSERVATIONS)
        mid = 1 if "合成A" in prompt else 2
        with sqlite3.connect(path) as reader:
            rows = reader.execute("SELECT meta FROM artifacts WHERE kind='extract_llm_chunk' "
                                  "AND message_id=?", (mid,)).fetchall()
        assert rows, "optional inference started before the first candidate was durable"
        assert any(json.loads(row[0]).get("quality_status") == "started" for row in rows)
        checks.append(True)
        return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        result = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert result["done"] == 2 and checks == [True, True]
        assert not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_answer_returned_after_absolute_deadline_cannot_finish_the_message(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: clock[0])

    def late(*args, **kwargs):
        clock[0] = 12.0
        return {"summary": "完全合成の遅れた回答"}

    monkeypatch.setattr(extract_llm, "_llm_call", late)
    assert extract_llm.llm_extract("完全合成の通常連絡。", deadline=11.0) is extract_llm._DEFERRED


@pytest.mark.parametrize("tail,addition", [
    ("母の血圧120/80です。本人の血圧120/80です。", {"vitals": {"sbp": 120, "dbp": 80}}),
    ("母は架空薬A5mgを内服中です。本人は架空薬A5mgを内服中です。", {"meds": [{
        "name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
        "status": "current", "subject": "patient", "evidence": "本人は架空薬A5mgを内服中です"}]}),
])
def test_repeated_family_and_patient_surface_still_finds_the_current_patient_candidate(monkeypatch, tail, addition):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(
                            OBSERVATIONS if len(calls) == 1 else {**OBSERVATIONS, **addition}))
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2 and set(addition) <= result.keys()


@pytest.mark.parametrize("interrupted", [False, True])
def test_unresolved_or_unknown_optional_dispatch_never_reports_complete_or_retries_automatically(
        tmp_path, monkeypatch, interrupted):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        if interrupted and len(calls) == 2:
            raise KeyboardInterrupt("synthetic interruption after optional dispatch reservation")
        return copy.deepcopy(OBSERVATIONS)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        if interrupted:
            with pytest.raises(KeyboardInterrupt):
                extract_llm.run_pending(db, budget_s=300)
            assert not db.artifacts("extract_llm")
        result = extract_llm.run_pending(db, budget_s=300)
        assert result["done"] == 1 and len(calls) == 2
        row = db.artifacts("extract_llm")[0]
        integrity = json.loads(row["meta"])["integrity"]
        statuses = {entry["state"] for entry in integrity["completeness"].values()}
        assert statuses == {"dispatch_unknown" if interrupted else "unresolved"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
        assert extract_llm.run_pending(db, budget_s=300)["selected"] == 0 and len(calls) == 2
    finally:
        db.close()


@pytest.mark.parametrize("workers", [1, 2])
def test_lost_source_or_lease_never_authorizes_the_optional_dispatch(tmp_path, monkeypatch, workers):
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in range(1, workers + 1)])
    calls = []
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    monkeypatch.setattr(extract_llm, "_owns_current_source", lambda *args: False)
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy(OBSERVATIONS))
    try:
        result = extract_llm.run_pending(db, budget_s=300, workers=workers)
        assert len(calls) == workers and result["done"] == 0
        assert result["deferred"] == workers
        assert not db.artifacts("extract_llm") and not db.artifacts("extract_llm_chunk")
    finally:
        db.close()


def test_parallel_cancellation_releases_checkpoint_waiters_before_executor_shutdown(tmp_path, monkeypatch):
    from concurrent import futures

    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body=body) for i in (1, 2)])
    first = threading.Event()
    calls, lock = [], threading.Lock()
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")

    def model(*args, **kwargs):
        with lock:
            calls.append(args)
            if len(calls) == 2:
                first.set()
        return copy.deepcopy(OBSERVATIONS)

    def cancel(*args, **kwargs):
        assert first.wait(5)
        raise KeyboardInterrupt("synthetic parent cancellation before checkpoint flush")

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    monkeypatch.setattr(futures, "wait", cancel)
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            extract_llm.run_pending(db, budget_s=300, workers=2)
        assert time.monotonic() - started < 10 and len(calls) == 2
        assert not db.artifacts("extract_llm")
        assert db.db.execute("SELECT count(*) FROM fetch_jobs WHERE kind='extract_claim'").fetchone()[0] == 0
    finally:
        db.close()


@pytest.mark.parametrize("earlier,subject,status", [
    ("母は架空薬A5mgを内服中です。", "family", "current"),
    ("本人は以前に架空薬A5mgを内服していました。", "patient", "past"),
    ("母は架空薬A5mgを内服中です。", "patient", "current"),
], ids=["family", "past", "source_family_mislabeled_patient"])
def test_same_name_other_person_or_past_fact_cannot_hide_a_current_patient_omission(
        monkeypatch, earlier, subject, status):
    current = "本人は架空薬A5mgを内服中です。"
    body = PREFIX + earlier + current
    previous = {"name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
                "subject": subject, "status": status, "evidence": earlier}
    patient = {**previous, "subject": "patient", "status": "current", "evidence": current}
    first = {**OBSERVATIONS, "meds": [previous]}
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy(first if len(calls) == 1 else {**OBSERVATIONS, "meds": [previous, patient]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_llm._facts(extract_llm._validate(first, body)) <= extract_llm._facts(result)
    if subject == "patient" and status == "current":
        # Only the owned evidence improved: predicate count is unchanged.
        assert extract_llm._facts(result) == extract_llm._facts(extract_llm._validate(first, body))
    else:
        assert len(result["meds"]) == 2
    assert any(med["evidence"] == current and med["subject"] == "patient"
               and med["status"] == "current" for med in result["meds"])


@pytest.mark.parametrize("current", [
    "本人は架空薬A50mgを内服中です。", "本人は架空薬AB5mgを内服中です。",
    "本人は架空薬A5gttを使用しています。", "本人は架空薬A5mgttを使用しています。",
    "本人は架空薬A5mg/日を内服中です。",
    "本人は架空薬A5mgを使用していません。",
])
def test_equal_predicate_cannot_gain_patient_evidence_from_a_different_drug_dose_scope_or_denial(current):
    earlier = "母は架空薬A5mgを内服中です。"
    body = PREFIX + earlier + current
    previous = {"name": "架空薬A", "dose": "5mg", "action": "none", "negated": False,
                "subject": "patient", "status": "current", "evidence": earlier}
    candidate = {**previous, "evidence": current}
    prior = extract_llm._validate({**OBSERVATIONS, "meds": [previous]}, body)
    repaired = extract_llm._validate({**OBSERVATIONS, "meds": [previous, candidate]}, body)
    assert extract_llm._facts(prior) == extract_llm._facts(repaired)
    assert not extract_llm._quality_improves(repaired, prior, body)


def test_same_budget_resumes_optional_work_without_repeating_or_blocking_later_initials(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    pieces = [f"本人に合成{tag}の発熱と咳があります。本人の血圧{sbp}/{dbp}です。\n"
              for tag, sbp, dbp in (("A", 120, 80), ("B", 125, 85), ("C", 130, 90))]
    assert len({len(piece) for piece in pieces}) == 1
    body = "".join(pieces)
    db.save_messages([_message(body=body)])
    monkeypatch.setattr(extract_llm, "_CHUNK_SIZE", len(pieces[0]))
    # Stored message bodies omit the final newline; interior chunk boundaries stay intact.
    pieces[-1] = pieces[-1].rstrip("\n")
    assert db.db.execute("SELECT body_text FROM messages WHERE message_id=1").fetchone()[0] == "".join(pieces)
    calls = []

    def model(prompt, **kwargs):
        piece = prompt.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
        repair = "未収載" in prompt
        calls.append((piece, repair))
        symptoms = [{"text": text, "evidence": piece} for text in ("発熱", "咳")]
        vitals = {"sbp": 120 + 5 * pieces.index(piece), "dbp": 80 + 5 * pieces.index(piece)}
        return {"symptoms": symptoms, **({"vitals": vitals} if repair else {})}

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        first = extract_llm.run_pending(db, budget_s=200)
        assert first["done"] == 0 and first["deferred"] == 1
        assert [piece for piece, repair in calls if not repair] == pieces
        second = extract_llm.run_pending(db, budget_s=200)
        assert second["done"] == 1
        assert [piece for piece, repair in calls if not repair] == pieces
        assert all(sum(found == piece and repair for found, repair in calls) == 1 for piece in pieces)
        assert not db.artifacts("extract_llm_chunk")
        assert extract_llm.run_pending(db, budget_s=200)["selected"] == 0
    finally:
        db.close()


def test_source_digest_change_does_not_requeue_completed_legacy_checkpoints_or_final_results(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    body = "本人の血圧120/80です。"
    db.save_messages([_message(body=body)])
    source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
    try:
        # Historical completed leaves have no quality tag, even when empty.
        extract_llm._persist_chunks(db, source, {0: {}})
        before_binding = extract_llm._chunk_context(source, None, checkpoint=True)
        monkeypatch.setattr(extract_llm, "_LOADED_SOURCE_DIGESTS", {
            **extract_llm._LOADED_SOURCE_DIGESTS, "clinical_values": "0" * 64})
        assert extract_llm._chunk_context(source, None, checkpoint=True) == before_binding
        assert extract_llm._saved_chunk_state(db, source)[1] == {0: {}}
        monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                            pytest.fail("old completed checkpoint was re-inferred"))
        assert extract_llm.run_pending(db, budget_s=200)["done"] == 1
        assert extract_llm._current(db, 1, _hash(db))
        assert db.db.execute("SELECT message_id FROM messages m WHERE " + extract_llm.pending_pred()).fetchall() == []
        assert extract_llm.run_pending(db, budget_s=200)["selected"] == 0
    finally:
        db.close()


def test_completeness_repair_can_raise_urgency_only_with_current_patient_source_evidence(monkeypatch):
    quote = "本人が意識を失っています"
    body = PREFIX + "本人の血圧120/80です。" + quote + "。"
    assert quote in body
    for initial_urgency in ("routine", "unclear"):
        first = {**OBSERVATIONS, "urgency": initial_urgency}
        repair = {**first, "vitals": {"sbp": 120, "dbp": 80},
                  "urgency": "high", "urgency_evidence": [quote]}
        prior = extract_llm._validate(copy.deepcopy(first), body)
        candidate = extract_llm._validate(copy.deepcopy(repair), body)
        assert prior["urgency"] == initial_urgency
        assert candidate["urgency"] == "high" and candidate["urgency_evidence"] == [quote]
        assert extract_llm._quality_improves(candidate, prior, body)
        calls = []
        monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                            calls.append(args) or copy.deepcopy(first if len(calls) == 1 else repair))
        result = extract_llm.llm_extract(body, hints=hints(body))
        assert len(calls) == 2
        assert result["urgency"] == "high" and result["urgency_evidence"] == [quote]
        assert result["vitals"] == {"sbp": 120.0, "dbp": 80.0}
        assert extract_llm._facts(prior) < extract_llm._facts(result)


def test_completeness_repair_cannot_raise_urgency_from_a_hint_without_source_evidence(monkeypatch):
    quote = "本人が意識を失っています"
    body = PREFIX + "本人の血圧120/80です。"
    assert quote not in body
    misleading_hints = {**hints(body), "requests": [{"kind": "contact", "ctx": quote}]}
    for initial_urgency in ("routine", "unclear"):
        first = {**OBSERVATIONS, "urgency": initial_urgency}
        repair = {**first, "vitals": {"sbp": 120, "dbp": 80},
                  "urgency": "high", "urgency_evidence": [quote]}
        prior = extract_llm._validate(copy.deepcopy(first), body)
        candidate = extract_llm._validate(copy.deepcopy(repair), body)
        assert candidate["urgency"] == "unclear" and "urgency_evidence" not in candidate
        assert candidate["_evidence_dropped"] == 1
        assert not extract_llm._quality_improves(candidate, prior, body)
        calls = []
        monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                            calls.append(args) or copy.deepcopy(first if len(calls) == 1 else repair))
        result = extract_llm.llm_extract(body, hints=misleading_hints)
        assert len(calls) == 2 and quote in calls[1][0]
        assert result == prior


@pytest.mark.parametrize("meta", [None, [], "synthetic", 1, False])
def test_non_object_final_metadata_remains_attention_read_only(tmp_path, meta):
    db = _ledger(tmp_path)
    try:
        db.save_messages([_message(mid=1, body=PREFIX)])
        source = dict(db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone())
        db.db.execute(
            "INSERT INTO artifacts(kind,project_id,message_id,content,model,meta,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (extract_llm.KIND, source["project_id"], 1,
                                     "{}", extract_llm.MODEL, json.dumps(meta), 1))
        db.db.commit()
        db.db.execute("PRAGMA query_only=ON")
        result = extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)
        assert result["state"] == "attention"
        assert result["completed"] == 0 and result["total"] == 1
    finally:
        db.close()


def test_unsent_start_saved_past_the_deadline_is_released_for_the_next_run(tmp_path, monkeypatch):
    """A start persisted just before its acknowledgement wait expires was never
    dispatched: the run must leave it pending so the next run repairs it."""
    db = _ledger(tmp_path)
    body = PREFIX + "本人の血圧120/80です。"
    db.save_messages([_message(mid=i, body="合成" + label + "。" + body)
                      for i, label in ((1, "A"), (2, "B"))])
    monkeypatch.setattr(extract_llm, "_probe_format", lambda **kwargs: "plain")
    real_clock, offset = time.monotonic, [0.0]
    monkeypatch.setattr(extract_llm.time, "monotonic", lambda: real_clock() + offset[0])
    real_persist = extract_llm._persist_chunks
    started = set()

    def persist(ledger, r, chunks, *a, **k):
        real_persist(ledger, r, chunks, *a, **k)
        if any(isinstance(v, dict) and v.get("_quality_status") == "started"
               for v in chunks.values()):
            started.add(r["message_id"])
            offset[0] = 10_000.0            # saved, then the ack wait hits the deadline
    monkeypatch.setattr(extract_llm, "_persist_chunks", persist)
    repairs = []

    def model(prompt, **kwargs):
        if "未収載" in prompt:
            repairs.append(prompt)
            return {**copy.deepcopy(OBSERVATIONS), "vitals": {"sbp": 120, "dbp": 80}}
        return copy.deepcopy(OBSERVATIONS)

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    try:
        first = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert first["done"] == 0 and not repairs
        newest = {}
        for mid, meta in db.db.execute("SELECT message_id,meta FROM artifacts "
                                       "WHERE kind='extract_llm_chunk' ORDER BY artifact_id"):
            newest[mid] = json.loads(meta).get("quality_status")
        # every start that was saved but never dispatched is released;
        # the newest checkpoint is what the next run resumes
        assert started and all(newest.get(mid) == "pending" for mid in started)
        offset[0] = 0.0
        monkeypatch.setattr(extract_llm, "_persist_chunks", real_persist)
        second = extract_llm.run_pending(db, budget_s=300, workers=2)
        assert second["done"] == 2 and len(repairs) == 2
    finally:
        db.close()


CONFIRM = {"to": "不明", "kind": "request", "action": "記録確認",
           "evidence": "本人の記録を確認をお願いします"}
CONTACT = {"to": "医師", "kind": "request", "action": "主治医へ連絡",
           "evidence": "主治医への連絡をお願いします"}
BOTH = "本人の記録の確認と主治医への連絡をお願いします"


@pytest.mark.parametrize("tail,first,repaired", [
    ("本人の記録を確認をお願いします。主治医への連絡をお願いします。", [CONFIRM], [CONFIRM, CONTACT]),
    (BOTH + "。", [{**CONFIRM, "evidence": BOTH}],
     [{**CONFIRM, "evidence": BOTH}, {**CONTACT, "evidence": BOTH}]),
], ids=["second_sentence_missing", "one_quote_two_requests"])
def test_one_extracted_request_does_not_hide_another_missing_request(monkeypatch, tail, first, repaired):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "主治医へ連絡"}


@pytest.mark.parametrize("requests", [
    [CONFIRM, CONTACT],
    [{**CONFIRM, "action": "記録をチェック"}, CONTACT],     # paraphrase of a single-request quote
], ids=["all_present", "paraphrased_single_request"])
def test_covered_requests_do_not_add_a_call(monkeypatch, requests):
    body = PREFIX + "本人の記録を確認をお願いします。主治医への連絡をお願いします。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


SECOND_CONFIRM = {"to": "不明", "kind": "request", "action": "検査値確認",
                  "evidence": "本人の検査値も確認してください"}


@pytest.mark.parametrize("tail,first,repaired", [
    # the rule hints name only the contact of a two-request sentence
    (BOTH + "。", [{**CONTACT, "evidence": BOTH}],
     [{**CONFIRM, "evidence": BOTH}, {**CONTACT, "evidence": BOTH}]),
    # the rule hints keep one confirm; the second sentence's confirm is tracked too
    ("本人の記録を確認をお願いします。本人の検査値も確認してください。", [CONFIRM],
     [CONFIRM, SECOND_CONFIRM]),
    # the rule hints keep one ask; a second sentence's question is tracked too
    ("本人の食事量を教えてください。本人の体温も教えてください。",
     [{"to": "不明", "kind": "question", "action": "食事量の確認",
       "evidence": "本人の食事量を教えてください"}],
     [{"to": "不明", "kind": "question", "action": "食事量の確認",
       "evidence": "本人の食事量を教えてください"},
      {"to": "不明", "kind": "question", "action": "体温の確認",
       "evidence": "本人の体温も教えてください"}]),
], ids=["confirm_missing_beside_contact", "second_sentence_same_kind", "second_question"])
def test_request_cues_cover_what_the_rule_hints_merge(monkeypatch, tail, first, repaired):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {item["action"] for item in repaired}


@pytest.mark.parametrize("tail", [
    "本人の連絡記録を確認してください。",           # 連絡 inside a noun is not a contact request
    "主治医から本人の件で連絡をいただきました。",   # a received contact is a report
])
def test_request_word_inside_a_noun_or_report_adds_no_call(monkeypatch, tail):
    body = PREFIX + tail
    confirm = [{"to": "不明", "kind": "request", "action": "連絡記録確認",
                "evidence": "本人の連絡記録を確認してください"}] if "記録" in tail else []
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": confirm})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


LATER_QUESTION = {"to": "不明", "kind": "question", "action": "体温の確認",
                  "evidence": "本人の体温を教えてください"}
LATER_CONTACT = {"to": "医師", "kind": "request", "action": "主治医へ連絡",
                 "evidence": "主治医へ連絡してください"}


@pytest.mark.parametrize("tail,question", [
    ("主治医から本人の件で連絡をいただきましたが、本人の体温を教えてください。", LATER_QUESTION),
    ("確認と連絡を終えたので、本人の体温を教えてください。", LATER_QUESTION),
    ("本人の記録の確認と記録係への連絡を終えたので、主治医へ連絡してください。", LATER_CONTACT),
], ids=["received_contact_then_question", "finished_listing_then_question",
        "finished_listing_then_contact"])
def test_a_later_request_ending_does_not_turn_a_report_into_a_request(monkeypatch, tail, question):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": [question]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


TWO_CONFIRMS = "本人の記録の確認と検査値の確認をお願いします"
RECORD = {"to": "不明", "kind": "request", "action": "記録確認", "evidence": TWO_CONFIRMS}
LAB = {"to": "不明", "kind": "request", "action": "検査値確認", "evidence": TWO_CONFIRMS}


@pytest.mark.parametrize("first", [[RECORD], [LAB]], ids=["lab_missing", "record_missing"])
def test_one_quote_and_one_stem_do_not_cover_two_targets_of_one_kind(monkeypatch, first):
    body = PREFIX + TWO_CONFIRMS + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else [RECORD, LAB]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "検査値確認"}


@pytest.mark.parametrize("requests", [
    [RECORD, LAB],
    [{**RECORD, "action": "記録と検査値の確認"}],     # one action names both targets
], ids=["both_present", "one_action_both_targets"])
def test_targets_named_by_the_extraction_add_no_call(monkeypatch, requests):
    body = PREFIX + TWO_CONFIRMS + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1
