"""Collect source-bound measurements and conservative observed-history coverage."""
import hashlib
import itertools
import json
import math
import re
import sqlite3

import clinical_values
import extract_llm
import inference_dependency_loss
import initial_import_state
from measurement_timeline import ambiguous_patient_context
from clinical_scope import FAMILY_PERSON, OTHER_PERSON, source_assertion_span, source_axes
from mcs_queries import current_extract_pred, current_fact_pred, decode_current_extractions, item_unverified
from mcs_util import locate_quote_span, loads_dict

_MAX_MESSAGES = 200
_MAX_RECORDS = 400
_MAX_PER_SOURCE = 256
_MAX_BODY_BYTES = 20000
_MAX_ARTIFACT_BYTES = 1000000
_MAX_INPUT_BYTES = 32 * 1024 * 1024
_MAX_FIELD_BYTES = 4096
_MESSAGE_FIELDS = ("message_id", "project_id", "body_text", "body_state", "content_hash", "posted_at",
                   "posted_at_ts", "parent_id", "sender_id", "sender_name", "sender_type", "profession",
                   "organization")
_ARTIFACT_KINDS = ("extract_v1", "extract_llm", "canonical_projection", "semantic_facts_v4")


def _bounded_text(value, limit):
    """Return UTF-8 size without encoding a value already over the limit."""
    if value is None:
        return 0
    if type(value) is not str or len(value) > limit or "\0" in value:
        return None
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    return size if size <= limit else None


_UNCERTAIN = re.compile(r"(?:では|じゃ|で)?(?:ない|ありません)|認めず|未確認|不明|未確定|未定|疑い|かもしれ|可能性|未実施|未測定|"
                        r"(?:採血|測定|検査)(?:は|が|を)?(?:していない|していません|されていない|せず|しない|なし|ありません)|[?？]")
_SENTENCE = re.compile(r"[。！？!?;；\n]")
_UNIT = re.compile(r"^(℃|mmHg|mg/dL|回/分|回／分|%|％)", re.I)
_CORRECTION = re.compile(
    r"誤り(?!では(?:ない|ありません|なく))|誤記|正しく(?:ない|ありません|なく)|"
    r"間違(?!いでは(?:ない|ありません|なく)|って(?:いない|いません))|"
    r"訂正(?!は(?:不要|ありません)|し(?:ない|ません)|する必要はない)|撤回|取り消|取消|"
    r"(?:数値|値|結果|記録|記載|投稿|件|内容|測定|検査|採血)"
    r"[^。！？!?;；\n]{0,60}(?:違(?:う|います|いました|った)|異な(?:る|ります|りました|った)|"
    r"誤っ(?:た|て(?:いました|いた|います|いる)))|"
    r"(?:実際(?:には|は)?|事実(?:は|としては)?)\s*(?:違(?:う|います|いました|った)|異な(?:る|ります|りました|った))")
_OTHER_MEASUREMENT = re.compile(rf"(?:{FAMILY_PERSON}|{OTHER_PERSON})の(?:数値|値|結果|記録|測定|検査|採血)")
_ADJACENT_UNCERTAIN = re.compile(
    r"未確認|不明|未確定|未定|疑い|かもしれ|可能性|確認(?:中|が必要|します|する)|"
    r"(?:です|ません|ない|でしょう)か(?:$|[、,。！？!?;；\n])|[?？]"
)
_FOLLOWUP_ITEM = r"(?:体温|脈拍|呼吸数|血圧|血糖|SpO2|酸素飽和度|酸素飽和|[A-Za-z]+)"
_FUTURE_FOLLOWUP = re.compile(
    rf"\s*(?:(?:明日|次回)(?:も|に)?\s*(?P<before>{_FOLLOWUP_ITEM})(?:を|の値を)"
    rf"|(?P<after>{_FOLLOWUP_ITEM})(?:を|の値を)(?:明日|次回)(?:も|に)?)"
    r"(?:確認します|確認する予定です)[。！!]?\s*", re.I
)
_STABLE_FOLLOWUP = re.compile(
    rf"\s*(?P<item>{_FOLLOWUP_ITEM})(?:は|が)(?:安定している|安定した|安定しております)"
    r"(?:ので|ため|ことから)経過を(?:確認|観察)(?:します|しています|する予定です)[。！!]?\s*", re.I
)
_INSTRUMENT_FAILURE = re.compile(
    r"(?P<device>体温計|血圧計|血糖測定器|パルスオキシメーター|"
    r"測定器|測定機器|測定機|センサー)"
    r"[^。！？!?;；\n]{0,40}?(?P<failure>故障|不良|エラー|壊れ|壊れて)"
)
_DEVICE_CLASS = {"体温計": "bt", "血圧計": "bp", "血糖測定器": "bs",
                 "パルスオキシメーター": "spo2"}


