"""Canonical-fact coverage in the patient rollup (T5).

The rollup prefers canonical_projection over extract_llm via
current_fact_pred; every verified fact must remain enumerable with
fact_id/evidence — newest generation wins per fact_id.
"""
import json

import pytest

import extract_llm
import history_walk
import inference_dependency_loss
import inference_version as inference
import mcs_queries
from extract_testkit import _message, _hash
from common_testkit import ledger_fixture
import rollup
from semantic_projection import PROJECTION_VERSION


db = ledger_fixture(name="db", patients=(1,))


def _complete_history(db, monkeypatch):
    monkeypatch.setattr(extract_llm.writer_lock, "updater_state", lambda: (False, 1))
    monkeypatch.setattr(extract_llm, "_WRITER_STATE", (False, 1))
    monkeypatch.setattr(extract_llm, "_rerender_cards", lambda *args, **kwargs: None)
    cohort = "a" * 32
    capture = {"version": 1, "cohort_id": cohort, "boundary_ts": 1,
               "boundary_source": "server", "started_at": 1, "updated_at": 1,
               "capture_phase": "complete", "saved_count": 0,
               "capture_proof": {"v": 1, "scope": history_walk.scope(1, cohort), "digest": "b" * 64}}
    db.db.execute("UPDATE patients SET fetch_state='complete', history_floor=-1, initial_import=? WHERE project_id=1",
                  (json.dumps(capture),))
    db.db.commit()


def _save_bound_extraction(db, mid, document):
    row = dict(db.db.execute("SELECT * FROM messages WHERE message_id=?", (mid,)).fetchone())
    row["_source_binding"] = inference_dependency_loss.source_binding(db.db, 1, mid)
    row["_target"] = ("", extract_llm.resolved_model({}))
    quality = {"validation": {"items_dropped": 0, "evidence_dropped": 0},
               "completeness_chunks": 1,
               "completeness": {"0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert extract_llm._replace_current(db, row, json.dumps(document, ensure_ascii=False), integrity=quality)


def _fact(fid, kind, statement, quote="引用"):
    return {"fact_id": fid, "kind": kind, "statement": statement,
            "subject": "patient:1", "evidence_quote": quote,
            "evidence_ids": ["ev"], "importance": "T1",
            "validation_status": "verified"}


def _save_karte_profile(db):
    db.db.execute("UPDATE patients SET karte_id=100 WHERE project_id=1")
    db.db.commit()
    db.karte_summary_store(1, 100, {"comment": "生活環境：正本の合成プロフィール。"})


def test_rollup_and_cached_reader_do_not_resurrect_contradictory_vitals(db, monkeypatch):
    _complete_history(db, monkeypatch)
    _save_karte_profile(db)
    body = "本人の2026-10-09測定で血圧120/80でした。"
    db.save_messages([_message(mid=1, body=body,
                               posted_at="2026-10-09T00:00:00+09:00")])
    _save_bound_extraction(db, 1, {"vitals": {"sbp": 180, "dbp": 80}})
    current = rollup.build_rollup(db.db, 1, cfg={})
    assert current["latest_vitals"] == {"at": "2026-10-09T00:00:00+09:00", "dbp": 80}
    old = {**current, "latest_vitals": {"at": "2026-10-09T00:00:00+09:00",
                                      "sbp": 180, "dbp": 80},
           "profile_extension": {"text": "架空の補足"},
           "patient_context": {"memo": {"living": [{"text": "古い合成プロフィール"}]}},
           "medications": [{"name": "架空の旧薬", "dose": "旧形式を保持"}]}
    meta = {"period_check_version": rollup.PERIOD_CHECK_VERSION - 1,
            inference.VERSION_KEY: inference.CURRENT_VERSION}
    before = db.db.total_changes
    safe = rollup.current_cached_refs(db.db, 1, old, meta, cfg={})
    assert safe["latest_vitals"] == current["latest_vitals"]
    assert "profile_extension" not in safe
    assert not safe.get("medications")
    assert safe["patient_context"]["memo"]["living"][0]["text"] == "正本の合成プロフィール。"
    assert "古い合成プロフィール" not in json.dumps(safe, ensure_ascii=False)
    assert db.db.total_changes == before


def _add(db, mid, kind, content, posted, *, body="合成本文"):
    db.save_messages([_message(mid=mid, body=body,
                               posted_at=posted)])
    meta = {"hash": _hash(db, mid), inference.VERSION_KEY: inference.CURRENT_VERSION}
    if kind in ("canonical_projection", "semantic_facts_v4"):
        meta["projection_version"] = PROJECTION_VERSION
    db.artifact_add(kind, json.dumps(content), project_id=1,
                    message_id=mid, meta=meta)


def test_rollup_collects_canonical_facts_newest_first(db):
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             _fact("f_old", "allergy_intolerance", "旧アレルギー", quote="旧アレルギー"),
             _fact("f_same", "vital_lab", "BP 110/70", quote="BP 110/70")]},
         "2026-09-18T00:00:00+09:00", body="本人は旧アレルギーがあり、BP 110/70でした。")
    _add(db, 2, "canonical_projection",
         {"canonical_facts": [
             _fact("f_same", "vital_lab", "BP 130/85", quote="130/85"),
             _fact("f_new", "adverse_drug_event", "嘔気", quote="嘔気")]},
         "2026-09-19T00:00:00+09:00", body="本人はBP 130/85で嘔気があります。")
    out = rollup.build_rollup(db.db, 1)
    facts = out["canonical_facts"]
    by_id = {f["fact_id"]: f for f in facts}
    assert set(by_id) == {"f_old", "f_same", "f_new"}
    # newest generation wins per fact_id
    assert by_id["f_same"]["statement"] == "BP 130/85"
    assert by_id["f_same"]["last"].startswith("2026-09-19")
    assert by_id["f_old"]["last"].startswith("2026-09-18")
    # evidence + kind ride along for the readers
    assert by_id["f_new"]["kind"] == "adverse_drug_event"
    assert by_id["f_new"]["evidence"] == "嘔気"


