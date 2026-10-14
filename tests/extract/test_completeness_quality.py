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


def _confirm(action, quote):
    return {"to": "不明", "kind": "request", "action": action, "evidence": quote}


HEEL = "本人のかかとの確認と検査値の確認をお願いします"
NURSING = "本人の記録の確認と看護記録の確認をお願いします"


@pytest.mark.parametrize("sentence,first,repaired", [
    (HEEL, [_confirm("検査値確認", HEEL)], [_confirm("かかと確認", HEEL), _confirm("検査値確認", HEEL)]),
    (NURSING, [_confirm("看護記録確認", NURSING)],
     [_confirm("記録確認", NURSING), _confirm("看護記録確認", NURSING)]),
], ids=["target_word_contains_to", "target_inside_a_longer_target"])
def test_target_boundaries_do_not_hide_a_missing_request(monkeypatch, sentence, first, repaired):
    body = PREFIX + sentence + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": first if len(calls) == 1 else repaired})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {item["action"] for item in repaired}


@pytest.mark.parametrize("sentence,requests", [
    (HEEL, [_confirm("かかと確認", HEEL), _confirm("検査値確認", HEEL)]),
    (NURSING, [_confirm("記録確認", NURSING), _confirm("看護記録確認", NURSING)]),
    (NURSING, [_confirm("記録と看護記録の確認", NURSING)]),
], ids=["heel_both", "nursing_both", "nursing_one_action_both"])
def test_named_targets_with_boundaries_add_no_call(monkeypatch, sentence, requests):
    body = PREFIX + sentence + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


THREE = "本人の記録の確認と検査値の確認と体温の確認をお願いします"
THREE_ITEMS = [_confirm("記録確認", THREE), _confirm("検査値確認", THREE), _confirm("体温確認", THREE)]


def test_the_first_of_three_listed_requests_is_tracked(monkeypatch):
    body = PREFIX + THREE + "。"
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": THREE_ITEMS[1:] if len(calls) == 1 else THREE_ITEMS})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"記録確認", "検査値確認", "体温確認"}


@pytest.mark.parametrize("tail,requests", [
    (THREE + "。", THREE_ITEMS),
    ("本人の記録の確認と検査値の確認と体温の確認を終えたので、主治医へ連絡してください。", [LATER_CONTACT]),
], ids=["three_present", "three_listed_then_report"])
def test_three_listed_requests_add_no_call_when_named_or_reported(monkeypatch, tail, requests):
    body = PREFIX + tail
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


BP_BS = "血圧の記録の確認と血糖の記録の確認をお願いします"


def _run(monkeypatch, body, first, repaired=None):
    calls = []

    def model(prompt, **kwargs):
        calls.append(prompt)
        requests = first if len(calls) == 1 or repaired is None else repaired
        return copy.deepcopy({**OBSERVATIONS, "requests": requests})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    return calls, extract_llm.llm_extract(body, hints=hints(body))


@pytest.mark.parametrize("first", [[_confirm("血糖記録確認", BP_BS)], [_confirm("血圧記録確認", BP_BS)]],
                         ids=["bp_missing", "bs_missing"])
def test_compound_targets_sharing_a_last_word_stay_distinct(monkeypatch, first):
    both = [_confirm("血圧記録確認", BP_BS), _confirm("血糖記録確認", BP_BS)]
    calls, result = _run(monkeypatch, PREFIX + BP_BS + "。", first, both)
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"血圧記録確認", "血糖記録確認"}


def test_compound_targets_both_present_add_no_call(monkeypatch):
    calls, _ = _run(monkeypatch, PREFIX + BP_BS + "。",
                    [_confirm("血圧記録確認", BP_BS), _confirm("血糖記録確認", BP_BS)])
    assert len(calls) == 1


def test_an_unrelated_negation_does_not_hide_a_request(monkeypatch):
    sentence = "本人は転倒がないので記録を確認してください"
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], [_confirm("記録確認", sentence)])
    assert len(calls) == 2 and result["requests"]


@pytest.mark.parametrize("tail,requests", [
    ("本人の記録を確認しないでください。", []),                        # the request itself is negated
    ("本人は転倒がないので記録を確認してください。",
     [_confirm("記録確認", "本人は転倒がないので記録を確認してください")]),
    (BOTH + "。", [{"to": "医師", "kind": "request", "action": "記録をチェックして主治医に伝達",
                    "evidence": BOTH}]),                                # one paraphrase names both targets
], ids=["negated_request", "unrelated_negation_present", "paraphrase_names_both_targets"])
def test_negated_or_target_named_requests_add_no_call(monkeypatch, tail, requests):
    calls, _ = _run(monkeypatch, PREFIX + tail, requests)
    assert len(calls) == 1


def test_a_request_without_a_named_target_is_checked_without_failing(monkeypatch):
    sentence = "確認をお願いします"
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], [_confirm("確認", sentence)])
    assert len(calls) == 2 and result["requests"]


VT = "本人の体温の確認と血圧の記録の確認と血糖の記録の確認をお願いします"
VT_ALL = [_confirm("体温確認", VT), _confirm("血圧記録確認", VT), _confirm("血糖記録確認", VT)]


def test_a_unique_target_stays_short_beside_extended_ones(monkeypatch):
    calls, _ = _run(monkeypatch, PREFIX + VT + "。", VT_ALL)
    assert len(calls) == 1


def test_one_of_the_extended_targets_missing_is_repaired(monkeypatch):
    calls, result = _run(monkeypatch, PREFIX + VT + "。", [VT_ALL[0], VT_ALL[2]], VT_ALL)
    assert len(calls) == 2
    assert {item["action"] for item in result["requests"]} == {"体温確認", "血圧記録確認", "血糖記録確認"}