def _clearly_separate_followup(sentence, target_label):
    if target_label is None:
        return False
    for pattern in (_FUTURE_FOLLOWUP, _STABLE_FOLLOWUP):
        match = pattern.fullmatch(sentence)
        if match:
            item = next(value for value in match.groupdict().values() if value is not None)
            return target_label.fullmatch(item) is not None
    return False


def _family_fault_is_separate(sentence, match):
    device = match.group("device")
    before, separator, after = sentence.rpartition("、")
    if not separator:
        before, separator, after = sentence.rpartition(",")
    if separator and not re.search(
            r"本人|患者|利用者|私|自分|共有|共用|一緒|同じ|両方|二人|使用|使",
            before[:match.start()] + before[match.start() + len(device):]):
        owner_only = re.fullmatch(
            rf"\s*(?:{FAMILY_PERSON})(?:だけ|のみ)(?:が|は)(?:その|同じ)?"
            rf"{re.escape(device)}を(?:使いました|使用しました)[。！!]?\s*", after)
        separate_device = re.fullmatch(
            rf"\s*(?:本人|患者|利用者)(?:は|が)別の{re.escape(device)}(?:を(?:使いました|使用しました)"
            r"|で測定しました)[。！!]?\s*", after)
        if owner_only or separate_device:
            return True
    outside_device = sentence[:match.start()] + sentence[match.start() + len(device):]
    return not re.search(
        r"本人|患者|利用者|私|自分|共有|共用|一緒|同じ|両方|二人|それ|これ|使用|使|測定", outside_device)


def _instrument_failure(sentence, target_class):
    for match in _INSTRUMENT_FAILURE.finditer(sentence):
        device = match.group('device')
        device_class = _DEVICE_CLASS.get(device)
        if device_class is not None and target_class is not None and device_class != target_class:
            continue
        subject, _ = source_axes(sentence, match.start(), match.start() + len(device), default="unknown")
        if subject in ("family", "other") and _family_fault_is_separate(sentence, match):
            continue
        tail = sentence[match.end():]
        negated = re.match(r"(?:し(?:て)?|て)?(?:いなかった|いない|いません|なかった|なく|ではなかった|ではない|ではありません|はない|はありません|なし)", tail)
        remainder = tail[negated.end():] if negated else tail
        if negated and re.fullmatch(r"(?:です|でした)?[。！!]?\s*", remainder):
            continue
        return True
    return False


def _correction_candidate(body):
    if not isinstance(body, str):
        return False
    for sentence in _SENTENCE.split(body):
        matches = list(_CORRECTION.finditer(sentence))
        other = _OTHER_MEASUREMENT.search(sentence)
        if not matches:
            continue
        if (len(matches) == 1 and other and other.start() <= matches[0].start()
                and not re.search(r"本人|患者|利用者", sentence[:other.start()] + sentence[other.end():])
                and not re.search(r"[、,]", sentence[other.end():matches[0].end()])):
            continue
        return True
    return False


