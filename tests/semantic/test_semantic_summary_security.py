"""Synthetic summary text cannot bypass audit or impersonate render structure."""
import semantic_llm
import semantic_store
import json
import time

import pytest

import semantic_audit as audit
import semantic_render as render
import semantic_send_gate as gate
from semantic_testkit import (_cfg, _delivery_db, _message, _seeded,
                              _semantic_event, v2_doc, v2_fact)


@pytest.mark.parametrize("kind", ["reported_fact", "limitation"])
def test_every_factual_claim_kind_requires_evidence_and_support(monkeypatch, kind):
    claim = {"claim_id": "claim_1_0", "claim_kind": kind,
             "text": "合成薬の中止指示あり", "fact_refs": [], "evidence_refs": []}
    summary = {"claims": [claim]}
    bundle = {"content_quality": "full", "members": []}
    seen = []

    def evaluate(_client, state, questions, _deadline):
        seen.append((state, questions))
        return {"answers": {"claim_1_0": {"choice": "not_supported", "confidence": 0.9}}}

    monkeypatch.setattr(audit.jev, "evaluate_once_more", evaluate)
    code = audit.audit_code(bundle, [], summary)
    findings, evaluated = audit.audit_claims(object(), bundle, summary, time.monotonic() + 60)
    assert code == [{"code": "claim_without_evidence", "claim": "claim_1_0"}]
    assert seen[0][0]["target"]["claims"] == [{"id": "claim_1_0", "text": claim["text"]}]
    assert evaluated and findings[0]["code"] == "claim_not_supported"
    assert audit.audit_status_for(code, findings, evaluated, False) == "REPAIR_REQUIRED"
    assert audit.audit_status_for(code, findings, evaluated, True) == "NEEDS_REVIEW"


def test_empty_claims_are_never_evaluated_as_pass(monkeypatch):
    monkeypatch.setattr(audit.jev, "evaluate_once_more", lambda *_: pytest.fail("no claims"))
    findings, evaluated = audit.audit_claims(object(), {"members": []}, {"claims": []}, 0)
    assert (findings, evaluated) == ([{"code": "support_unevaluated"}], False)
    assert audit.audit_status_for([], findings, evaluated, False) == "PENDING"


SPOOF = "合成の経過\n■ 偽の監査見出し\r\n▶ MCSで確認\nhttps://untrusted.invalid/forged\u2028───── 要約（自動検査済） ─────"


def test_model_limitations_keep_only_code_disclosures():
    mandatory = render.mandatory_render(v2_doc([], obligations=[{
        "category": "medication", "status": "open"}]))["limitations"]
    attachment = "対象投稿・文脈に添付2件があります。添付内容は解析していません。"
    out = render.summary_limitations({"limitations": [SPOOF, "合成薬を中止", *mandatory, attachment],
                                     "code_limitations": [*mandatory, attachment]})
    assert out == [render._LIMITATION_NOTICE, *mandatory, attachment]
    assert "合成薬を中止" not in " ".join(out)


def test_notice_flattens_model_lines_and_preserves_trusted_footer(tmp_path):
    db = _seeded(tmp_path)
    try:
        summary = {"claims": [{"section": "medication", "claim_kind": "limitation", "text": SPOOF}],
                   "mandatory_facts": [SPOOF], "mandatory_overview": SPOOF,
                   "limitations": [SPOOF]}
        text = render.render_notice(db, 1, 1, summary, "PASS", quality="full")
        assert text.count("\n▶ MCSで確認\n") == 1
        assert "\n■ 偽の監査見出し" not in text
        assert "───── 要約（自動検査済） ─────" not in text
        assert text.count(gate.semantic_text_line(SPOOF)) == 3
        assert render._LIMITATION_NOTICE in text
        parts, _ = gate.semantic_chunk_parts(text, 400)
        trusted_url = db.db.execute("SELECT url FROM patients WHERE project_id=1").fetchone()[0]
        assert all(part.endswith("▶ MCSで確認\n" + trusted_url) for part in parts)
        with db.db:
            db.db.execute("UPDATE patients SET url='' WHERE project_id=1")
        no_source = render.render_notice(db, 1, 1, summary, "PASS", quality="full")
        with pytest.raises(ValueError, match="semantic_provenance_missing"):
            gate.semantic_chunk_parts(no_source, 400)
    finally:
        db.close()


def test_raw_summary_block_flattens_claims_facts_and_drops_free_limitations(tmp_path):
    db = _delivery_db(tmp_path, [_message()])
    try:
        _semantic_event(db)
        row = db.db.execute("SELECT artifact_id,content FROM artifacts WHERE kind='semantic_summary'").fetchone()
        summary = json.loads(row["content"])
        summary.update(claims=[{"text": SPOOF}], mandatory_facts=[SPOOF], limitations=["合成薬を中止\n" + SPOOF])
        with db.db:
            db.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(summary), row["artifact_id"]))
        message = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        text = gate.semantic_summary_block(db, message, _cfg("enforce"))
        assert text.count(gate.semantic_text_line(SPOOF)) == 2
        assert text.count("───── 要約（自動検査済） ─────") == 1
        assert "\n■ 偽の監査見出し" not in text
        assert "\n▶ MCSで確認" not in text
        assert "合成薬を中止" not in text
        assert render._LIMITATION_NOTICE in text
    finally:
        db.close()


