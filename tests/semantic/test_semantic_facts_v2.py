"""Canonical semantic-facts/v2 extraction tests (T5).

The canonical path must emit a contract-validated document whose
obligations close only through facts or explicit adjudicated absence.
Dropped items, failed chunks, unverified facts, or contradicted "none"
verdicts keep the document incomplete — never a silent pass.
"""
import semantic_transport
import json
import sqlite3

import pytest

import semantic_extraction as extraction
import semantic_facts as sf
import semantic_extraction as ex
from semantic_testkit import NO_FACTS, facts_llm


FP = "sf_v2_test"


def _llm(*args, **kwargs):
    answer = facts_llm(*args, **kwargs)

    def observed(prompt):
        response = answer(prompt)
        semantic_transport._record_inference("local")
        return response

    return observed


def _member(body, **kw):
    member = {"project_id": 1, "message_id": "m1", "revision": "r1",
              "body_original": body, "body_state": "full",
              "posted_at": "2026-09-20",
              "sender": {"id": "s1", "type": "staff", "profession": "dr"}}
    member.update(kw)
    return member


class _FakeLedger:
    def __init__(self, request):
        self.db = sqlite3.connect(":memory:")
        request.addfinalizer(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE artifacts(artifact_id INTEGER PRIMARY KEY,kind TEXT,"
                        "project_id INTEGER,message_id TEXT,model TEXT,content TEXT,meta TEXT)")
        self.db.execute("CREATE TABLE messages(message_id TEXT, project_id INTEGER, parent_id TEXT)")
        self.db.execute("CREATE TABLE fetch_jobs(kind TEXT, project_id INTEGER, message_id TEXT, payload TEXT)")

    @property
    def rows(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM artifacts ORDER BY artifact_id")]

    def artifacts(self, kind, project_id=None, message_id=None):
        return [r for r in self.rows if r["kind"] == kind]

    def artifact_add(self, kind, content, project_id=None,
                     message_id=None, model=None, meta=None):
        self.db.execute("INSERT INTO artifacts(kind,project_id,message_id,model,content,meta) "
                        "VALUES(?,?,?,?,?,?)", (kind, project_id, message_id, model,
                                                content, json.dumps(meta or {})))
        self.db.commit()


def _obligations(doc, status=None):
    obs = doc["obligations"]
    if status is not None:
        obs = [o for o in obs if o["status"] == status]
    return obs


def test_complete_doc_validates_and_closes_obligations():
    body = "アムロジピン5mgを継続します。"
    llm = _llm(
        [{"statement": "アムロジピン5mg継続", "kind": "medication_event",
          "action": "continue", "subject_role": "patient",
          "polarity": "affirmed", "workflow_status": "performed",
          "importance": "T1",
          "evidence_quote": "アムロジピン5mgを継続"}],
        presence={"medication": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    assert result["complete"] and result["coverage"]["status"] == "complete"
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    assert not result["coverage"]["open_obligation_ids"]
    covered = _obligations(doc, "covered")
    assert covered and covered[0]["category"] == "medication"
    assert covered[0]["fact_ids"]
    # Model fact is verified with owning-chunk evidence.
    fact = next(f for f in doc["facts"] if f["provenance"] == "local_llm")
    assert fact["validation_status"] == "verified"
    ev = next(e for e in doc["evidence"]
              if e["evidence_id"] in fact["evidence_ids"])
    assert body[ev["start"]:ev["end"]] == ev["quote"]
    assert ev["atom_id"] in {a["atom_id"] for a in doc["atoms"]}
    # Stable identity: same inputs reproduce the same fact_id.
    again = extraction.extract_facts_v2(llm, _member(body))
    assert {f["fact_id"] for f in again["doc"]["facts"]} \
        == {f["fact_id"] for f in doc["facts"]}


def test_verdict_one_without_facts_stays_open():
    llm = _llm([], presence={"medication": "one"})
    result = extraction.extract_facts_v2(llm, _member("本日は特記なし。"))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    open_med = [o for o in _obligations(doc, "open")
                if o["category"] == "medication"]
    assert open_med
    assert result["coverage"]["status"] == "incomplete"
    assert not result["complete"]


def test_verdict_none_contradicted_by_deterministic_signal():
    # Model claims no medication, but extract_v1 sees "5mg".
    llm = _llm([], presence={})
    result = extraction.extract_facts_v2(
        llm, _member("アムロジピン5mgを継続。"))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    ambiguous = [o for o in _obligations(doc, "ambiguous")
                 if o["category"] == "medication"]
    assert ambiguous
    assert result["coverage"]["status"] == "incomplete"


def test_verdict_none_with_fact_is_ambiguous_not_covered():
    llm = _llm(
        [{"statement": "血圧120/80", "kind": "vital_lab",
          "evidence_quote": "血圧120/80"}],
        presence={"vital_lab": "none"})
    result = extraction.extract_facts_v2(llm, _member("血圧120/80でした。"))
    ambiguous = [o for o in _obligations(result["doc"], "ambiguous")
                 if o["category"] == "vital_lab"]
    assert ambiguous
    assert result["coverage"]["status"] == "incomplete"


def test_missing_category_presence_fails_chunk():
    def llm(_prompt):
        return json.dumps({"facts": []}, ensure_ascii=False)
    result = extraction.extract_facts_v2(llm, _member("本文です。"))
    assert not result["extraction_complete"]
    assert result["failure_reason"] == "model"
    assert _obligations(result["doc"], "failed")
    sf.validate_facts_doc(result["doc"])


def test_dropped_item_fails_chunk_and_rolls_back_links():
    llm = _llm(
        [{"statement": "ok", "kind": "vital_lab",
          "evidence_quote": "血圧120"},
         {"statement": "bad kind", "kind": "no_such_kind",
          "evidence_quote": "血圧120"}],
        presence={"vital_lab": "one"})
    result = extraction.extract_facts_v2(
        llm, _member("血圧120でした。"))
    assert not result["extraction_complete"]
    assert result["dropped_in_failed_chunk"] == 1
    doc = result["doc"]
    sf.validate_facts_doc(doc)   # rolled-back links keep doc consistent
    assert all(not o["fact_ids"] for o in doc["obligations"])


def test_llm_exception_marks_chunk_failed():
    def llm(_prompt):
        raise RuntimeError("model down")
    result = extraction.extract_facts_v2(llm, _member("本文です。"))
    assert result["failure_reason"] == "model"
    assert _obligations(result["doc"], "failed")
    assert result["coverage"]["status"] == "incomplete"
    sf.validate_facts_doc(result["doc"])


def test_unresolvable_evidence_keeps_fact_unverified():
    llm = _llm(
        [{"statement": "頭痛あり", "kind": "symptom_state",
          "evidence_quote": "存在しない引用"}],
        presence={"symptom_state": "one"})
    result = extraction.extract_facts_v2(llm, _member("本日は晴れ。"))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    fact = next(f for f in doc["facts"]
                if f["provenance"] == "local_llm")
    assert fact["validation_status"] == "unverified"
    assert "unverified_facts" in result["coverage"]["limitations"]
    assert result["coverage"]["status"] == "incomplete"


def test_out_of_chunk_quote_not_borrowed():
    body = "アムロジピンを開始。" + "あ" * 80 + "\n" + "無関係な文章。"
    llm = _llm(
        [{"statement": "s", "kind": "medication_event", "action": "start",
          "evidence_quote": "アムロジピンを開始"}],
        presence={"medication": "one"})

    def per_chunk(prompt):
        # Second chunk cannot see the quote -> its fact is unverified.
        if "アムロジピン" not in prompt:
            return json.dumps(
                {"facts": [{"statement": "s", "kind": "medication_event",
                            "action": "start",
                            "evidence_quote": "アムロジピンを開始"}],
                 "category_presence": NO_FACTS}, ensure_ascii=False)
        return llm(prompt)

    result = extraction.extract_facts_v2(
        per_chunk, _member(body), chunk_size=60)
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    assert result["chunks_total"] >= 2
    # the chunk that holds the quote verifies its fact; the other chunk's
    # copy cannot borrow that out-of-chunk evidence and stays unverified
    llm_facts = [f for f in doc["facts"] if f["provenance"] == "local_llm"]
    statuses = sorted(f["validation_status"] for f in llm_facts)
    assert statuses == ["unverified", "verified"], statuses
    unverified = next(f for f in llm_facts
                      if f["validation_status"] == "unverified")
    assert unverified["evidence_ids"] == []
    assert "unverified_facts" in doc["coverage"]["limitations"]


def test_durable_prefix_reuse_v2(request):
    body = "一段落目です。" + "あ" * 50 + "\n" + "二段落目です。" + "い" * 50

    ledger = _FakeLedger(request)
    member = _member(body)
    calls = []

    def llm_fail_second(prompt):
        calls.append(prompt)
        if len(calls) == 2:
            raise RuntimeError("model down")
        semantic_transport._record_inference("local")
        return json.dumps({"facts": [], "category_presence": NO_FACTS},
                          ensure_ascii=False)

    first = extraction.extract_facts_v2(
        llm_fail_second, member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40)
    assert not first["extraction_complete"]
    assert first["chunks_completed"] >= 1

    second_calls = []

    def llm_ok(prompt):
        second_calls.append(prompt)
        semantic_transport._record_inference("local")
        return json.dumps({"facts": [], "category_presence": NO_FACTS},
                          ensure_ascii=False)

    second = extraction.extract_facts_v2(
        llm_ok, member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40)
    assert second["extraction_complete"]
    assert second["reused_chunks"] == first["completed_chunks"]
    assert len(second_calls) < second["chunks_total"]
    sf.validate_facts_doc(second["doc"])
    rows = [r for r in ledger.rows
            if r["kind"] == extraction.KIND_CHUNK_V2]
    assert rows
    meta = json.loads(rows[0]["meta"])
    assert meta["schema"] == extraction.SCHEMA_VERSION_V2


def test_v2_cache_reuses_evidence_bearing_chunks(request):
    # FIX-SE2: cached v2 rows were validated with the v1 key spelling
    # (start_codepoint), so every evidence-bearing chunk missed the
    # cache and re-extracted on each restart.
    body = "アムロジピン5mgを継続します。"

    ledger = _FakeLedger(request)
    fact = {"statement": "アムロジピン5mg継続", "kind": "medication_event",
            "action": "continue", "subject_role": "patient",
            "evidence_quote": "アムロジピン5mgを継続"}
    llm = _llm([fact], presence={"medication": "one"})
    first = extraction.extract_facts_v2(llm, _member(body),
                                        ledger=ledger,
                                        source_fingerprint=FP)
    assert first["extraction_complete"]
    calls = []

    def llm_second(prompt):
        calls.append(prompt)
        return json.dumps({"facts": [], "category_presence": NO_FACTS})

    second = extraction.extract_facts_v2(llm_second, _member(body),
                                         ledger=ledger,
                                         source_fingerprint=FP)
    assert second["extraction_complete"]
    assert not calls  # every chunk reused — zero model calls
    assert second["reused_chunks"] == first["completed_chunks"]
    sf.validate_facts_doc(second["doc"])
    ev = list(second["doc"]["evidence"])
    assert ev and body[ev[0]["start"]:ev[0]["end"]] == ev[0]["quote"]


def test_v1_hints_union_with_provenance():
    body = "アムロジピン5mgを継続します。"
    result = extraction.extract_facts_v2(
        _llm([], presence={"medication": "one"}), _member(body))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    hints = [f for f in doc["facts"] if "extract_v1" in f["provenance"]]
    assert hints
    assert all(f["kind"] in sf.FACT_KINDS for f in hints)
    # Hint facts carry resolved evidence inside their owning atom.
    ev_ids = {e["evidence_id"] for e in doc["evidence"]}
    for fact in hints:
        assert all(ref in ev_ids for ref in fact["evidence_ids"])


def test_hint_dedup_retargets_dangling_obligation_links():
    # FIX-SE1: a v1 hint sharing (kind, normalized statement) with a
    # model fact is dropped, but its fact_id was already linked in the
    # evidence-owning chunk's obligation — leaving the link dangled and
    # crashed the doc validator. Links must retarget the survivor.
    body = "アムロジピン5mg を開始した。なお、アムロジピン5mg は来週中止予定。"
    llm = _llm(
        [{"statement": "アムロジピン 5mg", "kind": "medication_exposure",
          "subject_role": "patient",
          "evidence_quote": "アムロジピン5mg は来週中止予定",
          "polarity": "affirmed"}],
        presence={"medication": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    doc = result["doc"]          # must not raise doc_obligation_fact_unknown
    sf.validate_facts_doc(doc)
    fact_ids = {f["fact_id"] for f in doc["facts"]}
    for ob in doc["obligations"]:
        assert all(fid in fact_ids for fid in ob["fact_ids"])
    survivors = [f for f in doc["facts"]
                 if f["statement"] == "アムロジピン 5mg"]
    assert len(survivors) == 1
    # the hint quote occurs twice (no unique span), so it cannot point at
    # the model fact's evidence and never corroborates it
    assert survivors[0]["provenance"] == "local_llm"


def test_hint_dedup_subject_mismatch_also_safe():
    # Same mechanism via subject: hint subject is "unknown" while the
    # model fact carries patient:1 — different fact_id, same dedup key.
    body = "アムロジピン5mg を内服中。"
    llm = _llm(
        [{"statement": "アムロジピン 5mg", "kind": "medication_exposure",
          "subject_role": "patient",
          "evidence_quote": "アムロジピン5mg を内服中",
          "polarity": "affirmed"}],
        presence={"medication": "one"})
    doc = extraction.extract_facts_v2(llm, _member(body))["doc"]
    sf.validate_facts_doc(doc)
    fact_ids = {f["fact_id"] for f in doc["facts"]}
    for ob in doc["obligations"]:
        assert all(fid in fact_ids for fid in ob["fact_ids"])


def test_invalid_enums_degrade_to_unknown_not_drop():
    llm = _llm(
        [{"statement": "痛みあり", "kind": "symptom_state",
          "polarity": "bogus", "epistemic": 42, "workflow_status": "x",
          "importance": "Z9", "evidence_quote": "痛み"}],
        presence={"symptom_state": "one"})
    result = extraction.extract_facts_v2(llm, _member("痛みがあります。"))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    fact = next(f for f in doc["facts"] if f["kind"] == "symptom_state")
    assert fact["polarity"] == "unknown"
    assert fact["epistemic"] == "unknown"
    assert fact["workflow_status"] == "unknown"
    assert fact["importance"] == "unknown"


def test_subject_mapping_patient_family_unknown():
    body = "家族がオムツ交換を希望。本人の薬は継続。"
    llm = _llm(
        [{"statement": "オムツ交換希望", "kind": "preference",
          "subject_role": "family", "subject_name": "娘",
          "evidence_quote": "オムツ交換を希望"},
         {"statement": "薬継続", "kind": "medication_event",
          "action": "continue", "subject_role": "patient",
          "evidence_quote": "薬は継続"},
         {"statement": "主体不明の記述", "kind": "other_observation",
          "evidence_quote": "家族"}],
        presence={"preference": "one", "medication": "one",
                  "other_observation": "one"})
    doc = extraction.extract_facts_v2(llm, _member(body))["doc"]
    sf.validate_facts_doc(doc)
    by_stmt = {f["statement"]: f for f in doc["facts"]}
    assert by_stmt["オムツ交換希望"]["subject"].startswith("person:")
    assert by_stmt["薬継続"]["subject"] == "patient:1"
    assert by_stmt["主体不明の記述"]["subject"] == "unknown"


def test_empty_source_validates_empty_doc():
    result = extraction.extract_facts_v2(_llm([]), _member(""))
    sf.validate_facts_doc(result["doc"])
    assert result["coverage"]["status"] == "complete"
    assert result["doc"]["atoms"] == []


def test_relations_and_evidence_cross_references_hold():
    body = "ロキソプロフェンを開始します。"
    llm = _llm(
        [{"statement": "ロキソプロフェン開始", "kind": "medication_event",
          "action": "start", "subject_role": "patient",
          "evidence_quote": "ロキソプロフェンを開始"}],
        presence={"medication": "one"})
    doc = extraction.extract_facts_v2(llm, _member(body))["doc"]
    sf.validate_facts_doc(doc)
    fact_ids = {f["fact_id"] for f in doc["facts"]}
    for ob in doc["obligations"]:
        assert all(ref in fact_ids for ref in ob["fact_ids"])
    ev_ids = {e["evidence_id"] for e in doc["evidence"]}
    for fact in doc["facts"]:
        assert all(ref in ev_ids for ref in fact["evidence_ids"])
        assert all(ref in {o["obligation_id"] for o in doc["obligations"]}
                   for ref in fact["obligation_ids"])


class _FakeJev:
    """Deterministic preflight stub — Jev is never real in tests."""

    def __init__(self, verdicts=None, fail=False):
        self.verdicts = verdicts or {}
        self.fail = fail
        self.calls = 0

    def evaluate(self, state, questions, deadline):
        self.calls += 1
        if self.fail:
            raise RuntimeError("jev unreachable")
        answers = {}
        for key in questions:
            category = key.removeprefix("has_")
            answers[key] = {"choice": self.verdicts.get(
                category, "absent"), "confidence": 0.9}
        return {"answers": answers}


def test_jev_preflight_creates_jev_pre_obligations():
    body = "アムロジピン5mgを継続します。"
    jev = _FakeJev({"medication": "present"})
    llm = _llm(
        [{"statement": "アムロジピン5mg継続", "kind": "medication_event",
          "action": "continue", "subject_role": "patient",
          "evidence_quote": "アムロジピン5mgを継続"}],
        presence={"medication": "one"})
    doc = extraction.extract_facts_v2(
        llm, _member(body), jev_client=jev)["doc"]
    sf.validate_facts_doc(doc)
    jev_obs = [o for o in doc["obligations"] if o["source"] == "jev_pre"]
    assert len(jev_obs) == len(sf.MANDATORY_CATEGORIES)
    med = next(o for o in jev_obs if o["category"] == "medication")
    assert med["status"] == "covered" and med["fact_ids"]
    others = [o for o in jev_obs if o["category"] != "medication"]
    assert all(o["status"] == "explicit_no_fact" for o in others)


def test_jev_present_without_facts_stays_open():
    # preference has no deterministic extract_v1 signal — a Jev
    # "present" verdict with zero extracted facts cannot close.
    jev = _FakeJev({"preference": "present"})
    llm = _llm([], presence={})   # model says nothing anywhere
    doc = extraction.extract_facts_v2(
        llm, _member("本日は特記なし。"), jev_client=jev)["doc"]
    sf.validate_facts_doc(doc)
    ob = next(o for o in doc["obligations"]
              if o["source"] == "jev_pre" and o["category"] == "preference")
    assert ob["status"] == "open"


def test_jev_absent_contradicted_by_facts_is_ambiguous():
    jev = _FakeJev({"medication": "absent"})
    llm = _llm(
        [{"statement": "薬継続", "kind": "medication_event",
          "action": "continue", "evidence_quote": "薬"}],
        presence={"medication": "one"})
    doc = extraction.extract_facts_v2(
        llm, _member("薬を継続。"), jev_client=jev)["doc"]
    sf.validate_facts_doc(doc)
    ob = next(o for o in doc["obligations"]
              if o["source"] == "jev_pre" and o["category"] == "medication")
    assert ob["status"] == "ambiguous"


def test_jev_failure_marks_preflight_failed():
    jev = _FakeJev(fail=True)
    result = extraction.extract_facts_v2(
        _llm([], presence={}), _member("本文です。"), jev_client=jev)
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    failed = [o for o in doc["obligations"]
              if o["source"] == "jev_pre" and o["status"] == "failed"]
    assert len(failed) == len(sf.MANDATORY_CATEGORIES)
    assert result["coverage"]["status"] == "incomplete"


def test_no_jev_client_means_no_jev_pre_obligations():
    doc = extraction.extract_facts_v2(
        _llm([], presence={}), _member("本文です。"))["doc"]
    sf.validate_facts_doc(doc)
    assert all(o["source"] == "deterministic"
               for o in doc["obligations"])


def test_preflight_verdicts_cached_with_chunk(request):
    body = "一段落目です。" + "あ" * 50

    ledger = _FakeLedger(request)
    member = _member(body)
    jev = _FakeJev()
    extraction.extract_facts_v2(
        _llm([], presence={}), member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40, jev_client=jev)
    # Second run reuses the cached chunk — preflight verdicts come from
    # the chunk cache, no new Jev call for reused chunks.
    jev2 = _FakeJev()
    second = extraction.extract_facts_v2(
        _llm([], presence={}), member, ledger=ledger,
        source_fingerprint=FP, chunk_size=40, jev_client=jev2)
    assert second["reused_chunks"]
    assert jev2.calls == 0
    sf.validate_facts_doc(second["doc"])


def test_deadline_between_chunks_leaves_pending():
    body = "あ" * 50 + "\n" + "い" * 50 + "\n" + "う" * 50

    def llm(_prompt):
        return json.dumps({"facts": [], "category_presence": NO_FACTS},
                          ensure_ascii=False)

    member = _member(body)
    first = extraction.extract_facts_v2(
        llm, member, deadline=0.0, chunk_size=60)
    assert not first["extraction_complete"]
    assert first["failure_reason"] == "deadline"
    assert first["pending_chunks"]
    sf.validate_facts_doc(first["doc"])


@pytest.mark.parametrize("provenance", ["extract_v1", [], True])
def test_model_cannot_supply_trusted_provenance(provenance):
    result = extraction.extract_facts_v2(_llm([{
        "statement": "合成所見", "kind": "other_observation",
        "evidence_quote": "合成観察文", "_provenance": provenance}],
        {"other_observation": "one"}), _member("合成観察文です。"))
    fact = next(f for f in result["facts"] if f["statement"] == "合成所見")
    assert fact["provenance"] == "local_llm"


def test_ambiguous_chunk_quote_keeps_canonical_fact_unverified():
    result = extraction.extract_facts_v2(_llm([{
        "statement": "合成所見", "kind": "other_observation",
        "evidence_quote": "合成観察文"}], {"other_observation": "one"}),
        _member("合成観察文。合成観察文。"))
    fact = next(f for f in result["facts"] if f["statement"] == "合成所見")
    assert fact["validation_status"] == "unverified"
    assert not result["complete"]


@pytest.mark.parametrize("field,bad", [
    ("meta", []), ("content", []),
    ("meta", {"chunk_index": float("inf")}),
])
def test_malformed_cache_rows_are_ignored_and_reextracted(request, field, bad):
    ledger = _FakeLedger(request)
    member = _member("合成観察文です。")
    extraction.extract_facts_v2(_llm([]), member, ledger=ledger)
    # Only these two parameterized columns are permitted; the malformed value is bound data.
    ledger.db.execute(f"UPDATE artifacts SET {field}=? WHERE kind=?",
                      (json.dumps(bad), extraction.KIND_CHUNK_V2))
    ledger.db.commit()
    result = extraction.extract_facts_v2(_llm([]), member, ledger=ledger)
    assert result["complete"]
    assert result["reused_chunks"] == []


@pytest.mark.parametrize("change", [None, "model", "prompt"])
def test_cached_chunks_require_same_model_and_prompt(request, monkeypatch, change):
    import semantic_llm
    ledger = _FakeLedger(request)
    member = _member("合成観察文です。")
    extraction.extract_facts_v2(_llm([]), member, ledger=ledger)
    if change == "model":
        monkeypatch.setattr(semantic_transport, 'llm_model',
                            lambda cfg=None: "synthetic-other-model")
    elif change == "prompt":
        monkeypatch.setattr(semantic_llm, "_FACT_V2_PROMPT",
                            semantic_llm._FACT_V2_PROMPT + "\n")
    result = extraction.extract_facts_v2(_llm([]), member, ledger=ledger)
    assert result["complete"]
    assert bool(result["reused_chunks"]) is (change is None)


def test_new_chunk_layout_is_persisted_for_same_source(request):
    ledger = _FakeLedger(request)
    member = _member("合成観察文です。" * 10)
    for size in (40, 30, 40):
        result = extraction.extract_facts_v2(_llm([]), member, ledger=ledger, chunk_size=size)
        manifest = json.loads(ledger.artifacts(extraction.KIND_MANIFEST)[-1]["content"])
        assert manifest["atoms"] == result["manifest"]["atoms"]
        assert [c["end"] for c in manifest["chunks"]] == [
            c["end"] for c in result["manifest"]["chunks"]]


@pytest.mark.parametrize("confidence", [None, True, float("nan"), 10**1000],
                         ids=["missing", "boolean", "nan", "overflow"])
def test_invalid_preflight_confidence_cannot_close_absence(confidence):
    class InvalidJev(_FakeJev):
        def evaluate(self, state, questions, deadline):
            out = super().evaluate(state, questions, deadline)
            for answer in out["answers"].values():
                answer["confidence"] = confidence
            return out

    result = extraction.extract_facts_v2(
        _llm([]), _member("合成観察文です。"), jev_client=InvalidJev())
    assert not result["complete"]
    assert all(ob["status"] == "ambiguous" for ob in result["doc"]["obligations"]
               if ob["source"] == "jev_pre")


# Regression: repairing a fact must keep a covered jev_pre obligation covered.
BODY = "合成患者は個室を希望しています。"


def _jev_pre_llm(stmt):
    def call(_prompt):
        return json.dumps({
            "facts": [{"statement": stmt, "kind": "preference",
                       "subject_role": "patient", "polarity": "affirmed",
                       "epistemic": "reported",
                       "workflow_status": "reported",
                       "evidence_quote": "個室を希望"}],
            "category_presence": dict(NO_FACTS, preference="one"),
        }, ensure_ascii=False)
    return call


def test_repair_keeps_jev_pre_present_obligation_covered():
    r = ex.extract_facts_v2(_jev_pre_llm("個室希望"), _member(BODY),
                            jev_client=_FakeJev({"preference": "present"}))
    assert r["coverage"]["status"] == "complete"
    doc = r["doc"]
    fid = next(f["fact_id"] for f in doc["facts"]
               if f["kind"] == "preference")
    out = ex.repair_facts_v2(_jev_pre_llm("個室を希望"), _member(BODY), doc,
                             {fid: "wording"})
    assert out["repaired"]
    assert out["doc"]["coverage"]["status"] == "complete"
    jev = [ob for ob in out["doc"]["obligations"]
           if ob["source"] == "jev_pre" and ob["category"] == "preference"]
    assert jev and all(ob["status"] == "covered" and ob["fact_ids"]
                       for ob in jev)


def test_care_detail_survives_facts_doc_validation():
    from semantic_testkit import v2_doc, v2_fact
    detail = {"event_type": "intervention_resume",
              "workflow_status": "performed",
              "date_text": "11月9日", "service_actor": "訪問看護A"}
    fact = v2_fact("f1", kind="care_event", validation_status="unverified",
                   care_detail=detail)
    doc = v2_doc([fact])
    out = sf.validate_facts_doc(doc)
    kept = next(f for f in out["facts"] if f["fact_id"] == "f1")
    assert kept["care_detail"] == detail


@pytest.mark.parametrize("detail", [
    {"event_type": "bogus_type"},
    {"workflow_status": "performed"},
    {"event_type": "monitoring", "extra": "x"},
    {"event_type": "monitoring", "workflow_status": "bogus"},
    {"event_type": "monitoring", "date_text": 12},
    "not_a_dict",
])
def test_invalid_care_detail_fails_facts_doc_validation(detail):
    from semantic_testkit import v2_doc, v2_fact
    doc = v2_doc([v2_fact("f1", kind="care_event", validation_status="unverified",
                        care_detail=detail)])
    with pytest.raises(Exception):
        sf.validate_facts_doc(doc)


def test_care_detail_flows_from_extraction_to_projected_record():
    from semantic_projection import project_v2_doc_legacy
    body = "本人は本日訪問し、サービスを開始しました。"
    llm = _llm(
        [{"statement": "本人の訪問看護開始", "kind": "care_event",
          "subject_role": "patient", "polarity": "affirmed",
          "epistemic": "asserted",
          "workflow_status": "performed", "importance": "T1",
          "evidence_quote": "本人は本日訪問し、サービスを開始しました",
          "care_detail": {"event_type": "intervention_start",
                          "workflow_status": "performed",
                          "date_text": "本日"}}],
        presence={"care_event": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    assert result["coverage"]["status"] == "complete"
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    fact = next(f for f in doc["facts"] if f["kind"] == "care_event")
    assert fact["care_detail"]["event_type"] == "intervention_start"
    projected = project_v2_doc_legacy(doc)
    record = next(r for r in projected["event_records"]
                  if r.get("care_detail"))
    assert record["care_detail"]["event_type"] == "intervention_start"
    assert record["care_detail"]["date_text"] == "本日"


def test_care_detail_on_non_care_event_kind_fails_validation():
    from semantic_testkit import v2_doc, v2_fact
    fact = v2_fact("f1", kind="symptom_state", validation_status="unverified",
                   care_detail={"event_type": "monitoring"})
    with pytest.raises(Exception):
        sf.validate_facts_doc(v2_doc([fact]))


def test_merge_dupe_facts_care_detail_conflict_orders():
    import semantic_assembly as assembly
    from semantic_testkit import v2_fact
    a = v2_fact("f1", kind="care_event", validation_status="unverified",
                care_detail={"event_type": "intervention_start"})
    b = v2_fact("f1", kind="care_event", validation_status="unverified",
                care_detail={"event_type": "intervention_end"})
    plain = v2_fact("f1", kind="care_event", validation_status="unverified")
    for ordered in ([a, b, a], [b, a, b], [a, b], [b, a]):
        merged = assembly.merge_dupe_facts([dict(f) for f in ordered])
        assert len(merged) == 1 and "care_detail" not in merged[0]
    # Absence is not a conflict: a plain duplicate keeps a single detail.
    for ordered in ([plain, a], [a, plain]):
        merged = assembly.merge_dupe_facts([dict(f) for f in ordered])
        assert merged[0]["care_detail"] == a["care_detail"]


def test_repair_merge_does_not_revive_conflicting_care_detail():
    body = "本人は本日訪問し、サービスを開始しました。個室を希望しています。"
    item_a = {"statement": "本人の訪問看護開始", "kind": "care_event",
              "subject_role": "patient", "polarity": "affirmed",
              "epistemic": "asserted", "workflow_status": "performed",
              "importance": "T1",
              "evidence_quote": "本人は本日訪問し、サービスを開始しました",
              "care_detail": {"event_type": "intervention_start",
                              "workflow_status": "performed"}}
    item_p = {"statement": "個室を希望", "kind": "preference",
              "subject_role": "patient", "polarity": "affirmed",
              "epistemic": "reported", "workflow_status": "reported",
              "importance": "T2",
              "evidence_quote": "個室を希望"}
    r = extraction.extract_facts_v2(
        _llm([item_a, item_p], presence={"care_event": "one", "preference": "one"}),
        _member(body))
    doc = r["doc"]
    care = next(f for f in doc["facts"] if f["kind"] == "care_event")
    pref = next(f for f in doc["facts"] if f["kind"] == "preference")
    assert care["care_detail"]["event_type"] == "intervention_start"
    # The care_event fact stays kept while its chunk is re-extracted for the
    # rejected preference: a repair candidate for the same fact_id carrying a
    # different detail must not win silently — the merge drops it entirely.
    item_b = dict(item_a, care_detail={"event_type": "intervention_end",
                                       "workflow_status": "performed"})
    out = extraction.repair_facts_v2(
        _llm([item_b, item_p], presence={"care_event": "one", "preference": "one"}),
        _member(body), doc, {pref["fact_id"]: "wording"})
    fact = next(f for f in out["doc"]["facts"] if f["fact_id"] == care["fact_id"])
    assert "care_detail" not in fact


def test_invalid_care_detail_is_omitted_not_attached():
    body = "本人は本日訪問しました。"
    llm = _llm(
        [{"statement": "本人の訪問看護開始", "kind": "care_event",
          "subject_role": "patient", "polarity": "affirmed",
          "epistemic": "asserted",
          "workflow_status": "performed", "importance": "T1",
          "evidence_quote": "本人は本日訪問しました",
          "care_detail": {"event_type": "bogus_type"}}],
        presence={"care_event": "one"})
    result = extraction.extract_facts_v2(llm, _member(body))
    doc = result["doc"]
    sf.validate_facts_doc(doc)
    fact = next(f for f in doc["facts"] if f["kind"] == "care_event")
    assert "care_detail" not in fact
