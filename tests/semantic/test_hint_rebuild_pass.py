"""Stored canonical documents built before the evidence-span hint rule
are re-derived from their cached chunks only: an unchanged document keeps
every derived row; a changed or unrebuildable one stops its projection,
summary, audits, coverage and Loop candidates from being current and is
re-queued at most once without a new notification or retry budget.
Synthetic ledger, stub model and stub Jev only."""
import json
import time

import pytest

import semantic
import semantic_extraction as se
import semantic_loops
import semantic_send_gate
import semantic_v4 as v4
from semantic_testkit import NO_FACTS, _cfg, _kind_rows, _ledger, _message, _patient, _PassJev

HELD = ("semantic_summary", "semantic_audit", "semantic_coverage",
        "semantic_facts_audit")


def _fact(statement, quote):
    return {"statement": statement, "kind": "medication_exposure",
            "subject_role": "patient", "polarity": "affirmed",
            "epistemic": "reported", "workflow_status": "performed",
            "importance": "T1", "evidence_quote": quote}


class _Llm:
    """Message 1's hint lies inside the model evidence (a true merge);
    message 2's hint is elsewhere in the body (merged only by the old rule)."""
    def __init__(self):
        self.extractions = 0

    def __call__(self, prompt):
        if "要約器" in prompt:
            return json.dumps({"claims": [], "limitations": []})
        self.extractions += 1
        if "アムロジピン" in prompt:
            facts = [_fact("アムロジピン 5mg", "アムロジピン5mgを継続")]
        elif "メトホルミン" in prompt:
            facts = [_fact("メトホルミン 500mg", "500mgが出ています")]
        else:
            return json.dumps({"facts": [], "category_presence": NO_FACTS})
        return json.dumps({"facts": facts,
                           "category_presence": dict(NO_FACTS, medication="one")},
                          ensure_ascii=False)


def _config():
    return _cfg("enforce", loop_mode="off", fact_source="canonical",
                fact_source_gate="g6-v1:test")


