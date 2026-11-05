"""Validate durable Jev usage receipts and patient-quality answer evidence."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

from mcs_util import payload_hash, reject_constant, unique_pairs, valid_hash

MAX_SQL_INTEGER = 2**63 - 1


@dataclass(frozen=True)
class JevReservation:
    """The accounting day and artifact of one committed Jev reservation."""
    artifact_id: int
    jst_day: int


_QUALITY_ID_KEYS = ("input_key", "candidate_key", "failure_key", "audit_key")


_QUALITY_PURPOSES = {
    "summary": ("patient_summary_quality", "patient_context_summary_quality_v1"),
    "source": ("patient_source_quality", "patient_context_source_quality_v1"),
}


_QUALITY_QIDS = frozenset(f"q{i}" for i in range(12))


def _quality_purpose(purpose):
    if not isinstance(purpose, str) or purpose not in _QUALITY_PURPOSES:
        raise ValueError("patient_summary_purpose_invalid")
    return _QUALITY_PURPOSES[purpose]


def _quality_json(raw):
    if not isinstance(raw, str) or len(raw) > 32768:
        return None
    try:
        def finite_float(text):
            value = float(text)
            if not math.isfinite(value):
                raise ValueError("invalid_number")
            return value
        value = json.loads(raw, object_pairs_hook=unique_pairs, parse_constant=reject_constant,
                           parse_float=finite_float)
    except (TypeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _quality_identity(identity, *, purpose="summary"):
    if not isinstance(identity, dict) or any(not valid_hash(identity.get(key)) for key in _QUALITY_ID_KEYS):
        raise ValueError("patient_summary_identity_invalid")
    keys = {key: identity[key] for key in _QUALITY_ID_KEYS}
    if purpose == "summary":
        return keys
    _quality_purpose(purpose)
    source = identity.get("source_input_key")
    assessment = identity.get("assessment")
    questions, passing = identity.get("question_keys"), identity.get("passing_choices")
    if (not valid_hash(source) or not isinstance(assessment, dict) or set(assessment) != {"code", "policy"}
            or any(not valid_hash(value) for value in assessment.values())
            or not isinstance(questions, dict) or not questions
            or any(not valid_hash(fid) or not valid_hash(key) for fid, key in questions.items())
            or len(set(questions.values())) != len(questions)
            or not isinstance(passing, dict) or set(passing) != set(questions)
            or any(choice not in ("supports", "complete") for choice in passing.values())):
        raise ValueError("patient_source_identity_invalid")
    return {**keys, "source_input_key": source, "assessment": dict(assessment),
            "question_keys": dict(questions), "passing_choices": dict(passing)}


def _quality_request(identity, body, *, purpose="summary"):
    keys = _quality_identity(identity, purpose=purpose)
    try:
        questions = body["questions"]
        facts = body["state"]["target"]["facts"]
        if (not isinstance(questions, dict) or not 1 <= len(questions) <= 12
                or not isinstance(facts, list) or not 1 <= len(facts) <= 12):
            raise ValueError
        indexed = {}
        for fact in facts:
            qid, fid = fact["id"], fact["fact"]["id"]
            if qid not in _QUALITY_QIDS or not valid_hash(fid) or qid in indexed:
                raise ValueError
            indexed[qid] = fid
        mapping = {qid: indexed[qid] for qid in questions}
        if len(set(mapping.values())) != len(mapping):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("patient_summary_request_invalid") from None
    if purpose == "source":
        from semantic_audit import patient_source_question_keys
        computed = patient_source_question_keys(body, source_input_key=keys["source_input_key"],
                                                assessment=keys["assessment"])
        passing = "complete" if body["state"]["target"]["id"] == "source_coverage" else "supports"
        if (set(computed) != set(mapping.values()) or any(
                keys["question_keys"].get(fid) != key or keys["passing_choices"].get(fid) != passing
                for fid, key in computed.items())):
            raise ValueError("patient_source_request_invalid")
        return {"v": 2, **{key: keys[key] for key in _QUALITY_ID_KEYS},
                "source_input_key": keys["source_input_key"], "assessment_hash": payload_hash(keys["assessment"]),
                "questions": {qid: {"fact_id": fid, "question_key": computed[fid]} for qid, fid in mapping.items()}}
    return {"v": 1, **keys, "questions": mapping}


def _quality_bound(meta):
    if (not isinstance(meta, dict) or meta.get("operation") not in {v[0] for v in _QUALITY_PURPOSES.values()}
            or type(meta.get("jev_requests")) is not int or meta["jev_requests"] != 1
            or meta.get("reserved") is not True or not valid_hash(meta.get("request_fp"))
            or not isinstance(meta.get("reservation_id"), str) or len(meta["reservation_id"]) != 32
            or any(c not in "0123456789abcdef" for c in meta["reservation_id"])
            or type(meta.get("timeout_seconds")) not in (int, float)
            or not 0 < meta["timeout_seconds"] <= 1.7976931348623157e308
            or not math.isfinite(meta["timeout_seconds"])):
        return None
    request = meta.get("quality_request")
    if not isinstance(request, dict) or type(request.get("v")) is not int or request["v"] not in (1, 2):
        return None
    extra = {"source_input_key", "assessment_hash"} if request["v"] == 2 else set()
    if (set(request) != {"v", *_QUALITY_ID_KEYS, "questions", *extra}
            or extra and (meta["operation"] != "patient_source_quality"
                          or any(not valid_hash(request.get(key)) for key in extra))):
        return None
    try:
        _quality_identity(request)
        questions = request["questions"]
        if not isinstance(questions, dict) or not 1 <= len(questions) <= 12 or any(q not in _QUALITY_QIDS for q in questions):
            return None
        if request["v"] == 2:
            if any(not isinstance(value, dict) or set(value) != {"fact_id", "question_key"}
                   or any(not valid_hash(item) for item in value.values()) for value in questions.values()):
                return None
            if len({value["question_key"] for value in questions.values()}) != len(questions):
                return None
            ids = [value["fact_id"] for value in questions.values()]
        else:
            ids = list(questions.values())
        if any(not valid_hash(fid) for fid in ids) or len(set(ids)) != len(ids):
            return None
    except (TypeError, ValueError):
        return None
    return request


_NOT_SENT_REASONS = frozenset({"semantic_until_expired", "semantic_until_invalid", "semantic_config_unreadable"})


def _quality_result_parts(meta, request):
    result = meta.get("quality_result")
    if isinstance(result, dict) and set(result) == {"answers", "invalid", "not_sent"}:
        # nothing was asked: no question is answered or counted invalid
        if result["answers"] == {} and result["invalid"] == [] and result["not_sent"] in _NOT_SENT_REASONS:
            return {}, []
        return None
    if not isinstance(result, dict) or set(result) != {"answers", "invalid"}:
        return None
    answers, invalid = result["answers"], result["invalid"]
    if (not isinstance(answers, dict) or not isinstance(invalid, list)
            or any(not valid_hash(fid) for fid in invalid) or invalid != sorted(set(invalid))
            or any(not valid_hash(fid) for fid in answers)):
        return None
    if request["v"] == 2:
        ids = {value["fact_id"] for value in request["questions"].values()}
        if any(not isinstance(ref, dict) or set(ref) != {"answer_artifact_id", "answer_hash"}
               or type(ref["answer_artifact_id"]) is not int or not 0 < ref["answer_artifact_id"] <= MAX_SQL_INTEGER
               or not valid_hash(ref["answer_hash"]) for ref in answers.values()):
            return None
    else:
        ids = set(request["questions"].values())
        if any(not valid_hash(value) for value in answers.values()):
            return None
    if set(answers) & set(invalid) or set(answers) | set(invalid) != ids:
        return None
    return answers, invalid


def _quality_answer_evidence(db, project_id, request, answers, *, purpose="summary"):
    from inference_version import CURRENT_VERSION
    _, answer_kind = _quality_purpose(purpose)
    choices = {"supports", "contradicts", "not_supported", "ambiguous"}
    choices |= {"omission_ok", "important_missing"} if purpose == "summary" else {"complete", "missing"}
    found = {}
    marks = ",".join("?" for _ in answers) or "NULL"
    # Include malformed or duplicate-key metadata even if its SQL keys do
    # not match. Otherwise an ambiguous second key could hide a receipt.
    query = ("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND CASE "
             "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
             "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
             "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
             "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND "
             "abs(value)>1.7976931348623157e308) THEN 1 "
             "ELSE json_extract(meta,'$.input_key')=? AND json_extract(meta,'$.audit_key')=? "
             f"AND json_extract(meta,'$.fact_id') IN ({marks}) END")
    for row in db.execute(query, (answer_kind, project_id, request["input_key"],
                                  request["audit_key"], *answers)):
        answer_meta = _quality_json(row[1])
        if answer_meta is None:
            return False
        fid = answer_meta.get("fact_id")
        if (answer_meta.get("input_key") != request["input_key"]
                or answer_meta.get("audit_key") != request["audit_key"]
                or answer_meta.get("inference_engine_version") != CURRENT_VERSION or fid not in answers):
            continue
        value = _quality_json(row[0])
        if fid in found or value is None or set(value) != {"choice", "confidence"}:
            return False
        confidence = value.get("confidence")
        if (not isinstance(value.get("choice"), str)
                or value.get("choice") not in choices
                or type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1
                or (answers[fid] is not None and payload_hash(value) != answers[fid])):
            return False
        found[fid] = value
    return set(found) == set(answers)


def _source_quality_rows(db, pid, source_input_key, question_keys):
    """Narrow normal rows by logical question, while retaining malformed scope evidence."""
    def hash_sql(value, size=64):
        return f"(typeof({value})='text' AND length({value})={size} AND {value} NOT GLOB '*[^0-9a-f]*')"

    def get(path):
        return f"json_extract(meta,'$.{path}')"
    version = get("quality_request.v")
    source_operation = get("operation") + "='patient_source_quality'"
    question = "CASE q.type WHEN 'object' THEN json_extract(q.value,'$.question_key') END"
    field_checks = [hash_sql(get("quality_request." + key)) for key in _QUALITY_ID_KEYS]
    field_checks += [hash_sql(get("request_fp")), hash_sql(get("reservation_id"), 32)]
    source_fields = " AND ".join(hash_sql(get("quality_request." + key))
                                 for key in ("source_input_key", "assessment_hash"))
    legacy_question = hash_sql("q.value")
    source_question = ("CASE q.type WHEN 'object' THEN (SELECT COUNT(*) FROM json_each(q.value))=2 AND "
        + hash_sql("json_extract(q.value,'$.fact_id')") + " AND " + hash_sql(question) + " ELSE 0 END")
    qids = ",".join("'q" + str(i) + "'" for i in range(12))
    marks = ",".join("?" for _ in question_keys) or "NULL"
    query = ("SELECT artifact_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
        "FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND CASE "
        "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "WHEN json_extract(meta,'$.operation') NOT IN ('patient_source_quality','patient_summary_quality') "
        "OR json_extract(meta,'$.operation') IS NULL THEN "
        "json_type(meta,'$.quality_request') IS NOT NULL OR json_type(meta,'$.quality_result') IS NOT NULL "
        "WHEN NOT COALESCE((" + " AND ".join(field_checks) + ") AND "
        "json_type(meta,'$.jev_requests')='integer' AND json_extract(meta,'$.jev_requests')=1 AND "
        "json_type(meta,'$.reserved')='true' AND "
        "json_type(meta,'$.timeout_seconds') IN ('integer','real') AND json_extract(meta,'$.timeout_seconds')>0 AND "
        "json_type(meta,'$.quality_request')='object' AND json_type(meta,'$.quality_request.v')='integer' AND "
        f"(({source_operation} AND {version} IN (1,2)) OR (NOT ({source_operation}) AND {version}=1)) AND "
        f"(SELECT COUNT(*) FROM json_each(meta,'$.quality_request'))=CASE {version} WHEN 1 THEN 6 ELSE 8 END AND "
        "json_type(meta,'$.quality_request.questions')='object' AND "
        "(SELECT COUNT(*) FROM json_each(meta,'$.quality_request.questions')) BETWEEN 1 AND 12 AND "
        f"({version}=1 OR ({source_fields})) AND NOT EXISTS "
        "(SELECT 1 FROM json_each(meta,'$.quality_request.questions') q WHERE "
        f"q.key NOT IN ({qids}) OR NOT COALESCE(CASE {version} WHEN 1 THEN {legacy_question} "
        f"ELSE {source_question} END,0)),0) THEN 1 "
        f"WHEN {source_operation} AND {version}=1 THEN 1 "
        f"ELSE {source_operation} AND json_extract(meta,'$.quality_request.source_input_key')=? "
        "AND EXISTS (SELECT 1 FROM json_each(meta,'$.quality_request.questions') q "
        f"WHERE {question} IN ({marks})) END")
    return db.execute(query, (pid, source_input_key, *question_keys))


def _source_usage_artifact(db, project_id, meta, artifact_id=None):
    """Read the actual reservation; result sealing may add only its quality_result."""
    if artifact_id is None:
        rows = db.execute("SELECT artifact_id,meta FROM artifacts WHERE kind='semantic_usage' "
            "AND project_id=? AND CASE WHEN json_valid(meta) THEN "
            "json_extract(meta,'$.reservation_id') END=?", (project_id, meta.get("reservation_id"))).fetchmany(2)
        if len(rows) != 1:
            return None
        artifact_id, raw = rows[0]
    else:
        row = db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND artifact_id=?",
                         (project_id, artifact_id)).fetchone()
        if row is None:
            return None
        raw = row[0]
    stored = _quality_json(raw)
    if (stored is None or {k: v for k, v in stored.items() if k != "quality_result"}
            != {k: v for k, v in meta.items() if k != "quality_result"}):
        return None
    return artifact_id


def _source_answer_record(db, project_id, usage_id, meta, request, fid, ref):
    """Validate one original answer by primary key and its exact original reservation."""
    from inference_version import CURRENT_VERSION
    if (not isinstance(ref, dict) or set(ref) != {"answer_artifact_id", "answer_hash"}
            or type(ref["answer_artifact_id"]) is not int or not 0 < ref["answer_artifact_id"] <= MAX_SQL_INTEGER
            or not valid_hash(ref["answer_hash"])):
        return None
    row = db.execute("SELECT content,meta FROM artifacts WHERE kind='patient_context_source_quality_v1' "
                     "AND project_id=? AND artifact_id=?", (project_id, ref["answer_artifact_id"])).fetchone()
    answer_meta = _quality_json(row[1]) if row else None
    value = _quality_json(row[0]) if row else None
    qid = next((qid for qid, question in request["questions"].items() if question["fact_id"] == fid), None)
    if (qid is None or answer_meta is None or value is None or set(value) != {"choice", "confidence"}
            or answer_meta.get("inference_engine_version") != CURRENT_VERSION
            or any(answer_meta.get(key) != request[key] for key in ("input_key", "audit_key", "source_input_key"))
            or answer_meta.get("fact_id") != fid
            or answer_meta.get("question_key") != request["questions"][qid]["question_key"]
            or type(answer_meta.get("reservation_artifact_id")) is not int
            or answer_meta["reservation_artifact_id"] != usage_id
            or answer_meta.get("reservation_id") != meta["reservation_id"] or answer_meta.get("qid") != qid
            or value.get("choice") not in ("supports", "contradicts", "not_supported", "ambiguous", "complete", "missing")
            or type(value.get("confidence")) not in (int, float) or not 0 <= value["confidence"] <= 1
            or not math.isfinite(value["confidence"]) or payload_hash(value) != ref["answer_hash"]):
        return None
    return value


def _source_new_answer_refs(db, project_id, usage_id, meta, request, answers):
    """Find newly received answer records without borrowing another reservation's receipt."""
    query = ("SELECT artifact_id,content,meta FROM artifacts WHERE kind='patient_context_source_quality_v1' "
        "AND project_id=? AND CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "ELSE json_extract(meta,'$.reservation_artifact_id')=? OR json_extract(meta,'$.reservation_id')=? END")
    found = {}
    for artifact_id, content, raw in db.execute(query, (project_id, usage_id, meta["reservation_id"])):
        answer_meta, value = _quality_json(raw), _quality_json(content)
        fid = answer_meta.get("fact_id") if answer_meta else None
        if fid not in answers or fid in found or value != answers[fid]:
            raise ValueError("patient_source_result_invalid")
        ref = {"answer_artifact_id": artifact_id, "answer_hash": payload_hash(value)}
        if _source_answer_record(db, project_id, usage_id, meta, request, fid, ref) is None:
            raise ValueError("patient_source_result_invalid")
        found[fid] = ref
    if set(found) != set(answers):
        raise ValueError("patient_source_result_invalid")
    return found


def patient_source_answer_binding(db, pid, identity, reservation, fact_ids):
    """Bind a new response only to its live, unsealed source reservation in the caller's write transaction."""
    if not db.in_transaction or not isinstance(reservation, JevReservation):
        raise RuntimeError("patient_source_transaction_required")
    keys = _quality_identity(identity, purpose="source")
    if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
            or len(fact_ids) > 12 or any(not valid_hash(fid) for fid in fact_ids)):
        raise ValueError("patient_source_result_invalid")
    row = db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=? AND artifact_id=?",
                     (pid, reservation.artifact_id)).fetchone()
    meta = _quality_json(row[0]) if row else None
    request = _quality_bound(meta)
    if (request is None or request["v"] != 2 or meta["operation"] != "patient_source_quality"
            or "quality_result" in meta or any(request[key] != keys[key] for key in _QUALITY_ID_KEYS)
            or request["source_input_key"] != keys["source_input_key"]
            or request["assessment_hash"] != payload_hash(keys["assessment"])):
        raise ValueError("patient_source_request_invalid")
    questions = {item["fact_id"]: (qid, item["question_key"]) for qid, item in request["questions"].items()}
    if any(fid not in questions or keys["question_keys"].get(fid) != questions[fid][1] for fid in fact_ids):
        raise ValueError("patient_source_request_invalid")
    return {fid: {"source_input_key": keys["source_input_key"], "question_key": questions[fid][1],
                  "reservation_artifact_id": reservation.artifact_id, "reservation_id": meta["reservation_id"],
                  "qid": questions[fid][0]} for fid in fact_ids}


