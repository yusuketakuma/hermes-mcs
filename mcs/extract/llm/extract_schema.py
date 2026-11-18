"""JSON output shapes and shared medication syntax for extraction validation."""
from copy import deepcopy
import re

import clinical_allergies
from clinical_events import CARE_DETAIL_TYPES
import patient_context
from patient_context import CATEGORIES, DETAIL_KEYS

SCHEMA = {
    "name": "mcs_extract",
    "schema": {
        "type": "object",
        "properties": {
            "meds": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dose": {"type": ["string", "null"]},
                    "action": {"type": ["string", "null"],
                               "enum": ["start", "stop", "change",
                                        "decrease", "increase", "none",
                                        None]},
                    "status": {"type": "string",
                               "enum": ["current", "past", "planned"]},
                    "subject": {"type": "string",
                                "enum": ["patient", "family", "other"]},
                    "negated": {"type": "boolean"},
                    "route": {"type": ["string", "null"],
                              "enum": ["oral", "topical", "injection",
                                       "infusion", "inhalation", "tube",
                                       "other", None]},
                    "freq": {"type": ["string", "null"]},
                    "prn": {"type": "boolean"},
                    "evidence": {"type": "string"}},
                "required": ["name"],
                "additionalProperties": False}},
            "symptoms": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "negated": {"type": "boolean"},
                    "status": {"type": "string",
                               "enum": ["new", "ongoing", "resolved",
                                        "past"]},
                    "subject": {"type": "string",
                                "enum": ["patient", "family", "other"]},
                    "severity": {"type": ["string", "null"],
                                 "enum": ["mild", "moderate", "severe",
                                          None]},
                    "onset": {"type": ["string", "null"]},
                    "duration": {"type": ["string", "null"]},
                    "evidence": {"type": "string"}},
                "required": ["text", "negated"],
                "additionalProperties": False}},
            "events": {"type": "array", "items": {
                "type": "string",
                "enum": ["visit", "exam", "admission", "discharge",
                         "transfer", "fall", "eol", "care",
                         "family_contact", "prescription_inquiry", "other"]}},
            "event_records": {"type": "array", "items": {
                "type": "object", "properties": {
                    "kind": {"type": "string", "enum": [
                        "visit", "exam", "admission", "discharge", "transfer", "fall",
                        "eol", "care", "family_contact", "prescription_inquiry", "other"]},
                    "subject": {"type": "string", "enum": ["patient", "family", "other", "unknown"]},
                    "status": {"type": "string", "enum": ["current", "past", "planned", "conditional", "unknown"]},
                    "negated": {"type": ["boolean", "null"]},
                    "evidence": {"type": "string"},
                    "span": {"type": ["object", "null"], "properties": {
                        "start": {"type": "integer", "minimum": 0},
                        "end": {"type": "integer", "minimum": 1}},
                        "required": ["start", "end"], "additionalProperties": False},
                    "care_detail": {"type": ["object", "null"], "properties": {
                        "event_type": {"type": "string", "enum": list(CARE_DETAIL_TYPES)},
                        "workflow_status": {"type": ["string", "null"]},
                        "date_text": {"type": ["string", "null"]},
                        "service_actor": {"type": ["string", "null"]}},
                        "required": ["event_type"], "additionalProperties": False},
                    "unverified": {"type": "boolean"}},
                "required": ["kind", "subject", "status", "negated", "evidence"],
                "additionalProperties": False}},
            "requests": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "to": {"type": ["string", "null"]},
                    "from": {"type": ["string", "null"]},
                    "kind": {"type": ["string", "null"],
                             "enum": ["request", "question", "self_plan",
                                      None]},
                    "action": {"type": ["string", "null"]},
                    "condition": {"type": ["string", "null"]},
                    "due": {"type": ["string", "null"]},
                    "due_text": {"type": ["string", "null"]},
                    "evidence": {"type": "string"}},
                "required": ["to", "from", "kind", "action"],
                "additionalProperties": False}},
            "vitals": {"type": "object", "properties": {
                k: {"type": "number"} for k in
                ("bt", "hr", "rr", "sbp", "dbp", "spo2", "bs")},
                "additionalProperties": False},
            "labs": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "value": {"type": ["string", "number"]},
                    "unit": {"type": ["string", "null"]},
                    "flag": {"type": ["string", "null"],
                             "enum": ["high", "low", None]},
                    "evidence": {"type": "string"},
                    "subject": {"type": "string", "enum": ["patient", "family", "other"]},
                    "status": {"type": "string", "enum": ["current", "past", "planned"]},
                    "measured_on": {"type": "string"},
                    "condition": {"type": "string"}},
                "required": ["name", "value"],
                "additionalProperties": False}},
            "patient_context": {"type": "array", "items": {
                "type": "object", "properties": {
                    "category": {"type": "string", "enum": sorted(CATEGORIES)},
                    "text": {"type": "string", "minLength": 1},
                    "evidence": {"type": "string", "minLength": 1},
                    "subject": {"type": "string", "enum": ["patient", "family", "other"]},
                    "details": {"type": "array", "items": {
                        "type": "object", "properties": {
                            "key": {"type": "string", "enum": sorted(DETAIL_KEYS)},
                            "refers_to": {"type": "string", "pattern": "^S[1-9][0-9]*$"},
                            "value": {"type": "string", "minLength": 1},
                            "evidence": {"type": "string", "minLength": 1}},
                        "required": ["key", "value", "evidence"],
                        "additionalProperties": False}}},
                "required": ["category", "text", "evidence", "subject"],
                "additionalProperties": False}},
            "summary": {"type": "string", "minLength": 1},
            "points": {"type": "array", "items": {"type": "string"}},
            "urgency": {"type": "string",
                        "enum": ["high", "routine", "unclear"]},
            "urgency_evidence": {"type": "array",
                                 "items": {"type": "string"}},
            # reply-to-earlier-request classification; only meaningful
            # with thread context (llm_extract drops it otherwise)
            "reply": {"type": "object", "properties": {
                "kind": {"type": "string",
                         "enum": ["ack", "intent", "progress", "answer",
                                  "done", "cancel"]},
                "evidence": {"type": "string"}},
                "required": ["kind"],
                "additionalProperties": False}},
        # An omitted section concealed negative findings in constrained output.
        "required": ["symptoms"],
        "additionalProperties": False}}