def _drain(db, llm=None, jev=None):
    return semantic.run_due(db, _config(), {"errors": []}, time.monotonic() + 300,
                            jev_client=jev or _PassJev(), llm_fn=llm or _Llm())


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Two PASS targets drained with the pre-span merge, stored without a
    build the way documents written before this change are."""
    db = _ledger(tmp_path)
    patient = _patient(db)
    patient.messages = [_message(1, body="アムロジピン5mgを継続します。")]
    patient.messages[0].replies = [_message(2, parent=1, body="メトホルミン500mgが出ています。")]
    db.save_patient(patient, notify={"source": "unread"}, semantic=True)
    with monkeypatch.context() as old_rule:
        old_rule.setattr(se, "_hint_target", lambda candidates, hint:
                         candidates[-1] if candidates else None)
        _drain(db)
    db.db.execute("UPDATE artifacts SET meta=json_remove(meta,'$.build') "
                  "WHERE kind='semantic_facts_v2'")
    db.db.commit()
    yield db
    db.close()


def _scfg():
    return semantic.semantic_config(_config())[0]


def _meta(db, kind, mid):
    return [json.loads(r["meta"]) for r in _kind_rows(db, kind, mid)]


def _job(db):
    row = db.db.execute("SELECT state,payload FROM fetch_jobs WHERE kind='semantic'").fetchone()
    return row["state"], json.loads(row["payload"])


def _outbox(db):
    return db.db.execute("SELECT count(*) FROM notify_outbox").fetchone()[0]


def test_old_documents_are_marked_and_only_the_changed_one_is_held(world):
    db = world
    out = v4.reproject_stale(db, _scfg())
    assert out["hint_rebuild"] == {"same": 1, "replaced": 1, "skipped": 0,
                                   "history": 0, "seeded": 1}
    # message 1: the true merge is unchanged, nothing derived moves
    [same] = _meta(db, "semantic_facts_v2", 1)
    assert same["build"] == se.FACTS_V2_BUILD and same["hint_rebuild"] == "same"
    for kind in (v4.KIND_V4, *HELD):
        assert all(not m.get("invalidated") and not m.get("stale")
                   for m in _meta(db, kind, 1)), kind
    # message 2: a new document separates the hint; old derivatives held
    old, new = _kind_rows(db, "semantic_facts_v2", 2)
    assert json.loads(old["meta"])["hint_rebuild"] == "replaced"
    new_meta = json.loads(new["meta"])
    assert new_meta["build"] == se.FACTS_V2_BUILD
    assert new_meta["rebuilt_from"] == old["artifact_id"]
    assert sorted(f["provenance"] for f in json.loads(new["content"])["facts"]) \
        == ["extract_v1", "local_llm"]
    for kind in (v4.KIND_V4, *HELD):
        assert all(m.get("invalidated") is True for m in _meta(db, kind, 2)), kind
    assert all(m.get("stale") is True for m in _meta(db, "semantic_summary", 2))
    assert all("stale" not in m for m in _meta(db, "semantic_audit", 2))
    state, payload = _job(db)
    assert state == "pending" and payload["notification_free"] is True
    # the send gate holds message 2's summary and still publishes message 1's
    policy = semantic.policy_fingerprint(_scfg())
    bundle = semantic.thread_bundle(db, 1, 1)
    assert semantic_send_gate._summary_current(db, 1, 1, bundle, policy) is not None
    assert semantic_send_gate._summary_current(db, 1, 2, bundle, policy) is None


def test_second_pass_does_nothing(world):
    db = world
    v4.reproject_stale(db, _scfg())
    rows = db.db.execute("SELECT artifact_id,meta FROM artifacts ORDER BY artifact_id").fetchall()
    assert "hint_rebuild" not in v4.reproject_stale(db, _scfg())
    assert db.db.execute("SELECT artifact_id,meta FROM artifacts "
                         "ORDER BY artifact_id").fetchall() == rows


def test_requeued_thread_republishes_from_the_rebuilt_document_without_a_notice(world):
    db = world
    v4.reproject_stale(db, _scfg())
    outbox = _outbox(db)
    llm = _Llm()
    _drain(db, llm=llm)
    assert llm.extractions == 0                         # rebuilt doc reused
    [new_doc] = _kind_rows(db, "semantic_facts_v2", 2)[-1:]
    current = [m for m in _meta(db, v4.KIND_V4, 2) if not m.get("invalidated")]
    assert len(current) == 1
    assert current[0]["doc_hash"] == v4._doc_hash(json.loads(new_doc["content"]))
    assert [m for m in _meta(db, "semantic_summary", 2) if not m.get("invalidated")]
    assert _outbox(db) == outbox                        # notification-free
    assert _job(db)[0] == "done"


def _drop_chunks(db, mid):
    db.db.execute("DELETE FROM artifacts WHERE kind='semantic_extraction_chunk_v2' "
                  "AND message_id=?", (mid,))
    db.db.commit()


def test_uncached_document_is_held_requeued_once_and_never_reused(world):
    db = world
    _drop_chunks(db, 2)
    out = v4.reproject_stale(db, _scfg())
    assert out["hint_rebuild"]["skipped"] == 1
    assert out["skip_reasons"]["hint_rebuild:chunk_cache_miss"] == 1
    [old] = _meta(db, "semantic_facts_v2", 2)
    assert old["hint_rebuild"] == "skipped:chunk_cache_miss"
    for kind in (v4.KIND_V4, *HELD):
        assert all(m.get("invalidated") is True for m in _meta(db, kind, 2)), kind
    assert _job(db)[0] == "pending"
    llm = _Llm()
    _drain(db, llm=llm)
    assert llm.extractions == 1                         # not the held document
    newest = json.loads(_kind_rows(db, "semantic_facts_v2", 2)[-1]["meta"])
    assert newest["build"] == se.FACTS_V2_BUILD


def test_preflight_not_stored_is_never_asked_again(world):
    db = world
    for row in _kind_rows(db, "semantic_extraction_chunk_v2", 2):
        content = json.loads(row["content"])
        content.pop("jev_preflight")
        db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                      (json.dumps(content, ensure_ascii=False), row["artifact_id"]))
    db.db.commit()
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"]["hint_rebuild:jev_preflight_uncached"] == 1


@pytest.mark.parametrize("hold", ["failed", "needs_review"])
def test_failed_or_parked_generation_is_held_without_a_new_retry(world, hold):
    db = world
    _drop_chunks(db, 2)
    if hold == "failed":
        db.db.execute("UPDATE fetch_jobs SET state='failed' WHERE kind='semantic'")
    else:
        db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.needs_review',json('true'),"
                      "'$.coverage_retry',json('true')) "
                      "WHERE kind='semantic_facts_v2' AND message_id=2")
    db.db.commit()
    out = v4.reproject_stale(db, _scfg())
    reason = "job_failed" if hold == "failed" else "needs_review"
    assert out["skip_reasons"][f"hint_rebuild_seed:{reason}"] == 1
    assert out["hint_rebuild"]["seeded"] == 0
    assert _job(db)[0] == ("failed" if hold == "failed" else "done")
    assert all(m.get("invalidated") is True for m in _meta(db, v4.KIND_V4, 2))


def _published_twin(db, mid):
    """A canonical_projection twin of the message's PASS v4 row, so both
    published kinds are bound to the same audited document."""
    row = _kind_rows(db, v4.KIND_V4, mid)[-1]
    db.artifact_add("canonical_projection", row["content"], project_id=1,
                    message_id=mid, model=row["model"], meta=json.loads(row["meta"]))


def _current_ids(db, mid):
    from mcs_queries import current_projection_id, current_v4_id
    return tuple(db.db.execute(
        f"SELECT {current_projection_id('m')},{current_v4_id('m')} "
        "FROM messages m WHERE project_id=1 AND message_id=?", (mid,)).fetchone())


def _read_state(db, mid):
    import read_model
    rec = next(r for r in read_model.read_model(db.db, scope="detail", project_id=1)["records"]
               if r["message_id"] == mid)
    return {k: rec["extraction"][k]["state"]
            for k in ("canonical_projection", "semantic_facts_v4")}


@pytest.mark.parametrize("unrebuildable,broken", [
    ("repaired", None), ("document_invalid", "$.version"),
    ("document_invalid", "$.evidence")])              # unhashable: held by generation
def test_unrebuildable_document_never_leaves_a_current_projection(world, unrebuildable,
                                                                  broken):
    db = world
    for mid in (1, 2):
        _published_twin(db, mid)
    db.artifact_add("semantic_facts_repair", json.dumps({"status": "started"}),
                    project_id=1, message_id=2, meta={"synthetic": True})
    if unrebuildable == "repaired":
        db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.repaired',json('true')) "
                      "WHERE kind='semantic_facts_v2' AND message_id=2")
    else:
        db.db.execute("UPDATE artifacts SET content=json_remove(content,?) "
                      "WHERE kind='semantic_facts_v2' AND message_id=2", (broken,))
    db.db.commit()
    body = db.db.execute("SELECT body_text,content_hash FROM messages WHERE message_id=2").fetchone()
    repair = db.db.execute("SELECT content,meta FROM artifacts "
                           "WHERE kind='semantic_facts_repair'").fetchall()
    assert all(_current_ids(db, mid) != (None, None) for mid in (1, 2))
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"][f"hint_rebuild:{unrebuildable}"] == 1
    assert _current_ids(db, 2) == (None, None)
    assert _read_state(db, 2) == {"canonical_projection": "stale",
                                  "semantic_facts_v4": "stale"}
    assert None not in _current_ids(db, 1)              # the other message keeps both
    assert _read_state(db, 1) == {"canonical_projection": "current",
                                  "semantic_facts_v4": "current"}
    assert tuple(db.db.execute("SELECT body_text,content_hash FROM messages "
                               "WHERE message_id=2").fetchone()) == tuple(body)
    assert db.db.execute("SELECT content,meta FROM artifacts "
                         "WHERE kind='semantic_facts_repair'").fetchall() == repair
    if unrebuildable == "repaired":
        assert out["skip_reasons"]["hint_rebuild_seed:repaired"] == 1
        assert len(_kind_rows(db, "semantic_facts_v2", 2)) == 1   # repaired doc kept
        assert _job(db)[0] == "done"                              # manual retry only

def test_spent_coverage_retry_still_parks_after_a_held_document(world):
    """The drain never reuses a held document, and the retry it already
    spent still binds: an incomplete re-extraction parks for review."""
    db = world
    _drop_chunks(db, 2)
    db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.coverage_retry',json('true')) "
                  "WHERE kind='semantic_facts_v2' AND message_id=2")
    db.db.commit()
    v4.reproject_stale(db, _scfg())

    def broken(prompt):
        if "メトホルミン" in prompt and "要約器" not in prompt:
            return json.dumps({"facts": [], "category_presence":
                               dict(NO_FACTS, medication="one",
                                    symptom_state="ambiguous")})
        return _Llm()(prompt)
    _drain(db, llm=broken)
    newest = json.loads(_kind_rows(db, "semantic_facts_v2", 2)[-1]["meta"])
    assert newest["coverage_retry"] is True and newest["needs_review"] is True
    assert _job(db)[0] == "failed"


def test_reprojected_document_drops_loop_candidates_it_no_longer_yields(world):
    db = world
    v4.reproject_stale(db, _scfg())
    _drain(db)
    scfg = semantic.semantic_config(_cfg("enforce", fact_source="canonical",
                                         fact_source_gate="g6-v1:test"))[0]
    bundle = semantic.thread_bundle(db, 1, 1)
    stray = {"fact_id": "fact_stray", "kind": "schedule", "statement": "退院した",
             "polarity": "affirmed", "evidence_refs": [], "_evidence": None,
             "validation_status": "verified"}
    semantic_loops.update_loops(db, 1, bundle, {1: [stray]}, None, scfg,
                                time.monotonic() + 30)
    [loop] = _meta(db, "loop_candidate", 1)
    assert not loop.get("invalidated")
    db.db.execute("UPDATE artifacts SET meta=json_set(meta,'$.projection_version',1) "
                  "WHERE kind=? AND message_id=1", (v4.KIND_V4,))
    db.db.commit()
    assert v4.reproject_stale(db, scfg)["reprojected"] == 1
    [loop] = _meta(db, "loop_candidate", 1)
    assert loop["invalidated"] is True and loop["invalidated_reason"] == "reinterpreted"


def test_one_failing_document_never_blocks_the_next(world, monkeypatch):
    db = world
    real = se.rebuild_facts_v2_cached

    def flaky(member, *args):
        if member["message_id"] == 1:
            raise RuntimeError("synthetic rebuild failure")
        return real(member, *args)
    monkeypatch.setattr(se, "rebuild_facts_v2_cached", flaky)
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"]["hint_rebuild:rebuild_failed"] == 1
    assert out["hint_rebuild"]["replaced"] == 1          # message 2 still handled
    assert _meta(db, "semantic_facts_v2", 1)[0]["hint_rebuild"] == "skipped:rebuild_failed"
    assert all(m.get("invalidated") is True for m in _meta(db, v4.KIND_V4, 1))
    assert "hint_rebuild" not in v4.reproject_stale(db, _scfg())


def test_exhausted_done_job_is_requeued_once_and_ends_terminal(world):
    from semantic_runtime import DEFAULT_ATTEMPT_LIMIT
    db = world
    db.db.execute("UPDATE fetch_jobs SET attempts=? WHERE kind='semantic'",
                  (DEFAULT_ATTEMPT_LIMIT,))
    db.db.commit()
    assert v4.reproject_stale(db, _scfg())["hint_rebuild"]["seeded"] == 1
    _drain(db)
    assert _job(db)[0] in ("done", "failed")             # never stuck pending
    if _job(db)[0] == "failed":
        assert not [m for m in _meta(db, v4.KIND_V4, 2) if not m.get("invalidated")]


def test_failed_requeue_rolls_the_row_back_and_still_holds(world, monkeypatch):
    db = world

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic seed failure")
    monkeypatch.setattr(db, "_semantic_seed_tx", broken)
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"]["hint_rebuild:write_failed"] == 1
    assert out["skip_reasons"]["hint_rebuild_seed:write_failed"] == 1
    assert len(_kind_rows(db, "semantic_facts_v2", 2)) == 1   # no half-written doc
    assert _meta(db, "semantic_facts_v2", 2)[0]["hint_rebuild"] == "skipped:write_failed"
    for kind in (v4.KIND_V4, *HELD):
        assert all(m.get("invalidated") is True for m in _meta(db, kind, 2)), kind
    assert _job(db)[0] == "done"                              # nothing queued
    assert "hint_rebuild" not in v4.reproject_stale(db, _scfg())


@pytest.mark.parametrize("fact_source", ["shadow", "legacy"])
def test_non_canonical_fact_source_changes_nothing(world, fact_source):
    db = world
    scfg = semantic.semantic_config(_cfg("enforce", loop_mode="off",
                                         fact_source=fact_source))[0]
    rows = db.db.execute("SELECT artifact_id,meta FROM artifacts ORDER BY artifact_id").fetchall()
    assert "hint_rebuild" not in v4.reproject_stale(db, scfg)
    assert db.db.execute("SELECT artifact_id,meta FROM artifacts "
                         "ORDER BY artifact_id").fetchall() == rows
    assert _job(db)[0] == "done"


def test_paused_project_is_queued_but_never_run(world):
    db = world
    db.artifact_add("semantic_control", "paused", project_id=1)
    assert v4.reproject_stale(db, _scfg())["hint_rebuild"]["seeded"] == 1
    llm = _Llm()
    _drain(db, llm=llm)
    assert _job(db)[0] == "pending"                     # execution stays paused
    assert not [m for m in _meta(db, v4.KIND_V4, 2) if not m.get("invalidated")]


def _older_publication(db, mid, marker):
    """An earlier PASS row of the same generation bound to another
    document, inserted before the newest one so readers would fall back
    to it once the newest is held."""
    for kind in (v4.KIND_V4, "canonical_projection"):
        rows = _kind_rows(db, kind, mid)
        if not rows:
            continue
        newest = rows[-1]
        meta = dict(json.loads(newest["meta"]), doc_hash="0" * 64)
        content = dict(json.loads(newest["content"]), meds=[{"name": marker}])
        db.db.execute("UPDATE artifacts SET artifact_id=artifact_id+100000 "
                      "WHERE artifact_id=?", (newest["artifact_id"],))
        db.db.execute(
            "INSERT INTO artifacts(artifact_id,kind,project_id,message_id,content,"
            "model,meta,created_at) VALUES(?,?,?,?,?,?,?,0)",
            (newest["artifact_id"], kind, 1, mid, json.dumps(content, ensure_ascii=False),
             newest["model"], json.dumps(meta)))
    db.db.commit()


@pytest.mark.parametrize("outcome", ["replaced", "skipped"])
def test_held_generation_never_falls_back_to_an_older_publication(world, outcome):
    db = world
    for mid in (1, 2):
        _published_twin(db, mid)
        _older_publication(db, mid, "OLDER-SYNTHETIC")
    if outcome == "skipped":
        _drop_chunks(db, 2)
    db.artifact_add(v4.KIND_V4, "{}", project_id=1, message_id=2,
                    meta={"fingerprint": "f" * 64, "doc_hash": "e" * 64})   # another generation
    out = v4.reproject_stale(db, _scfg())
    assert out["hint_rebuild"][outcome] == 1
    assert [m for m in _meta(db, v4.KIND_V4, 2) if m["fingerprint"] == "f" * 64] \
        == [{"fingerprint": "f" * 64, "doc_hash": "e" * 64}]
    assert _current_ids(db, 2) == (None, None)
    assert _read_state(db, 2) == {"canonical_projection": "stale",
                                  "semantic_facts_v4": "stale"}
    import read_model
    records = read_model.read_model(db.db, scope="detail", project_id=1)["records"]
    record = next(r for r in records if r["message_id"] == 2)
    assert "OLDER-SYNTHETIC" not in json.dumps(record, ensure_ascii=False)
    assert all(meta.get("invalidated") is True
               for kind in (v4.KIND_V4, "canonical_projection")
               for meta in _meta(db, kind, 2) if meta["fingerprint"] != "f" * 64)
    # message 1 (same generation, other message) keeps its newest rows
    assert None not in _current_ids(db, 1)
    assert _read_state(db, 1) == {"canonical_projection": "current",
                                  "semantic_facts_v4": "current"}


def _set_facts(db, mid, facts_sql, *args):
    db.db.execute("UPDATE artifacts SET content=json_set(content,'$.facts',"
                  f"{facts_sql}) WHERE kind='semantic_facts_v2' AND message_id=?",
                  (*args, mid))
    db.db.commit()


def _bystanders(db):
    """Rows the pass must never touch: another patient's publication, the
    same message's other generation, the source body and formal requests."""
    other = _patient(db, 2)
    other.messages = [_message(9, pid=2, body="別患者の合成本文です。")]
    db.save_patient(other)
    fp = _meta(db, "semantic_facts_v2", 1)[0]["fingerprint"]
    db.artifact_add(v4.KIND_V4, "{}", project_id=2, message_id=9,
                    meta={"fingerprint": fp, "doc_hash": "d" * 64})
    db.artifact_add(v4.KIND_V4, "{}", project_id=1, message_id=1,
                    meta={"fingerprint": "f" * 64, "doc_hash": "e" * 64})
    return lambda: (
        db.db.execute("SELECT artifact_id,content,meta FROM artifacts WHERE project_id=2 "
                      "OR json_extract(meta,'$.fingerprint')=?", ("f" * 64,)).fetchall(),
        db.db.execute("SELECT message_id,body_text,content_hash FROM messages").fetchall(),
        db.db.execute("SELECT count(*) FROM requests").fetchone()[0])