CROSS = "本人の記録の確認と投薬を終えたので、主治医へ連絡をお願いします"
RIGHT_NEG = "本人の記録を確認してください、転倒はありません"
LEFT_DONE = "記録は確認済みですが、主治医へ連絡してください"
CROSS_CONTACT = {"to": "医師", "kind": "request", "action": "主治医へ連絡",
                 "evidence": "主治医へ連絡をお願いします"}
LEFT_CONTACT = {"to": "医師", "kind": "request", "action": "主治医へ連絡",
                "evidence": "主治医へ連絡してください"}


@pytest.mark.parametrize("sentence,repaired", [
    (RIGHT_NEG, [_confirm("記録確認", "本人の記録を確認してください")]),
    (LEFT_DONE, [LEFT_CONTACT]),
], ids=["negation_after_the_request", "done_report_before_the_request"])
def test_a_negation_in_another_clause_does_not_hide_a_request(monkeypatch, sentence, repaired):
    calls, result = _run(monkeypatch, PREFIX + sentence + "。", [], repaired)
    assert len(calls) == 2 and result["requests"]


@pytest.mark.parametrize("sentence,requests", [
    (CROSS, [CROSS_CONTACT]),                       # 確認 is not chained across ので、
    (RIGHT_NEG, [_confirm("記録確認", "本人の記録を確認してください")]),
    (LEFT_DONE, [LEFT_CONTACT]),
], ids=["listing_does_not_cross_a_clause", "right_negation_present", "left_done_present"])
def test_clause_bounded_requests_add_no_call(monkeypatch, sentence, requests):
    calls, _ = _run(monkeypatch, PREFIX + sentence + "。", requests)
    assert len(calls) == 1


STOP = "本人の架空薬Aを中止しました"
STOPPED = {"name": "架空薬A", "dose": None, "action": "stop", "status": "past",
           "subject": "patient", "negated": False, "evidence": STOP}
NAMED = {**STOPPED, "action": "none", "status": "current"}


TWO_STARTS = "本人は架空薬A5mgを開始し、架空薬B10mgも開始しました"
A_START = {**STOPPED, "dose": "5mg", "action": "start", "status": "current", "evidence": "架空薬A5mgを開始し"}
B_START = {**A_START, "name": "架空薬B", "dose": "10mg", "evidence": "架空薬B10mgも開始しました"}


@pytest.mark.parametrize("first", [[], [NAMED], [{**STOPPED, "action": "start"}]],
                         ids=["missing", "name_only", "other_action"])
def test_a_rule_change_word_needs_a_matching_med_action_not_just_the_name(monkeypatch, first):
    body = PREFIX + STOP + "。"
    calls = []

    def model(*args, **kwargs):
        calls.append(args)
        return copy.deepcopy({**OBSERVATIONS, "meds": first if len(calls) == 1 else [*first, STOPPED]})

    monkeypatch.setattr(extract_llm, "_llm_call", model)
    result = extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 2
    assert extract_llm._facts(extract_llm._validate({**OBSERVATIONS, "meds": first}, body)) <= extract_llm._facts(result)
    assert any(med["action"] == "stop" and med["evidence"] == STOP for med in result["meds"])


@pytest.mark.parametrize("tail,meds", [
    (STOP + "。", [STOPPED]),
    ("本人の架空薬Aを減量しました。", [{**STOPPED, "action": "decrease", "status": "current",
                                    "evidence": "本人の架空薬Aを減量しました"}]),
    ("本人の架空薬Aを中止し、架空薬Bを開始しました。", [
        {**STOPPED, "evidence": "架空薬Aを中止し"},
        {**STOPPED, "name": "架空薬B", "action": "start", "status": "current", "evidence": "架空薬Bを開始しました"}]),
    ("母の架空薬Aを中止しました。", []), ("母の現在の架空薬Aを中止しました。", []),
    ("本人は以前に架空薬Aを中止しました。", []),
    ("本人の架空薬Aは中止しません。", []),
    ("本人の架空薬Aの中止を検討します。", []), ("本人の架空薬Aは変更なしです。", []),
])
def test_matched_other_person_past_planned_or_denied_changes_add_no_call(monkeypatch, tail, meds):
    body = PREFIX + tail
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy({**OBSERVATIONS, "meds": meds}))
    extract_llm.llm_extract(body, hints=hints(body))
    assert len(calls) == 1


def _final_meta(db):
    row = db.db.execute("SELECT artifact_id, meta FROM artifacts WHERE kind=? "
                        "ORDER BY artifact_id DESC LIMIT 1", (extract_llm.KIND,)).fetchone()
    return row["artifact_id"], json.loads(row["meta"])


def test_an_unrepaired_change_word_leaves_the_message_in_attention(tmp_path, monkeypatch):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX + STOP + "。")])
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs:
                        calls.append(args) or copy.deepcopy({**OBSERVATIONS, "meds": [NAMED]}))
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1 and len(calls) == 2
        states = {e["state"] for e in _final_meta(db)[1]["integrity"]["completeness"].values()}
        assert states == {"unresolved"}
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
    finally:
        db.close()


@pytest.mark.parametrize("integrity,state", [
    ("absent", "complete"), ({}, "complete"), (None, "attention"),
    # the writer always records the remaining count; resolved means exactly integer 0
    ({"completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}, "complete"),
    ({"completeness": {"0": {"state": "hint_gaps_resolved"}}}, "attention"),
    ({"completeness": None}, "attention"), ({"completeness": []}, "attention"),
    ({"completeness": "hint_gaps_resolved"}, "attention"),
    ({"completeness": {"0": "hint_gaps_resolved"}}, "attention"),
    ([], "attention"), ("synthetic", "attention"),
], ids=["absent_legacy", "legacy_empty", "integrity_null", "resolved", "resolved_without_count", "null", "list", "string",
        "string_entry", "integrity_list", "integrity_string"])
def test_present_malformed_completeness_is_never_complete(tmp_path, monkeypatch, integrity, state):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX)])
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: copy.deepcopy(OBSERVATIONS))
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1
        artifact_id, meta = _final_meta(db)
        meta.pop("integrity", None)
        if integrity != "absent":
            meta["integrity"] = integrity
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact_id))
        db.db.commit()
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == state
    finally:
        db.close()