def _adjacent_measurement_retraction(body, span, *, target=None):
    """Hold a measurement when the next sentence questions that value."""
    if not isinstance(body, str) or not span:
        return False
    boundary = _SENTENCE.search(body, span[0], min(len(body), span[0] + _MAX_BODY_BYTES + 1))
    if boundary is None:
        return len(body) - span[0] > _MAX_BODY_BYTES
    start = boundary.end()
    while start < len(body) and body[start].isspace() and start - boundary.end() <= _MAX_BODY_BYTES:
        start += 1
    if start - boundary.end() > _MAX_BODY_BYTES:
        return True
    end = _SENTENCE.search(body, start, min(len(body), start + _MAX_BODY_BYTES + 1))
    if end is None and len(body) - start > _MAX_BODY_BYTES:
        return True
    sentence = body[start:end.end() if end else len(body)]
    if _bounded_text(sentence, _MAX_BODY_BYTES) is None:
        return True
    following = sentence
    labels = {"体温": "bt", "脈拍": "hr", "呼吸数": "rr", "血圧": "bp", "血糖": "bs",
              "spo2": "spo2", "酸素飽和度": "spo2", "酸素飽和": "spo2"}
    label = r"(?:体温|脈拍|呼吸数|血圧|血糖|SpO2|酸素飽和度|酸素飽和)"
    target_class = clinical_values._VITAL_CLASS.get(target)
    if target_class is None and isinstance(target, str):
        analyte = clinical_values.lab_analyte(target)
        if analyte is not None:
            target_class = "bs" if analyte == "blood_glucose" else "lab_other"
    if _instrument_failure(sentence, target_class):
        return True
    target_label = clinical_values._VITAL_LABELS.get(target_class)
    if target_label is None and isinstance(target, str) and 0 < len(target) <= 120:
        left = r"(?<![A-Za-z])" if target[0].isascii() and target[0].isalpha() else ""
        right = r"(?![A-Za-z])" if target[-1].isascii() and target[-1].isalpha() else ""
        target_label = re.compile(left + re.escape(target) + right, re.I)
    if _clearly_separate_followup(sentence, target_label):
        return False
    if target_label:
        for clause in re.split(r"[、,]", sentence):
            corrected = _correction_candidate(clause)
            candidate = corrected or _ADJACENT_UNCERTAIN.search(clause)
            if not candidate:
                continue
            matches = list(itertools.islice(target_label.finditer(clause), 65))
            if len(matches) > 64:
                return True
            if (re.match(r"\s*別の日の", clause) and len(matches) == 1
                    and not re.search(r"ではなく|ではない|ではありません|この(?:数値|値|結果)|同じ|"
                                      r"本人|患者|利用者", clause)):
                continue
            for match in matches:
                subject, _ = source_axes(clause, match.start(), match.end(), default="unknown")
                if subject not in ("family", "other"):
                    return True
    named_vital = re.match(rf"(?:その|先ほどの|上記の|直前の)?({label}(?:と{label})*)"
                           r"(?:の(?:測定値|数値|値|結果))?(?:は|が)", following, re.I)
    named = named_vital
    if named is None and isinstance(target, str) and 0 < len(target) <= 120:
        named = re.match(rf"(?:その|先ほどの|上記の|直前の)?({re.escape(target)})"
                         r"(?:の(?:測定値|数値|値|結果))?(?:は|が)", following, re.I)
    if named_vital and target_class is not None:
        classes = {labels[x.casefold()] for x in re.findall(label, named_vital.group(1), re.I)}
        trailing = following[named_vital.end():]
        if (target_class not in classes and target_label and not target_label.search(trailing)
                and not re.search(r"本人|患者|利用者|その(?:数値|値|結果)", trailing)):
            return False
    reference = named or re.match(r"(?:先ほど|先程|上記|直前|その)(?:の)?(?:検査値|測定値|数値|値|結果)"
                                  r"(?:は|が)", following)
    if reference is None:
        return False
    first, *rest = re.split(r"[、,]", sentence, maxsplit=1)
    other = re.match(rf"\s*(家族|他者|他の患者|別の日|{label})", rest[0], re.I) if rest else None
    other_name = other.group(1).casefold() if other else None
    distinct = (other_name in ("家族", "他者", "他の患者", "別の日")
                or other_name in labels and target_class is not None and labels[other_name] != target_class)
    if other_name == "別の日" and re.search(r"ではなく|ではない|ではありません|この(?:数値|値|結果)|同じ", rest[0]):
        distinct = False
    if (distinct and other_name != "別の日" and
            ((target_label and target_label.search(rest[0])) or re.search(r"本人|患者|利用者", rest[0]))):
        distinct = False
    if (distinct and re.fullmatch(r"(?:正しい(?:です|でした|ですが)|誤りでは(?:ない|ありません)(?:が)?|"
                                  r"間違って(?:いない|いません)(?:が)?)", first[reference.end():].strip())):
        sentence = first
    return bool(_correction_candidate(sentence) or _ADJACENT_UNCERTAIN.search(sentence))


def _source_record(message, ref, kind, key, value, unit, evidence, span, *, name=None,
                   measured_on=None, condition=None, status=None, subject=None, verified=False, index=None):
    body = message["body_text"] or ""
    if span is not None and 0 <= span[0] < span[1] <= len(body):
        quote = body[span[0]:span[1]]
    else:
        quote = None
        span = None
    return {"project_id": message["project_id"], "message_id": message["message_id"],
            "content_hash": message["content_hash"], "kind": kind, "key": key,
            "name": name, "value": value, "unit": unit, "measured_on": measured_on, "condition": condition,
            "posted_at": message["posted_at"], "subject": subject, "status": status,
            "negated": False if quote and not _UNCERTAIN.search(quote) else None,
            "verification": "quote_supported" if verified else "unverified",
            "source_body": body, "evidence": quote, "evidence_span": span,
            "source_ref": {"artifact_id": ref, "field": "labs" if kind == "lab" else "vitals",
                           **({"index": index} if kind == "lab" else {})}}