def test_symptom_same_timestamp_preserves_source_and_unknown_time(db):
    # Keep the same-time and missing-time cases: each dated result must
    # retain its own source; missing time cannot establish a current state.
    for mid in (1, 2, 3, 4):
        _add(db, mid, "extract_v1", {"symptoms": [f"合成症状{mid}"]},
             "2026-09-03T00:00:00+09:00", body=f"本人の合成症状{mid}があります。")
    db.db.execute("UPDATE messages SET posted_at_ts=NULL,posted_at='' "
                  "WHERE message_id IN (1,4)")
    out = rollup.build_rollup(db.db, 1)
    assert out["recent_symptoms"] == [
        {"symptom": "合成症状3", "last": "2026-09-03T00:00:00+09:00"},
        {"symptom": "合成症状2", "last": "2026-09-03T00:00:00+09:00"},
    ]
    missing = [fact for fact in out["clinical_state"]["facts"] if fact["message_id"] in (1, 4)]
    assert {fact["message_id"] for fact in missing} == {1, 4}
    assert all(fact["temporal"]["posted_day"] is None for fact in missing)


def test_rollup_canonical_shadows_and_stale_falls_back(db):
    """current_fact_pred: a hash-current projection owns the fact
    source; once the revision changes it drops out and the legacy row
    is read instead."""
    _add(db, 1, "extract_llm", {"meds": [{"name": "旧薬"}]},
         "2026-09-18T00:00:00+09:00", body="本人は旧薬を服用しています。")
    _add(db, 2, "canonical_projection",
         {"meds": [{"name": "新薬", "action": "stop"}],
          "canonical_facts": [_fact("f1", "preference", "午前希望", quote="午前希望")]},
         "2026-09-19T00:00:00+09:00", body="本人は午前希望で、新薬を中止します。")
    out = rollup.build_rollup(db.db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]

    # body revision -> every stored artifact hash goes stale
    db.db.execute("UPDATE messages SET body_text='改訂本文',"
                  " content_hash='newhash' WHERE message_id=2")
    db.db.commit()
    out = rollup.build_rollup(db.db, 1)
    assert out.get("canonical_facts", []) == []


def test_rollup_skips_unlisted_canonical_entries(db):
    """Malformed canonical_facts entries never reach the read model."""
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [
             {"statement": "fact_idなし"},          # no fact_id
             "not-a-dict",
             _fact("f1", "vital_lab", "BP 120/80", quote="BP 120/80")]},
         "2026-09-18T00:00:00+09:00", body="本人のBP 120/80でした。")
    out = rollup.build_rollup(db.db, 1)
    assert [f["fact_id"] for f in out["canonical_facts"]] == ["f1"]


@pytest.mark.parametrize("invalid", ["foreign_patient", "deleted"])
def test_rule_rollup_requires_current_patient_source(db, invalid):
    _add(db, 1, "extract_v1", {"vitals": {"temp": 38.1}, "symptoms": ["合成症状"]},
         "2026-09-18T00:00:00+09:00",
         body="本人の2026-09-18測定で体温38.1℃でした。本人の合成症状があります。")
    # A source-backed symptom proves the fixture was readable before mutation;
    # rule-only measurements alone cannot establish a complete latest value.
    before = rollup.build_rollup(db.db, 1)
    assert before["recent_symptoms"] == [{"symptom": "合成症状", "last": "2026-09-18T00:00:00+09:00"}]
    assert "latest_vitals" not in before
    if invalid == "foreign_patient":
        # Deliberate old-row corruption for the reader-defense assertion.
        db.db.execute("DROP TRIGGER g1_artifacts_msg_upd")
        db.db.execute("UPDATE artifacts SET project_id=2")
    else:
        db.db.execute("UPDATE messages SET body_state='deleted'")
    after = rollup.build_rollup(db.db, 1)
    assert "latest_vitals" not in after
    assert "recent_symptoms" not in after


def test_malformed_lab_container_keeps_other_facts(db):
    _add(db, 1, "extract_llm", {"labs": 7, "summary": "合成要約"},
         "2026-09-18T00:00:00+09:00")
    assert rollup.build_rollup(db.db, 1)["summary"]["text"] == "合成要約"


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm", "canonical_projection"])
def test_rollup_sql_valid_source_at_python_depth_limit_is_safe(db, kind):
    # Keep deep-decode, isolation and no-write checks; a source lacking
    # a completeness binding cannot prove a latest measurement.
    request = ({"kind": "confirm", "ctx": "合成依頼"} if kind == "extract_v1"
               else {"to": "合成看護師", "action": "合成依頼"})
    _add(db, 1, kind, {"vitals": {"temp": 37.1}, "requests": [request]},
         "2026-09-18T00:00:00+09:00",
         body="本人の2026-09-18測定で体温37.1℃でした。合成看護師へ合成依頼をしてください。")
    _add(db, 2, "extract_llm", {"summary": "合成要約",
                                "requests": [{"to": "合成医師", "action": "別の依頼"}]},
         "2026-09-19T00:00:00+09:00", body="合成要約。合成医師へ別の依頼をしてください。")
    blobs = mcs_queries.current_extraction_blobs(db.db, 1)
    decoded = mcs_queries.decode_current_extractions(blobs[1])
    assert (decoded[0] if kind == "extract_v1" else decoded[1])["vitals"]["temp"] == 37.1
    original_out = rollup.build_rollup(db.db, 1)
    assert "latest_vitals" not in original_out
    assert [(row["mid"], row["ctx"]) for row in original_out["recent_requests"]] == [
        (2, "別の依頼"), (1, "合成依頼")]
    source_id, original = db.db.execute("SELECT artifact_id,content FROM artifacts WHERE message_id=1").fetchone()
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert db.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    db.db.commit()
    before = tuple(db.db.iterdump())
    changes = db.db.total_changes
    out = rollup.build_rollup(db.db, 1)
    assert out["msg_count"] == 2
    assert out["summary"]["text"] == "合成要約"
    decoded = mcs_queries.decode_current_extractions(mcs_queries.current_extraction_blobs(db.db, 1)[1])
    if readable:
        assert (decoded[0] if kind == "extract_v1" else decoded[1])["vitals"]["temp"] == 37.1
        assert [(row["mid"], row["ctx"]) for row in out["recent_requests"]] == [
            (2, "別の依頼"), (1, "合成依頼")]
    else:
        assert decoded[0] == decoded[1] == {}
        assert [(row["mid"], row["ctx"]) for row in out["recent_requests"]] == [(2, "別の依頼")]
    assert "latest_vitals" not in out
    assert tuple(db.db.iterdump()) == before
    assert db.db.total_changes == changes