@pytest.mark.parametrize("first,covered", [
    ([A_START, {**B_START, "action": None}], False),
    ([A_START], False),
    ([A_START, B_START], True),
], ids=["b_name_only", "b_missing", "both"])
@pytest.mark.parametrize("sentence", [
    TWO_STARTS, "本人は架空薬A5mgと架空薬B10mgを開始しました", "本人は架空薬A5mgを開始し架空薬B10mgも開始しました",
], ids=["comma", "shared_predicate", "no_comma"])
def test_one_drugs_action_does_not_cover_another_drug_with_the_same_action(first, covered, sentence):
    body = PREFIX + sentence + "。"
    first = [{**med, "evidence": sentence} if sentence != TWO_STARTS else med for med in first]
    v = extract_llm._validate({**OBSERVATIONS, "meds": first}, body)
    assert bool(extract_llm._completeness_issues(v, hints(body), body)) is not covered


@pytest.mark.parametrize("a_word,a_action,aa_action,covered", [
    ("開始", "start", None, False), ("開始", "start", "start", True), ("中止", "stop", "start", True),
], ids=["aa_name_only", "both_started", "a_stopped_aa_started"])
def test_a_shorter_drug_name_does_not_cover_a_longer_one_containing_it(a_word, a_action, aa_action, covered):
    sentence = f"本人は架空薬Aを{a_word}し、架空薬AAを開始しました"
    body = PREFIX + sentence + "。"
    meds = [{**STOPPED, "action": a_action, "status": "past" if a_action == "stop" else "current", "evidence": sentence},
            {**STOPPED, "name": "架空薬AA", "action": aa_action, "status": "current", "evidence": sentence}]
    v = extract_llm._validate({**OBSERVATIONS, "meds": meds}, body)
    assert len(v["meds"]) == 2
    assert bool(extract_llm._completeness_issues(v, hints(body), body)) is not covered


def _rx_gap(text, meds):
    return extract_llm._rx_action_gaps(extract_llm._validate({"meds": meds}, text), hints(text), text)


def _med(name, text, action="start", status="current", dose=None):
    return {"name": name, "dose": dose, "action": action, "status": status,
            "subject": "patient", "negated": False, "evidence": text}


def test_a_listed_doseless_drug_the_extraction_missed_keeps_the_gap():
    text = "本人は架空薬A5mgと架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg"), _med("架空薬B", text)])


@pytest.mark.parametrize("text", [
    "本人は副作用なしで架空薬Aを開始しました。", "発熱はないものの架空薬A5mgを開始しました。",
    "母の付き添いで本人の架空薬A5mgを開始しました。",
])
def test_a_negation_or_owner_elsewhere_in_the_clause_does_not_hide_the_action(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])


@pytest.mark.parametrize("change", [{"status": "planned"}, {"status": None}, {"subject": None},
                                    {"evidence": None}], ids=["planned", "status", "subject", "evidence"])
def test_planned_or_unverified_meds_do_not_cover_an_action(change):
    text = "本人の架空薬Aを開始しました。"
    item = {k: v for k, v in {**_med("架空薬A", text), **change}.items() if v is not None}
    assert _rx_gap(text, [item])


@pytest.mark.parametrize("text", [
    "本人の架空薬A5mgは変更無しです。", "本人の架空薬A5mgを中止せず継続します。",
    "本人の架空薬A5mgの中止は見送ります。",
])
def test_stated_non_actions_need_no_action(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


def test_a_substitution_is_covered_by_the_source_stop_and_destination_start():
    text = "本人は架空薬Aから架空薬Bへ変更しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text, action="start"), _med("架空薬B", text)])


def test_a_wrong_action_kept_beside_the_right_one_stays_unresolved():
    text = "本人の架空薬Aを中止しました。"
    assert _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])
    # a neutral current item beside the stop keeps the stopped drug current for readers
    assert _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬A", text, action="stop", status="past")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", status="past"),
                              _med("架空薬A", text, action="stop", status="past")])


def _repair(monkeypatch, text, first, repaired):
    calls = []
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: calls.append(args) or copy.deepcopy(
        {**OBSERVATIONS, "meds": first if len(calls) == 1 else repaired}))
    return calls, extract_llm.llm_extract(PREFIX + text, hints=hints(PREFIX + text))


@pytest.mark.parametrize("text,old,new", [
    ("本人の架空薬Aを中止しました。", ("start", "current"), ("stop", "past")),
    ("本人の架空薬Aを開始しました。", ("none", "current"), ("start", "current")),
], ids=["wrong_action", "neutral_mention"])
def test_a_repair_may_replace_the_action_the_source_contradicts(monkeypatch, text, old, new):
    calls, result = _repair(monkeypatch, text, [_med("架空薬A", text, action=old[0], status=old[1])],
                            [_med("架空薬A", text, action=new[0], status=new[1])])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [new]
    assert not extract_llm._completeness_issues(result, hints(PREFIX + text), PREFIX + text)


@pytest.mark.parametrize("text,repaired", [
    # another field changed with the action
    ("本人の架空薬Aを中止しました。", [_med("架空薬A", "本人の架空薬Aを中止しました。", action="stop", status="past", dose="5mg")]),
    # the source states two changes for the drug: no single replacement
    ("本人の架空薬Aを中止しましたが、架空薬Aを開始しました。",
     [_med("架空薬A", "本人の架空薬Aを中止しましたが、架空薬Aを開始しました。", action="stop", status="past")]),
    # a different drug's fact cannot be dropped
    ("本人の架空薬Aを中止しました。", [_med("架空薬B", "本人の架空薬Aを中止しました。", action="stop", status="past")]),
], ids=["dose_changed", "two_changes", "other_drug"])
def test_a_repair_cannot_drop_other_facts_or_ambiguous_actions(monkeypatch, text, repaired):
    first = [_med("架空薬A", text)]
    calls, result = _repair(monkeypatch, text, first, repaired)
    assert len(calls) == 2
    assert extract_llm._facts(extract_llm._validate({**OBSERVATIONS, "meds": first}, PREFIX + text)) <= extract_llm._facts(result)