def _labs(message, doc, ref, patient_name, verified):
    raw = doc.get("labs", [])
    if not isinstance(raw, list):
        return [], 1
    out, invalid = [], 0
    body = message["body_text"] or ""
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"].strip():
            invalid += 1
            continue
        name, quote = item["name"], item.get("evidence")
        if (type(item.get("value")) not in (str, int, float) or
                any(item.get(k) is not None and not isinstance(item.get(k), str)
                    for k in ("unit", "evidence", "measured_on", "condition"))):
            invalid += 1
            continue
        span = locate_quote_span(body, quote) if isinstance(quote, str) else None
        actual = body[slice(*span)] if span else None
        candidate = clinical_values.lab_candidate(name, item.get("value", ""), item.get("unit"), actual,
                    unverified=item_unverified(item), subject=item.get("subject"), status=item.get("status"),
                    measured_on=item.get("measured_on"), condition=item.get("condition"), patient_name=patient_name)
        scope = clinical_values.patient_item_scope(item, body, patient_name=patient_name, surface=name)
        anchor = body.find(name, span[0], span[1]) if span else -1
        axes = source_axes(body, anchor, anchor + len(name)) if anchor >= 0 else ("unknown", "unknown")
        left = max((m.end() for m in _SENTENCE.finditer(body, 0, span[0])), default=0) if span else 0
        right = _SENTENCE.search(body, max(span[0], span[1] - 1)) if span else None
        context = body[left:right.end() if right else len(body)] if span else None
        condition = item.get("condition")
        assertion = source_assertion_span(body, name) if condition else None
        condition_ok = (condition is None or bool(condition.strip() and len(condition) <= 60
                        and assertion and body[slice(*assertion)].count(condition) == 1
                        and actual and condition in actual))
        subject = item.get("subject") or axes[0]
        period = item.get("status") or axes[1]
        planned = period in ("planned", "conditional")
        source_ok = bool(verified and not item_unverified(item)
                         and (candidate["confirmation"] == "quote_supported" or planned)
                         and subject == axes[0] == "patient" and period == axes[1]
                         and scope in ("patient", "past", "planned", "conditional")
                         and context and not _UNCERTAIN.search(context) and not ambiguous_patient_context(body, anchor)
                         and not _correction_candidate(context)
                         and not _adjacent_measurement_retraction(body, span, target=name) and condition_ok)
        observed = clinical_values.written_day(actual) if planned else None
        measured_on = (observed[1].isoformat() if observed and observed[0] == "ymd" else
                       candidate["measured_on"])
        key = clinical_values.lab_analyte(name) or clinical_values.fold_surface(name)
        out.append(_source_record(message, ref, "lab", key, item.get("value"), item.get("unit"), quote, span,
                   name=name, measured_on=measured_on, condition=condition if source_ok else None,
                   status=period, subject=subject,
                   verified=source_ok, index=index))
    return out, invalid


def _vitals(message, doc, ref, verified):
    raw = doc.get("vitals", {})
    if not isinstance(raw, dict):
        return [], 1
    body = message["body_text"] or ""
    out, invalid = [], 0
    for key, value in raw.items():
        if key not in clinical_values._VITAL_CLASS or type(value) not in (int, float):
            invalid += 1
            continue
        matches = [m for m in clinical_values._vital_numbers(body)
                   if clinical_values._bp_number(m[0]) == value
                   and clinical_values._nearest_vital_label(body, *m.span()) == clinical_values._VITAL_CLASS[key]]
        match = matches[0] if len(matches) == 1 else None
        if match:
            left = max((m.end() for m in _SENTENCE.finditer(body, 0, match.start())), default=0)
            right = _SENTENCE.search(body, match.end())
            span = (left, right.end() if right else len(body))
            quote = body[slice(*span)]
            subject, period = source_axes(body, *match.span())
            date_value = clinical_values.written_day(quote)
            measured = date_value[1].isoformat() if date_value and date_value[0] == "ymd" else None
            suffix = _UNIT.match(body[match.end():].lstrip())
            unit = suffix[0] if suffix else None
        else:
            span = quote = measured = unit = None
            subject = period = "unknown"
        source_ok = bool(verified and match and subject == "patient" and period in ("current", "past")
                         and quote and not _UNCERTAIN.search(quote) and not _correction_candidate(quote)
                         and not _adjacent_measurement_retraction(body, span, target=key)
                         and not ambiguous_patient_context(body, match.start()))
        out.append(_source_record(message, ref, "vital", key, value, unit, quote, span,
                                  measured_on=measured, status=period, subject=subject, verified=source_ok))
    return out, invalid