def patient_source_question_evidence(db, pid, identity, fact_ids):
    """Reuse original source answers, unknowns and invalid counts across candidate audits."""
    keys = _quality_identity(identity, purpose="source")
    if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
            or len(fact_ids) > 12
            or any(not valid_hash(fid) or fid not in keys["question_keys"] for fid in fact_ids)):
        raise ValueError("patient_source_question_invalid")
    requested = set(fact_ids)
    result = {"answers": {}, "references": {}, "problems": {}}
    if not requested:
        return result
    invalid_counts = dict.fromkeys(requested, 0)
    qkeys = [keys["question_keys"][fid] for fid in sorted(requested)]
    for row in _source_quality_rows(db, pid, keys["source_input_key"], qkeys):
        usage_id, raw = row
        meta = _quality_json(raw)
        request = _quality_bound(meta)
        if request is None or meta.get("operation") != "patient_source_quality" or request["v"] != 2:
            result["problems"].update(dict.fromkeys(requested, "unknown"))
            continue
        matching = {item["fact_id"] for item in request["questions"].values()
                    if item["fact_id"] in requested and item["question_key"] == keys["question_keys"][item["fact_id"]]}
        if request["source_input_key"] != keys["source_input_key"]:
            continue
        if request['assessment_hash'] != payload_hash(keys['assessment']):
            result['problems'].update(dict.fromkeys(matching, 'unknown'))
            continue
        parts = _quality_result_parts(meta, request)
        if parts is None:
            result["problems"].update(dict.fromkeys(matching, "unknown"))
            continue
        if "not_sent" in meta["quality_result"]:
            continue
        answers, invalid = parts
        checked = {fid: _source_answer_record(db, pid, usage_id, meta, request, fid, ref)
                   for fid, ref in answers.items()}
        if any(value is None for value in checked.values()):
            result["problems"].update(dict.fromkeys(matching, "unknown"))
            continue
        for fid in matching:
            if fid in invalid:
                invalid_counts[fid] += 1
                continue
            answer = checked[fid]
            choices = ({"supports", "contradicts", "not_supported", "ambiguous"}
                       if keys["passing_choices"][fid] == "supports" else {"complete", "missing", "ambiguous"})
            if fid in result["answers"] or answer["choice"] not in choices:
                result["problems"][fid] = "unknown"
                continue
            result["answers"][fid] = answer
            result["references"][fid] = {"usage_artifact_id": usage_id, "reservation_id": meta["reservation_id"],
                "answer_artifact_id": answers[fid]["answer_artifact_id"], "answer_hash": answers[fid]["answer_hash"]}
    marks = ",".join("?" for _ in qkeys)
    answer_query = ("SELECT artifact_id,CASE WHEN typeof(meta)='text' AND length(meta)<=32768 THEN meta END "
        "FROM artifacts WHERE kind='patient_context_source_quality_v1' AND project_id=? "
        "AND CASE WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL "
        "GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "WHEN json_type(meta)!='object' OR json_type(meta,'$.source_input_key') IS NOT 'text' "
        "OR length(json_extract(meta,'$.source_input_key'))!=64 "
        "OR json_extract(meta,'$.source_input_key') GLOB '*[^0-9a-f]*' "
        "OR json_type(meta,'$.question_key') IS NOT 'text' OR length(json_extract(meta,'$.question_key'))!=64 "
        "OR json_extract(meta,'$.question_key') GLOB '*[^0-9a-f]*' THEN 1 "
        "ELSE json_extract(meta,'$.source_input_key')=? "
        f"AND json_extract(meta,'$.question_key') IN ({marks}) END")
    qkey_facts = {keys["question_keys"][fid]: fid for fid in requested}
    for artifact_id, raw in db.execute(answer_query, (pid, keys["source_input_key"], *qkeys)):
        meta = _quality_json(raw)
        if meta is None or not valid_hash(meta.get("source_input_key")) or not valid_hash(meta.get("question_key")):
            result["problems"].update(dict.fromkeys(requested, "unknown"))
            continue
        fid = qkey_facts.get(meta["question_key"])
        ref = result["references"].get(fid)
        if fid is not None and (meta.get("fact_id") != fid or not ref or ref["answer_artifact_id"] != artifact_id):
            # An orphan or copied answer cannot turn a missing original reservation into an unasked question.
            result["problems"][fid] = "unknown"
    for fid, count in invalid_counts.items():
        if count >= 2 and fid not in result["answers"]:
            result["problems"].setdefault(fid, "invalid_exhausted")
    for fid in result["problems"]:
        result["answers"].pop(fid, None)
        result["references"].pop(fid, None)
    return result


