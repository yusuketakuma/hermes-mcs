"""Synthetic current supplemental summaries retain their exact source binding."""
import json

import pytest

import mcs_queries
from inference_version import CURRENT_VERSION, VERSION_KEY
from semantic_testkit import _cfg, _delivery_db, _message, _summary


def _source(led, *, hydrate=True, cfg=None):
    return next(mcs_queries.patient_context_sources(led.db, 1, mid=1, hydrate=hydrate, cfg=cfg))


def _local(led, text="合成の補助要約", *, kind="extract_llm", pid=1, **meta):
    body_hash = led.db.execute("SELECT content_hash FROM messages WHERE message_id=1").fetchone()[0]
    return led.artifact_add(kind, json.dumps({"summary": text}, ensure_ascii=False), project_id=pid,
        message_id=1, model="synthetic-local", meta={VERSION_KEY: CURRENT_VERSION, "hash": body_hash, **meta})


def test_current_local_full_text_signature_and_hydrate_parity(tmp_path):
    led = _delivery_db(tmp_path, [_message()])
    try:
        text = "合成補助要約\n" * 3000
        aid = _local(led, text)
        source = _source(led)
        assert source["derived_summaries"] == [{"kind": "extract_llm", "artifact_id": aid, "text": text}]
        shallow = _source(led, hydrate=False)
        assert shallow["derived_summary_sig"] == source["derived_summary_sig"]
        assert shallow["extraction_sig"] == source["extraction_sig"]
        assert shallow["derived_summaries"] == []
        assert shallow["derived_summary_chars"] == source["derived_summary_chars"] == len(text)
        assert "body_text" not in shallow
        row = led.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()
        meta = json.loads(row[0])
        meta.update(generated_at=123, fetched_at=456)
        with led.db:
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), aid))
        assert _source(led)["derived_summary_sig"] == source["derived_summary_sig"]
        with led.db:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps({"summary": "変更後"}), aid))
        changed = _source(led)
        assert changed["derived_summary_sig"] != source["derived_summary_sig"]
        assert changed["derived_summary_sig"] == _source(led, hydrate=False)["derived_summary_sig"]
    finally:
        led.close()


@pytest.mark.parametrize("override", [{"hash": "stale"}, {VERSION_KEY: "old"},
    {"pid": 2}, {"invalidated": True}, {"error": True},
    {"internal_proof_version": 1, "internal_proof_hash": "0" * 64}])
def test_stale_foreign_old_or_unproven_local_summary_is_excluded(tmp_path, override):
    led = _delivery_db(tmp_path, [_message()])
    try:
        empty = _source(led)["derived_summary_sig"]
        if override.get("pid") == 2:
            # Exercise the reader against a pre-guard/corrupt foreign artifact.
            led.db.execute("DROP TRIGGER g1_artifacts_msg_ins")
        _local(led, **override)
        led.artifact_add("patient_context_nightly", json.dumps({"summary": "過去の再帰要約"}), project_id=1)
        assert _source(led)["derived_summaries"] == []
        assert _source(led)["derived_summary_sig"] == empty
    finally:
        led.close()


def test_rule_and_local_summaries_remain_distinct(tmp_path):
    led = _delivery_db(tmp_path, [_message()])
    try:
        aid = _local(led, "ルール", kind="extract_v1")
        bid = _local(led, "局所推論")
        assert _source(led)["derived_summaries"] == [
            {"kind": "extract_llm", "artifact_id": bid, "text": "局所推論"},
            {"kind": "extract_v1", "artifact_id": aid, "text": "ルール"}]
    finally:
        led.close()


def test_validated_semantic_summary_uses_existing_gate_and_explicit_policy(tmp_path):
    led = _delivery_db(tmp_path, [_message()])
    try:
        _summary(led, claims=20)
        cfg = _cfg("enforce")
        source = _source(led, cfg=cfg)
        saved = led.artifacts("semantic_summary", message_id=1)[-1]
        assert source["derived_summaries"] == [{"kind": "semantic_summary", "artifact_id": saved["artifact_id"],
            "text": json.dumps(json.loads(saved["content"]), ensure_ascii=False, sort_keys=True)}]
        assert len(json.loads(source["derived_summaries"][0]["text"])["claims"]) == 20
        assert source["derived_summary_chars"] == len(source["derived_summaries"][0]["text"])
        assert source["derived_summary_chars"] == _source(led, cfg=cfg, hydrate=False)["derived_summary_chars"]
        assert source["derived_summary_sig"] == _source(led, cfg=cfg, hydrate=False)["derived_summary_sig"]
        for invalid_cfg in (None, {}, _cfg("shadow"), _cfg("enforce", calibration_version="changed-policy")):
            assert _source(led, cfg=invalid_cfg)["derived_summaries"] == []
        # Modifying a claimed summary without a matching audit invalidates the pair.
        with led.db:
            led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?",
                (json.dumps({"claims": [{"text": "未監査の変更"}]}), saved["artifact_id"]))
        assert _source(led, cfg=cfg)["derived_summaries"] == []
        assert _source(led, cfg=cfg)["derived_summary_sig"] != source["derived_summary_sig"]
    finally:
        led.close()


@pytest.mark.parametrize("change", [{VERSION_KEY: "old"}, {"fingerprint": "stale"},
    {"publication_mode": "shadow"}, {"invalidated": True}, {"audit_status": "FAIL"}])
def test_semantic_summary_rejects_stale_or_unverified_receipts(tmp_path, change):
    led = _delivery_db(tmp_path, [_message()])
    try:
        _summary(led)
        saved = led.artifacts("semantic_summary", message_id=1)[-1]
        meta = {**json.loads(saved["meta"]), **change}
        with led.db:
            led.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=?", (json.dumps(meta), saved["artifact_id"]))
        assert _source(led, cfg=_cfg("enforce"))["derived_summaries"] == []
    finally:
        led.close()