@pytest.mark.parametrize(("field", "value"), [
    ("generated_at", "broken"), ("generated_at", float("inf")),
    ("next_med_period_check", "broken"),
])
def test_invalid_rollup_schedule_is_rebuilt(db, field, value):
    _add(db, 1, "extract_v1", {}, "2026-09-18T00:00:00+09:00")
    rollup.rebuild(db, 1)
    row = db.db.execute("SELECT meta FROM artifacts WHERE kind=?",
                        (rollup.KIND,)).fetchone()
    meta = json.loads(row[0])
    meta[field] = value
    db.db.execute("UPDATE artifacts SET meta=? WHERE kind=?",
                  (json.dumps(meta), rollup.KIND))
    assert rollup.dirty_projects(db) == [1]


@pytest.mark.parametrize("flag", [True, "false", 0, None, "yes"])
def test_rollup_keeps_item_unverified_flag(db, flag):
    """Unverified requests keep their flag through the rollup; anything
    but a literal False (or a missing key) fails closed (todo 15)."""
    _add(db, 1, "canonical_projection",
         {"requests": [
             {"to": "SYNTH-医師", "action": "確認済み依頼",
              "unverified": False},
             {"to": "SYNTH-薬局", "action": "未確認依頼",
              "unverified": flag},
             {"to": "SYNTH-訪看", "action": "フラグ無し依頼"}]},
         "2026-09-19T00:00:00+09:00",
         body="医師へ確認済み依頼、薬局へ未確認依頼、訪看へフラグ無し依頼をお願いします。")
    out = rollup.build_rollup(db.db, 1)
    assert [(r["ctx"], r["unverified"]) for r in out["recent_requests"]] \
        == [("確認済み依頼", False), ("未確認依頼", True),
            ("フラグ無し依頼", False)]


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection"])
@pytest.mark.parametrize("flag", [True, "false", 0, None, [], {}])
def test_rollup_keeps_lab_verification_markers(db, monkeypatch, kind, flag):
    # Keep each original flag and its source while separately proving that
    # an unverified value cannot become a confirmed latest measurement.
    _complete_history(db, monkeypatch)
    posted = "2026-10-01T09:00:00+09:00"
    body = ("本人の2026-10-01採血でCr 1mg/dLでした。"
            "本人の2026-10-01採血でHb 2g/dLでした。"
            "本人の2026-10-01採血でAlb 3g/dLでした。")
    labs = [{"name": "Cr", "value": 1, "unit": "mg/dL", "unverified": False,
             "evidence": "本人の2026-10-01採血でCr 1mg/dLでした。", "measured_on": "2026-10-01"},
            {"name": "Hb", "value": 2, "unit": "g/dL", "unverified": flag,
             "evidence": "本人の2026-10-01採血でHb 2g/dLでした。", "measured_on": "2026-10-01"},
            {"name": "Alb", "value": 3, "unit": "g/dL",
             "evidence": "本人の2026-10-01採血でAlb 3g/dLでした。", "measured_on": "2026-10-01"}]
    db.save_messages([_message(mid=1, body=body, posted_at=posted)])
    _save_bound_extraction(db, 1, {"labs": labs})
    if kind == "canonical_projection":
        db.artifact_add(kind, json.dumps({"labs": labs}), project_id=1, message_id=1,
                        meta={"hash": _hash(db, 1), "projection_version": PROJECTION_VERSION,
                              inference.VERSION_KEY: inference.CURRENT_VERSION})
    decoded = mcs_queries.decode_current_extractions(mcs_queries.current_extraction_blobs(db.db, 1)[1])
    assert decoded[1]["labs"] == labs
    out = rollup.build_rollup(db.db, 1, cfg={})
    if kind == "extract_llm":
        assert out["measurement_coverage"]["complete"] is True
        assert out["recent_labs"] == [{"at": posted, **labs[0]}, {"at": posted, **labs[2]}]
    else:
        assert out["measurement_coverage"]["complete"] is False
        assert out["measurement_coverage"]["unbound_sources"] == 1
        assert "recent_labs" not in out


