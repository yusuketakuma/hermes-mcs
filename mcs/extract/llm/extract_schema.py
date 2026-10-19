"""JSON output shapes and shared medication syntax for extraction validation."""
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
BATCH_ITEM = dict(SCHEMA["schema"])
BATCH_ITEM["properties"] = dict(SCHEMA["schema"]["properties"])
BATCH_ITEM["properties"]["i"] = {"type": "integer"}
BATCH_ITEM["required"] = [*SCHEMA["schema"]["required"], "i"]

SCHEMA_BATCH = {
    "name": "mcs_extract_batch",
    "schema": {"type": "object",
               "properties": {"items": {"type": "array",
                                        "items": BATCH_ITEM}},
               "required": ["items"],
               "additionalProperties": False}}

RX_DOSE_ONE = (r"\s*[0-9０-９.．]+\s*(?:mg|μg|mcg|g|mL|ml|錠|%|単位|包|枚|本|カプセル|cap|Cap|CAP)"
                # 5mg錠, 5mg1錠: the strength's form or count, kept as written
                r"(?:\s*[0-9０-９]*\s*(?:錠|カプセル|包|枚|本|T|C)(?![A-Za-zＡ-Ｚａ-ｚ]))?")

RX_DOSE = f"(?:{RX_DOSE_ONE})?"

RX_PARTICLE = r"\s*(?:(?:を|は|も|の|が)\s*(?:[、，,]\s*)?)?\s*"