# Batch envelope: same per-message object plus the target index. The
# index is required — an item without "i" cannot be routed to its body
# and is dropped, never guessed.
def batch_schema(single):
    """Wrap the individual response shape with its required target index."""
    item = dict(single["schema"])
    item["properties"] = {**single["schema"]["properties"], "i": {"type": "integer"}}
    item["required"] = [*single["schema"]["required"], "i"]
    return {
        "name": "mcs_extract_batch",
        "schema": {"type": "object",
                   "properties": {"items": {"type": "array", "items": item}},
                   "required": ["items"],
                   "additionalProperties": False}}


SCHEMA_BATCH = batch_schema(SCHEMA)
BATCH_ITEM = SCHEMA_BATCH["schema"]["properties"]["items"]["items"]


def gen12_schema():
    """Build the next response contract without selecting it for runtime use."""
    # X9 selects this contract together with the generation and prompt switch.
    # Never mutate SCHEMA: it also describes saved responses from the current generation.
    schema = deepcopy(SCHEMA)
    props = schema["schema"]["properties"]
    context = props["patient_context"]["items"]["properties"]
    context["category"]["enum"] = sorted(patient_context.CATEGORIES)
    context["details"]["items"]["properties"]["key"]["enum"] = sorted(patient_context.DETAIL_KEYS)
    props[clinical_allergies.FIELD] = {"type": "array", "items": {
        "type": "object", "properties": {
            "allergen": {"type": ["string", "null"]},
            "kind": {"type": "string", "enum": list(clinical_allergies.KINDS)},
            "reaction": {"type": ["string", "null"]},
            "subject": {"type": "string", "enum": list(clinical_allergies.SUBJECTS)},
            "status": {"type": "string", "enum": list(clinical_allergies.STATUSES)},
            "negated": {"type": "boolean"},
            "certainty": {"type": "string", "enum": list(clinical_allergies.CERTAINTIES)},
            "evidence": {"type": ["string", "null"]}},
        "required": ["kind", "subject", "status", "negated", "certainty"],
        "additionalProperties": False}}
    return schema


RX_DOSE_ONE = (r"\s*[0-9０-９.．]+\s*(?:mg|μg|mcg|g|mL|ml|錠|%|単位|包|枚|本|カプセル|cap|Cap|CAP)"
                # 5mg錠, 5mg1錠: the strength's form or count, kept as written
                r"(?:\s*[0-9０-９]*\s*(?:錠|カプセル|包|枚|本|T|C)(?![A-Za-zＡ-Ｚａ-ｚ]))?")

RX_DOSE = f"(?:{RX_DOSE_ONE})?"

RX_PARTICLE = r"\s*(?:(?:を|は|も|の|が)\s*(?:[、，,]\s*)?)?\s*"


# Bounded repeats keep findall linear in the body length: the lazy name
# class shares digits with the dose, so unbounded runs backtrack cubically.
MEDICATION_TOKEN = re.compile(
    r"([ァ-ヶー一-龥][ァ-ヶー一-龥A-Za-z0-9０-９・ー\-]{0,30}?)"
    # (?<!\d): a dose never starts inside a longer number (7+ digits match nothing)
    r"(?<!\d)(\d{1,6}(?:\.\d{1,4})?)\s{0,8}(mg|μg|mcg|g|mL|単位|錠|cap|カプセル|包|枚|本)")