def _canonical_measurements(db, message, projection_row, cfg, patient_name):
    """Accept audited canonical measurements only under the current source and policy."""
    import inference_version
    import semantic_facts
    import semantic_policy
    import semantic_projection
    import semantic_store
    import semantic_transport
    import semantic_v4

    # Explicit mode prevents the omitted-mode API-key probe. Canonical promotion
    # validation may still read its separately owned approval receipt.
    if not isinstance(cfg, dict) or not isinstance(cfg.get("semantic"), dict) or "mode" not in cfg["semantic"]:
        return None, 0
    policy, errors = semantic_policy.runtime_config(cfg)
    if errors or policy.get("mode") == "off" or policy.get("fact_source") != "canonical":
        return None, 0
    meta = loads_dict(projection_row["meta"]) or {}
    mid, pid = message["message_id"], message["project_id"]
    binding = inference_dependency_loss.source_binding(db, pid, mid)
    if not binding or meta.get("source_binding") != binding:
        return None, 0
    bundle = semantic_store.thread_bundle(db, pid, message.get("parent_id") or mid,
                                          local_model=semantic_transport.llm_model(cfg))
    if (bundle is None or bundle.get("source_fingerprint") != meta.get("fingerprint")
            or semantic_policy.policy_fingerprint(policy) != meta.get("policy_fingerprint")):
        return None, 0
    member = next((item for item in bundle["members"] if item.get("message_id") == mid), None)
    raw, error = semantic_v4.reproject_doc(db, mid, meta)
    if member is None or error or raw is None:
        return None, 0
    try:
        clean = semantic_facts.validate_facts_doc(raw)
    except (semantic_facts.ContractError, KeyError, TypeError, ValueError):
        return None, 0
    if len(clean["facts"]) > _MAX_PER_SOURCE:
        return None, _MAX_PER_SOURCE + 1
    body = message["body_text"]
    source = clean["source"]
    if (member.get("body_state") != "full" or member.get("body_original") != body
            or member.get("revision") != message.get("content_hash")
            or source.get("message_id") != str(mid) or source.get("revision") != member.get("revision")
            or source.get("content_hash") != hashlib.sha256(body.encode("utf-8")).hexdigest()
            or source.get("body_codepoints") != len(body) or source.get("content_quality") != "full"
            or source.get("source_fingerprint") != bundle["source_fingerprint"]
            or loads_dict(projection_row["content"]) != semantic_projection.project_v2_doc_legacy(raw)):
        return None, 0
    for evidence in clean["evidence"]:
        start, end = evidence["start"], evidence["end"]
        if not 0 <= start < end <= len(body) or body[start:end] != evidence["quote"]:
            return None, 0
    # Reproduce the current_artifact selection; the newest matching row is authoritative.
    canonical_id = None
    for candidate in db.execute("SELECT artifact_id,content,meta,project_id FROM artifacts WHERE kind=? "
                                "AND message_id=? ORDER BY artifact_id DESC", ("semantic_facts_v2", mid)):
        candidate_meta = loads_dict(candidate["meta"]) or {}
        if (not inference_version.is_current(candidate_meta) or candidate_meta.get("invalidated")
                or candidate_meta.get("fingerprint") != meta.get("fingerprint")):
            continue
        if candidate["project_id"] != pid or loads_dict(candidate["content"]) != raw:
            return None, 0
        canonical_id = candidate["artifact_id"]
        break
    if canonical_id is None:
        return None, 0
    evidence_by_id = {item["evidence_id"]: item for item in clean["evidence"]}
    disputed = {rel.get(key) for rel in clean["relations"] if rel.get("type") in ("CONTRADICTION", "UNRESOLVED")
                for key in ("left_fact_id", "right_fact_id")}
    records, missing = [], 0
    for index, fact in enumerate(clean["facts"]):
        if fact.get("kind") != "vital_lab":
            continue
        if fact.get("subject") == "role:family" or str(fact.get("subject", "")).startswith("person:"):
            continue
        context = fact.get("patient_context") or {}
        details = context.get("details") if isinstance(context, dict) else None
        if (fact.get("fact_id") in disputed or fact.get("subject") != f"patient:{pid}" or fact.get("validation_status") != "verified"
                or fact.get("polarity") != "affirmed" or fact.get("epistemic") not in ("asserted", "reported")
                or fact.get("workflow_status") not in ("reported", "performed", "done")
                or fact.get("valid_time") not in ("current", "past") or not isinstance(details, list)):
            missing += 1
            continue
        values = {key: [d.get("value") for d in details if isinstance(d, dict) and d.get("key") == key]
                  for key in ("observation_name", "value", "unit", "measured_on", "condition")}
        if (any(len(values[key]) != 1 for key in ("observation_name", "value", "measured_on"))
                or any(len(values[key]) > 1 for key in ("unit", "condition"))):
            missing += 1
            continue
        name, value, date_text = (values[key][0] for key in ("observation_name", "value", "measured_on"))
        unit = values["unit"][0] if values["unit"] else None
        condition = values["condition"][0] if values["condition"] else None
        eligible = [evidence_by_id[eid] for eid in fact.get("evidence_ids", []) if eid in evidence_by_id
                    and all(isinstance(v, str) and v in evidence_by_id[eid]["quote"] for v in (name, value, date_text))
                    and (unit is None or unit in evidence_by_id[eid]["quote"])
                    and (condition is None or condition in evidence_by_id[eid]["quote"])]
        if len(eligible) != 1:
            missing += 1
            continue
        evidence = eligible[0]
        item = {"name": name, "value": value, "unit": unit, "measured_on": date_text, "condition": condition,
                "evidence": evidence["quote"], "subject": "patient", "status": fact["valid_time"]}
        produced, invalid = _labs(message, {"labs": [item]}, canonical_id, patient_name, True)
        if invalid or len(produced) != 1 or produced[0]["verification"] != "quote_supported":
            missing += 1
            continue
        produced[0]["source_ref"] = {"artifact_id": canonical_id, "field": "facts", "index": index,
                                     "evidence_id": evidence["evidence_id"]}
        records.extend(produced)
    return records, missing