def _quality_result(db, project_id, meta, request):
    parts = _quality_result_parts(meta, request)
    if request["v"] == 2:
        usage_id = _source_usage_artifact(db, project_id, meta)
        return bool(parts is not None and usage_id is not None and all(
            _source_answer_record(db, project_id, usage_id, meta, request, fid, ref) is not None
            for fid, ref in parts[0].items()))
    purpose = next((key for key, (operation, _) in _QUALITY_PURPOSES.items()
                    if meta.get("operation") == operation), None)
    return bool(purpose is not None and parts is not None
                and _quality_answer_evidence(db, project_id, request, parts[0], purpose=purpose))


def patient_summary_usage_resolved(db, project_id, meta):
    """Check a bound request and its persisted answer receipts without changing the DB."""
    request = _quality_bound(meta)
    return bool(type(project_id) is int and project_id > 0 and request
                and _quality_result(db, project_id, meta, request))


def patient_summary_usage_prunable(db, project_id, raw):
    """Keep unknown, malformed or unresolved quality and invalid-attempt evidence."""
    meta = _quality_json(raw)
    if meta is None:
        return False
    purpose = next((key for key, (operation, _) in _QUALITY_PURPOSES.items()
                    if meta.get("operation") == operation), None)
    if purpose is None:
        return not ("quality_request" in meta or "quality_result" in meta)
    if purpose == "source":
        # Source questions outlive a candidate audit; their original evidence is never a TTL cache.
        return False
    request = _quality_bound(meta)
    if not request or not patient_summary_usage_resolved(db, project_id, meta):
        return False
    _, invalid = _quality_result_parts(meta, request)
    return _quality_answer_evidence(db, project_id, request, dict.fromkeys(invalid), purpose=purpose)


