"""Pin the scan-input derivation of patient_context.py to PATIENT_CONTEXT_VERSION.

A source signature binds the stored context items by content and the body by
``content_hash``, but not the code that turns them into the scan input (quote
matching, heading extraction, merging and projection). The scan contract's
``context`` version stands in for that code. This test fails when the derived
output of a fixed, fully synthetic corpus changes while the version stays: bump
``extract_llm.PATIENT_CONTEXT_VERSION`` and record the new digest for it.
"""
import hashlib
import json

import extract_llm
from patient_context import context_items, extract_context, merged_context, project_context_items

# Digest of _derived() per PATIENT_CONTEXT_VERSION. Add an entry with each bump;
# never change an existing one.
PINNED = {4: "bd6bb0ad32a75409cd4fbed543026041364af9807be763694313bcb7cefb10e2"}

BODIES = (
    "生活環境: 合成の独居。日中は自宅で過ごす。\n既往歴: 合成の高血圧。\n連絡: 次回訪問は未定。",
    "【体重】52.4kg 2026/09/01に測定\n【血糖】110 mg/dL\n■服薬管理\n合成の降圧薬を朝1回、一包化で管理。",
    "主病名：合成の心不全\nアレルギー：なし\n本人の意向: 自宅での療養を希望。\n備考: 合成の補足。",
    "詳細のない合成の連絡です。",
)
SOURCE = {"project_id": 1, "message_id": 1, "content_hash": "0" * 64, "posted_at_ts": 0}


def _llm_document(body):
    """A stored LLM document: one verbatim item, one unverifiable quote and, where the
    body has one, a duplicate of a heading item that adds an attribute."""
    first = body.splitlines()[0]
    items = [{"category": "course", "text": first, "evidence": first, "subject": "patient", "origin": "llm"},
             {"category": "living", "text": "合成の存在しない記述", "evidence": "原文にない引用", "subject": "patient"}]
    for heading in extract_context(body):
        if heading["category"] == "medication_management" and "朝1回" in heading["text"]:
            items.append({**heading, "details": [
                {"key": "actual_frequency", "value": "朝1回", "evidence": heading["evidence"]}]})
    return {"patient_context": items}


def _derived():
    out = []
    for body in BODIES:
        headings = extract_context(body)
        merged = merged_context({"patient_context": headings}, _llm_document(body), body)
        detail = project_context_items(merged, "detail", SOURCE)
        out.append({"headings": headings, "merged": merged,
                    "scan": context_items({"patient_context": detail + headings}, body),
                    "aggregate": project_context_items(merged, "aggregate", SOURCE)})
    return hashlib.sha256(json.dumps(out, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def test_scan_input_derivation_change_requires_a_context_version_bump():
    digest = _derived()
    assert PINNED.get(extract_llm.PATIENT_CONTEXT_VERSION) == digest, (
        "patient_context.py now derives a different scan input; bump "
        "extract_llm.PATIENT_CONTEXT_VERSION and add PINNED[new version] = " + repr(digest))


def test_pin_corpus_exercises_headings_details_and_quote_filtering():
    # Guard against a corpus that pins nothing.
    heads = [extract_context(body) for body in BODIES]
    assert all(heads[:3]) and heads[3] == []
    assert any(item.get("details") for group in heads for item in group)
    merged = merged_context(None, _llm_document(BODIES[0]), BODIES[0])
    assert [item["category"] for item in merged] == ["course"]
    merged = merged_context({"patient_context": heads[1]}, _llm_document(BODIES[1]), BODIES[1])
    assert any(detail["key"] == "actual_frequency"
               for item in merged for detail in item.get("details") or [])