def collect_measurements(db, project_id, msgs, arts, refs, *, cfg=None):
    """Bound received inputs; caller must separately bound any earlier SQL materialization.

    Records contain transient raw bodies. Caller-supplied inputs have no proven
    common snapshot, so source_complete stays false even if local checks pass.
    """
    if type(project_id) is not int or not 0 < project_id < 2**63:
        raise ValueError("project_id_invalid")
    # Iterating a caller's cursor or generator can run code or begin a write.
    # Finish it before opening a savepoint, so RELEASE cannot commit that write.
    incoming = list(itertools.islice(iter(msgs), _MAX_MESSAGES + 1))
    truncated = len(incoming) > _MAX_MESSAGES
    messages, safe_arts, safe_refs = [], {}, {}
    input_bytes = 0
    if type(arts) is not dict or type(refs) is not dict:
        arts, refs = {}, {}
        truncated = True
    for original in incoming[:_MAX_MESSAGES]:
        row = {key: original[key] for key in _MESSAGE_FIELDS if key in original.keys()}
        mid = row.get("message_id")
        if (type(mid) is not int or not 0 < mid < 2**63 or
                any(type(value) not in (type(None), str, int, float) or
                    (type(value) is int and not -(2**63) <= value < 2**63) or
                    (type(value) is float and not math.isfinite(value))
                    for value in row.values())):
            truncated = True
            continue
        sizes = [_bounded_text(row.get("body_text"), _MAX_BODY_BYTES)]
        sizes.extend(_bounded_text(value, _MAX_FIELD_BYTES) for key, value in row.items()
                     if key != "body_text" and type(value) is str)
        blobs = arts.get(mid, {})
        if (type(blobs) is not dict or len(blobs) > len(_ARTIFACT_KINDS) or
                any(type(key) is not str or key not in _ARTIFACT_KINDS for key in blobs)):
            truncated = True
            continue
        sizes.extend(_bounded_text(value, _MAX_ARTIFACT_BYTES) for value in blobs.values())
        if any(size is None for size in sizes) or input_bytes + sum(sizes) > _MAX_INPUT_BYTES:
            truncated = True
            continue
        input_bytes += sum(sizes)
        messages.append(row)
        safe_arts[mid] = blobs.copy()
        for kind in _ARTIFACT_KINDS:
            ref = refs.get((mid, kind))
            if ref is not None:
                if type(ref) is not int or not 0 < ref < 2**63:
                    truncated = True
                else:
                    safe_refs[(mid, kind)] = ref
    db.execute("SAVEPOINT measurement_sources_read")
    try:
        result = _collect_measurements(db, project_id, messages, safe_arts, safe_refs, cfg=cfg, truncated=truncated)
    except BaseException:
        try:
            db.execute("RELEASE measurement_sources_read")
        except sqlite3.Error:
            pass  # SQLite may already have rolled back the savepoint.
        raise
    else:
        db.execute("RELEASE measurement_sources_read")
        return result


