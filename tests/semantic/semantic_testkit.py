"""Shared synthetic fixtures for the semantic test family — ledger /
thread seeding, config builders, deterministic Jev/LLM stubs, canonical
drain drivers, evaluation records and semantic-facts/v2 ``fact``/``doc``
builders.  Not a test module (no ``test_`` prefix); sibling files import
it via the tests/ sys.path bootstrap."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcs"))

import ledger
import mcs_adapter
import semantic
import semantic_evaluation as evaluation
import semantic_facts as sf
import semantic_jev as jev


BODY = "明日からカロナール300mgを1日3回に変更します。確認お願いします。"


def _ledger(tmp_path):
    return ledger.Ledger(str(tmp_path / "ledger.db"))


def _message(mid=1, pid=1, parent=None, body=BODY, unread=True):
    return mcs_adapter.Message(
        message_id=mid, project_id=pid, parent_id=parent,
        sender_id=1, sender_name="sender", sender_type="user",
        profession="", organization="",
        posted_at="2026-09-19T00:00:00+09:00",
        body_html=f"<p>{body}</p>", body_state="full",
        is_unread=unread, reply_count=0)


def _patient(db, pid=1):
    p = SimpleNamespace(project_id=pid, project_type="medical",
                        patient_name="テスト患者", disease="",
                        station_name="", url="https://www.medical-care."
                        "net/projects/medical/%d" % pid,
                        fetch_state="complete", fetch_reason=None,
                        messages=[])
    db.save_patient(p)
    return p


def _cfg(mode="shadow", **kw):
    return {"semantic": {"mode": mode,
                         "summary_mode": kw.pop("summary_mode", mode),
                         "loop_mode": kw.pop("loop_mode", mode),
                         "threshold_mode": "calibrated",
                         "calibration_version": "synthetic-test-v1",
                         "daily_request_budget": kw.pop("budget", 50),
                         "project_ids": kw.pop("project_ids", None),
                         **kw}}


class _FakeJev:
    """Deterministic evaluate(): noul=0.9, choice=first option (or the
    `choice` override / per-qid `choice_map`)."""
    def __init__(self, noul=0.9, choice_map=None, error=None, choice=None):
        self.requests_made = 0
        self.noul = noul
        self.choice_map = choice_map or {}
        self.error = error
        self.choice = choice
        self.calls = []

    def evaluate(self, state, questions, deadline):
        self.requests_made += 1
        self.calls.append(sorted(questions))
        if self.error:
            raise self.error
        out = {}
        for qid, q in questions.items():
            if q.get("type") == "choice":
                opts = list(q.get("criteria") or {})
                pick = self.choice or self.choice_map.get(
                    qid, "planned" if qid == "status"
                    else opts[0] if opts else None)
                out[qid] = {"type": "choice", "choice": pick,
                            "confidence": 0.9,
                            "probabilities": {o: 1.0 / len(opts)
                                             for o in opts}}
            else:
                out[qid] = {"type": "noul", "noul": self.noul}
        return {"answers": out, "model": jev.JEV_MODEL}


def _llm(prompt):
    if "要約器" in prompt:
        return json.dumps({"claims": [{
            "section": "medication",
            "text": "カロナールを300mg×3回に変更する記載がある",
            "claim_kind": "reported_fact", "fact_refs": [0],
            "status": "planned", "polarity": "affirmed"}],
            "limitations": ["採血結果の記載なし"]})
    if "事実候補抽出器" in prompt:
        return json.dumps({"facts": [{
            "statement": "用量変更の記載がある",
            "kind": "medication_event",
            "status": "planned", "polarity": "affirmed",
            "time_text": "明日から", "quantity": "300mg",
            "evidence_quote": "カロナール300mgを1日3回に変更"}]})
    return None


def _seeded(tmp_path):
    """Patient + unread parent + reply, saved through the notify path
    with semantic=True — produces one pending kind='semantic' job."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1)]
    p.messages[0].replies = [_message(2, parent=1)]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


NO_FACTS = {c: "none" for c in sf.MANDATORY_CATEGORIES}


