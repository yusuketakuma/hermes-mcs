"""Prompt layouts keep their fixed head first so llama.cpp's per-slot
prefix cache is reusable; cache_hit_ratio aggregates Σcache_n/(Σcache_n+Σprompt_n)."""
import semantic_assembly as assembly
import json
import os
import sys

import extract_llm
import patient_context_refresh
import semantic_extraction as extraction
import semantic_llm
from test_semantic_facts_v2 import FP

POSTED = "2026-01-01T09:00:00+09:00"


def test_single_batch_repair_share_fixed_leading_bytes(monkeypatch):
    prompts = []

    def call(prompt, **kw):
        prompts.append(prompt)
        return {"summary": "合成"}
    monkeypatch.setattr(extract_llm, "_llm_call", call)
    body = "合成の短い本文です。"
    extract_llm.llm_extract(body, context="文脈A", posted_at=POSTED)
    extract_llm.llm_extract(body, context="文脈B", hints={"meds": []}, posted_at=POSTED)
    single = os.path.commonprefix(prompts[:2])
    assert single.startswith(extract_llm._PROMPT_HEAD)
    batches = [extract_llm._batch_prompt([("合成本文一", POSTED, None)]),
               extract_llm._batch_prompt([("合成本文二", POSTED, None), ("合成本文三", POSTED, None)])]
    assert os.path.commonprefix(batches).startswith(extract_llm._BATCH_HEAD)
    assert os.path.commonprefix([prompts[0], batches[0]]).startswith(extract_llm._PROMPT_SPEC)
    repairs = [extract_llm._repair_prompt(POSTED, ["問題A"], {"summary": "一"}, None, ""),
               extract_llm._repair_prompt(POSTED, ["別の問題"], {"summary": "二"}, None,
                                          extract_llm._CTX_HEAD + "参考" + extract_llm._CTX_TAIL)]
    assert os.path.commonprefix(repairs) == extract_llm._REPAIR_HEAD


def test_split_piece_repair_puts_fixed_head_before_reference(monkeypatch):
    body = "合成の文です。" * 40
    prompts = []

    def call(prompt, **kw):
        prompts.append(prompt)
        if "前回出力" in prompt:
            return {"summary": "合成"}
        return {"meds": [{"name": "薬剤A", "evidence": "ない引用", "subject": "patient"}]}
    monkeypatch.setattr(extract_llm, "_llm_call", call)
    extract_llm.llm_extract(body, posted_at=POSTED, chunk_size=100)
    repair = next(p for p in prompts if "前回出力" in p)
    assert repair.startswith(extract_llm._REPAIR_HEAD)
    close, ctx = repair.index(extract_llm._REPAIR_CLOSE), repair.index(extract_llm._CTX_HEAD)
    assert close < ctx < repair.index("対象本文(投稿日時")
    piece = repair.rsplit("<<<\n", 1)[1].split("\n>>>", 1)[0]
    assert piece and body.startswith(piece) and repair.endswith(piece + extract_llm._PROMPT_TAIL)
    # the only raw fence close before the reference is the prior-output one
    assert repair[:ctx].count(">>>") == 1
    for rule in ("vitals", "meds", "details"):
        assert rule in extract_llm._REPAIR_HEAD and rule in repair


def test_repair_prompt_without_reference_is_byte_stable():
    issues, prior = ["問題1", "問題2"], {"summary": "途中 >>> 終わり"}
    expected = (extract_llm._REPAIR_HEAD + "\n".join(issues) + extract_llm._REPAIR_MID
                + extract_llm._sanitize_ctx(json.dumps(prior, ensure_ascii=False))
                + extract_llm._REPAIR_CLOSE + extract_llm._target_head(POSTED))
    assert extract_llm._repair_prompt(POSTED, issues, prior) == expected
    assert extract_llm._repair_prompt(POSTED, issues, prior, None, "") == expected
    assert ">>>" not in expected.split(extract_llm._REPAIR_MID, 1)[1].split(extract_llm._REPAIR_CLOSE, 1)[0]
    assert "＞＞＞" in expected


def test_context_refresh_prompts_lead_with_the_fixed_instruction(monkeypatch):
    payloads = []

    def ask(cfg, payload, deadline, admit):
        payloads.append(payload)
        raise patient_context_refresh.Deferred("captured")
    monkeypatch.setattr(patient_context_refresh, "_ask", ask)
    fact = {"id": "f1", "source": "s", "project_id": 1, "message_id": 1, "category": "living",
            "text": "合成の背景", "posted_at": POSTED, "subject": "patient"}
    for fn, args in ((patient_context_refresh._summary, ({}, [fact], None, lambda: True)),
                     (patient_context_refresh._scan, ({}, {"body_text": "合成の本文", "items": []},
                                              {}, "n1", None, lambda: True))):
        try:
            fn(*args)
        except patient_context_refresh.Deferred:
            pass
    assert list(payloads[0]) == ["instruction", "reports"]
    assert list(payloads[1])[:2] == ["instruction", "categories"]
    for payload in payloads:
        assert json.dumps(payload, ensure_ascii=False).startswith('{"instruction": "')


