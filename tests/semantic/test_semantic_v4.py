"""T18 — the unified self-correcting inference pipeline minted as v4.

Synthetic-only: temp Ledger, fake local LLM/Jev, no network. Covers
stage receipts S1–S8, the PASS-only read model boundary, generation
fencing, diagnostic (non-PASS) separation, pre-reserved repair, and
the finite retirement manifest.
"""
import json
import time

import extract_llm
import semantic
import semantic_jev as jev
import semantic_v4 as v4
from mcs_queries import (current_fact_pred, qc_v4_source_id)
from semantic_policy import KIND_FACT_REPAIR
from test_canonical_drain import _drain, _seeded_two
from test_mcs_semantic import _FakeJev, _ledger, _message, _patient

NOW = time.time()


class _PassJev(_FakeJev):
    """Jev answers that let a clean synthetic corpus reach PASS:
    category preflight 'present' for medication only, fact/claim
    support 'supports', coverage 'complete', and medication detail
    dimensions echoing the extracted fact's own value."""
    def evaluate(self, state, questions, deadline):
        self.requests_made += 1
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        target = (state or {}).get("target") or {}
        out = {}
        for qid, q in questions.items():
            if q.get("type") != "choice":
                out[qid] = {"type": "noul", "noul": self.noul}
                continue
            opts = list(q.get("criteria") or {})
            if qid in ("status", "polarity"):
                pick = target.get(qid)
                if pick not in opts:
                    pick = opts[0] if opts else None
            elif qid == "source_fact_coverage":
                pick = "complete"
            elif qid.startswith("has_"):
                pick = "present" if qid == "has_medication" else "absent"
            elif qid.startswith("fact_") or qid.startswith("claim"):
                pick = "supports"
            else:
                pick = opts[0] if opts else None
            out[qid] = {"type": "choice", "choice": pick,
                        "confidence": 0.95,
                        "probabilities": {o: 1.0 / len(opts)
                                         for o in opts}}
        return {"answers": out, "model": jev.JEV_MODEL}


def _rows(db, kind, mid):
    return db.db.execute(
        "SELECT artifact_id,content,meta FROM artifacts WHERE kind=? "
        "AND message_id=? ORDER BY artifact_id", (kind, mid)).fetchall()


def _fp(db, mid=1):
    return semantic.thread_bundle(db, 1, mid)["source_fingerprint"]


def test_v4_pass_publishes_read_model_and_stage_ledger(tmp_path):
    """A canonical drain reaching PASS mints a semantic_facts_v4 row
    bound to the audited doc_hash, with S1–S8 receipts persisted."""
    db = _seeded_two(tmp_path)
    try:
        out = _drain(db, jev=_PassJev())
        assert out["done"] == 1 and not out["failed"]
        fp = _fp(db)
        for mid in (1, 2):
            v4rows = _rows(db, v4.KIND_V4, mid)
            assert len(v4rows) == 1
            meta = json.loads(v4rows[0]["meta"])
            assert meta["engine_version"] == 4
            assert meta["extract_version"] == 4
            assert meta["doc_hash"]
            assert meta["fingerprint"] == fp
            # v4 content is the same audited projection the legacy
            # shape carries — identical canonical fact ids
            proj = json.loads(_rows(db, "canonical_projection",
                                    mid)[0]["content"])
            v4doc = json.loads(v4rows[0]["content"])
            assert [f["fact_id"] for f in v4doc["canonical_facts"]] \
                == [f["fact_id"] for f in proj["canonical_facts"]]
            stages = {s["stage"]: s["status"] for s in
                      v4.stage_ledger(db, mid, fp)}
            for s in ("s0_prep", "s1_extract", "s2_fact_audit",
                      "s5_projection", "s6_summary", "s7_summary_audit",
                      "s8_publish"):
                assert s in stages, stages
            assert stages["s8_publish"] == "PASS"
        # no diagnostic rows on a clean run
        assert not _rows(db, v4.KIND_V4_DIAG, 1)
    finally:
        db.close()