def test_starting_then_stopping_one_drug_under_a_shared_quote_is_no_contradiction():
    text = "本人は架空薬Aを開始しましたが、架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text)])


AB = "本人の架空薬Aを中止し、架空薬Bを開始しました。"


def test_another_drugs_action_does_not_excuse_or_block_one_drugs_correction(monkeypatch):
    b_start = _med("架空薬B", AB)
    # A's wrong start beside its right stop stays a contradiction despite B's start
    assert _rx_gap(AB, [_med("架空薬A", AB), _med("架空薬A", AB, action="stop", status="past"), b_start])
    # and a repair may still replace A's wrong start with stop
    calls, result = _repair(monkeypatch, AB, [_med("架空薬A", AB), b_start],
                            [_med("架空薬A", AB, action="stop", status="past"), b_start])
    assert len(calls) == 2
    assert sorted((m["name"], m["action"]) for m in result["meds"]) == [("架空薬A", "stop"), ("架空薬B", "start")]


def test_a_full_rule_action_list_cannot_prove_a_single_change():
    # v1 keeps 10 rx_actions; A's later start falls outside them
    text = ("本人の架空薬Aを中止しました。" + "".join(f"本人の架空薬C{i}を開始しました。" for i in range(9))
            + "本人の架空薬Aを開始しました。")
    body = PREFIX + text
    assert len(hints(body)["rx_actions"]) == 10
    prior = extract_llm._validate({**OBSERVATIONS, "meds": [_med("架空薬A", "本人の架空薬Aを開始しました")]}, body)
    candidate = extract_llm._validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人の架空薬Aを開始しました", action="stop", status="past")]}, body)
    assert not extract_llm._quality_improves(candidate, prior, body, hints(body))


@pytest.mark.parametrize("word", ["と共に", "と一緒に", "と同時に"])
def test_an_accompanying_drug_is_not_the_object_of_a_non_drug_change(word):
    text = f"本人は架空薬A{word}運動療法を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    # a drug started alongside it is still required
    text = f"本人は架空薬A{word}架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text)])


def test_a_spaced_dose_still_marks_a_listed_drug_the_extraction_missed():
    text = "本人は架空薬A5 mgと架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5 mg")])


@pytest.mark.parametrize("text", ["本人は架空薬Aとリハビリを開始しました。", "本人は架空薬A5mgと食事を開始しました。"])
def test_a_listed_non_medication_is_not_a_missed_drug(text):
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg" if "5mg" in text else None)])


@pytest.mark.parametrize("text,denied", [
    ("本人の架空薬Aを中止し経過観察予定です。", False),
    ("本人の架空薬Aは中止しなかった。", True), ("本人の架空薬Aは中止しませんでした。", True),
])
def test_only_negation_at_the_change_word_denies_it(text, denied):
    assert bool(_rx_gap(text, [_med("架空薬A", text, action="none")])) is not denied


def test_a_drug_under_another_predicate_is_no_object_of_the_change():
    text = "本人は架空薬Aを継続し架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="none")])


@pytest.mark.parametrize("text", ["本人は架空薬A5mgを内服中。リハビリを開始しました。",
                                  "本人は架空薬A5mgを内服中。心停止を認めました。"])
def test_a_neighbouring_sentences_drug_does_not_make_a_non_drug_change_a_medication_one(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text", ["本人は架空薬Aを架空薬Bに変更しました。", "本人は架空薬Aから架空薬Bへ変更しました。"])
def test_both_substitution_forms_accept_the_source_stop_and_destination_start(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])


def test_a_repair_cannot_erase_a_historical_action_of_the_same_drug():
    text = "本人は以前に架空薬Aを開始しました。本人の架空薬Aを中止しました。"
    body = PREFIX + text
    prior = extract_llm._validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人は以前に架空薬Aを開始しました", status="past")]}, body)
    candidate = extract_llm._validate({**OBSERVATIONS, "meds": [
        _med("架空薬A", "本人の架空薬Aを中止しました", action="stop", status="past")]}, body)
    assert not extract_llm._quality_improves(candidate, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は架空薬Aと架空薬Bと架空薬Cを開始しました。",
                                  "本人は架空薬A5mgと架空薬Bや架空薬Cを開始しました。"])
def test_several_listed_drugs_the_extraction_missed_keep_the_gap(text):
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg" if "5mg" in text else None)])
    assert not _rx_gap(text.replace("架空薬B", "リハビリ").replace("架空薬C", "食事"),
                       [_med("架空薬A", text.replace("架空薬B", "リハビリ").replace("架空薬C", "食事"),
                             dose="5mg" if "5mg" in text else None)])


@pytest.mark.parametrize("text,action", [("本人のカロナールを中止しました。", "stop"), ("本人のアムロジピンを増量しました。", "increase")])
def test_a_doseless_brand_without_a_rule_hint_still_needs_its_action(text, action):
    name = text[3:text.index("を")]
    assert not hints(PREFIX + text).get("rx_actions")
    assert _rx_gap(text, [_med(name, text, action="none")])
    assert not _rx_gap(text, [_med(name, text, action=action, status="past" if action == "stop" else "current")])


def test_comma_separated_drugs_share_the_change():
    text = "本人は架空薬A5mg、架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg"), _med("架空薬B", text)])
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg"), _med("架空薬B", text)])


@pytest.mark.parametrize("service", ["服薬指導", "投薬管理"])
def test_a_medication_service_is_not_a_missed_drug(service):
    text = f"本人は架空薬A5mgと{service}を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, dose="5mg")])