def test_semantic_chunk_prompt_leads_with_fixed_template():
    body = "家族\n" + "・架空薬A1mgは使用せず中止予定。\n" * 6 + "本人\n・架空薬B2mgを朝に服用。"
    manifest = assembly.build_manifest(body, FP, chunk_size=50)
    target = next(spec for spec in assembly.manifest_specs(manifest)
                  if spec["dependency_atom_ids"] and "家族" not in spec["text"])
    prompt = extraction._chunk_prompt(semantic_llm._FACT_V2_PROMPT, target, manifest, body, 50)
    assert prompt.startswith(semantic_llm._FACT_V2_PROMPT.split("<<<", 1)[0])
    assert prompt.index("参考文脈") > prompt.index(">>>")
    for template in (semantic_llm._FACT_V2_PROMPT, semantic_llm._SUMMARY_PROMPT):
        assert template.index("%s") > template.index("<<<")


def test_cache_hit_ratio_aggregate():
    stats = extract_llm._llm_call_stats([
        {"calls": 2, "timings": {"prompt_n": 300, "cache_n": 900}},
        {"calls": 1, "timings": {"prompt_n": 100, "cache_n": 100}}])
    assert stats["cache_hit_ratio"] == 0.714 and stats["calls"] == 3
    for only in ({"cache_n": 5}, {"prompt_n": 5}):
        stats = extract_llm._llm_call_stats([{"calls": 1, "timings": {**only, "prompt_ms": 2.0}}])
        assert stats["cache_hit_ratio"] is None and stats["prompt_n"] is None
        assert stats["calls"] == 1 and stats["prompt_ms"] == 2.0
    mixed = extract_llm._llm_call_stats([
        {"calls": 1, "timings": {"prompt_n": 100, "cache_n": 100}},
        {"calls": 1, "timings": {"prompt_n": 700}}])
    assert mixed["cache_hit_ratio"] == 0.5 and mixed["prompt_n"] == 100
    assert extract_llm._llm_call_stats([{"calls": 1, "timings": {"prompt_n": 0, "cache_n": 0}}])["cache_hit_ratio"] is None
    big = extract_llm._llm_call_stats([
        {"calls": 1, "timings": {"prompt_n": sys.float_info.max, "cache_n": 1.0}}
        for _ in range(2)])
    assert big["cache_hit_ratio"] is None and big["calls"] == 2
    assert big["prompt_n"] is None and big["cache_n"] is None
    json.dumps(big, allow_nan=False)


def test_cache_ratio_pairs_measurements_before_per_row_aggregation():
    for missing in ({"prompt_n": 700}, {"cache_n": 700}):
        summary = extract_llm._integrity_summary([
            {"kind": "repair", "timings": {"prompt_n": 100, "cache_n": 100}},
            {"kind": "repair", "timings": {**missing, "prompt_ms": 2.0}},
        ])
        stats = extract_llm._llm_call_stats([summary])
        assert stats["cache_hit_ratio"] == 0.5
        assert (stats["prompt_n"], stats["cache_n"],
                stats["repair_prompt_n"], stats["repair_cache_n"]) == (100, 100, 100, 100)
        assert stats["calls"] == 2 and stats["prompt_ms"] == 2.0
    summary = extract_llm._integrity_summary([
        {"timings": {"prompt_n": 100}}, {"timings": {"cache_n": 100}},
    ])
    stats = extract_llm._llm_call_stats([summary])
    assert stats["cache_hit_ratio"] is None
    assert stats["prompt_n"] is None and stats["cache_n"] is None


def test_model_target_scope_is_owned_once_and_restores_on_nested_failure(monkeypatch):
    import pytest
    outer = ("http://outer.invalid", "synthetic-outer")
    first = ("http://first.invalid", "synthetic-first")
    second = ("http://second.invalid", "synthetic-second")
    notes = []
    monkeypatch.setattr(extract_llm._CALL_NOTES, "target", outer, raising=False)
    monkeypatch.setattr(extract_llm._CALL_NOTES, "items", notes, raising=False)
    with extract_llm.pin_target(first):
        assert extract_llm._resolved_llm() == first
        with pytest.raises(RuntimeError, match="synthetic"):
            with extract_llm.pin_target(second):
                assert extract_llm._resolved_llm() == second
                raise RuntimeError("synthetic")
        assert extract_llm._resolved_llm() == first
    assert extract_llm._resolved_llm() == outer
    assert extract_llm._CALL_NOTES.items is notes and notes == []