class _AuditFailJev(_FakeJev):
    """Fact-support questions get 'not_supported' — the fact audit
    deterministically lands NEEDS_REVIEW with a rejected finding."""
    def evaluate(self, state, questions, deadline):
        if any(k.startswith("fact_") for k in questions):
            self.requests_made += 1
            return {"answers": {
                k: {"type": "choice", "choice": "not_supported",
                    "confidence": 0.9,
                    "probabilities": {"not_supported": 1.0}}
                for k in questions}, "model": jev.JEV_MODEL}
        return super().evaluate(state, questions, deadline)


def test_nonpass_leaves_diagnostic_only(tmp_path):
    """A NEEDS_REVIEW generation writes a v4_diagnostic receipt and NO
    semantic_facts_v4 row — and the shared read predicate can never
    select the diagnostic."""
    db = _seeded_two(tmp_path)
    try:
        _drain(db, jev=_AuditFailJev(
            choice_map={f"has_{c}": "absent"
                        for c in
                        __import__("semantic_facts").MANDATORY_CATEGORIES}
            | {"has_medication": "present"}))
        # no v4 read-model row for either target
        for mid in (1, 2):
            assert not _rows(db, v4.KIND_V4, mid)
        # the member processed first (mid=1) left a durable diagnostic
        diags = _rows(db, v4.KIND_V4_DIAG, 1)
        assert diags
        content = json.loads(diags[-1]["content"])
        assert content["status"] in ("NEEDS_REVIEW", "PENDING")
        # the diagnostic kind is not reachable through the shared
        # current-fact predicate at all
        sel = db.db.execute(
            f"""SELECT a.artifact_id FROM artifacts a
                JOIN messages m ON m.message_id=a.message_id
                WHERE a.kind='v4_diagnostic'
                  {current_fact_pred('a', 'm')}""").fetchall()
        assert sel == []
    finally:
        db.close()


def test_repair_receipt_reserved_before_dispatch(tmp_path):
    """S3: the 'started' repair receipt lands BEFORE repair_facts_v2
    runs — a seeded started receipt permanently consumes the dispatch
    (a crash never earns a second repair)."""
    db = _seeded_two(tmp_path)
    try:
        fp = _fp(db)
        calls = []
        real = __import__("semantic_extraction").repair_facts_v2

        def spied(*a, **k):
            calls.append(1)
            return real(*a, **k)

        import semantic_extraction
        orig = semantic_extraction.repair_facts_v2
        semantic_extraction.repair_facts_v2 = spied
        try:
            _drain(db, jev=_AuditFailJev(
                choice_map={
                    f"has_{c}": "absent"
                    for c in __import__(
                        "semantic_facts").MANDATORY_CATEGORIES}
                | {"has_medication": "present"}))
        finally:
            semantic_extraction.repair_facts_v2 = orig
        assert calls                          # repair ran once
        repairs = _rows(db, KIND_FACT_REPAIR, 1)
        statuses = [json.loads(r["content"]).get("status")
                    for r in repairs]
        assert statuses[0] == "started"       # reservation precedes
        stage_seq = [s["stage"] + ":" + s["status"]
                     for s in v4.stage_ledger(db, 1, fp)]
        assert "s3_repair:reserved" in stage_seq
        assert stage_seq.index("s3_repair:reserved") < \
            stage_seq.index("s3_repair:completed")

        # crash simulation: the 'started' receipt survives — a second
        # drain never calls repair again even though NEEDS_REVIEW stays
        calls.clear()
        semantic_extraction.repair_facts_v2 = spied
        try:
            _drain(db, jev=_AuditFailJev(
                choice_map={
                    f"has_{c}": "absent"
                    for c in __import__(
                        "semantic_facts").MANDATORY_CATEGORIES}
                | {"has_medication": "present"}))
        finally:
            semantic_extraction.repair_facts_v2 = orig
        assert calls == []                    # dispatch was consumed
    finally:
        db.close()