@pytest.mark.parametrize("text,action", [("本人は明日から架空薬Aを開始します。", "start"),
                                         ("本人の架空薬Aを中止してください。", "stop"),
                                         ("本人の架空薬Aの追加を相談しました。", "start"),
                                         ("本人は架空薬A5mgを開始してください。", "start")])
def test_future_requested_or_consulted_changes_are_checked_as_planned(text, action):
    planned = _med("架空薬A", text, action=action, status="planned")
    assert not _rx_gap(text, [planned])
    # an instruction needs its planned action; a consultation (追加を相談) states none
    assert bool(_rx_gap(text, [{**planned, "action": "none"}])) is ("相談" not in text)
    assert _rx_gap(text, [_med("架空薬A", text, action=action)])          # not done yet
    body = PREFIX + text
    prior = extract_llm._validate({**OBSERVATIONS, "meds": [{**planned, "action": "none"}]}, body)
    done = extract_llm._validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action=action)]}, body)
    assert not extract_llm._quality_improves(done, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は明日通院予定ですが、架空薬Aを中止しました。",
                                  "本人は来週受診予定ですが、架空薬Aを開始しました。"])
def test_a_future_time_in_another_clause_does_not_plan_this_change(text):
    action = "stop" if "中止" in text else "start"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action, status="past" if action == "stop" else "current")])


@pytest.mark.parametrize("text,action", [("本人の架空薬Aを2.5mgから5mgへ増量しました。", "increase"),
                                         ("本人の架空薬Aを1日2回から1回へ減量しました。", "decrease")])
def test_a_regimen_range_keeps_the_drug_as_the_object(text, action):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action)])


@pytest.mark.parametrize("text,action,repairable", [
    ("本人は明日から架空薬Aを開始します。", "start", True), ("本人の架空薬Aを中止してください。", "stop", True),
    ("本人の架空薬Aの追加を相談しました。", "start", False),
])
def test_a_planned_instruction_may_correct_its_planned_action_but_a_consultation_may_not(text, action, repairable):
    body = PREFIX + text
    prior = extract_llm._validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action="none", status="planned")]}, body)
    fixed = extract_llm._validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, action=action, status="planned")]}, body)
    assert extract_llm._quality_improves(fixed, prior, body, hints(body)) is repairable


def test_a_requested_stop_does_not_excuse_a_recorded_done_stop():
    text = "本人の架空薬Aを開始しました。本人の架空薬Aを中止してください。"
    right = [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="planned")]
    assert not _rx_gap(text, right)
    assert _rx_gap(text, [*right, _med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text", ["もし架空薬Aと同時に架空薬Bを開始する場合は連絡してください。",
                                  "母は架空薬Aと同時に架空薬Bを開始しました。",
                                  "本人は以前に架空薬Aと同時に架空薬Bを開始しました。"])
def test_simultaneous_start_keeps_real_conditions_other_people_and_history_out(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


def test_a_done_stop_must_be_past_and_a_repair_may_make_it_so(monkeypatch):
    text = "本人の架空薬Aを中止しました。"
    current, past = _med("架空薬A", text, action="stop"), _med("架空薬A", text, action="stop", status="past")
    assert _rx_gap(text, [current])
    assert _rx_gap(text, [current, past])            # the wrong current stop is kept beside the right one
    assert not _rx_gap(text, [past])
    calls, result = _repair(monkeypatch, text, [current], [past])
    assert len(calls) == 2 and [(m["action"], m["status"]) for m in result["meds"]] == [("stop", "past")]


def test_a_question_asks_for_no_action_and_allows_no_replacement():
    text = "本人の架空薬Aを開始しますか。"
    neutral = _med("架空薬A", text, action="none")
    assert not _rx_gap(text, [neutral])
    body = PREFIX + text
    for status in ("current", "planned"):
        prior = extract_llm._validate({**OBSERVATIONS, "meds": [{**neutral, "status": status}]}, body)
        made = extract_llm._validate({**OBSERVATIONS, "meds": [_med("架空薬A", text, status=status)]}, body)
        assert not extract_llm._quality_improves(made, prior, body, hints(body))


@pytest.mark.parametrize("text", ["本人は架空薬A1包と架空薬Bを開始しました。", "本人は架空薬A2カプセルと架空薬Bを開始しました。"])
def test_package_count_doses_still_mark_a_listed_drug_the_extraction_missed(text):
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_each_listed_drug_keeps_its_own_owner():
    text = "本人の架空薬Aと母の架空薬Bを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text)])


@pytest.mark.parametrize("text", ["本人は架空薬Aを開始後、中止しました。", "本人は架空薬Aを開始し、その後中止しました。"])
def test_an_elliptical_follow_up_change_carries_the_drug(text):
    assert _rx_gap(text, [_med("架空薬A", text)])
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text,source,other", [("本人の架空薬Aを変更しました。", "change", "increase"),
                                               ("本人の架空薬Aを増量しました。", "increase", "change"),
                                               ("本人の架空薬Aを減量しました。", "decrease", "change")])
def test_a_change_is_met_by_its_own_direction_only(text, source, other):
    assert not _rx_gap(text, [_med("架空薬A", text, action=source)])
    assert _rx_gap(text, [_med("架空薬A", text, action=other)])


@pytest.mark.parametrize("text", ["本人の中止薬を確認しました。", "本人の開始薬は架空薬Aです。", "本人の追加薬はありません。"])
def test_a_change_word_inside_a_noun_is_no_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")] if "架空薬A" in text else [])


def test_a_shorter_name_cannot_stand_for_an_unnamed_longer_drug():
    text = "本人の架空薬AAを開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_a_nearer_current_time_outweighs_an_earlier_future_word():
    text = "明日の退院に備えて本日架空薬Aを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text)])
    assert _rx_gap(text, [_med("架空薬A", text, status="planned")])


@pytest.mark.parametrize("text,fragment", [("本人のカロナールを中止しました。", "ロナール"),
                                           ("本人のカロナールAAを中止しました。", "カロナールA")])