def test_rollup_carries_request_kind_condition_due_text(db):
    """#20 order 3: LLM request rows expose kind/condition/due_text under
    NEW keys (req_kind/condition/due_text); kind (=to) and ctx are
    untouched, rows without them gain no keys, and no version bump means
    the rebuilt rollup is not re-dirtied."""
    _add(db, 1, "canonical_projection",
         {"requests": [
             {"to": "SYNTH-看護師", "action": "医師へ連絡", "kind": "request",
              "condition": "血圧が160を超えるようなら", "due": None,
              "due_text": "明日まで"},
             {"to": "SYNTH-医師", "action": "素の依頼"}]},
         "2026-09-19T00:00:00+09:00",
         body="血圧が160を超えるようなら、看護師は明日までに医師へ連絡してください。医師へ素の依頼をします。")
    out = rollup.build_rollup(db.db, 1)
    typed, plain = out["recent_requests"]
    assert (typed["kind"], typed["ctx"]) == ("SYNTH-看護師", "医師へ連絡")
    assert (typed["req_kind"], typed["condition"], typed["due_text"]) \
        == ("request", "血圧が160を超えるようなら", "明日まで")
    assert set(plain) == {"kind", "ctx", "at", "mid", "unverified"}
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []


def _thread(db, rows, *, bodies=None):
    """rows: (mid, parent, sender, posted, content) -> messages + LLM
    artifacts; the root request row is mid 1 by SYNTH-A."""
    bodies = bodies or {1: _REQUEST_BODY, 2: _DONE_BODY}
    for mid, parent, sender, posted, content in rows:
        msg = _message(mid=mid, body=bodies.get(mid, "合成本文"), parent_id=parent,
                       posted_at=posted)
        msg.sender_name = sender
        msg.sender_id = {"SYNTH-A": 1, "SYNTH-B": 2, "SYNTH-C": 3}[sender]
        db.save_messages([msg])
        db.artifact_add("extract_llm", json.dumps(content), project_id=1,
                        message_id=mid,
                        meta={"hash": _hash(db, mid), inference.VERSION_KEY: inference.CURRENT_VERSION})


def _seed_relations(db, origin, action, events, *, local_model=None):
    """Store only explicitly specified relations against matching synthetic source text."""
    import hashlib
    import semantic_store

    source = db.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=?", (origin,)).fetchone()
    body = source["body_text"]
    assert body.count(action) == 1
    start = body.index(action)
    root = source["parent_id"] or source["message_id"]
    bundle = semantic_store.thread_bundle(db.db, 1, root, local_model=local_model)
    assert bundle
    policy = hashlib.sha256(b"synthetic-explicit-relations").hexdigest()
    db.artifact_add("semantic_policy", policy)
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION,
            "fingerprint": bundle["source_fingerprint"], "policy_fingerprint": policy}
    evidence = {"evidence_id": "synthetic", "message_id": origin,
                "revision_id": source["content_hash"], "start_codepoint": start,
                "end_codepoint": start + len(action), "quote": action}
    candidate = {"description": action, "origin": {"message_id": origin,
                 "revision": source["content_hash"], "evidence_refs": ["synthetic"],
                 "evidence": evidence}, "state": "PROPOSED", "root_id": root,
                 "project_id": 1, "loop_id": f"synthetic-{origin}-{action}"}
    aid = db.artifact_add("loop_candidate", json.dumps(candidate), project_id=1, message_id=origin, meta=meta)
    for mid, relation, quote in events:
        trigger = db.db.execute("SELECT * FROM messages WHERE project_id=1 AND message_id=?", (mid,)).fetchone()
        assert trigger is not None and quote in trigger["body_text"]
        event = {"loop_artifact_id": aid, "loop_origin_id": origin,
                 "origin_revision": source["content_hash"], "trigger_message_id": mid,
                 "trigger_revision": trigger["content_hash"], "relation": relation,
                 "reported_relation": relation, "confidence": .9}
        db.artifact_add("loop_event", json.dumps(event), project_id=1, message_id=mid, meta=meta)


_REQUEST_BODY = "処方変更を確認してください。"
_DONE_BODY = "処方変更を確認しました。合成本文"
_ACK_BODY = "処方変更の確認を承知しました。合成本文"
_CANCEL_BODY = "処方変更の確認を取り消します。合成本文"
_DONE = (2, "completion_report", "処方変更を確認しました")


_REQ = {"requests": [{"to": "SYNTH-医師", "action": "処方変更を確認"}]}


def _reply(kind):
    return {"reply": {"kind": kind, "evidence": "合成本文"}}


@pytest.mark.parametrize(("replies", "bodies", "events", "state", "conflict"), [
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done"))], {2: _DONE_BODY}, [_DONE], "done", False),
    # the requester's own 「承知しました」 never answers the request
    ([(2, 1, "SYNTH-A", "T01:00", _reply("ack"))], {2: _ACK_BODY},
     [(2, "acknowledges", "処方変更の確認を承知しました")], None, False),
    ([(2, 1, "SYNTH-B", "T01:00", _reply("ack")),
      (3, 1, "SYNTH-B", "T02:00", _reply("done"))], {2: _ACK_BODY, 3: _DONE_BODY},
     [(2, "acknowledges", "処方変更の確認を承知しました"), (3, "completion_report", "処方変更を確認しました")], "done", False),
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done")),
      (3, 1, "SYNTH-C", "T02:00", _reply("cancel"))], {2: _DONE_BODY, 3: _CANCEL_BODY},
     [_DONE, (3, "cancellation_report", "処方変更の確認を取り消します")], "cancel", True),
    # done -> cancel -> done: the cancel is later than the FIRST done,
    # so it still conflicts (the walk is newest-first)
    ([(2, 1, "SYNTH-B", "T01:00", _reply("done")),
      (3, 1, "SYNTH-C", "T02:00", _reply("cancel")),
      (4, 1, "SYNTH-B", "T03:00", _reply("done"))], {2: _DONE_BODY, 3: _CANCEL_BODY, 4: _DONE_BODY},
     [_DONE, (3, "cancellation_report", "処方変更の確認を取り消します"),
      (4, "completion_report", "処方変更を確認しました")], "cancel", True),
    # reply on an unrelated thread (root 9) is not this request's
    ([(9, None, "SYNTH-B", "T01:00", {}),
      (10, 9, "SYNTH-C", "T02:00", _reply("done"))], {10: _DONE_BODY},
     [(10, "completion_report", "処方変更を確認しました")], None, False),
    # a reply posted BEFORE the request never counts
    ([(2, 1, "SYNTH-B", "T00:00", _reply("done"))], {2: _DONE_BODY}, [_DONE], None, False),
])
def test_rollup_thread_reply_state(db, replies, bodies, events, state, conflict):
    """Only an explicitly sourced Loop relation can establish a later reply."""
    day = "2026-09-19"
    _thread(db, [(1, None, "SYNTH-A", f"{day}T00:30:00+09:00", _REQ)] + [
        (mid, parent, sender, f"{day}{hm}:00+09:00", content)
        for mid, parent, sender, hm, content in replies], bodies={1: _REQUEST_BODY, **bodies})
    _seed_relations(db, 1, "処方変更を確認", events)
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert len(rows) == 1 and rows[0]["mid"] == 1
    assert rows[0].get("reply_state") == state
    assert rows[0].get("reply_conflict", False) is conflict
    assert rows[0]["unverified"] is False