def _seed_legacy_and_v4(db, mid=1):
    """A message with a current extract_llm row, a canonical_projection
    row, and a PASS v4 row — the precedence fixture."""
    h = db.db.execute(
        "SELECT content_hash FROM messages WHERE message_id=?",
        (mid,)).fetchone()[0]
    for kind, ver in (("extract_llm", 3), ("canonical_projection", None),
                      (v4.KIND_V4, 4)):
        meta = {"hash": h}
        if ver == 4:
            meta.update({"engine_version": 4, "extract_version": 4,
                         "doc_hash": "d"})
        elif ver:
            meta["extract_version"] = ver
        db.artifact_add(kind, json.dumps(
            {"meds": [], "canonical_facts": [
                {"fact_id": f"from-{kind}",
                 "kind": "medication_event",
                 "validation_status": "verified"}]},
            ensure_ascii=False),
            project_id=1, message_id=mid, meta=meta)
    return h


def test_v4_outranks_legacy_and_projection(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic")], project_id=1)
    try:
        _seed_legacy_and_v4(db)
        picked = db.db.execute(f"""
          SELECT a.kind, a.content FROM artifacts a
          JOIN messages m ON m.message_id=a.message_id
          WHERE a.kind IN ('extract_llm','canonical_projection',
                           'semantic_facts_v4')
            AND a.message_id=1 {current_fact_pred('a', 'm')}
        """).fetchall()
        assert [r["kind"] for r in picked] == ["semantic_facts_v4"]
        assert json.loads(picked[0]["content"])["canonical_facts"][0][
            "fact_id"] == "from-semantic_facts_v4"
        # qc_v4_source_id selects the v4 row; bare qc_source_id with
        # version=4 can never reach this kind
        v4src = db.db.execute(
            f"SELECT {qc_v4_source_id('m')} FROM messages m "
            "WHERE m.message_id=1").fetchone()[0]
        assert v4src is not None
        from mcs_queries import qc_source_id
        legacy = db.db.execute(
            f"SELECT {qc_source_id('m', version=4)} FROM messages m "
            "WHERE m.message_id=1").fetchone()[0]
        assert legacy is None
    finally:
        db.close()


def test_delayed_v3_writer_is_fenced(tmp_path):
    """Once v4 covers a source revision, a late extract_llm write for
    that hash is refused — not merely shadowed."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic")], project_id=1)
    try:
        _seed_legacy_and_v4(db)
        row = db.db.execute(
            "SELECT message_id,content_hash FROM messages "
            "WHERE message_id=1").fetchone()
        ok = extract_llm._replace_current(
            db, dict(row), json.dumps({"meds": [], "symptoms": []}))
        assert ok is False
        kinds = {r["kind"] for r in db.db.execute(
            "SELECT kind FROM artifacts WHERE message_id=1")}
        assert kinds == {"extract_llm", "canonical_projection",
                         "semantic_facts_v4"}
        assert db.db.execute(
            "SELECT COUNT(*) FROM artifacts WHERE kind='extract_llm' "
            "AND message_id=1").fetchone()[0] == 1
    finally:
        db.close()


def test_extract_admission_defaults_to_zero_under_v4(tmp_path, monkeypatch):
    """Under fact_source=canonical with no cohort, run_pending admits
    nothing — historical new inference is off by default."""
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic backlog")],
                     project_id=1)
    try:
        monkeypatch.setattr(extract_llm, "_llm_call",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("llm called")))
        res = extract_llm.run_pending(db, limit=5, budget_s=5,
                                      admitted_ids=set())
        assert res["done"] == 0 and res["failed"] == 0
        # manifest-declared ids DO get admitted
        v4.declare_cohort(
            db, "c1", [{"message_id": 1,
                        "content_hash": db.db.execute(
                            "SELECT content_hash FROM messages "
                            "WHERE message_id=1").fetchone()[0]}],
            {"items": 1}, NOW + 3600)
        assert v4.active_legacy_admissions(db, NOW) == {1}
        # an expired cohort admits nothing again
        assert v4.active_legacy_admissions(db, NOW + 7200) == set()
    finally:
        db.close()


def test_cohort_schedules_bounded_and_restart_safe(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(i, body=f"synthetic {i}")
                      for i in (1, 2, 3)], project_id=1)
    try:
        hashes = {r["message_id"]: r["content_hash"] for r in
                  db.db.execute("SELECT message_id,content_hash "
                                "FROM messages")}
        v4.declare_cohort(
            db, "c2",
            [{"message_id": i, "content_hash": hashes[i]}
             for i in (1, 2, 3)],
            {"items": 2}, NOW + 3600)
        res = v4.run_cohort(db, "c2", now=NOW)
        assert res["scheduled"] == 2          # ceiling respected
        # item receipts bound the cursor — re-running doesn't re-seed
        v4.mark_item_done(db, "c2", 1, "converted")
        res2 = v4.run_cohort(db, "c2", now=NOW)
        assert res2["scheduled"] <= 2
        jobs = db.db.execute(
            "SELECT COUNT(*) FROM fetch_jobs WHERE kind='semantic'"
        ).fetchone()[0]
        assert jobs <= 3
        assert v4.run_cohort(db, "c2", now=NOW + 7200)["error"] == \
            "cohort_expired"
    finally:
        db.close()


def test_retire_payloads_tombstones_only_after_all_gates(tmp_path):
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic"),
                      _message(2, body="synthetic two")], project_id=1)
    try:
        h = _seed_legacy_and_v4(db, 1)
        _seed_legacy_and_v4(db, 2)
        v4.declare_cohort(
            db, "c3",
            [{"message_id": 1, "content_hash": h},
             {"message_id": 2, "content_hash": db.db.execute(
                 "SELECT content_hash FROM messages WHERE message_id=2"
             ).fetchone()[0]}],
            {"items": 2}, NOW + 3600, retire_after_at=NOW + 100)
        v4.mark_item_done(db, "c3", 1, "converted")
        v4.mark_item_done(db, "c3", 2, "converted")
        # QC reference pins message 2's extract_llm row
        src = db.db.execute(
            "SELECT artifact_id FROM artifacts WHERE kind='extract_llm'"
            " AND message_id=2").fetchone()[0]
        db.artifact_add("extract_qc", json.dumps({"qc": "done"}),
                        project_id=1, message_id=2,
                        meta={"source_artifact_id": src})
        # too early → held
        res = v4.retire_payloads(db, "c3", now=NOW + 50)
        assert res["retired"] == 0
        res = v4.retire_payloads(db, "c3", now=NOW + 200)
        assert res["held"]
        # m1's extract_llm + canonical_projection are tombstoned
        tombs = db.db.execute(
            "SELECT artifact_id FROM artifacts WHERE message_id=1 "
            "AND kind IN ('extract_llm','canonical_projection')").fetchall()
        assert len(tombs) == 2
        for (aid,) in tombs:
            c = json.loads(db.db.execute(
                "SELECT content FROM artifacts WHERE artifact_id=?",
                (aid,)).fetchone()[0])
            assert c["_tombstone"] and c["retired_artifact_id"] == aid
        # m2's QC-referenced extract_llm is held, its projection retired
        m2_ext = json.loads(db.db.execute(
            "SELECT content FROM artifacts WHERE artifact_id=?",
            (src,)).fetchone()[0])
        assert "_tombstone" not in m2_ext
        # the v4 rows themselves are never tombstoned
        v4c = json.loads(db.db.execute(
            "SELECT content FROM artifacts WHERE kind='semantic_facts_v4'"
            " AND message_id=1").fetchone()[0])
        assert "_tombstone" not in v4c
        # the source body is untouched
        assert db.db.execute(
            "SELECT body_text FROM messages WHERE message_id=1"
        ).fetchone()[0] == "synthetic"
    finally:
        db.close()


def test_v4_row_survives_into_read_model(tmp_path):
    """read_model distinguishes the v4 generation and reports
    engine_version on the extraction entry."""
    import read_model
    db = _ledger(tmp_path)
    _patient(db)
    db.save_messages([_message(1, body="synthetic")], project_id=1)
    try:
        _seed_legacy_and_v4(db)
        rec = read_model.read_model(db.db)["records"][0]
        assert rec["extraction"]["semantic_facts_v4"]["state"] == \
            "current"
        assert rec["extraction"]["semantic_facts_v4"][
            "engine_version"] == 4
        assert rec["state"] == "current"
    finally:
        db.close()