def test_an_unhinted_brand_named_only_by_a_fragment_keeps_the_gap(text, fragment):
    assert not hints(PREFIX + text).get("rx_actions")
    assert _rx_gap(text, [_med(fragment, text, action="stop", status="past")])
    full = text[3:text.index("を")]
    assert not _rx_gap(text, [_med(full, text, action="stop", status="past")])


@pytest.mark.parametrize("text,fragment,full", [("本人のカロナールを中止しました。", "ロナール", "カロナール"),
                                                ("本人の架空薬AAを中止しました。", "架空薬A", "架空薬AA")])
def test_a_fragment_kept_beside_the_right_name_stays_unresolved(text, fragment, full):
    right = _med(full, text, action="stop", status="past")
    assert not _rx_gap(text, [right])
    assert _rx_gap(text, [_med(fragment, text, action="stop", status="past"), right])


def test_two_real_names_and_a_dose_glued_name_stay_valid():
    text = "本人の架空薬A5mgと架空薬AAを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past", dose="5mg"),
                              _med("架空薬AA", text, action="stop", status="past")])


@pytest.mark.parametrize("text", ["本人の架空薬Aを明日から開始いたします。", "本人の架空薬Aを明日から開始致します。"])
def test_a_polite_planned_change_is_planned(text):
    assert not _rx_gap(text, [_med("架空薬A", text, status="planned")])
    assert _rx_gap(text, [_med("架空薬A", text)])


def test_a_polite_denial_or_question_still_asks_nothing():
    for text in ("本人の架空薬Aを開始いたしません。", "本人の架空薬Aを開始いたしますか。"):
        assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


@pytest.mark.parametrize("text,right,wrong", [
    ("本人の架空薬Aの追加を相談しました。", {"action": None, "status": "planned"}, {"action": "stop", "status": "current"}),
    ("本人の架空薬Aの追加を相談しました。", {"action": None, "status": "planned"}, {"action": "stop", "status": "planned"}),
    ("本人の架空薬Aを開始してください。", {"action": "start", "status": "planned"}, {"action": "start", "status": "current"}),
])
def test_a_wrong_item_of_either_state_beside_the_right_one_stays_unresolved(text, right, wrong):
    assert not _rx_gap(text, [{**_med("架空薬A", text), **right}])
    assert _rx_gap(text, [{**_med("架空薬A", text), **right}, {**_med("架空薬A", text), **wrong}])


def test_a_completed_stop_is_a_past_stop():
    text = "本人の架空薬Aは中止済みです。"
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


@pytest.mark.parametrize("text,past_start_ok", [("本人は架空薬Aを開始しました。", False),
                                                ("本人は架空薬Aを開始し、その後中止しました。", True),
                                                ("本人の架空薬Aを中止し、その後開始しました。", False)])
def test_a_done_start_is_current_unless_the_source_stops_it_afterwards(text, past_start_ok):
    meds = [_med("架空薬A", text, action="start", status="past")]
    if "中止" in text:
        meds.append(_med("架空薬A", text, action="stop", status="past"))
    assert bool(_rx_gap(text, meds)) is not past_start_ok


@pytest.mark.parametrize("text", ["本人の架空薬Aを医師が中止しました。", "本人の架空薬Aを母が中止しました。",
                                  "本人のカロナールを本日中止しました。"])
def test_an_actor_or_modifier_between_the_drug_and_its_change_keeps_the_object(text):
    name = text[3:text.index("を")]
    assert _rx_gap(text, [_med(name, text, action="none")])
    assert not _rx_gap(text, [_med(name, text, action="stop", status="past")])


def test_an_action_the_source_never_states_for_that_drug_is_not_grounded():
    text = "本人は架空薬Aを中止しました。架空薬Bは内服中です。"
    a = _med("架空薬A", text, action="stop", status="past")
    assert not _rx_gap(text, [a, _med("架空薬B", text, action="none")])
    assert _rx_gap(text, [a, _med("架空薬B", text, action="stop", status="past")])
    history = "本人は以前に架空薬Aを開始していました。本人の架空薬Bを中止しました。"
    assert not _rx_gap(history, [_med("架空薬A", history, status="past"),
                                 _med("架空薬B", history, action="stop", status="past")])


def test_a_shared_administration_noun_keeps_both_drugs_as_objects():
    text = "本人は架空薬Aと架空薬Bの投与を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text, action="none")])


@pytest.mark.parametrize("other", ["母の架空薬Bを開始しました。", "もし本人の架空薬Bを開始した場合は連絡してください。"])
def test_another_persons_or_a_conditional_change_grounds_no_patient_action(other):
    text = "本人の架空薬Aを開始しました。" + other
    a = _med("架空薬A", text)
    assert not _rx_gap(text, [a, _med("架空薬B", text, action="none")])
    assert _rx_gap(text, [a, _med("架空薬B", text)])


@pytest.mark.parametrize("ending", ["開始予定です", "開始を検討中です"])
def test_an_undecided_change_supports_a_planned_item(ending):
    text = f"本人の架空薬Aは{ending}。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="planned")])


def test_two_changes_in_one_clause_keep_their_order():
    text = "本人の架空薬Aを開始し架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="past"), _med("架空薬A", text, action="stop", status="past")])


def test_an_unnamed_change_proves_no_drugs_action():
    text = "本人の架空薬Aを内服中に心停止しました。"
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_a_historical_stop_still_needs_a_past_item():
    text = "本人は以前に架空薬Aを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop")])


@pytest.mark.parametrize("word,action", [("増量", "increase"), ("減量", "decrease"), ("変更", "change")])
def test_a_done_dose_change_is_current(word, action):
    text = f"本人の架空薬Aを{word}しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action=action)])
    assert _rx_gap(text, [_med("架空薬A", text, action=action, status="past")])