def patient_summary_request_problem(db, pid, identity, fact_ids, *, purpose="summary"):
    """Return a durable unknown or exhausted-invalid reason for logical questions."""
    if purpose == "source":
        keys = _quality_identity(identity, purpose="source")
        if (type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset))
                or any(not valid_hash(fid) or fid not in keys["question_keys"] for fid in fact_ids)):
            raise ValueError("patient_source_question_invalid")
        ordered, problems = sorted(set(fact_ids)), set()
        for start in range(0, len(ordered), 12):
            ids = ordered[start:start + 12]
            page = {**keys, "question_keys": {fid: keys["question_keys"][fid] for fid in ids},
                    "passing_choices": {fid: keys["passing_choices"][fid] for fid in ids}}
            problems.update(patient_source_question_evidence(db, pid, page, ids)["problems"].values())
        if "unknown" in problems:
            return "patient_summary_request_unknown"
        return "patient_summary_invalid_exhausted" if problems else None
    keys = _quality_identity(identity)
    operation, _ = _quality_purpose(purpose)
    if type(pid) is not int or pid <= 0 or not isinstance(fact_ids, (list, tuple, set, frozenset)) or any(not valid_hash(fid) for fid in fact_ids):
        raise ValueError("patient_summary_request_invalid")
    requested = set(fact_ids)
    invalid_counts = dict.fromkeys(requested, 0)
    for row in db.execute("SELECT meta FROM artifacts WHERE kind='semantic_usage' AND project_id=?", (pid,)):
        meta = _quality_json(row[0])
        if meta is None:
            return "patient_summary_request_unknown"
        if meta.get("operation") not in {v[0] for v in _QUALITY_PURPOSES.values()}:
            if "quality_request" in meta or "quality_result" in meta:
                return "patient_summary_request_unknown"
            continue
        request = _quality_bound(meta)
        if request is None:
            return "patient_summary_request_unknown"
        resolved = "quality_result" in meta and patient_summary_usage_resolved(db, pid, meta)
        if "quality_result" in meta and not resolved:
            return "patient_summary_request_unknown"
        if meta["operation"] != operation:
            continue
        if (request["failure_key"] == keys["failure_key"] and requested.intersection(request["questions"].values())
                and not resolved):
            return "patient_summary_request_unknown"
        if request["failure_key"] == keys["failure_key"] and "quality_result" in meta:
            for fid in _quality_result_parts(meta, request)[1]:
                if fid in invalid_counts:
                    invalid_counts[fid] += 1
    for fid, count in invalid_counts.items():
        if count >= 2 and not _quality_answer_evidence(db, pid, keys, {fid: None}, purpose=purpose):
            return "patient_summary_invalid_exhausted"
    return None