def test_rollup_reply_survives_canonical_projection_shadowing(db):
    """A current canonical_projection shadows extract_llm as the fact
    source for that message; an independent current Loop relation remains usable."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    db.artifact_add("canonical_projection", json.dumps({"canonical_facts": []}),
                    project_id=1, message_id=2,
                    meta={"hash": _hash(db, 2), "projection_version": PROJECTION_VERSION,
                          inference.VERSION_KEY: inference.CURRENT_VERSION})
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert rows[0]["reply_state"] == "done"


def test_rollup_reply_work_is_bounded_by_returned_requests(db, monkeypatch):
    """Mixed rule/LLM requests keep their cap/order; old rows need no scan."""
    requests = [{"to": "SYNTH", "action": f"合成依頼{i:03d}"} for i in range(120)]
    # Source sentences independently identify a single request per reply.
    bodies = {1: "。".join([f"合成依頼{i:03d}をお願いします" for i in range(120)]
                              + [f"合成ルール{i}をお願いします" for i in range(4)]) + "。"}
    bodies.update({mid: f"合成依頼{mid - 2:03d}の確認を完了しました。合成本文" for mid in range(2, 22)})
    replies = [(mid, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", _reply("done"))
               for mid in range(2, 22)]
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00",
                  {"requests": requests}), *replies], bodies=bodies)
    # Independently specified relation table: each reply refers to one request.
    pairs = [(0, 2), (1, 3), (2, 4), (3, 5), (4, 6), (5, 7), (6, 8), (7, 9),
             (8, 10), (9, 11), (10, 12), (11, 13), (12, 14), (13, 15), (14, 16),
             (15, 17), (16, 18), (17, 19), (18, 20), (19, 21)]
    for index, mid in pairs:
        _seed_relations(db, 1, f"合成依頼{index:03d}",
                        [(mid, "completion_report", f"合成依頼{index:03d}の確認を完了しました")])
    db.artifact_add("extract_v1", json.dumps(
        {"requests": [{"kind": "rule", "ctx": f"合成ルール{i}"} for i in range(4)]}),
        project_id=1, message_id=1,
        meta={"hash": _hash(db, 1), inference.VERSION_KEY: inference.CURRENT_VERSION})
    calls = []

    class Stages(dict):
        def get(self, key, default=None):
            calls.append(key)
            return super().get(key, default)

    monkeypatch.setattr(rollup, "_REPLY_STAGE", Stages(rollup._REPLY_STAGE))
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert [row["ctx"] for row in rows] == (
        [f"合成ルール{i}" for i in range(4)] + [f"合成依頼{i:03d}" for i in range(11)])
    assert [row["reply_state"] for row in rows[4:]] == ["done"] * 11
    assert calls == ["done"] * 11
    # Removing the cap reaches all 20 annotated relations, and the next
    # request has no relation despite sharing the same thread.
    calls.clear()
    full = rollup.build_rollup(db.db, 1, request_limit=None)["recent_requests"]
    assert [row["ctx"] for row in full[4:25]] == [f"合成依頼{i:03d}" for i in range(21)]
    assert [row["reply_state"] for row in full[4:24]] == ["done"] * 20
    assert "reply_state" not in full[24]
    assert calls == ["done"] * 20


def test_rollup_sql_valid_shadowed_reply_at_python_depth_limit_is_safe(db):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", _reply("done"))])
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    db.artifact_add("canonical_projection", json.dumps({"canonical_facts": []}),
                    project_id=1, message_id=2,
                    meta={"hash": _hash(db, 2), "projection_version": PROJECTION_VERSION,
                          inference.VERSION_KEY: inference.CURRENT_VERSION})
    source_id, original = db.db.execute(
        "SELECT artifact_id,content FROM artifacts WHERE kind='extract_llm' AND message_id=2"
    ).fetchone()
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert db.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        pass
    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    db.db.commit()
    before = tuple(db.db.iterdump())
    changes = db.db.total_changes
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    # Independent current Loop evidence remains usable when a legacy blob is unreadable.
    assert rows[0].get("reply_state") == "done"
    assert tuple(db.db.iterdump()) == before
    assert db.db.total_changes == changes


def test_rollup_ignores_non_string_reply_kind(db):
    """A foreign/hand-written extract_llm row with reply.kind as a list
    must not abort the patient's rollup (unhashable in the stage set)."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  {"reply": {"kind": ["done"], "evidence": "x"}})])
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert rows[0].get("reply_state") is None