@pytest.mark.parametrize("text", ["本人の架空薬Aは中止していません。", "本人の架空薬Aは中止されていない。"])
def test_a_progressive_or_passive_denial_asks_nothing_and_grounds_nothing(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_ongoing_is_neither_denied_nor_planned():
    text = "本人は架空薬Aを開始しています。"
    assert not _rx_gap(text, [_med("架空薬A", text)])
    assert _rx_gap(text, [_med("架空薬A", text, status="planned")])


@pytest.mark.parametrize("text,name", [("本人の架空薬A5mgは変更なしです。", "架空薬A"), ("本人のカロナールは変更なしです。", "カロナール")])
def test_no_change_grounds_no_extracted_change(text, name):
    assert not _rx_gap(text, [_med(name, text, action="none")])
    assert _rx_gap(text, [_med(name, text, action="change")])


@pytest.mark.parametrize("text", ["本人のアムロジピン5mg錠を中止しました。", "本人のアムロジピン5mg1錠を中止しました。"])
def test_a_strength_with_its_form_keeps_the_drug_as_the_object(text):
    assert not _rx_gap(text, [_med("アムロジピン", text, action="stop", status="past", dose="5mg")])
    assert _rx_gap(text, [_med("アムロジピン", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text,name", [("本人の降圧薬アムロジピンを中止しました。", "アムロジピン"),
                                       ("本人は抗菌薬セフトリアキソンを開始しました。", "セフトリアキソン")])
def test_a_class_label_before_the_product_keeps_its_name(text, name):
    action, status = ("stop", "past") if "中止" in text else ("start", "current")
    assert not _rx_gap(text, [_med(name, text, action=action, status=status)])


@pytest.mark.parametrize("text,name,action,status", [
    ("本人の降圧薬アムロジピン5mgを中止しました。", "アムロジピン", "stop", "past"),
    ("本人の抗菌薬セフトリアキソン5mgを開始しました。", "セフトリアキソン", "start", "current")])
def test_a_class_labelled_rule_name_yields_to_the_product_name(text, name, action, status):
    assert not _rx_gap(text, [_med(name, text, action=action, status=status, dose="5mg")])
    assert _rx_gap(text, [_med(name, text, action="none", dose="5mg")])


def test_a_substitution_needs_the_stop_and_the_start_not_a_generic_change():
    text = "本人は架空薬Aから架空薬Bへ変更しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past"), _med("架空薬B", text)])
    assert _rx_gap(text, [_med("架空薬A", text, action="change"), _med("架空薬B", text, action="change")])


def test_oyobi_joins_two_drugs():
    text = "本人は架空薬A及び架空薬Bを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text)])


def test_a_restart_beside_another_drugs_stop_is_grounded():
    text = "本人の架空薬Aを再開しました。本人の架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text), _med("架空薬B", text, action="stop", status="past")])


def test_repeated_changes_of_one_drug_each_find_their_item():
    text = "本人の架空薬Aを開始し、中止し、その後開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, status="past"), _med("架空薬A", text, action="stop", status="past"),
                              _med("架空薬A", text)])


@pytest.mark.parametrize("count", [7, -1, True, False, 0.0, "0", None])
def test_a_resolved_entry_with_remaining_candidates_is_not_complete(tmp_path, monkeypatch, count):
    db = _ledger(tmp_path)
    db.save_messages([_message(body=PREFIX)])
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: copy.deepcopy(OBSERVATIONS))
    try:
        assert extract_llm.run_pending(db, budget_s=300)["done"] == 1
        artifact_id, meta = _final_meta(db)
        meta["integrity"] = {"completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": count}}}
        db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact_id))
        db.db.commit()
        source = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert extract_llm.progress_for_message(db.db, source, model=extract_llm.MODEL)["state"] == "attention"
    finally:
        db.close()


def test_a_narrow_quote_cannot_borrow_another_occurrences_status():
    old, now = "本人の架空薬Aを開始し、中止しました。", "本人の架空薬Aを開始しました。"
    text = old + now
    right = [_med("架空薬A", old, status="past"), _med("架空薬A", old, action="stop", status="past"), _med("架空薬A", now)]
    assert not _rx_gap(text, right)
    assert _rx_gap(text, [*right, _med("架空薬A", now, status="past")])


@pytest.mark.parametrize("text,denied", [("本人の架空薬A5mgは中止できません。", True), ("本人の架空薬A5mgは中止出来ない。", True),
                                         ("本人の架空薬A5mgは中止できるか確認中です。", True),
                                         ("本人の架空薬A5mgは中止出来るか確認中です。", True),
                                         ("本人の架空薬A5mgは中止できます。", True), ("本人の架空薬A5mgは中止出来ます。", True),
                                         ("本人の架空薬A5mgは中止できましたか。", True),
                                         ("本人の架空薬A5mgは中止出来ましたか。", True),
                                         ("本人の架空薬A5mgは中止できた。", False),
                                         ("本人の架空薬A5mgは中止できました。", False)])
def test_inability_denies_the_change_but_a_done_one_does_not(text, denied):
    stop = _med("架空薬A", text, action="stop", status="past", dose="5mg")
    assert bool(_rx_gap(text, [stop])) is denied
    assert bool(_rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])) is not denied


@pytest.mark.parametrize("text", ["本人の架空薬A5mgは副作用のため中止しました。", "本人の架空薬A5mgは発疹が出たので中止しました。"])
def test_a_reason_between_the_drug_and_its_change_keeps_the_drug(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past", dose="5mg")])


def test_a_drug_named_after_the_reason_is_that_changes_own():
    text = "本人の架空薬A5mgは副作用のため架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg"), _med("架空薬B", text, action="stop", status="past")])


def test_a_drugs_own_service_is_no_change_of_the_drug():
    text = "本人は架空薬A5mgの服薬指導を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text,action,status", [("本人は入院時に架空薬A5mgを開始しました。", "start", "current"),
                                                ("退院時に架空薬A5mgを中止しました。", "stop", "past")])