def patient_summary_resolve_request_tx(ledger, reservation, identity, answers, invalid_ids, *, project_id,
                                       purpose="summary", not_sent=None):
    """Seal one request only with its already-persisted answer evidence, or as
    ``not_sent`` (no answers) when its POST provably never started."""
    if not ledger.db.in_transaction or not isinstance(reservation, JevReservation):
        raise RuntimeError("patient_summary_transaction_required")
    keys = _quality_identity(identity, purpose=purpose)
    operation, _ = _quality_purpose(purpose)
    if (not isinstance(answers, dict) or not isinstance(invalid_ids, set)
            or not_sent is not None and (not_sent not in _NOT_SENT_REASONS or answers or invalid_ids)):
        raise ValueError("patient_summary_result_invalid")
    row = ledger.db.execute("SELECT project_id,meta FROM artifacts WHERE kind='semantic_usage' AND artifact_id=?",
                            (reservation.artifact_id,)).fetchone()
    meta = _quality_json(row[1]) if row else None
    request = _quality_bound(meta)
    if (type(project_id) is not int or project_id <= 0 or not row or row[0] != project_id
            or not request or any(request[k] != keys[k] for k in _QUALITY_ID_KEYS)
            or meta.get("operation") != operation):
        raise ValueError("patient_summary_request_invalid")
    try:
        if purpose == "source":
            if (request["v"] != 2 or request["source_input_key"] != keys["source_input_key"]
                    or request["assessment_hash"] != payload_hash(keys["assessment"])
                    or any(keys["question_keys"].get(value["fact_id"]) != value["question_key"]
                           for value in request["questions"].values())):
                raise ValueError("patient_source_request_invalid")
            for fid, value in answers.items():
                choices = ({"supports", "contradicts", "not_supported", "ambiguous"}
                           if keys["passing_choices"].get(fid) == "supports" else {"complete", "missing", "ambiguous"})
                if not isinstance(value, dict) or value.get("choice") not in choices:
                    raise ValueError("patient_source_result_invalid")
            refs = _source_new_answer_refs(ledger.db, project_id, reservation.artifact_id, meta, request, answers)
            result = {"answers": refs, "invalid": sorted(invalid_ids)}
        else:
            result = {"answers": {fid: payload_hash(value) for fid, value in answers.items()}, "invalid": sorted(invalid_ids)}
    except (TypeError, ValueError, RecursionError):
        raise ValueError("patient_summary_result_invalid") from None
    if not_sent is not None:
        result["not_sent"] = not_sent
    candidate = {**meta, "quality_result": result}
    if not _quality_result(ledger.db, row[0], candidate, request):
        raise ValueError("patient_summary_result_invalid")
    if "quality_result" in meta:
        if meta["quality_result"] != result:
            raise ValueError("patient_summary_result_conflict")
        return 0
    updated = ledger.db.execute("UPDATE artifacts SET meta=? WHERE artifact_id=? AND kind='semantic_usage' AND meta=?",
                                (json.dumps(candidate, separators=(",", ":")), reservation.artifact_id, row[1])).rowcount
    saved = ledger.db.execute("SELECT meta FROM artifacts WHERE artifact_id=?", (reservation.artifact_id,)).fetchone()
    if updated != 1 or not saved or _quality_json(saved[0]) != candidate:
        raise ValueError("patient_summary_result_unverified")
    return updated