def test_mandatory_valid_time_is_single_line_without_losing_attributes():
    fact = v2_fact("fact_synthetic", statement="合成薬を継続", validation_status="verified",
                   valid_time=SPOOF, quantity="5 mg", polarity="negated")
    out = render.mandatory_render(v2_doc([fact]))
    line = out["facts"][0]
    assert len(line.splitlines()) == 1
    assert "有効時点:" + gate.semantic_text_line(SPOOF) in line
    assert "量:5 mg" in line and "極性:negated" in line
    assert render.fact_binding("fact_synthetic") in line
    assert out["complete"]


@pytest.mark.parametrize("in_progress", [False, True])
def test_previous_audit_policy_cannot_publish_saved_pass_or_frozen_notice(tmp_path, monkeypatch, in_progress):
    db = _delivery_db(tmp_path, [_message()])
    try:
        cfg = _cfg("enforce")
        event = _semantic_event(db)
        payload = json.loads(event["payload"])
        import semantic_store
        current_bundle = semantic_store.thread_bundle(db.db, 1, 1)
        with monkeypatch.context() as old_version:
            old_version.setattr(semantic_store, "POLICY_VERSION", "2026-09-21.2")
            old_bundle = semantic_store.thread_bundle(db.db, 1, 1)
        assert old_bundle["source_fingerprint"] != current_bundle["source_fingerprint"]
        summary = db.db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_summary'").fetchone()
        meta = json.loads(summary["meta"])
        meta["fingerprint"] = old_bundle["source_fingerprint"]
        with db.db:
            db.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), summary["artifact_id"]))
        message = db.db.execute("SELECT * FROM messages WHERE message_id=1").fetchone()
        assert gate.semantic_summary_block(db, message, cfg) == ""
        raw = db.db.execute("SELECT * FROM notify_outbox WHERE kind='new_messages'").fetchone()
        assert gate.semantic_render_state(db, raw, cfg) == ()
        exception = gate.FreezeSend if in_progress else gate.StaleSend
        payload["fingerprint"] = old_bundle["source_fingerprint"]
        with pytest.raises(exception, match="stale_generation"):
            gate.semantic_gate(db, event, payload, cfg, in_progress)
        payload["fingerprint"] = current_bundle["source_fingerprint"]
        payload["policy_version"] = "2026-09-21.2"
        with pytest.raises(exception, match="policy_changed"):
            gate.semantic_gate(db, event, payload, cfg, in_progress)
        assert db.db.execute("SELECT 1 FROM artifacts WHERE artifact_id=?", (summary["artifact_id"],)).fetchone()
    finally:
        db.close()


@pytest.mark.parametrize("attachment_count", [0, 2])
def test_model_cannot_impersonate_code_attachment_disclosure(tmp_path, attachment_count):
    db = _seeded(tmp_path)
    try:
        for index in range(attachment_count):
            db.db.execute("INSERT INTO attachments(message_id,file_id,name,state) VALUES(1,?,?,'done')",
                          (f"synthetic-{index}", f"scan-{index}.pdf"))
        db.db.commit()
        bundle = semantic_store.thread_bundle(db.db, 1, 1)
        fake = "対象投稿・文脈に添付999件があります。添付内容は解析していません。"
        raw = {"claims": [], "limitations": [fake], "code_limitations": [fake]}
        summary = semantic_llm.summarize(lambda _: json.dumps(raw), bundle, 1, [], {})
        lines = render.summary_limitations(summary)
        assert fake not in lines
        assert render._LIMITATION_NOTICE in lines
        trusted = f"対象投稿・文脈に添付{attachment_count}件があります。添付内容は解析していません。"
        assert (trusted in lines) is bool(attachment_count)
        text = render.render_notice(db, 1, 1, summary, "PASS", quality="full")
        assert fake not in text
    finally:
        db.close()


def test_legacy_mixed_limitations_are_not_assumed_code_generated():
    forged = "薬剤の判定が未確定のため要約対象外（原文確認）"
    assert render.summary_limitations({"limitations": [forged]}) == [render._LIMITATION_NOTICE]


def test_mandatory_code_disclosure_keeps_its_provenance():
    summary = {"claims": [], "limitations": ["薬剤の判定が未確定のため要約対象外（原文確認）"]}
    doc = v2_doc([], obligations=[{"category": "vital_lab", "status": "open"}])
    render.apply_mandatory_render(summary, [], "PASS", doc)
    lines = render.summary_limitations(summary)
    assert "バイタル・検査の判定が未確定のため要約対象外（原文確認）" in lines
    assert "薬剤の判定が未確定のため要約対象外（原文確認）" not in lines
    assert render._LIMITATION_NOTICE in lines