def _ocr_local(led, key, cfg, text):
    import attachment_extraction
    import original_source_keys
    from mcs_util import payload_hash

    source = original_source_keys.source(led.db, 1, key)
    document = {"symptoms": [], "summary": text}
    quality = {"completeness_chunks": 1, "completeness": {
        "0": {"state": "hint_gaps_resolved", "remaining_hint_candidates": 0}}}
    assert attachment_extraction.validated(document, source["body_text"], quality)
    value = {"schema": attachment_extraction.SCHEMA, "source_key": key,
        "source_binding": source["_source_binding"], "stage": "local",
        "contract": attachment_extraction.contract(cfg), "document": document,
        "result_hash": payload_hash(document), "quality": quality,
        "provider_evidence": {"complete": True, "providers": ["local"]},
        "provider_contract": None, "local_artifact_id": None, "reservation_artifact_ids": []}
    return led.artifact_add(attachment_extraction.KIND, json.dumps(value), project_id=1,
        message_id=source["parent_message_id"], meta={"source_key": key, "stage": "local"})


def test_attachment_summary_uses_real_current_validator_and_stable_source_binding(tmp_path):
    import attachment_extraction
    from test_ocr_original_sources import seeded

    led, key = seeded(tmp_path)
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"},
           "cloud_llm": {"mode": "off"}}
    try:
        text = "合成薬Aの中止を記録。" * 5
        aid = _ocr_local(led, key, cfg, text)
        assert attachment_extraction.current(led.db, 1, key, cfg=cfg)["ready"] is True
        def read(hydrate=True):
            return next(mcs_queries.attachment_ocr_sources(led.db, 1, mid=key, cfg=cfg, hydrate=hydrate))
        full, lean = read(), read(False)
        assert full["derived_summaries"] == [{"kind": "attachment_extract_local", "artifact_id": aid, "text": text}]
        assert full["body_text"] != text and full["source_key"] == key
        assert full["derived_summary_chars"] == lean["derived_summary_chars"] == len(text)
        assert full["derived_summary_sig"] == lean["derived_summary_sig"]
        assert lean["derived_summaries"] == [] and "body_text" not in lean
        with led.db:
            led.db.execute("UPDATE attachments SET downloaded_at=2 WHERE attachment_id=1")
            led.db.execute("UPDATE artifacts SET created_at=3 WHERE artifact_id=?", (aid,))
        assert read()["derived_summary_sig"] == full["derived_summary_sig"]
        assert not list(mcs_queries.attachment_ocr_sources(led.db, 2, mid=key, cfg=cfg))
        _ocr_local(led, key, cfg, "変更された合成要約")
        assert read()["derived_summary_sig"] != full["derived_summary_sig"]
    finally:
        led.close()


@pytest.mark.parametrize("fault", ["contract", "proof", "unready"])
def test_attachment_summary_excludes_stale_unverified_or_unready_analysis(tmp_path, fault, monkeypatch):
    import attachment_extraction
    from test_ocr_original_sources import seeded

    led, key = seeded(tmp_path)
    cfg = {"attachment_ocr": {"enabled": True, "executable": "/synthetic/yomitoku"},
           "cloud_llm": {"mode": "off"}}
    try:
        aid = _ocr_local(led, key, cfg, "合成薬Aの中止を記録。")
        if fault == "unready":
            import cloud_policy
            from anthropic_client import AnthropicClient
            from test_cloud_response_recovery import legacy_cloud_routes

            cfg["cloud_llm"].update(mode="on", provider="anthropic", project_ids=[1])
            # The closed route leaves an unowned local page ready, even with cloud enabled.
            assert attachment_extraction.current(led.db, 1, key, cfg=cfg)["ready"] is True
            available = next(mcs_queries.attachment_ocr_sources(led.db, 1, mid=key, cfg=cfg))
            assert available["extraction_ready"] is True and available["derived_summaries"]
            monkeypatch.setattr(cloud_policy, "pricing_valid", lambda *args, **kwargs: True)
            reserve = cloud_policy.usage_reserver(led, cfg, route="reextract", project_id=1,
                unit_key=key + ":cloud", owner={"source_type": "attachment_ocr", "source_key": key,
                                             "local_artifact_id": aid})
            # Seed only the historical reservation: no response proves this unit completed.
            with legacy_cloud_routes():
                reservation = reserve(AnthropicClient("synthetic-unused").body(
                    "synthetic unresolved OCR input", 100, None), 1)
            assert not cloud_policy.send_route("reextract")
            assert reservation["unit_key"] == key + ":cloud"
            assert len(led.artifacts("cloud_usage_reserve")) == 1
            assert not led.artifacts("cloud_usage_result")
            assert attachment_extraction.stored(led.db, 1, key, "local", cfg)["artifact_id"] == aid
        else:
            saved = json.loads(led.db.execute("SELECT content FROM artifacts WHERE artifact_id=?", (aid,)).fetchone()[0])
            if fault == "contract":
                saved["contract"] = "stale"
            else:
                saved["provider_evidence"]["complete"] = False
            with led.db:
                led.db.execute("UPDATE artifacts SET content=? WHERE artifact_id=?", (json.dumps(saved), aid))
        analysis = attachment_extraction.current(led.db, 1, key, cfg=cfg)
        assert analysis is None or analysis["ready"] is False
        source = next(mcs_queries.attachment_ocr_sources(led.db, 1, mid=key, cfg=cfg))
        assert source["derived_summaries"] == [] and source["derived_summary_chars"] == 0
        assert source["extraction_ready"] is False
    finally:
        led.close()