_SOURCE_PASS_POLICY_KEYS = ('model', 'match_threshold', 'nomatch_threshold',
                          'calibration_version', 'fact_source', 'candidate_fingerprint')


def patient_source_assessment_descriptor(assessment, policy):
    """Describe the interpretation policy without credentials, scope lists or spending settings."""
    return {'v': 1, 'assessment': dict(assessment),
            'policy': {key: policy.get(key) for key in _SOURCE_PASS_POLICY_KEYS}}


def patient_source_pass_content(db, pid, identity, descriptor):
    """Rebuild a source PASS from its bounded policy and original Jev receipts."""
    import hashlib
    import extract_llm
    import semantic_candidate
    import semantic_policy
    from semantic_jev import JEV_MODEL

    if not db.in_transaction:
        raise ValueError('patient_source_snapshot_required')
    keys = _quality_identity(identity, purpose='source')
    if (type(pid) is not int or pid <= 0 or not isinstance(descriptor, dict)
            or set(descriptor) != {'v', 'assessment', 'policy'} or type(descriptor['v']) is not int
            or descriptor['v'] != 1 or not isinstance(descriptor['policy'], dict)
            or set(descriptor['policy']) != set(_SOURCE_PASS_POLICY_KEYS)):
        raise ValueError('patient_source_descriptor_invalid')
    policy = descriptor['policy']
    lo, hi = policy['nomatch_threshold'], policy['match_threshold']
    if (policy['model'] != JEV_MODEL or type(lo) not in (int, float) or type(hi) not in (int, float)
            or not math.isfinite(lo) or not math.isfinite(hi) or not 0 <= lo < hi <= 1
            or policy['fact_source'] not in ('legacy', 'shadow', 'canonical')
            or policy['calibration_version'] is not None and (
                not isinstance(policy['calibration_version'], str)
                or not policy['calibration_version'].strip() or len(policy['calibration_version']) > 128)
            or policy['fact_source'] == 'legacy' and policy['candidate_fingerprint'] is not None
            or policy['fact_source'] != 'legacy' and not valid_hash(policy['candidate_fingerprint'])):
        raise ValueError('patient_source_descriptor_invalid')
    assessment = descriptor['assessment']
    code = payload_hash([extract_llm._LOADED_SOURCE_DIGESTS, semantic_candidate.loaded_source_contract()])
    if (assessment != keys['assessment'] or not isinstance(assessment, dict)
            or set(assessment) != {'code', 'policy'} or assessment['code'] != code
            or assessment['policy'] != semantic_policy.policy_fingerprint(policy)):
        raise ValueError('patient_source_descriptor_invalid')
    ids = identity.get('fact_ids')
    if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
            or set(ids) != set(keys['question_keys']) or type(identity.get('source_fact_count')) is not int
            or identity['source_fact_count'] != len(ids) - 1):
        raise ValueError('patient_source_identity_invalid')
    references = hashlib.sha256()
    for start in range(0, len(ids), 12):
        page = ids[start:start + 12]
        evidence = patient_source_question_evidence(db, pid, identity, page)
        if (evidence['problems'] or set(evidence['answers']) != set(page)
                or set(evidence['references']) != set(page)
                or any(evidence['answers'][fid]['choice'] != keys['passing_choices'][fid]
                       or evidence['answers'][fid]['confidence'] < hi for fid in page)):
            return None
        for fid in page:
            references.update(bytes.fromhex(payload_hash([fid, evidence['references'][fid]])))
    return {'question_count': len(ids), 'source_fact_count': identity['source_fact_count'],
            'answer_refs_sig': references.hexdigest(), 'descriptor': descriptor}