def test_rollup_reply_without_relation_is_not_attributed(db):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00", _reply("done"))])
    # The body and reply.kind alone are not a request-specific Loop relation.
    assert "reply_state" not in rollup.build_rollup(db.db, 1)["recent_requests"][0]


def test_rollup_reply_with_unparseable_posted_at_is_never_later(db):
    """NULL posted_at_ts maps to 0 and can never be 'later' than the
    request, so a reply with an unparseable date sets nothing."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "not-a-date", _reply("done"))])
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert "reply_state" not in rows[0]


@pytest.mark.parametrize(("request_id", "reply_id", "same_name", "state"), [
    (1, 2, True, "done"),       # identical display names, distinct people
    (1, 1, False, None),        # renamed sender, same person
    (None, 2, False, None),     # unknown identities cannot prove a reply
    (1, None, False, None),
    (None, None, False, None),
    (0, 2, False, None),
])
def test_rollup_reply_uses_sender_ids(db, request_id, reply_id, same_name, state):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    db.db.execute("UPDATE messages SET sender_id=? WHERE message_id=1",
                  (request_id,))
    db.db.execute("UPDATE messages SET sender_id=? WHERE message_id=2",
                  (reply_id,))
    if same_name:
        db.db.execute("UPDATE messages SET sender_name='SYNTH-A'")
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    assert rollup.build_rollup(db.db, 1)["recent_requests"][0].get("reply_state") \
        == state


def test_rollup_old_name_based_reply_is_rebuilt(db):
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    rollup.rebuild(db, 1)
    db.db.execute("UPDATE messages SET sender_id=1 WHERE message_id=2")
    db.db.execute("UPDATE artifacts SET meta=json_set(meta,"
                  " '$.period_check_version',2) WHERE kind=?", (rollup.KIND,))
    assert rollup.dirty_projects(db) == [1]
    db.db.commit()
    rollup.rebuild(db, 1)
    content = db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                            (rollup.KIND,)).fetchone()[0]
    assert "reply_state" not in json.loads(content)["recent_requests"][0]
    assert rollup.dirty_projects(db) == []


def test_pre_flag_rollup_is_rebuilt_with_unverified_flag(db):
    """A rollup persisted before todo 15 (version-1 meta, no
    'unverified' keys) is dirty once and rebuilt with the flag; the
    rebuilt row is not re-dirtied (no rebuild loop)."""
    _add(db, 1, "canonical_projection",
         {"requests": [{"to": "SYNTH-薬局", "action": "未確認依頼",
                        "unverified": True}]},
         "2026-09-19T00:00:00+09:00", body="薬局へ未確認依頼をお願いします。")
    rollup.rebuild(db, 1)
    content, meta = db.db.execute(
        "SELECT content, meta FROM artifacts WHERE kind=?",
        (rollup.KIND,)).fetchone()
    content, meta = json.loads(content), json.loads(meta)
    for r in content["recent_requests"]:
        del r["unverified"]
    meta["period_check_version"] = 1
    db.db.execute("UPDATE artifacts SET content=?, meta=? WHERE kind=?",
                  (json.dumps(content), json.dumps(meta), rollup.KIND))
    assert rollup.dirty_projects(db) == [1]

    db.db.commit()
    for _ in range(2):  # an interrupted tick retrying stays idempotent
        rollup.rebuild(db, 1)
        rows = db.db.execute("SELECT content FROM artifacts WHERE kind=?",
                             (rollup.KIND,)).fetchall()
        assert len(rows) == 1
        assert [r["unverified"] for r in
                json.loads(rows[0][0])["recent_requests"]] == [True]
        assert rollup.dirty_projects(db) == []


@pytest.mark.parametrize("shadow, decodes", [(False, 2), (True, 4)])
def test_rollup_decodes_each_llm_blob_once(db, monkeypatch, shadow, decodes):
    """Current Loop relations survive projection shadowing without redundant decoding."""
    _thread(db, [(1, None, "SYNTH-A", "2026-09-19T00:30:00+09:00", _REQ),
                 (2, 1, "SYNTH-B", "2026-09-19T01:00:00+09:00",
                  _reply("done"))])
    _seed_relations(db, 1, "処方変更を確認", [_DONE])
    if shadow:
        for mid in (1, 2):
            db.artifact_add("canonical_projection", json.dumps(
                {"requests": _REQ["requests"]} if mid == 1 else {}),
                project_id=1, message_id=mid,
                meta={"hash": _hash(db, mid), "projection_version": PROJECTION_VERSION,
                      inference.VERSION_KEY: inference.CURRENT_VERSION})
    extraction_blobs = {r[0] for r in db.db.execute(
        "SELECT content FROM artifacts WHERE kind IN ('extract_llm','canonical_projection')")}
    calls = []
    real = json.loads
    monkeypatch.setattr(rollup.json, "loads",
                        lambda s, *a, **k: calls.append(s) or real(s, *a, **k))
    rows = rollup.build_rollup(db.db, 1)["recent_requests"]
    assert rows[0]["reply_state"] == "done"
    assert sum(s in extraction_blobs for s in calls) == decodes


def test_symptoms_equal_instants_different_offsets_keep_source_dates(db):
    for mid in (1, 2, 3, 4):
        # The two offsets represent the same instant, not inconsistent fields.
        posted = ("2026-09-02T15:00:00+00:00" if mid == 2
                  else "2026-09-03T00:00:00+09:00")
        _add(db, mid, "extract_v1", {"symptoms": [f"合成症状{mid}"]},
             posted,
             body=f"本人に合成症状{mid}があります。")
    stamps = [row[0] for row in db.db.execute(
        "SELECT posted_at_ts FROM messages WHERE message_id IN (2,3) ORDER BY message_id")]
    assert len(stamps) == 2 and stamps[0] == stamps[1] and stamps[0] is not None
    db.db.execute("UPDATE messages SET posted_at_ts=NULL,posted_at='' "
                  "WHERE message_id IN (1,4)")
    out = rollup.build_rollup(db.db, 1)
    assert out["recent_symptoms"] == [
        {"symptom": "合成症状3", "last": "2026-09-03T00:00:00+09:00"},
        {"symptom": "合成症状2", "last": "2026-09-02T15:00:00+00:00"},
    ]
    facts = {fact["message_id"]: fact for fact in out["clinical_state"]["facts"]
             if fact["category"] == "symptoms"}
    assert set(facts) == {1, 2, 3, 4}
    assert [facts[mid]["attributes"]["text"] for mid in (1, 2, 3, 4)] == [
        "合成症状1", "合成症状2", "合成症状3", "合成症状4"]
    groups = out["clinical_state"]["groups"]
    assert {facts[mid]["id"] for mid in (2, 3)} <= set(groups["current"])
    assert {facts[mid]["id"] for mid in (1, 4)} <= set(groups["unknown"])
    assert all(facts[mid]["temporal"]["posted_at_ts"] is None for mid in (1, 4))
    assert all(facts[mid]["source_ref"]["artifact_id"] for mid in (1, 2, 3, 4))


@pytest.mark.parametrize("invalid", ["legacy", "missing_version", "wrong_hash"])
def test_canonical_fact_requires_current_generation_and_source(db, invalid):
    _add(db, 1, "canonical_projection",
         {"canonical_facts": [_fact("f1", "preference", "本人は午前を希望します", quote="本人は午前を希望します")]},
         "2026-09-18T00:00:00+09:00", body="本人は午前を希望します。")
    assert [f["fact_id"] for f in rollup.build_rollup(db.db, 1)["canonical_facts"]] == ["f1"]
    artifact, meta = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='canonical_projection'").fetchone()
    meta = json.loads(meta)
    if invalid == "legacy":
        meta[inference.VERSION_KEY] = "0.1"
    elif invalid == "missing_version":
        del meta[inference.VERSION_KEY]
    else:
        meta["hash"] = "0" * 64
    db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), artifact))
    assert rollup.build_rollup(db.db, 1).get("canonical_facts", []) == []


@pytest.mark.parametrize("kind", ["extract_v1", "extract_llm", "canonical_projection"])
def test_rollup_depth_limit_preserves_bound_vital_or_other_source_fields(db, monkeypatch, kind):
    body = "本人の2026-09-17測定で体温37.1℃でした。"
    if kind == "extract_llm":
        _complete_history(db, monkeypatch)
        db.save_messages([_message(mid=1, body=body, posted_at="2026-09-18T00:00:00+09:00")])
        _save_bound_extraction(db, 1, {"vitals": {"bt": 37.1}})
    else:
        content = ({"next_planned": "合成訪問の予定"} if kind == "extract_v1"
                   else {"summary": "合成要約"})
        _add(db, 1, kind, content, "2026-09-18T00:00:00+09:00", body=body + "合成訪問の予定。合成要約。")
    initial = rollup.build_rollup(db.db, 1, cfg={})
    if kind == "extract_llm":
        assert initial["latest_vitals"]["bt"] == 37.1
    elif kind == "extract_v1":
        assert not initial.get("latest_vitals")
        assert initial["next_planned"] == "合成訪問の予定"
    else:
        assert not initial.get("latest_vitals")
        assert initial["summary"]["text"] == "合成要約"
    source_id, original = db.db.execute(
        "SELECT artifact_id,content FROM artifacts WHERE kind=? AND message_id=1", (kind,)).fetchone()
    deep = original[:-1] + ',"synthetic_unused":' + "[" * 999 + "0" + "]" * 999 + "}"
    assert db.db.execute("SELECT json_valid(?)", (deep,)).fetchone()[0] == 1
    try:
        json.loads(deep)
    except RecursionError:
        readable = False
    else:
        readable = True
    db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (deep, source_id))
    db.db.commit()
    before = tuple(db.db.iterdump())
    changes = db.db.total_changes
    out = rollup.build_rollup(db.db, 1, cfg={})
    assert out["msg_count"] == 1
    if readable and kind == "extract_llm":
        assert out["latest_vitals"]["bt"] == 37.1
    elif not readable:
        assert "latest_vitals" not in out
        assert "next_planned" not in out and "summary" not in out
    elif kind == "extract_v1":
        assert out["next_planned"] == "合成訪問の予定" and "latest_vitals" not in out
    else:
        assert out["summary"]["text"] == "合成要約" and "latest_vitals" not in out
    assert tuple(db.db.iterdump()) == before
    assert db.db.total_changes == changes


def test_rollup_decodes_in_bounded_pages_and_preserves_source_order(monkeypatch):
    messages = [{'message_id': mid} for mid in range(130)]
    arts = {mid: {'extract_llm': str(mid)} for mid in range(130)}
    decoded, pages = [], []

    def decode(blobs):
        mid = int(blobs['extract_llm'])
        decoded.append(mid)
        return ({}, {'id': mid}, {}, True)

    def bound(db, rows, kinds, refs, cfg, *, expected_model):
        pages.append([row['message_id'] for row in rows])
        assert kinds == ['extract_llm'] * len(rows)
        return [row['message_id'] % 2 == 0 for row in rows]

    monkeypatch.setattr(rollup, 'decode_current_extractions', decode)
    monkeypatch.setattr(rollup, '_measurement_sources_bound', bound)
    inputs = rollup._rollup_inputs(None, messages, arts, {}, None)
    first = next(inputs)
    assert first[0]['message_id'] == 0 and first[-1] is True
    assert decoded == list(range(64))
    rest = list(inputs)
    assert decoded == list(range(130))
    assert [len(page) for page in pages] == [64, 64, 2]
    assert [entry[0]['message_id'] for entry in [first, *rest]] == list(range(130))
    assert [entry[-1] for entry in [first, *rest]] == [mid % 2 == 0 for mid in range(130)]



def test_rollup_prior_day_contradictory_vitals_not_restored_from_cache(db, monkeypatch):
    body = "本人の2026-10-02測定で血圧120/80を測定しました。"
    _complete_history(db, monkeypatch)
    _save_karte_profile(db)
    db.save_messages([_message(mid=1, body=body, posted_at="2026-10-09T00:00:00+09:00")])
    _save_bound_extraction(db, 1, {"vitals": {"sbp": 180, "dbp": 80}})
    current = rollup.build_rollup(db.db, 1, cfg={})
    assert current["latest_vitals"] == {"at": "2026-10-09T00:00:00+09:00", "dbp": 80}
    old = {**current, "latest_vitals": {"at": "2026-10-09T00:00:00+09:00",
                                      "sbp": 180, "dbp": 80},
           "profile_extension": {"text": "架空の補足"},
           "patient_context": {"memo": {"living": [{"text": "古い合成プロフィール"}]}},
           "medications": [{"name": "架空の旧薬", "dose": "旧形式を保持"}]}
    meta = {inference.VERSION_KEY: inference.CURRENT_VERSION, "period_check_version": rollup.PERIOD_CHECK_VERSION - 1}
    before = db.db.total_changes
    safe = rollup.current_cached_refs(db.db, 1, old, meta, cfg={})
    assert safe["latest_vitals"] == current["latest_vitals"]
    assert "profile_extension" not in safe
    assert not safe.get("medications")
    assert safe["patient_context"]["memo"]["living"][0]["text"] == "正本の合成プロフィール。"
    assert "古い合成プロフィール" not in json.dumps(safe, ensure_ascii=False)
    assert db.db.total_changes == before


@pytest.mark.parametrize("kind", ["extract_llm", "canonical_projection"])
@pytest.mark.parametrize("flag", [True, "false", 0, None, [], {}])
def test_rollup_lab_flag_retention_with_prior_measurement_day(db, monkeypatch, kind, flag):
    day = "2026-09-30"
    quotes = [f"本人の{day}採血でCr 1 mg/dLでした。",
              f"本人の{day}採血でBUN 2 mg/dLでした。",
              f"本人の{day}採血でNa 3 mEq/Lでした。"]
    labs = [{"name": name, "value": value, "unit": unit, "evidence": quote,
             "subject": "patient", "status": "current", "measured_on": day}
            for name, value, unit, quote in zip(("Cr", "BUN", "Na"), (1, 2, 3),
                                                ("mg/dL", "mg/dL", "mEq/L"), quotes)]
    labs[0]["unverified"] = False
    labs[1]["unverified"] = flag
    body, posted = "".join(quotes), "2026-10-01T00:00:00+09:00"
    if kind == "extract_llm":
        _complete_history(db, monkeypatch)
        db.save_messages([_message(mid=1, body=body, posted_at=posted)])
        _save_bound_extraction(db, 1, {"labs": labs})
    else:
        _add(db, 1, kind, {"labs": labs}, posted, body=body)
    source = db.db.execute("SELECT content FROM artifacts WHERE kind=? AND message_id=1", (kind,)).fetchone()
    assert json.loads(source[0])["labs"] == labs
    out = rollup.build_rollup(db.db, 1, cfg={})
    latest = out.get("recent_labs", [])
    if kind == "extract_llm":
        assert [item["name"] for item in latest] == ["Cr", "Na"]
        assert latest[0]["unverified"] is False
        assert "unverified" not in latest[1]
        assert all(item["at"] == posted for item in latest)
        assert out["measurement_coverage"]["complete"] is True
    else:
        # A canonical projection without its independent source/policy proof
        # remains visible in the saved artifact but is not a current measurement.
        assert latest == [] and out["measurement_coverage"]["complete"] is False
    assert all(item["name"] != "BUN" for item in latest)


# ---------- 連携サマリー (#21) ----------------------------------------------

def _summary(comment="合成サマリー"):
    return {"comment": comment, "updated_at": "2026-09-30T10:00:00+09:00",
            "is_editable": True,
            "user": {"profession": "看護師", "name": "合成 花子"}}


def test_rollup_carries_karte_summary_or_none(db):
    db.save_messages([_message(mid=1, body="合成本文",
                               posted_at="2026-09-01T09:00:00+09:00")])
    assert rollup.build_rollup(db.db, 1)["karte_summary"] is None
    db.karte_summary_store(1, 10, None)                    # unregistered
    ks = rollup.build_rollup(db.db, 1)["karte_summary"]
    assert ks["empty"] is True and ks["comment"] is None
    assert isinstance(ks["fetched_at"], float)
    db.karte_summary_store(1, 10, _summary())
    ks = rollup.build_rollup(db.db, 1)["karte_summary"]
    assert ks == {"comment": "合成サマリー",
                  "updated_at": "2026-09-30T10:00:00+09:00",
                  "updater_profession": "看護師", "empty": False,
                  "fetched_at": ks["fetched_at"]}
    assert "合成 花子" not in json.dumps(ks, ensure_ascii=False)


def test_new_karte_summary_dirties_rollup(db):
    db.save_messages([_message(mid=1, body="合成本文",
                               posted_at="2026-09-01T09:00:00+09:00")])
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []
    db.karte_summary_store(1, 10, _summary())
    assert rollup.dirty_projects(db) == [1]
    rollup.rebuild(db, 1)
    assert rollup.dirty_projects(db) == []
