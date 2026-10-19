"""Synthetic report parity against the frozen pre-move implementation (8149d40e)."""
import copy
import hashlib
import json
import math
import re
import pytest

import patient_context
from patient_context import context_items


def valid_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


PHARMACY_STATE_KEYS = frozenset({"inquiry_result", "storage_status", "storage_place",
    "medication_manager", "interaction_check", "support_method", "residual_quantity",
    "missed_doses", "followup"})


def original_pharmacy_state_items(artifact, message) -> list[dict]:
    """Source-grounded pharmacy reports from one decoded current extraction, preserving each parent."""
    message = dict(message)
    if (message.get("body_state") not in (None, "full")
            or type(message.get("project_id")) is not int or message["project_id"] <= 0
            or type(message.get("message_id")) is not int or message["message_id"] <= 0
            or not valid_hash(message.get("content_hash"))):
        return []
    result = []
    for item in context_items(artifact, message.get("body_text")):
        parent = {key: item[key] for key in ("category", "text", "evidence", "subject")}
        parent["details"] = item.get("details", [])
        identity = {key: parent[key] for key in ("category", "text", "evidence", "subject")}
        parent_fp = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        names = sorted({detail["value"] for detail in parent["details"] if detail["key"] == "drug_name"})
        for detail in parent["details"]:
            if detail["key"] not in PHARMACY_STATE_KEYS:
                continue
            own_detail = {key: detail[key] for key in ("key", "value", "evidence")}
            fingerprint = hashlib.sha256(json.dumps([parent_fp, own_detail, names],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            stamp = message.get("posted_at_ts")
            result.append({**detail, "parent": parent, "parent_fingerprint": parent_fp,
                "fingerprint": fingerprint, "drug_names": names, "subject": parent["subject"],
                "project_id": message["project_id"], "message_id": message["message_id"],
                "content_hash": message["content_hash"], "posted_at": message.get("posted_at"),
                "posted_at_ts": stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None,
                "source": "chat"})
    return result


def sample(*, subject="patient", wording="残薬3錠。", names=("合成薬A",), key="residual_quantity", value="3錠"):
    quote = "本人の" + "と".join(names) + "は" + wording
    parent = {"category": "medication_management", "text": quote, "evidence": quote,
              "subject": subject, "details": [
                  *({"key": "drug_name", "value": name, "evidence": quote} for name in names),
                  {"key": key, "value": value, "evidence": quote}]}
    return {"patient_context": [parent]}, {"project_id": 1, "message_id": 2,
        "body_text": quote, "content_hash": "a" * 64, "body_state": "full",
        "posted_at": "2026-10-01T10:00:00+09:00", "posted_at_ts": 1790816400.0}


def assert_original(document, message):
    before = copy.deepcopy((document, message))
    try:
        expected = original_pharmacy_state_items(document, message)
    except (OverflowError, TypeError, ValueError) as exc:
        with pytest.raises(type(exc)):
            patient_context.pharmacy_state_items(document, message)
    else:
        assert patient_context.pharmacy_state_items(document, message) == expected
    assert (document, message) == before


@pytest.mark.parametrize("subject", ["patient", "family", "other", "unspecified"])
@pytest.mark.parametrize("wording", ["残薬3錠。", "以前は残薬3錠。", "明日は残薬3錠の予定。",
                                    "残薬3錠ではない。", "残薬3錠かは未確認。"])
def test_subject_and_temporal_wording_remain_raw_reports(subject, wording):
    document, message = sample(subject=subject, wording=wording)
    assert_original(document, message)
    report, = patient_context.pharmacy_state_items(document, message)
    assert report["subject"] == subject and report["parent"]["text"].endswith(wording)


@pytest.mark.parametrize("field,value", [
    ("body_state", "partial"), ("body_state", "deleted"),
    ("project_id", True), ("project_id", 0), ("project_id", "1"),
    ("message_id", False), ("message_id", -1), ("message_id", 2.0),
    ("content_hash", None), ("content_hash", 123), ("content_hash", "a" * 63),
    ("content_hash", "A" * 64), ("content_hash", "g" * 64), ("content_hash", "a" * 64 + "\n"),
])
def test_ineligible_source_never_creates_a_reference(field, value):
    document, message = sample()
    message[field] = value
    assert_original(document, message)
    assert patient_context.pharmacy_state_items(document, message) == []


@pytest.mark.parametrize("stamp", [None, "1790816400", True, float("inf"), float("-inf"),
                                    0, -1, 1.5, 10**400])
def test_timestamp_type_finiteness_and_original_exception_boundary(stamp):
    document, message = sample()
    message["posted_at_ts"] = stamp
    assert_original(document, message)


def test_nan_timestamp_is_not_promoted():
    document, message = sample()
    message["posted_at_ts"] = float("nan")
    assert original_pharmacy_state_items(document, message) == patient_context.pharmacy_state_items(document, message)
    assert patient_context.pharmacy_state_items(document, message)[0]["posted_at_ts"] is None


@pytest.mark.parametrize("mutation", ["value_outside_quote", "evidence_outside_parent", "repeated_quote",
                                      "non_medication_category", "family_owned_drug"])
def test_negative_grounding_oracle(mutation):
    document, message = sample(key="storage_place", value="冷蔵庫", wording="保管場所は冷蔵庫。")
    parent = document["patient_context"][0]
    detail = parent["details"][-1]
    if mutation == "value_outside_quote":
        detail["value"] = "棚"
        message["body_text"] += "別の記録は棚。"
    elif mutation == "evidence_outside_parent":
        detail["evidence"] = "別の記録は冷蔵庫。"
        message["body_text"] += detail["evidence"]
    elif mutation == "repeated_quote":
        message["body_text"] += message["body_text"]
    elif mutation == "non_medication_category":
        parent["category"] = "living"
    else:
        message["body_text"] = message["body_text"].replace("本人の", "家族の薬、")
        parent["text"] = parent["evidence"] = message["body_text"]
        for item in parent["details"]:
            item["evidence"] = message["body_text"]
    assert_original(document, message)
    assert patient_context.pharmacy_state_items(document, message) == []


def test_multiple_parents_values_and_drugs_preserve_order_and_group_identity():
    document, message = sample(names=("合成薬B", "合成薬A", "合成薬B"))
    parent = document["patient_context"][0]
    message["body_text"] += "別袋は8錠。"
    parent["text"] = parent["evidence"] = message["body_text"]
    parent["details"].append({"key": "residual_quantity", "value": "8錠", "evidence": "別袋は8錠。"})
    other_document, other_message = sample(subject="family", names=("合成薬C",))
    document["patient_context"] += other_document["patient_context"]
    message["body_text"] += "\n" + other_message["body_text"]
    assert_original(document, message)
    reports = patient_context.pharmacy_state_items(document, message)
    assert [(item["drug_names"], item["value"], item["subject"]) for item in reports] == [
        (["合成薬A", "合成薬B"], "3錠", "patient"),
        (["合成薬A", "合成薬B"], "8錠", "patient"),
        (["合成薬C"], "3錠", "family")]
    assert reports[0]["parent_fingerprint"] == reports[1]["parent_fingerprint"]
    assert len({item["fingerprint"] for item in reports}) == 3


@pytest.mark.parametrize("key", sorted(PHARMACY_STATE_KEYS))
def test_each_original_pharmacy_key_stays_in_the_owned_contract(key):
    document, message = sample(key=key)
    assert_original(document, message)
    assert [item["key"] for item in patient_context.pharmacy_state_items(document, message)] == [key]


@pytest.mark.parametrize("value", [None, 123, "", "a" * 63, "A" * 64, "a" * 64 + "\n", "0" * 64, "f" * 64])
def test_reference_hash_grammar_matches_the_frozen_original(value):
    assert patient_context.valid_reference_hash(value) == valid_hash(value)