def patient_source_pass_descriptor(db, pid, audit_key):
    """Read only a unique, strictly parsed source PASS descriptor by audit identity."""
    from inference_version import CURRENT_VERSION, current_meta_pred

    if not db.in_transaction or type(pid) is not int or pid <= 0 or not valid_hash(audit_key):
        raise ValueError('patient_source_descriptor_invalid')
    kinds = ('patient_context_source_quality_fail_v1', 'patient_context_source_quality_pass_v1')
    bad = db.execute("SELECT 1 FROM artifacts WHERE project_id=? AND kind IN (?,?) AND CASE "
        "WHEN typeof(meta)!='text' OR length(meta)>32768 OR NOT json_valid(meta) THEN 1 "
        "WHEN json_type(meta)!='object' THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE key IS NOT NULL GROUP BY parent,key HAVING COUNT(*)>1) THEN 1 "
        "WHEN EXISTS (SELECT 1 FROM json_tree(meta) WHERE type='real' AND abs(value)>1.7976931348623157e308) THEN 1 "
        "ELSE 0 END LIMIT 1", (pid, *kinds)).fetchone()
    if bad:
        return None
    rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
        + current_meta_pred('meta', target=CURRENT_VERSION)
        + " AND json_extract(meta,'$.audit_key')=? ORDER BY artifact_id LIMIT 2",
        (kinds[1], pid, audit_key)).fetchall()
    if len(rows) != 1:
        return None
    content, meta = _quality_json(rows[0][0]), _quality_json(rows[0][1])
    if content is None or meta is None or meta.get('audit_key') != audit_key:
        return None
    return content.get('descriptor')