def _seeded_two(tmp_path):
    """Parent + reply with DISTINCT bodies -> one job, targets {1,2}."""
    db = _ledger(tmp_path)
    p = _patient(db)
    p.messages = [_message(1, body="アムロジピン5mgを継続します。")]
    p.messages[0].replies = [
        _message(2, parent=1, body="メトホルミン500mgが出ています。")]
    db.save_patient(p, notify={"source": "unread"}, semantic=True)
    return db


def _canonical_cfg():
    return _cfg("enforce", loop_mode="off",
                fact_source="canonical",
                fact_source_gate="g6-v1:test")


def _med_fact(drug, quote, action):
    return {"statement": quote, "kind": "medication_event",
            "action": action, "subject_role": "patient",
            "polarity": "affirmed", "workflow_status": "performed",
            "importance": "T1", "evidence_quote": quote}


def _llm_v2(prompt):
    """Per-target v2 extraction + empty summary.  The prompt embeds the
    member body, so the response is keyed by which drug it mentions."""
    if "要約器" in prompt:
        return json.dumps({"claims": [], "limitations": []},
                          ensure_ascii=False)
    if "アムロジピン" in prompt:
        return json.dumps({
            "facts": [_med_fact("アムロジピン", "アムロジピン5mgを継続",
                                "continue")],
            "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)
    if "メトホルミン" in prompt:
        return json.dumps({
            "facts": [_med_fact("メトホルミン", "メトホルミン500mgが出ています",
                                "continue")],
            "category_presence": dict(NO_FACTS, medication="one")},
            ensure_ascii=False)
    return json.dumps({"facts": [],
                       "category_presence": dict(NO_FACTS)},
                      ensure_ascii=False)


def _preflight_jev():
    """FakeJev answering 'present' for medication, 'absent' elsewhere —
    preflight obligations close cleanly for the two-target corpus."""
    choice_map = {f"has_{c}": "absent" for c in sf.MANDATORY_CATEGORIES}
    choice_map["has_medication"] = "present"
    return _FakeJev(choice_map=choice_map)


def _drain(db, llm=None, jev=None):
    return semantic.run_due(
        db, _canonical_cfg(), {"errors": []},
        time.monotonic() + 300,
        jev_client=jev or _preflight_jev(), llm_fn=llm or _llm_v2)


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


EV = {"evidence_id": "ev_1", "message_id": "m1", "revision": "r1",
      "start": 0, "end": 9, "quote": "アムロジピン", "atom_id": "a1"}


def _drained_old_version(tmp_path):
    """Two canonical targets drained to PASS, then every projection/v4
    row rewritten as if minted by the previous projection version with
    older (sentinel) content."""
    import semantic_v4 as v4
    db = _seeded_two(tmp_path)
    semantic.run_due(db, _canonical_cfg(), {"errors": []},
                     time.monotonic() + 300, jev_client=_PassJev(),
                     llm_fn=_llm_v2)
    for kind in ("canonical_projection", v4.KIND_V4):
        assert len(_kind_rows(db, kind)) == 2
    db.db.execute(
        "UPDATE artifacts SET content=?, meta=json_remove(meta,"
        "'$.projection_version') WHERE kind IN (?,?)",
        (json.dumps({"meds": [{"name": "OLD"}]}), "canonical_projection",
         v4.KIND_V4))
    db.artifact_add("patient_rollup", "{}", project_id=1)
    db.db.commit()
    return db


def _kind_rows(db, kind, mid=None):
    q = ("SELECT artifact_id,message_id,content,meta,model FROM artifacts "
         "WHERE kind=?")
    args = [kind]
    if mid is not None:
        q += " AND message_id=?"
        args.append(mid)
    return db.db.execute(q + " ORDER BY artifact_id", args).fetchall()


def _pending_fact(member):
    facts, complete, _ = semantic.extract_facts(_llm, member)
    assert complete
    facts[0]["kind"] = "pending_item"
    return facts


MANIFEST = {
    "version": "manifest-v1",
    "bundle_version": "bundle-v1",
    "candidate_version": "candidate-v1",
    "label_version": "label-v1",
}


CRITERIA = {
    "version": "criteria-v1",
    "required_metrics": list(evaluation.METRICS),
    "minimums": {
        "important_fact_recall": 1.0,
        "final_recall": 1.0,
        "medication_recall": 1.0,
        "negation_recall": 1.0,
        "time_recall": 1.0,
        "speaker_relation_recall": 1.0,
        "loop_conformity": 1.0,
        "loop_precision": 1.0,
        "mandatory_fact_recall": 1.0,
        "rendered_fact_recall": 1.0,
        "delivered_fact_recall": 1.0,
    },
    "maximums": {
        "critical_overclaim": 0.0,
        "loop_false_resolution": 0.0,
        "loop_unresolved_miss_rate": 0.0,
        "defer_rate": 0.0,
        "silent_drop": 0.0,
    },
    "required_splits": ["test"],
    "min_human_labels": 1,
}


def _record(source="synthetic"):
    return {
        "case_id": "case-1",
        "split": "test",
        "account_id": "account-1",
        "project_id": "project-1",
        "thread_id": "thread-1",
        "bundle": {
            "version": "bundle-v1",
            "messages": [{"message_id": "m0"}, {"message_id": "m1"}],
            "attachments": [{
                "attachment_id": "att-1", "message_id": "m1",
                "path": "fixtures/attachment-1.bin",
                "context_before": ["m0"],
            }],
            "body_original": "ORIGINAL-SECRET-MUST-NOT-LEAK",
        },
        "candidate": {
            "version": "candidate-v1",
            "facts": [
                {"fact_id": "f1", "important": True,
                 "medication": "drug-a", "negation": "affirmed",
                 "time": "tomorrow", "speaker_relation": "doctor",
                 "evidence_ids": ["ev-1"]},
                {"fact_id": "f2", "important": True,
                 "medication": "drug-b", "negation": "negated",
                 "time": "today", "speaker_relation": "nurse",
                 "evidence_ids": ["ev-2"]},
            ],
            "verified_fact_ids": ["f1", "f2"],
            "rendered_fact_ids": ["f1", "f2"],
            "delivered_fact_ids": ["f1", "f2"],
            "relations": [{"left_fact_id": "f1", "right_fact_id": "f2",
                           "type": "COMPLEMENTS"}],
            "unresolved": [],
            "claims": [
                {"claim_id": "c1", "critical": False,
                 "fact_refs": ["f1"], "attachment_refs": ["att-1"]},
                {"claim_id": "c2", "critical": False,
                 "fact_refs": ["f2"], "attachment_refs": []},
            ],
            "loops": [{"loop_id": "loop-1", "resolved": False}],
            "status": "complete",
            "latency_ms": 100,
            "usage": {"requests": 2, "input_tokens": 10,
                       "output_tokens": 5, "total_tokens": 15},
        },
        "label": {
            "version": "label-v1",
            "source": source,
            **({"receipt": {"receipt_id": "rcpt-1",
                            "labelled_at": "2026-09-21",
                            "reviewer": "reviewer-1"}}
               if source == "human" else {}),
            "facts": [
                {"fact_id": "f1", "important": True, "mandatory": True,
                 "medication": "drug-a", "negation": "affirmed",
                 "time": "tomorrow", "speaker_relation": "doctor"},
                {"fact_id": "f2", "important": True,
                 "medication": "drug-b", "negation": "negated",
                 "time": "today", "speaker_relation": "nurse"},
            ],
            "relations": [{"left_fact_id": "f1", "right_fact_id": "f2",
                           "type": "COMPLEMENTS"}],
            "claims": [
                {"claim_id": "c1", "critical": True, "supported": True,
                 "final": True, "covered_gold_fact_ids": ["f1"]},
                {"claim_id": "c2", "critical": False, "supported": True,
                 "final": True, "covered_gold_fact_ids": ["f2"]},
            ],
            "loops": [{"loop_id": "loop-1", "resolved": False}],
        },
    }


def _delivery_patient(messages=()):
    return SimpleNamespace(
        project_id=1, project_type="medical", patient_name="患者",
        disease="", station_name="",
        url="https://www.medical-care.net/projects/medical/1",
        fetch_state="complete", fetch_reason=None, messages=list(messages))


def _delivery_db(tmp_path, messages=()):
    db = ledger.Ledger(str(tmp_path / "ledger.db"))
    db.save_patient(_delivery_patient(messages), notify={"source": "unread"})
    return db


def _summary(db, claims=1):
    bundle = semantic.thread_bundle(db, 1, 1)
    content = {"claims": [
        {"text": f"claim-{i}-" + "x" * 220, "section": "status",
         "claim_kind": "reported_fact"}
        for i in range(claims)
    ], "limitations": ["制約-" + "y" * 120]}
    db.artifact_add(
        "semantic_summary", json.dumps(content, ensure_ascii=False),
        project_id=1, message_id=1, model="Qwen3.5-9B",
        meta={"fingerprint": bundle["source_fingerprint"],
              "policy_fingerprint": semantic.policy_fingerprint(semantic.semantic_config(_cfg("enforce"))[0]),
              "audit_status": "PASS",
              "publication_mode": "enforce",
              "target_revision": bundle["members"][0]["revision"]})


def _notice_text(body):
    return (
        "【患者】\n"
        "対象新着：1投稿｜対象投稿の最終時刻：2026/09/19 00:00 JST\n"
        "取得：完全\n"
        "要約：自動検査完了\n\n"
        "■ 今回の重要情報\n"
        f"{body}\n\n"
        "▶ MCSで確認\n"
        "https://www.medical-care.net/projects/medical/1"
    )


def _semantic_event(db, text="notice"):
    src = db.outbox_add("new_messages", 1, {"message_ids": [1]})
    for row in db.db.execute(
            "SELECT event_id FROM notify_outbox WHERE kind='new_messages'"):
        db.outbox_mark(row["event_id"], "accepted")
    bundle = semantic.thread_bundle(db, 1, 1)
    _summary(db, claims=1)
    payload = {
        "root_id": 1, "target_message_id": 1, "src_event_id": src,
        "target_revision": bundle["members"][0]["revision"],
        "fingerprint": bundle["source_fingerprint"],
        "policy_version": semantic.POLICY_VERSION,
        "policy_fingerprint": semantic.policy_fingerprint(semantic.semantic_config(_cfg("enforce"))[0]), "text": _notice_text(text),
    }
    db.outbox_add("semantic_notice", 1, payload)
    return db.db.execute(
        "SELECT * FROM notify_outbox WHERE kind='semantic_notice'"
    ).fetchone()


SOURCE = {"message_id": "m1", "revision": "r1", "content_hash": "h",
          "body_codepoints": 20, "content_quality": "full",
          "attachments_complete": True, "source_fingerprint": "sf_x"}


def v2_fact(fid, **overrides):
    """Verified semantic-facts/v2 medication fact; ``overrides`` replace
    or add top-level keys."""
    out = {"fact_id": fid, "kind": "medication_event",
           "subject": "patient:1", "actor": "sender:s1",
           "statement": "アムロジピン継続", "polarity": "affirmed",
           "epistemic": "asserted", "workflow_status": "performed",
           "event_time": "unknown", "valid_time": "unknown",
           "evidence_ids": [], "obligation_ids": [],
           "importance": "T1", "provenance": "local_llm",
           "validation_status": "verified"}
    out.update(overrides)
    return out


def v2_doc(facts, evidence=(), **overrides):
    """Complete semantic-facts/v2 document bound to ``SOURCE``;
    ``overrides`` replace top-level keys (``source`` included)."""
    out = {"version": sf.CONTRACT_VERSION, "source": dict(SOURCE),
           "atoms": [], "chunks": [], "obligations": [],
           "evidence": list(evidence), "facts": list(facts),
           "relations": [],
           "coverage": {"category_counts": {},
                        "open_obligation_ids": [], "limitations": [],
                        "status": "complete"}}
    out.update(overrides)
    return out