def test_an_event_time_of_a_done_change_is_no_condition(text, action, status):
    assert _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])
    assert not _rx_gap(text, [_med("架空薬A", text, action=action, status=status, dose="5mg")])
    cond = "もし入院時に架空薬Aを開始する場合は連絡してください。"
    assert not _rx_gap(cond, [_med("架空薬A", cond, action="none")])


def test_a_drugs_service_grounds_no_start_of_the_drug():
    text = "本人は架空薬A5mgの服薬指導を開始しました。"
    assert _rx_gap(text, [_med("架空薬A", text, dose="5mg")])


def test_a_reason_followed_by_another_object_keeps_its_clause():
    text = "本人は架空薬A5mgを内服しているので運動療法を開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none", dose="5mg")])


@pytest.mark.parametrize("text", ["本人の架空薬Aを中止しましたか？", "本人の架空薬Aを開始しましたか？"])
def test_a_past_question_states_no_done_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    action, status = ("stop", "past") if "中止" in text else ("start", "current")
    assert _rx_gap(text, [_med("架空薬A", text, action=action, status=status)])


@pytest.mark.parametrize("text", ["本人の架空薬Aを中止する予定はありません。", "本人の架空薬Aの中止を検討していません。"])
def test_a_denied_plan_grounds_no_planned_change(text):
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert _rx_gap(text, [_med("架空薬A", text, action="stop", status="planned")])


@pytest.mark.parametrize("repaired,state", [
    ([_med("架空薬A", "本人の架空薬A5mgを中止しました", action="none", dose="5mg")], "unresolved"),
    ([_med("架空薬A", "本人の架空薬A5mgを中止しました", action="stop", status="past", dose="5mg")], None)])
def test_a_repair_after_an_invalid_answer_records_its_own_gaps(monkeypatch, repaired, state):
    body = PREFIX + "本人の架空薬A5mgを中止しました。"
    calls, meta = [], {}
    monkeypatch.setattr(extract_llm, "_llm_call", lambda *args, **kwargs: calls.append(args) or copy.deepcopy(
        {"meds": "broken"} if len(calls) == 1 else {**OBSERVATIONS, "meds": repaired}))
    extract_llm.llm_extract(body, hints=hints(body), meta_out=meta)
    assert len(calls) == 2
    states = {e["state"] for e in (meta.get("completeness") or {}).values()}
    assert states == ({state} if state else set())


@pytest.mark.parametrize("name", ["リハビリ", "マッサージ", "スイミング"])
@pytest.mark.parametrize("keep_neutral", [False, True])
def test_a_model_name_alone_cannot_gain_a_medication_action(monkeypatch, name, keep_neutral):
    text = f"本人は{name}を開始しました。"
    neutral = _med(name, text, action="none")
    repaired = ([neutral] if keep_neutral else []) + [_med(name, text)]
    calls, result = _repair(monkeypatch, text, [neutral], repaired)
    assert len(calls) == 2
    assert [(m["name"], m["action"]) for m in result["meds"]] == [(name, "none")]
    assert extract_llm._completeness_issues(result, hints(PREFIX + text), PREFIX + text)


def test_an_unrelated_drug_does_not_support_a_model_only_name(monkeypatch):
    text = "本人はリハビリを開始しました。本人の架空薬A5mgを開始しました。"
    neutral = _med("リハビリ", text, action="none")
    drug = _med("架空薬A", text, dose="5mg")
    calls, result = _repair(monkeypatch, text, [neutral, drug], [neutral, drug, _med("リハビリ", text)])
    assert len(calls) == 2
    assert [(m["name"], m["action"]) for m in result["meds"]] == [("リハビリ", "none"), ("架空薬A", "start")]


@pytest.mark.parametrize("text,name,dose", [
    ("本人のカロナール5mgを中止しました。", "カロナール", "5mg"),
    ("本人の降圧薬アムロジピンを中止しました。", "アムロジピン", None),
    ("本人の架空薬Aを中止しました。", "架空薬A", None)])
def test_independent_medication_context_still_allows_a_proven_action_repair(monkeypatch, text, name, dose):
    calls, result = _repair(monkeypatch, text, [_med(name, text, action="none", dose=dose)],
                            [_med(name, text, action="stop", status="past", dose=dose)])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [("stop", "past")]
    assert not extract_llm._completeness_issues(result, hints(PREFIX + text), PREFIX + text)


def test_a_doseless_brand_keeps_its_gap_without_an_unsupported_action_repair(monkeypatch):
    text = "本人のカロナールを中止しました。"
    neutral = _med("カロナール", text, action="none")
    stopped = _med("カロナール", text, action="stop", status="past")
    assert _rx_gap(text, [neutral])
    assert not _rx_gap(text, [stopped])
    calls, result = _repair(monkeypatch, text, [neutral], [stopped])
    assert len(calls) == 2
    assert [(m["action"], m["status"]) for m in result["meds"]] == [("none", "current")]
    assert extract_llm._completeness_issues(result, hints(PREFIX + text), PREFIX + text)


@pytest.mark.parametrize("text", ["本人の架空薬Aを、中止しました。", "本人の架空薬Aは、本日中止しました。",
                                  "本人の架空薬Aを本日、中止しました。"])
def test_an_object_particle_comma_does_not_drop_its_medication(text):
    assert _rx_gap(text, [_med("架空薬A", text, action="none")])
    assert not _rx_gap(text, [_med("架空薬A", text, action="stop", status="past")])


def test_a_particle_comma_does_not_carry_a_drug_into_a_different_non_drug_object():
    text = "本人の架空薬Aは、リハビリを開始しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none")])


def test_a_particle_comma_does_not_carry_a_drug_into_another_drugs_operation():
    text = "本人の架空薬Aは、架空薬Bを中止しました。"
    assert not _rx_gap(text, [_med("架空薬A", text, action="none"), _med("架空薬B", text, action="stop", status="past")])