def test_opaque_fact_entry_is_held_without_stopping_the_pass(world):
    db = world
    snapshot = _bystanders(db)
    before = snapshot()
    _set_facts(db, 1, "json_insert(json_extract(content,'$.facts'),'$[#]',?)",
               "synthetic-opaque")
    out = v4.reproject_stale(db, _scfg())
    assert out["skip_reasons"]["hint_rebuild:document_invalid"] == 1
    assert out["hint_rebuild"]["replaced"] == 1          # the next document still runs
    assert _meta(db, "semantic_facts_v2", 1)[0]["hint_rebuild"] == "skipped:document_invalid"
    assert _current_ids(db, 1)[1] is None                 # its generation is held
    assert snapshot() == before
    assert "hint_rebuild" not in v4.reproject_stale(db, _scfg())


@pytest.mark.parametrize("facts", ["json('{}')", "json('\"x\"')", "json('[7,null,[1]]')",
                                   "json('[{\"provenance\":5}]')"])
def test_documents_without_a_merged_fact_entry_are_never_candidates(world, facts):
    db = world
    _set_facts(db, 1, facts)
    rows = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE message_id=1 "
                         "ORDER BY artifact_id").fetchall()
    out = v4.reproject_stale(db, _scfg())
    assert out["hint_rebuild"] == {"same": 0, "replaced": 1, "skipped": 0,
                                   "history": 0, "seeded": 1}
    assert db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE message_id=1 "
                         "ORDER BY artifact_id").fetchall() == rows