def patient_source_pass_verified(db, pid, identity, descriptor):
    """Verify recorded policy and original receipts, not current config or publication authority."""
    from inference_version import CURRENT_VERSION, current_meta_pred

    if patient_source_pass_descriptor(db, pid, identity['audit_key']) != descriptor:
        return False
    expected = patient_source_pass_content(db, pid, identity, descriptor)
    if expected is None:
        return False
    for kind, key, value in (('patient_context_source_quality_fail_v1', 'failure_key', identity['failure_key']),
                             ('patient_context_source_quality_pass_v1', 'audit_key', identity['audit_key'])):
        rows = db.execute("SELECT content,meta FROM artifacts WHERE kind=? AND project_id=? AND "
            + current_meta_pred('meta', target=CURRENT_VERSION)
            + " AND json_extract(meta,?)=? ORDER BY artifact_id LIMIT 2",
            (kind, pid, '$.' + key, value)).fetchall()
        if kind.endswith('_fail_v1'):
            if rows:
                return False
        else:
            if len(rows) != 1 or _quality_json(rows[0][0]) != expected:
                return False
            meta = _quality_json(rows[0][1])
            if (not isinstance(meta, dict)
                    or any(meta.get(key) != identity[key] for key in ('input_key', 'candidate_key', 'audit_key'))):
                return False
    return True