def _collect_measurements(db, project_id, msgs, arts, refs, *, cfg=None, truncated=False):
    required = {
        "patients": {"project_id", "patient_name", "fetch_state", "fetch_reason", "history_floor", "initial_import", "is_archived"},
        "messages": {"message_id", "project_id", "body_text", "body_state", "content_hash", "posted_at",
                     "posted_at_ts", "parent_id", "sender_id", "sender_name", "sender_type", "profession",
                     "organization"},
        "message_revisions": {"message_id", "seq"},
        "artifacts": {"artifact_id", "project_id", "message_id", "kind", "content", "meta", "model"},
        "fetch_jobs": {"project_id", "kind", "state"},
        "attachments": {"message_id"},
    }
    columns = {table: {row[1] for row in db.execute("PRAGMA table_info(" + table + ")")}
               for table in required}
    if "project_id" in columns["patients"] and db.execute(
            "SELECT 1 FROM patients WHERE project_id=?", (project_id,)).fetchone() is None:
        raise ValueError("unknown_project")
    if any(not fields.issubset(columns[table]) for table, fields in required.items()):
        # Older snapshots can lack the capture and occurrence fences. An empty
        # result is not evidence that the patient has no measurements or gaps.
        counts = dict.fromkeys(("stored", "full", "empty", "deleted", "incomplete", "unprocessed",
                                "analysis_attention", "canonical_unverified", "canonical_vital_unprojected",
                                "invalid_items", "correction_candidates"))
        return {"records": [], "coverage": {**counts, "source_complete": False, "local_checks_complete": None,
                "ids_match": None, "acquisition_complete": False, "dependency_complete": False,
                "dependency_gaps": None, "history_basis": "schema_unavailable",
                "attachment_count": None, "truncated": truncated, "source_schema_available": False,
                "upstream_snapshot_proven": False, "scope_state": "schema_unknown"}}
    patient = db.execute("SELECT patient_name,fetch_state,fetch_reason,history_floor,is_archived "
                         "FROM patients WHERE project_id=?",
                         (project_id,)).fetchone()
    if patient is None:
        raise ValueError("unknown_project")
    if (patient["is_archived"] != 0 or patient["fetch_state"] == "forbidden"
            or patient["fetch_reason"] in ("forbidden", "permission_denied", "access_revoked")):
        counts = dict.fromkeys(("stored", "full", "empty", "deleted", "incomplete", "unprocessed",
                                "analysis_attention", "canonical_unverified", "canonical_vital_unprojected",
                                "invalid_items", "correction_candidates"))
        return {"records": [], "coverage": {**counts, "source_complete": False, "local_checks_complete": None,
                "ids_match": None, "acquisition_complete": False, "dependency_complete": False,
                "dependency_gaps": None, "history_basis": "scope_unavailable",
                "attachment_count": None, "truncated": truncated, "source_schema_available": True,
                "upstream_snapshot_proven": False, "scope_state": "scope_unavailable"}}
    messages = msgs
    supplied = {m.get("message_id") for m in messages if m.get("project_id") == project_id}
    fields = ("message_id", "project_id", "body_text", "body_state", "content_hash", "posted_at",
              "posted_at_ts", "parent_id", "sender_id", "sender_name", "sender_type")
    actual, matched_indexes = set(), set()
    match_sql = "SELECT 1 FROM messages WHERE " + " AND ".join(field + " IS ?" for field in fields)
    for index, message in enumerate(messages):
        if message.get("project_id") == project_id and db.execute(
                match_sql, tuple(message.get(field) for field in fields)).fetchone():
            actual.add(message.get("message_id"))
            matched_indexes.add(index)
    total = db.execute("SELECT COUNT(*) FROM messages WHERE project_id=?", (project_id,)).fetchone()[0]
    ids_match = not truncated and len(supplied) == len(messages) == total and supplied == actual
    cohort = None
    try:
        cohort = initial_import_state.load(db, project_id)
        cohort_ok = cohort is None or (cohort["capture_phase"] == "complete" and "capture_proof" in cohort)
    except ValueError:
        cohort_ok = False
    pending = db.execute("SELECT COUNT(*) FROM fetch_jobs WHERE project_id=? "
                         "AND kind IN ('history','thread','reply') AND state IS NOT 'done'",
                         (project_id,)).fetchone()[0]
    acquisition = (patient["fetch_state"] == "complete" and not patient["fetch_reason"]
                   and patient["history_floor"] == -1 and cohort_ok and pending == 0)
    attachments = db.execute("SELECT COUNT(*) FROM attachments a JOIN messages m ON m.message_id=a.message_id "
                             "WHERE m.project_id=?", (project_id,)).fetchone()[0]
    dependency = inference_dependency_loss.read(db, project_id)
    ref_ids = sorted({v for m in messages for kind in ("extract_v1", "extract_llm", "canonical_projection", "semantic_facts_v4")
                      if type(v := refs.get((m.get("message_id"), kind))) is int and v > 0})
    rows = db.execute("SELECT a.artifact_id,a.message_id,a.kind,a.content,a.meta,a.model FROM artifacts a "
                      "JOIN messages m ON m.message_id=a.message_id WHERE m.project_id=? "
                      "AND a.artifact_id IN (SELECT value FROM json_each(?)) " + current_extract_pred()
                      + " AND (a.kind='extract_v1' OR (1=1 " + current_fact_pred() + "))"
                      + " AND a.artifact_id=(SELECT MAX(b.artifact_id) FROM artifacts b "
                        "WHERE b.message_id=m.message_id AND b.kind=a.kind "
                      + current_extract_pred('b', 'm')
                      + " AND (b.kind IN ('extract_v1','extract_llm') OR (1=1 "
                      + current_fact_pred('b', 'm') + "))) ",
                      (project_id, json.dumps(ref_ids))).fetchall()
    selected = {r["artifact_id"]: r for r in rows}
    canonical_latest = {}
    message_ids = sorted({m.get("message_id") for m in messages if m.get("project_id") == project_id})
    for mid, kind, artifact_id in db.execute(
            "SELECT message_id,kind,MAX(artifact_id) FROM artifacts "
            "WHERE (project_id=? OR project_id IS NULL) AND message_id IN (SELECT value FROM json_each(?)) "
            "AND kind IN ('canonical_projection','semantic_facts_v4') GROUP BY message_id,kind",
            (project_id, json.dumps(message_ids))):
        canonical_latest.setdefault(mid, {})[kind] = artifact_id
    canonical_configured = (isinstance(cfg, dict) and isinstance(cfg.get("semantic"), dict)
                            and cfg["semantic"].get("fact_source") == "canonical")
    counts = dict.fromkeys(("stored", "full", "empty", "deleted", "incomplete", "unprocessed",
                            "analysis_attention", "canonical_unverified", "canonical_vital_unprojected",
                            "invalid_items", "correction_candidates"), 0)
    counts["stored"] = len(messages)
    records = []
    for index, message in enumerate(messages):
        if index not in matched_indexes:
            counts["incomplete"] += 1
            continue
        state, body = message.get("body_state"), message.get("body_text")
        if state == "deleted":
            counts["deleted"] += 1
            continue
        if state != "full" or not isinstance(body, str):
            counts["incomplete"] += 1
            continue
        if not body:
            counts["empty"] += 1
            continue
        counts["full"] += 1
        counts["correction_candidates"] += int(_correction_candidate(body))
        mid = message["message_id"]
        blobs = arts.get(mid, {}) if isinstance(arts, dict) else {}
        _v1, doc, _llm, has_fact = decode_current_extractions(blobs)
        kind = next((k for k in ("semantic_facts_v4", "canonical_projection", "extract_llm") if k in blobs), None)
        ref = refs.get((mid, kind)) if kind else None
        latest = canonical_latest.get(mid, {})
        authoritative = next((k for k in ("semantic_facts_v4", "canonical_projection") if k in latest), None)
        if (canonical_configured or authoritative) and (
                authoritative is None or kind != authoritative or ref != latest[authoritative]):
            counts["canonical_unverified"] += 1
            continue
        row = selected.get(ref)
        if not has_fact or row is None or row["message_id"] != mid or row["kind"] != kind or row["content"] != blobs.get(kind):
            counts["unprocessed"] += 1
            continue
        if isinstance(doc.get("labs"), list) and len(doc["labs"]) > _MAX_PER_SOURCE:
            truncated = True
            doc = {**doc, "labs": doc["labs"][:_MAX_PER_SOURCE]}
        if isinstance(doc.get("vitals"), dict) and len(doc["vitals"]) > _MAX_PER_SOURCE:
            truncated = True
            doc = {**doc, "vitals": dict(itertools.islice(doc["vitals"].items(), _MAX_PER_SOURCE))}
        if kind != "extract_llm":
            canonical, unprojected = _canonical_measurements(db, message, row, cfg, patient["patient_name"])
            if canonical is None:
                counts["canonical_unverified"] += 1
                truncated |= unprojected > _MAX_PER_SOURCE
                candidates = doc.get("canonical_facts")
                if isinstance(candidates, list):
                    counts["canonical_vital_unprojected"] += sum(isinstance(f, dict) and f.get("kind") == "vital_lab" for f in candidates)
                else:
                    counts["invalid_items"] += 1
            else:
                room = _MAX_RECORDS - len(records)
                records.extend(canonical[:room])
                truncated |= len(canonical) > room
                counts["canonical_vital_unprojected"] += unprojected
            continue
        meta = loads_dict(row["meta"]) or {}
        model = meta.get("actual_model")
        progress = extract_llm.progress_for_message(db, message, model=model)
        current_binding = inference_dependency_loss.source_binding(db, project_id, mid)
        verified = (isinstance(cfg, dict) and model == extract_llm.resolved_model(cfg)
                    and isinstance(current_binding, str) and meta.get("source_binding") == current_binding
                    and progress.get("state") == "complete" and row["model"] == model)
        if not verified:
            counts["analysis_attention"] += 1
        labs, bad_labs = _labs(message, doc, ref, patient["patient_name"], verified)
        vitals, bad_vitals = _vitals(message, doc, ref, verified)
        produced = labs + vitals
        room = _MAX_RECORDS - len(records)
        records.extend(produced[:room])
        truncated |= len(produced) > room
        counts["invalid_items"] += bad_labs + bad_vitals
    complete = bool(not truncated and ids_match and acquisition and not attachments and dependency["complete"] and not any(counts[key] for key in
        ("deleted", "incomplete", "unprocessed", "analysis_attention", "canonical_unverified",
         "canonical_vital_unprojected", "invalid_items", "correction_candidates")))
    return {"records": records, "coverage": {**counts, "source_complete": False, "local_checks_complete": complete,
            "ids_match": ids_match, "acquisition_complete": acquisition,
            "attachment_count": attachments,
            "truncated": truncated,
            "dependency_complete": dependency["complete"], "dependency_gaps": dependency["gaps"],
            "history_basis": "completed_cohort" if acquisition and cohort is not None else "legacy_floor" if acquisition else "incomplete",
            "source_schema_available": True, "upstream_snapshot_proven": False, "scope_state": "observed"}}
